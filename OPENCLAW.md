# OPENCLAW Integration Guide — ATC Tracker

This document explains how the openclaw agent can launch and use the ATC Tracker to monitor live ATC radio traffic and forward every transcribed transmission to the user's Telegram chat and/or Discord server.

---

## What the tool does

`atc_tracker.py` streams multiple live ATC feeds from LiveATC.net, detects each radio call via voice activity detection, transcribes it locally on Apple Silicon (no external API needed), and:

- Prints every transcription to the terminal log with a timestamp and station label
- Saves the transmission audio to `recordings/YYYY-MM-DD/<ICAO>_<HHMMSS>.wav`
- Sends every transcription to that station's own Discord channel (see `DISCORD.md`)
- Sends every transcription to the configured Telegram chat when Telegram is enabled
- Mirrors keyword matches into Discord's `#alerts` channel, in two tiers: emergency terms (MAYDAY, PAN PAN, squawk 7700/7600/7500) ping `@here`; interest terms (display/military callsigns, RESTRICTED, COASTAL) post without a ping
- Separately tracks live ADS-B aircraft positions around the Gold Coast and posts military/airshow movements to their own channels (see "Live ADS-B tracking" below)

Currently monitored stations:
| # | ICAO | Name | LiveATC mount |
|---|------|------|----------|
| 1 | YBCG | Brisbane Centre | `ybcg3_centre` |
| 2 | YBCG_TWR | Gold Coast Ground 121.800 / Tower 118.700 | `ybcg3_gnd_twr` |
| 3 | YSPT | Southport | `yspt2` |
| 4 | YBBN | Brisbane Tower | `ybbn7_twr` |

`YBCG` and `YBCG_TWR` are different frequencies despite the shared prefix: the first is Brisbane Centre sector audio hosted under a `ybcg` mount, the second is the aerodrome's own ground and tower — the pair airshow display aircraft actually work.

Each station connects through LiveATC's redirector (`http://d.liveatc.net/<mount>`), which resolves to whichever edge host currently serves that mount, so ordinary edge rotation no longer breaks a feed. If the redirector is unreachable the tracker falls back through the known edge hosts.

The streams are pre-squelched at source — only actual radio calls produce output.

**Speech-to-text backend** is selectable: `whisper` (mlx-whisper large-v3-turbo, default) or `parakeet` (NVIDIA Parakeet TDT via parakeet-mlx). Set `STT_BACKEND` in `.env` or pass `--stt`. Compare them on recorded audio with `venv/bin/python bench_stt.py`.

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
DISCORD_CHANNEL_YBBN=

STREAM_URL_YBCG=
STREAM_URL_YSPT=
STREAM_URL_YBBN=

STT_BACKEND=whisper
RECORDING_ENABLED=1
RECORDING_RETENTION_DAYS=14

HUGGINGFACE_TOKEN=
```

`STREAM_URL_<ICAO>` is optional and normally unnecessary — the redirector handles edge rotation on its own. Set one only to pin a specific URL; doing so disables the automatic edge fallback for that station. A live Discord `/seturl` override (see below) takes precedence over both while active.

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

On **first run**, `run.command` automatically creates a venv and installs all dependencies. The model (~1.6 GB for Whisper large-v3-turbo, ~600 MB for Parakeet) downloads at startup and is cached in `~/.cache/huggingface/`.

`run.command` is a supervisor: it starts the tracker as a child, watches `origin/main` once a minute, and when new commits land it pulls, posts a summary to `#commands`, and restarts the tracker — while the supervisor itself keeps running, so a bad commit can never leave a launcher that will not start. A failed pull (dirty tree, diverged branch) leaves the running tracker alone. It also restarts the tracker on a crash with a 10-second backoff. Use `AUTO_UPDATE=0 bash run.command` to supervise without pulling, or `NO_SUPERVISOR=1 bash run.command` to run the tracker directly.

Startup runs a preflight check against all three feeds and prints ✓/✗ per station before the UI takes over the terminal, so a dead feed is visible immediately rather than halfway through an event.

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
- Every radio call is transcribed when the transmission ends (0.7 s of silence), capped at 25 s per transmission
- Transcripts pass a quality gate and hallucination filter before being sent anywhere; rejected ones are counted under `gated` in `/health`
- The audio is saved to `recordings/YYYY-MM-DD/<ICAO>_<HHMMSS>.wav` and named in the log line and Discord embed footer
- A Discord embed goes to that station's channel for every call, regardless of keywords; Telegram too when enabled
- Emergency-tier matches also post to `#alerts` with an `@here` ping; interest-tier matches post there silently
- Muting/unmuting a station or pausing/resuming the whole tracker (from either platform) posts a status update into that station's Discord channel(s) too
- Each station channel carries one pinned status message, edited in place on connect/disconnect
- Independently of the audio, an `adsb-poll` thread queries adsb.fi every 10 s for aircraft within 60 nm of YBCG, classifies them, and posts military/display movements to `#mil-tracker` and Gold Coast Airport movements to `#gc-flights`, plus a pinned live board refreshed every 30 s

