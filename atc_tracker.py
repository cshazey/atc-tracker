#!/usr/bin/env python3
"""
ATC Tracker — multi-station live speech-to-text transcription.
Streams MP3 audio from LiveATC.net, detects transmissions via VAD,
transcribes with Whisper, highlights configurable keywords, and forwards
every transmission to Telegram when credentials are configured.

Controls:
  1/2/3/… — toggle individual stations on/off
  K       — toggle keyword highlighting on/off
  Q / Ctrl+C — quit
"""

import argparse
import collections
import html
import queue
import threading
import time
from datetime import datetime
from typing import Optional

import miniaudio
import mlx_whisper
import numpy as np
import requests
from rich.console import Console
from rich.text import Text

import config
from config import (
    KEYWORDS,
    MAX_TRANSMISSION_SEC,
    RECONNECT_DELAY_SEC,
    STREAMS,
    VAD_CHUNK_FRAMES,
    VAD_RMS_THRESHOLD,
    VAD_SAMPLE_RATE,
    VAD_SILENCE_HANGOVER,
    WHISPER_MODEL,
)


# ---------------------------------------------------------------------------
# Shared runtime state
# ---------------------------------------------------------------------------

class SharedState:
    def __init__(self, keywords_enabled: bool):
        self.keywords_enabled = keywords_enabled
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        # icao -> bool; all enabled by default, populated by main()
        self.station_enabled: dict = {}

    def toggle_keywords(self) -> bool:
        with self._lock:
            self.keywords_enabled = not self.keywords_enabled
            return self.keywords_enabled

    def toggle_station(self, icao: str) -> bool:
        with self._lock:
            self.station_enabled[icao] = not self.station_enabled.get(icao, True)
            return self.station_enabled[icao]

    def is_enabled(self, icao: str) -> bool:
        return self.station_enabled.get(icao, True)


# ---------------------------------------------------------------------------
# HTTP stream source for miniaudio
# ---------------------------------------------------------------------------

class LiveATCSource(miniaudio.StreamableSource):
    _BUFFER_MAX = 65536

    def __init__(self, url: str, headers: dict):
        self._deque: collections.deque = collections.deque()
        self._deque_bytes = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._error: Optional[Exception] = None
        self._thread = threading.Thread(target=self._fetch, args=(url, headers), daemon=True)
        self._thread.start()

    def _fetch(self, url: str, headers: dict):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=4096):
                    if self._stop.is_set():
                        return
                    if not chunk:
                        continue
                    with self._cond:
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
                while not self._deque and not self._stop.is_set() and self._error is None:
                    self._cond.wait(timeout=1.0)
                if self._error is not None:
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
    def __init__(self, threshold: float = VAD_RMS_THRESHOLD):
        self.threshold = threshold

    def is_speech(self, chunk: np.ndarray) -> bool:
        return float(np.sqrt(np.mean(chunk ** 2))) > self.threshold


# ---------------------------------------------------------------------------
# Transmission audio buffer
# ---------------------------------------------------------------------------

class TransmissionBuffer:
    def __init__(self, sample_rate: int = VAD_SAMPLE_RATE):
        self._chunks: list = []
        self._sample_rate = sample_rate

    def append(self, chunk: np.ndarray):
        self._chunks.append(chunk)

    def flush(self) -> np.ndarray:
        if not self._chunks:
            return np.array([], dtype=np.float32)
        audio = np.concatenate(self._chunks)
        self._chunks = []
        return audio

    @property
    def duration_seconds(self) -> float:
        return sum(len(c) for c in self._chunks) / self._sample_rate


# ---------------------------------------------------------------------------
# Transcriber thread
# ---------------------------------------------------------------------------

class Transcriber:
    _MIN_DURATION = 0.5

    def __init__(self, model: str, state: SharedState, console: Console):
        self._model = model
        self._state = state
        self._console = console
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="transcriber")
        self._thread.start()

    def submit(self, audio: np.ndarray, duration: float, icao: str, station_name: str, ts: str) -> bool:
        if duration < self._MIN_DURATION:
            return False
        try:
            self._queue.put_nowait((audio, icao, station_name, ts))
            return True
        except queue.Full:
            self._console.print(f"[yellow]⚠ [{icao}] Transcriber busy — dropping TX[/yellow]")
            return False

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            audio, icao, station_name, ts = item
            try:
                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=self._model,
                    language="en",
                    verbose=False,
                    temperature=0.0,
                    no_speech_threshold=0.6,
                )
                text = (result.get("text") or "").strip()
                if text:
                    self._log(text, icao, station_name, ts)
            except Exception as exc:
                msg = str(exc)
                if "401" in msg or "authentication" in msg.lower() or "username or password" in msg.lower():
                    self._console.print(
                        "[red]HuggingFace authentication required to download the Whisper model.\n"
                        "  1. Sign up free at https://huggingface.co\n"
                        "  2. Create a token at https://huggingface.co/settings/tokens\n"
                        "  3. Add HUGGINGFACE_TOKEN=your_token to your .env file and restart.[/red]"
                    )
                else:
                    self._console.print(f"[red]Transcription error [{icao}]: {exc}[/red]")
            finally:
                self._queue.task_done()

    def _log(self, text: str, icao: str, station_name: str, ts: str):
        prefix = Text(f"[{ts}] {icao} {station_name:<18} │ ", style="bold green")
        content = _highlight_keywords(text, KEYWORDS, enabled=self._state.keywords_enabled)
        self._console.print(Text.assemble(prefix, content))
        _send_telegram(text, ts, icao, station_name, _has_keywords(text, KEYWORDS))

    def shutdown(self):
        self._queue.put(None)
        self._thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

