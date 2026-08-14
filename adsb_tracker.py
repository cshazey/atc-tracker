"""Per-aircraft state machine and the polling thread that drives it.

The hard problem here is not fetching data, it is not alerting on nothing.

Low-flying aircraft over water sit at the edge of ground-receiver coverage and
drop in and out of the feed constantly. A naive "was here, now gone" alert
produces a transponder-off/transponder-on pair every twenty seconds for a
display aircraft — which is exactly the aircraft you most want to hear about,
and exactly the message you would immediately mute.

The answer is a four-state presence machine with an explicit intermediate:

    (untracked) --first report--> SEEDING --confirmed--> LIVE
                                                          |  absent > FADE_SEC
                     +---- any report (silent) -------> FADING
                     |                                    |  absent > LOST_SEC
                     +--------------------------------> LOST --> pruned

Alerts fire on exactly two transitions: SEEDING->LIVE (APPEARED) and
FADING->LOST (DISAPPEARED). LIVE->FADING and FADING->LIVE are silent. A target
flickering at the edge of coverage therefore oscillates between LIVE and FADING
and emits nothing at all, while a genuine transponder shutdown walks all the
way to LOST and reports once.

Everything else in here is a variation on the same theme: box occupancy uses a
Schmitt trigger, phase changes need consecutive confirmations, and a token
bucket caps how much can reach Discord in any one minute.

Structure: run() owns threads, sleeps and network. poll_once() is a pure
function of (reports, now) -> events. All the interesting logic lives in the
latter, which is why the tests can drive the whole machine with a fake clock
and a list of synthetic records.
"""

from __future__ import annotations

import argparse
import collections
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import adsb_classify
import adsb_source
import adsb_store
import config
import geo

# Presence states.
ST_SEEDING = "seeding"
ST_LIVE = "live"
ST_FADING = "fading"
ST_LOST = "lost"

# Flight phases, relative to the home aerodrome.
PH_UNKNOWN = "unknown"
PH_GROUND = "ground"
PH_DEPARTING = "departing"
PH_AIRBORNE = "airborne"
PH_ARRIVING = "arriving"
PH_LANDED = "landed"

# Event kinds.
EV_APPEARED = "appeared"
EV_DISAPPEARED = "disappeared"
EV_BOX_ENTER = "box_enter"
EV_BOX_EXIT = "box_exit"
EV_DEPARTURE = "departure"
EV_INBOUND = "inbound"
EV_LANDED = "landed"
EV_EMERGENCY = "emergency"
EV_POSITION = "position"

# Alert tiers, mirroring atc_tracker's. Duplicated rather than imported because
# atc_tracker pulls in mlx/miniaudio and this module must stay importable
# without them (the CLI below runs on a bare interpreter).
TIER_NONE = 0
TIER_INTEREST = 1
TIER_EMERGENCY = 2

EMERGENCY_SQUAWKS = frozenset({"7500", "7600", "7700"})
_SQUAWK_MEANING = {
    "7500": "unlawful interference (hijack)",
    "7600": "radio failure",
    "7700": "general emergency",
}

_TZ = ZoneInfo(config.TIMEZONE)


@dataclass
class TrackEvent:
    kind: str
    track: "Track"
    at: float
    tier: int = TIER_NONE
    headline: str = ""
    detail: str = ""
    fields: list = field(default_factory=list)
    military: bool = False

    @property
    def hex(self) -> str:
        return self.track.hex


