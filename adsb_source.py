"""ADS-B feed client: fetch, rate-limit, fail over, normalise.

Talks to adsb.fi (primary) and adsb.lol (failover). Both are free, need no API
key, and speak the readsb "re-api" v2 shape. Neither is a paid product, so the
politeness rules here are load-bearing rather than decorative:

  * One shared rate limiter guards *every* request path, including the
    on-demand lookups a Discord /track command makes. If the limiter lived in
    the poller instead, a user spamming /track could burst straight through
    adsb.fi's 1 req/s ceiling and get the whole host blocked.
  * A real User-Agent, not the browser-spoof headers the LiveATC side needs.
  * Attribution is required by adsb.fi's terms; the Discord layer puts it in
    every embed.

This module holds no tracking state. It turns HTTP into a list of
``AircraftReport`` and nothing else, which is what lets the classifier and the
state machine be tested without a network.

Response-shape landmines, all confirmed against the live API:

  * The aircraft array is keyed ``"aircraft"`` on /v2/lat/lon/dist but ``"ac"``
    on /v2/mil. Both must be handled.
  * ``alt_baro`` is an int OR the literal string ``"ground"``.
  * ``dbFlags`` is a bitfield (1 military, 2 interesting, 4 PIA, 8 LADD) and is
    frequently absent. Mask with ``&``; never compare with ``==``.
  * /v2/mil records often carry no position at all — roughly a quarter of them
    at any given moment.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

import config


@dataclass(frozen=True)
class AircraftReport:
    """One aircraft as of one poll. Immutable; the tracker holds the history."""

    hex: str
    ident: str = ""          # "flight", stripped. Callsign as broadcast.
    reg: str = ""            # "r"
    typ: str = ""            # "t" — ICAO type designator
    descr: str = ""          # "desc" — human type name (adsb.fi only)
    owner: str = ""          # "ownOp" — registered operator (adsb.fi only)
    dbflags: int = 0
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: Optional[int] = None
    on_ground: bool = False
    alt_geom: Optional[int] = None
    gs_kt: Optional[float] = None
    track_deg: Optional[float] = None
    baro_rate: Optional[int] = None
    squawk: str = ""
    emergency: str = "none"
    category: str = ""
    seen: float = 0.0        # seconds since any message
    seen_pos: float = 0.0    # seconds since a positional message
    dst_nm: Optional[float] = None   # present only on radius queries
    rssi: Optional[float] = None
    mlat: bool = False
    tisb: bool = False
    source: str = ""
    received_at: float = 0.0

    @property
    def is_db_military(self) -> bool:
        return bool(self.dbflags & 1)

    @property
    def is_db_interesting(self) -> bool:
        return bool(self.dbflags & 2)

    @property
    def is_db_pia(self) -> bool:
        return bool(self.dbflags & 4)

    @property
    def is_db_ladd(self) -> bool:
        return bool(self.dbflags & 8)

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    def label(self) -> str:
        """Best available human name: callsign, else registration, else hex."""
        return self.ident or self.reg or self.hex.upper()


EMERGENCY_SQUAWKS = frozenset({"7500", "7600", "7700"})


def parse_alt(raw) -> tuple[Optional[int], bool]:
    """Normalise alt_baro. Returns (feet_or_None, on_ground).

    The feed uses the string "ground" rather than 0 for aircraft on the
    surface, so a naive int() here would throw on every taxiing aircraft.
    """
    if raw is None:
        return None, False
    if isinstance(raw, str):
        return (None, True) if raw.strip().lower() == "ground" else (None, False)
    try:
        return int(raw), False
    except (TypeError, ValueError):
        return None, False


def _f(raw) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _i(raw) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def extract_aircraft(payload) -> list:
    """Pull the aircraft array out of a response, whichever key it used."""
    if not isinstance(payload, dict):
        return []
    for key in ("aircraft", "ac"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def normalise(raw, source: str, now: float) -> Optional[AircraftReport]:
    """One raw record to an AircraftReport. None only when hex is missing.

    Never raises — a single malformed record must not take down a poll.
    """
    if not isinstance(raw, dict):
        return None
    hex_ = str(raw.get("hex") or "").strip().lower()
    if not hex_:
        return None
    try:
        alt_ft, on_ground = parse_alt(raw.get("alt_baro"))
        emergency = str(raw.get("emergency") or "none").strip().lower()
        return AircraftReport(
            hex=hex_,
            ident=str(raw.get("flight") or "").strip().upper(),
            reg=str(raw.get("r") or "").strip().upper(),
            typ=str(raw.get("t") or "").strip().upper(),
            descr=str(raw.get("desc") or "").strip(),
            owner=str(raw.get("ownOp") or "").strip(),
            dbflags=_i(raw.get("dbFlags")) or 0,
            lat=_f(raw.get("lat")),
            lon=_f(raw.get("lon")),
            alt_ft=alt_ft,
            on_ground=on_ground,
            alt_geom=_i(raw.get("alt_geom")),
            gs_kt=_f(raw.get("gs")),
            track_deg=_f(raw.get("track")),
            baro_rate=_i(raw.get("baro_rate")),
            squawk=str(raw.get("squawk") or "").strip(),
            emergency=emergency or "none",
            category=str(raw.get("category") or "").strip().upper(),
            seen=_f(raw.get("seen")) or 0.0,
            seen_pos=_f(raw.get("seen_pos")) or 0.0,
            dst_nm=_f(raw.get("dst")),
            rssi=_f(raw.get("rssi")),
            mlat=bool(raw.get("mlat")),
            tisb=bool(raw.get("tisb")),
            source=source,
            received_at=now,
        )
    except Exception:
        return None


@dataclass
class SourceHealth:
    active: str = ""
    last_ok_at: float = 0.0
    last_error: str = ""
    consecutive_errors: int = 0
    backoff_until: float = 0.0
    requests: int = 0
    failures: int = 0
    rate_limited: int = 0


# Backoff ladder, seconds. Repeated failures hold at the last value rather
# than growing without bound — this feed is worth retrying indefinitely.
_BACKOFF = (5.0, 10.0, 20.0, 40.0, 60.0)


class AdsbSource:
    """Rate-limited, failing-over HTTP client for the v2 aircraft API."""

    def __init__(
        self,
        primary: str = "",
        fallback: str = "",
        min_interval: float = 0.0,
        timeout: float = 0.0,
        session=None,
        clock=time.monotonic,
    ):
        self.primary = (primary or config.ADSB_PRIMARY_URL).rstrip("/")
        self.fallback = (fallback or config.ADSB_FALLBACK_URL).rstrip("/")
        self.min_interval = min_interval or config.ADSB_MIN_REQUEST_INTERVAL_SEC
        self.timeout = timeout or config.ADSB_TIMEOUT_SEC
        self._clock = clock
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._using_fallback = False
        self._fell_back_at = 0.0
        self._health = SourceHealth(active=self._host_name(self.primary))

    # -- naming -----------------------------------------------------------

    @staticmethod
    def _host_name(url: str) -> str:
        try:
            return url.split("//", 1)[1].split("/", 1)[0]
        except IndexError:
            return url

    def _base(self) -> str:
        return self.fallback if self._using_fallback else self.primary

    # -- transport --------------------------------------------------------

    def _throttle(self) -> None:
        """Block until min_interval has passed since the last request.

        Holds the lock across the sleep on purpose: the point is to serialise
        every caller — poller and command threads alike — onto one request
        stream, and releasing early would let them all wake up together.
        """
        now = self._clock()
        wait = self._last_request_at + self.min_interval - now
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = self._clock()

    def _fetch_json(self, base: str, path: str):
        url = f"{base}/{path.lstrip('/')}"
        resp = self._session.get(
            url,
            headers={
                "user-agent": config.ADSB_USER_AGENT,
                "accept": "application/json",
            },
            timeout=self.timeout,
        )
        if resp.status_code == 429:
            self._health.rate_limited += 1
            raise RuntimeError("429 rate limited")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.json()

    def _get(self, path: str) -> list:
        """Fetch and normalise one endpoint. Returns [] on any failure.

        Failure policy: count consecutive errors, back off, and after
        ADSB_FAILOVER_ERRORS switch hosts. Retry the primary once
        ADSB_FAILBACK_SEC has elapsed, so a transient adsb.fi outage does not
        pin us to the less-detailed fallback forever.
        """
        with self._lock:
            now = self._clock()
            if now < self._health.backoff_until:
                return []

            # Time to try coming home?
            if (
                self._using_fallback
                and now - self._fell_back_at >= config.ADSB_FAILBACK_SEC
            ):
                self._using_fallback = False
                self._health.active = self._host_name(self.primary)

            self._throttle()
            base = self._base()
            try:
                payload = self._fetch_json(base, path)
            except Exception as exc:
                return self._on_failure(exc, now)

            self._health.requests += 1
            self._health.consecutive_errors = 0
            self._health.last_ok_at = now
            self._health.last_error = ""
            self._health.active = self._host_name(base)
            source = self._health.active
            wall = time.time()
            out = []
            for raw in extract_aircraft(payload):
                rep = normalise(raw, source, wall)
                if rep is not None:
                    out.append(rep)
            return out

    def _on_failure(self, exc: Exception, now: float) -> list:
        h = self._health
        h.requests += 1
        h.failures += 1
        h.consecutive_errors += 1
        h.last_error = str(exc)[:200]
        h.backoff_until = now + _BACKOFF[min(h.consecutive_errors - 1, len(_BACKOFF) - 1)]
        if (
            not self._using_fallback
            and h.consecutive_errors >= config.ADSB_FAILOVER_ERRORS
            and self.fallback
        ):
            self._using_fallback = True
            self._fell_back_at = now
            h.active = self._host_name(self.fallback)
            # Give the new host an immediate chance rather than serving out the
            # backoff we accrued against the old one.
            h.backoff_until = now
            h.consecutive_errors = 0
        return []

    # -- endpoints --------------------------------------------------------

    def radius(self, lat: float, lon: float, nm: float) -> list:
        return self._get(f"lat/{lat:.4f}/lon/{lon:.4f}/dist/{int(nm)}")

    def military(self) -> list:
        return self._get("mil")

    def by_hex(self, hex_: str) -> list:
        return self._get(f"hex/{hex_.strip().lower()}")

    def by_callsign(self, callsign: str) -> list:
        return self._get(f"callsign/{callsign.strip().upper()}")

    def by_registration(self, reg: str) -> list:
        return self._get(f"registration/{reg.strip().upper()}")

    def by_squawk(self, squawk: str) -> list:
        return self._get(f"sqk/{squawk.strip()}")

    # -- introspection ----------------------------------------------------

    def health(self) -> SourceHealth:
        with self._lock:
            return SourceHealth(**vars(self._health))
