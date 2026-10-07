"""Callsigns heard on the radio, and which aircraft in the sky they belong to.

The audio side knows what was *said*; the ADS-B side knows what is *flying*.
This module joins the two. Every transcript is mined for callsigns:

    military       "Wolf two one"                 -> WOLF21   (military_callsigns)
    airline        "Qantas four one two"          -> QFA412   (data/airline_telephony.json)
    registration   "Victor Hotel Alpha Bravo Charlie", "VH-ABC",
                   "Gold Coast Tower, Alpha Bravo Charlie" -> VH-ABC
    special        "Rescue five zero zero"        -> RESCUE500

...and each one is matched against the live tracks near the station that heard
it. A match is the whole point: "Wolf two one" is a word; WOLF21 at 3,000 ft,
four miles off Surfers and inbound the CTR is a situation.

Precision over recall, as everywhere else in this project. A spoken
registration is only taken from an unambiguous three-letter group, never from
"taxi via Alpha Bravo Charlie", and every non-military callsign needs a
correlated track before it can reach Discord.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

import config
import military_callsigns

KIND_MILITARY = "military"
KIND_AIRLINE = "airline"
KIND_REGISTRATION = "registration"
KIND_SPECIAL = "special"

PHONETIC = {
    "ALPHA": "A", "ALFA": "A", "BRAVO": "B", "CHARLIE": "C", "DELTA": "D",
    "ECHO": "E", "FOXTROT": "F", "GOLF": "G", "HOTEL": "H", "INDIA": "I",
    "JULIET": "J", "JULIETT": "J", "KILO": "K", "LIMA": "L", "MIKE": "M",
    "NOVEMBER": "N", "OSCAR": "O", "PAPA": "P", "QUEBEC": "Q", "ROMEO": "R",
    "SIERRA": "S", "TANGO": "T", "UNIFORM": "U", "VICTOR": "V",
    "WHISKEY": "W", "WHISKY": "W", "XRAY": "X", "X-RAY": "X", "YANKEE": "Y",
    "ZULU": "Z",
}

DIGITS = {
    "ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "TREE": "3",
    "FOUR": "4", "FOWER": "4", "FIVE": "5", "FIFE": "5", "SIX": "6",
    "SEVEN": "7", "EIGHT": "8", "NINE": "9", "NINER": "9",
}
# "Qantas six thirty two", "Jetstar five fifty nine", "Rescue five hundred".
_TENS = {
    "TWENTY": "2", "THIRTY": "3", "FORTY": "4", "FIFTY": "5",
    "SIXTY": "6", "SEVENTY": "7", "EIGHTY": "8", "NINETY": "9",
}
_TEENS = {
    "TEN": "10", "ELEVEN": "11", "TWELVE": "12", "THIRTEEN": "13",
    "FOURTEEN": "14", "FIFTEEN": "15", "SIXTEEN": "16", "SEVENTEEN": "17",
    "EIGHTEEN": "18", "NINETEEN": "19",
}

# A phonetic group right after one of these is a taxiway, an approach or an
# ATIS letter, not an aircraft.
_NOT_A_REGO_AFTER = frozenset({
    "VIA", "TAXIWAY", "TAXIWAYS", "INFORMATION", "ATIS", "POINT", "APPROACH",
    "RNP", "ILS", "RNAV", "VOR", "NDB", "SID", "STAR", "BAY", "GATE", "APRON",
    "HOLDING", "TAXI", "INTERSECTION", "CROSS", "ARRIVAL", "DEPARTURE",
})

# Numbers that belong to the phrase after a callsign, not to the callsign.
_UNIT_WORDS = frozenset({
    "MILES", "MILE", "THOUSAND", "HUNDRED", "FEET", "FOOT", "KNOTS", "KNOT",
    "DEGREES", "DEGREE", "MINUTES", "MINUTE", "NAUTICAL", "DME", "OCLOCK",
})

# Punctuation is kept as its own token: Whisper's commas are the only record of
# where "Virgin eight one two" ends and "five miles final" begins.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|[,.;:?!]")
_VH_RE = re.compile(r"\bVH-?([A-Z]{3})\b")


@dataclass
class RadioCallsign:
    kind: str
    spoken: str
    canonical: str            # QFA412, VH-ABC, WOLF21, RESCUE500
    number: str = ""
    prefixes: tuple = ()      # ADS-B ident prefixes this may broadcast as
    confidence: float = 1.0
    strong: bool = True
    detail: str = ""          # e.g. aircraft/squadron for a military callsign

    @property
    def label(self) -> str:
        return self.canonical


@dataclass
class Mention:
    """One callsign in one transmission, with whatever it was matched to."""
    callsign: RadioCallsign
    track: object = None      # adsb_tracker.Track
    score: float = 0.0
    roles: set = field(default_factory=set)
    zones: tuple = ()


# --- data ------------------------------------------------------------------

_tables_lock = threading.Lock()
_tables: dict = {}
_mtime: Optional[float] = None


def _load() -> dict:
    global _tables, _mtime
    path = config.RADIO_AIRLINE_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    with _tables_lock:
        if _tables and mtime == _mtime:
            return _tables
        _mtime = mtime
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            raw = {}
        airlines = {
            tuple(k.upper().split()): str(v).upper()
            for k, v in (raw.get("airlines") or {}).items() if k.strip() and v
        }
        special = {
            tuple(k.upper().split()): tuple(p.upper() for p in v)
            for k, v in (raw.get("special") or {}).items() if k.strip()
        }
        _tables = {
            "airlines": airlines,
            "special": special,
            "max_words": max([len(k) for k in list(airlines) + list(special)] or [1]),
        }
        return _tables


# --- extraction ------------------------------------------------------------


def _read_number(tokens: list, i: int, max_digits: int = 4) -> tuple:
    """Digits spoken from tokens[i] on: ('412', next_index). '' if none.

    "Qantas four one two five miles" — a unit word claims the digit before
    it, so the callsign keeps 412 and the distance keeps its five.
    """
    parts: list = []
    while i < len(tokens) and sum(len(p) for p in parts) < max_digits:
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in DIGITS:
            part = DIGITS[tok]
            if nxt == "HUNDRED" and (tokens[i + 2] if i + 2 < len(tokens) else "") not in _UNIT_WORDS:
                part += "00"
                i += 1
        elif tok in _TENS:
            if nxt in DIGITS and DIGITS[nxt] != "0":
                part = _TENS[tok] + DIGITS[nxt]
                i += 1
            else:
                part = _TENS[tok] + "0"
        elif tok in _TEENS:
            part = _TEENS[tok]
        elif tok.isdigit():
            part = tok
        else:
            break
        if sum(len(p) for p in parts) + len(part) > max_digits:
            break
        parts.append(part)
        i += 1
    if parts and i < len(tokens) and tokens[i] in _UNIT_WORDS:
        parts.pop()
        i -= 1
    return "".join(parts), i


def _military(text: str, military: Optional[list]) -> list:
    matches = military if military is not None else military_callsigns.detect(text)
    out = []
    for m in matches:
        # m.flight is e.g. "One One", "21" or "Flight"; keep only the digits.
        number = "".join(
            DIGITS.get(t, t if t.isdigit() else "") for t in m.flight.upper().split()
        )
        rec = military_callsigns.get_registry().lookup(m.callsign)
        prefixes = tuple(rec.prefixes) if rec is not None else ()
        squashed = military_callsigns.squash(m.callsign)
        out.append(RadioCallsign(
            kind=KIND_MILITARY,
            spoken=f"{m.matched_text} {m.flight}".strip(),
            canonical=f"{squashed}{number}",
            number=number,
            prefixes=tuple(dict.fromkeys(prefixes + (squashed,))),
            confidence=m.confidence,
            strong=m.strong,
            detail=" — ".join(b for b in (m.aircraft, m.squadron) if b),
        ))
    return out


def _telephony(tokens: list) -> list:
    tables = _load()
    out = []
    i = 0
    while i < len(tokens):
        hit = None
        for width in range(min(tables["max_words"], len(tokens) - i), 0, -1):
            key = tuple(tokens[i:i + width])
            if key in tables["airlines"]:
                hit = (KIND_AIRLINE, key, (tables["airlines"][key],))
                break
            if key in tables["special"]:
                hit = (KIND_SPECIAL, key, tables["special"][key])
                break
        if hit is None:
            i += 1
            continue
        kind, key, prefixes = hit
        number, nxt = _read_number(tokens, i + len(key))
        spoken = " ".join(key)
        if kind == KIND_AIRLINE:
            if number:
                out.append(RadioCallsign(
                    kind=kind, spoken=f"{spoken} {number}".title(),
                    canonical=f"{prefixes[0]}{number.lstrip('0') or '0'}",
                    number=number, prefixes=prefixes, confidence=0.95,
                ))
        else:
            # "the rescue helicopter" is a phrase; "Rescue five hundred" is a callsign.
            if number:
                out.append(RadioCallsign(
                    kind=kind, spoken=f"{spoken} {number}".title(),
                    canonical=f"{''.join(key)}{number}", number=number,
                    prefixes=prefixes, confidence=0.9,
                ))
        i = max(nxt, i + len(key))
    return out


def _registrations(text: str, tokens: list) -> list:
    found: dict = {}
    for m in _VH_RE.finditer(text.upper()):
        rego = f"VH-{m.group(1)}"
        found[rego] = RadioCallsign(KIND_REGISTRATION, m.group(0), rego, confidence=1.0)

    letters = [PHONETIC.get(t) for t in tokens]
    i = 0
    while i < len(tokens):
        if letters[i] is None:
            i += 1
            continue
        j = i
        while j < len(tokens) and letters[j] is not None:
            j += 1
        run = "".join(letters[i:j])
        spoken = " ".join(t.title() for t in tokens[i:j])
        before = tokens[i - 1] if i > 0 else ""
        if run.startswith("VH") and len(run) == 5:
            found.setdefault(f"VH-{run[2:]}", RadioCallsign(
                KIND_REGISTRATION, spoken, f"VH-{run[2:]}", confidence=1.0,
            ))
        elif len(run) == 3 and before not in _NOT_A_REGO_AFTER and not before.isdigit():
            # Abbreviated Australian GA callsign: the last three letters.
            found.setdefault(f"VH-{run}", RadioCallsign(
                KIND_REGISTRATION, spoken, f"VH-{run}", confidence=0.7,
            ))
        i = j
    return list(found.values())


def extract(text: str, military: Optional[list] = None) -> list:
    """Every callsign in one transcript, de-duplicated, military first.

    Pass the transcript's military matches if they are already computed, so
    the registry is not consulted twice for the same text.
    """
    if not text:
        return []
    tokens = [t.upper() for t in _TOKEN_RE.findall(text)]
    spoken = _telephony(tokens)
    words_taken = {w for cs in spoken for w in cs.spoken.upper().split()}
    # "Polair three zero" is also a weak fuzzy hit on PELAIR; the telephony
    # match is the real one.
    mil = [
        cs for cs in _military(text, military)
        if cs.strong or cs.spoken.split()[0].upper() not in words_taken
    ]
    out: dict = {}
    for cs in mil + spoken + _registrations(text, tokens):
        prev = out.get(cs.canonical)
        if prev is None or (cs.strong, cs.confidence) > (prev.strong, prev.confidence):
            out[cs.canonical] = cs
    order = {KIND_MILITARY: 0, KIND_SPECIAL: 1, KIND_AIRLINE: 2, KIND_REGISTRATION: 3}
    return sorted(out.values(), key=lambda c: (order[c.kind], -c.confidence, c.canonical))


# --- correlation -----------------------------------------------------------


def _norm(s: str) -> str:
    return (s or "").upper().replace("-", "").replace(" ", "").strip()


def _score(cs: RadioCallsign, track) -> float:
    ident = _norm(track.ident)
    reg = _norm(track.reg)
    if cs.kind == KIND_REGISTRATION:
        want = _norm(cs.canonical)                     # VHABC
        if reg and reg == want:
            return 1.0
        if ident and (ident == want or ident == want[2:]):
            return 0.95
        return 0.0
    if cs.kind == KIND_AIRLINE:
        if ident == cs.canonical:
            return 1.0
        prefix = cs.prefixes[0] if cs.prefixes else ""
        if ident.startswith(prefix) and ident[len(prefix):].lstrip("0") == cs.number.lstrip("0"):
            return 1.0
        return 0.0
    # Military and special: callsign prefix plus the spoken number.
    for p in cs.prefixes:
        if not p or not ident.startswith(p):
            continue
        rest = ident[len(p):]
        if cs.number and rest.lstrip("0") == cs.number.lstrip("0"):
            return 0.95
        if not cs.number and (not rest or rest.isdigit()):
            return 0.6
    if cs.kind == KIND_SPECIAL and cs.number and track.classification.special:
        if ident.endswith(cs.number):
            return 0.7
    return 0.0


def correlate(cs: RadioCallsign, tracks) -> tuple:
    """(track, score) for the single best match, or (None, 0.0).

    Two tracks tying for best is treated as no match: attaching a radio call
    to the wrong aircraft is worse than attaching it to none.
    """
    scored = sorted(
        ((s, t) for t in tracks if (s := _score(cs, t)) > 0),
        key=lambda x: -x[0],
    )
    if not scored:
        return None, 0.0
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None, 0.0
    return scored[0][1], scored[0][0]


def station(icao: str) -> Optional[dict]:
    for s in config.STREAMS:
        if s["icao"] == icao:
            return s
    return None


def analyse(text: str, icao: str, poller=None, military: Optional[list] = None) -> list:
    """Extract every callsign and match each to a track near the station."""
    callsigns = extract(text, military)
    if not callsigns:
        return []
    tracks = []
    st = station(icao)
    if poller is not None and st is not None:
        try:
            tracks = poller.tracks_near(st["lat"], st["lon"], st.get("relevance_nm", 30))
        except Exception:
            tracks = []
    out = []
    for cs in callsigns:
        track, score = correlate(cs, tracks) if tracks else (None, 0.0)
        mention = Mention(callsign=cs, track=track, score=score)
        if track is not None:
            mention.roles = set(track.roles())
            mention.zones = tuple(sorted(track.zones))
        out.append(mention)
    return out


def should_flag(m: Mention, tier: int) -> bool:
    """Is this mention worth a post in #airspace-watch?

    Always for a strong military callsign and for any callsign in an
    alert-tier transmission; otherwise only when it is matched to an aircraft
    carrying one of RADIO_FLAG_ROLES.
    """
    cs = m.callsign
    if cs.kind == KIND_MILITARY:
        return cs.strong
    if tier >= 1:
        return True
    return bool(m.track is not None and m.roles & config.RADIO_FLAG_ROLES)


class FlagCooldown:
    """Per (callsign, station) cooldown, so a chatty frequency does not repeat."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._last: dict = {}
        self._lock = threading.Lock()

    def ready(self, key: tuple, now: float) -> bool:
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.seconds:
                return False
            self._last[key] = now
            if len(self._last) > 2000:
                cutoff = now - self.seconds
                self._last = {k: v for k, v in self._last.items() if v >= cutoff}
            return True
