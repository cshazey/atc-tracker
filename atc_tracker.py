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
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import miniaudio
import mlx_whisper
import numpy as np
import requests
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
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

_AEST = ZoneInfo("Australia/Brisbane")


def _now_ts() -> str:
    utc = datetime.now(timezone.utc)
    aest = utc.astimezone(_AEST)
    return f"{aest.strftime('%H:%M:%S')} AEST / {utc.strftime('%H:%M:%S')}Z"


# ---------------------------------------------------------------------------
# Shared runtime state
# ---------------------------------------------------------------------------

class SharedState:
    def __init__(self, keywords_enabled: bool):
        self.keywords_enabled = keywords_enabled
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self.station_enabled: dict = {}
        self.display: Optional["LiveDisplay"] = None

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
# Live terminal UI
# ---------------------------------------------------------------------------

class LiveDisplay:
    """Thread-safe live UI: fixed status header + scrolling transmission log.

    All threads call .log(Text) or .refresh(). Rich's Live handles rendering
    on the main thread so output never interleaves.
    """

    def __init__(self, model: str, state: SharedState, console: Console):
        self._model = model
        self._state = state
        self._console = console
        self._lines: collections.deque = collections.deque(maxlen=500)
        self._lock = threading.Lock()
        self._live = Live(
            self._render(),
            console=console,
            auto_refresh=False,
            transient=False,
        )

    # ── Public API (thread-safe) ────────────────────────────────────────────

    def log(self, line: Text) -> None:
        with self._lock:
            self._lines.append(line)
        self._do_refresh()

    def refresh(self) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        self._live.update(self._render())
        self._live.refresh()

    # ── Context manager ─────────────────────────────────────────────────────

    def __enter__(self):
        self._live.__enter__()
        return self

    def __exit__(self, *args):
        self._live.__exit__(*args)

    # ── Rendering ───────────────────────────────────────────────────────────

    def _build_header(self) -> Text:
        h = Text()

        h.append("  Model   : ", style="dim")
        h.append(self._model + "\n", style="cyan")

        h.append("  Keywords: ", style="dim")
        kw = self._state.keywords_enabled
        h.append("ON   " if kw else "OFF  ", style="green" if kw else "yellow")
        h.append("  Telegram: ", style="dim")
        if config.TELEGRAM_ENABLED:
            h.append("ON", style="green")
            h.append(f"  →  {config.TELEGRAM_CHAT_ID}  (/help for commands)\n", style="dim")
        else:
            h.append("OFF\n", style="yellow")

        h.append("  " + "─" * 50 + "\n", style="dim cyan")

        for i, s in enumerate(STREAMS, 1):
            enabled = self._state.is_enabled(s["icao"])
            h.append(f"  [{i}] {s['icao']}  {s['name']:<16}  ")
            h.append("ON   \n" if enabled else "MUTED\n",
                     style="green" if enabled else "red")

        h.append("  " + "─" * 50 + "\n", style="dim cyan")
        h.append("  [K] keywords   [Q] quit", style="dim")

        return h

    def _render(self):
        # Header: 2 border lines + 2 info lines + 2 separators + N station lines + 1 controls line
        header_size = 7 + len(STREAMS)

        header_panel = Panel(
            self._build_header(),
            title="[bold cyan]ATC Tracker[/bold cyan]",
            border_style="cyan",
        )

        term_h = self._console.height or 40
        log_visible = max(3, term_h - header_size - 2)

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

        log_panel = Panel(
            log_text,
            title="[dim]Transmissions[/dim]",
            border_style="dim",
        )

        layout = Layout()
        layout.split_column(
            Layout(header_panel, name="header", size=header_size),
            Layout(log_panel, name="log"),
        )
        return layout


# Module-level display reference used by _post_telegram (set in main).
_display_ref: Optional[LiveDisplay] = None


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

    def __init__(self, model: str, state: SharedState, log_file: Optional[Path] = None):
        self._model = model
        self._state = state
        self._log_file = log_file
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="transcriber")
        self._thread.start()

    def submit(self, audio: np.ndarray, duration: float, icao: str, station_name: str, ts: str) -> bool:
        if duration < self._MIN_DURATION:
            return False
        try:
            self._queue.put_nowait((audio, duration, icao, station_name, ts))
            return True
        except queue.Full:
            self._tui_log(Text(f"⚠ [{icao}] Transcriber busy — dropping TX", style="yellow"))
            return False

    def _tui_log(self, line: Text) -> None:
        if self._state.display:
            self._state.display.log(line)

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            audio, duration, icao, station_name, ts = item
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
                    self._log(text, icao, station_name, ts, duration)
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

    def _log(self, text: str, icao: str, station_name: str, ts: str, duration: float = 0.0):
        dur_str = f"({duration:.1f}s) " if duration > 0 else ""
        line = Text()
        line.append(f"[{ts}] {icao} {station_name:<16} {dur_str}│ ", style="bold green")
        line.append_text(_highlight_keywords(text, KEYWORDS, enabled=self._state.keywords_enabled))
        self._tui_log(line)
        _send_telegram(text, ts, icao, station_name, _has_keywords(text, KEYWORDS))
        if self._log_file:
            try:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(f"{ts} | {icao} | {station_name} | {text}\n")
            except Exception:
                pass

    def shutdown(self):
        self._queue.put(None)
        self._thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

