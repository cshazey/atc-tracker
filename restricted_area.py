"""Restricted-area activity from NAIPS — is R609 (Evans Head) hot right now?

Uses the documented `restricted-area-status` operation of Airservices' NAIPS
SOAP briefing service, the same data behind the website's "Restricted Area
Briefing" (https://www.airservicesaustralia.com/naips/Fua) — not a scrape of
that page. The WSDL is public at

    https://www.airservicesaustralia.com/naips/briefing-service?wsdl

Auth is per request: `requestor` and `password` ride as XML attributes on the
request element, so nothing here may ever log a request body. Everything that
leaves this module goes through _scrub().

How NAIPS reports state, per the XSD and live responses:

  * `type` is NOTAM, H24, HJ or Other. A NOTAM-type area (R609 is one) is
    inactive unless a NOTAM activates it.
  * Each `period` is a NOTAM'd window in which the area is *not* in its
    default state, with its own vertical limits. For a NOTAM-type area that
    means active. No periods = deactivated — the website's green cell.
  * Times are UTC with no zone suffix.

The watch loop (RestrictedAreaWatch) is kept free of Discord so the tests can
drive it with a fake clock and a fake fetch.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

ENDPOINT = "https://www.airservicesaustralia.com/naips/briefing-service"
NS = "http://www.airservicesaustralia.com/naips/xsd"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SOURCE = "atc-tracker"

# NAIPS rejects the whole request with a bare INVALID if either credential
# fails its schema. Checking locally turns that into something actionable.
_ACCOUNT_RE = re.compile(r"[A-Z0-9][A-Z0-9_.\-]{0,19}")
_PASSWORD_RE = re.compile(r"[a-zA-Z0-9]{7,20}")


class NaipsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RaPeriod:
    start: datetime          # UTC
    end: datetime            # UTC
    notam: str = ""          # "C43/26"
    lower: str = ""          # "SFC", "1500 ft", "FL220"; blank = default limits
    upper: str = ""
    comment: str = ""

    def covers(self, now: datetime) -> bool:
        return self.start <= now < self.end


@dataclass(frozen=True)
class RaStatus:
    area: str                # "R609"
    type: str                # NOTAM | H24 | HJ | Other
    activity: str = ""
    authority: str = ""
    hours: str = ""
    lower: str = ""          # default limits, often blank for NOTAM-type areas
    upper: str = ""
    periods: tuple = ()

    def active_period(self, now: datetime) -> Optional[RaPeriod]:
        return next((p for p in self.periods if p.covers(now)), None)

    def is_active(self, now: datetime) -> bool:
        # H24 is active as published (the website's yellow cell) whether or
        # not a NOTAM amends it. Everything else is active only inside a
        # NOTAM'd period — the only state R609 ever has.
        return self.type == "H24" or self.active_period(now) is not None

    def next_period(self, now: datetime) -> Optional[RaPeriod]:
        upcoming = [p for p in self.periods if p.start > now]
        return min(upcoming, key=lambda p: p.start) if upcoming else None

    def limits(self, period: Optional[RaPeriod] = None) -> str:
        lower = (period.lower if period and period.lower else self.lower) or "NOTAM"
        upper = (period.upper if period and period.upper else self.upper) or "NOTAM"
        return f"{lower} – {upper}"


# ---------------------------------------------------------------------------
# NAIPS request / response
# ---------------------------------------------------------------------------

def credentials_from_env() -> Optional[tuple[str, str]]:
    user = (os.environ.get("NAIPS_USERNAME", "") or os.environ.get("NAIPS_USER_ID", "")).strip()
    secret = os.environ.get("NAIPS_PASSWORD", "").strip()
    if not (user and secret):
        return None
    # Account ids are uppercase in NAIPS and the schema rejects anything else.
    return user.upper(), secret


def validation_error(user: str, secret: str) -> Optional[str]:
    """Why NAIPS would reject these, in words — never including the values."""
    if not _ACCOUNT_RE.fullmatch(user):
        return "NAIPS_USERNAME must be 1-20 characters of A-Z, 0-9, _ . or -"
    if not _PASSWORD_RE.fullmatch(secret):
        return ("NAIPS_PASSWORD must be 7-20 letters and digits only — the SOAP "
                "service rejects symbols even if the website accepted them")
    return None


def _scrub(text: str, user: str, secret: str) -> str:
    for value in (secret, user):
        if value:
            text = text.replace(value, "***")
    return text


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_request(areas: list[str], user: str, secret: str, hours: int = 24) -> bytes:
    # Element order matters: the BriefingRequest base (duration) precedes the
    # extension's choice (restricted-area), and NAIPS validates against the XSD.
    items = "".join(f"<restricted-area>{_escape(a)}</restricted-area>" for a in areas[:5])
    body = (
        f'<restricted-area-status-rqs xmlns="{NS}" source="{SOURCE}"'
        f' requestor="{_escape(user)}" password="{_escape(secret)}">'
        f"<duration>PT{int(hours)}H</duration>{items}</restricted-area-status-rqs>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_NS}"><soap:Body>{body}</soap:Body></soap:Envelope>'
    ).encode("utf-8")


def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child(element, name: str):
    return element.find(f"{{{NS}}}{name}") if element is not None else None


def _text(element, name: str) -> str:
    found = _child(element, name)
    return (found.text or "").strip() if found is not None else ""


def _utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _altitude(element) -> str:
    if element is None:
        return ""
    kind = (element.get("type") or "").upper()
    value = _text(element, "value")
    if kind in ("SFC", "UNL", "GND"):
        return kind
    if kind == "FL":
        return f"FL{int(value):03d}" if value.isdigit() else "FL"
    if value:
        return f"{value} ft" + (" AGL" if kind == "AGL" else "")
    return kind


def _notam_id(element) -> str:
    number = _child(element, "number")
    if number is None:
        return ""
    series = _text(number, "series-id")
    num = _text(number, "number")
    year = _text(number, "year")
    return f"{series}{num}/{year[-2:]}" if num else ""


def _area_name(full_id: str) -> str:
    # NAIPS answers with "R609-YBBB-R" (area, FIR, type); callers asked for "R609".
    return full_id.split("-", 1)[0].upper()


def parse_response(xml_text: str) -> dict[str, RaStatus]:
    """Parse a restricted-area-status response into {area: RaStatus}."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise NaipsError(f"NAIPS returned unparseable XML: {exc}") from None

    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        raise NaipsError(f"NAIPS fault: {(fault.findtext('faultstring') or 'unspecified').strip()}")

    rsp = next((e for e in root.iter() if _tag(e) == "restricted-area-status-rsp"), None)
    if rsp is None:
        raise NaipsError("NAIPS response had no restricted-area-status-rsp")
    status = (rsp.get("status") or "").upper()
    if status != "SUCCESS":
        detail = _text(rsp, "info") or "no detail"
        raise NaipsError(f"NAIPS status {status or '?'}: {detail}")

    out: dict[str, RaStatus] = {}
    for group in rsp.findall(f"{{{NS}}}group"):
        for st in group.findall(f"{{{NS}}}status"):
            limits = _child(st, "limits")
            periods = []
            for p in st.findall(f"{{{NS}}}period"):
                window = _child(p, "period")
                start, end = _utc(_text(window, "from")), _utc(_text(window, "till"))
                if start is None or end is None:
                    continue
                plim = _child(p, "limits")
                periods.append(RaPeriod(
                    start=start, end=end,
                    notam=_notam_id(_child(p, "notam-id")),
                    lower=_altitude(_child(plim, "lower")),
                    upper=_altitude(_child(plim, "upper")),
                    comment=_text(p, "comment"),
                ))
            name = _area_name(st.get("id", ""))
            out[name] = RaStatus(
                area=name,
                type=_text(st, "type") or "Other",
                activity=_text(st, "activity"),
                authority=_text(st, "authority"),
                hours=_text(st, "hours"),
                lower=_altitude(_child(limits, "lower")),
                upper=_altitude(_child(limits, "upper")),
                periods=tuple(sorted(periods, key=lambda x: x.start)),
            )
    return out


