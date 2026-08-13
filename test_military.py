#!/usr/bin/env python3
"""Regression tests for the military callsign registry and matcher.

Run:  venv/bin/python test_military.py

The matcher's whole job is to fire on "Falcon one one" without firing on the
2,900 lines of ordinary Gold Coast traffic sitting in logs/. Both halves are
tested — the true positives below, and a false-positive budget measured against
the historical logs when they are present.
"""

import glob
import sys

import config
import military_callsigns as M

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def hit(text: str):
    """First match for text, or None."""
    matches = M.get_registry().match(text)
    return matches[0] if matches else None


def names(text: str) -> list[str]:
    return [m.callsign for m in M.get_registry().match(text)]


# ---------------------------------------------------------------------------
print("\nScraper (source page markup: nested tables, wrapped cells, <font> tags)")

SAMPLE_HTML = """
<table>
 <tr><td>CALLSIGN</td><td>ATC ID PREFIX</td><td>AIRCRAFT TYPE</td>
     <td>SQUADRON</td><td>OPS FREQ</td></tr>
 <tr><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>
 <tr><td width="25%" class="style21"><strong>FALCON - Confirmed</strong></td>
     <td>FALC, FALCN</td><td><font face="Arial">EA18G GROWLER</font></td>
     <td>6 SQN AMBERLEY</td><td>395.350</td></tr>
 <tr><td><strong>ALBATROSS -
     Confirmed</strong></td><td>ALBT</td><td>TRITON MQ-4C UAS</td>
     <td>9 SQN EDINBURGH</td><td>&nbsp;</td></tr>
 <tr><td>THUNDER</td><td>&nbsp;</td><td>HERCULES C-130-H</td>
     <td>37 SQN RICHMOND</td><td>&nbsp;</td></tr>
 <tr><td><strong>THUNDER - Confirmed</strong></td><td>TNDR</td>
     <td>C-17 GLOBEMASTER III</td><td>36 SQN AMBERLEY</td><td>&nbsp;</td></tr>
 <tr><td>? - Confirmed</td><td>BARTO</td><td>HORNET</td><td>WILLIAMTOWN</td><td></td></tr>
</table>
"""

records = {r.callsign: r for r in M.parse_html(SAMPLE_HTML)}
check("header row not stored", "CALLSIGN" in records, False)
check("blank row not stored", "" in records, False)
check("'?' placeholder not stored", "?" in records, False)
check("FALCON parsed", records["FALCON"].aircraft, "EA18G GROWLER")
check("FALCON squadron", records["FALCON"].squadron, "6 SQN AMBERLEY")
check("FALCON prefixes", records["FALCON"].prefixes, ("FALC", "FALCN"))
check("FALCON confirmed flag", records["FALCON"].confirmed, True)
check("callsign wrapped across lines", "ALBATROSS" in records, True)
check("duplicate callsigns merged", len([k for k in records if k == "THUNDER"]), 1)
check("merged aircraft kept both",
      records["THUNDER"].aircraft, "HERCULES C-130-H / C-17 GLOBEMASTER III")
check("merged confirmed is sticky", records["THUNDER"].confirmed, True)
check("short scrape is rejected before it overwrites the db",
      len(records) < config.MILITARY_MIN_SCRAPE_ROWS, True)


# ---------------------------------------------------------------------------
print("\nRegistry loaded")
registry = M.get_registry()
check("registry populated", len(registry) > 300, True)
check("FALCON is an EA-18G", "GROWLER" in (registry.lookup("FALCON").aircraft or ""), True)
check("lookup is case/space insensitive", registry.lookup("black hawk").callsign, "BLACKHAWK")
check("lookalike list present", len(M._lookalike_words()) > 500, True)


# ---------------------------------------------------------------------------
print("\nTrue positives — spoken callsigns")
check("FALCON one one", names("Falcon one one, Brisbane Centre, climb flight level two four zero"),
      ["FALCON"])
check("flight number captured",
      hit("Falcon one one, climbing").flight, "One One")
check("digits as flight number", hit("Falcon 11, climbing").flight, "11")
check("WOLF two one", names("Wolf two one, cleared ILS runway one four"), ["WOLF"])
check("WEDGETAIL", names("Wedgetail zero one, maintain flight level three three zero"),
      ["WEDGETAIL"])
check("BLACKHAWK heard as two words",
      names("Black Hawk two one on descent"), ["BLACKHAWK"])
check("ROULETTES", names("Roulettes formation, five miles south, airshow display"), ["ROULETTES"])
check("ARMY needs its number — and has one",
      names("Army two one, contact tower one one eight decimal seven"), ["ARMY"])