@dataclass
class Track:
    """Everything known about one aircraft, across polls."""

    hex: str
    ident: str = ""
    reg: str = ""
    typ: str = ""
    descr: str = ""
    owner: str = ""
    dbflags: int = 0
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: Optional[int] = None
    on_ground: bool = False
    gs_kt: Optional[float] = None
    track_deg: Optional[float] = None
    baro_rate: Optional[int] = None
    squawk: str = ""
    emergency: str = "none"
    category: str = ""
    source: str = ""

    first_seen: float = 0.0
    last_seen: float = 0.0
    seen_count: int = 0
    confirm_count: int = 0
    state: str = ST_SEEDING
    suppress_appear: bool = False

    in_box: bool = False
    box_since: Optional[float] = None
    box_pending: Optional[bool] = None
    box_pending_since: float = 0.0

    phase: str = PH_UNKNOWN
    phase_pending: str = ""
    phase_pending_count: int = 0

    home_dist_nm: Optional[float] = None
    home_bearing: Optional[float] = None

    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=60))
    classification: adsb_classify.Classification = adsb_classify.NOT_INTERESTING

    last_position_alert_at: float = 0.0
    last_alert_alt: Optional[int] = None
    last_alert_track: Optional[float] = None
    emergency_clear_polls: int = 0
    in_emergency: bool = False

    # -- derived ----------------------------------------------------------

    def label(self) -> str:
        return self.ident or self.reg or self.hex.upper()

    def type_label(self) -> str:
        return self.typ or "?"

    def globe_url(self) -> str:
        return f"https://globe.adsbexchange.com/?icao={self.hex}"

    def closing_kt(self) -> Optional[float]:
        samples = [(t, d) for (t, d, _a) in self.history if d is not None]
        if len(samples) < 2:
            return None
        return geo.closing_rate_kt(samples[-3:] if len(samples) >= 3 else samples)

    def eta_sec(self) -> Optional[float]:
        closing = self.closing_kt()
        if not closing or closing <= 0 or self.home_dist_nm is None:
            return None
        eta = self.home_dist_nm / (closing / 3600.0)
        return eta if 0 < eta <= 3600 else None

    def position_text(self) -> str:
        bits = []
        if self.alt_ft is not None:
            bits.append("on the ground" if self.on_ground else f"{self.alt_ft:,} ft")
        elif self.on_ground:
            bits.append("on the ground")
        if self.gs_kt:
            bits.append(f"{self.gs_kt:.0f} kt")
        if self.home_dist_nm is not None and self.home_bearing is not None:
            bits.append(
                f"{self.home_dist_nm:.0f} nm {geo.compass_point(self.home_bearing)} "
                f"of {config.ADSB_HOME_ICAO}"
            )
        return " · ".join(bits)

    def as_dict(self) -> dict:
        return {
            "hex": self.hex,
            "ident": self.ident,
            "reg": self.reg,
            "type": self.typ,
            "desc": self.descr,
            "owner": self.owner,
            # The map needs this: when a type designator has no silhouette of
            # its own, the ADS-B emitter category picks the fallback shape.
            "category": self.category,
            "lat": self.lat,
            "lon": self.lon,
            "alt_ft": self.alt_ft,
            "on_ground": self.on_ground,
            "gs_kt": self.gs_kt,
            "track_deg": self.track_deg,
            "baro_rate": self.baro_rate,
            "squawk": self.squawk,
            "emergency": self.emergency,
            "state": self.state,
            "phase": self.phase,
            "in_box": self.in_box,
            "dist_nm": self.home_dist_nm,
            "bearing": self.home_bearing,
            "military": self.classification.military,
            "probable": self.classification.probable,
            "confidence": self.classification.confidence,
            "title": self.classification.title,
            "reasons": self.classification.reason_text(),
            "last_seen": self.last_seen,
            "seen_count": self.seen_count,
            "url": self.globe_url(),
        }


def _now_aest(ts: float) -> str:
    return datetime.fromtimestamp(ts, _TZ).strftime("%H:%M:%S AEST")


def in_quiet_hours(when: float, spec: str = "") -> bool:
    """Is `when` inside a "HH:MM-HH:MM" local window? Handles midnight wrap."""
    spec = (spec if spec is not None else config.ADSB_QUIET_HOURS).strip()
    if not spec or "-" not in spec:
        return False
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except ValueError:
        return False
    local = datetime.fromtimestamp(when, _TZ)
    minutes = local.hour * 60 + local.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # wraps midnight


def _seen_recently(last: Optional[float], now: float) -> bool:
    """Was this aircraft seen within ADSB_APPEAR_GAP_MIN of now?

    A negative delta means the stored timestamp is in the future — a clock
    adjustment, or a database carried over from another machine. Treat that as
    "not seen recently" rather than as an enormous gap in our favour, so a bad
    timestamp cannot silently suppress appearances until it passes.
    """
    if not last:
        return False
    delta = now - last
    return 0 <= delta < config.ADSB_APPEAR_GAP_MIN * 60


class _TokenBucket:
    """Caps sustained alert rate while letting a short burst through.

    The Discord outbox drains at roughly four messages a second, shared with
    the transcript feed. An airshow launch is twenty aircraft in three minutes;
    without a cap here the ADS-B side would push transcripts down the queue,
    which is the exact failure the outbox exists to prevent.
    """

    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.tokens = float(self.capacity)
        self.rate = self.capacity / 60.0
        self.last = 0.0
        self.suppressed = 0

    def take(self, now: float) -> bool:
        if self.last:
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        self.suppressed += 1
        return False


