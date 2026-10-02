#!/usr/bin/env python3
"""
ATC Tracker — multi-station live speech-to-text transcription.
Streams MP3 audio from LiveATC.net, detects transmissions via VAD, transcribes
them with a pluggable local backend (Whisper or Parakeet — see
transcription.py), highlights configurable keywords, records each transmission
to disk, and forwards everything to Discord and Telegram.

Controls:
  1/2/3/… — toggle individual stations on/off
  K       — toggle keyword highlighting on/off
  T       — toggle Telegram forwarding
  P       — pause/resume transcription
  Q / Ctrl+C — quit
"""

import argparse
import collections
import functools
import html
import json
import queue
import random
import re
import shutil
import sys
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import miniaudio
import numpy as np
from scipy.signal import butter, sosfilt
import requests
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

import adsb_classify
import adsb_tracker
import adsb_web
import config
import geo
import military_callsigns
import transcription
from config import (
    ATC_CORRECTIONS,
    AUDIO_PREPROCESSING,
    DISCORD_ALERTS_CHANNEL_ID,
    DISCORD_COMMANDS_CHANNEL_ID,
    KEYWORDS,
    KEYWORDS_EMERGENCY,
    KEYWORDS_INTEREST,
    MAX_COMPRESSION_RATIO,
    MAX_NO_SPEECH_PROB,
    MAX_TRANSMISSION_SEC,
    MIN_AVG_LOGPROB,
    PROMPT_ECHO_MAX_OVERLAP,
    RECONNECT_DELAY_SEC,
    RECONNECT_MAX_DELAY_SEC,
    REPEAT_NGRAM_MAX_WORDS,
    REPEAT_NGRAM_THRESHOLD,
    STREAMS,
    STREAM_STALL_TIMEOUT_SEC,
    VAD_ADAPTIVE,
    VAD_CHUNK_FRAMES,
    VAD_NOISE_FLOOR_MULT,
    VAD_NOISE_WINDOW_FRAMES,
    VAD_PREROLL_SEC,
    VAD_RMS_THRESHOLD,
    VAD_SAMPLE_RATE,
    VAD_SILENCE_HANGOVER,
    WEAK_AVG_LOGPROB,
    WHISPER_MODEL,
)

_AEST = ZoneInfo("Australia/Brisbane")


def _now_ts() -> str:
    utc = datetime.now(timezone.utc)
    aest = utc.astimezone(_AEST)
    return f"{aest.strftime('%H:%M:%S')} AEST / {utc.strftime('%H:%M:%S')}Z"


def _now_iso() -> str:
    """ISO8601 UTC timestamp for Discord's native embed timestamp field."""
    return datetime.now(timezone.utc).isoformat()


class _TranscriptFeed:
    """Thread-safe ring of the most recent ATC transcripts, for the web map.

    The dashboard shows the latest radio calls beside the traffic picture, so
    you can see what is being *said* next to what is being *tracked*. This is a
    read-through of what already goes to Discord — nothing is transcribed twice
    and no audio is retained here, only the text.
    """

    def __init__(self, maxlen: int):
        self._items: collections.deque = collections.deque(maxlen=max(1, maxlen))
        self._lock = threading.Lock()

    def add(self, item: dict) -> None:
        with self._lock:
            self._items.append(item)

    def recent(self, limit: int = 40) -> list:
        with self._lock:
            items = list(self._items)
        items.reverse()
        return items[: max(0, limit)]


_transcript_feed = _TranscriptFeed(config.ADSB_WEB_NOTES)


def recent_transcripts(limit: int = 40) -> list:
    """Newest-first ATC transcripts for the web dashboard's `/api/notes`."""
    return _transcript_feed.recent(limit)


# ---------------------------------------------------------------------------
# Shared runtime state
# ---------------------------------------------------------------------------

