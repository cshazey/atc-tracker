#!/usr/bin/env python3
"""Military callsign registry and transcript matcher.

Scrapes the ADF callsign table published at swld.com.au into a local SQLite
database, refreshes it in the background roughly once a day, and flags military
callsigns appearing in speech-to-text transcripts.

The matcher has to cope with Whisper mangling a callsign it has never been
trained on — "Falcon one one" comes back as "Falcum 11", "Falken one one",
"Foulcon". Three tiers are used, strongest first:

    exact     the squashed token equals the callsign
    fuzzy     bounded Levenshtein distance, budget scaled by callsign length
    phonetic  Soundex-style consonant skeleton matches and the distance is
              still small

False positives are the real risk. These feeds carry "runway one four" and "Golf
Kilo Delta" all day, and an unguarded search over 400+ callsigns fires on about
one transmission in twenty-five. Four guards bring that down to a handful across
three days of logs:

    BLOCKLIST     callsigns never matched at all — the phonetic alphabet,
                  formation colours, and civil types that are spoken with a
                  number after them ("Dash 8", "King Air 350")
    AMBIGUOUS     callsigns that are ordinary words often enough that a flight
                  number alone is not enough; they alert only with military
                  context, and otherwise just annotate
    lookalikes    English words within reach of a callsign ("starting" for
                  STARLING, "water" for WALER) are never fuzzy-matched
    flight number an everyday-word callsign must be followed by one, because
                  military traffic calls itself "Falcon one one", not "Falcon"

Matches come back strong or weak. Strong drives alerts; weak only annotates the
transcript, which is where most misheard hits land.

Standalone use:

    python3 military_callsigns.py refresh [--force]
    python3 military_callsigns.py stats
    python3 military_callsigns.py dump [--limit N]
    python3 military_callsigns.py match "falcum one one, cleared visual"
    python3 military_callsigns.py export-seed
    python3 military_callsigns.py build-lookalikes
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

import config

# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

_SCRAPE_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-AU,en;q=0.9",
}

# Header row wording, so the table header is not stored as a callsign.
_HEADER_CELLS = {"CALLSIGN", "ATC ID PREFIX", "AIRCRAFT TYPE", "SQUADRON", "OPS FREQ"}

# The page marks entries it has verified off-air with a "- Confirmed" suffix on
# the callsign cell. Spelling and spacing of that suffix is inconsistent
# ("EAGLE OPS -CONFIRMED"), so it is stripped loosely.
_CONFIRMED_RE = re.compile(r"[\s\-–]*\bCONFIRMED\b[\s\-–]*$", re.IGNORECASE)

# Callsign cells are occasionally placeholders ("?") or footnotes.
_VALID_CALLSIGN_RE = re.compile(r"^[A-Z][A-Z0-9 '\-()]{1,24}$")


class _TableParser(HTMLParser):
    """Collects every table row on the page as a list of cell strings.

    The page is FrontPage-era XHTML with nested tables, inline <font> tags and
    callsigns wrapped across source lines, so cell text is accumulated across
    child tags and whitespace-collapsed at the end.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


@dataclass
class CallsignRecord:
    callsign: str
    prefixes: tuple[str, ...] = ()
    aircraft: str = ""
    squadron: str = ""
    ops_freq: str = ""
    confirmed: bool = False

    def describe(self) -> str:
        bits = [b for b in (self.aircraft, self.squadron) if b]
        return " — ".join(bits) if bits else "unlisted type"


def _clean_cell(value: str) -> str:
    return value.replace("\xa0", " ").strip(" \t\r\n·-–,")


def _split_prefixes(value: str) -> tuple[str, ...]:
    parts = re.split(r"[,/\s]+", _clean_cell(value).upper())
    return tuple(p for p in parts if p and p.isalnum() and 1 < len(p) <= 6)