class AdsbPoller:
    """Owns the track store, the state machine, and the polling thread."""

    def __init__(
        self,
        stop_event: Optional[threading.Event] = None,
        on_event: Optional[Callable] = None,
        on_board: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
        source=None,
        store=None,
        clock: Callable[[], float] = time.time,
    ):
        self.stop_event = stop_event or threading.Event()
        self._on_event = on_event
        self._on_board = on_board
        self._on_log = on_log
        self.source = source if source is not None else adsb_source.AdsbSource()
        # store=False means "explicitly none" (the tests use it); store=None
        # means "give me the default".
        self.store = adsb_store.AdsbStore() if store is None else (store or None)
        self._clock = clock

        self.tracks: dict = {}
        self._lock = threading.RLock()
        self.enabled = True
        self.polls = 0
        self.last_poll_at = 0.0
        self.last_mil_sweep_at = 0.0
        self.events_emitted = 0
        self._bucket = _TokenBucket(config.ADSB_MAX_ALERTS_PER_MIN)
        self.watch_hex = set(config.ADSB_WATCH_HEX)
        self.watch_callsign = set(config.ADSB_WATCH_CALLSIGN)
        self._box_poly = config.ADSB_BOX_POLY
        self._box_poly_out = (
            geo.inflate_polygon(self._box_poly, config.ADSB_BOX_HYST_NM)
            if self._box_poly
            else None
        )
        self._box_out = geo.inflate_bbox(config.ADSB_BOX, config.ADSB_BOX_HYST_NM)

    # -- geometry ---------------------------------------------------------

    def _inside_box(self, lat, lon, strict: bool) -> bool:
        """strict=True tests the real box; False tests the inflated one.

        Two boundaries make the dead band: a track is only "in" once it clears
        the inner edge and only "out" once it clears the outer one, so sitting
        on the line cannot produce an enter/exit stream.
        """
        if lat is None or lon is None:
            return False
        if self._box_poly:
            poly = self._box_poly if strict else self._box_poly_out
            return geo.point_in_polygon(lat, lon, poly)
        box = config.ADSB_BOX if strict else self._box_out
        return geo.point_in_bbox(lat, lon, box)

    # -- the state machine ------------------------------------------------

    def poll_once(self, reports: list, now: float) -> list:
        """Fold one poll's reports into the track store and return events.

        Pure with respect to the outside world: no network, no Discord, no
        clock reads. Everything the tests need to drive is a parameter.
        """
        events: list = []
        with self._lock:
            self.polls += 1
            self.last_poll_at = now
            seeding = self.polls <= config.ADSB_SEED_POLLS

            seen_now = set()
            for rep in reports:
                if not rep.hex:
                    continue
                seen_now.add(rep.hex)
                events.extend(self._update_track(rep, now, seeding))

            events.extend(self._age_missing(seen_now, now))
            self._prune(now)
        return events

    def _update_track(self, rep, now: float, seeding: bool) -> list:
        events = []
        track = self.tracks.get(rep.hex)
        fresh = track is None

        if fresh:
            track = Track(hex=rep.hex, first_seen=now)
            # Cold start: the first polls after boot would otherwise report
            # every aircraft in the sky as newly appeared. Seed them silently.
            # A restart is handled the same way via store.last_seen_bulk().
            if seeding:
                track.state = ST_LIVE
                track.suppress_appear = True
            elif self.store is not None and _seen_recently(
                self.store.last_seen(rep.hex), now
            ):
                # Known to us from before this process started, and recently
                # enough that this is a restart rather than a new arrival.
                track.state = ST_LIVE
                track.suppress_appear = True
            self.tracks[rep.hex] = track

        was_state = track.state
        self._absorb(track, rep, now)

        # Presence transitions.
        if track.state in (ST_FADING, ST_LOST):
            # Silent recovery. A target that came back before we called it lost
            # was a coverage hole, not an event.
            track.state = ST_LIVE
        elif track.state == ST_SEEDING:
            track.confirm_count += 1
            if track.confirm_count >= config.ADSB_APPEAR_CONFIRM_POLLS:
                track.state = ST_LIVE
                if not track.suppress_appear:
                    ev = self._appeared_event(track, now)
                    if ev:
                        events.append(ev)
                track.suppress_appear = False

        if was_state == ST_LOST and track.state == ST_LIVE:
            # Came back after we announced the loss. Not an alert on its own —
            # the board will show it again, and _appeared_event's gap rule
            # decides whether a genuine re-appearance is worth reporting.
            pass

        events.extend(self._check_emergency(track, now))
        events.extend(self._check_box(track, now))
        events.extend(self._check_phase(track, now))
        events.extend(self._check_position_update(track, now))
        return events

    def _absorb(self, track: Track, rep, now: float) -> None:
        """Copy a report onto a track and recompute derived fields."""
        track.ident = rep.ident or track.ident
        track.reg = rep.reg or track.reg
        track.typ = rep.typ or track.typ
        track.descr = rep.descr or track.descr
        track.owner = rep.owner or track.owner
        track.dbflags = rep.dbflags or track.dbflags
        track.source = rep.source or track.source
        track.squawk = rep.squawk or track.squawk
        track.emergency = rep.emergency
        track.category = rep.category or track.category
        track.gs_kt = rep.gs_kt
        track.track_deg = rep.track_deg
        track.baro_rate = rep.baro_rate
        track.on_ground = rep.on_ground
        if rep.alt_ft is not None or rep.on_ground:
            track.alt_ft = 0 if rep.on_ground else rep.alt_ft
        track.last_seen = now
        track.seen_count += 1

        if rep.has_position:
            track.lat, track.lon = rep.lat, rep.lon
            track.home_dist_nm = geo.haversine_nm(
                config.ADSB_HOME_LAT, config.ADSB_HOME_LON, rep.lat, rep.lon
            )
            track.home_bearing = geo.bearing_deg(
                config.ADSB_HOME_LAT, config.ADSB_HOME_LON, rep.lat, rep.lon
            )
            track.history.append((now, track.home_dist_nm, track.alt_ft))

        strict_in = self._inside_box(track.lat, track.lon, strict=True)
        track.classification = adsb_classify.classify(
            rep,
            in_box=strict_in,
            watch_hex=self.watch_hex,
            watch_callsign=self.watch_callsign,
        )

    # -- individual checks ------------------------------------------------

    def _appeared_event(self, track: Track, now: float) -> Optional[TrackEvent]:
        """Only report an appearance that is not just a coverage gap closing."""
        if self.store is not None and _seen_recently(
            self.store.last_seen(track.hex), now
        ):
            return None
        if not track.classification.interesting:
            return None
        return TrackEvent(
            kind=EV_APPEARED,
            track=track,
            at=now,
            tier=TIER_INTEREST,
            military=track.classification.military,
            headline=f"Transponder on — {track.label()}",
            detail=track.position_text(),
        )

    def _check_emergency(self, track: Track, now: float) -> list:
        emergency = (
            track.squawk in EMERGENCY_SQUAWKS
            or track.emergency not in ("", "none")
        )
        if not emergency:
            # Require several clean polls before re-arming, so one garbled
            # squawk decode cannot produce a stream of alerts.
            if track.in_emergency:
                track.emergency_clear_polls += 1
                if track.emergency_clear_polls >= 3:
                    track.in_emergency = False
                    track.emergency_clear_polls = 0
            return []
        track.emergency_clear_polls = 0
        if track.in_emergency:
            return []
        track.in_emergency = True
        reason = _SQUAWK_MEANING.get(track.squawk, track.emergency)
        return [
            TrackEvent(
                kind=EV_EMERGENCY,
                track=track,
                at=now,
                tier=TIER_EMERGENCY,
                military=track.classification.military,
                headline=f"EMERGENCY — {track.label()} squawking {track.squawk or track.emergency}",
                detail=f"{reason} · {track.position_text()}",
            )
        ]

    def _check_box(self, track: Track, now: float) -> list:
        if track.lat is None:
            return []
        if track.in_box:
            # Only leaves once clear of the inflated boundary.
            target = not self._inside_box(track.lat, track.lon, strict=False)
        else:
            target = self._inside_box(track.lat, track.lon, strict=True)
        want = (not track.in_box) if target else track.in_box

        if want == track.in_box:
            track.box_pending = None
            return []

        # Candidate change — must hold for BOX_DWELL_SEC.
        if track.box_pending != want:
            track.box_pending = want
            track.box_pending_since = now
            return []
        if now - track.box_pending_since < config.ADSB_BOX_DWELL_SEC:
            return []

        track.in_box = want
        track.box_pending = None
        if want:
            track.box_since = now
            return [
                TrackEvent(
                    kind=EV_BOX_ENTER,
                    track=track,
                    at=now,
                    tier=TIER_INTEREST,
                    military=track.classification.military,
                    headline=f"Entered display box — {track.label()}",
                    detail=track.position_text(),
                )
            ]
        dwell = now - (track.box_since or now)
        track.box_since = None
        return [
            TrackEvent(
                kind=EV_BOX_EXIT,
                track=track,
                at=now,
                tier=TIER_NONE,
                military=track.classification.military,
                headline=f"Left display box — {track.label()}",
                detail=f"was in the box {dwell / 60:.0f} min · {track.position_text()}",
            )
        ]

    def _propose_phase(self, track: Track, phase: str) -> bool:
        """Advance the confirm counter; True once the phase should commit."""
        if phase == track.phase:
            track.phase_pending = ""
            track.phase_pending_count = 0
            return False
        if track.phase_pending != phase:
            track.phase_pending = phase
            track.phase_pending_count = 1
            return False
        track.phase_pending_count += 1
        return track.phase_pending_count >= config.ADSB_PHASE_CONFIRM_POLLS

    def _check_phase(self, track: Track, now: float) -> list:
        dist = track.home_dist_nm
        if dist is None:
            return []
        agl = None
        if track.alt_ft is not None:
            agl = track.alt_ft - config.ADSB_HOME_ELEV_FT
        closing = track.closing_kt()

        target = track.phase
        if track.on_ground and dist < 3:
            target = PH_LANDED if track.phase == PH_ARRIVING else PH_GROUND
        elif (
            track.phase in (PH_GROUND, PH_UNKNOWN, PH_LANDED)
            and agl is not None
            and agl > config.ADSB_DEP_ALT_FT
            and dist < config.ADSB_DEP_RANGE_NM
            and (closing is not None and closing < 0)
        ):
            target = PH_DEPARTING
        elif track.phase == PH_DEPARTING and (
            (agl is not None and agl > 3000) or dist > 10
        ):
            target = PH_AIRBORNE
        elif (
            track.phase in (PH_AIRBORNE, PH_UNKNOWN)
            and dist < config.ADSB_ARR_RANGE_NM
            and agl is not None
            and agl < config.ADSB_ARR_ALT_FT
            and (track.baro_rate or 0) < -200
            and (closing is not None and closing > config.ADSB_ARR_CLOSING_KT)
        ):
            target = PH_ARRIVING
        elif track.phase == PH_ARRIVING and (
            closing is not None and closing < 20
        ):
            target = PH_AIRBORNE  # missed approach, or it was only passing by

        if not self._propose_phase(track, target):
            return []

        previous = track.phase
        track.phase = target
        track.phase_pending = ""
        track.phase_pending_count = 0

        interesting_civil = adsb_classify.is_reportable_civil_track(track)
        if not (track.classification.interesting or interesting_civil):
            return []

        if target == PH_DEPARTING:
            return [
                TrackEvent(
                    kind=EV_DEPARTURE,
                    track=track,
                    at=now,
                    tier=TIER_INTEREST if track.classification.military else TIER_NONE,
                    military=track.classification.military,
                    headline=f"Departed {config.ADSB_HOME_ICAO} — {track.label()}",
                    detail=track.position_text(),
                )
            ]
        if target == PH_ARRIVING:
            eta = track.eta_sec()
            eta_text = f" · ETA ~{eta / 60:.0f} min" if eta else ""
            return [
                TrackEvent(
                    kind=EV_INBOUND,
                    track=track,
                    at=now,
                    tier=TIER_INTEREST if track.classification.military else TIER_NONE,
                    military=track.classification.military,
                    headline=f"Inbound {config.ADSB_HOME_ICAO} — {track.label()}",
                    detail=f"{track.position_text()}{eta_text}",
                )
            ]
        if target == PH_LANDED and previous == PH_ARRIVING:
            return [
                TrackEvent(
                    kind=EV_LANDED,
                    track=track,
                    at=now,
                    tier=TIER_NONE,
                    military=track.classification.military,
                    headline=f"Landed {config.ADSB_HOME_ICAO} — {track.label()}",
                    detail=track.position_text(),
                )
            ]
        return []

    def _check_position_update(self, track: Track, now: float) -> list:
        """Periodic position refresh for tracked military aircraft only."""
        if not track.classification.military:
            return []
        # Only for confirmed tracks. A contact still being confirmed has not
        # earned a position post, and emitting one would announce an aircraft
        # the presence machine has deliberately decided to stay quiet about.
        if track.state != ST_LIVE:
            return []
        interval = (
            config.ADSB_POS_UPDATE_SEC_BOX if track.in_box else config.ADSB_POS_UPDATE_SEC
        )
        due = now - track.last_position_alert_at >= interval
        moved = False
        if track.last_alert_alt is not None and track.alt_ft is not None:
            moved = abs(track.alt_ft - track.last_alert_alt) >= 3000
        if not moved and track.last_alert_track is not None and track.track_deg is not None:
            moved = abs(geo.angle_diff(track.track_deg, track.last_alert_track)) >= 60
        if not (due or (moved and now - track.last_position_alert_at > 60)):
            return []
        if track.last_position_alert_at == 0.0:
            # First sighting is already covered by APPEARED; do not double up.
            track.last_position_alert_at = now
            track.last_alert_alt = track.alt_ft
            track.last_alert_track = track.track_deg
            return []
        track.last_position_alert_at = now
        track.last_alert_alt = track.alt_ft
        track.last_alert_track = track.track_deg
        return [
            TrackEvent(
                kind=EV_POSITION,
                track=track,
                at=now,
                tier=TIER_NONE,
                military=True,
                headline=f"{track.label()} — position",
                detail=track.position_text(),
            )
        ]

    def _age_missing(self, seen_now: set, now: float) -> list:
        events = []
        for track in self.tracks.values():
            if track.hex in seen_now or track.state == ST_LOST:
                continue
            gone = now - track.last_seen
            if track.state == ST_SEEDING:
                # Confirmation requires CONSECUTIVE polls, so a missed poll
                # resets the count. Without this an aircraft flickering at the
                # edge of coverage accumulates confirmations across the gaps
                # and eventually announces itself — which is precisely the
                # alert this whole state machine exists to avoid. It stays in
                # the store and on the board meanwhile; it just never fires.
                track.confirm_count = 0
                continue
            if track.state == ST_LIVE and gone >= config.ADSB_FADE_SEC:
                track.state = ST_FADING
                continue
            if track.state != ST_FADING:
                continue
            lost_after = (
                config.ADSB_LOST_SEC_BOX if track.in_box else config.ADSB_LOST_SEC
            )
            if gone < lost_after:
                continue
            track.state = ST_LOST
            ev = self._disappeared_event(track, now, gone)
            if ev:
                events.append(ev)
        return events

    def _disappeared_event(self, track: Track, now: float, gone: float) -> Optional[TrackEvent]:
        # A target we only ever caught a handful of frames of was never
        # reliably received, so its loss carries no information.
        if track.seen_count < config.ADSB_MIN_SEEN_FOR_LOSS:
            return None
        if not track.classification.interesting:
            return None
        high = (track.alt_ft or 0) >= config.ADSB_TXPDR_OFF_MIN_ALT_FT
        if high:
            headline = f"Transponder off — {track.label()}"
            detail = (
                f"was {track.alt_ft:,} ft, silent {gone / 60:.0f} min. "
                "At that altitude coverage is reliable, so this is most likely "
                "the transponder being switched off."
            )
        else:
            headline = f"Signal lost — {track.label()}"
            detail = (
                f"last seen {track.position_text()}, silent {gone / 60:.0f} min. "
                "Low-level coverage over water is patchy, so this may be a "
                "receiver gap rather than a transponder shutdown."
            )
        return TrackEvent(
            kind=EV_DISAPPEARED,
            track=track,
            at=now,
            tier=TIER_INTEREST,
            military=track.classification.military,
            headline=headline,
            detail=detail,
        )

    def _prune(self, now: float) -> None:
        dead = [
            h
            for h, t in self.tracks.items()
            if now - t.last_seen > config.ADSB_PRUNE_SEC
            or (t.state == ST_SEEDING and now - t.last_seen > config.ADSB_FADE_SEC)
        ]
        for h in dead:
            del self.tracks[h]

    # -- emission ---------------------------------------------------------

    def _emit(self, events: list, now: float) -> None:
        """Apply the output gates, then hand surviving events to the callback.

        Emergencies bypass every gate below. Quiet hours, a confidence floor
        and a rate cap are all reasonable ways to be less noisy about routine
        traffic, and all of them would be the wrong answer for a 7700.
        """
        for ev in events:
            if ev.tier < TIER_EMERGENCY and not self._passes_gates(ev, now):
                continue
            if self.store is not None:
                self.store.mark_alert(ev.hex, ev.kind, now)
                self.store.log_event(ev)
            self.events_emitted += 1
            if self._on_event is not None:
                try:
                    self._on_event(ev)
                except Exception as exc:  # a bad formatter must not kill the poll
                    self._log(f"ADS-B event handler failed: {exc}", warn=True)

    def _passes_gates(self, ev, now: float) -> bool:
        if in_quiet_hours(now, config.ADSB_QUIET_HOURS):
            return False
        if (
            ev.kind != EV_POSITION
            and ev.track.classification.confidence < config.ADSB_ALERT_MIN_CONFIDENCE
            and not adsb_classify.is_reportable_civil_track(ev.track)
        ):
            return False
        # Persisted, so a restart cannot replay what was already announced.
        if self.store is not None and not self.store.should_alert(
            ev.hex, ev.kind, config.ADSB_EVENT_COOLDOWN_SEC, now
        ):
            return False
        return self._bucket.take(now)

    def _log(self, message: str, warn: bool = False) -> None:
        if self._on_log is not None:
            try:
                self._on_log(message, warn)
            except Exception:
                pass

    # -- thread body ------------------------------------------------------

    def run(self) -> None:
        self._log(
            f"ADS-B tracking started — {config.ADSB_RADIUS_NM:.0f} nm around "
            f"{config.ADSB_HOME_ICAO}, data from adsb.fi"
        )
        board_due = 0.0
        while not self.stop_event.is_set():
            try:
                if self.enabled:
                    now = self._clock()
                    reports = self.source.radius(
                        config.ADSB_HOME_LAT, config.ADSB_HOME_LON, config.ADSB_RADIUS_NM
                    )
                    if reports:
                        self._emit(self.poll_once(reports, now), now)
                        if self.store is not None:
                            self.store.record_sightings(self.tracks.values(), now)

                    if (
                        config.ADSB_MIL_SWEEP_ENABLED
                        and now - self.last_mil_sweep_at >= config.ADSB_MIL_POLL_SEC
                    ):
                        self.last_mil_sweep_at = now
                        self._mil_sweep(now)

                    if now >= board_due and self._on_board is not None:
                        board_due = now + config.ADSB_BOARD_REFRESH_SEC
                        try:
                            self._on_board(self.snapshot_board())
                        except Exception as exc:
                            self._log(f"ADS-B board refresh failed: {exc}", warn=True)
            except Exception as exc:
                self._log(f"ADS-B poll failed: {exc}", warn=True)
            self.stop_event.wait(config.ADSB_POLL_SEC)

    def _mil_sweep(self, now: float) -> None:
        """Catch ADF aircraft still outside the radius, for early warning.

        Deliberately does not feed the presence machine: these are one-off
        wide-area looks, and folding them in would make an aircraft that is
        simply out of range look like it had appeared and vanished repeatedly.
        """
        wide = config.ADSB_RADIUS_NM * 4
        for rep in self.source.military():
            if not rep.has_position:
                continue
            dist = geo.haversine_nm(
                config.ADSB_HOME_LAT, config.ADSB_HOME_LON, rep.lat, rep.lon
            )
            if dist > wide or rep.hex in self.tracks:
                continue
            self._log(
                f"ADS-B: {rep.label()} ({rep.typ or '?'}) {dist:.0f} nm out, inbound watch"
            )

    # -- queries ----------------------------------------------------------

    def snapshot(
        self,
        military_only: bool = False,
        box_only: bool = False,
        airborne_only: bool = True,
        limit: int = 0,
    ) -> list:
        with self._lock:
            rows = []
            for t in self.tracks.values():
                if t.state == ST_LOST:
                    continue
                if box_only and not t.in_box:
                    continue
                if military_only and not t.classification.interesting:
                    continue
                if airborne_only and t.on_ground:
                    continue
                rows.append(t.as_dict())
        rows.sort(key=lambda r: (r["dist_nm"] is None, r["dist_nm"] or 0))
        return rows[:limit] if limit else rows

    def _source_health(self):
        """Source health, tolerating an absent or broken client.

        The board renders on the poll thread and a raise here would be caught
        and logged as a board failure, losing the picture over a detail nobody
        reads. Degrade to "?" instead.
        """
        try:
            return self.source.health()
        except Exception:
            return adsb_source.SourceHealth(active="?")

    def snapshot_board(self) -> dict:
        with self._lock:
            military = self.snapshot(military_only=True, airborne_only=False)
            everything = self.snapshot(airborne_only=True)
            return {
                "at": self.last_poll_at,
                "military": military,
                "all": everything,
                "box": [r for r in everything if r["in_box"]],
                "source": self._source_health().active,
                "tracks": len(self.tracks),
            }

    def find(self, needle: str) -> list:
        needle = (needle or "").strip().upper()
        if not needle:
            return []
        with self._lock:
            return [
                t
                for t in self.tracks.values()
                if needle in (t.hex.upper(), t.ident, t.reg)
                or t.ident.startswith(needle)
                or t.reg.replace("-", "") == needle.replace("-", "")
            ]

    def health(self) -> dict:
        h = self._source_health()
        with self._lock:
            mil = sum(1 for t in self.tracks.values() if t.classification.military)
            box = sum(1 for t in self.tracks.values() if t.in_box)
            return {
                "enabled": self.enabled,
                "polls": self.polls,
                "last_poll_at": self.last_poll_at,
                "tracks": len(self.tracks),
                "military": mil,
                "in_box": box,
                "events": self.events_emitted,
                "suppressed": self._bucket.suppressed,
                "source": h.active,
                "requests": h.requests,
                "failures": h.failures,
                "rate_limited": h.rate_limited,
                "last_error": h.last_error,
                "watch": sorted(self.watch_hex | self.watch_callsign),
            }

    def add_watch(self, item: str) -> bool:
        item = (item or "").strip().upper()
        if not item:
            return False
        with self._lock:
            target = self.watch_hex if len(item) == 6 and _is_hex(item) else self.watch_callsign
            if item in target:
                return False
            target.add(item)
        adsb_classify._classify_cache.clear()
        return True

    def remove_watch(self, item: str) -> bool:
        item = (item or "").strip().upper()
        with self._lock:
            removed = False
            for target in (self.watch_hex, self.watch_callsign):
                if item in target:
                    target.discard(item)
                    removed = True
        if removed:
            adsb_classify._classify_cache.clear()
        return removed

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    def shutdown(self) -> None:
        self.stop_event.set()


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
        return True
    except ValueError:
        return False


