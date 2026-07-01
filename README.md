# ATC Tracker

Real-time speech-to-text transcription of live ATC audio from [LiveATC.net](https://www.liveatc.net), running locally on Apple Silicon via MLX Whisper.

Currently monitoring:
- **YBCG** — Brisbane Centre (Gold Coast)
- **YSPT** — Southport
- **YSSY** — Sydney Centre (128.600)
- **YBBN** — Brisbane Tower

Detects each radio call via voice activity detection, transcribes it, logs it to the terminal, and optionally forwards every transmission to Telegram and/or Discord (dual-send — both can run at once). Keywords like MILITARY, MAYDAY, F-18 etc. are highlighted in red and flagged as alerts on both platforms.

---

## Requirements

- macOS on Apple Silicon (M1/M2/M3/M4)
- Python 3.10+

---

## Setup

### 1. Configure credentials

Copy `.env.example` to `.env` and fill in your details:

```bash
cp .env.example .env
```

Then open `.env` and set:

```
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=987654321

DISCORD_BOT_TOKEN=...
DISCORD_ALERTS_CHANNEL_ID=...
DISCORD_COMMANDS_CHANNEL_ID=...
DISCORD_CHANNEL_YBCG=...
DISCORD_CHANNEL_YSPT=...
DISCORD_CHANNEL_YSSY=...
DISCORD_CHANNEL_YBBN=...

HUGGINGFACE_TOKEN=hf_...
```

**HuggingFace token** is required to download the Whisper model. Get a free one at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) (read-only access is enough).

> Telegram and Discord are both optional — the tracker logs to terminal without either. See [Telegram setup](#telegram-setup) and [Discord setup](#discord-setup) below.

### 2. Run

**Double-click `run.command`** in Finder — Terminal opens and it starts automatically.

Or from the command line:

```bash
bash run.command
```

On **first run** it creates a virtual environment and installs all dependencies automatically. After that it starts immediately.

---

## Terminal output

```
──────────────── ATC Tracker ────────────────
Stations: YBCG Brisbane Centre  |  YSPT Southport
Model   : mlx-community/whisper-small.en
Keywords: ON  (press K to toggle)
Telegram: OFF  (press T to enable)
Discord : ON  (see #commands for commands)
──────────────────────────────────────────────
[10:42:31] YBCG Brisbane Centre    │ Connected
[10:42:31] YSPT Southport          │ Connected
[10:42:38] YBCG Brisbane Centre    │ Golf Bravo Charlie cleared COASTAL two departure runway two eight
[10:43:01] YSPT Southport          │ Southport traffic, Cessna 172 final runway one four
[10:43:15] YBCG Brisbane Centre    │ MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
[10:44:01] YBCG Brisbane Centre    │ F-18 formation track RESTRICTED area seven delta
```

Keywords appear in **bold red** inline. All transmissions are always logged — keyword highlighting is cosmetic only.

---

## Controls

| Key | Action |
|-----|--------|
| `1` / `2` / `3` … | Mute or unmute that station (shown in startup list) |
| `K` | Toggle keyword highlighting on/off (does not affect Telegram) |
| `T` | Toggle Telegram sending on/off |
| `P` | Pause/resume transcription & forwarding |
| `Q` or `Ctrl+C` | Quit |

Muting a station keeps the stream connected but discards transcriptions and skips Telegram/Discord for that feed. Unmuting resumes immediately — no reconnect needed.

**Telegram starts disabled by default** even when `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are configured — press `T` to turn it on for the session. This also stops the tracker from polling Telegram at all while off, so a flaky Telegram connection can't spam the terminal with timeout warnings when you don't need it. Discord is unaffected by this toggle.

---

## Options

Pass flags after `atc_tracker.py` by editing the last line of `run.command`, or run directly:

```bash
venv/bin/python atc_tracker.py --stations YBCG YSPT          # start with only these stations active
venv/bin/python atc_tracker.py --model mlx-community/whisper-tiny-mlx  # faster, less accurate
venv/bin/python atc_tracker.py --no-keywords                            # start with highlighting off
venv/bin/python atc_tracker.py --calibrate YBCG                        # print live RMS values for YBCG
venv/bin/python atc_tracker.py --calibrate YSSY                        # print live RMS values for YSSY
```

---

## Telegram setup

1. Message `@BotFather` on Telegram → send `/newbot` → copy the token
2. Message `@userinfobot` on Telegram → copy your numeric chat ID
3. Send `/start` to your new bot (so it can message you)
4. Paste both values into `.env`

The tracker sends every transcription to your chat. Keyword matches get a 🔴 prefix:

```
📻 YBCG Brisbane Centre
[10:42:38] Golf Bravo Charlie cleared COASTAL two departure runway two eight

🔴 [KEYWORD ALERT] YBCG Brisbane Centre
[10:43:15] MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
```

---

## Discord setup

Full walkthrough, permissions, and a per-channel reference are in **[DISCORD.md](DISCORD.md)**. Short version:

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications), copy its token
2. Invite it to your server (OAuth2 → URL Generator → scope `bot`, permissions View Channel / Send Messages / Embed Links / Read Message History — Embed Links is easy to miss and required, since almost every message is an embed)
3. Create one channel per station + `#alerts` + a private `#commands` channel
4. Copy each channel's ID (enable Developer Mode first) and paste everything into `.env`

Each station posts only its own transmissions to its own channel; keyword matches also mirror into `#alerts`; `/mute`, `/unmute`, `/pause`, `/resume`, `/keywords`, `/status`, `/help` all work from `#commands`, exactly like the Telegram commands below — both platforms stay in sync regardless of which one you send a command from.

---

## Adding stations

Edit `STREAMS` in `config.py` to add more LiveATC feeds:

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
    # Add more here...
]
```

Each station streams and transcribes independently in its own thread.

If Discord is enabled, also create a channel for the new station and add its ID as `DISCORD_CHANNEL_<ICAO>` in `.env` (e.g. `DISCORD_CHANNEL_YMML=...`) — see [DISCORD.md](DISCORD.md).

---

## Adding keywords

Edit the `KEYWORDS` list in `config.py`:

```python
KEYWORDS = [
    "MILITARY",
    "MAYDAY",
    # add your own here...
]
```

---

## VAD calibration

If you're getting false positives or missing calls, check the live RMS levels:

```bash
venv/bin/python atc_tracker.py --calibrate YBCG
```

Silence should read near `0.000`; transmissions spike above `0.003`. Adjust `VAD_RMS_THRESHOLD` in `config.py` accordingly.

---

## Configuration reference

| File | What to edit |
|------|-------------|
| `.env` | Telegram credentials |
| `.env` | Discord bot token + channel IDs (see [DISCORD.md](DISCORD.md)) |
| `config.py` → `STREAMS` | Add/remove ATC feeds |
| `config.py` → `KEYWORDS` | Add/remove flagged keywords |
| `config.py` → `WHISPER_MODEL` | Swap Whisper model size |
| `config.py` → `VAD_RMS_THRESHOLD` | Tune sensitivity |
| `config.py` → `VAD_SILENCE_HANGOVER` | Silence gap before TX is considered done |