def _has_keywords(text: str, keywords: list) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _highlight_keywords(text: str, keywords: list, enabled: bool) -> Text:
    if not enabled:
        return Text(text)

    lower = text.lower()
    spans: list = []
    for kw in keywords:
        kw_lower = kw.lower()
        start = 0
        while True:
            idx = lower.find(kw_lower, start)
            if idx == -1:
                break
            spans.append((idx, idx + len(kw)))
            start = idx + 1

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

def _post_telegram(message: str) -> None:
    """Low-level send — all Telegram calls go through here."""
    if not config.TELEGRAM_ENABLED:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception:
        pass


def _send_telegram(text: str, ts: str, icao: str, station_name: str, has_keywords: bool) -> None:
    label = f"{icao} {station_name}"
    header = (
        f"🔴 <b>[KEYWORD ALERT]</b> {label}"
        if has_keywords
        else f"📻 {label}"
    )
    _post_telegram(f"{header}\n<code>[{ts}]</code> {html.escape(text)}")


def _send_startup_notification(state: SharedState) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    lines = [f"🟢 <b>ATC Tracker started</b>  <code>[{ts}]</code>", ""]
    for s in STREAMS:
        enabled = state.is_enabled(s["icao"])
        icon = "✅" if enabled else "🔇"
        lines.append(f"{icon} {s['icao']} {s['name']}")
    _post_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# Station status display
# ---------------------------------------------------------------------------

def _print_station_status(state: SharedState, console: Console):
    lines = ["[bold cyan]── Station status ──────────────────────────[/bold cyan]"]
    for i, s in enumerate(STREAMS, 1):
        enabled = state.is_enabled(s["icao"])
        status = "[green]ON   [/green]" if enabled else "[red]MUTED[/red]"
        lines.append(f"  [{i}] {s['icao']}  {s['name']:<22} {status}")
    lines.append("[bold cyan]────────────────────────────────────────────[/bold cyan]")
    console.print("\n".join(lines))


# ---------------------------------------------------------------------------
# Keyboard listener
# ---------------------------------------------------------------------------

def _keyboard_listener(state: SharedState, console: Console):
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
                        enabled = state.toggle_keywords()
                        label = "ON" if enabled else "OFF"
                        console.print(f"[cyan]── Keywords: {label} ──[/cyan]")
                    elif ch.isdigit() and ch != "0":
                        idx = int(ch) - 1
                        if idx < len(STREAMS):
                            state.toggle_station(STREAMS[idx]["icao"])
                            _print_station_status(state, console)
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

def _stream_loop(stream_cfg: dict, transcriber: Transcriber, state: SharedState, console: Console):
    icao = stream_cfg["icao"]
    station_name = stream_cfg["name"]
    url = stream_cfg["url"]
    headers = stream_cfg["headers"]

    while not state.stop_event.is_set():
        source: Optional[LiveATCSource] = None
        try:
            console.print(f"[dim]Connecting to {icao} {station_name}...[/dim]")
            source = LiveATCSource(url, headers)

            pcm_gen = miniaudio.stream_any(
                source,
                source_format=miniaudio.FileFormat.MP3,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=VAD_SAMPLE_RATE,
                frames_to_read=VAD_CHUNK_FRAMES,
            )

            vad = VoiceActivityDetector()
            buf = TransmissionBuffer()
            in_tx = False
            silence_since: Optional[float] = None

            ts = datetime.now().strftime("%H:%M:%S")
            console.print(f"[green][{ts}] {icao} {station_name} — connected[/green]")

            for raw_chunk in pcm_gen:
                if state.stop_event.is_set():
                    break

                chunk = np.frombuffer(bytes(raw_chunk), dtype=np.int16).astype(np.float32) / 32768.0

                if vad.is_speech(chunk):
                    in_tx = True
                    silence_since = None
                    buf.append(chunk)
                    if buf.duration_seconds > MAX_TRANSMISSION_SEC:
                        audio = buf.flush()
                        if state.is_enabled(icao):
                            ts = datetime.now().strftime("%H:%M:%S")
                            transcriber.submit(audio, len(audio) / VAD_SAMPLE_RATE, icao, station_name, ts)
                else:
                    if in_tx:
                        buf.append(chunk)
                        now = time.monotonic()
                        if silence_since is None:
                            silence_since = now
                        elif now - silence_since >= VAD_SILENCE_HANGOVER:
                            audio = buf.flush()
                            if state.is_enabled(icao):
                                ts = datetime.now().strftime("%H:%M:%S")
                                transcriber.submit(audio, len(audio) / VAD_SAMPLE_RATE, icao, station_name, ts)
                            in_tx = False
                            silence_since = None

        except Exception as exc:
            if not state.stop_event.is_set():
                console.print(f"[red]{icao} stream error: {exc}[/red]")
                console.print(f"[dim]Reconnecting {icao} in {RECONNECT_DELAY_SEC}s...[/dim]")
                time.sleep(RECONNECT_DELAY_SEC)
        finally:
            if source:
                source.close()