# --- CLI -------------------------------------------------------------------
#
#   python3 adsb_tracker.py --once      one poll, print the picture, send nothing
#   python3 adsb_tracker.py --dry-run   run the loop, print events, send nothing
#
# --dry-run is the one to use before pointing this at a real Discord channel:
# it shows exactly what would have been posted, so alert volume can be measured
# against a live airshow rather than guessed at.


def _print_table(rows: list) -> None:
    if not rows:
        print("  (nothing)")
        return
    print(f"  {'CALLSIGN':10} {'TYPE':6} {'ALT':>7} {'GS':>5} {'DIST':>6} {'BRG':>4}  NOTE")
    for r in rows:
        alt = "ground" if r["on_ground"] else (
            format(r["alt_ft"], ",") if r["alt_ft"] is not None else "?"
        )
        gs = f"{r['gs_kt']:.0f}" if r["gs_kt"] else "?"
        dist = f"{r['dist_nm']:.1f}" if r["dist_nm"] is not None else "?"
        brg = geo.compass_point(r["bearing"]) if r["bearing"] is not None else "?"
        note = []
        if r["in_box"]:
            note.append("BOX")
        if r["military"]:
            note.append("MIL")
        elif r["probable"]:
            note.append("probable")
        if r["reasons"]:
            note.append(r["reasons"])
        name = r["ident"] or r["reg"] or r["hex"]
        print(
            f"  {name:10} {r['type'] or '?':6} {alt:>7} {gs:>5} "
            f"{dist:>6} {brg:>4}  {' · '.join(note)[:70]}"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="single poll, print, exit")
    parser.add_argument("--dry-run", action="store_true", help="run the loop, print events, send nothing")
    parser.add_argument("--radius", type=float, default=0.0, help="override radius in NM")
    args = parser.parse_args(argv)

    if args.radius:
        config.ADSB_RADIUS_NM = args.radius

    poller = AdsbPoller(on_log=lambda m, warn=False: print(("! " if warn else "  ") + m))

    if args.once:
        reports = poller.source.radius(
            config.ADSB_HOME_LAT, config.ADSB_HOME_LON, config.ADSB_RADIUS_NM
        )
        poller.poll_once(reports, time.time())
        print(
            f"\n{len(reports)} aircraft within {config.ADSB_RADIUS_NM:.0f} nm of "
            f"{config.ADSB_HOME_ICAO} · source {poller.source.health().active}\n"
        )
        print("MILITARY / DISPLAY")
        _print_table(poller.snapshot(military_only=True, airborne_only=False))
        print("\nDISPLAY BOX")
        _print_table(poller.snapshot(box_only=True, airborne_only=False))
        print("\nALL AIRBORNE")
        _print_table(poller.snapshot(limit=25))
        print("\nData from adsb.fi — https://adsb.fi")
        return 0

    def show(ev):
        stamp = _now_aest(ev.at)
        mark = {TIER_EMERGENCY: "!!", TIER_INTEREST: " *"}.get(ev.tier, "  ")
        print(f"{mark} [{stamp}] {ev.kind.upper():13} {ev.headline}")
        if ev.detail:
            print(f"       {ev.detail}")
        if ev.track.classification.signals:
            print(f"       why: {ev.track.classification.reason_text()}")

    poller._on_event = show
    print(
        f"Dry run — polling every {config.ADSB_POLL_SEC:.0f}s, printing what would "
        f"be posted. Ctrl-C to stop.\n"
    )
    try:
        poller.run()
    except KeyboardInterrupt:
        poller.shutdown()
        print("\n" + str(poller.health()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
