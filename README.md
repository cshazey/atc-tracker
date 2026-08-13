# ATC Tracker

Real-time speech-to-text transcription of live ATC audio from [LiveATC.net](https://www.liveatc.net), running locally on Apple Silicon via MLX.

Currently monitoring:
- **YBCG** — Brisbane Centre (Gold Coast)
- **YSPT** — Southport
- **YBBN** — Brisbane Tower

Detects each radio call via voice activity detection, transcribes it with a local model, records the audio, logs it to the terminal, and forwards every transmission to Discord and/or Telegram (dual-send — both can run at once).

Alerts are two-tier: **emergency** terms (MAYDAY, PAN PAN, squawk 7700/7600/7500) ping `@here` in `#alerts`; **interest** terms (display and military callsigns, RESTRICTED, COASTAL) post to `#alerts` without a ping. Both are highlighted in red in the terminal.

Every transcript is also checked against a [scraped register of ADF callsigns](#military-callsign-detection), so "Falcon one one" is flagged as a 6 SQN Growler without anyone having to add FALCON to a keyword list.

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
Model   : mlx-community/whisper-large-v3-turbo  [whisper]
Keywords: ON    Telegram: OFF  (press T to enable)
Discord : ON  (see #commands for commands)
──────────────────────────────────────────────
 [1] YBCG  Brisbane Centre   ON
 [2] YSPT  Southport         ON
 [3] YBBN  Brisbane Tower    ON
──────────────────────────────────────────────
[10:42:31] YBCG Brisbane Centre    │ Connected  https://s1-fmt2.liveatc.net/ybcg3_centre
[10:42:31] YSPT Southport          │ Connected  https://s1-bos.liveatc.net/yspt2
[10:42:38] YBCG Brisbane Centre  (3.2s) │ Golf Bravo Charlie cleared runway two eight
[10:43:01] YSPT Southport        (2.8s) │ Southport traffic, Cessna one seven two, final runway one four
[10:43:15] YBCG Brisbane Centre  (4.1s) │ MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
[10:44:01] YBCG Brisbane Centre  (2.6s) │ Hornet formation track RESTRICTED area seven delta
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
venv/bin/python atc_tracker.py --stations YBCG YSPT   # start with only these stations active
venv/bin/python atc_tracker.py --stt parakeet         # use the Parakeet backend instead of Whisper
venv/bin/python atc_tracker.py --model mlx-community/whisper-tiny-mlx  # faster, less accurate
venv/bin/python atc_tracker.py --no-keywords          # start with highlighting off
venv/bin/python atc_tracker.py --no-recording         # don't save transmission audio
venv/bin/python atc_tracker.py --calibrate YBCG       # print live RMS values for YBCG
venv/bin/python atc_tracker.py --no-military          # disable military callsign detection
venv/bin/python atc_tracker.py --refresh-callsigns    # re-scrape the ADF callsign list and exit
```

---

## Speech-to-text backends

Two local backends ship, both running entirely on-device:

| Backend | Model | Notes |
|---|---|---|
| `whisper` (default) | `mlx-community/whisper-large-v3-turbo` | Accepts a text prompt, which keeps ATC numbers spelled out ("one seven two"). |
| `parakeet` | `mlx-community/parakeet-tdt-0.6b-v3` | NVIDIA Parakeet TDT. A transducer — reads the audio once rather than decoding token-by-token. Having no text prompt, it cannot echo one. |

Select with `--stt` or `STT_BACKEND` in `.env`.

**Which one is better for your feeds is an empirical question.** General-purpose WER leaderboards don't predict which model handles a squelched 8 kHz VHF feed better, so compare them on audio you actually captured:

```bash
venv/bin/python bench_stt.py --out bench.md
```

That runs both backends over everything in `recordings/`, prints transcripts side by side, and reports realtime factor plus how often each backend produced output the quality filters had to reject.

---

## Transcript quality filters

Whisper hallucinates on quiet or noisy audio, and it does so *confidently* — a real prompt-echo loop was measured at `avg_logprob -0.152` (high confidence) with a `compression_ratio` of 14.54. mlx-whisper computes those signals but never rejects on them, so the tracker does:

- **Quality gate** — segments are dropped on `compression_ratio > 2.4`, `avg_logprob < -1.2`, or high `no_speech_prob` paired with a weak logprob.
- **Phrase-repetition filter** — any 1–6 word phrase repeated 4+ times ("turning base, turning base, turning base…").
- **Prompt-echo detector** — output whose 5-grams overlap the station's own prompt by more than half.
- **Filler blocklist** — "Thank you.", "Thanks for watching", and similar subtitle-training artefacts.

Each station has its own short prompt (see `config.py`). **Keep every prompt under 200 tokens.** Whisper's prompt window is 223 tokens and it silently keeps only the *tail* of anything longer — a single 232-token prompt shared across stations once got truncated down to its Southport section, and every station then echoed Southport phraseology into its transcripts. `test_filters.py` asserts the limit:

```bash
venv/bin/python test_filters.py
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
[10:42:38] Golf Bravo Charlie cleared runway two eight

🔴 [ALERT] YBCG Brisbane Centre
[10:44:01] Hornet formation track RESTRICTED area seven delta

🚨 [EMERGENCY] YBCG Brisbane Centre
[10:43:15] MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
```

---

## Discord setup

Full walkthrough, permissions, and a per-channel reference are in **[DISCORD.md](DISCORD.md)**. Short version:

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications), copy its token
2. Invite it to your server (OAuth2 → URL Generator → scope `bot`, permissions View Channel / Send Messages / Embed Links / Read Message History — Embed Links is easy to miss and required, since almost every message is an embed)
3. Create one channel per station + `#alerts` + a private `#commands` channel
4. Copy each channel's ID (enable Developer Mode first) and paste everything into `.env`

Each station posts only its own transmissions to its own channel; alerts mirror into `#alerts`; and every command below works from `#commands`, exactly like the Telegram commands — both platforms stay in sync regardless of which one you send a command from.

| Command | Action |
|---|---|
| `/status` | Station states, keyword mode, stream URLs |
| `/health` | Last audio, last TX, reconnect count, queue depth, dropped count per station |
| `/mute YBCG` · `/unmute YBCG` · `/mute all` | Mute or unmute stations |
| `/url YBCG` · `/urls` | Show active stream URLs |
| `/seturl YBCG https://…` · `/reseturl YBCG` | Pin or restore a stream URL |
| `/reconnect YBCG` | Force a station to redial |
| `/vad YBCG` · `/vad YBCG 0.004` | Show or set a station's VAD threshold |
| `/record on` · `/record off` | Toggle transmission audio recording |
| `/keywords on` · `/keywords off` | Toggle keyword highlighting |
| `/pause` · `/resume` | Suspend or restart transcription |
| `/help` | Command list |

Each station channel also carries a **pinned status message** that is edited in place on connect/disconnect, rather than a new pinned message per event (Discord caps a channel at 50 pins).

### Stream connection and LiveATC edge rotation

LiveATC rotates which edge host (`s1-bos`, `s1-fmt2`, …) serves a given mount, so a hardcoded edge URL goes stale without warning. Each station therefore connects through LiveATC's **redirector** — `http://d.liveatc.net/<mount>` — the same address the site's own `.pls` playlists use. It 302s to whichever edge is currently serving the mount, so rotation is handled automatically. If the redirector itself is unreachable, the tracker falls back through the known edge hosts in turn.

A feed that stops delivering audio without closing its socket is caught by a 20-second stall watchdog, and reconnects use exponential backoff with jitter so three feeds that drop together don't retry in lockstep.

You can still pin a URL when you need to:

**While running, via Discord** — send this in `#commands`:

```text
/seturl YBCG https://s1-fmt2.liveatc.net/ybcg3_centre
```

The station reconnects on the new URL and the override is saved to `runtime_config.json` (local-only, git-ignored). Use `/url YBCG` or `/urls` to check, and `/reseturl YBCG` to return to the redirector default.

**Permanently, via `.env`** — set `STREAM_URL_<ICAO>`. Note that a pinned URL, from either route, disables the automatic edge fallback for that station — pinning means you've said which host to use.

---

## Adding stations

Edit `STREAMS` in `config.py` to add more LiveATC feeds:

```python
STREAMS = [
    {
        "icao": "YBCG",
        "name": "Brisbane Centre",
        "mount": "ybcg3_centre",        # LiveATC mount name; the URL is built from this
        "headers": _HEADERS,
        "prompt": _PROMPT_YBCG,         # keep under 200 tokens — see below
        "vad_threshold": 0.003,
    },
    # Add more here...
]
```

Give each new station its own short `prompt` written in that station's phraseology (a tower prompt for a tower feed, CTAF for a CTAF feed). Do **not** reuse one long prompt across stations — that is exactly the bug described under [Transcript quality filters](#transcript-quality-filters).

Each station streams and transcribes independently in its own thread.

If Discord is enabled, also create a channel for the new station and add its ID as `DISCORD_CHANNEL_<ICAO>` in `.env` (e.g. `DISCORD_CHANNEL_YMML=...`) — see [DISCORD.md](DISCORD.md).

---

## Adding keywords

Keywords are tiered in `config.py`. Emergency terms ping `@here` in `#alerts`; interest terms post there silently:

```python
KEYWORDS_EMERGENCY = [
    "MAYDAY",
    "SQUAWK 7700",
    # …
]

KEYWORDS_INTEREST = [
    "HORNET",
    "ROULETTES",
    "RESTRICTED",
    # add display callsigns here…
]
```

Matching is whole-word and case-insensitive. **Avoid bare numbers** — `"18"` and `"500"` used to live in this list and fired on every "runway 18" and "500 feet", which buries the alerts that matter.

---

## Military callsign detection

`military_callsigns.py` keeps a local register of Australian Defence Force callsigns — around 500 of them, scraped from [swld.com.au](https://www.swld.com.au/pages/aus_raaf_callsigns.htm) — and checks every transcript against it. A hit reports the airframe and unit alongside the transmission:

```
[14:22:07] YBCG Brisbane Centre  (4.2s) │ Falcon one one, Brisbane Centre, climb flight level two four zero
                                  🛩 FALCON One One (EA18G GROWLER — 6 SQN AMBERLEY)
```

The register lives in `data/military_callsigns.db` (SQLite, git-ignored) and a background thread re-scrapes the page once a day. `data/military_callsigns_seed.json` is a committed snapshot, loaded automatically when the database is empty, so detection works on a fresh clone and offline. Refresh by hand with `--refresh-callsigns`, or `/military refresh` from Discord.

**Matching is fuzzy, because Whisper has never been trained on these words** and mangles them — "Falcum 11", "Foulcon one one". Three tiers are tried: exact, bounded Levenshtein (budget scales with callsign length), then a Soundex-style consonant skeleton.

**Hits are strong or weak.** A strong hit raises the transmission to the interest tier and posts to `#alerts`. A weak hit — usually a misheard callsign — only annotates the transcript in its own station channel, marked *[possible]*. That split exists because an unguarded search over 500 callsigns fires on roughly one transmission in twenty-five: `starting 53` becomes STARLING, `Water four zero one` becomes WALER, and every `Golf Kilo Delta` becomes DELTA. Four guards get it down to ~1% of transmissions annotated across the historical logs, most of them genuine:

| Guard | What it stops |
|-------|---------------|
| `MILITARY_CALLSIGN_BLOCKLIST` | Phonetic alphabet, formation colours, and civil types spoken with a number — "Dash 8", "King Air 350", "Baron 58" |
| `MILITARY_CALLSIGN_AMBIGUOUS` | Callsigns that are everyday words (TIGER, REACH, STORM): exact match only, and no alert without military context |
| `data/english_lookalikes.txt` | English words within reach of a callsign are never fuzzy-matched |
| Flight-number rule | An everyday-word callsign must be followed by a number — real traffic says "Falcon one one", not "Falcon" |

Tuning and inspection:

```bash
venv/bin/python military_callsigns.py match "falcum one one, request descent"  # try the matcher
venv/bin/python military_callsigns.py dump --limit 20                          # what's stored
venv/bin/python military_callsigns.py stats                                    # count, last scrape
venv/bin/python military_callsigns.py refresh --force                          # re-scrape now
venv/bin/python military_callsigns.py build-lookalikes                         # after editing thresholds
venv/bin/python test_military.py                                               # regression suite
```

Add a callsign the source page hasn't listed yet — a visiting display team, say — via `MILITARY_EXTRA_CALLSIGNS` in `config.py`. Set `MILITARY_ALERT_ON_FUZZY = True` if you would rather have every near-miss alert; expect roughly one false alert per 100 transmissions. Disable the feature entirely with `MILITARY_DETECTION_ENABLED=0`, `--no-military`, or `/military off`.

A scrape that returns fewer than `MILITARY_MIN_SCRAPE_ROWS` (300) rows is treated as a layout change on the source site and discarded, so the existing register survives the page being restyled or taken down.

---

## Recordings

Every detected transmission is saved to `recordings/YYYY-MM-DD/<ICAO>_<HHMMSS>.wav` (16 kHz mono, pre-processing, so it stays a faithful source for re-transcription). The filename appears in the terminal log line and in the Discord embed footer, so any transcript can be traced back to its audio.

Recordings older than `RECORDING_RETENTION_DAYS` (default 14) are pruned at startup. Disable entirely with `RECORDING_ENABLED=0`, `--no-recording`, or `/record off` at runtime. `recordings/` is git-ignored.

---

## VAD calibration

If you're getting false positives or missing calls, check the live RMS levels:

```bash
venv/bin/python atc_tracker.py --calibrate YBCG
```

Silence should read near `0.000`; transmissions spike above the station's threshold. Adjust that station's `vad_threshold` in `config.py`, or set it live with `/vad YBCG 0.004`.

The gate also tracks a rolling noise floor and requires speech to sit a multiple above it, so a feed that turns hissy mid-event can't latch permanently open.

---

## Configuration reference

| File | What to edit |
|------|-------------|
| `.env` | Telegram credentials |
| `.env` | Discord bot token + channel IDs (see [DISCORD.md](DISCORD.md)) |
| `.env` → `STT_BACKEND` | `whisper` or `parakeet` |
| `.env` → `RECORDING_ENABLED` / `RECORDING_RETENTION_DAYS` | Transmission audio recording |
| `config.py` → `STREAMS` | Add/remove ATC feeds, per-station prompt and VAD threshold |
| `config.py` → `KEYWORDS_EMERGENCY` / `KEYWORDS_INTEREST` | Alert tiers |
| `config.py` → `WHISPER_MODEL` / `PARAKEET_MODEL` | Swap models |
| `config.py` → `MAX_COMPRESSION_RATIO`, `MIN_AVG_LOGPROB` | Transcript quality gate |
| `config.py` → `VAD_SILENCE_HANGOVER` | Silence gap before a TX is considered done (0.7 s) |
| `config.py` → `VAD_PREROLL_SEC` | Audio kept from before VAD trips, so callsigns aren't clipped |
| `config.py` → `STREAM_STALL_TIMEOUT_SEC` | How long a silent connection may sit before reconnecting |
| `config.py` → `MILITARY_CALLSIGN_BLOCKLIST` / `_AMBIGUOUS` | Which callsigns may fire, and how much corroboration they need |
| `config.py` → `MILITARY_MIN_CONFIDENCE` / `MILITARY_ALERT_ON_FUZZY` | How readily a misheard callsign counts |
| `.env` → `MILITARY_DETECTION_ENABLED` / `MILITARY_REFRESH_HOURS` | Military callsign detection on/off and scrape interval |
