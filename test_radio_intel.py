"""Offline checks for radio callsign extraction and radio<->ADS-B correlation.

Same hand-rolled shape as the other test_*.py files: no pytest, no network.

    python3 test_radio_intel.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import adsb_source as S
import adsb_store as ST
import adsb_tracker as T
import airspace as A
import config
import radio_intel as R

failures: list = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def section(name: str) -> None:
    print(f"\n{name}")


def found(text: str) -> list:
    return [(c.kind, c.canonical) for c in R.extract(text)]


section("Airline telephony")
check("Qantas spoken digits", found("Gold Coast Tower, Qantas four one two, ready runway one four"),
      [("airline", "QFA412")])
check("Jetstar written digits", found("Jetstar 472 contact ground"), [("airline", "JST472")])
check("Virgin, number ends at the comma", found("Virgin eight one two, five miles final"),
      [("airline", "VOZ812")])
check("a unit word claims its own number", found("Virgin eight one two five miles final"),
      [("airline", "VOZ812")])
check("niner and tree", found("Velocity niner tree one"), [("airline", "VOZ931")])
check("multi-word telephony", found("New Zealand one four five heavy"), [("airline", "ANZ145")])
check("grouped tens", found("Qantas six thirty two, Brisbane Tower"), [("airline", "QFA632")])
check("grouped tens and units", found("Jetstar five fifty nine, contact departures"), [("airline", "JST559")])
check("a round ten", found("Virgin nine twenty, ready"), [("airline", "VOZ920")])
check("an airline name without a number is not a callsign", found("the Qantas lounge is shut"), [])
check("REX the word needs a number", found("rex is on the apron"), [])

section("Registrations")
check("full Victor Hotel", found("Victor Hotel Alpha Bravo Charlie, taxi via Alpha Bravo Charlie"),
      [("registration", "VH-ABC")])
check("abbreviated three letters", found("Alpha Kilo Delta, cleared for takeoff"),
      [("registration", "VH-AKD")])
check("written VH-", found("VH-SIC overhead Surfers"), [("registration", "VH-SIC")])
check("a taxiway route is not a registration", found("taxi via Alpha Bravo Charlie holding point"), [])
check("an ATIS letter is not a registration", found("information Kilo Lima Mike"), [])
check("two letters is not a registration", found("Bravo Charlie report final"), [])
check("four letters is not a registration", found("Alpha Bravo Charlie Delta report final"), [])

section("Military and emergency services")
check("military callsign with flight number", found("Wolf two one, descend five thousand"),
      [("military", "WOLF21")])
check("emergency services", found("Rescue five zero zero, Gold Coast Tower"), [("special", "RESCUE500")])
check("'five hundred' as a callsign", found("Rescue five hundred, identified"), [("special", "RESCUE500")])
check("...but not 'five hundred feet'", found("Rescue five hundred feet"), [])
check("'the rescue helicopter' is not a callsign", found("the rescue helicopter is on the ground"), [])
check("Polair is not also a weak PELAIR", found("Polair 30 orbiting Surfers"), [("special", "POLAIR30")])
check("an English lookalike is not a callsign", [k for k, _ in found("starting engines now")
                                                   if k == "military"], [])
check("military sorts first", [k for k, _ in found("Qantas four one two, traffic is Wolf two one")],
      ["military", "airline"])
check("empty text", R.extract(""), [])


# --- correlation -----------------------------------------------------------

section("Correlation")


def track(hex_, ident="", reg="", lat=-28.0, lon=153.45, **kw):
    t = T.Track(hex=hex_, ident=ident, reg=reg, lat=lat, lon=lon)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def cs(text):
    return R.extract(text)[0]


qfa = track("7c0001", ident="QFA412", reg="VH-VZA")
jst = track("7c0002", ident="JST472")
ga = track("7c0003", ident="AKD", reg="VH-AKD")
wolf = track("ae0001", ident="WOLF21")
pool = [qfa, jst, ga, wolf]
check("airline ident", R.correlate(cs("Qantas four one two"), pool)[0], qfa)
check("airline with a leading zero in the ident",
      R.correlate(cs("Jetstar seven two"), [track("x", ident="JST072")])[0].hex, "x")
check("GA rego via the ident suffix", R.correlate(cs("Alpha Kilo Delta"), pool)[0], ga)
check("GA rego via the registration", R.correlate(cs("VH-VZA"), pool)[0], qfa)
check("military prefix plus number", R.correlate(cs("Wolf two one"), pool)[0], wolf)
check("no match", R.correlate(cs("Qantas nine nine nine"), pool), (None, 0.0))
check("a tie is no match at all",
      R.correlate(cs("Wolf two one"), [wolf, track("ae0002", ident="WOLF21")]), (None, 0.0))

# analyse() only looks near the station that heard the call.
p = T.AdsbPoller(store=False, zones=A.ZoneSet(zones=[]))
p.source = None
near_ybcg = S.AircraftReport(hex="7c0001", ident="QFA412", lat=-28.10, lon=153.50,
                             alt_ft=3000, source="t", received_at=0.0)
far_away = S.AircraftReport(hex="7c0009", ident="QFA999", lat=-33.9, lon=151.2,
                            alt_ft=30000, source="t", received_at=0.0)
p.poll_once([near_ybcg, far_away], 1000.0)
m = R.analyse("Gold Coast Tower, Qantas four one two", "YBCG_TWR", p)
check("analyse matches a nearby aircraft", m[0].track.hex if m[0].track else None, "7c0001")
m = R.analyse("Gold Coast Tower, Qantas nine nine nine", "YBCG_TWR", p)
check("...but not one 400 nm away", m[0].track, None)
m = R.analyse("Qantas four one two", "NOPE", p)
check("an unknown station correlates nothing", m[0].track, None)
check("analyse without a poller still extracts", [x.callsign.canonical for x in R.analyse(
    "Qantas four one two", "YBCG_TWR", None)], ["QFA412"])

section("Note heard")
heard = {"at": 1000.0, "icao": "YBCG_TWR", "text": "Qantas four one two"}
check("note_heard attaches to a live track", p.note_heard("7c0001", heard).last_heard["icao"], "YBCG_TWR")
check("note_heard on an unknown hex", p.note_heard("ffffff", heard), None)


# --- flagging --------------------------------------------------------------

section("What gets flagged")
plain = R.Mention(callsign=cs("Qantas four one two"), track=qfa, roles=set())
check("a matched airliner is not flagged", R.should_flag(plain, 0), False)
check("...unless the transmission was alert-tier", R.should_flag(plain, 1), True)
flagged_role = R.Mention(callsign=cs("Alpha Kilo Delta"), track=ga, roles={"watch"})
check("a callsign on a watched aircraft is flagged", R.should_flag(flagged_role, 0), True)
special = R.Mention(callsign=cs("Rescue five zero zero"), track=track("r", ident="RSCU500"), roles={"special"})
check("an emergency-services aircraft is flagged", R.should_flag(special, 0), True)
unmatched = R.Mention(callsign=cs("Alpha Kilo Delta"))
check("an unmatched registration is not flagged", R.should_flag(unmatched, 0), False)
check("a strong military callsign always is", R.should_flag(R.Mention(callsign=cs("Wolf two one")), 0), True)

cd = R.FlagCooldown(300)
check("first flag passes the cooldown", cd.ready(("QFA412", "YBCG"), 1000.0), True)
check("a repeat inside it does not", cd.ready(("QFA412", "YBCG"), 1100.0), False)
check("another station is independent", cd.ready(("QFA412", "YBBN"), 1100.0), True)
check("it re-arms after the cooldown", cd.ready(("QFA412", "YBCG"), 1400.0), True)


# --- persistence and digest ------------------------------------------------

section("Radio log and digest")
with tempfile.TemporaryDirectory() as tmp:
    store = ST.AdsbStore(Path(tmp) / "s.db")
    t0 = 1_700_000_000.0
    for i in range(3):
        store.log_mention({"at": t0 + i, "icao": "YBCG_TWR", "callsign": "QFA412",
                           "kind": "airline", "hex": "7c0001", "ident": "QFA412",
                           "flagged": i == 0, "text": "Qantas four one two"})
    store.log_mention({"at": t0 + 5, "icao": "YBCG", "callsign": "WOLF21", "kind": "military",
                       "flagged": True, "text": "Wolf two one"})
    for i in range(4):
        store.log_transmission(t0 + i * 60, "YBCG_TWR", 2 if i == 0 else 0)
    check("mentions read back newest first", store.recent_mentions(10)[0]["callsign"], "WOLF21")
    check("prefix search", len(store.recent_mentions(10, "QFA")), 3)
    check("search ignores hyphens", store.recent_mentions(10, "wolf-21")[0]["callsign"], "WOLF21")
    d = store.digest(t0 - 1, t0 + 3600)
    check("digest ranks callsigns", [c[0] for c in d["callsigns"]], ["QFA412", "WOLF21"])
    check("digest counts flagged mentions", d["flagged_mentions"], 2)
    check("digest counts transmissions per station", d["stations"]["YBCG_TWR"]["total"], 4)
    check("digest finds the busiest hour", d["stations"]["YBCG_TWR"]["busiest_hour"] is not None, True)
    check("digest counts radio emergencies", d["radio_emergencies"], 1)
    check("digest of an empty window", store.digest(0, 1)["callsigns"], [])


# --- historical corpus -----------------------------------------------------

section("Historical transcripts (logs/)")
logs = sorted(Path(__file__).parent.glob("logs/atc_*.log"))
if not logs:
    print("  skip  (no logs/ present)")
else:
    lines = []
    for path in logs[-7:]:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split(" | ")
            if len(parts) >= 4:
                lines.append(parts[3])
    hits = [c for text in lines for c in R.extract(text)]
    rate = len(hits) / max(1, len(lines))
    print(f"  info  {len(lines)} transmissions, {len(hits)} callsigns ({rate:.0%} of lines)")
    check("extraction finds callsigns in real traffic", len(lines) < 50 or len(hits) > 0, True)
    regos = [c for c in hits if c.kind == R.KIND_REGISTRATION and c.confidence < 1.0]
    check("abbreviated registrations stay a minority of lines", len(regos) <= max(5, len(lines) * 0.5), True)

print()
if failures:
    print(f"❌ {len(failures)} check(s) failed:")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print("✅ all radio intel checks passed")