**Regular call (Telegram):**
```
📻 YBCG Brisbane Centre
[10:42:38] Golf Bravo Charlie cleared runway two eight
```

**Interest alert (Telegram):**
```
🔴 [ALERT] YBCG Brisbane Centre
[10:44:01] Hornet formation track RESTRICTED area seven delta
```

**Emergency alert (Telegram):**
```
🚨 [EMERGENCY] YBCG Brisbane Centre
[10:43:15] MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
```

**Regular call (Discord embed, posted in `#ybcg-brisbane-center`):**
```
📻 YBCG Brisbane Centre
Golf Bravo Charlie cleared runway two eight
14:42:38 AEST / 04:42:38Z · YBCG_144238.wav
```

**Emergency alert (Discord, posted in both the station channel and `#alerts`):**
```
@here **MAYDAY** on YBCG Brisbane Centre
🚨 EMERGENCY — YBCG Brisbane Centre
MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
14:43:15 AEST / 04:43:15Z · YBCG_144315.wav
```

---

## Adding a new ATC station

Edit `STREAMS` in `config.py`:

```python
STREAMS = [
    {
        "icao": "YBCG",
        "name": "Brisbane Centre",
        "mount": "ybcg3_centre",     # LiveATC mount name — the URL is built from this
        "headers": _HEADERS,
        "prompt": _PROMPT_YBCG,      # station-specific, under 200 tokens
        "vad_threshold": 0.003,
    },
    # paste new station here
]
```

`mount` is the LiveATC mount name, which is the last path segment of the feed's `.pls` playlist URL (e.g. `http://d.liveatc.net/ybcg3_centre` → `ybcg3_centre`). Verify a mount exists before adding it — the redirector 302s for any name, so check the resolved edge actually returns HTTP 200 with an `icy-name` header.

Give the new station **its own short prompt** written in that station's phraseology. Never share one long prompt across stations: Whisper's prompt window is 223 tokens, it silently keeps only the tail of anything longer, and every station then echoes that tail into its transcripts. `venv/bin/python test_filters.py` asserts the limit.

If Discord is enabled, also create a channel for the new station and set `DISCORD_CHANNEL_<ICAO>` in `.env` (see `DISCORD.md`).

---

## Monitored keywords

Keywords are tiered. Both tiers mirror into Discord's `#alerts`; only the emergency tier pings.

| Tier | Category | Keywords | Ping |
|---|---|---|---|
| 🚨 Emergency | Distress | MAYDAY, PAN PAN, EMERGENCY, DISTRESS, FUEL EMERGENCY, GUARD | `@here` |
| 🚨 Emergency | Squawk codes | SQUAWK 7700/7600/7500, and bare 7700/7600/7500 | `@here` |
| 🔴 Interest | Military/display types | MILITARY, RAAF, ROULETTES, HORNET, F-18, F/A-18, F-35, GROWLER, WEDGETAIL, POSEIDON, HERCULES, C-130, C-17, GLOBEMASTER, TROJAN, SPITFIRE, MUSTANG, WARBIRD | none |
| 🔴 Interest | Airshow operations | AIRSHOW, DISPLAY, AEROBATIC(S), FORMATION, PAUL BENNET, SKY ACES | none |
| 🔴 Interest | Airspace | RESTRICTED, COASTAL, TEMPORARY RESTRICTED | none |

To add keywords, edit `KEYWORDS_EMERGENCY` / `KEYWORDS_INTEREST` in `config.py`. Matching is whole-word and case-insensitive.

**Do not add bare numbers.** `"18"` and `"500"` were previously in the list and fired on every "runway 18" and "500 feet" — during an event that volume of false alerts buries the real ones.

### Military callsigns

Separately from the keyword lists, every transcript is matched against ~500 ADF callsigns scraped daily from swld.com.au into `data/military_callsigns.db` (`military_callsigns.py`). A strong hit — an exact callsign, or a misheard one corroborated by a flight number and military context — raises the transmission to the 🔴 interest tier and names the airframe and unit in the alert. A weak hit only annotates the station's own message as *[possible]*.