def _kw_pattern(kw: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)


def _has_keywords(text: str, keywords: list) -> bool:
    return any(_kw_pattern(kw).search(text) for kw in keywords)


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

def _post_telegram(message: str) -> None:
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
    except Exception as exc:
        if _display_ref is not None:
            _display_ref.log(Text(f"⚠ Telegram send failed: {exc}", style="dim yellow"))


def _send_telegram(text: str, ts: str, icao: str, station_name: str, has_keywords: bool) -> None:
    label = f"{icao} {station_name}"
    header = (
        f"\U0001f534 <b>[KEYWORD ALERT]</b> {label}"
        if has_keywords
        else f"\U0001f4fb {label}"
    )
    _post_telegram(f"{header}\n<code>[{ts}]</code> {html.escape(text)}")


def _send_startup_notification(state: SharedState) -> None:
    ts = _now_ts()
    lines = [f"\U0001f7e2 <b>ATC Tracker started</b>  <code>[{ts}]</code>", ""]
    for s in STREAMS:
        enabled = state.is_enabled(s["icao"])
        icon = "✅" if enabled else "\U0001f507"
        lines.append(f"{icon} {s['icao']} {s['name']}")
    _post_telegram("\n".join(lines))


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


def _handle_command(text: str, state: SharedState) -> None:
    if not text.startswith("/"):
        return
    parts = text.split(None, 2)
    cmd = parts[0].lower().lstrip("/")
    arg = parts[1].lower() if len(parts) > 1 else ""

    def tui(msg: str, style: str = "dim cyan") -> None:
        if state.display:
            state.display.log(Text(msg, style=style))

    if cmd == "help":
        _post_telegram(
            "\U0001f4e1 <b>ATC Tracker commands</b>\n\n"
            "/status — show all station states\n"
            "/mute 1  or  /mute YBCG — mute a station\n"
            "/unmute 1  or  /unmute YBCG — unmute a station\n"
            "/mute all — mute every station\n"
            "/unmute all — unmute every station\n"
            "/keywords on|off — toggle keyword highlighting\n"
            "/help — this message"
        )

    elif cmd == "status":
        lines = ["\U0001f4e1 <b>Station status</b>"]
        for i, s in enumerate(STREAMS, 1):
            icon = "✅" if state.is_enabled(s["icao"]) else "\U0001f507"
            lines.append(f"{icon} [{i}] {s['icao']} {s['name']}")
        kw = "ON" if state.keywords_enabled else "OFF"
        lines.append(f"\nKeywords: <b>{kw}</b>")
        _post_telegram("\n".join(lines))

    elif cmd in ("mute", "unmute"):
        want_enabled = cmd == "unmute"
        label = "active" if want_enabled else "muted"
        icon = "✅" if want_enabled else "\U0001f507"
        if arg == "all":
            for s in STREAMS:
                state.station_enabled[s["icao"]] = want_enabled
            _post_telegram(f"{icon} All stations {label}")
            tui(f"Telegram → all stations {label}")
            if state.display:
                state.display.refresh()
        else:
            station = _resolve_station(arg)
            if station is None:
                _post_telegram(
                    f"⚠ Unknown station: <code>{arg}</code>\n"
                    f"Use a number (1–{len(STREAMS)}) or ICAO code."
                )
                return
            state.station_enabled[station["icao"]] = want_enabled
            _post_telegram(f"{icon} {station['icao']} {station['name']} — {label}")
            tui(f"Telegram → {station['icao']} {label}")
            if state.display:
                state.display.refresh()

    elif cmd == "keywords":
        if arg in ("on", "off"):
            want = arg == "on"
            with state._lock:
                state.keywords_enabled = want
            kw_label = "ON" if want else "OFF"
            _post_telegram(f"\U0001f50d Keywords: <b>{kw_label}</b>")
            tui(f"Telegram → Keywords {kw_label}")
            if state.display:
                state.display.refresh()
        else:
            _post_telegram("Usage: /keywords on  or  /keywords off")

    else:
        _post_telegram(f"Unknown command: <code>/{cmd}</code>\nSend /help for a list.")


