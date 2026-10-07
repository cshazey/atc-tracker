"""Offline checks for the ADS-B tracking modules.

Same hand-rolled shape as test_filters.py / test_military.py: no pytest, no
network, no threads, no writes outside a temp directory.

    python3 test_adsb.py

The fixtures are real records captured from adsb.fi over the Gold Coast during
the Pacific Airshow on 14 Aug 2026. They are the reason this feature is built
the way it is, so they are pinned here as regression guards — in particular
"a dbFlags-only filter would have seen none of this".
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import adsb_classify as C
import adsb_source as S
import adsb_tracker as T
import airspace as A
import config
import geo
import military_callsigns as M

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


# --- fixtures --------------------------------------------------------------
# Verbatim from the live feed. Do not "tidy" these.

RAW_T63 = {
    "hex": "7c64cd", "type": "adsb_icao", "flight": "T63     ", "r": None,
    "t": None, "desc": None, "ownOp": None, "dbFlags": None,
    "alt_baro": 1225, "gs": 242.4, "track": 60.05,
    "lat": -27.972473, "lon": 153.473775, "squawk": "1756", "category": "A1",
}
RAW_SIC = {
    "hex": "7c5c42", "flight": "SIC     ", "r": "VH-SIC", "t": "L39",
    "desc": "LET L-39 Albatros", "ownOp": "PERFORMANCE AERO PTY LTD",
    "dbFlags": 8, "alt_baro": 5250, "gs": 238.9, "track": 190.37,
    "lat": -28.010109, "lon": 153.083496, "squawk": "1200", "category": "A1",
}
RAW_JPV = {
    "hex": "7c2fc1", "flight": "JPV     ", "r": "VH-JPV", "t": "JPRO",
    "desc": "BAC P-84 Jet Provost", "ownOp": "POVAIR PTY. LTD.",
    "alt_baro": 1000, "gs": 122.3, "lat": -28.119186, "lon": 153.498336,
    "squawk": "3311", "category": "A2",
}
RAW_TROJ = {
    "hex": "7cf839", "flight": "TROJ23  ", "r": "A97-466", "t": "C130",
    "dbFlags": 1, "alt_baro": 9600, "gs": 237.0,
    "lat": -30.0, "lon": 151.0, "squawk": "2047",
}
RAW_PA28 = {
    "hex": "7c6ac9", "flight": "VDN     ", "r": "VH-VDN", "t": "P28A",
    "desc": "PIPER PA-28-140/150/160/180", "ownOp": "WOODWARD, Neil Andrew",
    "alt_baro": 6825, "gs": 110.0, "lat": -28.12, "lon": 152.43, "squawk": "2542",
}
RAW_GROUND = {
    "hex": "7c5e43", "flight": "SWL     ", "r": "VH-SWL", "t": "AS50",
    "ownOp": "SEA WORLD HELICOPTERS PTY LTD", "alt_baro": "ground",
    "lat": -27.958, "lon": 153.423, "squawk": "6515", "category": "A7",
}

BOX_LAT, BOX_LON = -27.99, 153.47          # inside the test zone below
OUT_LAT, OUT_LON = -28.30, 153.20          # well outside it

# The old airshow display box, as a zone. The tests use a fixed zone set
# rather than data/airspace_zones.json so editing that file cannot break them.
TEST_ZONE = A.zone_from_dict({
    "id": "testbox", "name": "Test box",
    "polygon": [[-28.06, 153.42], [-28.06, 153.52], [-27.94, 153.52], [-27.94, 153.42]],
    "ceiling_ft": 10000, "triggers": list(A.ROLES), "anon_signal": True,
    "lost_sec": 60, "hyst_nm": 0.5, "dwell_sec": 20,
})


def test_zones():
    return A.ZoneSet(zones=[TEST_ZONE])


def rep(**kw):
    base = dict(hex="abc123", source="test", received_at=0.0)
    base.update(kw)
    return S.AircraftReport(**base)


def mil_rep(hex_="7cf839", ident="TROJ23", **kw):
    return rep(hex=hex_, ident=ident, reg="A97-466", typ="C130", dbflags=1, **kw)


def make_poller(store=False, zones=None, **_):
    # store=False means "no store at all" — these tests must not touch the
    # real data/adsb_state.db. Pass an AdsbStore on a temp path to exercise it.
    p = T.AdsbPoller(store=store, zones=zones if zones is not None else test_zones())
    p.source = None  # nothing in these tests may touch the network
    return p


def drive(poller, frames, start=1_000_000.0, step=10.0):
    """Run a sequence of per-poll report lists. Returns every event emitted."""
    events = []
    for i, reports in enumerate(frames):
        events.extend(poller.poll_once(reports, start + i * step))
    return events


def kinds(events):
    return [e.kind for e in events]


# --- geo -------------------------------------------------------------------

section("Geometry")
near("YBCG -> YAMB distance", geo.haversine_nm(-28.1644, 153.5047, -27.641, 152.712), 52.5, 0.3)
near("YBCG -> YAMB bearing", geo.bearing_deg(-28.1644, 153.5047, -27.641, 152.712), 307.0, 1.0)
near("YBCG -> YBBN distance", geo.haversine_nm(-28.1644, 153.5047, -27.384, 153.117), 51.2, 0.3)
near("YBCG -> T63 fix", geo.haversine_nm(-28.1644, 153.5047, -27.972473, 153.473775), 11.6, 0.2)

_lat, _lon = geo.destination_point(-28.0, 153.4, 90.0, 10.0)
near("destination_point round-trips distance", geo.haversine_nm(-28.0, 153.4, _lat, _lon), 10.0, 0.01)
near("destination_point round-trips bearing", geo.bearing_deg(-28.0, 153.4, _lat, _lon), 90.0, 0.01)

# 349.4 deg sits just past the NNW sector edge (348.75), so N is correct here.
check("compass 349.4 -> N", geo.compass_point(349.4), "N")
check("compass 337.5 -> NNW", geo.compass_point(337.5), "NNW")
check("compass 0 -> N", geo.compass_point(0), "N")
check("compass 359.9 -> N", geo.compass_point(359.9), "N")
check("compass 45 -> NE", geo.compass_point(45), "NE")
check("compass 180 -> S", geo.compass_point(180), "S")

BOX = (-28.06, -27.94, 153.42, 153.52)
check("box contains the T63 fix", geo.point_in_bbox(-27.972473, 153.473775, BOX), True)
check("box excludes YBCG", geo.point_in_bbox(-28.1644, 153.5047, BOX), False)
for name, (la, lo) in {
    "SW corner": (-28.06, 153.42), "NE corner": (-27.94, 153.52),
    "NW corner": (-27.94, 153.42), "SE corner": (-28.06, 153.52),
}.items():
    check(f"box includes {name}", geo.point_in_bbox(la, lo, BOX), True)
check("box excludes a point just north", geo.point_in_bbox(-27.93, 153.47, BOX), False)
check("box excludes a point just east", geo.point_in_bbox(-28.0, 153.53, BOX), False)

_poly = geo.bbox_to_polygon(BOX)
check("polygon agrees inside", geo.point_in_polygon(-27.99, 153.47, _poly), True)
check("polygon agrees outside", geo.point_in_polygon(-28.30, 153.20, _poly), False)
check("degenerate polygon is never inside", geo.point_in_polygon(-28.0, 153.4, [(0, 0), (1, 1)]), False)

_inf = geo.inflate_bbox(BOX, 0.5)
check("inflated box still holds the original", geo.point_in_bbox(-27.99, 153.47, _inf), True)
check(
    "a point in the dead band is outside the box but inside the inflated one",
    (geo.point_in_bbox(-27.935, 153.47, BOX), geo.point_in_bbox(-27.935, 153.47, _inf)),
    (False, True),
)

check("closing_rate over a 2s span is rejected as jitter", geo.closing_rate_kt([(0, 10.0), (2, 9.9)]), None)
check("closing_rate needs two samples", geo.closing_rate_kt([(0, 10.0)]), None)
near("closing_rate 30->25.833 nm in 60s", geo.closing_rate_kt([(0, 30.0), (60, 25.833)]), 250.0, 1.0)
near("closing_rate is negative when opening", geo.closing_rate_kt([(0, 10.0), (60, 14.167)]), -250.0, 1.0)


# --- source parsing --------------------------------------------------------

section("Feed parsing")
check("alt_baro 'ground'", S.parse_alt("ground"), (None, True))
check("alt_baro int", S.parse_alt(1225), (1225, False))
check("alt_baro missing", S.parse_alt(None), (None, False))
check("alt_baro garbage", S.parse_alt("banana"), (None, False))

check("aircraft key (radius query)", len(S.extract_aircraft({"aircraft": [{}, {}]})), 2)
check("ac key (/v2/mil)", len(S.extract_aircraft({"ac": [{}]})), 1)
check("empty payload", S.extract_aircraft({}), [])
check("airplanes.live error body", S.extract_aircraft({"error": "Please contact us"}), [])
check("non-dict payload", S.extract_aircraft("nope"), [])

_t63 = S.normalise(RAW_T63, "adsb.fi", 1.0)
check("T63 normalises", (_t63.hex, _t63.ident, _t63.reg, _t63.typ), ("7c64cd", "T63", "", ""))
check("T63 altitude", (_t63.alt_ft, _t63.on_ground), (1225, False))
check("T63 has a position", _t63.has_position, True)
check("null dbFlags becomes 0", _t63.dbflags, 0)
check("absent emergency defaults to none", _t63.emergency, "none")

_ground = S.normalise(RAW_GROUND, "adsb.fi", 1.0)
check("on-ground record", (_ground.alt_ft, _ground.on_ground), (None, True))

check("record with no hex is dropped", S.normalise({"flight": "X"}, "t", 1.0), None)
check("non-dict record is dropped", S.normalise("junk", "t", 1.0), None)
check("empty record is dropped", S.normalise({}, "t", 1.0), None)

check("dbFlags 1 -> military", rep(dbflags=1).is_db_military, True)
check("dbFlags 3 -> military and interesting", (rep(dbflags=3).is_db_military, rep(dbflags=3).is_db_interesting), (True, True))
check("dbFlags 8 (LADD) is NOT military", rep(dbflags=8).is_db_military, False)
check("dbFlags 9 -> military", rep(dbflags=9).is_db_military, True)
check("dbFlags 0 -> not military", rep(dbflags=0).is_db_military, False)


# --- ident splitting -------------------------------------------------------

section("Callsign idents")
check("TROJ23 with padding", C.split_ident("TROJ23  "), ("TROJ", "23"))
check("BLKT10", C.split_ident("BLKT10"), ("BLKT", "10"))
check("ROLR6 single digit", C.split_ident("ROLR6"), ("ROLR", "6"))
check("EMBR722 three digits", C.split_ident("EMBR722"), ("EMBR", "722"))
check("bare rego suffix", C.split_ident("VDN"), ("VDN", ""))
check("airline ident", C.split_ident("QFA621"), ("QFA", "621"))
check("empty", C.split_ident(""), ("", ""))
check("hyphenated is not an ident", C.split_ident("VH-ABC"), ("", ""))
# T63 gives a one-character stem. The classifier must not try to look that up.
check("T63 yields a 1-char stem", C.split_ident("T63"), ("T", "63"))


# --- registry prefix index -------------------------------------------------

section("ADF callsign prefix lookup")
reg = M.get_registry()
check("BLKT -> BLACKCAT", reg.lookup_prefix("BLKT").callsign, "BLACKCAT")
check("TROJ -> TROJAN", reg.lookup_prefix("TROJ").callsign, "TROJAN")
check("TROJ resolves a Hercules", "C130" in reg.lookup_prefix("TROJ").aircraft.replace("-", ""), True)
check("ADDR -> ADDER", reg.lookup_prefix("ADDR").callsign, "ADDER")
check("RCH -> REACH", reg.lookup_prefix("RCH").callsign, "REACH")
check("RAPT -> RAPTOR", reg.lookup_prefix("RAPT").callsign, "RAPTOR")
check("lookup is case-insensitive", reg.lookup_prefix("blkt").callsign, "BLACKCAT")
check("misparsed prefix AND is blocked", reg.lookup_prefix("AND"), None)
check("misparsed prefix SQN is blocked", reg.lookup_prefix("SQN"), None)
check("misparsed prefix SEE is blocked", reg.lookup_prefix("SEE"), None)
check("2-char prefix is below the floor", reg.lookup_prefix("EG"), None)
check("unknown prefix", reg.lookup_prefix("ZZZZ"), None)
check("module wrapper works", M.lookup_ident_prefix("TROJ").callsign, "TROJAN")
check("module wrapper tolerates empty input", M.lookup_ident_prefix(""), None)

# The prefix index must not widen transcript matching. These four are the
# regression guards for that.
check("lookup() still does not resolve prefixes", reg.lookup("BLKT"), None)
check("match() does not fire on a bare prefix", reg.match("blkt one zero"), [])
check("match() still resolves the spoken name", [m.callsign for m in reg.match("Blackcat one zero")], ["BLACKCAT"])
check("matchable record count is unchanged", len(reg) > 400, True)


# --- classifier ------------------------------------------------------------

section("Classification")
near("noisy-OR of 0.55 and 0.45", C._combine([0.55, 0.45]), 0.7525, 0.0001)
check("noisy-OR of a single weight", C._combine([0.40]), 0.40)
check("noisy-OR of nothing", C._combine([]), 0.0)

_c = C.classify(S.normalise(RAW_T63, "t", 0), anon_zone=True)
check("T63 low in a watched zone is probable", (_c.military, _c.probable), (False, True))
near("T63 confidence", _c.confidence, 0.40, 0.001)
check("T63's only signal is the anonymous-in-zone rule", [s.code for s in _c.signals], [C.SIG_ANON_ZONE])
check("...which gives it the anon_fast role", _c.roles(), {"probable", "anon_fast"})

# The zone gate is load-bearing: the same aircraft elsewhere is uninteresting.
_c = C.classify(S.normalise(RAW_T63, "t", 0), anon_zone=False)
check("T63 outside a watched zone is nothing", _c.interesting, False)

_c = C.classify(S.normalise(RAW_SIC, "t", 0), anon_zone=False)
check("VH-SIC is military-tier", _c.military, True)
near("VH-SIC confidence", _c.confidence, 0.7525, 0.001)
check(
    "VH-SIC matches on type and operator",
    sorted(s.code for s in _c.signals),
    sorted([C.SIG_TYPE_WARBIRD, C.SIG_OPERATOR]),
)
check("VH-SIC's LADD flag contributes nothing", C.SIG_DBFLAGS in [s.code for s in _c.signals], False)
check("VH-SIC is a notable type", "notable" in _c.roles(), True)

_c = C.classify(S.normalise(RAW_JPV, "t", 0))
check("VH-JPV is probable", (_c.military, _c.probable), (False, True))

_c = C.classify(S.normalise(RAW_TROJ, "t", 0))
check("TROJ23 is military", _c.military, True)
check("TROJ23 is maximally confident", _c.confidence, 1.0)
check(
    "TROJ23 matches on all four signals",
    sorted(s.code for s in _c.signals),
    sorted([C.SIG_DBFLAGS, C.SIG_HEX_BLOCK, C.SIG_CALLSIGN, C.SIG_TYPE_MIL]),
)
check("TROJ23 is titled with its callsign", "TROJAN" in _c.title, True)

# Negative controls.
for name, raw in (("a Piper PA-28", RAW_PA28), ("a scenic helicopter", RAW_GROUND)):
    _c = C.classify(S.normalise(raw, "t", 0))
    check(f"{name} is not interesting", _c.interesting, False)
    check(f"{name} has no roles", _c.roles(), set())
_c = C.classify(S.normalise(RAW_GROUND, "t", 0), anon_zone=True)
check("a helicopter parked in a watched zone is still not interesting", _c.interesting, False)

# Emergency services: a role, not a confidence signal.
_c = C.classify(rep(hex="7c1111", ident="POL30", typ="EC35", reg="VH-PVX",
                    owner="QUEENSLAND POLICE SERVICE"))
check("a police helicopter is special", bool(_c.special), True)
check("...but not military", (_c.military, _c.probable), (False, False))
check("...and carries the special role", _c.roles(), {"special"})
check("a bare 'POL' rego suffix is not police",
      C.classify(rep(hex="7c1112", ident="POL", typ="C172")).special, "")

# The core finding, pinned so nobody "simplifies" the classifier away.
_dbflags_only = [
    raw for raw in (RAW_T63, RAW_SIC, RAW_JPV) if (raw.get("dbFlags") or 0) & 1
]
check("a dbFlags-only filter would have missed every airshow aircraft", _dbflags_only, [])
_multi = [
    raw for raw in (RAW_T63, RAW_SIC, RAW_JPV)
    if C.classify(S.normalise(raw, "t", 0), anon_zone=True).interesting
]
check("the multi-signal classifier catches all three", len(_multi), 3)

check("hex 7cf839 is in the ADF block", C.hex_block("7cf839").country, "AU")
check("hex 7cfaff is the top of the ADF block", C.hex_block("7cfaff").country, "AU")
check("hex 7cfb00 is past the ADF block", C.hex_block("7cfb00"), None)
check("hex 7c6ac9 (civil AU) is not military", C.hex_block("7c6ac9"), None)
check("hex ae1234 is US military", C.hex_block("ae1234").country, "US")
check("garbage hex", C.hex_block("zzzz"), None)

check("watchlist forces a hit", C.classify(rep(hex="7c6ac9"), watch_hex={"7C6AC9"}).military, True)

_saved_mode = config.ADSB_CIVIL_REPORTING
config.ADSB_CIVIL_REPORTING = "airline"
check("an airline movement is reportable", C.is_reportable_civil(rep(ident="QFA621", category="A3")), True)
check("a light GA movement is not", C.is_reportable_civil(rep(ident="VDN", category="A1")), False)
check("an emergency squawk always is", C.is_reportable_civil(rep(ident="VDN", squawk="7700")), True)
config.ADSB_CIVIL_REPORTING = "mil_only"
check("mil_only suppresses even airlines", C.is_reportable_civil(rep(ident="QFA621", category="A3")), False)
config.ADSB_CIVIL_REPORTING = "all"
check("all reports everything", C.is_reportable_civil(rep(ident="VDN")), True)
config.ADSB_CIVIL_REPORTING = _saved_mode


# --- presence state machine ------------------------------------------------

section("Presence state machine")

# Cold start. Without the seeding rule this is 46 "transponder on" alerts at
# the moment the process launches.
p = make_poller()
crowd = [mil_rep(hex_=f"7cf8{i:02x}", ident=f"TROJ{i}") for i in range(46)]
evs = drive(p, [crowd, crowd, crowd])
check("cold start with 46 aircraft emits nothing", kinds(evs), [])
check("cold-start tracks go straight to live", p.tracks["7cf800"].state, T.ST_LIVE)

# A single frame is not a track.
p = make_poller()
evs = drive(p, [[], [], [mil_rep()], []])
check("one frame then gone emits nothing", kinds(evs), [])

# A genuine new arrival, after seeding.
p = make_poller()
evs = drive(p, [[], [], [mil_rep()], [mil_rep()], [mil_rep()]])
check("a confirmed new aircraft reports once", kinds(evs), [T.EV_APPEARED])

# Fading is silent; only a full loss reports.
p = make_poller()
frames = [[], []] + [[mil_rep()]] * 8 + [[]] * 5    # 50s of silence
evs = drive(p, frames)
check("50s of silence does not report a loss", [k for k in kinds(evs) if k == T.EV_DISAPPEARED], [])
check("the track is fading, not lost", p.tracks["7cf839"].state, T.ST_FADING)

p = make_poller()
frames = [[], []] + [[mil_rep()]] * 8 + [[]] * 14   # 140s of silence
evs = drive(p, frames)
check("140s of silence reports exactly one loss", [k for k in kinds(evs) if k == T.EV_DISAPPEARED], [T.EV_DISAPPEARED])

# Too few frames to have been tracked reliably in the first place.
p = make_poller()
frames = [[], []] + [[mil_rep()]] * 3 + [[]] * 14
evs = drive(p, frames)
check("a barely-seen target's loss is not reported", [k for k in kinds(evs) if k == T.EV_DISAPPEARED], [])

# THE flapping test. This is the whole reason for the FADING state: a display
# aircraft at the edge of coverage alternating present/absent every poll must
# produce absolutely nothing.
p = make_poller()
frames = [[], []]
for i in range(30):                                  # 5 minutes at 10s polls
    frames.append([mil_rep()] if i % 2 == 0 else [])
evs = drive(p, frames)
check("an aircraft flapping every poll for 5 minutes emits nothing", kinds(evs), [])
# It must still be *visible* though — silence is about alerts, not about
# pretending the aircraft is not there.
check("...but it is still tracked", "7cf839" in p.tracks, True)
check("...and still shows on the board", len(p.snapshot(airborne_only=False)), 1)

# Once it settles into consecutive polls it may report, exactly once.
evs = drive(p, [[mil_rep()]] * 4, start=1_001_000.0)
check("a flapping aircraft that settles reports one appearance", kinds(evs).count(T.EV_APPEARED), 1)
check("...and an unconfirmed track never posts a position", kinds(evs)[0], T.EV_APPEARED)

# A target inside a zone with lost_sec set is called lost sooner.
p = make_poller()
box_ac = lambda: mil_rep(lat=BOX_LAT, lon=BOX_LON, alt_ft=1200, gs_kt=200.0)
frames = [[], []] + [[box_ac()]] * 8 + [[]] * 8      # 80s of silence
evs = drive(p, frames)
check("a zone target reports loss after 80s", T.EV_DISAPPEARED in kinds(evs), True)

# Loss above 5000 ft is stated as a transponder shutdown; below, as a gap.
p = make_poller()
evs = drive(p, [[], []] + [[mil_rep(alt_ft=20000)]] * 8 + [[]] * 14)
lost = [e for e in evs if e.kind == T.EV_DISAPPEARED][0]
check("a high loss is called a transponder shutdown", "Transponder off" in lost.headline, True)
p = make_poller()
evs = drive(p, [[], []] + [[mil_rep(alt_ft=900)]] * 8 + [[]] * 14)
lost = [e for e in evs if e.kind == T.EV_DISAPPEARED][0]
check("a low loss is called a signal loss", "Signal lost" in lost.headline, True)


# --- airspace zones --------------------------------------------------------

section("Airspace zone occupancy")

p = make_poller()
inside = lambda: mil_rep(lat=BOX_LAT, lon=BOX_LON, alt_ft=1200, gs_kt=200.0)
outside = lambda: mil_rep(lat=OUT_LAT, lon=OUT_LON, alt_ft=1200, gs_kt=200.0)
evs = drive(p, [[outside()], [outside()]] + [[inside()]] * 6)
check("entering a zone reports once", [k for k in kinds(evs) if k == T.EV_ZONE_ENTER], [T.EV_ZONE_ENTER])
enter = [e for e in evs if e.kind == T.EV_ZONE_ENTER][0]
check("...naming the zone", (enter.zone_id, enter.zone_name), ("testbox", "Test box"))
check("...with a per-zone dedupe key", enter.alert_key, "zone_enter:testbox")
check("...and the roles that made it worth saying", "military" in enter.roles, True)
check("the track knows it is inside", p.tracks["7cf839"].zones, {"testbox"})

evs = drive(p, [[outside()]] * 6, start=1_000_100.0)
check("leaving a zone reports once", [k for k in kinds(evs) if k == T.EV_ZONE_EXIT], [T.EV_ZONE_EXIT])

# Hysteresis: a track sitting on the northern boundary must not chatter. The
# real edge is -27.94; these two points straddle it by ~0.2 NM either way,
# which is inside the 0.5 NM dead band.
p = make_poller()
edge_in = lambda: mil_rep(lat=-27.9435, lon=153.47, alt_ft=1200, gs_kt=200.0)
edge_out = lambda: mil_rep(lat=-27.9365, lon=153.47, alt_ft=1200, gs_kt=200.0)
frames = [[], []] + [[edge_in()]] * 4
for i in range(30):
    frames.append([edge_out() if i % 2 else edge_in()])
evs = drive(p, frames)
check("a track oscillating across the boundary enters once", kinds(evs).count(T.EV_ZONE_ENTER), 1)
check("...and never reports an exit", kinds(evs).count(T.EV_ZONE_EXIT), 0)
evs = drive(p, [[outside()]] * 6, start=1_000_400.0)
check("...but a clean departure does report an exit", kinds(evs).count(T.EV_ZONE_EXIT), 1)

# Ceiling: a zone is a volume. An airliner routed over the strip at cruise is
# not in it, and gating occupancy on the ceiling is what stops every one of
# them firing an entry.
check("zone holds a contact at the ceiling", TEST_ZONE.contains(BOX_LAT, BOX_LON, 10000, strict=True), True)
check("zone rejects a contact above the ceiling", TEST_ZONE.contains(BOX_LAT, BOX_LON, 10001, strict=True), False)
check("zone keeps a contact whose altitude is unknown", TEST_ZONE.contains(BOX_LAT, BOX_LON, None, strict=True), True)

over_high = lambda: mil_rep(lat=BOX_LAT, lon=BOX_LON, alt_ft=25000, gs_kt=450.0)
p = make_poller()
evs = drive(p, [[outside()], [outside()]] + [[over_high()]] * 8)
check("an aircraft overflying above the ceiling never enters", kinds(evs).count(T.EV_ZONE_ENTER), 0)

# Same ground track, but descending through the ceiling into the zone.
p = make_poller()
evs = drive(p, [[outside()], [outside()]] + [[over_high()]] * 4 + [[inside()]] * 6)
check("a contact that descends below the ceiling then enters", kinds(evs).count(T.EV_ZONE_ENTER), 1)

# An aircraft that climbs out through the ceiling leaves the zone.
p = make_poller()
evs = drive(p, [[outside()], [outside()]] + [[inside()]] * 6 + [[over_high()]] * 6)
check("a zone contact that climbs through the ceiling leaves", kinds(evs).count(T.EV_ZONE_EXIT), 1)

# Roles gate the alert, not the occupancy: an ordinary Piper in the zone is
# tracked as inside but nobody is told.
civil = lambda: rep(hex="7c6ac9", ident="VDN", reg="VH-VDN", typ="P28A",
                    lat=BOX_LAT, lon=BOX_LON, alt_ft=1500, gs_kt=100.0)
p = make_poller()
evs = drive(p, [[], []] + [[civil()]] * 6)
check("a civil aircraft entering a zone raises nothing", [k for k in kinds(evs) if k in T.ZONE_EVENTS], [])
check("...but it is still counted as inside", p.tracks["7c6ac9"].zones, {"testbox"})
check("...and the zone summary counts it", p.zone_summary()[0]["count"], 1)
check("...without listing it as flagged", p.zone_summary()[0]["occupants"], [])

# A zone that only cares about the watchlist ignores military traffic.
watch_only = A.zone_from_dict({
    "id": "w", "name": "Watch only", "polygon": TEST_ZONE.polygon,
    "triggers": ["watch"], "dwell_sec": 20,
})
p = make_poller(zones=A.ZoneSet(zones=[watch_only]))
evs = drive(p, [[], []] + [[inside()]] * 6)
check("a zone's triggers decide who alerts", kinds(evs).count(T.EV_ZONE_ENTER), 0)
p.add_watch("7cf839")
evs = drive(p, [[outside()]] * 6 + [[inside()]] * 6, start=1_000_500.0)
check("...and a watched aircraft does", kinds(evs).count(T.EV_ZONE_ENTER), 1)

# Cold start inside a zone: adopted silently, or every restart re-announces
# everything sitting in the CTR.
p = make_poller()
evs = drive(p, [[inside()]] * 6)
check("a cold start inside a zone announces nothing", [k for k in kinds(evs) if k in T.ZONE_EVENTS], [])
check("...but knows it is inside", p.tracks["7cf839"].zones, {"testbox"})

# Turning a zone off clears it from every track.
check("toggling an unknown zone fails", p.set_zone_enabled("nope", False), False)
check("toggling a zone off succeeds", p.set_zone_enabled("testbox", False), True)
check("...and empties it", p.tracks["7cf839"].zones, set())
evs = drive(p, [[inside()]] * 6, start=1_000_600.0)
check("...and a disabled zone raises nothing", [k for k in kinds(evs) if k in T.ZONE_EVENTS], [])
p.set_zone_enabled("testbox", True)


# --- predicted entry -------------------------------------------------------

section("Predicted zone entry")

# Heading due north at 240 kt (4 nm/min) from 12 nm south of the zone's
# southern edge: enters in ~3 minutes.
def inbound(lat, **kw):
    return mil_rep(lat=lat, lon=153.47, alt_ft=3000, gs_kt=240.0, track_deg=0.0, **kw)

start_lat = -28.06 - 12 / 60.0
p = make_poller()
frames = [[], []] + [[inbound(start_lat + i * (40 / 3600.0))] for i in range(4)]
evs = drive(p, frames)
pred = [e for e in evs if e.kind == T.EV_ZONE_PREDICT]
check("an inbound military track is predicted once", len(pred), 1)
near("...with an ETA of about three minutes", pred[0].eta_sec / 60, 2.8, 0.6)
check("...and it is shown on the track", "testbox" in p.tracks["7cf839"].predicted, True)

# Turning away cancels it quietly.
evs = drive(p, [[mil_rep(lat=start_lat, lon=153.47, alt_ft=3000, gs_kt=240.0, track_deg=180.0)]] * 2,
            start=1_000_100.0)
check("a turn away emits nothing", [k for k in kinds(evs) if k in T.ZONE_EVENTS], [])
check("...and clears the prediction", p.tracks["7cf839"].predicted, {})

# A civil aircraft on the same line is not predicted.
p = make_poller()
frames = [[], []] + [[rep(hex="7c6ac9", ident="VDN", typ="P28A", lat=start_lat, lon=153.47,
                          alt_ft=3000, gs_kt=240.0, track_deg=0.0)]] * 4
check("civil traffic is never predicted", [e.kind for e in drive(p, frames)
                                           if e.kind == T.EV_ZONE_PREDICT], [])

# Disabled prediction stays silent.
p = make_poller()
p.predict_enabled = False
frames = [[], []] + [[inbound(start_lat)]] * 4
check("prediction can be switched off", [e for e in drive(p, frames) if e.kind == T.EV_ZONE_PREDICT], [])


# --- zone alert gates ------------------------------------------------------

section("Zone alert gates")
import datetime as _dtg

_three_am = _dtg.datetime(2026, 8, 14, 3, 0, tzinfo=T._TZ).timestamp()
_saved_quiet = config.ADSB_QUIET_HOURS
config.ADSB_QUIET_HOURS = "00:00-23:59"
p = make_poller()
_mil_ev = T.TrackEvent(kind=T.EV_ZONE_ENTER, track=T.Track(hex="aaaaaa"),
                       at=1000.0, zone_id="testbox", roles=("military",))
_watch_ev = T.TrackEvent(kind=T.EV_ZONE_ENTER, track=T.Track(hex="bbbbbb"),
                         at=1000.0, zone_id="testbox", roles=("watch",))
check("quiet hours hold back a military zone alert", p._passes_gates(_mil_ev, _three_am), False)
check("...but not a watchlist one", p._passes_gates(_watch_ev, _three_am), True)
config.ADSB_QUIET_HOURS = _saved_quiet


# --- emergencies -----------------------------------------------------------

section("Emergencies")
p = make_poller()
mayday = lambda: mil_rep(squawk="7700", lat=-28.0, lon=153.4, alt_ft=4000)
evs = drive(p, [[], []] + [[mayday()]] * 4)
emg = [e for e in evs if e.kind == T.EV_EMERGENCY]
check("squawk 7700 reports once", len(emg), 1)
check("...at emergency tier", emg[0].tier, T.TIER_EMERGENCY)
check("...and says what the code means", "general emergency" in emg[0].detail, True)

# One clean poll must not re-arm — a single garbled decode would then chatter.
evs = drive(p, [[mil_rep(squawk="1200")], [mayday()]], start=1_000_100.0)
check("one clean poll does not re-arm the emergency", [e for e in evs if e.kind == T.EV_EMERGENCY], [])
evs = drive(p, [[mil_rep(squawk="1200")]] * 3 + [[mayday()]], start=1_000_200.0)
check("three clean polls do re-arm it", len([e for e in evs if e.kind == T.EV_EMERGENCY]), 1)

check("7500 is recognised", T.EMERGENCY_SQUAWKS >= {"7500", "7600", "7700"}, True)


# --- quiet hours -----------------------------------------------------------

section("Quiet hours")
import datetime as _dt


def _at(hour, minute=0):
    local = _dt.datetime(2026, 8, 14, hour, minute, tzinfo=T._TZ)
    return local.timestamp()


check("23:30 is inside 22:00-06:00", T.in_quiet_hours(_at(23, 30), "22:00-06:00"), True)
check("02:00 is inside (past midnight)", T.in_quiet_hours(_at(2), "22:00-06:00"), True)
check("07:00 is outside", T.in_quiet_hours(_at(7), "22:00-06:00"), False)
check("22:00 is the inclusive start", T.in_quiet_hours(_at(22), "22:00-06:00"), True)
check("06:00 is the exclusive end", T.in_quiet_hours(_at(6), "22:00-06:00"), False)
check("a same-day window works", T.in_quiet_hours(_at(13), "12:00-14:00"), True)
check("an empty spec is never quiet", T.in_quiet_hours(_at(3), ""), False)
check("a malformed spec is never quiet", T.in_quiet_hours(_at(3), "nonsense"), False)


# --- token bucket ----------------------------------------------------------

section("Alert rate limiting")
bucket = T._TokenBucket(12)
taken = sum(1 for _ in range(40) if bucket.take(1000.0))
check("a burst of 40 is capped at the bucket size", taken, 12)
check("...and the rest are counted as suppressed", bucket.suppressed, 28)
check("tokens refill over time", bucket.take(1060.0), True)


# --- persistence -----------------------------------------------------------

section("Persistence and cross-restart dedupe")
import adsb_store as ST

with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "adsb_state.db"
    store = ST.AdsbStore(db)

    check("a fresh store knows nothing", store.last_seen("7cf839"), None)
    check("a fresh store permits an alert", store.should_alert("7cf839", "appeared", 600, 1000.0), True)

    store.mark_alert("7cf839", "appeared", 1000.0)
    check("the same alert is suppressed inside the cooldown", store.should_alert("7cf839", "appeared", 600, 1300.0), False)
    check("...and permitted once it expires", store.should_alert("7cf839", "appeared", 600, 1700.0), True)
    check("a different kind is independent", store.should_alert("7cf839", "disappeared", 600, 1300.0), True)

    # The guarantee that matters: reopening the database must not forget.
    reopened = ST.AdsbStore(db)
    check("cooldown survives a restart", reopened.should_alert("7cf839", "appeared", 600, 1300.0), False)

    T0 = 1_000_000.0
    p = T.AdsbPoller(store=store, zones=test_zones())
    p.source = None
    drive(p, [[], []] + [[mil_rep()]] * 4, start=T0)
    last_track_time = p.tracks["7cf839"].last_seen
    check("sightings are written", store.record_sightings(p.tracks.values(), last_track_time), 1)
    check("...and read back", ST.AdsbStore(db).last_seen("7cf839"), last_track_time)
    check("upsert does not duplicate", store.record_sightings(p.tracks.values(), last_track_time), 1)
    check("still one row after the upsert", ST.AdsbStore(db).stats()["sightings"], 1)

    # A restart mid-event must not re-announce aircraft it had already seen.
    p2 = T.AdsbPoller(store=ST.AdsbStore(db), zones=test_zones())
    p2.source = None
    evs = drive(p2, [[mil_rep()]] * 6, start=last_track_time + 60)
    check("a restart does not re-announce a known aircraft", kinds(evs).count(T.EV_APPEARED), 0)

    # ...but a genuinely long absence still counts as a new appearance.
    p3 = T.AdsbPoller(store=ST.AdsbStore(db), zones=test_zones())
    p3.source = None
    later = last_track_time + config.ADSB_APPEAR_GAP_MIN * 60 + 600
    evs = drive(p3, [[], []] + [[mil_rep()]] * 4, start=later)
    check("a gap beyond APPEAR_GAP_MIN does report an appearance", kinds(evs).count(T.EV_APPEARED), 1)

    # A stored timestamp in the future (clock change, or a database copied from
    # another machine) must not suppress appearances until real time catches up.
    check("no stored time -> not seen recently", T._seen_recently(None, 1000.0), False)
    check("a recent time -> seen recently", T._seen_recently(900.0, 1000.0), True)
    check("an old time -> not seen recently", T._seen_recently(1000.0 - 3600, 1000.0), False)
    check("a FUTURE time -> not seen recently", T._seen_recently(9_999_999.0, 1000.0), False)

    store.log_event(
        T.TrackEvent(kind="appeared", track=p.tracks["7cf839"], at=1000.0, detail="x")
    )
    check("events are logged", len(store.recent_events(10)), 1)
    check("events can be filtered by aircraft", len(store.recent_events(10, "7cf839")), 1)
    check("...and by an aircraft with none", len(store.recent_events(10, "aaaaaa")), 0)
    check("history round-trips", store.history("7cf839")["ident"], "TROJ23")
    check("history of an unknown aircraft", store.history("aaaaaa"), None)
    check("stats report the db path", Path(store.stats()["db"]).name, "adsb_state.db")

# A store pointed at an unwritable path must degrade, not raise.
broken = ST.AdsbStore("/proc/nonexistent/adsb.db")
check("an unusable database does not raise on read", broken.last_seen("7cf839"), None)
check("...or on write", broken.mark_alert("7cf839", "appeared", 1.0), None)
check("...and still permits alerts", broken.should_alert("7cf839", "appeared", 600, 1000.0), True)


# --- map marker shapes -----------------------------------------------------

section("Aircraft silhouettes (web map)")
_shapes_path = Path(config.ADSB_MARKER_SHAPES_FILE)
if not _shapes_path.exists():
    print("  skip  (data/adsb_marker_shapes.json not present)")
else:
    import json as _json
    _m = _json.loads(_shapes_path.read_text())
    _sh, _td, _cat = _m["shapes"], _m["typeDesignators"], _m["categories"]

    check("shape table is populated", len(_sh) > 80, True)
    check("type designator table is populated", len(_td) > 400, True)
    check("the GPL notice survived", "GPL-2.0" in _m["_license"], True)

    # Every mapping must name a shape that exists. This is the guard for
    # hand-editing the file — a typo here would silently draw nothing.
    _bad_types = sorted(t for t, e in _td.items() if e[0] not in _sh)
    check("every type designator maps to a real shape", _bad_types, [])
    _bad_cats = sorted(c for c, e in _cat.items() if e[0] not in _sh)
    check("every category maps to a real shape", _bad_cats, [])
    check("the default shape exists", _m["default"][0] in _sh, True)

    # Shapes must carry what the renderer needs.
    _incomplete = sorted(
        n for n, s in _sh.items()
        if not s.get("svg") and not (s.get("path") and s.get("viewBox") and s.get("w"))
    )
    check("every shape has a path, viewBox and size", _incomplete, [])

    # The types this tracker exists to show.
    for _t, _want in (
        ("C17", "c17"), ("C130", "c130"), ("E737", "e737"), ("P8", "p8"),
        ("F35", "f35"), ("HAWK", "bae_hawk"), ("B738", "b738"),
    ):
        check(f"{_t} draws as {_want}", _td.get(_t, [None])[0], _want)

    # Airshow warbirds — these are this project's own additions.
    for _t in ("P40", "SPIT", "JPRO", "HUNT", "PC21", "T6"):
        check(f"{_t} has a silhouette", _td.get(_t, [None])[0] in _sh, True)

    # Every classifier type should resolve to something, so nothing the
    # tracker calls interesting draws as a generic blob.
    _types = _json.loads(Path(config.ADSB_TYPES_FILE).read_text())
    _unmapped = sorted(
        t for t in (_types["military"] + _types["warbird"])
        if t not in _td
    )
    check("every military/warbird type has a silhouette", _unmapped, [])


# --- formation grouping ----------------------------------------------------

section("Formation grouping")


def frow(hex_, typ="PC21", lat=-28.0, lon=153.4, alt=1500, ident="", **kw):
    """One board row, shaped like Track.as_dict, for the grouper."""
    return {
        "hex": hex_, "ident": ident, "reg": kw.get("reg", ""), "type": typ,
        "desc": kw.get("desc", ""), "lat": lat, "lon": lon, "alt_ft": alt,
        "gs_kt": kw.get("gs_kt", 200.0), "track_deg": kw.get("track_deg", 90.0),
        "on_ground": False, "in_zone": kw.get("in_zone", False),
        "military": kw.get("military", True), "probable": kw.get("probable", False),
        "title": kw.get("title", ""), "reasons": kw.get("reasons", ""),
        "squawk": kw.get("squawk", ""), "first_seen": kw.get("first_seen", 0.0),
        "url": "",
    }


# Six Roulettes PC-21s in a tight display formation collapse to one contact.
roul = [
    frow(f"7cf9f{i}", typ="PC21", lat=-28.00 - i * 0.004, lon=153.40 + i * 0.004,
         alt=1500, ident=f"RLTS{i + 1}")
    for i in range(6)
]
g = T.group_formations(roul, radius_nm=12, alt_band_ft=5000, min_size=2)
check("six PC-21s flying together make one group", len(g), 1)
check("...of size six", g[0]["size"], 6)
check("...flagged as a formation", g[0]["formation"], True)
check("...labelled by the shared callsign stem", g[0]["label"], "RLTS")
check("...carrying every aircraft", len(g[0]["members"]), 6)

# Two KC-30As in company — the exact case that spammed sixteen messages.
kc = [
    frow("7cf865", typ="A332", lat=-28.10, lon=153.20, alt=9000, ident="THUMPER", reg="A39-005"),
    frow("7cf868", typ="A332", lat=-28.12, lon=153.22, alt=9200, ident="A39-002", reg="A39-002"),
]
g = T.group_formations(kc, radius_nm=12, alt_band_ft=5000, min_size=2)
check("two KC-30As in company make one group", len(g), 1)
check("...of size two", g[0]["size"], 2)
check("...falls back to the type when callsigns differ", g[0]["label"], "A332")

# Same type, far apart — two separate transits, not a formation.
far = [
    frow("aaa001", typ="A332", lat=-28.10, lon=153.20, alt=9000),
    frow("aaa002", typ="A332", lat=-28.60, lon=152.40, alt=9000),
]
g = T.group_formations(far, radius_nm=12, alt_band_ft=5000, min_size=2)
check("same type far apart stays separate", len(g), 2)
check("...and neither is a formation", any(x["formation"] for x in g), False)

# Same type, close horizontally but in different altitude bands.
split = [
    frow("bbb001", typ="C130", lat=-28.000, lon=153.30, alt=1000),
    frow("bbb002", typ="C130", lat=-28.005, lon=153.305, alt=15000),
]
g = T.group_formations(split, radius_nm=12, alt_band_ft=5000, min_size=2)
check("a high transit and a low display of one type stay apart", len(g), 2)

# Different types sitting together never merge.
mixed = [
    frow("ccc001", typ="PC21", lat=-28.000, lon=153.30, alt=1500, ident="RLTS1"),
    frow("ccc002", typ="F35", lat=-28.001, lon=153.301, alt=1500, ident="ADDR1"),
]
g = T.group_formations(mixed, radius_nm=12, alt_band_ft=5000, min_size=2)
check("different types nearby do not merge", len(g), 2)

# radius 0 disables merging entirely — the ADSB_FORMATION_ENABLED=off path.
g = T.group_formations(roul, radius_nm=0, alt_band_ft=5000, min_size=2)
check("radius 0 disables merging", len(g), 6)
check("...leaving all singletons", all(x["size"] == 1 for x in g), True)

# A higher threshold keeps a pair as two individual contacts.
g = T.group_formations(kc, radius_nm=12, alt_band_ft=5000, min_size=3)
check("min_size 3 keeps a pair as two singletons", len(g), 2)
check("...neither flagged a formation", any(x["formation"] for x in g), False)

# A contact with no position is always its own singleton.
g = T.group_formations(
    [frow("ddd001", typ="PC21", lat=None, lon=None, alt=None, ident="RLTS1")],
    radius_nm=12, alt_band_ft=5000, min_size=2,
)
check("a positionless contact is its own singleton", (len(g), g[0]["size"]), (1, 1))

# Nearest group sorts first.
g = T.group_formations(kc + far, radius_nm=12, alt_band_ft=5000, min_size=2)
check("groups sort nearest-first", g[0]["dist_nm"] <= g[-1]["dist_nm"], True)


# --- summary ---------------------------------------------------------------

print()
if failures:
    print(f"❌ {len(failures)} check(s) failed:")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print("✅ all ADS-B checks passed")
