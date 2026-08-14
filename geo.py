"""Geometry helpers: distances, bearings, and area membership tests.

Spherical-earth model. Over the <=100 NM area this tracker watches, the
small-angle errors are far below ADS-B position noise.

``haversine_nm``, ``bearing_deg``, ``destination_point`` and ``angle_diff`` are
adapted from the sibling flight-tracker project's ``backend/geo.py``. They are
copied rather than imported: the two repos have separate dependency sets and
release cadences, and a cross-repo import would break the moment either moves.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

EARTH_RADIUS_NM = 3440.065  # nautical miles
NM_PER_DEG_LAT = 60.0

# 16-point compass, indexed by (bearing + 11.25) // 22.5.
_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from point 1 to point 2 in degrees (0-360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(
    lat: float, lon: float, bearing: float, dist_nm: float
) -> tuple[float, float]:
    """Point reached from (lat, lon) travelling dist_nm along a true bearing."""
    ang = dist_nm / EARTH_RADIUS_NM
    br = math.radians(bearing)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(
        math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(br)
    )
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(ang) * math.cos(p1),
        math.cos(ang) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def angle_diff(a: float, b: float) -> float:
    """Smallest signed difference a-b, wrapped to [-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


def compass_point(bearing: float) -> str:
    """16-point compass label for a bearing. 349.4 -> 'NNW'."""
    return _COMPASS[int((bearing % 360.0) / 22.5 + 0.5) % 16]


def nm_per_deg_lon(lat: float) -> float:
    """Nautical miles per degree of longitude at a given latitude.

    At the Gold Coast (28 S) this is ~53 NM, not 60 — which matters when
    inflating a bounding box by a distance rather than by degrees.
    """
    return NM_PER_DEG_LAT * math.cos(math.radians(lat))


# --- area membership -------------------------------------------------------
#
# A bbox is (lat_min, lat_max, lon_min, lon_max). Kept as a plain tuple rather
# than a class so it can come straight out of a comma-separated .env value.


def point_in_bbox(lat: float, lon: float, bbox: Sequence[float]) -> bool:
    lat_min, lat_max, lon_min, lon_max = bbox
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def inflate_bbox(bbox: Sequence[float], nm: float) -> tuple[float, float, float, float]:
    """Grow a bbox outward by nm on every side.

    Used for exit hysteresis: an aircraft is "inside" the true box but only
    "outside" once it clears the inflated one, so a track sitting on the
    boundary cannot oscillate.
    """
    lat_min, lat_max, lon_min, lon_max = bbox
    dlat = nm / NM_PER_DEG_LAT
    # Widen using whichever edge latitude gives the larger longitude delta, so
    # the inflated box is never narrower than requested anywhere along it.
    per_lon = min(nm_per_deg_lon(lat_min), nm_per_deg_lon(lat_max))
    dlon = nm / per_lon if per_lon > 0.001 else 180.0
    return (lat_min - dlat, lat_max + dlat, lon_min - dlon, lon_max + dlon)


def point_in_polygon(lat: float, lon: float, poly: Sequence[Sequence[float]]) -> bool:
    """Ray-casting point-in-polygon. ``poly`` is a sequence of (lat, lon).

    Treats the polygon as closed regardless of whether the last vertex repeats
    the first. Behaviour exactly on an edge is not defined (and does not matter
    here — the caller applies dwell and hysteresis on top).
    """
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lon_i = poly[i][0], poly[i][1]
        lat_j, lon_j = poly[j][0], poly[j][1]
        if (lon_i > lon) != (lon_j > lon):
            # Longitude of the edge at this point's longitude-crossing.
            t = (lon - lon_i) / (lon_j - lon_i)
            if lat < lat_i + t * (lat_j - lat_i):
                inside = not inside
        j = i
    return inside


def polygon_centroid(poly: Sequence[Sequence[float]]) -> tuple[float, float]:
    return (
        sum(p[0] for p in poly) / len(poly),
        sum(p[1] for p in poly) / len(poly),
    )


def inflate_polygon(
    poly: Sequence[Sequence[float]], nm: float
) -> list[tuple[float, float]]:
    """Push every vertex radially outward from the centroid by nm.

    Crude compared to a proper Minkowski offset, but the shapes here are
    near-rectangular display boxes and this only has to produce a dead band
    wider than position jitter.
    """
    clat, clon = polygon_centroid(poly)
    out: list[tuple[float, float]] = []
    for lat, lon in ((p[0], p[1]) for p in poly):
        d = haversine_nm(clat, clon, lat, lon)
        if d < 0.001:
            out.append((lat, lon))
            continue
        out.append(destination_point(clat, clon, bearing_deg(clat, clon, lat, lon), d + nm))
    return out


def bbox_to_polygon(bbox: Sequence[float]) -> list[tuple[float, float]]:
    lat_min, lat_max, lon_min, lon_max = bbox
    return [
        (lat_min, lon_min),
        (lat_min, lon_max),
        (lat_max, lon_max),
        (lat_max, lon_min),
    ]


# --- motion ----------------------------------------------------------------


def closing_rate_kt(samples: Sequence[Sequence[float]]) -> Optional[float]:
    """Closing speed in knots from (t_epoch, dist_nm) samples.

    Positive means the range is shrinking. Returns None when there is not
    enough to work with: fewer than two samples, or a span under MIN_SPAN_S.
    That floor matters — over a 2-second span, ordinary ADS-B position jitter
    of a tenth of a mile reads as 180 knots of closure.
    """
    MIN_SPAN_S = 5.0
    if len(samples) < 2:
        return None
    t0, d0 = samples[0][0], samples[0][1]
    t1, d1 = samples[-1][0], samples[-1][1]
    span = t1 - t0
    if span < MIN_SPAN_S:
        return None
    return (d0 - d1) / span * 3600.0