def _telegram_command_listener(state: SharedState) -> None:
    offset = 0
    base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    while not state.stop_event.is_set():
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
                _handle_command(text, state)
        except Exception as exc:
            if not state.stop_event.is_set():
                if state.display:
                    state.display.log(Text(f"⚠ Telegram poll error: {exc}", style="dim yellow"))
                time.sleep(RECONNECT_DELAY_SEC)


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
                    elif ch.isdigit() and ch != "0":
                        idx = int(ch) - 1
                        if idx < len(STREAMS):
                            state.toggle_station(STREAMS[idx]["icao"])
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

def _stream_loop(stream_cfg: dict, transcriber: Transcriber, state: SharedState) -> None:
    icao = stream_cfg["icao"]
    station_name = stream_cfg["name"]
    url = stream_cfg["url"]
    headers = stream_cfg["headers"]

    def tui_log(line: Text) -> None:
        if state.display:
            state.display.log(line)

    while not state.stop_event.is_set():
        source: Optional[LiveATCSource] = None
        try:
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

            ts = _now_ts()
            line = Text()
            line.append(f"[{ts}] {icao} {station_name:<16}  ", style="bold green")
            line.append("│ Connected", style="green")
            tui_log(line)

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
                            ts = _now_ts()
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
                                ts = _now_ts()
                                transcriber.submit(audio, len(audio) / VAD_SAMPLE_RATE, icao, station_name, ts)
                            in_tx = False
                            silence_since = None

        except Exception as exc:
            if not state.stop_event.is_set():
                tui_log(Text(f"⚠ {icao}: {exc}", style="red"))
                tui_log(Text(f"  Reconnecting in {RECONNECT_DELAY_SEC}s…", style="dim"))
                time.sleep(RECONNECT_DELAY_SEC)
        finally:
            if source:
                source.close()


# ---------------------------------------------------------------------------
# Model download check
# ---------------------------------------------------------------------------

def _ensure_model(model: str, console: Console) -> None:
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import disable_progress_bars, enable_progress_bars

    console.print(f"[dim]Checking model {model}...[/dim]")
    try:
        snapshot_download(repo_id=model, local_files_only=True)
        console.print("[green]✓ Model already cached — ready[/green]")
    except Exception:
        console.print(f"[yellow]Downloading {model} (∼480 MB) — please wait…[/yellow]")
        try:
            disable_progress_bars()
            snapshot_download(repo_id=model)
            enable_progress_bars()
            console.print("[green]✓ Model downloaded successfully[/green]")
        except Exception as exc:
            enable_progress_bars()
            console.print(
                f"[red]Download failed: {exc}\n"
                "  Make sure HUGGINGFACE_TOKEN is set in .env and you have internet access.[/red]"
            )


# ---------------------------------------------------------------------------
# Calibrate mode
# ---------------------------------------------------------------------------

def _calibrate(console: Console, stream_cfg: dict) -> None:
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

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ATC Tracker — multi-station live speech-to-text transcription"
    )
    parser.add_argument("--model", default=None, metavar="REPO",
                        help="Whisper model repo")
    parser.add_argument("--no-keywords", action="store_true",
                        help="Start with keyword highlighting disabled")
    parser.add_argument("--stations", nargs="+", metavar="ICAO",
                        help="Start only these stations, e.g. --stations YBCG YSPT")
    parser.add_argument("--calibrate", default=None, metavar="ICAO",
                        help="Calibrate VAD for a station, e.g. --calibrate YBCG")
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

    _ensure_model(model, console)

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"atc_{datetime.now(timezone.utc).astimezone(_AEST).strftime('%Y-%m-%d')}.log"

    global _display_ref
    display = LiveDisplay(model, state, console)
    state.display = display
    _display_ref = display

    with display:
        kb_thread = threading.Thread(
            target=_keyboard_listener, args=(state,), daemon=True, name="keyboard"
        )
        kb_thread.start()

        transcriber = Transcriber(model, state, log_file=log_file)

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

        _send_startup_notification(state)

        try:
            state.stop_event.wait()
        except KeyboardInterrupt:
            state.stop_event.set()
        finally:
            transcriber.shutdown()

    console.print("[dim]Done.[/dim]")


if __name__ == "__main__":
    main()
