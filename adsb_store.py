"""SQLite store for ADS-B sightings, alert dedupe, and an event log.

The alert dedupe is the point of this module. Until now nothing in this project
remembered what it had already said across a restart — restart the tracker
mid-airshow and it would cheerfully re-announce every aircraft it could see.
``should_alert``/``mark_alert`` close that gap, and ``last_seen`` lets the
presence machine tell "this aircraft just switched its transponder on" apart
from "this process just started".

Conventions follow military_callsigns.Registry: a connection per operation
(SQLite is happy with that and it sidesteps cross-thread handle sharing), the
schema applied once per store on first connect, and upserts written as
ON CONFLICT ... DO UPDATE.

Two naming notes. The description column is ``descr`` because DESC is a SQL
keyword. Locals holding an ICAO address are ``hex_`` because ``hex`` shadows a
builtin.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    hex        TEXT PRIMARY KEY,
    ident      TEXT NOT NULL DEFAULT '',
    reg        TEXT NOT NULL DEFAULT '',
    typ        TEXT NOT NULL DEFAULT '',
    descr      TEXT NOT NULL DEFAULT '',
    owner      TEXT NOT NULL DEFAULT '',
    military   INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    signals    TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL DEFAULT 0,
    last_seen  REAL NOT NULL DEFAULT 0,
    last_lat   REAL,
    last_lon   REAL,
    last_alt   INTEGER,
    seen_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alerts (
    hex     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    last_at REAL NOT NULL,
    PRIMARY KEY (hex, kind)
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     REAL NOT NULL,
    hex    TEXT NOT NULL,
    kind   TEXT NOT NULL,
    ident  TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
CREATE INDEX IF NOT EXISTS idx_events_hex ON events(hex);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class AdsbStore:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path or config.ADSB_STATE_DB)
        self._lock = threading.Lock()
        # Small write-through cache. last_seen is read once per new aircraft
        # per poll, and going to disk for each would be pointless I/O in a
        # process that is already busy running speech recognition.
        self._last_seen: dict = {}
        self._alerts: dict = {}
        self._loaded = False
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._schema_ready:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA synchronous=NORMAL")
        if not self._schema_ready:
            # WAL lets the live map read while the poller writes every 10 s.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._schema_ready = True
        return conn

    def load(self) -> None:
        """Warm the caches from disk. Safe to call repeatedly."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                with self._connect() as conn:
                    for hex_, last in conn.execute("SELECT hex, last_seen FROM sightings"):
                        self._last_seen[hex_] = last
                    for hex_, kind, last in conn.execute(
                        "SELECT hex, kind, last_at FROM alerts"
                    ):
                        self._alerts[(hex_, kind)] = last
            except Exception:
                # A corrupt or unreadable database must not stop tracking; the
                # cost is one restart's worth of duplicate alerts.
                pass
            self._loaded = True

    # -- sightings --------------------------------------------------------

    def last_seen(self, hex_: str) -> Optional[float]:
        self.load()
        return self._last_seen.get(hex_)

    def last_seen_bulk(self, hexes: Iterable[str]) -> dict:
        self.load()
        return {h: self._last_seen[h] for h in hexes if h in self._last_seen}

    def record_sightings(self, tracks, now: float) -> int:
        """Persist every current track in one transaction.

        One connection per poll, not one per aircraft — at 50 aircraft every
        10 seconds the latter would be 5 connections a second for no reason.
        """
        rows = []
        for t in tracks:
            cls = t.classification
            rows.append(
                (
                    t.hex, t.ident, t.reg, t.typ, t.descr, t.owner,
                    1 if cls.military else 0,
                    float(cls.confidence),
                    json.dumps([s.code for s in cls.signals]),
                    t.first_seen or now, t.last_seen or now,
                    t.lat, t.lon, t.alt_ft, t.seen_count,
                )
            )
        if not rows:
            return 0
        try:
            with self._lock, self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO sightings (hex, ident, reg, typ, descr, owner,
                        military, confidence, signals, first_seen, last_seen,
                        last_lat, last_lon, last_alt, seen_count)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(hex) DO UPDATE SET
                        ident=excluded.ident, reg=excluded.reg, typ=excluded.typ,
                        descr=excluded.descr, owner=excluded.owner,
                        military=excluded.military, confidence=excluded.confidence,
                        signals=excluded.signals, last_seen=excluded.last_seen,
                        last_lat=excluded.last_lat, last_lon=excluded.last_lon,
                        last_alt=excluded.last_alt, seen_count=excluded.seen_count
                    """,
                    rows,
                )
            for row in rows:
                self._last_seen[row[0]] = row[10]
        except Exception:
            return 0
        return len(rows)

    # -- alert dedupe -----------------------------------------------------

    def should_alert(self, hex_: str, kind: str, cooldown_sec: float, now: float) -> bool:
        self.load()
        last = self._alerts.get((hex_, kind))
        return last is None or (now - last) >= cooldown_sec

    def mark_alert(self, hex_: str, kind: str, now: float) -> None:
        self.load()
        self._alerts[(hex_, kind)] = now
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO alerts (hex, kind, last_at) VALUES (?,?,?)
                    ON CONFLICT(hex, kind) DO UPDATE SET last_at=excluded.last_at
                    """,
                    (hex_, kind, now),
                )
        except Exception:
            pass

    # -- event log --------------------------------------------------------

    def log_event(self, ev) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO events (at, hex, kind, ident, detail) VALUES (?,?,?,?,?)",
                    (ev.at, ev.hex, ev.kind, ev.track.ident, ev.detail[:500]),
                )
        except Exception:
            pass

    def recent_events(self, limit: int = 20, hex_: str = "") -> list:
        try:
            with self._connect() as conn:
                if hex_:
                    cur = conn.execute(
                        "SELECT at, hex, kind, ident, detail FROM events "
                        "WHERE hex = ? ORDER BY at DESC LIMIT ?",
                        (hex_.lower(), limit),
                    )
                else:
                    cur = conn.execute(
                        "SELECT at, hex, kind, ident, detail FROM events "
                        "ORDER BY at DESC LIMIT ?",
                        (limit,),
                    )
                return [
                    {"at": a, "hex": h, "kind": k, "ident": i, "detail": d}
                    for a, h, k, i, d in cur
                ]
        except Exception:
            return []

    def history(self, hex_: str) -> Optional[dict]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT hex, ident, reg, typ, descr, owner, military, confidence,"
                    " first_seen, last_seen, seen_count FROM sightings WHERE hex = ?",
                    (hex_.lower(),),
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        keys = (
            "hex", "ident", "reg", "typ", "descr", "owner", "military",
            "confidence", "first_seen", "last_seen", "seen_count",
        )
        return dict(zip(keys, row))

    # -- housekeeping -----------------------------------------------------

    def prune(self, older_than_days: float = 14.0) -> int:
        cutoff = time.time() - older_than_days * 86400
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
                conn.execute("DELETE FROM alerts WHERE last_at < ?", (cutoff,))
                return cur.rowcount or 0
        except Exception:
            return 0

    def stats(self) -> dict:
        try:
            with self._connect() as conn:
                def count(table):
                    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                mil = conn.execute(
                    "SELECT COUNT(*) FROM sightings WHERE military = 1"
                ).fetchone()[0]
                return {
                    "db": str(self.db_path),
                    "sightings": count("sightings"),
                    "military_seen": mil,
                    "alerts": count("alerts"),
                    "events": count("events"),
                }
        except Exception as exc:
            return {"db": str(self.db_path), "error": str(exc)}

    def get_meta(self, key: str, default: str = "") -> str:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row else default
        except Exception:
            return default

    def set_meta(self, key: str, value: str) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )
        except Exception:
            pass
