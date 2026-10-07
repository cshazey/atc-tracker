"""Offline checks for airspace zones (airspace.py) and the zone data file.

Same hand-rolled shape as the other test_*.py files: no pytest, no network.

    python3 test_airspace.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import airspace as A
import config
import geo

failures: list = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def near(label: str, got, want, tol) -> None:
    if got is not None and abs(got - want) <= tol:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r} +-{tol}")
        failures.append(label)


def section(name: str) -> None:
    print(f"\n{name}")


YBCG = (-28.1644, 153.5047)

section("Geometry helpers")
square = [(-28.0, 153.0), (-28.0, 153.1), (-27.9, 153.1), (-27.9, 153.0)]
near("distance to a polygon edge from outside", geo.distance_to_polygon_nm(-28.0 - 1 / 60, 153.05, square), 1.0, 0.01)
# Nearest edge is east/west: 0.05 deg of longitude at 28 S is ~2.65 nm.
near("distance to a polygon edge from inside", geo.distance_to_polygon_nm(-27.95, 153.05, square), 2.65, 0.02)
near("distance to a vertex", geo.distance_to_polygon_nm(-28.0 - 1 / 60, 153.0 - 1 / 53.0, square), 1.41, 0.05)
_c = geo.circle_polygon(YBCG[0], YBCG[1], 10, points=24)
check("circle polygon has the requested points", len(_c), 24)
near("circle polygon vertices sit on the radius", geo.haversine_nm(*YBCG, *_c[5]), 10.0, 0.01)

section("Zone construction")
circle = A.zone_from_dict({
    "id": "CTR", "name": "Test CTR", "circle": {"lat": YBCG[0], "lon": YBCG[1], "radius_nm": 10},
    "floor_ft": 500, "ceiling_ft": 4500, "triggers": ["military", "nonsense"],
})
check("ids are lower-cased", circle.id, "ctr")
check("unknown roles are dropped from triggers", circle.triggers, frozenset({"military"}))
check("defaults come from config", (circle.hyst_nm, circle.dwell_sec),
      (config.AIRSPACE_DEFAULT_HYST_NM, config.AIRSPACE_DEFAULT_DWELL_SEC))
check("altitude text", circle.alt_text(), "500 ft–4,500 ft")
check("a zone with no shape is rejected", A.zone_from_dict({"id": "x"}), None)
check("a two-point polygon is rejected", A.zone_from_dict({"id": "x", "polygon": [[0, 0], [1, 1]]}), None)
check("a zero-radius circle is rejected",
      A.zone_from_dict({"id": "x", "circle": {"lat": 0, "lon": 0, "radius_nm": 0}}), None)

section("Containment")
check("centre is inside", circle.contains(*YBCG, 2000), True)
check("below the floor is outside", circle.contains(*YBCG, 400), False)
check("above the ceiling is outside", circle.contains(*YBCG, 4600), False)
check("unknown altitude counts as inside", circle.contains(*YBCG, None), True)
_edge_out = geo.destination_point(*YBCG, 0.0, 10.3)
check("0.3 nm outside the edge is outside strictly", circle.contains(*_edge_out, 2000, strict=True), False)
check("...but inside the hysteresis band", circle.contains(*_edge_out, 2000, strict=False), True)
_far = geo.destination_point(*YBCG, 0.0, 11.0)
check("1 nm outside is outside either way", circle.contains(*_far, 2000, strict=False), False)
check("a missing position is never inside", circle.contains(None, None, 1000), False)

corridor = A.zone_from_dict({
    "id": "long", "name": "Long corridor",
    "polygon": [[-28.0, 153.40], [-28.0, 153.42], [-27.5, 153.42], [-27.5, 153.40]],
    "hyst_nm": 0.5,
})
# The point of distance_to_polygon over inflate_polygon: a long thin strip
# gets a real dead band along its sides, not just at its ends.
_side = (-27.75, 153.42 + 0.3 / geo.nm_per_deg_lon(-27.75))
check("a point 0.3 nm off a corridor's side is in the dead band",
      (corridor.contains(*_side, strict=True), corridor.contains(*_side, strict=False)), (False, True))

section("Prediction")
_south = geo.destination_point(*YBCG, 180.0, 22.0)
hit = A.predict_entry(circle, _south[0], _south[1], 3000, 240, 0.0, 0, 300, 30)
check("a track heading at the zone is predicted", hit is not None, True)
near("...with the right ETA (12 nm at 4 nm/min)", hit[0], 180, 30)
check("a track heading away is not", A.predict_entry(circle, _south[0], _south[1], 3000, 240, 180.0, 0, 300, 30), None)
check("too far for the horizon is not", A.predict_entry(circle, _south[0], _south[1], 3000, 60, 0.0, 0, 300, 30), None)
check("a climb through the ceiling first is not",
      A.predict_entry(circle, _south[0], _south[1], 4000, 240, 0.0, 3000, 300, 30), None)
check("no ground speed, no prediction", A.predict_entry(circle, _south[0], _south[1], 3000, 0, 0.0, 0, 300, 30), None)

section("Zone set")
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "zones.json"
    path.write_text(json.dumps({"zones": [
        {"id": "a", "name": "Alpha", "circle": {"lat": 0, "lon": 0, "radius_nm": 5}},
        {"id": "b", "name": "Bravo Zone", "circle": {"lat": 1, "lon": 1, "radius_nm": 5}, "enabled": False},
        {"id": "a", "name": "Duplicate", "circle": {"lat": 2, "lon": 2, "radius_nm": 5}},
        {"id": "bad"},
    ]}))
    zs = A.ZoneSet(path=str(path))
    check("duplicates and bad entries are skipped", [z.id for z in zs.all()], ["a", "b"])
    check("disabled zones are not active", [z.id for z in zs.active()], ["a"])
    check("find by name fragment", zs.find("bravo").id, "b")
    check("runtime override enables", (zs.set_enabled("b", True), [z.id for z in zs.active()]), (True, ["a", "b"]))
    check("unknown zone cannot be toggled", zs.set_enabled("zzz", True), False)
    path.write_text("{ not json")
    check("a broken edit keeps the previous zones", (zs.reload(force=True), [z.id for z in zs.all()]), (False, ["a", "b"]))
    check("...and reports why", bool(zs.error), True)

_saved = config.AIRSPACE_LEGACY_BOX
config.AIRSPACE_LEGACY_BOX = [(-28.06, 153.42), (-28.06, 153.52), (-27.94, 153.52), (-27.94, 153.42)]
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "zones.json"
    path.write_text(json.dumps({"zones": []}))
    zs = A.ZoneSet(path=str(path))
    check("a legacy ADSB_BOX setting becomes a 'custom' zone", [z.id for z in zs.all()], ["custom"])
    check("...that alerts on every role", zs.get("custom").triggers, frozenset(A.ROLES))
config.AIRSPACE_LEGACY_BOX = _saved

section("Shipped zones (data/airspace_zones.json)")
shipped = A.ZoneSet()
check("the file loads cleanly", shipped.error, "")
ids = {z.id for z in shipped.all()}
check("the Gold Coast zones are present", {"ybcg_ctr", "gc_coast", "gc_area"} <= ids, True)
check("the military areas are present", {"yamb_ctr", "evans_head"} <= ids, True)
check("YBCG is inside the Gold Coast CTR", shipped.get("ybcg_ctr").contains(*YBCG, 0), True)
check("YBCG is not on the coastal corridor", shipped.get("gc_coast").contains(*YBCG, 0), False)
check("Surfers beachfront at 1,000 ft is on the corridor", shipped.get("gc_coast").contains(-28.00, 153.432, 1000), True)
check("...but not at 3,000 ft", shipped.get("gc_coast").contains(-28.00, 153.432, 3000), False)
_poll_radius = config.ADSB_RADIUS_NM
_beyond = [
    z.id for z in shipped.active()
    if z.center and geo.haversine_nm(*YBCG, *z.center) > _poll_radius
]
check("every enabled zone centre is inside the ADS-B poll radius", _beyond, [])
check("every zone triggers on emergencies", [z.id for z in shipped.all() if "emergency" not in z.triggers], [])

print()
if failures:
    print(f"❌ {len(failures)} check(s) failed:")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print("✅ all airspace checks passed")
