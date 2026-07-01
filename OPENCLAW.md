# OPENCLAW Integration Guide — ATC Tracker

This document explains how the openclaw agent can launch and use the ATC Tracker to monitor live ATC radio traffic and forward every transcribed transmission to the user's Telegram chat and/or Discord server.

---

## What the tool does

`atc_tracker.py` streams multiple live ATC feeds from LiveATC.net, detects each radio call via voice activity detection, transcribes it locally with Whisper (Apple Silicon, no external API needed), and:

- Prints every transcription to the terminal log with a timestamp and station label
- Sends every transcription to the configured Telegram chat automatically
- Sends every transcription to that station's own Discord channel automatically (dual-send alongside Telegram — see `DISCORD.md`)
- Marks keyword matches (MILITARY, F-18, MAYDAY, COASTAL, etc.) with a 🔴 KEYWORD ALERT in Telegram, and mirrors the same matches into Discord's `#alerts` channel

Currently monitored stations:
| # | ICAO | Name | Feed URL |
|---|------|------|----------|
| 1 | YBCG | Brisbane Centre | `https://s1-bos.liveatc.net/ybcg3_centre` |
| 2 | YSPT | Southport | `https://s1-bos.liveatc.net/yspt2` |
| 3 | YSSY | Sydney Centre | `https://s1-fmt2.liveatc.net/yssy1_ctr_128600` |
| 4 | YBBN | Brisbane Tower | `https://s1-fmt2.liveatc.net/ybbn7_twr` |

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

DISCORD_BOT_TOKEN=
DISCORD_ALERTS_CHANNEL_ID=
DISCORD_COMMANDS_CHANNEL_ID=
DISCORD_CHANNEL_YBCG=
DISCORD_CHANNEL_YSPT=
DISCORD_CHANNEL_YSSY=
DISCORD_CHANNEL_YBBN=

HUGGINGFACE_TOKEN=
```

`HUGGINGFACE_TOKEN` is required to download Whisper models. Get a free read-only token at https://huggingface.co/settings/tokens.

### One-time Telegram setup (if not already done)

1. Message `@BotFather` on Telegram → send `/newbot` → copy the token
2. Message `@userinfobot` on Telegram → copy the numeric chat ID
3. Send `/start` to the new bot so it can message the user
4. Paste both values into `.env`

### One-time Discord setup (if not already done)

Full walkthrough and a per-channel reference are in `DISCORD.md`. Summary: create a bot + invite it to the server with View Channel / Send Messages / Embed Links / Read Message History (Embed Links is required — nearly every message the bot sends is an embed, and it fails silently-ish without this permission), create one channel per station plus `#alerts` and a private `#commands` channel, copy each channel's ID, paste everything into `.env`.

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
- A Telegram message and a Discord embed (in that station's channel) are sent for every call, regardless of keywords
- Keyword matches get a 🔴 KEYWORD ALERT prefix on Telegram and also mirror into Discord's `#alerts` channel
- Muting/unmuting a station or pausing/resuming the whole tracker (from either platform) posts a status update into that station's Discord channel(s) too

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

**Regular call (Discord embed, posted in `#ybcg-brisbane-center`):**
```
📻 YBCG Brisbane Centre
Golf Bravo Charlie cleared COASTAL two departure runway two eight
14:42:38 AEST / 04:42:38Z
```

**Keyword alert (Discord embed, posted in both the station channel and `#alerts`):**
```
🔴 KEYWORD ALERT — YBCG Brisbane Centre
MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
14:43:15 AEST / 04:43:15Z
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

If Discord is enabled, also create a channel for the new station and set `DISCORD_CHANNEL_<ICAO>` in `.env` (see `DISCORD.md`).

---

## Monitored keywords

These terms trigger a 🔴 KEYWORD ALERT on both Telegram and Discord (mirrored into `#alerts`):

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
| `1` / `2` / `3` … | Mute or unmute that station in real time |
| `K` | Toggle keyword highlighting in terminal (does not affect Telegram/Discord) |
| `T` | Toggle Telegram sending on/off (starts OFF by default) |
| `P` | Pause/resume transcription & forwarding |
| `Q` or `Ctrl+C` | Quit |

Station numbers match the order in the startup list (and the `STREAMS` list in `config.py`). Muting keeps the stream connected but drops transcriptions and Telegram/Discord messages for that feed until unmuted, and posts a status update to that station's Discord channel.

**Telegram defaults to disabled** on every startup, even with valid credentials in `.env` — press `T` to enable it for that session. While disabled, the tracker doesn't poll Telegram's API at all (no outgoing sends, no incoming command polling), so a Telegram-side outage or timeout can't produce log noise unless it's been turned on. Discord is unaffected and follows its own `DISCORD_ENABLED` gate as before.

---

## Troubleshooting

**No Telegram messages:**
- Check `.env` has both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` filled in
- The user must have sent `/start` to the bot at least once
- Telegram starts OFF every session by design — press `T` to enable it, then the startup banner shows `Telegram: ON → chat <id>`

**No Discord messages:**
- Check `.env` has `DISCORD_BOT_TOKEN`, `DISCORD_ALERTS_CHANNEL_ID`, `DISCORD_COMMANDS_CHANNEL_ID`, and the station's `DISCORD_CHANNEL_<ICAO>` filled in
- Startup banner shows `Discord: ON` if credentials loaded correctly
- A `403 Missing Access` in the terminal log means the bot hasn't been invited to the server, or lacks a permission override on that specific channel (common for a private `#commands` channel) — see `DISCORD.md` → Troubleshooting

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
| `.env` | `DISCORD_BOT_TOKEN` | Discord bot token |
| `.env` | `DISCORD_ALERTS_CHANNEL_ID` | Discord `#alerts` channel ID |
| `.env` | `DISCORD_COMMANDS_CHANNEL_ID` | Discord `#commands` channel ID |
| `.env` | `DISCORD_CHANNEL_<ICAO>` | Discord channel ID for that station |
| `config.py` → `STREAMS` | list of dicts | ATC feeds to monitor |
| `config.py` → `KEYWORDS` | list of strings | Terms that trigger 🔴 alerts |
| `config.py` → `WHISPER_MODEL` | string | Whisper model size |
| `config.py` → `VAD_RMS_THRESHOLD` | float | Transmission detection sensitivity |
| `config.py` → `VAD_SILENCE_HANGOVER` | float (seconds) | Silence gap before TX is considered done |
| `config.py` → `MAX_TRANSMISSION_SEC` | int (seconds) | Safety cap on buffer length |
| `config.py` → `RECONNECT_DELAY_SEC` | int (seconds) | Delay before reconnecting after stream error |
