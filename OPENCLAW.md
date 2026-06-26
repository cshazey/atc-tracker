# OPENCLAW Integration Guide — ATC Tracker

This document explains how the openclaw agent can launch and utilise the ATC Tracker to monitor Brisbane Centre (YBCG) radio traffic and forward every transcribed transmission to the user's Telegram chat.

---

## What the tool does

`atc_tracker.py` streams the YBCG Brisbane Centre live ATC feed from LiveATC.net, detects each radio call via voice activity detection, transcribes it locally with Whisper (Apple Silicon, no API key needed), and:

- Prints every transcription to the terminal log with a timestamp
- Sends every transcription to the configured Telegram chat automatically
- Highlights and marks keyword matches (MILITARY, F-18, MAYDAY, COASTAL, etc.) with a 🔴 KEYWORD ALERT in Telegram

The stream is pre-squelched at source, so only actual radio calls produce output — there is no background noise chatter.

---

## Before running — one-time Telegram setup

The user needs a Telegram bot token and their chat ID configured. This only needs to be done once.

### Step 1 — create a bot (if not already done)

1. Open Telegram and message `@BotFather`
2. Send `/newbot` and follow the prompts
3. Copy the bot token (format: `123456789:ABCdef...`)

### Step 2 — get the user's chat ID

1. Message `@userinfobot` on Telegram — it replies with your chat ID (a number like `987654321`)

### Step 3 — configure the tracker

Set the credentials as environment variables before running:

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token_here"
export TELEGRAM_CHAT_ID="your_chat_id_here"
```

Or edit `config.py` directly and hardcode the values:

```python
TELEGRAM_BOT_TOKEN = "your_bot_token_here"
TELEGRAM_CHAT_ID = "your_chat_id_here"
```

---

## How to start the tracker

```bash
cd /Users/openclaw/Documents/GitHub/atc-tracker
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python3 atc_tracker.py
```

On first run, the Whisper model downloads automatically (~230 MB, cached after that). The tracker then connects to the stream and begins monitoring.

To run it silently in the background (no terminal needed):

```bash
nohup python3 atc_tracker.py > atc_tracker.log 2>&1 &
echo "PID: $!"
```

To stop a background run:

```bash
kill <PID>
```

---

## What happens while it runs

- Every radio call is transcribed when the transmission ends (silence detected)
- A Telegram message is sent to the user's chat for every call
- Keyword matches get a 🔴 alert prefix in the Telegram message

**Regular call (Telegram message):**
```
📻 YBCG Brisbane Centre
[10:42:38] Golf Bravo Charlie cleared COASTAL two departure runway two eight
```

**Keyword alert (Telegram message):**
```
🔴 [KEYWORD ALERT] YBCG Brisbane Centre
[10:43:15] MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
```

---

## Monitored keywords

The following terms trigger a 🔴 KEYWORD ALERT in the Telegram message:

| Category | Keywords |
|---|---|
| Military aircraft | MILITARY, F18, F-18, 18, EIGHTEEN |
| Restricted airspace | COASTAL, RESTRICTED |
| Emergency | EMERGENCY, MAYDAY, PAN-PAN, GUARD |
| Altitude | 500 |
| Emergency squawk codes | SQUAWK 7700, SQUAWK 7600, SQUAWK 7500 |

To add or remove keywords, edit the `KEYWORDS` list in `config.py`.

---

## Controls (when running in foreground)

| Key | Action |
|-----|--------|
| `K` | Toggle keyword highlighting in terminal (does not affect Telegram) |
| `Q` or `Ctrl+C` | Quit |

---

## Troubleshooting

**No Telegram messages arriving:**
- Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set correctly
- Make sure the user has started a conversation with the bot first (send `/start` to the bot)
- The startup banner shows `Telegram: ON → chat <id>` if credentials are loaded correctly

**No transcriptions appearing:**
- The stream may be quiet (YBCG is not always busy)
- Run `python3 atc_tracker.py --calibrate` to check live RMS levels and confirm the stream is flowing

**VAD is too sensitive / missing calls:**
- Adjust `VAD_RMS_THRESHOLD` in `config.py` (lower = more sensitive, higher = less sensitive)
- Default is `0.003` — run `--calibrate` to see the actual RMS values for this stream

---

## Configuration reference (`config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `STREAM_URL` | YBCG feed | URL of the LiveATC MP3 stream |
| `WHISPER_MODEL` | `whisper-small.en` | Whisper model (tiny.en = faster, large = more accurate) |
| `VAD_RMS_THRESHOLD` | `0.003` | Minimum RMS to count as a transmission |
| `VAD_SILENCE_HANGOVER` | `1.5s` | Silence after a TX before it's considered finished |
| `MAX_TRANSMISSION_SEC` | `60s` | Safety cap — flush buffer if a TX runs this long |
| `KEYWORDS` | see list above | Terms that trigger 🔴 alerts |
| `TELEGRAM_BOT_TOKEN` | env var | Telegram bot API token |
| `TELEGRAM_CHAT_ID` | env var | Telegram chat/user ID to send messages to |
| `TELEGRAM_ENABLED` | auto | `True` when both token and chat ID are set |