def parse_html(page: str) -> list[CallsignRecord]:
    """Extract callsign records from the source page's HTML.

    Duplicate callsigns are merged — the page lists e.g. THUNDER three times for
    three different airframes, and a transcript hit should report all of them.
    """
    parser = _TableParser()
    parser.feed(page)

    merged: dict[str, CallsignRecord] = {}
    for row in parser.rows:
        if len(row) < 3:
            continue
        raw_callsign = _clean_cell(row[0])
        if not raw_callsign or raw_callsign.upper() in _HEADER_CELLS:
            continue
        name = _CONFIRMED_RE.sub("", raw_callsign).strip(" \t-–")
        confirmed = bool(_CONFIRMED_RE.search(raw_callsign))
        name = " ".join(name.upper().split())
        if not _VALID_CALLSIGN_RE.match(name):
            continue

        prefixes = _split_prefixes(row[1]) if len(row) > 1 else ()
        aircraft = _clean_cell(row[2]) if len(row) > 2 else ""
        squadron = _clean_cell(row[3]) if len(row) > 3 else ""
        ops_freq = _clean_cell(row[4]) if len(row) > 4 else ""

        existing = merged.get(name)
        if existing is None:
            merged[name] = CallsignRecord(
                callsign=name,
                prefixes=prefixes,
                aircraft=aircraft,
                squadron=squadron,
                ops_freq=ops_freq,
                confirmed=confirmed,
            )
            continue

        existing.prefixes = tuple(dict.fromkeys(existing.prefixes + prefixes))
        existing.aircraft = _join_unique(existing.aircraft, aircraft)
        existing.squadron = _join_unique(existing.squadron, squadron)
        existing.ops_freq = _join_unique(existing.ops_freq, ops_freq)
        existing.confirmed = existing.confirmed or confirmed

    return sorted(merged.values(), key=lambda r: r.callsign)


def _join_unique(current: str, addition: str, sep: str = " / ") -> str:
    if not addition:
        return current
    if not current:
        return addition
    parts = [p.strip() for p in current.split(sep)]
    if addition.strip() in parts:
        return current
    return sep.join(parts + [addition.strip()])


def fetch_page(url: str = "", timeout: float = 30.0) -> str:
    url = url or config.MILITARY_CALLSIGN_URL
    resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=timeout)
    resp.raise_for_status()
    # The page is served without a charset and contains a UTF-8 BOM.
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class Match:
    callsign: str
    aircraft: str
    squadron: str
    matched_text: str       # what the transcript actually said
    flight: str = ""        # trailing flight number, if one was spoken
    confidence: float = 1.0
    kind: str = "exact"     # exact | fuzzy | phonetic
    strong: bool = True     # strong drives alerts; weak only annotates

    @property
    def label(self) -> str:
        return f"{self.callsign} {self.flight}".strip()

    def describe(self) -> str:
        detail = " — ".join(b for b in (self.aircraft, self.squadron) if b)
        out = self.label
        if detail:
            out += f" ({detail})"
        if self.kind != "exact":
            out += f" [heard “{self.matched_text}”, {self.kind} {self.confidence:.2f}]"
        if not self.strong:
            out += " [possible]"
        return out


_WORD_RE = re.compile(r"[A-Za-z0-9'\-]+")
_LETTERS_RE = re.compile(r"[^A-Z]")

_NUMBER_WORDS = {
    "ZERO", "ONE", "TWO", "THREE", "TREE", "FOUR", "FOWER", "FIVE", "FIFE",
    "SIX", "SEVEN", "EIGHT", "NINE", "NINER", "TEN", "ELEVEN", "TWELVE",
}

# Spoken right after a callsign these are part of the flight identifier rather
# than the next sentence.
_FLIGHT_SUFFIX_WORDS = {"FLIGHT", "FORMATION", "LEAD", "OPS"}

# A number followed by one of these is a distance or an altitude, not a flight
# number — "Roulettes formation, five miles south" is not Roulettes Five.
_UNIT_WORDS = {
    "MILES", "MILE", "THOUSAND", "HUNDRED", "FEET", "FOOT", "KNOTS", "KNOT",
    "DEGREES", "DEGREE", "MINUTES", "MINUTE", "TRACK", "NAUTICAL", "LITRES",
    "SOULS", "POB", "PERSONS", "OCLOCK", "O'CLOCK",
}

# NATO alphabet — every one of these is spoken constantly in registrations, so
# they are never treated as callsigns even though the source page lists several.
PHONETIC_ALPHABET = frozenset({
    "ALPHA", "ALFA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF",
    "HOTEL", "INDIA", "JULIET", "JULIETT", "KILO", "LIMA", "MIKE", "NOVEMBER",
    "OSCAR", "PAPA", "QUEBEC", "ROMEO", "SIERRA", "TANGO", "UNIFORM", "VICTOR",
    "WHISKEY", "WHISKY", "XRAY", "X-RAY", "YANKEE", "ZULU",
})

