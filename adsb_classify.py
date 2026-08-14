"""Decide whether an ADS-B contact is military or an airshow display aircraft.

Why this is not a one-liner
---------------------------
The obvious approach is ``dbFlags & 1`` — the feed's own military flag. It does
not work for this use case. Sampled live over the Gold Coast during the Pacific
Airshow, a 250 NM sweep found exactly one flagged aircraft, 150 NM inland,
while the display box held:

    T63     no registration, no type, 1225 ft, 242 kt, squawk 1756
    VH-SIC  L-39 Albatros,   operator PERFORMANCE AERO PTY LTD, dbFlags 8
    VH-JPV  BAC Jet Provost, operator POVAIR PTY LTD
    VH-AK4  P-40 Kittyhawk,  1200 ft, 176 kt, overhead the box

None of them carry the military flag. Display aircraft at an Australian airshow
are overwhelmingly civil-registered warbirds, and the genuinely interesting one
is often an airframe no database has ever seen.

So the classifier combines several weak signals instead of trusting one strong
one. Confidence is a noisy-OR: ``1 - prod(1 - w)``. Two independent mediocre
signals therefore beat one mediocre signal, and nothing can exceed 1.0.

Performance contract
--------------------
This runs on every aircraft on every poll, inside a process that is otherwise
busy doing local Whisper inference. Everything here is an exact dict/frozenset
lookup or an integer compare, and results are memoised on aircraft identity.
Do not introduce fuzzy matching, edit distance, or ``Registry.match()`` into
this path — the ADS-B ident arrives as exact text and needs none of it.
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

# Signal codes. Stable strings — they end up in the SQLite event log.
SIG_DBFLAGS = "dbflags"
SIG_WATCHLIST = "watchlist"
SIG_HEX_BLOCK = "hex_block"
SIG_CALLSIGN = "callsign_prefix"
SIG_CALLSIGN_WEAK = "callsign_prefix3"
SIG_TYPE_MIL = "type_mil"
SIG_TYPE_WARBIRD = "type_warbird"
SIG_TYPE_AEROBATIC = "type_aerobatic"
SIG_OPERATOR = "operator"
SIG_ANON_BOX = "anon_box"

# Weights. See the module docstring for the reasoning; the short version is
# that anything at or above ADSB_MIL_CONFIDENCE (0.60) alerts on its own, and
# anything below it has to corroborate with something else first.
WEIGHTS = {
    SIG_DBFLAGS: 1.00,
    SIG_WATCHLIST: 1.00,
    SIG_HEX_BLOCK: 0.90,
    SIG_CALLSIGN: 0.80,
    SIG_TYPE_MIL: 0.70,
    SIG_TYPE_WARBIRD: 0.55,
    SIG_OPERATOR: 0.45,
    # Three letters collides with airline ICAO codes (MON, STO, THA are all
    # registry prefixes). Deliberately below the alert threshold so it can
    # never fire alone.
    SIG_CALLSIGN_WEAK: 0.45,
    SIG_ANON_BOX: 0.40,
    # A Pitts doing weekend circuits is not news. Only interesting alongside
    # the display box or a display operator.
    SIG_TYPE_AEROBATIC: 0.30,
}

# "TROJ23" -> ("TROJ", "23"). Airline idents look the same (QFA621), which is
# fine: they simply will not be in the ADF registry.
_IDENT_RE = re.compile(r"^([A-Z]+)(\d{1,4})$")

# Registry rows whose aircraft/squadron text is a placeholder rather than a
# real answer. Matching still counts as a military signal; we just do not print
# the label.
_USELESS_LABELS = frozenset({"", "VARIOUS", "VARIOUS HELO", "UNKNOWN", "N/A", "-"})


@dataclass(frozen=True)
class Signal:
    code: str
    label: str
    weight: float


@dataclass(frozen=True)
class HexBlock:
    start: int
    end: int
    country: str
    operator: str


@dataclass(frozen=True)
class Classification:
    confidence: float = 0.0
    military: bool = False
    probable: bool = False
    signals: tuple = ()
    title: str = ""

    def reason_text(self) -> str:
        return " · ".join(s.label for s in self.signals)

    @property
    def interesting(self) -> bool:
        return self.military or self.probable


NOT_INTERESTING = Classification()


# --- data tables -----------------------------------------------------------

_tables_lock = threading.Lock()
_hex_blocks: list = []
_types: dict = {}
_mtimes: dict = {}


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_tables(force: bool = False) -> None:
    """Load (or reload) the hex-block and type tables.

    Reloads when a file's mtime changes so the lists can be edited live — at an
    airshow you want to add a type designator without restarting a process that
    takes a minute to warm up its Whisper model.
    """
    global _hex_blocks, _types
    with _tables_lock:
        for path in (config.ADSB_HEX_BLOCKS_FILE, config.ADSB_TYPES_FILE):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            if force or _mtimes.get(path) != mtime:
                _mtimes[path] = mtime
                break
        else:
            return

        blocks = []
        raw = _load_json(config.ADSB_HEX_BLOCKS_FILE) or {}
        for entry in raw.get("blocks", []):
            try:
                blocks.append(
                    HexBlock(
                        start=int(entry["start"], 16),
                        end=int(entry["end"], 16),
                        country=str(entry.get("country", "")),
                        operator=str(entry.get("operator", "military")),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        _hex_blocks = sorted(blocks, key=lambda b: b.start)

        raw = _load_json(config.ADSB_TYPES_FILE) or {}
        _types = {
            "military": frozenset(t.upper() for t in raw.get("military", [])),
            "warbird": frozenset(t.upper() for t in raw.get("warbird", [])),
            "aerobatic": frozenset(t.upper() for t in raw.get("aerobatic", [])),
            "operators": tuple(
                k.upper() for k in raw.get("operator_keywords", []) if k.strip()
            ),
        }
        _classify_cache.clear()


def _tables() -> dict:
    if not _types:
        load_tables(force=True)
    return _types


# --- individual signals ----------------------------------------------------


def split_ident(flight: str) -> tuple:
    """('TROJ23 ') -> ('TROJ', '23'). Non-conforming idents give ('', '')...

    ...except a pure-alpha ident, which is returned whole with no number:
    Australian GA transmits its registration suffix ('VDN' for VH-VDN).
    """
    text = (flight or "").strip().upper()
    if not text:
        return "", ""
    m = _IDENT_RE.match(text)
    if m:
        return m.group(1), m.group(2)
    if text.isalpha():
        return text, ""
    return "", ""


def hex_block(hex_: str) -> Optional[HexBlock]:
    """Military hex-range membership, or None."""
    load_tables()
    try:
        value = int(hex_, 16)
    except (TypeError, ValueError):
        return None
    for block in _hex_blocks:
        if block.start <= value <= block.end:
            return block
        if block.start > value:
            break  # sorted, so nothing further can match
    return None


def _registry_label(rec) -> str:
    """'TROJAN (HERCULES C130J-30, 37 SQN RICHMOND)' — omitting junk fields.

    Several source rows are misparsed, so the name a prefix resolves to is less
    trustworthy than the fact that it matched at all. Only well-formed extras
    are shown.
    """
    if rec is None:
        return ""
    extras = []
    aircraft = (rec.aircraft or "").strip()
    squadron = (rec.squadron or "").strip()
    # Some rows repeat the callsign in the aircraft column ("WEDGETAIL" is
    # both), which would render as "WEDGETAIL (WEDGETAIL, 2 SQN …)".
    if aircraft.upper() not in _USELESS_LABELS and aircraft.upper() != rec.callsign.upper():
        extras.append(aircraft[:40])
    if squadron.upper() not in _USELESS_LABELS:
        extras.append(squadron[:40])
    return f"{rec.callsign} ({', '.join(extras)})" if extras else rec.callsign


def _operator_hit(owner: str) -> str:
    if not owner:
        return ""
    upper = owner.upper()
    for keyword in _tables()["operators"]:
        if keyword in upper:
            return keyword
    return ""


# --- the classifier --------------------------------------------------------

_classify_cache: dict = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 4000


def _combine(weights) -> float:
    """Noisy-OR. Independent evidence accumulates; nothing exceeds 1.0."""
    remainder = 1.0
    for w in weights:
        remainder *= 1.0 - w
    return 1.0 - remainder


def classify(rep, *, in_box: bool = False, watch_hex=(), watch_callsign=()) -> Classification:
    """Classify one AircraftReport. Memoised on aircraft identity."""
    key = (
        rep.hex,
        rep.ident,
        rep.typ,
        rep.reg,
        rep.owner,
        rep.dbflags,
        in_box,
    )
    with _cache_lock:
        hit = _classify_cache.get(key)
    if hit is not None:
        return hit

    result = _classify_uncached(rep, in_box, watch_hex, watch_callsign)

    with _cache_lock:
        # Crude bound. Identities are stable, so this only grows with distinct
        # aircraft seen, but a long-running process still needs a ceiling.
        if len(_classify_cache) > _CACHE_MAX:
            _classify_cache.clear()
        _classify_cache[key] = result
    return result


def _classify_uncached(rep, in_box, watch_hex, watch_callsign) -> Classification:
    load_tables()
    tables = _tables()
    signals = []
    title_bits = []

    def add(code: str, label: str) -> None:
        signals.append(Signal(code=code, label=label, weight=WEIGHTS[code]))

    hex_upper = rep.hex.upper()

    if hex_upper in watch_hex or (rep.ident and rep.ident in watch_callsign):
        add(SIG_WATCHLIST, "on your watchlist")

    if rep.is_db_military:
        add(SIG_DBFLAGS, "database flags it military")

    block = hex_block(rep.hex)
    if block is not None:
        add(SIG_HEX_BLOCK, f"{block.operator} hex block {block.country}")
        if block.operator not in title_bits:
            title_bits.append(block.operator)

    prefix, _number = split_ident(rep.ident)
    if len(prefix) >= 4:
        rec = military_callsigns.lookup_ident_prefix(prefix)
        if rec is not None:
            add(SIG_CALLSIGN, f"callsign {prefix} → {_registry_label(rec)}")
            title_bits.append(rec.callsign)
    elif len(prefix) == 3:
        rec = military_callsigns.lookup_ident_prefix(prefix)
        if rec is not None:
            add(SIG_CALLSIGN_WEAK, f"callsign {prefix} → {_registry_label(rec)}")
            title_bits.append(rec.callsign)

    typ = rep.typ
    if typ:
        if typ in tables["military"]:
            add(SIG_TYPE_MIL, f"military type {typ}")
        elif typ in tables["warbird"]:
            add(SIG_TYPE_WARBIRD, f"warbird type {typ}")
        elif typ in tables["aerobatic"]:
            add(SIG_TYPE_AEROBATIC, f"aerobatic type {typ}")

    keyword = _operator_hit(rep.owner)
    if keyword:
        add(SIG_OPERATOR, f"operator matches “{keyword}”")

    # The T63 case: an aircraft manoeuvring inside the display box that no
    # database has heard of. Requires movement so a parked or drifting
    # position-only contact does not qualify.
    if (
        in_box
        and not rep.reg
        and not rep.typ
        and not rep.on_ground
        and (rep.alt_ft is None or rep.alt_ft < config.ADSB_BOX_MAX_ALT_FT)
        and (rep.gs_kt or 0) > 60
    ):
        add(SIG_ANON_BOX, "unidentified aircraft manoeuvring in the display box")

    if not signals:
        return NOT_INTERESTING

    confidence = _combine(s.weight for s in signals)
    if rep.descr:
        title_bits.append(rep.descr)
    elif typ:
        title_bits.append(typ)

    # De-duplicate while preserving order.
    seen = set()
    title = " · ".join(b for b in title_bits if not (b in seen or seen.add(b)))

    return Classification(
        confidence=round(confidence, 4),
        military=confidence >= config.ADSB_MIL_CONFIDENCE,
        probable=config.ADSB_PROBABLE_CONFIDENCE <= confidence < config.ADSB_MIL_CONFIDENCE,
        signals=tuple(signals),
        title=title,
    )


# --- civil reporting gate --------------------------------------------------

# Airline ICAO prefixes that actually appear at YBCG. Anything else civil is
# treated as GA and skipped unless ADSB_CIVIL_REPORTING is "all".
AIRLINE_PREFIXES = frozenset({
    "QFA", "QLK", "JST", "VOZ", "TGW", "RXA", "ANO", "UAE", "SIA", "ANZ",
    "JQ", "NJS", "SKI", "FDA", "PBN", "BNZ", "CPA", "AIC", "MAS", "THA",
    "GIA", "PAL", "CES", "CSN", "CAL", "EVA", "KAL", "AAR", "NZM", "ADA",
})

# Large-aircraft ADS-B emitter categories: A3 = 75-300t, A4 = high-vortex,
# A5 = heavy. Catches an airline movement whose ident we do not recognise.
_HEAVY_CATEGORIES = frozenset({"A3", "A4", "A5"})


def _reportable_civil(ident: str, squawk: str, emergency: str, category: str) -> bool:
    mode = config.ADSB_CIVIL_REPORTING
    if mode == "all":
        return True
    if mode == "mil_only":
        return False
    if squawk in ("7500", "7600", "7700") or emergency not in ("", "none"):
        return True
    prefix, number = split_ident(ident)
    if number and prefix in AIRLINE_PREFIXES:
        return True
    return category in _HEAVY_CATEGORIES


def is_reportable_civil(rep) -> bool:
    """Should this civil aircraft's YBCG movement reach the flights channel?

    YBCG runs busy GA, flight training and scenic helicopter operations. At
    the default "airline" setting those are noise; airline movements are not.
    """
    return _reportable_civil(rep.ident, rep.squawk, rep.emergency, rep.category)


def is_reportable_civil_track(track) -> bool:
    """Same question, asked of a Track rather than a single report."""
    return _reportable_civil(
        track.ident, track.squawk, track.emergency, track.category
    )