class RuntimeConfig:
    """Persist runtime changes that should survive restarts."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._stream_urls: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            return
        stream_urls = data.get("stream_urls", {})
        if isinstance(stream_urls, dict):
            self._stream_urls = {
                str(k).upper(): str(v)
                for k, v in stream_urls.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }

    def _save_locked(self) -> None:
        data = {"stream_urls": dict(sorted(self._stream_urls.items()))}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self._path)

    def stream_overrides(self) -> dict[str, str]:
        with self._lock:
            return dict(self._stream_urls)

    def set_stream_url(self, icao: str, url: str) -> None:
        with self._lock:
            self._stream_urls[icao.upper()] = url
            self._save_locked()

    def reset_stream_url(self, icao: str) -> bool:
        with self._lock:
            existed = icao.upper() in self._stream_urls
            self._stream_urls.pop(icao.upper(), None)
            self._save_locked()
            return existed


class StationHealth:
    """Per-station liveness counters, surfaced by /health."""

    __slots__ = ("last_audio_at", "last_tx_at", "reconnects", "dropped", "connected", "active_url")

    def __init__(self):
        self.last_audio_at: Optional[float] = None
        self.last_tx_at: Optional[float] = None
        self.reconnects = 0
        self.dropped = 0
        self.connected = False
        self.active_url = ""


class SharedState:
    def __init__(self, keywords_enabled: bool, runtime_config: RuntimeConfig):
        self.keywords_enabled = keywords_enabled
        self.paused = False
        self.recording_enabled = config.RECORDING_ENABLED
        self.military_enabled = config.MILITARY_DETECTION_ENABLED
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self.station_enabled: dict = {}
        self.display: Optional["LiveDisplay"] = None
        self._runtime_config = runtime_config
        self._default_stream_urls = {s["icao"]: s["url"] for s in STREAMS}
        self._stream_urls = dict(self._default_stream_urls)
        self._stream_versions = {s["icao"]: 0 for s in STREAMS}
        self.health = {s["icao"]: StationHealth() for s in STREAMS}
        self._vad_thresholds = {
            s["icao"]: s.get("vad_threshold", VAD_RMS_THRESHOLD) for s in STREAMS
        }
        for icao, url in runtime_config.stream_overrides().items():
            if icao in self._stream_urls:
                self._stream_urls[icao] = url

    def toggle_keywords(self) -> bool:
        with self._lock:
            self.keywords_enabled = not self.keywords_enabled
            return self.keywords_enabled

    def set_keywords(self, enabled: bool) -> None:
        with self._lock:
            self.keywords_enabled = enabled

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = paused

    def set_recording(self, enabled: bool) -> None:
        with self._lock:
            self.recording_enabled = enabled

    def set_military(self, enabled: bool) -> None:
        with self._lock:
            self.military_enabled = enabled

    def get_vad_threshold(self, icao: str) -> float:
        with self._lock:
            return self._vad_thresholds.get(icao, VAD_RMS_THRESHOLD)

    def set_vad_threshold(self, icao: str, value: float) -> None:
        with self._lock:
            self._vad_thresholds[icao.upper()] = value

    def toggle_pause(self) -> bool:
        with self._lock:
            self.paused = not self.paused
            return self.paused

    def toggle_station(self, icao: str) -> bool:
        with self._lock:
            self.station_enabled[icao] = not self.station_enabled.get(icao, True)
            return self.station_enabled[icao]

    def is_enabled(self, icao: str) -> bool:
        return self.station_enabled.get(icao, True)

    def get_stream_url(self, icao: str) -> str:
        with self._lock:
            return self._stream_urls.get(icao, self._default_stream_urls.get(icao, ""))

    def get_default_stream_url(self, icao: str) -> str:
        return self._default_stream_urls.get(icao, "")

    def get_stream_url_snapshot(self, icao: str) -> tuple[str, int]:
        with self._lock:
            return (
                self._stream_urls.get(icao, self._default_stream_urls.get(icao, "")),
                self._stream_versions.get(icao, 0),
            )

    def get_stream_url_version(self, icao: str) -> int:
        with self._lock:
            return self._stream_versions.get(icao, 0)

    def set_stream_url(self, icao: str, url: str) -> bool:
        icao = icao.upper()
        with self._lock:
            if self._stream_urls.get(icao) == url:
                return False
            self._stream_urls[icao] = url
            self._stream_versions[icao] = self._stream_versions.get(icao, 0) + 1
        self._runtime_config.set_stream_url(icao, url)
        return True

    def reset_stream_url(self, icao: str) -> bool:
        icao = icao.upper()
        default_url = self._default_stream_urls.get(icao, "")
        with self._lock:
            changed = self._stream_urls.get(icao) != default_url
            self._stream_urls[icao] = default_url
            if changed:
                self._stream_versions[icao] = self._stream_versions.get(icao, 0) + 1
        persisted = self._runtime_config.reset_stream_url(icao)
        return changed or persisted

    def bump_stream_url_version(self, icao: str) -> None:
        """Signals the station's stream loop to drop its connection and redial.
        The loop already watches this counter to pick up /seturl changes."""
        icao = icao.upper()
        with self._lock:
            self._stream_versions[icao] = self._stream_versions.get(icao, 0) + 1

    def has_stream_override(self, icao: str) -> bool:
        with self._lock:
            return self._stream_urls.get(icao) != self._default_stream_urls.get(icao)


def _valid_stream_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _format_stream_url_line(state: SharedState, station: dict) -> str:
    icao = station["icao"]
    suffix = " (override)" if state.has_stream_override(icao) else " (default)"
    return f"{icao} {station['name']}: <code>{html.escape(state.get_stream_url(icao))}</code>{suffix}"


# ---------------------------------------------------------------------------
# Live terminal UI
# ---------------------------------------------------------------------------

class LiveDisplay:
    """Renders the TUI at ~10 fps from a dedicated thread.

    Uses Rich only for formatting (Panel/Text/color); cursor control is done
    directly with ANSI sequences so it works regardless of how the process
    was launched. All worker threads call .log() and the render thread owns
    stdout exclusively while active.
    """

    # header: top-border + 3 info lines + 2 separators + N station lines + controls + bottom-border
    _HEADER_LINES = 8  # base count excluding stations

    def __init__(self, model: str, state: SharedState, console: Console):
        self._model = model
        self._state = state
        self._console = console
        self._lines: collections.deque = collections.deque(maxlen=500)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._prev_height = 0
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="render"
        )

    # ── Public API (thread-safe) ────────────────────────────────────────────

    def log(self, line: Text) -> None:
        with self._lock:
            self._lines.append(line)

    def refresh(self) -> None:
        pass  # render loop picks up changes on next tick

    # ── Context manager ─────────────────────────────────────────────────────

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join(timeout=2)

    # ── Render loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._draw()
            time.sleep(0.1)

    def _draw(self) -> None:
        cols, rows = shutil.get_terminal_size((80, 24))
        header_size = self._HEADER_LINES + len(STREAMS)
        log_visible = max(3, rows - header_size - 4)

        # Render panels into a string buffer using Rich for colour/borders
        buf = StringIO()
        tmp = Console(
            file=buf, width=cols,
            force_terminal=True, force_interactive=True,
            highlight=False, no_color=False,
        )
        tmp.print(Panel(
            self._build_header(),
            title="[bold cyan]ATC Tracker[/bold cyan]",
            border_style="cyan",
        ))

        with self._lock:
            visible = list(self._lines)[-log_visible:]

        if visible:
            log_text = Text()
            for i, line in enumerate(visible):
                log_text.append_text(line)
                if i < len(visible) - 1:
                    log_text.append("\n")
        else:
            log_text = Text("  waiting for transmissions...", style="dim")

        tmp.print(Panel(
            log_text,
            title="[dim]Transmissions[/dim]",
            border_style="dim",
        ))

        lines = buf.getvalue().splitlines()
        height = len(lines)

        # Build one output string: cursor-up, then overwrite each line
        out: list = []
        if self._prev_height > 0:
            out.append(f"\033[{self._prev_height}A\r")
        for line in lines:
            out.append(f"\r\033[2K{line}\n")
        # Clear leftover lines if render shrank
        for _ in range(max(0, self._prev_height - height)):
            out.append("\r\033[2K\n")

        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._prev_height = height

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_header(self) -> Text:
        h = Text()

        h.append("  Model   : ", style="dim")
        h.append(self._model + "\n", style="cyan")

        h.append("  Keywords: ", style="dim")
        kw = self._state.keywords_enabled
        h.append("ON   " if kw else "OFF  ", style="green" if kw else "yellow")
        h.append("  Telegram: ", style="dim")
        if not config.TELEGRAM_ENABLED:
            h.append("OFF", style="yellow")
            h.append("  (not configured)", style="dim")
        elif _telegram_active:
            h.append("ON", style="green")
            h.append(f"  →  {config.TELEGRAM_CHAT_ID}  (/help for commands)", style="dim")
        else:
            h.append("OFF", style="yellow")
            h.append("  (press T to enable)", style="dim")
        if self._state.paused:
            h.append("  ⏸ PAUSED", style="bold red")
        h.append("\n")

        h.append("  Discord : ", style="dim")
        if config.DISCORD_ENABLED:
            h.append("ON", style="green")
            h.append("  (see #commands for commands)", style="dim")
        else:
            h.append("OFF", style="yellow")
        h.append("\n")

        h.append("  " + "─" * 50 + "\n", style="dim cyan")

        for i, s in enumerate(STREAMS, 1):
            enabled = self._state.is_enabled(s["icao"])
            h.append(f"  [{i}] {s['icao']}  {s['name']:<16}  ")
            h.append("ON   \n" if enabled else "MUTED\n",
                     style="green" if enabled else "red")

        h.append("  " + "─" * 50 + "\n", style="dim cyan")
        h.append("  [K] keywords   [T] telegram   [P] pause/resume   [Q] quit", style="dim")

        return h


# Module-level display reference used by _post_telegram (set in main).
_display_ref: Optional[LiveDisplay] = None

# Runtime toggle for Telegram sending — off by default even when credentials
# are configured. Toggle with the "T" key. Gates _post_telegram only; Discord
# and the terminal/log file are unaffected.
_telegram_active: bool = False

# Set in main(); read by /health so the command handler doesn't need them
# threaded through every call site.
_transcriber_ref: Optional["Transcriber"] = None
_backend_name: str = config.STT_BACKEND


# ---------------------------------------------------------------------------
# HTTP stream source for miniaudio
# ---------------------------------------------------------------------------

class LiveATCSource(miniaudio.StreamableSource):
    _BUFFER_MAX = 65536

    def __init__(self, url: str, headers: dict, stall_timeout: float = STREAM_STALL_TIMEOUT_SEC):
        self._deque: collections.deque = collections.deque()
        self._deque_bytes = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._error: Optional[Exception] = None
        self._stall_timeout = stall_timeout
        self.last_data_at: Optional[float] = None
        self.resolved_url = url
        self._thread = threading.Thread(target=self._fetch, args=(url, headers), daemon=True)
        self._thread.start()

    def _fetch(self, url: str, headers: dict):
        try:
            # allow_redirects is the point of using d.liveatc.net: it 302s to
            # whichever edge currently serves the mount.
            with requests.get(
                url, headers=headers, stream=True, timeout=30, allow_redirects=True
            ) as resp:
                resp.raise_for_status()
                self.resolved_url = resp.url
                for chunk in resp.iter_content(chunk_size=4096):
                    if self._stop.is_set():
                        return
                    if not chunk:
                        continue
                    with self._cond:
                        self.last_data_at = time.monotonic()
                        self._deque.append(chunk)
                        self._deque_bytes += len(chunk)
                        while self._deque_bytes > self._BUFFER_MAX and self._deque:
                            dropped = self._deque.popleft()
                            self._deque_bytes -= len(dropped)
                        self._cond.notify_all()
        except Exception as exc:
            with self._cond:
                self._error = exc
                self._cond.notify_all()

    def read(self, num_bytes: int) -> bytes:
        result = bytearray()
        while len(result) < num_bytes:
            with self._cond:
                # The stall check has to live here, not in the consumer loop: a
                # LiveATC edge that holds the socket open but stops sending
                # parks this thread in the wait() below, so the consumer never
                # gets another iteration in which to notice.
                waiting_since = time.monotonic()
                while not self._deque and not self._stop.is_set() and self._error is None:
                    self._cond.wait(timeout=1.0)
                    idle_since = self.last_data_at or waiting_since
                    if time.monotonic() - idle_since > self._stall_timeout:
                        raise IOError(
                            f"stream stalled — no audio for {self._stall_timeout}s"
                        )
                # Drain whatever arrived before the error: audio already in the
                # buffer is still good, and discarding it truncates the final
                # transmission of every disconnect.
                if self._error is not None and not self._deque:
                    if result:
                        break  # hand back the tail, raise on the next read
                    raise IOError(f"Stream fetch error: {self._error}") from self._error
                if self._stop.is_set() and not self._deque:
                    break
                while self._deque and len(result) < num_bytes:
                    chunk = self._deque.popleft()
                    self._deque_bytes -= len(chunk)
                    need = num_bytes - len(result)
                    if len(chunk) <= need:
                        result.extend(chunk)
                    else:
                        result.extend(chunk[:need])
                        leftover = chunk[need:]
                        self._deque.appendleft(leftover)
                        self._deque_bytes += len(leftover)
        return bytes(result)

    def close(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# Voice Activity Detection
# ---------------------------------------------------------------------------

class VoiceActivityDetector:
    """RMS gate with an adaptive noise floor.

    The configured threshold is a floor, not a ceiling: if a feed turns hissy
    mid-event its own noise would otherwise sit above the gate and hold it
    permanently open, turning the whole stream into one endless "transmission".
    Tracking a rolling low percentile of recent frames and requiring speech to
    sit a multiple above it keeps the gate honest on a degraded feed.
    """

    def __init__(self, threshold: float = VAD_RMS_THRESHOLD, adaptive: bool = VAD_ADAPTIVE):
        self.threshold = threshold
        self.adaptive = adaptive
        self._recent: collections.deque = collections.deque(maxlen=VAD_NOISE_WINDOW_FRAMES)
        self._floor = 0.0
        self._since_recompute = 0

    def set_base_threshold(self, threshold: float) -> None:
        self.threshold = threshold

    @property
    def effective_threshold(self) -> float:
        if not self.adaptive:
            return self.threshold
        return max(self.threshold, self._floor * VAD_NOISE_FLOOR_MULT)

    def is_speech(self, chunk: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        speech = rms > self.effective_threshold
        if self.adaptive and not speech:
            # Only non-speech frames inform the noise floor, so a long
            # transmission cannot drag the floor up after itself.
            self._recent.append(rms)
            self._since_recompute += 1
            if self._since_recompute >= 100 and len(self._recent) >= 50:
                self._floor = float(np.percentile(np.fromiter(self._recent, dtype=np.float32), 20))
                self._since_recompute = 0
        return speech


class AudioPreprocessor:
    """Bandpass filter (300–3400 Hz) + squelch click trim + amplitude normalisation for VHF radio audio."""

    _SR = VAD_SAMPLE_RATE
    # VHF squelch opens/closes with a brief noise click; trim those edges before Whisper sees the audio.
    _LEAD_SAMPLES = int(_SR * 0.080)   # 80 ms — squelch open click
    _TAIL_SAMPLES = int(_SR * 0.050)   # 50 ms — squelch close click

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._sos = butter(4, [300 / 8000.0, 3400 / 8000.0], btype='bandpass', output='sos')

    def process(self, audio: np.ndarray, preroll_samples: int = 0) -> np.ndarray:
        """`preroll_samples` is how much of `audio` precedes the VAD trigger.

        The lead trim is only allowed to eat into that pre-roll. Without a
        pre-roll to spend it would take 80 ms off the start of the transmission
        itself, which is where the callsign lives.
        """
        if not self.enabled or len(audio) == 0:
            return audio
        filtered = sosfilt(self._sos, audio).astype(np.float32)
        lead = min(self._LEAD_SAMPLES, max(0, preroll_samples))
        trim = lead + self._TAIL_SAMPLES
        if len(filtered) > trim:
            filtered = filtered[lead: len(filtered) - self._TAIL_SAMPLES]
        peak = float(np.percentile(np.abs(filtered), 95))
        if peak > 1e-6:
            filtered = (filtered * (0.7 / peak)).clip(-1.0, 1.0).astype(np.float32)
        return filtered


_preprocessor = AudioPreprocessor(enabled=AUDIO_PREPROCESSING)


# ---------------------------------------------------------------------------
# Transmission audio buffer
# ---------------------------------------------------------------------------

class TransmissionBuffer:
    """Accumulates a transmission, with a rolling pre-roll of the audio just
    before it started. VHF speech begins at the same instant the gate opens,
    so without a pre-roll the first syllable of the callsign is already gone by
    the time VAD has decided this is speech."""

    def __init__(self, sample_rate: int = VAD_SAMPLE_RATE, preroll_sec: float = VAD_PREROLL_SEC):
        self._chunks: list = []
        self._sample_rate = sample_rate
        preroll_chunks = max(1, int((preroll_sec * sample_rate) / VAD_CHUNK_FRAMES))
        self._preroll: collections.deque = collections.deque(maxlen=preroll_chunks)
        self._preroll_samples = 0

    def observe_silence(self, chunk: np.ndarray):
        """Feed a non-speech chunk into the pre-roll ring (dropped once the
        transmission ends, kept as lead-in when one starts)."""
        if not self._chunks:
            self._preroll.append(chunk)

    def append(self, chunk: np.ndarray):
        if not self._chunks and self._preroll:
            self._chunks.extend(self._preroll)
            self._preroll_samples = sum(len(c) for c in self._preroll)
            self._preroll.clear()
        self._chunks.append(chunk)

    def flush(self) -> tuple[np.ndarray, int]:
        """Returns (audio, preroll_samples)."""
        if not self._chunks:
            return np.array([], dtype=np.float32), 0
        audio = np.concatenate(self._chunks)
        preroll = self._preroll_samples
        self._chunks = []
        self._preroll_samples = 0
        return audio, preroll

    @property
    def duration_seconds(self) -> float:
        return sum(len(c) for c in self._chunks) / self._sample_rate


# ---------------------------------------------------------------------------
# Transcriber thread
# ---------------------------------------------------------------------------

class Transcriber:
    _MIN_DURATION = 0.7
    # 95th-percentile amplitude of the RAW audio; below this the clip is squelch
    # noise that tripped VAD rather than speech. Sits just above the 0.003 RMS
    # gate so it rejects only marginal trips.
    _MIN_PEAK = 0.005

    def __init__(
        self,
        backend: transcription.TranscriptionBackend,
        state: SharedState,
        log_file: Optional[Path] = None,
        recordings_dir: Optional[Path] = None,
    ):
        self._backend = backend
        self._state = state
        self._log_file = log_file
        self._recordings_dir = recordings_dir
        self._queue: queue.Queue = queue.Queue(maxsize=24)
        self._recent: dict = {}  # icao -> (text, monotonic_time) for dedup
        self._prompts = {s["icao"]: s.get("prompt", "") for s in STREAMS}
        self.gated = 0      # transmissions rejected by the quality gate
        self._thread = threading.Thread(target=self._worker, daemon=True, name="transcriber")
        self._thread.start()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def submit(
        self,
        audio: np.ndarray,
        raw_audio: np.ndarray,
        duration: float,
        icao: str,
        station_name: str,
        ts: str,
        ts_iso: str,
    ) -> bool:
        if duration < self._MIN_DURATION:
            return False
        # Checked against the raw audio, not the processed audio: the
        # preprocessor normalises the 95th percentile to 0.7, so this test
        # would never fire on its output.
        if len(raw_audio) and float(np.percentile(np.abs(raw_audio), 95)) < self._MIN_PEAK:
            return False
        try:
            self._queue.put_nowait((audio, raw_audio, duration, icao, station_name, ts, ts_iso))
            return True
        except queue.Full:
            self._state.health[icao].dropped += 1
            self._tui_log(Text(f"⚠ [{icao}] Transcriber busy — dropping TX", style="yellow"))
            return False

    def _tui_log(self, line: Text) -> None:
        if self._state.display:
            self._state.display.log(line)

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            audio, raw_audio, duration, icao, station_name, ts, ts_iso = item
            try:
                segments = self._backend.transcribe(audio, prompt=self._prompts.get(icao, ""))
                text = _apply_quality_gate(segments)
                if text:
                    self._log(text, icao, station_name, ts, ts_iso, duration, raw_audio)
                elif segments:
                    self.gated += 1
            except Exception as exc:
                msg = str(exc)
                if "401" in msg or "authentication" in msg.lower() or "username or password" in msg.lower():
                    self._tui_log(Text(
                        "⚠ HuggingFace auth failed — check HUGGINGFACE_TOKEN in .env",
                        style="red",
                    ))
                else:
                    self._tui_log(Text(f"⚠ Transcription error [{icao}]: {exc}", style="red"))
            finally:
                self._queue.task_done()

    def _save_recording(self, raw_audio: np.ndarray, icao: str) -> Optional[Path]:
        """Writes the pre-preprocessing audio, so recordings stay a faithful
        source for re-transcription and for bench_stt.py."""
        if not self._recordings_dir or not self._state.recording_enabled or not len(raw_audio):
            return None
        try:
            now = datetime.now(timezone.utc).astimezone(_AEST)
            day_dir = self._recordings_dir / now.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            path = day_dir / f"{icao}_{now.strftime('%H%M%S')}.wav"
            pcm = (np.clip(raw_audio, -1.0, 1.0) * 32767).astype(np.int16)
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(VAD_SAMPLE_RATE)
                wf.writeframes(pcm.tobytes())
            return path
        except Exception as exc:
            self._tui_log(Text(f"⚠ Recording failed [{icao}]: {exc}", style="dim yellow"))
            return None

    def _log(
        self,
        text: str,
        icao: str,
        station_name: str,
        ts: str,
        ts_iso: str,
        duration: float = 0.0,
        raw_audio: Optional[np.ndarray] = None,
    ):
        text = _apply_atc_corrections(text)
        if _is_hallucination(text, prompt=self._prompts.get(icao, "")):
            self.gated += 1
            return
        now = time.monotonic()
        prev_text, prev_time = self._recent.get(icao, ("", 0.0))
        if text == prev_text and now - prev_time < 20.0:
            return
        self._recent[icao] = (text, now)
        self._state.health[icao].last_tx_at = time.time()

        recording = self._save_recording(raw_audio if raw_audio is not None else np.array([]), icao)
        rec_name = recording.name if recording else ""

        military = _military_matches(text, self._state)
        highlight = KEYWORDS + [m.matched_text for m in military]

        dur_str = f"({duration:.1f}s) " if duration > 0 else ""
        line = Text()
        line.append(f"[{ts}] {icao} {station_name:<16} {dur_str}│ ", style="bold green")
        line.append_text(_highlight_keywords(text, highlight, enabled=self._state.keywords_enabled))
        self._tui_log(line)
        if military:
            self._tui_log(Text(
                f"{'':>{len(ts) + 2}} \U0001f6e9 {_military_summary(military)}",
                style="bold magenta" if any(m.strong for m in military) else "dim magenta",
            ))

        tier = _keyword_tier(text)
        if any(m.strong for m in military):
            tier = max(tier, TIER_INTEREST)
        _send_telegram(text, ts, icao, station_name, tier, military)
        _send_discord(text, ts, ts_iso, icao, station_name, tier, rec_name, military)
        _transcript_feed.add({
            "at": time.time(),
            "ts": ts,
            "icao": icao,
            "station": station_name,
            "text": text,
            "tier": tier,
            "military": [m.label for m in military if m.strong],
        })
        if self._log_file:
            try:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(f"{ts} | {icao} | {station_name} | {text} | {rec_name}\n")
            except Exception:
                pass

    def shutdown(self):
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------

# Matches any 1–4 char sequence repeated 8+ times (e.g. "9.9.9.9.9.9.9.9.9")
_REPETITION_RE = re.compile(r'(.{1,4})\1{7,}')

# Short filler phrases Whisper emits on silence/noise. Whisper's training data
# was full of subtitle files, so it falls back to sign-off lines when there is
# nothing to hear.
_WHISPER_FILLERS = frozenset({
    "you", ".", "", "thank you", "thanks", "thanks for watching",
    "thank you for watching", "bye", "bye bye", "okay", "ok", "yeah",
    "please subscribe", "subtitles by the amara.org community", "the end",
})

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalise_words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _has_repeated_ngram(
    words: list[str],
    max_n: int = REPEAT_NGRAM_MAX_WORDS,
    threshold: int = REPEAT_NGRAM_THRESHOLD,
) -> bool:
    """True if any 1..max_n word phrase repeats back-to-back `threshold` times.

    The old character-level filter only caught 1–4 character loops, so real
    failures like "North Carolina, " x70 and "turning base, " x12 went straight
    through to Discord — 8.5% of the historical log lines.
    """
    n_words = len(words)
    for n in range(1, max_n + 1):
        if n * threshold > n_words:
            break
        for i in range(n_words - n * threshold + 1):
            phrase = words[i:i + n]
            if all(words[i + k * n: i + (k + 1) * n] == phrase for k in range(1, threshold)):
                return True
    return False


def _ngrams(words: list[str], n: int = 5) -> set:
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _is_prompt_echo(text: str, prompt: str) -> bool:
    """True when the output is mostly the station's own prompt read back.

    Whisper reproduces its initial_prompt verbatim on low-content audio, and
    does so at high confidence (an observed echo scored avg_logprob -0.152), so
    no confidence threshold catches this — it has to be compared to the prompt.
    """
    if not prompt:
        return False
    words = _normalise_words(text)
    if len(words) < 5:
        return False
    text_grams = _ngrams(words)
    if not text_grams:
        return False
    overlap = len(text_grams & _prompt_grams(prompt)) / len(text_grams)
    return overlap > PROMPT_ECHO_MAX_OVERLAP


@functools.lru_cache(maxsize=32)
def _prompt_grams(prompt: str) -> frozenset:
    return frozenset(_ngrams(_normalise_words(prompt)))


def _is_hallucination(text: str, prompt: str = "") -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if _REPETITION_RE.search(stripped):
        return True
    if stripped.rstrip(".!?").lower() in _WHISPER_FILLERS:
        return True
    words = _normalise_words(stripped)
    if not words:
        return True
    if _has_repeated_ngram(words):
        return True
    if _is_prompt_echo(stripped, prompt):
        return True
    return False


def _apply_quality_gate(segments: list) -> str:
    """Drops segments the model itself flagged as low quality, then joins.

    mlx-whisper computes avg_logprob / no_speech_prob / compression_ratio but
    never rejects on them: compression_ratio_threshold only triggers a retry at
    a higher temperature, and the last result is kept regardless. A prompt-echo
    loop was measured at compression_ratio 14.54 against a sane ceiling of 2.4,
    so the rejection has to happen here.
    """
    kept: list[str] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        if seg.compression_ratio is not None and seg.compression_ratio > MAX_COMPRESSION_RATIO:
            continue
        if seg.avg_logprob is not None and seg.avg_logprob < MIN_AVG_LOGPROB:
            continue
        if (
            seg.no_speech_prob is not None
            and seg.no_speech_prob > MAX_NO_SPEECH_PROB
            and seg.avg_logprob is not None
            and seg.avg_logprob < WEAK_AVG_LOGPROB
        ):
            continue
        kept.append(text)
    return " ".join(kept).strip()


# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def _kw_pattern(kw: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)


@functools.lru_cache(maxsize=8)
def _kw_union(keywords: tuple) -> re.Pattern:
    alternatives = "|".join(re.escape(kw) for kw in sorted(keywords, key=len, reverse=True))
    return re.compile(r"\b(?:" + alternatives + r")\b", re.IGNORECASE)


def _has_keywords(text: str, keywords: list) -> bool:
    return bool(keywords) and _kw_union(tuple(keywords)).search(text) is not None


def _matched_keywords(text: str, keywords: list) -> list[str]:
    return [kw for kw in keywords if _kw_pattern(kw).search(text)]


# Alert tiers. EMERGENCY pings @here; INTEREST posts to #alerts silently;
# NONE goes only to the station's own channel.
TIER_NONE = 0
TIER_INTEREST = 1
TIER_EMERGENCY = 2


def _keyword_tier(text: str) -> int:
    if _has_keywords(text, KEYWORDS_EMERGENCY):
        return TIER_EMERGENCY
    if _has_keywords(text, KEYWORDS_INTEREST):
        return TIER_INTEREST
    return TIER_NONE


# ---------------------------------------------------------------------------
# Military callsign detection (see military_callsigns.py)
#
# A strong hit — an exact callsign, or a misheard one corroborated by a flight
# number and military context — raises the transmission to TIER_INTEREST even
# when no keyword fired. A weak hit is annotated on the station's own message
# but never escalated: the registry holds 400+ words and Whisper mangles enough
# of them that alerting on every near-miss would bury the real ones.
# ---------------------------------------------------------------------------

def _military_matches(text: str, state: Optional[SharedState] = None) -> list:
    if state is not None and not state.military_enabled:
        return []
    return military_callsigns.detect(text)


def _military_summary(matches: list, strong_only: bool = False) -> str:
    picked = [m for m in matches if m.strong or not strong_only]
    return " · ".join(m.describe() for m in picked)


_ATC_CORRECTIONS = [(re.compile(p, re.IGNORECASE), r) for p, r in ATC_CORRECTIONS]


def _apply_atc_corrections(text: str) -> str:
    for pattern, replacement in _ATC_CORRECTIONS:
        text = pattern.sub(replacement, text)
    return text


def _highlight_keywords(text: str, keywords: list, enabled: bool) -> Text:
    if not enabled:
        return Text(text)

    spans: list = []
    for kw in keywords:
        for m in _kw_pattern(kw).finditer(text):
            spans.append((m.start(), m.end()))

    if not spans:
        return Text(text)

    spans.sort()
    merged: list = []
    for s, e in spans:
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    result = Text()
    pos = 0
    for s, e in merged:
        if pos < s:
            result.append(text[pos:s])
        result.append(text[s:e], style="bold bright_red")
        pos = e
    if pos < len(text):
        result.append(text[pos:])
    return result


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

# One pooled session for Discord and Telegram: keep-alive skips a TLS handshake
# on every post.
_http = requests.Session()

# Telegram is sent from its own thread so a slow API never stalls the
# transcriber or the ADS-B poller, which both used to block on it.
_telegram_queue: queue.Queue = queue.Queue(maxsize=100)
_telegram_thread: Optional[threading.Thread] = None
_telegram_thread_lock = threading.Lock()


def _telegram_worker() -> None:
    while True:
        message = _telegram_queue.get()
        try:
            _send_telegram_now(message)
        finally:
            _telegram_queue.task_done()


def _post_telegram(message: str) -> None:
    global _telegram_thread
    if not config.TELEGRAM_ENABLED or not _telegram_active:
        return
    with _telegram_thread_lock:
        if _telegram_thread is None:
            _telegram_thread = threading.Thread(
                target=_telegram_worker, daemon=True, name="telegram-outbox"
            )
            _telegram_thread.start()
    try:
        _telegram_queue.put_nowait(message)
    except queue.Full:
        if _display_ref is not None:
            _display_ref.log(Text("⚠ Telegram outbox full, dropping message", style="dim yellow"))


def _send_telegram_now(message: str) -> None:
    try:
        _http.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as exc:
        if _display_ref is not None:
            _display_ref.log(Text(f"⚠ Telegram send failed: {exc}", style="dim yellow"))


def _send_telegram(
    text: str,
    ts: str,
    icao: str,
    station_name: str,
    tier: int,
    military: Optional[list] = None,
) -> None:
    label = f"{icao} {station_name}"
    if tier == TIER_EMERGENCY:
        header = f"\U0001f6a8 <b>[EMERGENCY]</b> {label}"
    elif tier == TIER_INTEREST:
        header = f"\U0001f534 <b>[ALERT]</b> {label}"
    else:
        header = f"\U0001f4fb {label}"
    body = f"{header}\n<code>[{ts}]</code> {html.escape(text)}"
    if military:
        body += f"\n\U0001f6e9 <b>{html.escape(_military_summary(military))}</b>"
    _post_telegram(body)


def _send_startup_notification(state: SharedState) -> None:
    ts = _now_ts()
    lines = [f"\U0001f7e2 <b>ATC Tracker started</b>  <code>[{ts}]</code>", ""]
    for s in STREAMS:
        enabled = state.is_enabled(s["icao"])
        icon = "✅" if enabled else "\U0001f507"
        lines.append(f"{icon} {s['icao']} {s['name']}")
    _post_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# Discord helpers (dual-send alongside Telegram — see DISCORD.md)
# ---------------------------------------------------------------------------

_DISCORD_API = "https://discord.com/api/v10"


class DiscordOutbox:
    """Background sender so Discord can never stall an audio or decode thread.

    Everything the tracker sends goes through here. A connect/disconnect used to
    do up to three blocking HTTP calls from inside _stream_loop, during which
    LiveATCSource silently discards the oldest buffered MP3 bytes and corrupts
    the decoder.
    """

    def __init__(self, maxsize: int = 200):
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="discord-outbox")
        self._thread.start()

    def submit(
        self,
        channel_id: str,
        content: Optional[str] = None,
        embed: Optional[dict] = None,
        priority: bool = False,
        message_id: Optional[str] = None,
        allowed_mentions: Optional[dict] = None,
    ) -> None:
        if not config.DISCORD_ENABLED or not channel_id:
            return
        item = (channel_id, content, dict(embed) if embed else None, message_id, allowed_mentions)
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            if priority:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self._queue.put_nowait(item)
                    return
                except Exception:
                    pass
            if _display_ref is not None:
                _display_ref.log(Text("⚠ Discord outbox full — dropping transcript post", style="dim yellow"))

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            channel_id, content, embed, message_id, allowed_mentions = item
            try:
                if message_id:
                    _edit_discord(channel_id, message_id, content=content, embed=embed)
                else:
                    _post_discord(
                        channel_id, content=content, embed=embed,
                        allowed_mentions=allowed_mentions,
                    )
            finally:
                self._queue.task_done()
            # Discord allows ~5 messages / 5s per channel; a small gap keeps a
            # busy airshow feed from spending its time in 429 retries.
            self._stop.wait(0.25)

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=5)


_discord_outbox: Optional[DiscordOutbox] = None
# Set in main(). Module-level so _handle_command can reach it, the same way it
# reaches _transcriber_ref and _discord_outbox.
_adsb_poller: Optional["adsb_tracker.AdsbPoller"] = None


def _discord_channel_for(icao: str) -> str:
    for s in STREAMS:
        if s["icao"] == icao:
            return s.get("discord_channel_id", "")
    return ""


def _discord_payload(
    content: Optional[str],
    embed: Optional[dict],
    allowed_mentions: Optional[dict] = None,
) -> dict:
    payload: dict = {}
    if content:
        payload["content"] = content[:2000]
    if embed:
        embed["title"] = embed.get("title", "")[:256]
        if embed.get("description"):
            embed["description"] = embed["description"][:4096]
        payload["embeds"] = [embed]
    if payload:
        # Explicit by default: without this an embed containing "@everyone" in
        # transcribed audio could ping the server.
        payload["allowed_mentions"] = allowed_mentions or {"parse": []}
    return payload


def _discord_request(method: str, url: str, payload: dict, what: str) -> Optional[dict]:
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    try:
        resp = _http.request(method, url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 429:
            retry_after = 1.0
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                pass
            time.sleep(min(retry_after, 5.0) + 0.05)
            resp = _http.request(method, url, json=payload, headers=headers, timeout=10)
        if resp.status_code >= 400:
            if _display_ref is not None:
                _display_ref.log(Text(
                    f"⚠ Discord {what} failed: HTTP {resp.status_code} {resp.text[:200]}",
                    style="dim yellow",
                ))
            return None
        return resp.json()
    except Exception as exc:
        if _display_ref is not None:
            _display_ref.log(Text(f"⚠ Discord {what} failed: {exc}", style="dim yellow"))
        return None


def _post_discord(
    channel_id: str,
    content: Optional[str] = None,
    embed: Optional[dict] = None,
    allowed_mentions: Optional[dict] = None,
) -> Optional[str]:
    if not config.DISCORD_ENABLED or not channel_id:
        return None
    payload = _discord_payload(content, embed, allowed_mentions)
    if not payload:
        return None
    data = _discord_request(
        "POST", f"{_DISCORD_API}/channels/{channel_id}/messages", payload,
        f"send [{channel_id}]",
    )
    return (data or {}).get("id")


def _edit_discord(
    channel_id: str,
    message_id: str,
    content: Optional[str] = None,
    embed: Optional[dict] = None,
) -> bool:
    if not config.DISCORD_ENABLED or not channel_id or not message_id:
        return False
    payload = _discord_payload(content, embed)
    if not payload:
        return False
    data = _discord_request(
        "PATCH", f"{_DISCORD_API}/channels/{channel_id}/messages/{message_id}", payload,
        f"edit [{channel_id}]",
    )
    return data is not None


def _enqueue_discord(
    channel_id: str,
    content: Optional[str] = None,
    embed: Optional[dict] = None,
    priority: bool = False,
    message_id: Optional[str] = None,
    allowed_mentions: Optional[dict] = None,
) -> None:
    if _discord_outbox is not None:
        _discord_outbox.submit(
            channel_id, content=content, embed=embed, priority=priority,
            message_id=message_id, allowed_mentions=allowed_mentions,
        )
    elif message_id:
        _edit_discord(channel_id, message_id, content=content, embed=embed)
    else:
        _post_discord(channel_id, content=content, embed=embed, allowed_mentions=allowed_mentions)


def _pin_discord(channel_id: str, message_id: str) -> None:
    if not config.DISCORD_ENABLED or not channel_id or not message_id:
        return
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    try:
        resp = _http.put(
            f"{_DISCORD_API}/channels/{channel_id}/pins/{message_id}",
            headers=headers, timeout=10,
        )
        if resp.status_code >= 400 and _display_ref is not None:
            _display_ref.log(Text(
                f"⚠ Discord pin failed [{channel_id}]: HTTP {resp.status_code} {resp.text[:200]}",
                style="dim yellow",
            ))
    except Exception as exc:
        if _display_ref is not None:
            _display_ref.log(Text(f"⚠ Discord pin failed: {exc}", style="dim yellow"))


_TIER_COLOR = {
    TIER_NONE: 0x2ECC71,        # green
    TIER_INTEREST: 0xE67E22,    # orange
    TIER_EMERGENCY: 0xE74C3C,   # red
}


def _send_discord(
    text: str,
    ts: str,
    ts_iso: str,
    icao: str,
    station_name: str,
    tier: int,
    recording: str = "",
    military: Optional[list] = None,
) -> None:
    if tier == TIER_EMERGENCY:
        title = f"\U0001f6a8 {icao} {station_name}"
    elif tier == TIER_INTEREST:
        title = f"\U0001f534 {icao} {station_name}"
    else:
        title = f"\U0001f4fb {icao} {station_name}"
    # "timestamp" drives Discord's own localized clock display in the embed —
    # set to when the transmission was received (ts_iso), not when this POST
    # happens, since transcription can lag behind receipt by several seconds.
    footer = f"{ts} · {recording}" if recording else ts
    embed = {
        "title": title,
        "description": text,
        "color": _TIER_COLOR[tier],
        "footer": {"text": footer},
        "timestamp": ts_iso,
    }
    if military:
        strong = [m for m in military if m.strong]
        embed["fields"] = [{
            "name": "\U0001f6e9 Military callsign" if strong else "\U0001f6e9 Possible military callsign",
            "value": _military_summary(military)[:1000],
            "inline": False,
        }]
    channel_id = _discord_channel_for(icao)
    if channel_id:
        _enqueue_discord(channel_id, embed=dict(embed))
    if tier == TIER_NONE or not DISCORD_ALERTS_CHANNEL_ID:
        return

    alert_embed = dict(embed)
    if tier == TIER_EMERGENCY:
        matched = ", ".join(_matched_keywords(text, KEYWORDS_EMERGENCY)) or "emergency"
        alert_embed["title"] = f"\U0001f6a8 EMERGENCY — {icao} {station_name}"
        _enqueue_discord(
            DISCORD_ALERTS_CHANNEL_ID,
            content=f"@here **{matched.upper()}** on {icao} {station_name}",
            embed=alert_embed,
            priority=True,
            allowed_mentions={"parse": ["everyone"]},
        )
    else:
        labels = _matched_keywords(text, KEYWORDS_INTEREST)
        labels += [m.label for m in (military or []) if m.strong]
        matched = ", ".join(dict.fromkeys(labels))
        alert_embed["title"] = f"\U0001f534 {matched or 'ALERT'} — {icao} {station_name}"
        _enqueue_discord(DISCORD_ALERTS_CHANNEL_ID, embed=alert_embed, priority=True)


def _send_discord_startup(state: SharedState, backend_name: str = "") -> None:
    lines = ["\U0001f7e2 **ATC Tracker started**", ""]
    for s in STREAMS:
        icon = "✅" if state.is_enabled(s["icao"]) else "\U0001f507"
        lines.append(f"{icon} {s['icao']} {s['name']}")
    if backend_name:
        lines.append(f"\nSTT backend: `{backend_name}`")
    lines.append("Send `/help` for commands.")
    _enqueue_discord(DISCORD_COMMANDS_CHANNEL_ID, content="\n".join(lines))


def _html_to_discord_md(html_text: str) -> str:
    """Convert the small HTML subset Telegram replies use into Discord markdown."""
    out = html_text.replace("<b>", "**").replace("</b>", "**")
    out = out.replace("<code>", "`").replace("</code>", "`")
    # <pre> carries the aircraft tables, which only line up in a monospaced
    # block. Telegram renders the tag directly; Discord needs a fence.
    out = out.replace("<pre>", "```\n").replace("</pre>", "\n```")
    return html.unescape(out)


def _discord_respond(msg: str) -> None:
    _post_discord(DISCORD_COMMANDS_CHANNEL_ID, content=_html_to_discord_md(msg))


def _broadcast_station_status(station: dict, enabled: bool) -> None:
    if not config.DISCORD_ENABLED:
        return
    channel_id = station.get("discord_channel_id", "")
    if not channel_id:
        return
    icon = "✅" if enabled else "\U0001f507"
    label = "Active" if enabled else "Muted"
    embed = {
        "title": f"{icon} {station['icao']} {station['name']} — {label}",
        "color": 0x2ECC71 if enabled else 0x95A5A6,
        "footer": {"text": _now_ts()},
    }
    _enqueue_discord(channel_id, embed=embed)


def _broadcast_pause_status(paused: bool) -> None:
    if not config.DISCORD_ENABLED:
        return
    icon = "⏸" if paused else "▶️"
    label = "Paused" if paused else "Resumed"
    detail = "Transcription & forwarding stopped" if paused else "Transcription active"
    embed = {
        "title": f"{icon} ATC Tracker {label}",
        "description": detail,
        "color": 0xF1C40F if paused else 0x2ECC71,
        "footer": {"text": _now_ts()},
    }
    for s in STREAMS:
        channel_id = s.get("discord_channel_id", "")
        if channel_id:
            _enqueue_discord(channel_id, embed=dict(embed))


# One long-lived status message per station channel, created and pinned once at
# startup then edited in place. The previous design posted and pinned a fresh
# message on every connect/disconnect, which spams the channel and walks into
# Discord's hard cap of 50 pins per channel over a long event.
_stream_status_message: dict = {}  # channel_id -> message_id


_STATUS_TITLE_MARKER = "— Connected"
_STATUS_TITLE_MARKERS = (_STATUS_TITLE_MARKER, "— Disconnected", "— Starting…")


def _find_existing_status_message(
    channel_id: str, markers: tuple = _STATUS_TITLE_MARKERS
) -> Optional[str]:
    """Looks for a status message this bot already pinned in the channel.

    Reusing it across restarts matters: creating a fresh one every startup would
    still march towards Discord's 50-pin ceiling over a weekend of restarts,
    just more slowly than the old post-per-transition behaviour did.

    ``markers`` selects which kind of pinned message to match, so the ADS-B
    board can reuse the same discovery logic without colliding with the
    per-station stream status.
    """
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    for path in ("pins/messages", "pins"):
        try:
            resp = requests.get(f"{_DISCORD_API}/channels/{channel_id}/{path}",
                                headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", [])
            for item in items:
                msg = item.get("message", item) if isinstance(item, dict) else {}
                if not msg.get("author", {}).get("bot"):
                    continue
                for embed in msg.get("embeds", []):
                    title = embed.get("title") or ""
                    if any(marker in title for marker in markers):
                        return msg.get("id")
            return None
        except Exception:
            continue
    return None


def _init_stream_status_messages(state: SharedState) -> None:
    """Establishes the per-station pinned status message, reusing the one from a
    previous run when there is one. Runs at startup on the main thread, where
    blocking on HTTP is harmless."""
    if not config.DISCORD_ENABLED:
        return
    for station in STREAMS:
        channel_id = station.get("discord_channel_id", "")
        if not channel_id:
            continue
        embed = {
            "title": f"⚪ {station['icao']} {station['name']} — Starting…",
            "color": 0x95A5A6,
            "footer": {"text": _now_ts()},
        }
        existing = _find_existing_status_message(channel_id)
        if existing and _edit_discord(channel_id, existing, embed=embed):
            _stream_status_message[channel_id] = existing
            continue
        message_id = _post_discord(channel_id, embed=embed)
        if message_id:
            _stream_status_message[channel_id] = message_id
            _pin_discord(channel_id, message_id)


def _broadcast_stream_status(station: dict, connected: bool, detail: str = "") -> None:
    """Updates the station's pinned status message. Called on state transitions
    only (not on every reconnect attempt), so a persistently down feed does not
    generate traffic while it retries."""
    if not config.DISCORD_ENABLED:
        return
    channel_id = station.get("discord_channel_id", "")
    if not channel_id:
        return
    if connected:
        embed = {
            "title": f"\U0001f7e2 {station['icao']} {station['name']} — Connected",
            "color": 0x2ECC71,
            "footer": {"text": _now_ts()},
        }
    else:
        embed = {
            "title": f"\U0001f534 {station['icao']} {station['name']} — Disconnected",
            "description": f"{detail[:200]}\nReconnecting…" if detail else "Reconnecting…",
            "color": 0xE74C3C,
            "footer": {"text": _now_ts()},
        }
    message_id = _stream_status_message.get(channel_id)
    if message_id:
        _enqueue_discord(channel_id, embed=embed, message_id=message_id)
    else:
        # No pinned message (startup failed or Discord came up late) — fall back
        # to a normal post rather than losing the status entirely.
        _enqueue_discord(channel_id, embed=embed)


# ---------------------------------------------------------------------------
# Live ADS-B tracking → Discord
#
# The poller in adsb_tracker knows nothing about Discord; it hands TrackEvents
# to the callback wired up in main(). Everything that turns one of those into a
# message lives here, alongside the transcript formatters it shares conventions
# with (_TIER_COLOR, _now_ts, _now_iso, the outbox).
#
# adsb.fi's terms require attribution. It goes in the board description as a
# real link (Discord does not render links in footers) and in every event
# embed's footer, so it cannot be lost by editing one of them out.
# ---------------------------------------------------------------------------

_ADSB_ATTRIBUTION = "Data: [adsb.fi](https://adsb.fi) · [globe](https://globe.adsbexchange.com/)"

_adsb_board_message: dict = {}  # channel_id -> message_id
_ADSB_BOARD_MARKERS = ("Military & display", "Gold Coast air picture")

_ADSB_EVENT_ICON = {
    adsb_tracker.EV_APPEARED: "\U0001f4e1",       # satellite antenna
    adsb_tracker.EV_DISAPPEARED: "\U0001f4f4",    # phone off
    adsb_tracker.EV_BOX_ENTER: "\U0001f3af",      # target
    adsb_tracker.EV_BOX_EXIT: "↗️",
    adsb_tracker.EV_DEPARTURE: "\U0001f6eb",
    adsb_tracker.EV_INBOUND: "\U0001f6ec",
    adsb_tracker.EV_LANDED: "\U0001f6ec",
    adsb_tracker.EV_EMERGENCY: "\U0001f6a8",
    adsb_tracker.EV_POSITION: "\U0001f6e9️",
}


def _adsb_event_embed(ev) -> dict:
    track = ev.track
    icon = _ADSB_EVENT_ICON.get(ev.kind, "✈️")
    lines = []
    if track.classification.title:
        lines.append(f"**{track.classification.title}**")
    if ev.detail:
        lines.append(ev.detail)
    lines.append(f"[Track on globe.adsbexchange.com]({track.globe_url()})")

    embed = {
        "title": f"{icon} {ev.headline}"[:256],
        "description": "\n".join(lines),
        "color": _TIER_COLOR[ev.tier],
        "footer": {"text": f"{_now_ts()} · {track.hex.upper()} · data adsb.fi"},
        "timestamp": _now_iso(),
    }
    fields = []
    if track.reg:
        fields.append({"name": "Registration", "value": track.reg, "inline": True})
    if track.typ:
        fields.append({"name": "Type", "value": track.typ, "inline": True})
    if track.squawk:
        fields.append({"name": "Squawk", "value": track.squawk, "inline": True})
    reasons = track.classification.reason_text()
    if reasons:
        # Why this was flagged. Shown on every alert on purpose: a bad
        # classifier rule is then obvious from the message itself rather than
        # something you have to go digging in the code to work out.
        fields.append({"name": "Matched on", "value": reasons[:1024], "inline": False})
    if fields:
        embed["fields"] = fields[:6]
    return embed


def _adsb_channels_for(ev) -> list:
    """Which channels this event belongs in."""
    channels = []
    if ev.military or ev.track.classification.interesting:
        if config.DISCORD_CHANNEL_MILITARY:
            channels.append(config.DISCORD_CHANNEL_MILITARY)
    elif config.DISCORD_CHANNEL_FLIGHTS:
        channels.append(config.DISCORD_CHANNEL_FLIGHTS)
    # Aerodrome movements belong in the flights channel even for military
    # aircraft — that is the channel someone watches to see what is using YBCG.
    if ev.kind in (
        adsb_tracker.EV_DEPARTURE,
        adsb_tracker.EV_INBOUND,
        adsb_tracker.EV_LANDED,
    ) and config.DISCORD_CHANNEL_FLIGHTS:
        if config.DISCORD_CHANNEL_FLIGHTS not in channels:
            channels.append(config.DISCORD_CHANNEL_FLIGHTS)
    return channels


def _send_adsb_event(ev, state: SharedState) -> None:
    """Route one TrackEvent to Discord (and Telegram for the loud ones)."""
    if state.paused:
        return
    embed = _adsb_event_embed(ev)
    emergency = ev.tier >= TIER_EMERGENCY

    # The live-card view already carries these — a fresh post per poll is
    # exactly the position spam we are getting rid of, so route them to the
    # cards only. Emergencies, box entries and aerodrome movements are discrete
    # and still worth their own message.
    cards_on = config.DISCORD_ENABLED and config.ADSB_LIVE_CARDS_ENABLED
    card_covered = ev.kind in (
        adsb_tracker.EV_APPEARED,
        adsb_tracker.EV_POSITION,
        adsb_tracker.EV_DISAPPEARED,
        adsb_tracker.EV_BOX_EXIT,
    )
    if not (cards_on and card_covered):
        for channel_id in _adsb_channels_for(ev):
            _enqueue_discord(channel_id, embed=dict(embed), priority=emergency)

    if emergency and config.DISCORD_ALERTS_CHANNEL_ID:
        _enqueue_discord(
            config.DISCORD_ALERTS_CHANNEL_ID,
            content=f"@here **{ev.headline}**",
            embed=dict(embed),
            priority=True,
            allowed_mentions={"parse": ["everyone"]},
        )

    # Telegram mirrors only the things worth a phone buzz. A transponder drop
    # over water flaps, so it is deliberately not one of them.
    if config.TELEGRAM_ENABLED and (
        emergency
        or ev.kind == adsb_tracker.EV_BOX_ENTER
        or (ev.military and ev.kind == adsb_tracker.EV_APPEARED)
    ):
        detail = f"\n{html.escape(ev.detail)}" if ev.detail else ""
        _post_telegram(f"<b>{html.escape(ev.headline)}</b>{detail}")

    if _display_ref is not None:
        style = "bold red" if emergency else ("bold cyan" if ev.military else "dim cyan")
        _display_ref.log(Text(f"✈ {ev.headline}", style=style))


def _render_adsb_board(board: dict) -> str:
    """The monospaced table body for the pinned board."""
    rows = board.get("military") or []
    if not rows:
        return "```\nNothing military or unusual in range.\n```"
    lines = [f"{'CALLSIGN':10} {'TYPE':5} {'ALT':>7} {'GS':>4} {'DIST':>5} {'BRG':>4}  NOTE"]
    for r in rows[: config.ADSB_BOARD_MAX_ROWS]:
        alt = "grnd" if r["on_ground"] else (
            format(r["alt_ft"], ",") if r["alt_ft"] is not None else "?"
        )
        gs = f"{r['gs_kt']:.0f}" if r["gs_kt"] else "?"
        dist = f"{r['dist_nm']:.0f}" if r["dist_nm"] is not None else "?"
        brg = geo.compass_point(r["bearing"]) if r["bearing"] is not None else "?"
        note = []
        if r["in_box"]:
            note.append("IN BOX")
        note.append(r["title"] or ("military" if r["military"] else "probable display"))
        name = (r["ident"] or r["reg"] or r["hex"].upper())[:10]
        lines.append(
            f"{name:10} {(r['type'] or '?')[:5]:5} {alt:>7} {gs:>4} "
            f"{dist:>5} {brg:>4}  {' · '.join(note)[:44]}"
        )
    extra = len(rows) - config.ADSB_BOARD_MAX_ROWS
    if extra > 0:
        lines.append(f"… and {extra} more")
    return "```\n" + "\n".join(lines) + "\n```"


def _adsb_board_embed(board: dict) -> dict:
    rows = board.get("military") or []
    box = board.get("box") or []
    summary = (
        f"{len(rows)} tracked · {len(box)} in the display box · "
        f"{board.get('tracks', 0)} aircraft in range · source {board.get('source', '?')}"
    )
    return {
        "title": "\U0001f396️ Military & display — Gold Coast",
        "description": f"{_render_adsb_board(board)}\n{summary}\n{_ADSB_ATTRIBUTION}",
        "color": 0xE67E22 if rows else 0x2ECC71,
        "footer": {"text": _now_ts()},
        "timestamp": _now_iso(),
    }


_CARD_COLOURS = {
    "box": 0xE74C3C,
    "mil": 0xE67E22,
    "prob": 0x95A5A6,
    "lost": 0x596273,
}


def _card_position_line(row: dict) -> str:
    bits = []
    if row.get("on_ground"):
        bits.append("on the ground")
    elif row.get("alt_ft") is not None:
        bits.append(f"{row['alt_ft']:,} ft")
    if row.get("gs_kt"):
        bits.append(f"{row['gs_kt']:.0f} kt")
    if row.get("dist_nm") is not None and row.get("bearing") is not None:
        bits.append(
            f"{row['dist_nm']:.0f} nm {geo.compass_point(row['bearing'])} "
            f"of {config.ADSB_HOME_ICAO}"
        )
    if row.get("track_deg") is not None:
        bits.append(f"hdg {row['track_deg']:.0f}°")
    return " · ".join(bits)


def _member_label(row: dict) -> str:
    return row.get("ident") or row.get("reg") or row.get("hex", "").upper()


class _AdsbLiveCards:
    """One editable Discord message per active contact or formation.

    Turns the per-poll position feed into a living picture. A card opens when a
    contact — or a formation of them — is first tracked, is edited in place as
    it moves, and is finalised to its last-known state when it drops out of
    range. Two KC-30As therefore cost two messages that update quietly in
    place, not sixteen that scroll the channel.

    Driven entirely from the board snapshot on the poll thread, so there is a
    single writer and no lock is needed. A new card is opened with a blocking
    POST — rare, only on a genuinely new sighting — to capture the message id;
    every later edit goes through the async outbox.
    """

    def __init__(self):
        self._cards: dict = {}

    def sync(self, board: dict, channel_id: str) -> None:
        if not (config.DISCORD_ENABLED and config.ADSB_LIVE_CARDS_ENABLED and channel_id):
            return
        now = time.time()
        rows = board.get("military") or []
        radius = config.ADSB_FORMATION_RADIUS_NM if config.ADSB_FORMATION_ENABLED else 0.0
        groups = adsb_tracker.group_formations(
            rows,
            radius_nm=radius,
            alt_band_ft=config.ADSB_FORMATION_ALT_BAND_FT,
            min_size=config.ADSB_FORMATION_MIN,
        )
        busy = _discord_outbox is not None and _discord_outbox.depth > 40

        matched = set()
        # New cards open with a blocking POST (to capture the message id), so
        # cap how many we open in one pass — a mass launch then spreads its new
        # cards over a few board cycles instead of stalling the poll thread.
        new_budget = 4
        for g in groups:
            key = self._match(g, matched)
            matched.add(key)
            card = self._cards.get(key)
            body, embed = self._embed(g, now, lost=False)
            if card is None:
                active = sum(1 for c in self._cards.values() if not c["retired"])
                if active >= config.ADSB_LIVE_CARDS_MAX or new_budget <= 0:
                    continue
                message_id = _post_discord(channel_id, embed=embed)
                if not message_id:
                    continue
                new_budget -= 1
                self._cards[key] = {
                    "message_id": message_id, "channel": channel_id, "body": body,
                    "hexes": set(g["hexes"]), "group": g, "created": now,
                    "updated": now, "seen": now, "lost": False, "lost_at": 0.0,
                    "retired": False,
                }
            else:
                relit = card["lost"]
                card.update(hexes=set(g["hexes"]), group=g, seen=now, lost=False)
                if (body != card["body"] or relit) and not busy:
                    card.update(body=body, updated=now)
                    _enqueue_discord(channel_id, embed=embed, message_id=card["message_id"])
        self._retire_absent(matched, now)

    def _match(self, group: dict, taken: set) -> str:
        """Reuse the card sharing the most aircraft with this group.

        Keying on membership keeps a card stable as aircraft join or leave a
        formation; only a group that shares nothing with any live card opens a
        new one.
        """
        hexes = set(group["hexes"])
        best_key, best = None, 0
        for key, card in self._cards.items():
            if key in taken or card["retired"]:
                continue
            overlap = len(hexes & card["hexes"])
            if overlap > best:
                best, best_key = overlap, key
        if best_key is not None:
            return best_key
        return group["key"] or (min(hexes) if hexes else f"card{len(self._cards)}")

    def _retire_absent(self, matched: set, now: float) -> None:
        for key, card in list(self._cards.items()):
            if key in matched or card["retired"]:
                continue
            if not card["lost"]:
                body, embed = self._embed(card["group"], now, lost=True)
                if body != card["body"]:
                    card["body"] = body
                    _enqueue_discord(card["channel"], embed=embed, message_id=card["message_id"])
                card.update(lost=True, lost_at=now)
            elif now - card["lost_at"] >= config.ADSB_CARD_RETIRE_SEC:
                card["retired"] = True

    def _embed(self, group: dict, now: float, lost: bool) -> tuple:
        formation = group["formation"] and group["size"] >= 2
        icon = "\U0001f507" if lost else ("\U0001f3af" if group["in_box"] else "\U0001f4e1")
        label, typ, members = group["label"], group["family"] or "?", group["members"]

        if formation:
            head = f"{icon} {label} formation · {group['size']}× {typ}"
        else:
            subtype = members[0].get("type") or members[0].get("desc") or ""
            head = f"{icon} {label}" + (f" — {subtype}" if subtype else "")

        lines = []
        if group["title"]:
            lines.append(f"**{group['title']}**")
        if formation:
            summary = [f"{group['size']} aircraft"]
            if group["alt_min"] is not None and group["alt_max"] is not None:
                summary.append(
                    f"{group['alt_min']:,} ft" if group["alt_min"] == group["alt_max"]
                    else f"{group['alt_min']:,}–{group['alt_max']:,} ft"
                )
            if group["dist_nm"] is not None and group["bearing"] is not None:
                summary.append(
                    f"{group['dist_nm']:.0f} nm {geo.compass_point(group['bearing'])} "
                    f"of {config.ADSB_HOME_ICAO}"
                )
            lines.append(" · ".join(summary))
            for m in members[:8]:
                lines.append(f"• **{_member_label(m)}** — {_card_position_line(m) or '?'}")
            if group["size"] > 8:
                lines.append(f"…and {group['size'] - 8} more")
        else:
            pos = _card_position_line(members[0])
            if pos:
                lines.append(pos)
        if lost:
            when = datetime.fromtimestamp(now, _AEST).strftime("%H:%M")
            lines.append(f"_No longer in range — last tracked ~{when}._")
        globe = members[0].get("url") or ""
        if globe:
            lines.append(f"[Track on globe.adsbexchange.com]({globe})")

        colour = (
            _CARD_COLOURS["lost"] if lost
            else _CARD_COLOURS["box"] if group["in_box"]
            else _CARD_COLOURS["mil"] if group["military"]
            else _CARD_COLOURS["prob"]
        )
        embed = {
            "title": head[:256],
            "description": "\n".join(lines)[:4096],
            "color": colour,
            "footer": {"text": f"updated {_now_ts()} · data adsb.fi"},
            "timestamp": _now_iso(),
        }
        fields = []
        if formation:
            fields.append({"name": "Aircraft", "value": str(group["size"]), "inline": True})
            fields.append({"name": "Type", "value": typ, "inline": True})
        else:
            m0 = members[0]
            if m0.get("reg"):
                fields.append({"name": "Registration", "value": m0["reg"], "inline": True})
            if m0.get("type"):
                fields.append({"name": "Type", "value": m0["type"], "inline": True})
            if m0.get("squawk"):
                fields.append({"name": "Squawk", "value": m0["squawk"], "inline": True})
        reasons = members[0].get("reasons")
        if reasons:
            fields.append({"name": "Matched on", "value": reasons[:1024], "inline": False})
        if fields:
            embed["fields"] = fields[:6]

        # Change-detection key: what a reader would see, minus the footer clock
        # (so an unmoved aircraft is not re-edited every single poll).
        return head + "|" + "\n".join(lines), embed


_adsb_live_cards: Optional["_AdsbLiveCards"] = None
_adsb_board_last: dict = {}


def _broadcast_adsb_board(board: dict) -> None:
    """Edit the pinned board in place, but only when something actually changed.

    Two guards keep this cheap. First, an unchanged render is not re-sent at
    all, so a quiet night costs zero Discord traffic. Second, board refreshes
    go in at normal priority and are skipped outright when the outbox is
    backing up — there will be another refresh in 30 seconds, whereas a dropped
    event is gone for good.
    """
    if not config.DISCORD_ENABLED:
        return
    channel_id = config.DISCORD_CHANNEL_MILITARY or config.DISCORD_CHANNEL_FLIGHTS
    if not channel_id:
        return

    # Live cards manage their own change detection and backpressure, so refresh
    # them before the board's own guards below can early-return.
    if _adsb_live_cards is not None:
        try:
            _adsb_live_cards.sync(board, channel_id)
        except Exception as exc:
            if _display_ref is not None:
                _display_ref.log(Text(f"⚠ ADS-B live cards failed: {exc}", style="dim yellow"))

    if _discord_outbox is not None and _discord_outbox.depth > 50:
        return

    body = _render_adsb_board(board)
    if _adsb_board_last.get(channel_id) == body:
        return
    _adsb_board_last[channel_id] = body

    embed = _adsb_board_embed(board)
    message_id = _adsb_board_message.get(channel_id)
    if message_id:
        _enqueue_discord(channel_id, embed=embed, message_id=message_id)
    else:
        _enqueue_discord(channel_id, embed=embed)


def _announce_map_urls() -> None:
    """Post the live map's reachable addresses to #commands on startup.

    The Tailscale address is the useful one and is only knowable at runtime —
    it depends on which machine this is and whether Tailscale is up — so it
    goes out with every start rather than being written down anywhere.
    """
    addresses, note = adsb_web.bind_addresses()
    urls = [u for addr in addresses for u in adsb_web.access_urls(addr)]
    if not urls:
        return
    lines = [f"[{u.split()[0]}]({u.split()[0]})" + (f" — {' '.join(u.split()[1:])}"
             if len(u.split()) > 1 else "") for u in urls]
    body = "\n".join(lines)
    if note:
        body += f"\n\n*{note}*"
    if not config.ADSB_WEB_TOKEN and any(not a.startswith("127.") for a in addresses):
        body += "\n\n⚠️ No `ADSB_WEB_TOKEN` set — anyone who can reach the port can view it."
    _enqueue_discord(
        DISCORD_COMMANDS_CHANNEL_ID,
        embed={
            "title": "\U0001f5fa️ Live aircraft map is up",
            "description": body,
            "color": 0x3498DB,
            "footer": {"text": _now_ts()},
        },
    )
    if _display_ref is not None:
        for u in urls:
            _display_ref.log(Text(f"🗺  map  →  {u}", style="bold cyan"))


def _init_adsb_board_messages(state: SharedState) -> None:
    """Create or adopt the pinned ADS-B board. Main thread, startup only."""
    if not (config.DISCORD_ENABLED and config.ADSB_ENABLED):
        return
    channel_id = config.DISCORD_CHANNEL_MILITARY or config.DISCORD_CHANNEL_FLIGHTS
    if not channel_id:
        return
    embed = {
        "title": "\U0001f396️ Military & display — Gold Coast",
        "description": f"Starting up…\n{_ADSB_ATTRIBUTION}",
        "color": 0x95A5A6,
        "footer": {"text": _now_ts()},
    }
    existing = _find_existing_status_message(channel_id, _ADSB_BOARD_MARKERS)
    if existing and _edit_discord(channel_id, existing, embed=embed):
        _adsb_board_message[channel_id] = existing
        return
    message_id = _post_discord(channel_id, embed=embed)
    if message_id:
        _adsb_board_message[channel_id] = message_id
        _pin_discord(channel_id, message_id)


# ---------------------------------------------------------------------------
# Telegram command listener (bidirectional control)
# ---------------------------------------------------------------------------

def _resolve_station(arg: str) -> Optional[dict]:
    arg = arg.strip().upper()
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(STREAMS):
            return STREAMS[idx]
        return None
    for s in STREAMS:
        if s["icao"] == arg:
            return s
    return None


def _adsb_rows_text(rows: list, empty: str) -> str:
    """Rows as a <pre> table. Shared by /air, /mil and /box.

    Responses are written in the same HTML subset the Telegram handler uses;
    _html_to_discord_md converts for Discord, so one implementation serves
    both transports.
    """
    if not rows:
        return empty
    lines = [f"{'CALLSIGN':9} {'TYPE':5} {'ALT':>6} {'GS':>4} {'DIST':>5} BRG"]
    for r in rows:
        alt = "grnd" if r["on_ground"] else (
            format(r["alt_ft"], ",") if r["alt_ft"] is not None else "?"
        )
        gs = f"{r['gs_kt']:.0f}" if r["gs_kt"] else "?"
        dist = f"{r['dist_nm']:.0f}" if r["dist_nm"] is not None else "?"
        brg = geo.compass_point(r["bearing"]) if r["bearing"] is not None else "?"
        name = (r["ident"] or r["reg"] or r["hex"].upper())[:9]
        lines.append(
            f"{name:9} {(r['type'] or '?')[:5]:5} {alt:>6} {gs:>4} {dist:>5} {brg}"
        )
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def _handle_adsb_command(
    cmd: str,
    arg: str,
    respond: Callable[[str], None],
    tui: Callable[..., None],
    source: str,
) -> None:
    """/air /mil /box /track /adsb /watch /unwatch — transport-agnostic."""
    poller = _adsb_poller
    if poller is None:
        respond(
            "\U0001f6e9 ADS-B tracking is not running.\n"
            "Set <code>DISCORD_CHANNEL_MILITARY</code> and/or "
            "<code>DISCORD_CHANNEL_FLIGHTS</code> in <code>.env</code>, "
            "or check you did not start with <code>--no-adsb</code>."
        )
        return

    arg_l = arg.strip().lower()

    if cmd == "adsb":
        if arg_l in ("on", "off"):
            poller.set_enabled(arg_l == "on")
            respond(f"\U0001f4e1 ADS-B tracking: <b>{arg_l.upper()}</b>")
            tui(f"{source} → ADS-B tracking {arg_l.upper()}")
            return
        h = poller.health()
        age = time.time() - h["last_poll_at"] if h["last_poll_at"] else None
        lines = [
            "\U0001f4e1 <b>ADS-B tracking</b>",
            f"State: <b>{'ON' if h['enabled'] else 'OFF'}</b> · source <code>{h['source']}</code>",
            f"Last poll: {f'{age:.0f}s ago' if age is not None else 'never'} ({h['polls']} polls)",
            f"Tracking: {h['tracks']} aircraft · {h['military']} military · {h['in_box']} in the box",
            f"Events sent: {h['events']} · rate-limited away: {h['suppressed']}",
            f"Requests: {h['requests']} · failures: {h['failures']} · 429s: {h['rate_limited']}",
        ]
        if h["last_error"]:
            lines.append(f"Last error: <code>{html.escape(h['last_error'])}</code>")
        if h["watch"]:
            lines.append(f"Watchlist: <code>{html.escape(', '.join(h['watch']))}</code>")
        if poller.store is not None:
            s = poller.store.stats()
            if "error" not in s:
                lines.append(
                    f"Seen all-time: {s['sightings']} aircraft ({s['military_seen']} military)"
                )
        lines.append("Data from adsb.fi — https://adsb.fi")
        respond("\n".join(lines))
        return

    if cmd in ("watch", "unwatch"):
        if not arg:
            respond(f"Usage: <code>/{cmd} &lt;hex|callsign&gt;</code>")
            return
        if cmd == "watch":
            ok = poller.add_watch(arg)
            respond(
                f"\U0001f440 Watching <code>{html.escape(arg.upper())}</code>"
                if ok else f"Already watching <code>{html.escape(arg.upper())}</code>"
            )
        else:
            ok = poller.remove_watch(arg)
            respond(
                f"Stopped watching <code>{html.escape(arg.upper())}</code>"
                if ok else f"<code>{html.escape(arg.upper())}</code> was not on the watchlist"
            )
        tui(f"{source} → {cmd} {arg.upper()}")
        return

    if cmd == "track":
        if not arg:
            respond("Usage: <code>/track &lt;hex|callsign|registration&gt;</code>")
            return
        found = poller.find(arg)
        if not found:
            # Not in range — ask the feed directly. This goes through the same
            # rate limiter as the poller, so it cannot burst the API.
            reports = (
                poller.source.by_hex(arg) if len(arg) == 6 and _is_hexish(arg)
                else poller.source.by_callsign(arg) or poller.source.by_registration(arg)
            )
            if not reports:
                respond(
                    f"\U0001f50d No aircraft matching <code>{html.escape(arg.upper())}</code> "
                    "is transmitting right now."
                )
                return
            r = reports[0]
            cls = adsb_classify.classify(r, in_box=False)
            respond(
                f"\U0001f50d <b>{html.escape(r.label())}</b> — outside the tracked area\n"
                f"{html.escape(r.reg or '')} {html.escape(r.descr or r.typ or '')}\n"
                f"{r.lat:.3f}, {r.lon:.3f} · {r.alt_ft or '?'} ft\n"
                f"{html.escape(cls.reason_text()) if cls.signals else ''}\n"
                f"https://globe.adsbexchange.com/?icao={r.hex}"
            )
            return
        t = found[0]
        cls = t.classification
        lines = [
            f"\U0001f50d <b>{html.escape(t.label())}</b>",
            html.escape(cls.title or t.descr or t.typ or "unidentified"),
            html.escape(t.position_text()),
            f"State: {t.state} · phase: {t.phase}"
            + (" · <b>IN THE DISPLAY BOX</b>" if t.in_box else ""),
        ]
        if t.squawk:
            lines.append(f"Squawk: <code>{t.squawk}</code>")
        if cls.signals:
            lines.append(
                f"Classified {cls.confidence:.0%}: {html.escape(cls.reason_text())}"
            )
        if poller.store is not None:
            recent = poller.store.recent_events(5, t.hex)
            if recent:
                lines.append("Recent:")
                for e in recent:
                    when = datetime.fromtimestamp(e["at"], _AEST).strftime("%H:%M")
                    lines.append(f"  {when} {e['kind']}")
        lines.append(t.globe_url())
        respond("\n".join(lines))
        return

    if cmd == "mil":
        rows = poller.snapshot(military_only=True, airborne_only=False)
        respond(
            "\U0001f396 <b>Military &amp; display aircraft</b>\n"
            + _adsb_rows_text(rows, "Nothing military or unusual in range.")
        )
        return

    if cmd == "box":
        rows = poller.snapshot(box_only=True, airborne_only=False)
        respond(
            "\U0001f3af <b>Airshow display box</b>\n"
            + _adsb_rows_text(rows, "The display box is empty.")
        )
        return

    # /air
    rows = poller.snapshot(airborne_only=True, limit=25)
    respond(
        f"✈️ <b>Airborne within {config.ADSB_RADIUS_NM:.0f} nm of "
        f"{config.ADSB_HOME_ICAO}</b>\n"
        + _adsb_rows_text(rows, "Nothing airborne in range.")
    )


def _is_hexish(text: str) -> bool:
    try:
        int(text, 16)
        return True
    except ValueError:
        return False


def _handle_command(
    text: str,
    state: SharedState,
    respond: Callable[[str], None],
    source: str = "Telegram",
) -> None:
    if not text.startswith("/"):
        return
    parts = text.split(None, 2)
    cmd = parts[0].lower().lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""
    arg_l = arg.lower()
    rest = parts[2].strip() if len(parts) > 2 else ""

    def tui(msg: str, style: str = "dim cyan") -> None:
        if state.display:
            state.display.log(Text(msg, style=style))

    if cmd == "help":
        respond(
            "\U0001f4e1 <b>ATC Tracker commands</b>\n\n"
            "/status — show all station states\n"
            "/health — connection & queue diagnostics\n"
            "/mute 1  or  /mute YBCG — mute a station\n"
            "/unmute 1  or  /unmute YBCG — unmute a station\n"
            "/mute all — mute every station\n"
            "/unmute all — unmute every station\n"
            "/url YBCG — show a station stream URL\n"
            "/urls — show all stream URLs\n"
            "/seturl YBCG https://... — update a stream URL\n"
            "/reseturl YBCG — restore the default stream URL\n"
            "/reconnect YBCG — force a station to reconnect\n"
            "/vad YBCG 0.003 — show or set a station's VAD threshold\n"
            "/record on|off — toggle transmission audio recording\n"
            "/keywords on|off — toggle keyword highlighting\n"
            "/military — military callsign registry status\n"
            "/military on|off — toggle military callsign detection\n"
            "/military refresh — re-scrape the callsign list now\n"
            "/military FALCON — look a callsign up\n"
            "\n<b>Live ADS-B tracking</b>\n"
            "/air — what is airborne in range now\n"
            "/mil — military &amp; airshow display aircraft only\n"
            "/box — who is in the airshow display box\n"
            "/track VH-SIC — detail on one aircraft (hex, callsign or rego)\n"
            "/watch 7CF839 — always alert on this aircraft\n"
            "/unwatch 7CF839 — stop watching it\n"
            "/adsb — ADS-B poller health\n"
            "/adsb on|off — toggle ADS-B tracking\n"
            "/pause — suspend all transcription & forwarding\n"
            "/resume — restart transcription & forwarding\n"
            "/help — this message"
        )

    elif cmd == "health":
        now = time.time()

        def ago(when: Optional[float]) -> str:
            return "never" if not when else f"{now - when:.0f}s ago"

        lines = ["\U0001fa7a <b>Health</b>"]
        for s in STREAMS:
            h = state.health[s["icao"]]
            icon = "\U0001f7e2" if h.connected else "\U0001f534"
            lines.append(
                f"{icon} <b>{s['icao']}</b> audio {ago(h.last_audio_at)} · "
                f"last TX {ago(h.last_tx_at)} · reconnects {h.reconnects} · dropped {h.dropped}"
            )
            if h.active_url:
                lines.append(f"   <code>{html.escape(h.active_url)}</code>")
        if _transcriber_ref is not None:
            lines.append(
                f"\nTranscriber queue: <b>{_transcriber_ref.queue_depth}</b> · "
                f"gated (hallucination/low quality): <b>{_transcriber_ref.gated}</b>"
            )
        if _discord_outbox is not None:
            lines.append(f"Discord outbox: <b>{_discord_outbox.depth}</b>")
        lines.append(f"Recording: <b>{'ON' if state.recording_enabled else 'OFF'}</b>")
        lines.append(f"STT backend: <b>{_backend_name}</b>")
        mil = military_callsigns.get_registry().stats()
        lines.append(
            f"Military callsigns: <b>{mil['count']}</b> "
            f"({'on' if state.military_enabled else 'off'}, "
            f"scraped {mil['last_success_at']})"
        )
        if _adsb_poller is not None:
            a = _adsb_poller.health()
            age = time.time() - a["last_poll_at"] if a["last_poll_at"] else None
            lines.append(
                f"ADS-B: <b>{a['tracks']}</b> tracked "
                f"({a['military']} military, {a['in_box']} in box) · "
                f"polled {f'{age:.0f}s ago' if age is not None else 'never'} · "
                f"{a['source']}"
                + (f" · {a['suppressed']} alerts rate-limited" if a["suppressed"] else "")
            )
        respond("\n".join(lines))

    elif cmd == "status":
        lines = ["\U0001f4e1 <b>Station status</b>"]
        for i, s in enumerate(STREAMS, 1):
            icon = "✅" if state.is_enabled(s["icao"]) else "\U0001f507"
            override = " override" if state.has_stream_override(s["icao"]) else ""
            lines.append(f"{icon} [{i}] {s['icao']} {s['name']}{override}")
        kw = "ON" if state.keywords_enabled else "OFF"
        lines.append(f"\nKeywords: <b>{kw}</b>")
        paused_str = "⏸ Paused" if state.paused else "▶ Running"
        lines.append(f"Status: <b>{paused_str}</b>")
        lines.append("\nStream URLs:")
        for s in STREAMS:
            lines.append(_format_stream_url_line(state, s))
        respond("\n".join(lines))

    elif cmd in ("mute", "unmute"):
        want_enabled = cmd == "unmute"
        label = "active" if want_enabled else "muted"
        icon = "✅" if want_enabled else "\U0001f507"
        if arg_l == "all":
            for s in STREAMS:
                state.station_enabled[s["icao"]] = want_enabled
                _broadcast_station_status(s, want_enabled)
            respond(f"{icon} All stations {label}")
            tui(f"{source} → all stations {label}")
            if state.display:
                state.display.refresh()
        else:
            station = _resolve_station(arg)
            if station is None:
                respond(
                    f"⚠ Unknown station: <code>{arg}</code>\n"
                    f"Use a number (1–{len(STREAMS)}) or ICAO code."
                )
                return
            state.station_enabled[station["icao"]] = want_enabled
            _broadcast_station_status(station, want_enabled)
            respond(f"{icon} {station['icao']} {station['name']} — {label}")
            tui(f"{source} → {station['icao']} {label}")
            if state.display:
                state.display.refresh()

    elif cmd in ("url", "streamurl"):
        station = _resolve_station(arg)
        if station is None:
            respond(
                f"Usage: /url <code>YBCG</code>\n"
                f"Use a number (1–{len(STREAMS)}) or ICAO code."
            )
            return
        respond(_format_stream_url_line(state, station))

    elif cmd in ("urls", "streamurls"):
        lines = ["\U0001f517 <b>Stream URLs</b>"]
        for s in STREAMS:
            lines.append(_format_stream_url_line(state, s))
        respond("\n".join(lines))

    elif cmd in ("seturl", "stream"):
        station = _resolve_station(arg)
        if station is None or not rest:
            respond(
                f"Usage: /seturl <code>YBCG</code> <code>https://...</code>\n"
                f"Use a number (1–{len(STREAMS)}) or ICAO code."
            )
            return
        if not _valid_stream_url(rest):
            respond("⚠ Stream URL must start with <code>http://</code> or <code>https://</code> and include a host.")
            return
        changed = state.set_stream_url(station["icao"], rest)
        respond(
            f"\U0001f517 {station['icao']} {station['name']} stream URL "
            f"{'updated' if changed else 'already set'}.\n"
            f"<code>{html.escape(rest)}</code>"
        )
        tui(f"{source} → {station['icao']} stream URL updated; reconnecting")

    elif cmd in ("reseturl", "resetstream"):
        station = _resolve_station(arg)
        if station is None:
            respond(
                f"Usage: /reseturl <code>YBCG</code>\n"
                f"Use a number (1–{len(STREAMS)}) or ICAO code."
            )
            return
        changed = state.reset_stream_url(station["icao"])
        default_url = state.get_default_stream_url(station["icao"])
        respond(
            f"\U0001f504 {station['icao']} {station['name']} stream URL "
            f"{'restored to default' if changed else 'is already default'}.\n"
            f"<code>{html.escape(default_url)}</code>"
        )
        if changed:
            tui(f"{source} → {station['icao']} stream URL reset; reconnecting")

    elif cmd == "reconnect":
        station = _resolve_station(arg)
        if station is None:
            respond(
                f"Usage: /reconnect <code>YBCG</code>\n"
                f"Use a number (1–{len(STREAMS)}) or ICAO code."
            )
            return
        # Bumping the URL version is what the stream loop watches, so setting
        # the URL to its current value is enough to make it cycle.
        state.bump_stream_url_version(station["icao"])
        respond(f"\U0001f504 {station['icao']} {station['name']} — reconnecting")
        tui(f"{source} → {station['icao']} forced reconnect")

    elif cmd == "vad":
        station = _resolve_station(arg)
        if station is None:
            respond(
                f"Usage: /vad <code>YBCG</code> or /vad <code>YBCG 0.004</code>\n"
                f"Use a number (1–{len(STREAMS)}) or ICAO code."
            )
            return
        icao = station["icao"]
        if not rest:
            respond(f"\U0001f39a {icao} VAD threshold: <b>{state.get_vad_threshold(icao):.5f}</b>")
            return
        try:
            value = float(rest)
        except ValueError:
            respond(f"⚠ <code>{html.escape(rest)}</code> is not a number.")
            return
        if not 0.0 < value < 1.0:
            respond("⚠ VAD threshold must be between 0 and 1 (typical range 0.001–0.02).")
            return
        state.set_vad_threshold(icao, value)
        respond(f"\U0001f39a {icao} VAD threshold set to <b>{value:.5f}</b>")
        tui(f"{source} → {icao} VAD threshold {value:.5f}")

    elif cmd == "record":
        if arg_l in ("on", "off"):
            want = arg_l == "on"
            state.set_recording(want)
            respond(f"\U0001f3a4 Recording: <b>{'ON' if want else 'OFF'}</b>")
            tui(f"{source} → recording {'ON' if want else 'OFF'}")
        else:
            respond(
                f"Usage: /record on  or  /record off\n"
                f"Currently: <b>{'ON' if state.recording_enabled else 'OFF'}</b>"
            )

    elif cmd == "keywords":
        if arg_l in ("on", "off"):
            want = arg_l == "on"
            state.set_keywords(want)
            kw_label = "ON" if want else "OFF"
            respond(f"\U0001f50d Keywords: <b>{kw_label}</b>")
            tui(f"{source} → Keywords {kw_label}")
            if state.display:
                state.display.refresh()
        else:
            respond("Usage: /keywords on  or  /keywords off")

    # "mil" deliberately NOT in this tuple — it now means "what military is
    # airborne right now", which during an event gets asked constantly, whereas
    # "look up a callsign in the register" does not. /military is unchanged.
    elif cmd in ("military", "callsign", "callsigns"):
        if arg_l in ("on", "off"):
            want = arg_l == "on"
            state.set_military(want)
            respond(f"\U0001f6e9 Military callsign detection: <b>{'ON' if want else 'OFF'}</b>")
            tui(f"{source} → military detection {'ON' if want else 'OFF'}")
        elif arg_l == "refresh":
            respond("\U0001f6e9 Refreshing the callsign list…")
            result = military_callsigns.get_registry().refresh(force=True)
            respond(("✅ " if result.ok else "⚠ ") + html.escape(result.message))
            tui(f"{source} → callsign refresh: {result.message}")
        elif arg:
            record = military_callsigns.get_registry().lookup(arg)
            if record is None:
                respond(f"\U0001f6e9 <code>{html.escape(arg.upper())}</code> is not in the registry")
            else:
                respond(
                    f"\U0001f6e9 <b>{html.escape(record.callsign)}</b>\n"
                    f"{html.escape(record.describe())}"
                    + (f"\nOps freq: <code>{html.escape(record.ops_freq)}</code>" if record.ops_freq else "")
                    + ("\n<i>confirmed</i>" if record.confirmed else "")
                )
        else:
            stats = military_callsigns.get_registry().stats()
            lines = [
                "\U0001f6e9 <b>Military callsign registry</b>",
                f"Detection: <b>{'ON' if state.military_enabled else 'OFF'}</b>",
                f"Callsigns: <b>{stats['count']}</b>",
                f"Last scrape: {stats['last_success_at']}",
            ]
            if stats["last_error"]:
                lines.append(f"Last error: <code>{html.escape(stats['last_error'])}</code>")
            lines.append(f"Source: {html.escape(stats['source'])}")
            lines.append("\n/military on|off · /military refresh · /military FALCON")
            respond("\n".join(lines))

    elif cmd in ("air", "mil", "box", "track", "adsb", "watch", "unwatch"):
        _handle_adsb_command(cmd, arg, respond, tui, source)

    elif cmd in ("pause", "resume"):
        want_paused = cmd == "pause"
        state.set_paused(want_paused)
        icon = "⏸" if want_paused else "▶️"
        label = "paused" if want_paused else "resumed"
        detail = "transcription & forwarding stopped" if want_paused else "transcription active"
        _broadcast_pause_status(want_paused)
        respond(f"{icon} ATC Tracker <b>{label}</b> — {detail}")
        tui(f"{source} → {label}", style="bold yellow" if want_paused else "bold green")
        if state.display:
            state.display.refresh()

    else:
        respond(f"Unknown command: <code>/{cmd}</code>\nSend /help for a list.")


def _telegram_command_listener(state: SharedState) -> None:
    offset = 0
    base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    while not state.stop_event.is_set():
        if not _telegram_active:
            # Disabled — don't touch the network at all until re-enabled.
            state.stop_event.wait(2)
            continue
        try:
            resp = requests.get(
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id != str(config.TELEGRAM_CHAT_ID):
                    continue
                text = (msg.get("text") or "").strip()
                _handle_command(text, state, respond=_post_telegram, source="Telegram")
        except Exception as exc:
            if not state.stop_event.is_set():
                if state.display:
                    state.display.log(Text(f"⚠ Telegram poll error: {exc}", style="dim yellow"))
                time.sleep(RECONNECT_DELAY_SEC)


def _discord_command_listener(state: SharedState) -> None:
    channel_id = DISCORD_COMMANDS_CHANNEL_ID
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    base_url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    healthy = True
    error_count = 0

    # Seed last_id from the newest existing message so old command history
    # in #commands isn't replayed on startup.
    last_id: Optional[str] = None
    seed_failed = False
    try:
        resp = requests.get(base_url, params={"limit": 1}, headers=headers, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")
        msgs = resp.json()
        if isinstance(msgs, list) and msgs:
            last_id = msgs[0]["id"]
    except Exception:
        seed_failed = True

    while not state.stop_event.is_set():
        try:
            params = {"limit": 50}
            if last_id:
                params["after"] = last_id
            resp = requests.get(base_url, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except Exception:
                    pass
                state.stop_event.wait(min(retry_after, 10.0) + 0.05)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")
            if not healthy and state.display:
                state.display.log(Text("✓ Discord command polling recovered", style="dim green"))
            healthy = True
            error_count = 0
            msgs = resp.json()
            if isinstance(msgs, list) and msgs:
                ordered = sorted(msgs, key=lambda m: int(m["id"]))
                for msg in ordered:
                    last_id = msg["id"]
                    if seed_failed:
                        # First cycle after a failed seed: capture last_id but
                        # skip processing to avoid replaying old history.
                        continue
                    if msg.get("author", {}).get("bot"):
                        continue
                    text = (msg.get("content") or "").strip()
                    _handle_command(text, state, respond=_discord_respond, source="Discord")
                seed_failed = False
        except Exception as exc:
            if not state.stop_event.is_set():
                error_count += 1
                delay = min(60.0, 2.5 * (2 ** min(error_count - 1, 5)))
                if healthy and state.display:
                    state.display.log(Text(
                        f"⚠ Discord command polling unavailable: {exc}. Retrying with backoff.",
                        style="dim yellow",
                    ))
                elif error_count in (3, 6) and state.display:
                    state.display.log(Text(
                        f"⚠ Discord command polling still unavailable; next retry in {delay:.0f}s",
                        style="dim yellow",
                    ))
                healthy = False
                state.stop_event.wait(delay)
                continue
        state.stop_event.wait(2.5)


# ---------------------------------------------------------------------------
# Keyboard listener
# ---------------------------------------------------------------------------

def _keyboard_listener(state: SharedState) -> None:
    try:
        import termios
        import tty as tty_mod

        with open("/dev/tty", "r") as tty_fh:
            old = termios.tcgetattr(tty_fh)
            try:
                tty_mod.setraw(tty_fh.fileno())
                while not state.stop_event.is_set():
                    ch = tty_fh.read(1)
                    if ch.lower() == "k":
                        state.toggle_keywords()
                        if state.display:
                            state.display.refresh()
                    elif ch.lower() == "t":
                        global _telegram_active
                        _telegram_active = not _telegram_active
                        if state.display:
                            ts = _now_ts()
                            msg = "📨 Telegram ENABLED" if _telegram_active else "📨 Telegram DISABLED"
                            style = "bold green" if _telegram_active else "bold yellow"
                            state.display.log(Text(f"[{ts}] {msg}", style=style))
                            state.display.refresh()
                    elif ch.lower() == "p":
                        paused = state.toggle_pause()
                        _broadcast_pause_status(paused)
                        if state.display:
                            ts = _now_ts()
                            msg = "⏸ PAUSED — transcription & Telegram stopped" if paused else "▶ RESUMED — transcription active"
                            style = "bold yellow" if paused else "bold green"
                            state.display.log(Text(f"[{ts}] {msg}", style=style))
                            state.display.refresh()
                    elif ch.isdigit() and ch != "0":
                        idx = int(ch) - 1
                        if idx < len(STREAMS):
                            enabled = state.toggle_station(STREAMS[idx]["icao"])
                            _broadcast_station_status(STREAMS[idx], enabled)
                            if state.display:
                                state.display.refresh()
                    elif ch in ("\x03", "\x04", "q", "Q"):
                        state.stop_event.set()
                        break
            finally:
                termios.tcsetattr(tty_fh, termios.TCSADRAIN, old)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-station stream loop (runs in its own thread)
# ---------------------------------------------------------------------------

def _candidate_urls(stream_cfg: dict, state: SharedState, attempt: int) -> str:
    """Rotates through the redirector then the known edge hosts.

    A user-set /seturl or STREAM_URL_<ICAO> override is never second-guessed —
    if someone pinned a URL, that is the URL we use.
    """
    icao = stream_cfg["icao"]
    url = state.get_stream_url(icao)
    if state.has_stream_override(icao):
        return url
    ladder = [url] + [u for u in stream_cfg.get("fallback_urls", []) if u != url]
    return ladder[attempt % len(ladder)]


def _stream_loop(stream_cfg: dict, transcriber: Transcriber, state: SharedState) -> None:
    icao = stream_cfg["icao"]
    station_name = stream_cfg["name"]
    headers = stream_cfg["headers"]
    health = state.health[icao]

    def tui_log(line: Text) -> None:
        if state.display:
            state.display.log(line)

    def flush_tx(buf: TransmissionBuffer) -> None:
        raw, preroll = buf.flush()
        if not len(raw):
            return
        audio = _preprocessor.process(raw, preroll_samples=preroll)
        if state.is_enabled(icao) and not state.paused:
            transcriber.submit(
                audio, raw, len(audio) / VAD_SAMPLE_RATE,
                icao, station_name, _now_ts(), _now_iso(),
            )

    was_connected = False
    failures = 0

    while not state.stop_event.is_set():
        source: Optional[LiveATCSource] = None
        try:
            url_version = state.get_stream_url_version(icao)
            url = _candidate_urls(stream_cfg, state, failures)
            source = LiveATCSource(url, headers)

            pcm_gen = miniaudio.stream_any(
                source,
                source_format=miniaudio.FileFormat.MP3,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=VAD_SAMPLE_RATE,
                frames_to_read=VAD_CHUNK_FRAMES,
            )

            vad = VoiceActivityDetector(threshold=state.get_vad_threshold(icao))
            buf = TransmissionBuffer()
            in_tx = False
            silent_samples = 0
            last_threshold_check = 0.0

            ts = _now_ts()
            line = Text()
            line.append(f"[{ts}] {icao} {station_name:<16}  ", style="bold green")
            line.append(f"│ Connected  {source.resolved_url}", style="green")
            tui_log(line)
            health.connected = True
            health.active_url = source.resolved_url
            if not was_connected:
                _broadcast_stream_status(stream_cfg, connected=True)
                was_connected = True
            failures = 0

            url_changed = False
            for raw_chunk in pcm_gen:
                if state.stop_event.is_set():
                    break
                if state.get_stream_url_version(icao) != url_version:
                    url_changed = True
                    tui_log(Text(f"↻ {icao}: stream URL changed — reconnecting", style="cyan"))
                    break

                now = time.monotonic()
                # (A stalled feed is detected inside LiveATCSource.read, which is
                # where the blocking actually happens.)
                if now - last_threshold_check > 1.0:
                    vad.set_base_threshold(state.get_vad_threshold(icao))
                    last_threshold_check = now

                health.last_audio_at = time.time()
                chunk = np.frombuffer(bytes(raw_chunk), dtype=np.int16).astype(np.float32) / 32768.0

                if vad.is_speech(chunk):
                    in_tx = True
                    silent_samples = 0
                    buf.append(chunk)
                    if buf.duration_seconds > MAX_TRANSMISSION_SEC:
                        flush_tx(buf)
                else:
                    if in_tx:
                        buf.append(chunk)
                        # Measured in audio, not wall-clock. miniaudio delivers
                        # decoded chunks in bursts out of its buffer, so seconds
                        # of silent audio can arrive within milliseconds of real
                        # time — a wall-clock hangover simply never fires during
                        # a burst, and consecutive radio calls get glued into one
                        # transmission.
                        silent_samples += len(chunk)
                        if silent_samples >= VAD_SILENCE_HANGOVER * VAD_SAMPLE_RATE:
                            flush_tx(buf)
                            in_tx = False
                            silent_samples = 0
                    else:
                        buf.observe_silence(chunk)

            if url_changed:
                was_connected = False
                failures = 0

        except Exception as exc:
            if not state.stop_event.is_set():
                failures += 1
                health.connected = False
                health.reconnects += 1
                # Exponential backoff with jitter, so three feeds that all lose
                # their edge at once don't retry in lockstep forever.
                delay = min(
                    RECONNECT_MAX_DELAY_SEC,
                    RECONNECT_DELAY_SEC * (2 ** min(failures - 1, 4)),
                )
                delay += random.uniform(0, delay * 0.25)
                tui_log(Text(f"⚠ {icao}: {exc}", style="red"))
                tui_log(Text(f"  Reconnecting in {delay:.0f}s…", style="dim"))
                if was_connected:
                    _broadcast_stream_status(stream_cfg, connected=False, detail=str(exc))
                    was_connected = False
                state.stop_event.wait(delay)
        finally:
            if source:
                source.close()


# ---------------------------------------------------------------------------
# Model download check
# ---------------------------------------------------------------------------

def _check_prompt_lengths(console: Console) -> None:
    """Whisper silently truncates an oversized prompt to its tail. A single
    shared 232-token prompt once got cut down to its Southport section, so every
    station was conditioned on Southport phraseology and echoed it into
    transcripts. This is the check that would have caught it."""
    try:
        from mlx_whisper.tokenizer import get_tokenizer
        tokenizer = get_tokenizer(multilingual=True, language="en", task="transcribe")
    except Exception:
        return  # tokenizer unavailable (e.g. parakeet-only run) — skip silently
    for s in STREAMS:
        prompt = s.get("prompt", "")
        if not prompt:
            continue
        n = len(tokenizer.encoding.encode(" " + prompt.strip()))
        if n > config.MAX_PROMPT_TOKENS:
            console.print(
                f"[red]⚠ {s['icao']} prompt is {n} tokens (max {config.MAX_PROMPT_TOKENS}).\n"
                "  Whisper keeps only the tail of an oversized prompt and will echo it "
                "into transcripts. Shorten it in config.py.[/red]"
            )


def _preflight_streams(state: SharedState, console: Console) -> list[str]:
    """Checks every feed before the UI takes over the terminal, so a dead
    station is obvious at launch rather than halfway through the show."""
    results: list[str] = []
    console.print("[dim]Checking streams...[/dim]")
    for s in STREAMS:
        icao = s["icao"]
        url = state.get_stream_url(icao)
        try:
            with requests.get(
                url, headers=s["headers"], stream=True, timeout=12, allow_redirects=True
            ) as resp:
                name = resp.headers.get("icy-name", "")
                if resp.status_code == 200:
                    console.print(
                        f"[green]  ✓ {icao} {s['name']} — {name or 'live'}[/green] "
                        f"[dim]{resp.url}[/dim]"
                    )
                    results.append(f"✅ {icao} {s['name']} — {name or 'live'}")
                else:
                    console.print(f"[red]  ✗ {icao} {s['name']} — HTTP {resp.status_code}[/red]")
                    results.append(f"❌ {icao} {s['name']} — HTTP {resp.status_code}")
        except Exception as exc:
            console.print(f"[red]  ✗ {icao} {s['name']} — {exc}[/red]")
            results.append(f"❌ {icao} {s['name']} — {str(exc)[:80]}")
    return results


def _prune_recordings(recordings_dir: Path, retention_days: int, console: Console) -> None:
    if retention_days <= 0 or not recordings_dir.exists():
        return
    cutoff = (datetime.now(timezone.utc).astimezone(_AEST) - timedelta(days=retention_days)).date()
    removed = 0
    for day_dir in recordings_dir.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            for f in day_dir.iterdir():
                f.unlink(missing_ok=True)
                removed += 1
            day_dir.rmdir()
    if removed:
        console.print(f"[dim]Pruned {removed} recordings older than {retention_days} days[/dim]")


# ---------------------------------------------------------------------------
# Calibrate mode
# ---------------------------------------------------------------------------

def _calibrate(console: Console, stream_cfg: dict) -> None:
    icao = stream_cfg["icao"]
    threshold = stream_cfg.get("vad_threshold", VAD_RMS_THRESHOLD)
    console.print(
        f"[cyan]Calibrate — {icao} {stream_cfg['name']} — printing live RMS for 15s.\n"
        f"Silence ≈ 0.000; transmissions spike above {threshold}. Ctrl+C to stop.[/cyan]"
    )
    source = LiveATCSource(stream_cfg["url"], stream_cfg["headers"])
    pcm_gen = miniaudio.stream_any(
        source,
        source_format=miniaudio.FileFormat.MP3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=VAD_SAMPLE_RATE,
        frames_to_read=VAD_CHUNK_FRAMES,
    )
    deadline = time.monotonic() + 15
    try:
        for raw_chunk in pcm_gen:
            chunk = np.frombuffer(bytes(raw_chunk), dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            bar = "█" * min(60, int(rms * 5000))
            flag = " ← TX" if rms > threshold else ""
            console.print(f"RMS {rms:.5f}  {bar}{flag}", highlight=False)
            if time.monotonic() > deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        source.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ATC Tracker — multi-station live speech-to-text transcription"
    )
    parser.add_argument("--model", default=None, metavar="REPO",
                        help="Model repo (defaults to the backend's own default)")
    parser.add_argument("--stt", default=None, choices=["whisper", "parakeet"],
                        help="Speech-to-text backend (default from STT_BACKEND, else whisper)")
    parser.add_argument("--no-keywords", action="store_true",
                        help="Start with keyword highlighting disabled")
    parser.add_argument("--no-recording", action="store_true",
                        help="Do not save transmission audio to recordings/")
    parser.add_argument("--stations", nargs="+", metavar="ICAO",
                        help="Start only these stations, e.g. --stations YBCG YSPT")
    parser.add_argument("--calibrate", default=None, metavar="ICAO",
                        help="Calibrate VAD for a station, e.g. --calibrate YBCG")
    parser.add_argument("--no-military", action="store_true",
                        help="Disable military callsign detection")
    parser.add_argument("--no-adsb", action="store_true",
                        help="Disable live ADS-B aircraft tracking")
    parser.add_argument("--refresh-callsigns", action="store_true",
                        help="Re-scrape the military callsign list and exit")
    args = parser.parse_args()

    console = Console()

    if args.refresh_callsigns:
        result = military_callsigns.get_registry().refresh(force=True)
        console.print(("[green]✅ " if result.ok else "[red]⚠ ") + result.message)
        return

    if args.calibrate:
        icao = args.calibrate.upper()
        matches = [s for s in STREAMS if s["icao"] == icao]
        if not matches:
            console.print(f"[red]Unknown ICAO '{icao}'. Available: {[s['icao'] for s in STREAMS]}[/red]")
            return
        _calibrate(console, matches[0])
        return

    global _backend_name
    _backend_name = args.stt or config.STT_BACKEND
    try:
        backend = transcription.build_backend(_backend_name, args.model)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    model = args.model or (
        WHISPER_MODEL if _backend_name == "whisper" else config.PARAKEET_MODEL
    )

    runtime_config = RuntimeConfig(Path(__file__).parent / "runtime_config.json")
    state = SharedState(keywords_enabled=not args.no_keywords, runtime_config=runtime_config)
    if args.no_military:
        state.set_military(False)

    requested = {s.upper() for s in args.stations} if args.stations else None
    for s in STREAMS:
        state.station_enabled[s["icao"]] = (
            True if requested is None else s["icao"] in requested
        )

    # Pre-display setup (output via console.print is fine here — Live not started yet)
    if config.HUGGINGFACE_TOKEN:
        try:
            from huggingface_hub import login as hf_login
            hf_login(token=config.HUGGINGFACE_TOKEN, add_to_git_credential=False)
        except Exception as exc:
            console.print(f"[yellow]HuggingFace login warning: {exc}[/yellow]")
    else:
        console.print(
            "[yellow]⚠ HUGGINGFACE_TOKEN not set — model download may fail.\n"
            "  Add your token to .env (see .env.example for instructions).[/yellow]"
        )

    _check_prompt_lengths(console)
    backend.ensure_ready(log=lambda msg: console.print(f"[dim]{msg}[/dim]"))

    if args.no_recording:
        state.set_recording(False)

    preflight = _preflight_streams(state, console)

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"atc_{datetime.now(timezone.utc).astimezone(_AEST).strftime('%Y-%m-%d')}.log"

    recordings_dir = Path(__file__).parent / "recordings"
    if state.recording_enabled:
        recordings_dir.mkdir(exist_ok=True)
        _prune_recordings(recordings_dir, config.RECORDING_RETENTION_DAYS, console)

    global _display_ref
    display = LiveDisplay(f"{model}  [{_backend_name}]", state, console)
    state.display = display
    _display_ref = display

    # Blocking HTTP, so it runs before the render loop starts — but after
    # _display_ref is set, so a permissions failure is reported rather than
    # silently swallowed.
    _init_stream_status_messages(state)
    if not args.no_adsb:
        _init_adsb_board_messages(state)

    with display:
        global _discord_outbox, _transcriber_ref, _adsb_poller, _adsb_live_cards
        if config.DISCORD_ENABLED:
            _discord_outbox = DiscordOutbox()
            if config.ADSB_LIVE_CARDS_ENABLED and not args.no_adsb:
                _adsb_live_cards = _AdsbLiveCards()

        kb_thread = threading.Thread(
            target=_keyboard_listener, args=(state,), daemon=True, name="keyboard"
        )
        kb_thread.start()

        if state.military_enabled:
            military_callsigns.start_auto_refresh(
                stop_event=state.stop_event,
                on_result=lambda r: display.log(Text(
                    f"\U0001f6e9 Callsign registry: {r.message}",
                    style="dim cyan" if r.ok else "dim yellow",
                )),
            )

        transcriber = Transcriber(
            backend, state, log_file=log_file, recordings_dir=recordings_dir,
        )
        _transcriber_ref = transcriber

        for stream_cfg in STREAMS:
            t = threading.Thread(
                target=_stream_loop,
                args=(stream_cfg, transcriber, state),
                daemon=True,
                name=f"stream-{stream_cfg['icao']}",
            )
            t.start()
            time.sleep(0.5)

        if config.TELEGRAM_ENABLED:
            tg_cmd_thread = threading.Thread(
                target=_telegram_command_listener,
                args=(state,),
                daemon=True,
                name="telegram-cmd",
            )
            tg_cmd_thread.start()

        if config.DISCORD_ENABLED:
            dc_cmd_thread = threading.Thread(
                target=_discord_command_listener,
                args=(state,),
                daemon=True,
                name="discord-cmd",
            )
            dc_cmd_thread.start()

        # Must start AFTER _discord_outbox exists: without it, _enqueue_discord
        # falls through to a blocking HTTP post on the calling thread, and the
        # calling thread here is the poll loop.
        if config.ADSB_ENABLED and not args.no_adsb:
            _adsb_poller = adsb_tracker.AdsbPoller(
                stop_event=state.stop_event,
                on_event=lambda ev: _send_adsb_event(ev, state),
                on_board=_broadcast_adsb_board,
                on_log=lambda msg, warn=False: display.log(
                    Text(msg, style="dim yellow" if warn else "dim cyan")
                ),
            )
            # With live cards driving Discord, the per-poll position events are
            # redundant — the cards already show current position — so stop the
            # state machine minting them at all.
            _adsb_poller.emit_position_events = _adsb_live_cards is None
            threading.Thread(
                target=_adsb_poller.run, daemon=True, name="adsb-poll"
            ).start()

            if config.ADSB_WEB_ENABLED:
                threading.Thread(
                    target=adsb_web.serve,
                    args=(_adsb_poller, state.stop_event),
                    kwargs={
                        "notes_provider": recent_transcripts,
                        "on_log": lambda msg, warn=False: display.log(
                            Text(msg, style="dim yellow" if warn else "dim cyan")
                        ),
                    },
                    daemon=True,
                    name="adsb-web",
                ).start()
                _announce_map_urls()

        _send_startup_notification(state)
        if config.DISCORD_ENABLED:
            _send_discord_startup(state, backend_name=_backend_name)
            failed = [line for line in preflight if line.startswith("❌")]
            if failed:
                _enqueue_discord(
                    DISCORD_COMMANDS_CHANNEL_ID,
                    content="⚠️ **Stream preflight**\n" + "\n".join(preflight),
                )

        try:
            state.stop_event.wait()
        except KeyboardInterrupt:
            state.stop_event.set()
        finally:
            if _adsb_poller is not None:
                _adsb_poller.shutdown()
                _adsb_poller = None
            transcriber.shutdown()
            if _discord_outbox is not None:
                _discord_outbox.shutdown()
                _discord_outbox = None

    console.print("[dim]Done.[/dim]")


if __name__ == "__main__":
    main()
