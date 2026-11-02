"""Offline checks for the NAIPS restricted-area watch (restricted_area.py).

Same hand-rolled shape as the other test_*.py files: no pytest, no network.
The XML below is trimmed from real restricted-area-status responses
(2 Nov 2026): R609 deactivated, R625A active on a NOTAM, R654A with two
periods.

    python3 test_restricted_area.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta, timezone

import restricted_area as R

failures: list = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def section(name: str) -> None:
    print(f"\n{name}")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


R609_XML = """<?xml version='1.0' encoding='UTF-8'?><S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body><restricted-area-status-rsp xmlns="http://www.airservicesaustralia.com/naips/xsd" status="SUCCESS"><period><from>2026-11-02T05:38:31</from><till>2026-11-03T05:38:31</till></period><group><status id="R609-YBBB-R"><type>NOTAM</type><hours>NOTAM</hours><limits/><authority>RDA</authority><activity>MILITARY FLYING</activity><cdr-code>3</cdr-code><complete>true</complete></status></group></restricted-area-status-rsp></S:Body></S:Envelope>"""

GROUP_XML = """<?xml version='1.0' encoding='UTF-8'?><S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body><restricted-area-status-rsp xmlns="http://www.airservicesaustralia.com/naips/xsd" status="SUCCESS"><period><from>2026-11-02T05:38:31</from><till>2026-11-03T05:38:31</till></period><group area="40">
<status id="R625A-YBBB-R"><type>NOTAM</type><hours>NOTAM</hours><authority>RDA</authority><activity>MILITARY FLYING</activity><cdr-code>1</cdr-code><complete>true</complete><period><period><from>2026-11-01T22:00:42</from><till>2026-11-02T13:00:42</till></period><notam-id><loc><id>AMX</id><fir>YBBB</fir><type>R</type></loc><number><series-id>C</series-id><number>4384</number><year>2026</year></number></notam-id><limits><lower type="AMSL"><value>1500</value></lower><upper type="AMSL"><value>8500</value></upper></limits><comment>RR CA </comment></period></status>
<status id="R654A-YBBB-R"><type>NOTAM</type><hours>NOTAM</hours><authority>RDOK</authority><activity>MILITARY FLYING</activity><cdr-code>2</cdr-code><complete>true</complete><period><period><from>2026-11-02T23:30:31</from><till>2026-11-03T13:30:31</till></period><notam-id><loc><id>OKX</id><fir>YBBB</fir><type>R</type></loc><number><series-id>C</series-id><number>484</number><year>2026</year></number></notam-id><limits><lower type="SFC"/><upper type="FL"><value>220</value></upper></limits><comment>RR CA </comment></period><period><period><from>2026-11-01T23:30:31</from><till>2026-11-02T13:30:31</till></period><notam-id><loc><id>OKX</id><fir>YBBB</fir><type>R</type></loc><number><series-id>C</series-id><number>484</number><year>2026</year></number></notam-id><limits><lower type="SFC"/><upper type="AMSL"><value>6500</value></upper></limits></period></status>
</group></restricted-area-status-rsp></S:Body></S:Envelope>"""

INVALID_XML = """<?xml version='1.0' encoding='UTF-8'?><S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body><restricted-area-status-rsp xmlns="http://www.airservicesaustralia.com/naips/xsd" status="INVALID"><info>Restricted area R601A is unknown
</info></restricted-area-status-rsp></S:Body></S:Envelope>"""

NOW = utc(2026, 11, 2, 5, 40)   # 15:40 AEST

# ---------------------------------------------------------------------------
section("Parsing real NAIPS responses")
r609 = R.parse_response(R609_XML)["R609"]
check("R609 parsed under its short name", r609.area, "R609")
check("R609 type NOTAM", r609.type, "NOTAM")
check("R609 activity", r609.activity, "MILITARY FLYING")
check("R609 with no periods is deactivated (website: green)", r609.is_active(NOW), False)
check("R609 limits default to NOTAM", r609.limits(), "NOTAM – NOTAM")

grp = R.parse_response(GROUP_XML)
r625 = grp["R625A"]
check("R625A active inside its NOTAM period", r625.is_active(NOW), True)
cur = r625.active_period(NOW)
check("R625A period end is UTC", cur.end, utc(2026, 11, 2, 13, 0, 42))
check("R625A NOTAM id", cur.notam, "C4384/26")
check("R625A limits", r625.limits(cur), "1500 ft – 8500 ft")
check("R625A inactive after the period", r625.is_active(utc(2026, 11, 2, 13, 1)), False)

r654 = grp["R654A"]
check("R654A periods sorted by start", [p.start.day for p in r654.periods], [1, 2])
check("R654A FL upper limit", r654.periods[1].upper, "FL220")
check("R654A SFC lower limit", r654.periods[1].lower, "SFC")
check("R654A next period found", r654.next_period(NOW).start, utc(2026, 11, 2, 23, 30, 31))

try:
    R.parse_response(INVALID_XML)
    check("INVALID status raises", False, True)
except R.NaipsError as exc:
    check("INVALID status raises with NAIPS's reason", "R601A is unknown" in str(exc), True)

h24 = R.RaStatus(area="R999", type="H24")
check("H24 area is active as published", h24.is_active(NOW), True)

# ---------------------------------------------------------------------------
section("Request building and credential hygiene")
body = R.build_request(["R609"], "ABC123", "secretpw1").decode()
check("duration precedes restricted-area (XSD order)",
      body.index("<duration>") < body.index("<restricted-area>"), True)
check("24 h duration", "<duration>PT24H</duration>" in body, True)
check("scrub removes password and user",
      R._scrub("bad ABC123 secretpw1", "ABC123", "secretpw1"), "bad *** ***")
check("symbol in password rejected locally",
      R.validation_error("ABC123", "pass word!") is not None, True)
check("valid creds pass", R.validation_error("ABC123", "secretpw1"), None)

# ---------------------------------------------------------------------------
section("Watch loop")


class FakeNaips:
    def __init__(self):
        self.periods: tuple = ()
        self.fail = False
        self.calls = 0

    def __call__(self) -> R.RaStatus:
        self.calls += 1
        if self.fail:
            raise R.NaipsError("NAIPS unreachable: timeout")
        return R.RaStatus(area="R609", type="NOTAM", activity="MILITARY FLYING",
                          periods=self.periods)


def make_watch(state_path: str = ""):
    naips = FakeNaips()
    events: list = []
    errors: list = []
    watch = R.RestrictedAreaWatch(
        "R609", fetch=naips,
        on_event=lambda kind, st, now, info: events.append(kind),
        on_error=errors.append,
        window=(dtime(7, 0), dtime(19, 0)),
        idle_interval=1800, active_interval=900,
        state_path=state_path,
    )
    return watch, naips, events, errors


# 09:00 AEST = 23:00 UTC the day before.
t0 = utc(2026, 11, 1, 23, 0)
watch, naips, events, errors = make_watch()
delay = watch.step(t0)
check("first check while deactivated: no alert", events, [])
check("idle interval is 30 min", delay, 1800)

naips.periods = (R.RaPeriod(start=t0 + timedelta(minutes=10), end=t0 + timedelta(hours=3)),)
delay = watch.step(t0 + timedelta(minutes=5))
check("wakes just after a NOTAM'd start rather than a full interval", delay, 360)

t1 = t0 + timedelta(minutes=11)
delay = watch.step(t1)
check("activation alerts once", events, [R.EV_ACTIVATED])
check("active interval is 15 min", delay, 900)
watch.step(t1 + timedelta(minutes=15))
check("still active: no repeat alert", events, [R.EV_ACTIVATED])

# Released early: NAIPS drops the period.
naips.periods = ()
watch.step(t1 + timedelta(minutes=30))
check("deactivation alerts", events, [R.EV_ACTIVATED, R.EV_DEACTIVATED])
watch.step(t1 + timedelta(minutes=60))
check("stays quiet once deactivated", len(events), 2)

section("Watch hours")
watch, naips, events, errors = make_watch()
night = utc(2026, 11, 2, 12, 0)          # 22:00 AEST
calls = naips.calls
delay = watch.step(night)
check("no NAIPS call outside 07:00-19:00", naips.calls, calls)
check("sleep is capped at an hour", delay, 3600)
early = utc(2026, 11, 2, 20, 30)          # 06:30 AEST
check("06:30 sleeps until 07:00", watch.step(early), 1800)

# Active into the evening: one 'still active' note at close, then silence.
watch, naips, events, errors = make_watch()
afternoon = utc(2026, 11, 2, 8, 0)        # 18:00 AEST
naips.periods = (R.RaPeriod(start=afternoon - timedelta(hours=1), end=afternoon + timedelta(hours=5)),)
delay = watch.step(afternoon)
check("an active check near close wakes at 19:00", delay, 15 * 60)  # 15 min < 60 min to close
watch.step(utc(2026, 11, 2, 9, 0, 30))    # 19:00:30 AEST
check("window close while active posts a note", events, [R.EV_ACTIVATED, R.EV_WINDOW_CLOSED])
watch.step(utc(2026, 11, 2, 10, 0))
check("window-close note sent once", events.count(R.EV_WINDOW_CLOSED), 1)

section("Errors")
watch, naips, events, errors = make_watch()
naips.fail = True
delay = watch.step(t0)
check("NAIPS failure retries in 5 min", delay, 300)
watch.step(t0 + timedelta(minutes=5))
check("failure reported once per outage", len(errors), 1)
naips.fail = False
watch.step(t0 + timedelta(minutes=10))
check("recovery reported with an empty message", errors[-1], "")

section("Restart does not re-alert")
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "ra_state.json")
    watch, naips, events, errors = make_watch(path)
    naips.periods = (R.RaPeriod(start=t0 - timedelta(hours=1), end=t0 + timedelta(hours=3)),)
    watch.step(t0)
    check("first run alerts an area that is already active", events, [R.EV_ACTIVATED])
    watch2, naips2, events2, _ = make_watch(path)
    naips2.periods = naips.periods
    watch2.step(t0 + timedelta(minutes=15))
    check("restart while still active stays quiet", events2, [])
    naips2.periods = ()
    watch2.step(t0 + timedelta(minutes=30))
    check("restarted watch still reports deactivation", events2, [R.EV_DEACTIVATED])

section("Window parsing")
check("default window", R.parse_window("07:00-19:00"), (dtime(7, 0), dtime(19, 0)))
check("garbage falls back to 07-19", R.parse_window("nonsense"), (dtime(7, 0), dtime(19, 0)))

print()
if failures:
    print(f"❌ {len(failures)} check(s) failed:")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print("✅ all restricted-area checks passed")