def _short_error(exc: Exception) -> str:
    """Boil urllib3's nested retry message down to the part a human needs."""
    text = str(exc)
    if "NameResolutionError" in text or "Failed to resolve" in text:
        return "DNS lookup for www.airservicesaustralia.com failed (network/DNS outage on this host)"
    if isinstance(exc, requests.Timeout):
        return "request timed out"
    m = re.search(r"Caused by (\w+)\(", text)
    return m.group(1) if m else text[:200]


def fetch_status(
    areas: list[str],
    credentials: Optional[tuple[str, str]] = None,
    timeout: float = 30.0,
    retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, RaStatus]:
    """Ask NAIPS for the next 24 h of status for up to five restricted areas.

    Connection-level failures (DNS lookups, resets, timeouts) are retried after
    each of `retry_delays` — the host's resolver drops out for a few seconds at
    a time, and one blip shouldn't fail the check. Raises NaipsError with a
    scrubbed message on any failure that survives the retries.
    """
    credentials = credentials or credentials_from_env()
    if credentials is None:
        raise NaipsError("NAIPS credentials not configured (NAIPS_USERNAME / NAIPS_PASSWORD)")
    user, secret = credentials
    invalid = validation_error(user, secret)
    if invalid:
        raise NaipsError(invalid)
    body = build_request(areas, user, secret)
    for attempt in range(len(retry_delays) + 1):
        try:
            # requests verifies TLS against certifi. Never disable that here — the
            # request body carries the NAIPS password.
            resp = requests.post(
                ENDPOINT,
                data=body,
                headers={"Content-Type": "text/xml; charset=utf-8",
                         "SOAPAction": "restricted-area-status"},
                timeout=timeout,
            )
            break
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt < len(retry_delays):
                sleep(retry_delays[attempt])
                continue
            raise NaipsError(_scrub(f"NAIPS unreachable: {_short_error(exc)}", user, secret)) from None
        except requests.RequestException as exc:
            raise NaipsError(_scrub(f"NAIPS unreachable: {exc}", user, secret)) from None
    if resp.status_code != 200 and "Envelope" not in resp.text[:500]:
        raise NaipsError(_scrub(f"NAIPS HTTP {resp.status_code}: {resp.text[:300]}", user, secret))
    try:
        return parse_response(resp.text)
    except NaipsError as exc:
        raise NaipsError(_scrub(str(exc), user, secret)) from None


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def parse_window(spec: str) -> tuple[dtime, dtime]:
    """"07:00-19:00" -> (07:00, 19:00). Falls back to 07-19 on garbage."""
    try:
        a, b = spec.split("-")
        start = dtime(*(int(x) for x in a.strip().split(":")))
        end = dtime(*(int(x) for x in b.strip().split(":")))
        if start < end:
            return start, end
    except (ValueError, TypeError):
        pass
    return dtime(7, 0), dtime(19, 0)


