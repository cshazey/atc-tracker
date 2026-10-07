"""Named airspace zones: where they are, who is in them, who is about to be.

Replaces the single hard-coded airshow display box with any number of named
volumes loaded from data/airspace_zones.json. Each zone carries its own floor,
ceiling, entry hysteresis, dwell and — the important part — the set of
aircraft *roles* that are worth an alert on entry. A police helicopter in the
Gold Coast CTR is news; a Hercules in the Amberley circuit is not.

The occupancy state machine itself lives in adsb_tracker, next to the presence
machine it mirrors. This module is pure geometry and data, so the tests can
exercise it without a poller.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

import config
import geo

ROLE_MILITARY = "military"
ROLE_PROBABLE = "probable"
ROLE_WATCH = "watch"
ROLE_EMERGENCY = "emergency"
ROLE_SPECIAL = "special"
ROLE_NOTABLE = "notable"
ROLE_ANON_FAST = "anon_fast"

ROLES = (
    ROLE_EMERGENCY, ROLE_WATCH, ROLE_MILITARY, ROLE_PROBABLE,
    ROLE_SPECIAL, ROLE_NOTABLE, ROLE_ANON_FAST,
)

ROLE_LABELS = {
    ROLE_MILITARY: "military",
    ROLE_PROBABLE: "probable military",
    ROLE_WATCH: "watchlist",
    ROLE_EMERGENCY: "emergency",
    ROLE_SPECIAL: "emergency services / government",
    ROLE_NOTABLE: "notable type",
    ROLE_ANON_FAST: "unidentified fast mover",
}


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    kind: str = "custom"
    polygon: tuple = ()
    center: Optional[tuple] = None
    radius_nm: float = 0.0
    floor_ft: Optional[float] = None
    ceiling_ft: Optional[float] = None
    hyst_nm: float = 0.5
    dwell_sec: float = 20.0
    triggers: frozenset = frozenset()
    severity: int = 1
    color: str = "#e67e22"
    enabled: bool = True
    anon_signal: bool = False
    lost_sec: Optional[float] = None
    predict: bool = True
    note: str = ""
    # Quick-reject box, already grown by the hysteresis margin.
    bbox: tuple = field(default=(0.0, 0.0, 0.0, 0.0), compare=False)

    def contains(self, lat, lon, alt_ft=None, strict: bool = True) -> bool:
        """Is this point inside the volume?

        strict=False tests the zone grown by hyst_nm — the outer edge of the
        dead band a track must clear before it counts as having left. Unknown
        altitude is not treated as outside: we only exclude what we can place.
        """
        if lat is None or lon is None:
            return False
        if alt_ft is not None:
            if self.ceiling_ft is not None and alt_ft > self.ceiling_ft:
                return False
            if self.floor_ft is not None and alt_ft < self.floor_ft:
                return False
        if not geo.point_in_bbox(lat, lon, self.bbox):
            return False
        margin = 0.0 if strict else self.hyst_nm
        if self.center is not None:
            return geo.haversine_nm(self.center[0], self.center[1], lat, lon) <= self.radius_nm + margin
        if geo.point_in_polygon(lat, lon, self.polygon):
            return True
        return margin > 0 and geo.distance_to_polygon_nm(lat, lon, self.polygon) <= margin

    def outline(self) -> list:
        if self.center is not None:
            return geo.circle_polygon(self.center[0], self.center[1], self.radius_nm)
        return list(self.polygon)

    def alt_text(self) -> str:
        lo = "SFC" if not self.floor_ft else f"{self.floor_ft:,.0f} ft"
        hi = "UNL" if self.ceiling_ft is None else f"{self.ceiling_ft:,.0f} ft"
        return f"{lo}–{hi}"

    def wants(self, roles) -> bool:
        return bool(self.triggers & set(roles))

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "color": self.color,
            "enabled": self.enabled,
            "severity": self.severity,
            "alt": self.alt_text(),
            "triggers": sorted(self.triggers),
            "note": self.note,
            "outline": [[round(a, 5), round(b, 5)] for a, b in self.outline()],
        }


def _bbox_for(center, radius_nm, polygon, margin_nm) -> tuple:
    if center is not None:
        r = radius_nm + margin_nm
        dlat = r / geo.NM_PER_DEG_LAT
        dlon = r / max(geo.nm_per_deg_lon(center[0]), 0.001)
        return (center[0] - dlat, center[0] + dlat, center[1] - dlon, center[1] + dlon)
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return geo.inflate_bbox((min(lats), max(lats), min(lons), max(lons)), margin_nm)


def _opt_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def zone_from_dict(raw: dict) -> Optional[Zone]:
    """Build one Zone from its JSON entry, or None if it is unusable."""
    try:
        zid = str(raw["id"]).strip().lower()
        name = str(raw.get("name") or zid)
        center, radius, polygon = None, 0.0, ()
        if isinstance(raw.get("circle"), dict):
            c = raw["circle"]
            center = (float(c["lat"]), float(c["lon"]))
            radius = float(c["radius_nm"])
            if radius <= 0:
                return None
        else:
            polygon = tuple((float(p[0]), float(p[1])) for p in raw.get("polygon") or ())
            if len(polygon) < 3:
                return None
        hyst = float(raw.get("hyst_nm", config.AIRSPACE_DEFAULT_HYST_NM))
        triggers = frozenset(
            str(t).strip().lower() for t in raw.get("triggers", ()) if str(t).strip() in ROLES
        )
        return Zone(
            id=zid,
            name=name,
            kind=str(raw.get("kind", "custom")),
            polygon=polygon,
            center=center,
            radius_nm=radius,
            floor_ft=_opt_float(raw.get("floor_ft")),
            ceiling_ft=_opt_float(raw.get("ceiling_ft")),
            hyst_nm=hyst,
            dwell_sec=float(raw.get("dwell_sec", config.AIRSPACE_DEFAULT_DWELL_SEC)),
            triggers=triggers,
            severity=int(raw.get("severity", 1)),
            color=str(raw.get("color", "#e67e22")),
            enabled=bool(raw.get("enabled", True)),
            anon_signal=bool(raw.get("anon_signal", False)),
            lost_sec=_opt_float(raw.get("lost_sec")),
            predict=bool(raw.get("predict", True)),
            note=str(raw.get("note", "")),
            bbox=_bbox_for(center, radius, polygon, hyst),
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def legacy_box_zone(poly) -> Zone:
    """The pre-zones ADSB_BOX_* setting, carried over as a zone called 'custom'."""
    return zone_from_dict({
        "id": "custom",
        "name": "Custom box",
        "kind": "custom",
        "polygon": [list(p) for p in poly],
        "ceiling_ft": config._LEGACY_BOX_CEILING_FT,
        "triggers": list(ROLES),
        "anon_signal": True,
        "lost_sec": 60,
        "color": "#e74c3c",
    })


class ZoneSet:
    """The live zone list. Re-reads its file when it changes on disk.

    Readers take a reference to the current tuple and never see a half-built
    list; a reload swaps the whole thing.
    """

    def __init__(self, path: Optional[str] = None, zones: Optional[list] = None,
                 overrides: Optional[dict] = None):
        self.path = path if path is not None else config.AIRSPACE_ZONES_FILE
        self._lock = threading.Lock()
        self._mtime: Optional[float] = None
        self._fixed = zones is not None
        self._all: tuple = tuple(zones or ())
        self.overrides: dict = dict(overrides or {})
        self.error = ""
        if not self._fixed:
            self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        if self._fixed:
            return False
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = 0.0
        if not force and mtime == self._mtime:
            return False
        with self._lock:
            self._mtime = mtime
            zones = []
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                for entry in raw.get("zones", []):
                    z = zone_from_dict(entry)
                    if z is not None and all(z.id != other.id for other in zones):
                        zones.append(z)
                self.error = ""
            except FileNotFoundError:
                self.error = f"{self.path} not found"
            except Exception as exc:
                # Keep the previous zones rather than going blind on a typo.
                self.error = f"{os.path.basename(self.path)}: {exc}"
                return False
            if config.AIRSPACE_LEGACY_BOX:
                box = legacy_box_zone(config.AIRSPACE_LEGACY_BOX)
                if box is not None:
                    zones.append(box)
            self._all = tuple(zones)
        return True

    def all(self) -> tuple:
        return self._all

    def active(self) -> list:
        return [z for z in self._all if self.is_enabled(z)]

    def is_enabled(self, zone: Zone) -> bool:
        return bool(self.overrides.get(zone.id, zone.enabled))

    def get(self, zone_id: str) -> Optional[Zone]:
        zone_id = (zone_id or "").strip().lower()
        for z in self._all:
            if z.id == zone_id:
                return z
        return None

    def find(self, needle: str) -> Optional[Zone]:
        """By id, or by a case-insensitive fragment of the name."""
        z = self.get(needle)
        if z is not None:
            return z
        needle = (needle or "").strip().lower()
        hits = [z for z in self._all if needle and needle in z.name.lower()]
        return hits[0] if len(hits) == 1 else None

    def set_enabled(self, zone_id: str, on: bool) -> bool:
        z = self.get(zone_id)
        if z is None:
            return False
        self.overrides[z.id] = bool(on)
        return True


def predict_entry(
    zone: Zone,
    lat: float,
    lon: float,
    alt_ft: Optional[float],
    gs_kt: Optional[float],
    track_deg: Optional[float],
    baro_rate: Optional[float],
    horizon_sec: float,
    step_sec: float,
) -> Optional[tuple]:
    """First (eta_sec, lat, lon, alt_ft) at which a straight-line projection is
    inside the zone, or None.

    Deliberately simple dead reckoning: constant ground track, speed and
    vertical rate. Anything cleverer would be guessing at intent, and the
    caller re-runs this every poll anyway, so a turn simply cancels it.
    """
    if lat is None or lon is None or not gs_kt or track_deg is None or step_sec <= 0:
        return None
    t = step_sec
    while t <= horizon_sec + 1e-6:
        plat, plon = geo.destination_point(lat, lon, track_deg, gs_kt * t / 3600.0)
        palt = None
        if alt_ft is not None:
            palt = max(0.0, alt_ft + (baro_rate or 0) * t / 60.0)
        if zone.contains(plat, plon, palt, strict=True):
            return t, plat, plon, palt
        t += step_sec
    return None
