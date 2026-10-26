"""Run with python3 -m unittest test_transcript_log (no STT model required)."""

import csv
import runpy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import atc_tracker as tracker
from transcript_log import FIELDS, TranscriptLog, export_csv


class LogFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "transcripts"
        self.log = TranscriptLog(self.directory)

    def append(self, received="2026-10-26T13:59:59+00:00", station="YSPT", text='South Port, "ABC"\nturning base'):
        return self.log.append(
            received_at=received, station_id=station, station_name="Southport",
            duration=3.125, backend="test", raw_text=text,
            text=text.replace("South Port", "Southport"),
        )

    def rows(self, path):
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))


class TranscriptLogTests(LogFixture):
    def test_restart_midnight_and_csv_quoting(self):
        path = self.append()
        self.log = TranscriptLog(self.directory)  # simulate a fresh process
        self.append("2026-10-26T14:00:01+00:00")
        rows = self.rows(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["local_date"], "2026-10-26")
        self.assertEqual(rows[1]["local_date"], "2026-10-27")
        self.assertEqual(rows[1]["local_time"], "00:00:01")
        self.assertEqual(rows[1]["local_weekday"], "Tuesday")
        self.assertEqual(rows[0]["timezone"], "Australia/Brisbane")
        self.assertEqual(rows[0]["raw_text"], 'South Port, "ABC"\nturning base')
        self.assertEqual(rows[0]["text"], 'Southport, "ABC"\nturning base')
        self.assertEqual(rows[0]["duration_seconds"], "3.125")
        with path.open(newline="") as handle:
            self.assertEqual(list(csv.reader(handle)).count(list(FIELDS)), 1)

    def test_stations_separate_and_utc_normalized(self):
        self.append(station="YBCG")
        path = self.append("2026-10-27T00:00:00+10:00", station="YBCG_TWR")
        self.assertEqual(self.rows(path)[0]["received_at_utc"], "2026-10-26T14:00:00+00:00")
        self.assertEqual(len(list(self.directory.glob("*.csv"))), 2)

    def test_multiple_writers_keep_one_header_and_complete_rows(self):
        other = TranscriptLog(self.directory)

        def write(index):
            logger = self.log if index % 2 else other
            logger.append(received_at="2026-10-26T14:00:00+00:00", station_id="YSPT",
                          station_name="Southport", duration=1, backend="test",
                          raw_text=f"Call {index}", text=f"Call {index}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(30)))
        rows = self.rows(self.directory / "YSPT.csv")
        self.assertEqual(len(rows), 30)
        self.assertEqual({r["text"] for r in rows}, {f"Call {i}" for i in range(30)})

    def test_invalid_timestamp_and_identifier_do_not_write(self):
        with self.assertRaises(ValueError):
            self.append("2026-10-26T12:00:00")
        with self.assertRaises(ValueError):
            self.append(station="../escape")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_existing_wrong_header_left_intact(self):
        path = self.directory / "YSPT.csv"
        path.write_text("old,header\nkeep,this\n")
        with self.assertRaises(ValueError):
            self.append()
        self.assertEqual(path.read_text(), "old,header\nkeep,this\n")

    def test_combined_export_order_dates_and_source_preservation(self):
        path = self.append("2026-10-26T14:00:02+00:00")
        self.append("2026-10-26T14:00:01+00:00", station="YBCG")
        self.append("2026-10-26T13:00:00+00:00")
        before = path.read_bytes()
        output = Path(self.temp.name) / "exports" / "context.csv"
        count = export_csv(self.directory, output, ["YSPT", "YBCG", "YBCG_TWR", "YSPT"],
                           date(2026, 10, 27), date(2026, 10, 27))
        self.assertEqual(count, 2)
        self.assertEqual([r["station_id"] for r in self.rows(output)], ["YBCG", "YSPT"])
        self.assertEqual(before, path.read_bytes())
        with self.assertRaises(ValueError):
            export_csv(self.directory, path, ["YSPT"])
        with self.assertRaises(ValueError):
            export_csv(self.directory, self.directory / "YBCG.csv", ["YSPT"])
        with self.assertRaises(ValueError):
            export_csv(self.directory, output, ["YSPT"], date(2030, 1, 1))
        self.assertEqual(len(self.rows(output)), 2)  # failed export keeps prior output


class TrackerLoggingTests(LogFixture):
    def setUp(self):
        super().setUp()
        self.state = SimpleNamespace(
            display=None, keywords_enabled=False,
            health={"YSPT": SimpleNamespace(last_tx_at=0, dropped=0)},
        )
        for name, result in (("_send_discord", None), ("_send_telegram", None),
                             ("_military_matches", []), ("_process_radio_intel", [])):
            mock = patch.object(tracker, name, return_value=result)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        self.transcriber = tracker.Transcriber(
            SimpleNamespace(name="test"), self.state, transcript_log=self.log,
        )
        self.addCleanup(self.transcriber.shutdown)

    def log_call(self, text="South Port traffic ABC, five miles east, inbound"):
        self.transcriber._log(text, "YSPT", "Southport", "00:00:00 AEST",
                              "2026-10-26T14:00:00+00:00", 3.0)

    def test_actual_logging_before_repeat_suppression(self):
        self.log_call()
        self.log_call()
        rows = self.rows(self.directory / "YSPT.csv")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["raw_text"].startswith("South Port"))
        self.assertTrue(rows[0]["text"].startswith("Southport"))
        self.assertEqual(self._send_discord.call_count, 1)

    def test_hallucinations_not_logged(self):
        self.log_call("Thanks for watching!")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_forwarding_failure_does_not_lose_log(self):
        self._send_discord.side_effect = RuntimeError("delivery failed")
        with self.assertRaises(RuntimeError):
            self.log_call()
        self.assertEqual(len(self.rows(self.directory / "YSPT.csv")), 1)

    def test_disk_failure_warns_and_forwarding_continues(self):
        with patch.object(self.log, "append", side_effect=OSError("disk full")), \
                patch.object(self.transcriber, "_tui_log") as warning:
            self.log_call()
        self._send_discord.assert_called_once()
        self.assertTrue(any("Transcript CSV failed" in str(c) for c in warning.call_args_list))

    def test_worker_quality_gate(self):
        self.transcriber._backend.transcribe = lambda *a, **k: [
            tracker.transcription.Segment("South Port traffic ABC inbound", -0.2, 0.01, 1.0),
            tracker.transcription.Segment("unreliable speech", -2.0, 0.5, 1.0),
        ]
        audio = np.full(16000, 0.1, dtype=np.float32)
        self.assertTrue(self.transcriber.submit(audio, audio, 1, "YSPT", "Southport",
                                               "00:00:00", "2026-10-26T14:00:00+00:00"))
        self.transcriber._queue.join()
        row = self.rows(self.directory / "YSPT.csv")[0]
        self.assertEqual(row["raw_text"], "South Port traffic ABC inbound")


class TowerConfigTests(unittest.TestCase):
    def test_channel_alias_precedence_and_mount(self):
        for preferred, legacy, expected in (("123", "456", "123"), ("", "456", "456"),
                                            ("   ", "456", "456"), ("123", "", "123")):
            with self.subTest(preferred=preferred, legacy=legacy), \
                    patch.dict("os.environ", {"DISCORD_CHANNEL_YBCG_TOWER": preferred,
                                               "DISCORD_CHANNEL_YBCG_TWR": legacy}), \
                    patch("dotenv.load_dotenv"):
                cfg = runpy.run_path(str(Path(__file__).parent / "config.py"))
                tower = next(s for s in cfg["STREAMS"] if s["icao"] == "YBCG_TWR")
                self.assertEqual(tower["discord_channel_id"], expected)
                self.assertEqual(tower["mount"], "ybcg3_gnd_twr")


if __name__ == "__main__":
    unittest.main()