# Event kinds handed to on_event.
EV_ACTIVATED = "activated"
EV_DEACTIVATED = "deactivated"
EV_WINDOW_CLOSED = "window_closed"   # watch hours ended while still active


class RestrictedAreaWatch:
    """Polls one restricted area inside the daily watch window.

    Idle it checks every ``idle_interval`` seconds. Once it sees the area go
    active it tightens to ``active_interval`` (15 min) until it sees it
    deactivate. It also wakes just after any scheduled start or end it already
    knows about from the NOTAM, so a transition is reported within a minute or
    so of the published time rather than up to a full interval late.

    Last-known state is persisted so a restart does not re-announce an
    activation that was already alerted.
    """

    def __init__(
        self,
        area: str,
        fetch: Callable[[], RaStatus],
        on_event: Callable[[str, RaStatus, datetime, dict], None],
        on_status: Callable[[RaStatus, bool, datetime, datetime], None] = lambda *a: None,
        on_error: Callable[[str], None] = lambda msg: None,
        tz: str = "Australia/Brisbane",
        window: tuple[dtime, dtime] = (dtime(7, 0), dtime(19, 0)),
        idle_interval: float = 1800.0,
        active_interval: float = 900.0,
        error_retry: float = 300.0,
        error_threshold: int = 3,
        state_path: str = "",
    ):
        self.area = area.upper()
        self._fetch = fetch
        self._on_event = on_event
        self._on_status = on_status
        self._on_error = on_error
        self._tz = ZoneInfo(tz)
        self.window = window
        self.idle_interval = idle_interval
        self.active_interval = active_interval
        self.error_retry = error_retry
        self.error_threshold = max(1, error_threshold)
        self._error_count = 0
        self._state_path = state_path
        self._lock = threading.Lock()
        self.last_status: Optional[RaStatus] = None
        self.last_checked: Optional[datetime] = None
        self.last_error: str = ""
        self._error_reported = False
        self.active: Optional[bool] = None
        self.active_since: Optional[datetime] = None
        self._window_close_sent = ""
        self._load_state()

    # -- persistence -------------------------------------------------------

    def _load_state(self) -> None:
        if not self._state_path:
            return
        try:
            with open(self._state_path) as fh:
                data = json.load(fh).get(self.area, {})
        except (OSError, ValueError):
            return
        self.active = data.get("active")
        since = data.get("active_since")
        self.active_since = datetime.fromisoformat(since) if since else None
        self._window_close_sent = data.get("window_close_sent", "")

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            try:
                with open(self._state_path) as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            data[self.area] = {
                "active": self.active,
                "active_since": self.active_since.isoformat() if self.active_since else None,
                "window_close_sent": self._window_close_sent,
            }
            tmp = f"{self._state_path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    # -- scheduling --------------------------------------------------------

    def in_window(self, now: datetime) -> bool:
        local = now.astimezone(self._tz).time()
        return self.window[0] <= local < self.window[1]

    def _next_window_start(self, now: datetime) -> datetime:
        local = now.astimezone(self._tz)
        start = local.replace(hour=self.window[0].hour, minute=self.window[0].minute,
                              second=0, microsecond=0)
        if start <= local:
            start += timedelta(days=1)
        return start.astimezone(timezone.utc)

    def _window_end(self, now: datetime) -> datetime:
        local = now.astimezone(self._tz)
        end = local.replace(hour=self.window[1].hour, minute=self.window[1].minute,
                            second=0, microsecond=0)
        return end.astimezone(timezone.utc)

    def _next_check_delay(self, status: RaStatus, active: bool, now: datetime) -> float:
        delay = self.active_interval if active else self.idle_interval
        # Wake shortly after the next boundary NAIPS already told us about.
        boundaries = []
        current = status.active_period(now)
        if current:
            boundaries.append(current.end)
        upcoming = status.next_period(now)
        if upcoming:
            boundaries.append(upcoming.start)
        for b in boundaries:
            seconds = (b - now).total_seconds() + 60
            if 0 < seconds < delay:
                delay = seconds
        # Don't sleep past the end of the watch window; the close is an event.
        until_close = (self._window_end(now) - now).total_seconds() + 1
        if 0 < until_close < delay:
            delay = until_close
        return max(60.0, delay)

    # -- one tick ----------------------------------------------------------

    def check_now(self, now: Optional[datetime] = None) -> RaStatus:
        """Fetch without touching alert state — for the /ra command."""
        status = self._fetch()
        with self._lock:
            self.last_status = status
            self.last_checked = now or datetime.now(timezone.utc)
        return status

    def step(self, now: Optional[datetime] = None) -> float:
        """Run one check if due; return seconds until the next one."""
        now = now or datetime.now(timezone.utc)

        if not self.in_window(now):
            today = now.astimezone(self._tz).strftime("%Y-%m-%d")
            if (self.active and self.last_status is not None
                    and now.astimezone(self._tz).time() >= self.window[1]
                    and self._window_close_sent != today):
                self._window_close_sent = today
                self._save_state()
                self._on_event(EV_WINDOW_CLOSED, self.last_status, now,
                               {"since": self.active_since})
            wait = (self._next_window_start(now) - now).total_seconds()
            # Cap the sleep so a suspended laptop or DST edge self-corrects.
            return max(60.0, min(wait, 3600.0))

        try:
            status = self._fetch()
        except Exception as exc:
            self.last_error = str(exc)
            self._error_count += 1
            # Brief outages are absorbed silently; only a failure that
            # persists across `error_threshold` checks is worth a message.
            if not self._error_reported and self._error_count >= self.error_threshold:
                self._error_reported = True
                self._on_error(self.last_error)
            return self.error_retry
        if self._error_reported:
            self._on_error("")   # recovered
        self._error_reported = False
        self._error_count = 0
        self.last_error = ""

        active = status.is_active(now)
        with self._lock:
            previous = self.active
            self.last_status = status
            self.last_checked = now

        if active and previous is not True:
            self.active = True
            self.active_since = now
            self._save_state()
            self._on_event(EV_ACTIVATED, status, now, {"since": now})
        elif not active and previous is True:
            since = self.active_since
            self.active = False
            self.active_since = None
            self._save_state()
            self._on_event(EV_DEACTIVATED, status, now, {"since": since})
        elif previous is None:
            self.active = active
            self._save_state()

        delay = self._next_check_delay(status, active, now)
        self._on_status(status, active, now, now + timedelta(seconds=delay))
        return delay

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                delay = self.step()
            except Exception as exc:   # never let the thread die
                self._on_error(f"watch loop error: {exc}")
                delay = self.error_retry
            if stop_event.wait(delay):
                return