That split matters for the same reason bare numbers do: matching 500 words fuzzily against noisy STT fires on roughly one transmission in twenty-five if left unguarded ("starting 53" → STARLING, "Water four zero one" → WALER). The guards are a blocklist, an ambiguous list, a generated English-lookalike list, and a rule that an everyday-word callsign must be followed by a flight number. `venv/bin/python test_military.py` asserts the whole thing, including a false-positive budget measured against `logs/`.

Runtime: `/military`, `/military on|off`, `/military refresh`, `/military FALCON`. Startup: `--no-military`, `--refresh-callsigns`.

---

## Live ADS-B tracking

Separate from the audio pipeline and independently switchable. Watches a 60 nm radius around Gold Coast Airport and reports what is actually flying, including aircraft that never key a microphone.

**Data source.** [adsb.fi](https://adsb.fi), free and keyless, with adsb.lol as automatic failover. Personal non-commercial use only, **attribution required** — it appears on the pinned board and every alert; leave it in place. Published limit is 1 req/s; the poller runs at 0.1 req/s and a single lock covers every request path, so on-demand `/track` lookups cannot burst past the budget either.

**Turning it on.** Create two Discord channels and set `DISCORD_CHANNEL_MILITARY` and `DISCORD_CHANNEL_FLIGHTS` in `.env`. Until at least one is set, `config.ADSB_ENABLED` stays False and no thread starts.

**Before pointing it at real channels:**
```bash
venv/bin/python adsb_tracker.py --once      # print the current picture, send nothing
venv/bin/python adsb_tracker.py --dry-run   # run the loop, print what it would post
```

**What it alerts on:** transponder on/off, entering/leaving the offshore airshow display box, YBCG departures and inbounds, emergency squawks, and a live position card per tracked military contact (or formation) that edits in place rather than re-posting.

**How it decides something is military.** Not by the feed's military flag alone — that finds almost nothing at an Australian airshow, where display aircraft are civil-registered warbirds. Several weak signals combine as a noisy-OR: the feed flag, ADF/allied hex blocks, the ADF callsign register's ATC-ID prefixes (`TROJ23` → TROJAN), military-only and warbird ICAO types, operator-name keywords, and "unidentified aircraft manoeuvring inside the display box". Weights are in `adsb_classify.WEIGHTS`; the type and hex tables are JSON under `data/` and are re-read when their mtime changes, so you can add a type without restarting.

**How it avoids alert storms.** Presence is a four-state machine (`SEEDING → LIVE → FADING → LOST`) and alerts fire only on `SEEDING→LIVE` and `FADING→LOST`. Aircraft low over water flicker in and out of receiver coverage constantly; the FADING state absorbs that silently. Plus: a cold-start seeding window, a 30-minute re-appearance gap, a Schmitt trigger on box occupancy, a per-`(aircraft, event)` cooldown persisted in `data/adsb_state.db` so restarts do not replay, and a 12/min token bucket so ADS-B traffic cannot starve transcripts out of the shared Discord outbox.

**Live cards + formations.** On top of the above, a tracked military contact no longer gets a fresh post per position update — the source of the "sixteen notifications for two KC-30As" complaint. Instead `_AdsbLiveCards` (in `atc_tracker.py`, driven from the board snapshot) opens ONE Discord message per contact, edits it in place as the aircraft moves, and finalises it to a last-known state when the contact leaves. Same-type aircraft flying together — the six Roulettes PC-21s, a pair of KC-30As — are consolidated into a single card by `adsb_tracker.group_formations()` (a pure, tested function: same type, within `ADSB_FORMATION_RADIUS_NM`, inside a shared altitude band). With cards on, the state machine stops minting `EV_POSITION` (`emit_position_events=False`) and Telegram mirrors only emergencies, box entries and first appearances. All tunable via `ADSB_LIVE_CARDS_*` / `ADSB_FORMATION_*`.

**Files:** `adsb_source.py` (HTTP, rate limit, failover) · `adsb_classify.py` (pure classifier) · `adsb_tracker.py` (state machine + poll thread + `group_formations()`; `poll_once()` is the pure, testable core) · `adsb_store.py` (SQLite) · `adsb_web.py` (optional map — flight-path trails, live ATC transcript panel, and a recent-activity feed) · `geo.py`. Tests: `venv/bin/python test_adsb.py` — no network, no threads, no writes outside a temp dir.

**Map.** `ADSB_WEB_ENABLED=1` serves a live map on port `8099`. Read-only (GET/HEAD only, no filesystem serving). Aircraft draw as their real type silhouette, rotated to track, using tar1090's shape set in `data/adsb_marker_shapes.json` — **that file is GPL-2.0-or-later**, unlike the rest of the repo; the notice is inside it. `ADSB_WEB_BIND=tailscale` (the default in `.env.example`) listens on this host's `100.64/10` Tailnet address **and** loopback — reachable from your phone, invisible to the local network. The address is detected at startup by scanning the interfaces, so the Mac mini resolves its own; both URLs are printed by `run.command`, logged in the terminal, and posted to `#commands` when the tracker starts. `0.0.0.0` still works, with a warning; `ADSB_WEB_TOKEN` adds a shared secret on top.

**Off switches:** `ADSB_ENABLED=0`, `--no-adsb`, or `/adsb off` at runtime.

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
- Send `/health` in `#commands` first. It distinguishes the two cases: `audio` recent but `last TX` old means the feed is fine and the frequency is quiet; `audio` old with `reconnects` climbing means the feed itself is down.
- The stream may simply be quiet — ATC is not always active
- Run `venv/bin/python atc_tracker.py --calibrate YBCG` to confirm audio is flowing

**A station won't connect:**
- Startup runs a preflight check on all three feeds and prints ✓/✗ per station before the UI starts; failures are also posted to `#commands`
- Connection goes through `d.liveatc.net`, which follows edge rotation automatically, and falls back through `s1-bos` / `s1-fmt2` if the redirector is unreachable
- `/reconnect YBCG` forces a redial without restarting
- Only pin a URL with `/seturl` if you know which edge you want — pinning disables the automatic fallback

**Transcripts contain phrases nobody said (especially repeated ones):**
- This was the dominant failure mode before the quality filters were added. If it returns, check that every prompt in `config.py` is under 200 tokens — `venv/bin/python test_filters.py` asserts this. Whisper silently keeps only the last 223 tokens of an oversized prompt and then reproduces them as if they were speech.
- `/health` reports a `gated` count: the number of transmissions the filters rejected. A high and rising gated count with few real transcripts means the model is hallucinating rather than transcribing.

**VAD too sensitive / missing calls:**
- Adjust that station's `vad_threshold` in `config.py`, or set it live with `/vad YBCG 0.004` (lower = more sensitive)
- Default `0.003` — use `--calibrate` to see live RMS values
- The gate also adapts to a rolling noise floor, so a feed that becomes hissy won't latch permanently open

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
| `.env` | `STT_BACKEND` | `whisper` or `parakeet` |
| `.env` | `RECORDING_ENABLED` / `RECORDING_RETENTION_DAYS` | Transmission audio recording |
| `config.py` → `STREAMS` | list of dicts | ATC feeds — mount, per-station prompt and VAD threshold |
| `config.py` → `KEYWORDS_EMERGENCY` | list of strings | Terms that trigger 🚨 `@here` alerts |
| `config.py` → `KEYWORDS_INTEREST` | list of strings | Terms that trigger 🔴 silent alerts |
| `config.py` → `WHISPER_MODEL` / `PARAKEET_MODEL` | string | Model per backend |
| `config.py` → `MAX_PROMPT_TOKENS` | int | Hard cap on station prompt length (Whisper truncates above 223) |
| `config.py` → `MAX_COMPRESSION_RATIO` / `MIN_AVG_LOGPROB` | float | Transcript quality gate |
| `config.py` → `VAD_RMS_THRESHOLD` | float | Default transmission detection sensitivity |
| `config.py` → `VAD_PREROLL_SEC` | float (seconds) | Audio kept from before VAD trips, so callsigns aren't clipped |
| `config.py` → `STREAM_STALL_TIMEOUT_SEC` | int (seconds) | Silent-connection watchdog |
| `config.py` → `VAD_SILENCE_HANGOVER` | float (seconds) | Silence gap before TX is considered done |
| `config.py` → `MAX_TRANSMISSION_SEC` | int (seconds) | Safety cap on buffer length |
| `config.py` → `RECONNECT_DELAY_SEC` | int (seconds) | Delay before reconnecting after stream error |
| `config.py` → `MILITARY_CALLSIGN_BLOCKLIST` | set of strings | Callsigns never matched (phonetic alphabet, civil types) |
| `config.py` → `MILITARY_CALLSIGN_AMBIGUOUS` | set of strings | Callsigns needing military context before they alert |
| `config.py` → `MILITARY_MIN_CONFIDENCE` / `MILITARY_ALERT_ON_FUZZY` | float / bool | How readily a misheard callsign counts |
| `.env` | `MILITARY_DETECTION_ENABLED` / `MILITARY_REFRESH_HOURS` | Military callsign detection on/off, scrape interval |
