# OPENCLAW Integration Guide — ATC Tracker

This document explains how the openclaw agent can launch and use the ATC Tracker to monitor live ATC radio traffic and forward every transcribed transmission to the user's Telegram chat.

---

## What the tool does

`atc_tracker.py` streams multiple live ATC feeds from LiveATC.net, detects each radio call via voice activity detection, transcribes it locally with Whisper (Apple Silicon, no external API needed), and:

- Prints every transcription to the terminal log with a timestamp and station label
- Sends every transcription to the configured Telegram chat automatically
- Marks keyword matches (MILITARY, F-18, MAYDAY, COASTAL, etc.) with a 🔴 KEYWORD ALERT in Telegram

Currently monitored stations:
| ICAO | Name | Feed URL |
|------|------|----------|
| YBCG | Brisbane Centre | `https://s1-bos.liveatc.net/ybcg3_centre` |
| YSPT | Southport | `https://s1-bos.liveatc.net/yspt2` |

The stream is pre-squelched at source — only actual radio calls produce output.

---

## Credentials — `.env` file

All credentials live in `.env` at the project root. This file is gitignored and never committed.

```
/Users/openclaw/Documents/GitHub/atc-tracker/.env
```

Current contents template (copy from `.env.example`):

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### One-time Telegram setup (if not already done)

1. Message `@BotFather` on Telegram → send `/newbot` → copy the token
2. Message `@userinfobot` on Telegram → copy the numeric chat ID
3. Send `/start` to the new bot so it can message the user
4. Paste both values into `.env`

---

## How to start the tracker

### Option A — double-click (macOS Finder)

Double-click `run.command` in Finder. Terminal opens and the tracker starts.

### Option B — from the terminal

```bash
bash /Users/openclaw/Documents/GitHub/atc-tracker/run.command
```

### Option C — run directly (venv must exist)

```bash
cd /Users/openclaw/Documents/GitHub/atc-tracker
venv/bin/python atc_tracker.py
```

On **first run**, `run.command` automatically creates a venv and installs all dependencies. The Whisper model (~230 MB) downloads on the first transcription and is cached in `~/.cache/huggingface/`.

### Background (headless)

```bash
cd /Users/openclaw/Documents/GitHub/atc-tracker
set -a; source .env; set +a
nohup venv/bin/python atc_tracker.py > atc_tracker.log 2>&1 &
echo "PID: $!"
```

To stop:
```bash
kill <PID>
```

---

## What happens while it runs

- Each station streams independently in its own thread
- Every radio call is transcribed when the transmission ends (silence detected)
- A Telegram message is sent for every call, regardless of keywords
- Keyword matches get a 🔴 KEYWORD ALERT prefix

**Regular call (Telegram):**
```
📻 YBCG Brisbane Centre
[10:42:38] Golf Bravo Charlie cleared COASTAL two departure runway two eight
```

**Keyword alert (Telegram):**
```
🔴 [KEYWORD ALERT] YBCG Brisbane Centre
[10:43:15] MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
```

---

## Adding a new ATC station

Edit `STREAMS` in `config.py`:

```python
STREAMS = [
    {
        "icao": "YBCG",
        "name": "Brisbane Centre",
        "url": "https://s1-bos.liveatc.net/ybcg3_centre",
        "headers": _HEADERS,
    },
    {
        "icao": "YSPT",
        "name": "Southport",
        "url": "https://s1-bos.liveatc.net/yspt2",
        "headers": _HEADERS,
    },
    # paste new station here
]
```

The stream URL comes from the LiveATC feed page — inspect the audio player network requests to find the direct MP3 stream URL.

---

## Monitored keywords

These terms trigger 🔴 KEYWORD ALERT in Telegram:

| Category | Keywords |
|---|---|
| Military | MILITARY, F18, F-18, 18, EIGHTEEN |
| Airspace | COASTAL, RESTRICTED |
| Emergency | EMERGENCY, MAYDAY, PAN-PAN, GUARD |
| Altitude | 500 |
| Squawk codes | SQUAWK 7700, SQUAWK 7600, SQUAWK 7500 |

To add keywords, edit `KEYWORDS` in `config.py`.

---

## Controls (foreground)

| Key | Action |
|-----|--------|
| `K` | Toggle keyword highlighting in terminal (does not affect Telegram) |
| `Q` or `Ctrl+C` | Quit |

---

## Troubleshooting

**No Telegram messages:**
- Check `.env` has both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` filled in
- The user must have sent `/start` to the bot at least once
- Startup banner shows `Telegram: ON → chat <id>` if credentials loaded correctly

**No transcriptions:**
- The stream may simply be quiet — ATC is not always active
- Run `venv/bin/python atc_tracker.py --calibrate YBCG` to confirm audio is flowing

**VAD too sensitive / missing calls:**
- Adjust `VAD_RMS_THRESHOLD` in `config.py` (lower = more sensitive)
- Default `0.003` — use `--calibrate` to see live RMS values

---

## Configuration reference

| Location | Setting | Purpose |
|----------|---------|---------|
| `.env` | `TELEGRAM_BOT_TOKEN` | Telegram bot API token |
| `.env` | `TELEGRAM_CHAT_ID` | Telegram chat/user ID |
| `config.py` → `STREAMS` | list of dicts | ATC feeds to monitor |
| `config.py` → `KEYWORDS` | list of strings | Terms that trigger 🔴 alerts |
| `config.py` → `WHISPER_MODEL` | string | Whisper model size |
| `config.py` → `VAD_RMS_THRESHOLD` | float | Transmission detection sensitivity |
| `config.py` → `VAD_SILENCE_HANGOVER` | float (seconds) | Silence gap before TX is considered done |
| `config.py` → `MAX_TRANSMISSION_SEC` | int (seconds) | Safety cap on buffer length |
| `config.py` → `RECONNECT_DELAY_SEC` | int (seconds) | Delay before reconnecting after stream error |