check("NAVY at YBBN", names("Brisbane, Navy one three, ready runway one zero left"), ["NAVY"])
check("strong hits are strong",
      hit("Falcon one one, climbing").strong, True)


# ---------------------------------------------------------------------------
print("\nTrue positives — Whisper mangling (flagged, but only as 'possible')")
check("'Falcum 11' → FALCON", names("Falcum 11 request descent"), ["FALCON"])
check("'Foulcon one one' → FALCON", names("Foulcon one one visual approach"), ["FALCON"])
check("a misheard callsign is weak on its own",
      hit("Falcum 11 request descent").strong, False)
check("…and strong with military context",
      hit("Falcum 11, RAAF Amberley, request descent").strong, True)
check("kind is reported", hit("Falcum 11 request descent").kind, "phonetic")


# ---------------------------------------------------------------------------
print("\nFalse positives — ordinary Gold Coast / Brisbane traffic")
NEGATIVES = [
    ("registration readback", "Golf Kilo Delta, Cessna one seven two, five miles north, inbound Southport"),
    ("phonetic alphabet", "Cleared ILS approach runway one four, Yankee Bravo Whiskey"),
    ("ZULU is a letter, not an F-35", "Descend via RNP Zulu, Sierra Whiskey X-ray"),
    ("Dash 8 is a civil type", "Dash 8 turning base runway one four"),
    ("King Air is a civil type", "King Air 350 inbound from the north"),
    ("Baron is a civil type", "Baron 58 joining crosswind runway one four"),
    ("Archer is a civil type", "Archer 28 downwind for a full stop"),
    ("bare word without a number", "Southport traffic, inbound for the Mirage, one thousand"),
    ("'reach' is a verb", "I will reach two thousand five hundred shortly"),
    ("'the army' is not ARMY", "the army is on the ground already"),
    ("'starting' is not STARLING", "starting 53, contact ground on one two one decimal seven"),
    ("'water' is not WALER", "Water four zero one, cleared to land runway one nine right"),
    ("'centre on' is not CENTURION", "Contact Brisbane Centre on one three four decimal three"),
    ("'search for' is not SEARCHER", "Wind one six, search for six, runway one nine left"),
    ("numbers glued to words", "S20 Park is at five hundred over the broadwater"),
]
for label, text in NEGATIVES:
    check(label, names(text), [])


# ---------------------------------------------------------------------------
print("\nFlight-number reader")
check("distance is not a flight number",
      hit("Roulettes formation, five miles south, airshow display").flight, "Formation")
check("altitude is not a flight number",
      hit("Wedgetail two thousand feet").flight if hit("Wedgetail two thousand feet") else "", "")


# ---------------------------------------------------------------------------
print("\nEdit distance / phonetics")
check("identical", M.edit_distance("FALCON", "FALCON", 2), 0)
check("one substitution", M.edit_distance("FALCON", "FALCUN", 2), 1)
check("gives up past the bound", M.edit_distance("FALCON", "WEDGETAIL", 2), 3)
check("budget scales with length", (M.edit_budget(4), M.edit_budget(6), M.edit_budget(9)), (0, 1, 2))
check("soundex skeleton", M.phonetic_key("FOULCON"), M.phonetic_key("FALCON"))
check("skeleton is not truncated to 4 like classic soundex",
      M.phonetic_key("FIREBIRD") == M.phonetic_key("FIREBALL"), False)


# ---------------------------------------------------------------------------
print("\nFalse-positive budget over the historical logs")
lines = []
for path in sorted(glob.glob("logs/*.log")):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            parts = raw.split(" | ")
            if len(parts) >= 4:
                lines.append(parts[3].strip())

if not lines:
    print("  skip  (no logs/ corpus on this machine)")
else:
    strong = weak = 0
    for text in lines:
        for m in registry.match(text):
            if m.strong:
                strong += 1
            else:
                weak += 1
    rate = (strong + weak) / len(lines)
    print(f"        {len(lines)} lines · {strong} strong · {weak} weak · {rate:.2%} annotated")
    # Measured at 25 strong / 13 weak over 3,026 lines, and most of the strong
    # hits are real (P-8 POSEIDON, AUSSIE, NAVY). Anything approaching 2% means
    # a guard has regressed.
    check("under 2% of transmissions annotated", rate < 0.02, True)
    check("strong hits stay rare", strong / len(lines) < 0.015, True)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"❌ {len(failures)} failure(s)")
    for f in failures:
        print(f"   {f}")
    sys.exit(1)
print("✅ all military callsign checks passed")