# Ordinary ATC vocabulary. A transcript token in this set is never fuzzy-matched
# against a callsign — it is far more likely the controller said the everyday
# word than a callsign one edit away from it.
ATC_VOCABULARY = frozenset({
    "ABEAM", "ABORT", "ACKNOWLEDGE", "ACTIVE", "ADVISE", "AFFIRM", "AIRBORNE",
    "AIRCRAFT", "AIRFIELD", "AIRPORT", "AIRSPACE", "ALTITUDE", "APPROACH",
    "APPROVED", "ARRIVAL", "ATIS", "AVAILABLE", "BASE", "BRISBANE", "BROADCAST",
    "CANCEL", "CAUTION", "CEILING", "CENTRE", "CENTER", "CHANGE", "CIRCUIT",
    "CLEARANCE", "CLEARED", "CLIMB", "CLOSED", "COAST", "COASTAL", "CONFIRM",
    "CONTACT", "CONTINUE", "COPIED", "CORRECT", "COURSE", "CROSSING",
    "CROSSWIND", "CRUISE", "DEGREES", "DELAY", "DEPARTURE", "DESCEND",
    "DEPARTING", "DIRECT", "DOWNWIND", "ESTABLISHED", "ESTIMATE", "EXPECT",
    "FINAL", "FLIGHT", "FOLLOW", "FREQUENCY", "GLIDESLOPE", "GOLDCOAST",
    "GROUND", "HEADING", "HOLDING", "IDENTIFIED", "IMMEDIATE", "INBOUND",
    "INFORMATION", "INTENTIONS", "INTERSECTION", "JOINING", "KNOTS", "LANDING",
    "LEAVING", "LEVEL", "LOCALIZER", "LOCALISER", "MAINTAIN", "MILES",
    "MISSED", "NEGATIVE", "NUMBER", "OVERHEAD", "PARALLEL", "PASSING",
    "PATTERN", "POSITION", "PROCEED", "RADAR", "READBACK", "RECEIVED",
    "REPORT", "REQUEST", "RESUME", "ROGER", "RUNWAY", "SEQUENCE", "SERVICE",
    "SHORT", "SOUTHPORT", "SPEED", "SQUAWK", "STANDBY", "STATION", "STRAIGHT",
    "SURFACE", "TAKEOFF", "TAXI", "TERMINAL", "THOUSAND", "THRESHOLD",
    "TOUCHDOWN", "TOWER", "TRACK", "TRAFFIC", "TRANSITION", "TURNING",
    "UNABLE", "VACATE", "VECTOR", "VISUAL", "WEATHER", "WILCO", "WINDS",
})

# Signals that the transmission is military even when the callsign itself is a
# common word. Used to admit AMBIGUOUS callsigns without a spoken flight number.
_MILITARY_CONTEXT_RE = re.compile(
    r"\b(?:RAAF|R\.A\.A\.F|MILITARY|AIR\s*FORCE|NAVY|ARMY|DEFENCE|DEFENSE|"
    r"SQUADRON|SQN|AMBERLEY|WILLIAMTOWN|RICHMOND|EDINBURGH|TOWNSVILLE|OAKEY|"
    r"PEARCE|TINDAL|DARWIN|NOWRA|EAST\s*SALE|FORMATION|TANKER|REFUEL\w*|"
    r"GROWLER|HORNET|POSEIDON|WEDGETAIL|GLOBEMASTER|HERCULES|SPARTAN|"
    r"BLACKHAWK|CHINOOK|TAIPAN|ROULETTES?)\b",
    re.IGNORECASE,
)

# Ordinary English words that sit close enough to a callsign to be mistaken for
# one — "starting"/STARLING, "water"/WALER, "margin"/MARLIN. Generated offline
# from a system dictionary (see the build-lookalikes CLI command) and committed,
# so matching behaves the same on every machine.
_LOOKALIKE_CACHE: Optional[frozenset] = None


def _lookalike_words() -> frozenset:
    global _LOOKALIKE_CACHE
    if _LOOKALIKE_CACHE is None:
        words = set()
        try:
            for line in Path(config.MILITARY_LOOKALIKE_FILE).read_text(
                encoding="utf-8"
            ).splitlines():
                word = squash(line)
                if word:
                    words.add(word)
        except OSError:
            pass
        _LOOKALIKE_CACHE = frozenset(words)
    return _LOOKALIKE_CACHE


_SOUNDEX_GROUPS = {
    "B": "1", "F": "1", "P": "1", "V": "1",
    "C": "2", "G": "2", "J": "2", "K": "2", "Q": "2", "S": "2", "X": "2", "Z": "2",
    "D": "3", "T": "3",
    "L": "4",
    "M": "5", "N": "5",
    "R": "6",
}


def squash(text: str) -> str:
    """Letters only, uppercased — 'Black Hawk' and 'BLACKHAWK' compare equal."""
    return _LETTERS_RE.sub("", text.upper())


def phonetic_key(text: str) -> str:
    """Soundex consonant skeleton, untruncated.

    Untruncated because Soundex's 4-character form collapses far too much for
    words this long: FIREBIRD and FIREBALL would both be F616.
    """
    word = squash(text)
    if not word:
        return ""
    out = [word[0]]
    last = _SOUNDEX_GROUPS.get(word[0], "")
    for ch in word[1:]:
        code = _SOUNDEX_GROUPS.get(ch, "")
        if code and code != last:
            out.append(code)
        if ch not in "HW":
            last = code
    return "".join(out)


