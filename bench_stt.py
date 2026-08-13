#!/usr/bin/env python3
"""Compare speech-to-text backends on real recorded ATC audio.

Usage:
    venv/bin/python bench_stt.py                      # everything in recordings/
    venv/bin/python bench_stt.py --station YSPT       # one station
    venv/bin/python bench_stt.py --limit 50 --out bench.md

There is no ground-truth transcript for LiveATC audio, so this does not compute
WER. It gives you the two things that can be measured objectively — speed and
how often each backend produces output the quality filters reject — plus a
side-by-side transcript table for your own judgement. Run it on audio captured
from the stations you actually care about; general-purpose WER leaderboards do
not predict which model handles a squelched 8 kHz VHF feed better.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

import miniaudio
import numpy as np

import atc_tracker
import config
import transcription


def load_wav(path: Path) -> np.ndarray:
    decoded = miniaudio.decode_file(
        str(path),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=config.VAD_SAMPLE_RATE,
    )
    return np.array(decoded.samples, dtype=np.float32) / 32768.0


def station_of(path: Path) -> str:
    return path.stem.split("_")[0].upper()


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare STT backends on recorded ATC audio")
    parser.add_argument("--recordings", default="recordings", help="Directory of .wav recordings")
    parser.add_argument("--station", default=None, help="Only clips from this ICAO")
    parser.add_argument("--limit", type=int, default=None, help="Maximum clips to process")
    parser.add_argument("--backends", nargs="+", default=["whisper", "parakeet"])
    parser.add_argument("--out", default=None, help="Write a markdown report here")
    args = parser.parse_args()

    root = Path(args.recordings)
    clips = sorted(root.rglob("*.wav"))
    if args.station:
        clips = [c for c in clips if station_of(c) == args.station.upper()]
    if args.limit:
        clips = clips[: args.limit]
    if not clips:
        print(f"No recordings found under {root}/. Run the tracker with recording enabled first.")
        return 1

    prompts = {s["icao"]: s.get("prompt", "") for s in config.STREAMS}

    backends = {}
    for name in args.backends:
        try:
            backend = transcription.build_backend(name)
            backend.ensure_ready(log=lambda m: print(f"  {m}"))
            backends[name] = backend
        except Exception as exc:
            print(f"Skipping {name}: {exc}")
    if not backends:
        print("No usable backends.")
        return 1

    print(f"\n{len(clips)} clips × {len(backends)} backends\n")

    results: dict[str, list] = {name: [] for name in backends}
    rows = []
    for i, clip in enumerate(clips, 1):
        audio = load_wav(clip)
        duration = len(audio) / config.VAD_SAMPLE_RATE
        icao = station_of(clip)
        processed = atc_tracker._preprocessor.process(audio)
        row = {"clip": clip.name, "station": icao, "duration": duration}
        for name, backend in backends.items():
            start = time.time()
            try:
                segments = backend.transcribe(processed, prompt=prompts.get(icao, ""))
                gated = atc_tracker._apply_quality_gate(segments)
                raw = " ".join(s.text for s in segments).strip()
                rejected = bool(raw) and not gated
                hallucinated = bool(gated) and atc_tracker._is_hallucination(
                    gated, prompt=prompts.get(icao, "")
                )
            except Exception as exc:
                raw, gated, rejected, hallucinated = f"<error: {exc}>", "", False, False
            elapsed = time.time() - start
            results[name].append({
                "elapsed": elapsed,
                "duration": duration,
                "rejected": rejected,
                "hallucinated": hallucinated,
                "empty": not gated,
            })
            row[name] = {"text": gated or raw, "elapsed": elapsed,
                         "rejected": rejected, "hallucinated": hallucinated}
        rows.append(row)
        print(f"[{i}/{len(clips)}] {clip.name} ({duration:.1f}s)")
        for name in backends:
            flag = ""
            if row[name]["rejected"]:
                flag = "  [GATED]"
            elif row[name]["hallucinated"]:
                flag = "  [HALLUCINATION]"
            print(f"    {name:9} {row[name]['elapsed']:5.2f}s{flag}  {row[name]['text'][:110]}")

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    summary_lines = []
    for name, entries in results.items():
        total_audio = sum(e["duration"] for e in entries)
        total_time = sum(e["elapsed"] for e in entries)
        rtf = total_audio / total_time if total_time else 0
        med = statistics.median(e["elapsed"] for e in entries)
        rejected = sum(1 for e in entries if e["rejected"])
        hallucinated = sum(1 for e in entries if e["hallucinated"])
        empty = sum(1 for e in entries if e["empty"])
        line = (
            f"{name:9}  {rtf:5.1f}x realtime  median {med:5.2f}s/clip  "
            f"gated {rejected:3d}  hallucinated {hallucinated:3d}  empty {empty:3d}"
        )
        print(line)
        summary_lines.append(line)
    print("\n'gated' and 'hallucinated' are how often a backend produced output the")
    print("quality filters had to throw away — lower is better. Read the transcripts")
    print("above for accuracy; these numbers only measure speed and failure rate.")

    if args.out:
        out = Path(args.out)
        with out.open("w", encoding="utf-8") as f:
            f.write("# STT backend comparison\n\n")
            f.write(f"{len(clips)} clips from `{root}/`\n\n## Summary\n\n```\n")
            f.write("\n".join(summary_lines) + "\n```\n\n## Transcripts\n\n")
            f.write("| Clip | Station | Dur | " + " | ".join(backends) + " |\n")
            f.write("|---|---|---|" + "---|" * len(backends) + "\n")
            for row in rows:
                cells = []
                for name in backends:
                    text = row[name]["text"].replace("|", "\\|")[:200] or "_(empty)_"
                    if row[name]["rejected"]:
                        text = f"**[gated]** {text}"
                    cells.append(text)
                f.write(
                    f"| {row['clip']} | {row['station']} | {row['duration']:.1f}s | "
                    + " | ".join(cells) + " |\n"
                )
        print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
