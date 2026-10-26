"""Continuous, local-only CSV transcripts and an optional combined export.

The tracker has one writer thread. Each append opens and closes the file so a
normal quit/restart retains every completed row without a shutdown flush.
This module uses only the standard library; exporting needs no STT model.
"""

import argparse
import csv
import fcntl
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo


DEFAULT_DIRECTORY = Path(__file__).resolve().parent / "logs" / "transcripts"
FIELDS = (
    "received_at_utc", "local_date", "local_time", "local_weekday", "timezone",
    "station_id", "station_name", "duration_seconds", "stt_backend",
    "raw_text", "text",
)


class TranscriptLog:
    def __init__(self, directory=DEFAULT_DIRECTORY, timezone_name="Australia/Brisbane"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)
        self._lock = Lock()

    def append(self, *, received_at, station_id, station_name, duration,
               backend, raw_text, text):
        if not station_id or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in station_id):
            raise ValueError("Invalid station identifier")
        received = datetime.fromisoformat(received_at)
        if received.tzinfo is None:
            raise ValueError("Transmission timestamp must include a timezone")
        local = received.astimezone(self.timezone)
        row = dict(zip(FIELDS, (
            received.astimezone(timezone.utc).isoformat(),
            local.date().isoformat(), local.time().isoformat(),
            ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[local.weekday()],
            self.timezone_name, station_id, station_name, round(duration, 3),
            backend, raw_text, text,
        )))
        path = self.directory / f"{station_id}.csv"
        with self._lock, path.open("a+", encoding="utf-8", newline="") as handle:
            # Coordinate with exports (and another tracker process) so readers
            # never observe a half-written row or a duplicate initial header.
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            header = next(csv.reader(handle), None)
            if header is not None and header != list(FIELDS):
                raise ValueError(f"Unexpected CSV header in {path}; existing log left intact")
            handle.seek(0, 2)
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if header is None:
                writer.writeheader()
            writer.writerow(row)
        return path


def export_csv(directory, output, stations, from_date=None, to_date=None):
    """Combine selected station files chronologically; source files stay intact."""
    paths = [Path(directory) / f"{station}.csv" for station in dict.fromkeys(stations)]
    output = Path(output)
    if (output.resolve().parent == Path(directory).resolve()
            or output.resolve() in {path.resolve() for path in paths}):
        raise ValueError("Export must not overwrite a station log")
    if from_date and to_date and from_date > to_date:
        raise ValueError("Start date must not be after end date")
    rows = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            reader = csv.DictReader(handle)
            if reader.fieldnames != list(FIELDS):
                raise ValueError(f"Unexpected CSV header in {path}")
            for row in reader:
                # Ignore structurally incomplete rows left by an interrupted write.
                if None in row or any(value is None for value in row.values()):
                    continue
                day = date.fromisoformat(row["local_date"])
                if (from_date is None or day >= from_date) and (to_date is None or day <= to_date):
                    rows.append(row)
    if not rows:
        raise ValueError("No transcripts found for the selected stations and dates")
    rows.sort(key=lambda row: row["received_at_utc"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Combine live station CSV logs for AI analysis")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_DIRECTORY.parent / "exports" / "radio-context.csv")
    parser.add_argument("--stations", nargs="+", choices=("YBCG", "YBCG_TWR", "YSPT", "YBBN"),
                        default=["YBCG", "YBCG_TWR", "YSPT"])
    parser.add_argument("--from-date", type=date.fromisoformat, help="Inclusive local date: YYYY-MM-DD")
    parser.add_argument("--to-date", type=date.fromisoformat, help="Inclusive local date: YYYY-MM-DD")
    args = parser.parse_args()
    try:
        count = export_csv(args.directory, args.output, args.stations, args.from_date, args.to_date)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(f"Exported {count} transmissions to {args.output}")


if __name__ == "__main__":
    main()