# ---------------------------------------------------------------------------
# Calibrate mode
# ---------------------------------------------------------------------------

def _calibrate(console: Console, stream_cfg: dict):
    icao = stream_cfg["icao"]
    console.print(
        f"[cyan]Calibrate — {icao} {stream_cfg['name']} — printing live RMS for 15s.\n"
        "Silence ≈ 0.000; transmissions spike above 0.003. Ctrl+C to stop.[/cyan]"
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
            flag = " ← TX" if rms > VAD_RMS_THRESHOLD else ""
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

def main():
    parser = argparse.ArgumentParser(
        description="ATC Tracker — multi-station live speech-to-text transcription"
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="REPO",
        help="Whisper model repo (default: mlx-community/whisper-small.en)",
    )
    parser.add_argument(
        "--no-keywords",
        action="store_true",
        help="Start with keyword highlighting disabled",
    )
    parser.add_argument(
        "--stations",
        nargs="+",
        metavar="ICAO",
        help="Start only these stations, e.g. --stations YBCG YSPT",
    )
    parser.add_argument(
        "--calibrate",
        default=None,
        metavar="ICAO",
        help="Calibrate VAD for a station, e.g. --calibrate YBCG",
    )
    args = parser.parse_args()

    console = Console()

    if args.calibrate:
        icao = args.calibrate.upper()
        matches = [s for s in STREAMS if s["icao"] == icao]
        if not matches:
            console.print(f"[red]Unknown ICAO '{icao}'. Available: {[s['icao'] for s in STREAMS]}[/red]")
            return
        _calibrate(console, matches[0])
        return

    model = args.model or WHISPER_MODEL

    state = SharedState(keywords_enabled=not args.no_keywords)

    # Initialise per-station enabled flags
    requested = {s.upper() for s in args.stations} if args.stations else None
    for s in STREAMS:
        state.station_enabled[s["icao"]] = (
            True if requested is None else s["icao"] in requested
        )

    # HuggingFace login — needed to download Whisper models
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

    tg_status = (
        f"[green]ON[/green] → chat {config.TELEGRAM_CHAT_ID}"
        if config.TELEGRAM_ENABLED
        else "[yellow]OFF[/yellow]  (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable)"
    )

    console.rule("[bold]ATC Tracker[/bold]")
    console.print(f"Model   : [cyan]{model}[/cyan]  (downloads ~230 MB on first run)")
    console.print(f"Keywords: [cyan]{'OFF' if args.no_keywords else 'ON'}[/cyan]  (press K to toggle)")
    console.print(f"Telegram: {tg_status}")
    console.print("")
    console.print("Stations — press number to mute/unmute:")
    for i, s in enumerate(STREAMS, 1):
        enabled = state.is_enabled(s["icao"])
        status = "[green]ON [/green]" if enabled else "[red]OFF[/red]"
        console.print(f"  [{i}] {s['icao']} {s['name']:<20} {status}")
    console.print("")
    console.print("  [K] toggle keywords   [Q] quit")
    console.rule()

    kb_thread = threading.Thread(
        target=_keyboard_listener, args=(state, console), daemon=True, name="keyboard"
    )
    kb_thread.start()

    transcriber = Transcriber(model, state, console)

    for i, stream_cfg in enumerate(STREAMS):
        t = threading.Thread(
            target=_stream_loop,
            args=(stream_cfg, transcriber, state, console),
            daemon=True,
            name=f"stream-{stream_cfg['icao']}",
        )
        t.start()
        time.sleep(0.5)  # stagger starts so connection messages don't overlap

    _send_startup_notification(state)

    try:
        state.stop_event.wait()
    except KeyboardInterrupt:
        state.stop_event.set()
    finally:
        console.print("[dim]Shutting down...[/dim]")
        transcriber.shutdown()
        console.print("[dim]Done.[/dim]")


if __name__ == "__main__":
    main()