def edit_distance(a: str, b: str, max_dist: int) -> int:
    """Levenshtein distance, giving up once it provably exceeds max_dist."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < best:
                best = cur[j]
        if best > max_dist:
            return max_dist + 1
        prev = cur
    return prev[lb]


def edit_budget(length: int) -> int:
    """How many characters a callsign of this length may be misheard by."""
    if length < 5:
        return 0
    if length < 8:
        return 1
    if length < 11:
        return 2
    return 3


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS callsigns (
    callsign   TEXT PRIMARY KEY,
    prefixes   TEXT NOT NULL DEFAULT '',
    aircraft   TEXT NOT NULL DEFAULT '',
    squadron   TEXT NOT NULL DEFAULT '',
    ops_freq   TEXT NOT NULL DEFAULT '',
    confirmed  INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL DEFAULT '',
    last_seen  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class RefreshResult:
    ok: bool
    message: str
    added: int = 0
    updated: int = 0
    total: int = 0


class Registry:
    """SQLite-backed callsign store plus the transcript matcher.

    Thread-safe: a connection is opened per operation (SQLite is happy with
    that) and the in-memory index is rebuilt under a lock.
    """

    def __init__(self, db_path: Optional[Path] = None, seed_path: Optional[Path] = None):
        self.db_path = Path(db_path or config.MILITARY_CALLSIGN_DB)
        self.seed_path = Path(seed_path or config.MILITARY_CALLSIGN_SEED)
        self._lock = threading.RLock()
        self._all: dict[str, CallsignRecord] = {}
        self._records: dict[str, CallsignRecord] = {}
        self._by_squash: dict[str, CallsignRecord] = {}
        self._by_length: dict[int, list[tuple[str, CallsignRecord]]] = {}
        self._by_phonetic: dict[str, list[tuple[str, CallsignRecord]]] = {}
        # ATC ID prefix -> record, for clean ADS-B idents. See lookup_prefix().
        self._by_prefix: dict[str, CallsignRecord] = {}
        self._token_cache: dict[str, Optional[tuple[CallsignRecord, float, str]]] = {}
        self._loaded = False
        self._max_words = 1

    # -- storage ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.executescript(_SCHEMA)
        return conn

    def _read_db(self) -> list[CallsignRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT callsign, prefixes, aircraft, squadron, ops_freq, confirmed "
                "FROM callsigns"
            ).fetchall()
        return [
            CallsignRecord(
                callsign=r[0],
                prefixes=tuple(p for p in r[1].split(",") if p),
                aircraft=r[2],
                squadron=r[3],
                ops_freq=r[4],
                confirmed=bool(r[5]),
            )
            for r in rows
        ]

    def _write_db(self, records: Iterable[CallsignRecord]) -> tuple[int, int]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = updated = 0
        with self._connect() as conn:
            existing = {r[0] for r in conn.execute("SELECT callsign FROM callsigns")}
            for rec in records:
                if rec.callsign in existing:
                    updated += 1
                else:
                    added += 1
                conn.execute(
                    """
                    INSERT INTO callsigns
                        (callsign, prefixes, aircraft, squadron, ops_freq,
                         confirmed, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(callsign) DO UPDATE SET
                        prefixes  = excluded.prefixes,
                        aircraft  = excluded.aircraft,
                        squadron  = excluded.squadron,
                        ops_freq  = excluded.ops_freq,
                        confirmed = excluded.confirmed,
                        last_seen = excluded.last_seen
                    """,
                    (
                        rec.callsign, ",".join(rec.prefixes), rec.aircraft,
                        rec.squadron, rec.ops_freq, int(rec.confirmed), now, now,
                    ),
                )
        return added, updated

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- loading ----------------------------------------------------------

    def _load_seed(self) -> list[CallsignRecord]:
        """Bundled snapshot, so detection works before the first fetch."""
        try:
            data = json.loads(self.seed_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        out = []
        for item in data.get("callsigns", []):
            try:
                out.append(CallsignRecord(
                    callsign=item["callsign"],
                    prefixes=tuple(item.get("prefixes", ())),
                    aircraft=item.get("aircraft", ""),
                    squadron=item.get("squadron", ""),
                    ops_freq=item.get("ops_freq", ""),
                    confirmed=bool(item.get("confirmed", False)),
                ))
            except (KeyError, TypeError):
                continue
        return out

    def load(self, force: bool = False) -> None:
        with self._lock:
            if self._loaded and not force:
                return
            records = self._read_db()
            if not records:
                records = self._load_seed()
                if records:
                    self._write_db(records)
                    self.set_meta("seeded_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            for name, detail in config.MILITARY_EXTRA_CALLSIGNS.items():
                records.append(CallsignRecord(
                    callsign=name.upper(),
                    aircraft=detail,
                    squadron="local addition",
                    confirmed=True,
                ))
            self._index(records)
            self._loaded = True

    def _index(self, records: Iterable[CallsignRecord]) -> None:
        # _all is everything scraped; _records is only what the matcher may fire
        # on. The blocklist is a matching policy, so blocked callsigns stay
        # stored and stay visible to /military lookups.
        self._all = {r.callsign: r for r in records}
        self._records = {}
        self._by_squash = {}
        self._by_length = {}
        self._by_phonetic = {}
        self._by_prefix = {}
        self._token_cache = {}
        self._max_words = 1
        blocked = {squash(w) for w in config.MILITARY_CALLSIGN_BLOCKLIST} | {
            squash(w) for w in PHONETIC_ALPHABET
        }
        for rec in records:
            # Prefixes are indexed for EVERY record, deliberately before the
            # blocklist gate below. That blocklist is a transcript-matching
            # policy — it exists because Whisper turns ordinary speech into
            # HAWK and DUKE. An ADS-B ident is transmitted as text, so none of
            # that applies and excluding e.g. HAWK here would lose real RAAF
            # Hawk 127s. The separate MILITARY_PREFIX_BLOCKLIST covers the
            # genuine hazard: prefixes the source page misparses.
            for pfx in rec.prefixes:
                pkey = squash(pfx)
                if (
                    len(pkey) < config.MILITARY_MIN_PREFIX_LEN
                    or pkey in config.MILITARY_PREFIX_BLOCKLIST
                ):
                    continue
                self._by_prefix.setdefault(pkey, rec)
            key = squash(rec.callsign)
            if not key or key in blocked or len(key) < config.MILITARY_MIN_CALLSIGN_LEN:
                continue
            self._records[rec.callsign] = rec
            # Later duplicates lose; parse_html has already merged same-name rows.
            self._by_squash.setdefault(key, rec)
            self._by_length.setdefault(len(key), []).append((key, rec))
            self._by_phonetic.setdefault(phonetic_key(key), []).append((key, rec))
            self._max_words = max(self._max_words, len(rec.callsign.split()))

    # -- refresh ----------------------------------------------------------

    def stale(self) -> bool:
        last = self.get_meta("last_success_at")
        if not last:
            return True
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            return True
        age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
        return age_h >= config.MILITARY_REFRESH_HOURS

    def refresh(self, force: bool = False, timeout: float = 30.0) -> RefreshResult:
        """Re-scrape the source page and merge it into the database."""
        self.load()
        if not force and not self.stale():
            return RefreshResult(True, "up to date", total=len(self._records))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            page = fetch_page(timeout=timeout)
            records = parse_html(page)
        except Exception as exc:
            self.set_meta("last_attempt_at", now)
            self.set_meta("last_error", f"{type(exc).__name__}: {exc}")
            return RefreshResult(False, f"fetch failed: {type(exc).__name__}: {exc}",
                                 total=len(self._records))

        # A parse that returns almost nothing means the page layout changed;
        # keeping the previous database beats wiping it on a bad scrape.
        if len(records) < config.MILITARY_MIN_SCRAPE_ROWS:
            self.set_meta("last_attempt_at", now)
            self.set_meta("last_error", f"only {len(records)} rows parsed — layout change?")
            return RefreshResult(
                False,
                f"parsed only {len(records)} rows (min {config.MILITARY_MIN_SCRAPE_ROWS}) — "
                "keeping existing data",
                total=len(self._records),
            )

        added, updated = self._write_db(records)
        self.set_meta("last_attempt_at", now)
        self.set_meta("last_success_at", now)
        self.set_meta("last_error", "")
        self.set_meta("row_count", str(len(records)))
        self.set_meta("source_url", config.MILITARY_CALLSIGN_URL)
        self.load(force=True)
        return RefreshResult(
            True,
            f"{len(records)} callsigns ({added} new, {updated} updated)",
            added=added, updated=updated, total=len(self._records),
        )

    # -- lookups ----------------------------------------------------------

    def __len__(self) -> int:
        self.load()
        return len(self._records)

    def all_records(self) -> list[CallsignRecord]:
        self.load()
        return sorted(self._all.values(), key=lambda r: r.callsign)

    def lookup(self, callsign: str) -> Optional[CallsignRecord]:
        self.load()
        key = squash(callsign)
        found = self._by_squash.get(key)
        if found is not None:
            return found
        for rec in self._all.values():
            if squash(rec.callsign) == key:
                return rec
        return None

    def lookup_prefix(self, prefix: str) -> Optional[CallsignRecord]:
        """Exact ATC-ID-prefix lookup for a clean ADS-B ident: 'BLKT' -> BLACKCAT.

        Deliberately NOT fuzzy, and deliberately separate from lookup(), which
        resolves callsign *names*. match() has to tolerate mangling because
        Whisper has never heard these words; an ADS-B ident arrives as exact
        text, so the same tolerance here would be pure false-positive surface.

        Callers should treat a hit as the military signal in its own right and
        only surface record.aircraft / record.squadron when they look sane —
        a handful of source rows are misparsed, so the *name* attached to a
        prefix is less trustworthy than the fact that it matched.
        """
        self.load()
        return self._by_prefix.get(squash(prefix))

    def prefix_count(self) -> int:
        self.load()
        return len(self._by_prefix)

    def stats(self) -> dict:
        self.load()
        return {
            "count": len(self._records),
            "stored": len(self._all),
            "last_success_at": self.get_meta("last_success_at", "never"),
            "last_attempt_at": self.get_meta("last_attempt_at", "never"),
            "last_error": self.get_meta("last_error", ""),
            "source": self.get_meta("source_url", config.MILITARY_CALLSIGN_URL),
            "db": str(self.db_path),
        }

    # -- matching ---------------------------------------------------------

    def fuzzy_candidates(self, token: str) -> Optional[tuple[CallsignRecord, float, str]]:
        """Nearest callsign to a single mangled token, or None.

        Deliberately narrow. Whisper turns "Falcon" into "Falcum" but it also
        turns "Centre" into "Center on" and "start" into "starting", and the
        registry holds 400+ words — so an unguarded neighbourhood search matches
        something on roughly every fourth transmission. Callers apply the
        lookalike and corroboration gates on top.
        """
        if token in self._token_cache:
            return self._token_cache[token]

        best: Optional[tuple[CallsignRecord, float, str]] = None
        best_dist = 99
        budget = edit_budget(len(token))
        for length in range(len(token) - budget - 1, len(token) + budget + 2):
            for key, cand in self._by_length.get(length, ()):
                if cand.callsign in config.MILITARY_CALLSIGN_AMBIGUOUS:
                    continue  # ambiguous callsigns are exact-match only
                allowed = min(budget, edit_budget(len(key)))
                if allowed <= 0:
                    continue
                dist = edit_distance(token, key, allowed + 1)
                if dist > allowed + 1:
                    continue
                if dist <= allowed:
                    kind, conf = "fuzzy", 1.0 - dist / (len(key) + 1.0)
                elif len(key) >= config.MILITARY_MIN_PHONETIC_LEN \
                        and token[0] == key[0] \
                        and phonetic_key(token) == phonetic_key(key):
                    kind, conf = "phonetic", config.MILITARY_PHONETIC_CONFIDENCE
                else:
                    continue
                if dist < best_dist:
                    best, best_dist = (cand, conf, kind), dist

        self._token_cache[token] = best
        return best

    def _lookalike(self, token: str) -> bool:
        """Is this heard token an ordinary word that merely resembles a callsign?"""
        if token in ATC_VOCABULARY:
            return True
        return token in _lookalike_words()

    def match(self, text: str) -> list[Match]:
        """Every military callsign detected in one transcript, best first."""
        if not text:
            return []
        self.load()
        if not self._records:
            return []

        words = _WORD_RE.findall(text)
        if not words:
            return []
        upper = [w.upper() for w in words]

        found: dict[str, Match] = {}
        i = 0
        while i < len(upper):
            hit = None
            span = 1
            # Longest window first: "WINDSOR CASTLE" beats "WINDSOR", and a
            # split-up "BLACK HAWK" rejoins into BLACKHAWK. Multi-word windows
            # are exact-only — squashing several words together invents
            # callsigns ("search for" → SEARCHER, "base at" → BASSETT).
            for width in range(min(self._max_words, len(upper) - i), 0, -1):
                window = upper[i:i + width]
                if any(w in _NUMBER_WORDS or any(c.isdigit() for c in w) for w in window):
                    continue
                key = squash("".join(window))
                if len(key) < config.MILITARY_MIN_CALLSIGN_LEN:
                    continue
                rec = self._by_squash.get(key)
                if rec is not None:
                    hit, span = (rec, 1.0, "exact"), width
                    break
                if width == 1 and config.MILITARY_FUZZY and not self._lookalike(key):
                    candidate = self.fuzzy_candidates(key)
                    if candidate is not None:
                        hit, span = candidate, width
                        break
            if hit is None:
                i += 1
                continue

            rec, confidence, kind = hit
            flight = _read_flight_number(upper, i + span)
            # Context has to come from somewhere other than the hit itself,
            # or ARMY would vouch for "the army is on the ground".
            has_context = bool(_MILITARY_CONTEXT_RE.search(
                " ".join(upper[:i] + upper[i + span:])
            ))
            verdict = self._admit(rec, kind, confidence, upper, i, span, flight, has_context)
            if verdict is None:
                i += 1
                continue

            matched_text = " ".join(words[i:i + span])
            match = Match(
                callsign=rec.callsign,
                aircraft=rec.aircraft,
                squadron=rec.squadron,
                matched_text=matched_text,
                flight=flight,
                confidence=confidence,
                kind=kind,
                strong=verdict,
            )
            previous = found.get(rec.callsign)
            if previous is None or (match.strong, match.confidence) > (previous.strong, previous.confidence):
                found[rec.callsign] = match
            i += span
        return sorted(found.values(), key=lambda m: (not m.strong, -m.confidence, m.callsign))

    def _admit(
        self,
        rec: CallsignRecord,
        kind: str,
        confidence: float,
        upper: list[str],
        start: int,
        span: int,
        flight: str,
        has_context: bool,
    ) -> Optional[bool]:
        """Second gate. True = strong (alertable), False = weak, None = drop.

        Military callsigns are spoken as callsign-plus-number ("Falcon one
        one", "Aussie six one three"). A bare callsign word with no number and
        nothing else military in the transmission is almost always the English
        word — the corpus has "inbound for the Mirage" and "I will reach two"
        far more often than it has MIRAGE or REACH.
        """
        if confidence < config.MILITARY_MIN_CONFIDENCE:
            return None

        # "Golf Kilo Delta" — a phonetic letter either side means this is a
        # registration being read back, not a callsign.
        before = upper[start - 1] if start > 0 else ""
        after = upper[start + span] if start + span < len(upper) else ""
        if before in PHONETIC_ALPHABET or after in PHONETIC_ALPHABET:
            return None

        ambiguous = rec.callsign in config.MILITARY_CALLSIGN_AMBIGUOUS
        everyday = ambiguous or squash(rec.callsign) in _lookalike_words()

        if kind == "exact":
            if not everyday:
                return True                      # WEDGETAIL, POSEIDON, ROULETTES
            if not flight and not has_context:
                return None                      # "inbound for the Mirage"
            if ambiguous and not has_context:
                return False                     # TIGER, REACH — annotate only
            return True

        # Misheard. Requires a flight number, and is only strong enough to
        # alert on when the transmission is militarily flavoured elsewhere.
        if not flight:
            return None
        if config.MILITARY_ALERT_ON_FUZZY:
            return True
        return bool(has_context)


def _read_flight_number(upper: list[str], index: int) -> str:
    """Collect the number spoken straight after a callsign ('FALCON ONE ONE')."""
    out: list[str] = []
    i = index
    while i < len(upper) and len(out) < 4:
        word = upper[i]
        if word.isdigit() and len(word) <= 4:
            out.append(word)
        elif word in _NUMBER_WORDS:
            out.append(word.title())
        elif not out and word in _FLIGHT_SUFFIX_WORDS:
            out.append(word.title())
            i += 1
            continue
        else:
            break
        i += 1
        # "five miles south" / "two thousand" — that number belongs to the
        # phrase after the callsign, not to the callsign.
        if i < len(upper) and upper[i] in _UNIT_WORDS:
            out.pop()
            break
    return " ".join(out)


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------

_registry: Optional[Registry] = None
_registry_lock = threading.Lock()


def get_registry() -> Registry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = Registry()
        return _registry


def detect(text: str) -> list[Match]:
    """Military callsigns mentioned in a transcript. Never raises."""
    if not config.MILITARY_DETECTION_ENABLED:
        return []
    try:
        return get_registry().match(text)
    except Exception:
        return []


def lookup_ident_prefix(prefix: str) -> Optional[CallsignRecord]:
    """Registry record for an ADS-B callsign prefix, or None. Never raises.

    Used by adsb_classify to turn 'TROJ23' into 'TROJAN, C-130J-30, 37SQN'.
    """
    if not prefix:
        return None
    try:
        return get_registry().lookup_prefix(prefix)
    except Exception:
        return None


def start_auto_refresh(
    stop_event: Optional[threading.Event] = None,
    on_result: Optional[Callable[[RefreshResult], None]] = None,
) -> threading.Thread:
    """Background daily refresh. Returns the (already started) daemon thread."""
    stop = stop_event or threading.Event()

    def _worker() -> None:
        # Small initial delay so a startup refresh never competes with model
        # loading and stream connects.
        if stop.wait(config.MILITARY_REFRESH_STARTUP_DELAY_SEC):
            return
        while not stop.is_set():
            try:
                result = get_registry().refresh()
                if on_result is not None and result.message != "up to date":
                    on_result(result)
            except Exception as exc:  # pragma: no cover - defensive
                if on_result is not None:
                    on_result(RefreshResult(False, f"refresh crashed: {exc}"))
            if stop.wait(config.MILITARY_REFRESH_CHECK_SEC):
                return

    thread = threading.Thread(target=_worker, daemon=True, name="callsign-refresh")
    thread.start()
    return thread


def build_lookalikes(dict_path: str = "/usr/share/dict/words",
                     out_path: Optional[Path] = None) -> int:
    """Regenerate the English-lookalike word list from a system dictionary.

    Keeps every lowercase dictionary word that is a callsign, or close enough to
    one that the fuzzy matcher would claim it. Proper nouns (capitalised in the
    dictionary file) are deliberately kept out — POSEIDON and AUSSIE are names,
    not words a controller says by accident.
    """
    reg = get_registry()
    reg.load()
    target = Path(out_path or config.MILITARY_LOOKALIKE_FILE)
    words = set()
    with open(dict_path, encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            word = raw.strip()
            if not word.islower() or not word.isalpha():
                continue
            key = squash(word)
            if len(key) < config.MILITARY_MIN_CALLSIGN_LEN or len(key) > 16:
                continue
            if key in reg._by_squash or reg.fuzzy_candidates(key) is not None:
                words.add(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")
    global _LOOKALIKE_CACHE
    _LOOKALIKE_CACHE = None
    return len(words)


def export_seed(path: Optional[Path] = None) -> int:
    """Write the current database out as the bundled seed snapshot."""
    reg = get_registry()
    records = reg.all_records()
    target = Path(path or reg.seed_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": config.MILITARY_CALLSIGN_URL,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "callsigns": [
            {
                "callsign": r.callsign,
                "prefixes": list(r.prefixes),
                "aircraft": r.aircraft,
                "squadron": r.squadron,
                "ops_freq": r.ops_freq,
                "confirmed": r.confirmed,
            }
            for r in records
        ],
    }
    target.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return len(records)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ADF military callsign registry")
    sub = parser.add_subparsers(dest="cmd")

    p_refresh = sub.add_parser("refresh", help="scrape the source page now")
    p_refresh.add_argument("--force", action="store_true",
                           help="refresh even if the data is still fresh")
    sub.add_parser("stats", help="show registry status")
    p_dump = sub.add_parser("dump", help="print stored callsigns")
    p_dump.add_argument("--limit", type=int, default=0)
    p_match = sub.add_parser("match", help="run the matcher over some text")
    p_match.add_argument("text", nargs="+")
    sub.add_parser("export-seed", help="write data/military_callsigns_seed.json")
    p_look = sub.add_parser("build-lookalikes",
                            help="regenerate data/english_lookalikes.txt")
    p_look.add_argument("--dict", default="/usr/share/dict/words")

    args = parser.parse_args(argv)
    reg = get_registry()

    if args.cmd == "refresh":
        result = reg.refresh(force=args.force)
        print(("✅ " if result.ok else "⚠ ") + result.message)
        return 0 if result.ok else 1

    if args.cmd == "stats":
        for k, v in reg.stats().items():
            print(f"{k:>16}: {v}")
        return 0

    if args.cmd == "dump":
        records = reg.all_records()
        if args.limit:
            records = records[:args.limit]
        for r in records:
            flag = "✓" if r.confirmed else " "
            print(f"{flag} {r.callsign:<20} {r.aircraft:<45} {r.squadron}")
        print(f"\n{len(records)} callsigns")
        return 0

    if args.cmd == "build-lookalikes":
        try:
            n = build_lookalikes(args.dict)
        except OSError as exc:
            print(f"⚠ {exc} — pass --dict with a word list")
            return 1
        print(f"wrote {n} lookalike words to {config.MILITARY_LOOKALIKE_FILE}")
        return 0

    if args.cmd == "export-seed":
        n = export_seed()
        print(f"wrote {n} callsigns to {reg.seed_path}")
        return 0

    if args.cmd == "match":
        text = " ".join(args.text)
        matches = reg.match(text)
        if not matches:
            print("no military callsigns detected")
            return 0
        for m in matches:
            print(f"• {m.describe()}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
