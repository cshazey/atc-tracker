#!/usr/bin/env python3
"""Regression tests for the transcript quality filters.

Run:  venv/bin/python test_filters.py

These guard the failure that made the tracker unusable: an oversized Whisper
prompt gets truncated to its tail, Whisper reproduces that tail verbatim at high
confidence, and the result reaches Discord as if it were a real radio call.
Historical logs in logs/ are used as a corpus when present.
"""

import glob
import math
import sys
from collections import Counter

import atc_tracker as A
import config
import transcription as T

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


# ---------------------------------------------------------------------------
print("\nPrompt length (the bug: >223 tokens is silently truncated to its tail)")
try:
    from mlx_whisper.tokenizer import get_tokenizer

    tokenizer = get_tokenizer(multilingual=True, language="en", task="transcribe")
    for s in config.STREAMS:
        n = len(tokenizer.encoding.encode(" " + s["prompt"].strip()))
        check(f"{s['icao']} prompt {n} tokens <= {config.MAX_PROMPT_TOKENS}",
              n <= config.MAX_PROMPT_TOKENS, True)
except ImportError:
    print("  skip  (mlx_whisper unavailable)")


# ---------------------------------------------------------------------------
print("\nPhrase-repetition filter (the old 1-4 char filter missed these)")
check("'contact,' x12", A._has_repeated_ngram(A._normalise_words("Southport 35, " + "contact, " * 12)), True)
check("'North Carolina,' x10", A._has_repeated_ngram(A._normalise_words("CTAF, " + "North Carolina, " * 10)), True)
check("'turning base' x8", A._has_repeated_ngram(A._normalise_words("Southport CTAF, " + "turning base, " * 8)), True)
check("normal readback kept", A._has_repeated_ngram(A._normalise_words(
    "Cleared ILS approach runway one four, Yankee Bravo Whiskey, cleared ILS runway one four")), False)
check("legitimate triple (mayday x3) kept", A._has_repeated_ngram(A._normalise_words(
    "MAYDAY MAYDAY MAYDAY Yankee Bravo Whiskey engine failure")), False)


# ---------------------------------------------------------------------------
print("\nPrompt-echo detector")
OLD_PROMPT_TAIL = (
    "Southport traffic, Golf Kilo Delta, Cessna one seven two, inbound Southport, "
    "CTAF one two six decimal seven, joining crosswind runway one three, circuits. "
    "Southport traffic, final runway one three, stop and go. Taxiing to holding point. "
    "Southport CTAF, turning base, turning final, clear of the runway, backtracking."
)
check("verbatim prompt line flagged", A._is_prompt_echo(
    "Southport traffic, final runway one three, stop and go. Taxiing to holding point.",
    OLD_PROMPT_TAIL), True)
check("second prompt line flagged", A._is_prompt_echo(
    "Southport CTAF, turning base, turning final, clear of the runway, backtracking.",
    OLD_PROMPT_TAIL), True)
check("real traffic not flagged", A._is_prompt_echo(
    "Golf Kilo Delta, five miles north, inbound, joining crosswind runway one four",
    OLD_PROMPT_TAIL), False)
check("other station not flagged", A._is_prompt_echo(
    "Brisbane Centre, Yankee Bravo Whiskey, request descent seven thousand",
    OLD_PROMPT_TAIL), False)
check("short text never flagged", A._is_prompt_echo("Southport traffic", OLD_PROMPT_TAIL), False)


# ---------------------------------------------------------------------------
print("\nFiller blocklist")
for filler in ("Thank you.", "you", "Thanks for watching!", "Bye."):
    check(f"{filler!r} dropped", A._is_hallucination(filler), True)
check("real call kept", A._is_hallucination("Tower, Qantas four one two, ready runway one nine"), False)


# ---------------------------------------------------------------------------
print("\nQuality gate (mlx-whisper computes these signals but never rejects on them)")
S = T.Segment
check("clean segment kept",
      bool(A._apply_quality_gate([S("Yankee Bravo Whiskey descend seven thousand", -0.35, 0.01, 1.2)])), True)
check("observed echo loop (compression_ratio 14.54 at avg_logprob -0.152) dropped",
      bool(A._apply_quality_gate([S("Southport traffic, final. " * 8, -0.152, 0.0, 14.54)])), False)
check("low-confidence decode dropped",
      bool(A._apply_quality_gate([S("mumble mumble", -1.8, 0.2, 1.1)])), False)
check("silence (high no_speech + weak logprob) dropped",
      bool(A._apply_quality_gate([S("Thank you.", -1.05, 0.85, 0.6)])), False)
check("mixed batch keeps only the good segment",
      A._apply_quality_gate([
          S("cleared to land runway one four", -0.3, 0.0, 1.2),
          S("aaa " * 40, -0.2, 0.0, 9.0),
      ]), "cleared to land runway one four")
check("segments with no confidence signals pass through",
      bool(A._apply_quality_gate([S("runway one four cleared to land")])), True)


# ---------------------------------------------------------------------------
print("\nParakeet confidence maps onto the same avg_logprob threshold")
check("confidence 0.99 kept", math.log(0.99) >= config.MIN_AVG_LOGPROB, True)
check("confidence 0.50 kept", math.log(0.50) >= config.MIN_AVG_LOGPROB, True)
check("confidence 0.10 dropped", math.log(0.10) >= config.MIN_AVG_LOGPROB, False)


# ---------------------------------------------------------------------------
print("\nKeyword tiers")
check("mayday -> emergency", A._keyword_tier("MAYDAY MAYDAY MAYDAY engine failure"), A.TIER_EMERGENCY)
check("squawk 7700 -> emergency", A._keyword_tier("aircraft squawking 7700"), A.TIER_EMERGENCY)
check("hornet -> interest", A._keyword_tier("Hornet formation departing to the east"), A.TIER_INTEREST)
check("routine -> none", A._keyword_tier("Qantas four one two contact ground one two one decimal seven"), A.TIER_NONE)
check("'runway 18' no longer alerts", A._keyword_tier("joining downwind runway 18"), A.TIER_NONE)
check("'500 feet' no longer alerts", A._keyword_tier("climbing to 500 feet"), A.TIER_NONE)


# ---------------------------------------------------------------------------
log_files = sorted(glob.glob("logs/*.log"))
if log_files:
    print("\nHistorical corpus (logs/) — what the filters would have removed")
    prompts = {s["icao"]: s["prompt"] for s in config.STREAMS}
    total, dropped = Counter(), Counter()
    for path in log_files:
        for line in open(path, encoding="utf-8", errors="replace"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            icao, text = parts[1], parts[3]
            total[icao] += 1
            if A._is_hallucination(text, prompt=prompts.get(icao, "")):
                dropped[icao] += 1
    for icao in sorted(total):
        pct = dropped[icao] / total[icao] * 100
        print(f"  {icao}: {dropped[icao]:4d} / {total[icao]:4d} dropped ({pct:.1f}%)")
    grand_total, grand_dropped = sum(total.values()), sum(dropped.values())
    check("corpus drop rate is meaningful (>5%)", grand_dropped / grand_total > 0.05, True)
    check("corpus drop rate is not indiscriminate (<30%)", grand_dropped / grand_total < 0.30, True)
else:
    print("\n  skip  (no logs/ corpus present)")


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAILED — {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All checks passed.")
