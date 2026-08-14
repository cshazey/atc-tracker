# ATC Tracker

Real-time speech-to-text transcription of live ATC audio from [LiveATC.net](https://www.liveatc.net), running locally on Apple Silicon via MLX.

Currently monitoring:
- **YBCG** — Brisbane Centre (Gold Coast)
- **YBCG_TWR** — Gold Coast Ground 121.800 / Tower 118.700
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

## Live ADS-B tracking

The audio side tells you what is being *said*. This tells you what is actually in the air — including the aircraft that are saying nothing. It watches a 60 NM radius around Gold Coast Airport, works out which contacts are military or airshow display aircraft, and posts to Discord: transponders coming on and going off, the offshore airshow display box, YBCG arrivals and departures, and emergency squawks.

Data comes from [adsb.fi](https://adsb.fi) — free, no API key, no signup — with [adsb.lol](https://api.adsb.lol) as automatic failover. **adsb.fi is licensed for personal, non-commercial use and requires attribution**, which appears on the pinned board and in every alert. The published limit is 1 request/second; this polls once every 10 seconds, and a single lock covers every request path including on-demand `/track` lookups, so nothing can burst past it.

### Setup

Create two Discord channels, put their IDs in `.env`, and restart:

```bash
DISCORD_CHANNEL_MILITARY=      # #mil-tracker
DISCORD_CHANNEL_FLIGHTS=       # #gc-flights
```

Before pointing it at a real channel, see what it would actually post:

```bash
venv/bin/python adsb_tracker.py --once
```

That prints the current classified picture and sends nothing. `--dry-run` runs the full poll loop and prints the messages it *would* post — worth leaving for ten minutes during a busy period to check the alert volume suits you.

### Why the classifier is not just "is it flagged military"

The feed carries a military flag (`dbFlags`). Relying on it alone does not work here. Sampled live over the Gold Coast during the Pacific Airshow, a 250 NM sweep found exactly **one** flagged aircraft, 150 NM inland — while the display box held a P-40 Kittyhawk, an L-39 Albatros, a Jet Provost, and an aircraft with no registration and no type in any database, doing 242 knots at 1,200 feet. None of them were flagged. Display aircraft at Australian airshows are overwhelmingly civil-registered warbirds.

So several weak signals are combined instead, with confidence as a noisy-OR:

| Signal | Weight |
|---|---|
| Feed's military flag | 1.00 |
| On your `/watch` list | 1.00 |
| ADF or allied hex block (`7CF800`–`7CFAFF` is the ADF) | 0.90 |
| Callsign prefix in the ADF register (`TROJ23` → TROJAN) | 0.80 |
| Military-only ICAO type (F35, C17, C130, P8, PC21…) | 0.70 |
| Warbird type (L39, JPRO, P40, SPIT, T6…) | 0.55 |
| Registered operator matches a display/defence keyword | 0.45 |
| Unidentified aircraft manoeuvring inside the display box | 0.40 |

Two mediocre signals therefore outrank one. Every alert carries the reasons it fired, so a bad rule is visible in the message rather than something to go hunting for in the code. The type and operator lists live in `data/adsb_types.json` and the hex ranges in `data/adsb_hex_blocks.json` — both are re-read when their modification time changes, so you can add a type mid-event without restarting.

Callsign resolution reuses the ADF register that already backs transcript detection, via its ATC-ID-prefix column: `BLKT10` → BLACKCAT (P-8A), `ADDR11` → ADDER (F-35, 3SQN Williamtown). That lookup is exact-only, deliberately unlike the fuzzy transcript matcher — an ADS-B callsign arrives as exact text, so tolerance there would be pure false-positive surface.

### Not alerting on nothing

The hard part is not fetching data, it is staying quiet. Aircraft flying low over water sit at the edge of ground-receiver coverage and drop in and out of the feed constantly — and they are exactly the aircraft you most want to hear about. A naive "was here, now gone" alert would fire every twenty seconds.

Presence is therefore a four-state machine:

```
(untracked) --first report--> SEEDING --2 consecutive polls--> LIVE
                                                                | absent >45s
                    +---- any report (silent) -----------> FADING
                    |                                           | absent >120s
                    +---------------------------------------> LOST
```

**Alerts fire on exactly two transitions: `SEEDING→LIVE` and `FADING→LOST`.** The intermediate ones are silent, so a flickering target oscillates between LIVE and FADING and says nothing, while a real transponder shutdown walks all the way to LOST and reports once. `test_adsb.py` pins this down: an aircraft alternating present/absent every single poll for five minutes must emit zero events.

The same idea runs through the rest. The display box uses a Schmitt trigger — you are "in" at the boundary but only "out" 0.5 NM beyond it — so orbiting the edge cannot chatter. Phase changes need two consecutive confirmations. A loss above 5,000 ft is reported as "transponder off" and below it as the softer "signal lost", because low-level coverage over water genuinely is unreliable and the alert should not claim more than it knows. Alert history lives in `data/adsb_state.db`, so restarting mid-event does not replay everything you have already been told.

Turn the volume down further with `ADSB_QUIET_HOURS`, `ADSB_CIVIL_REPORTING=mil_only`, or by raising `ADSB_LOST_SEC`. Disable entirely with `ADSB_ENABLED=0`, `--no-adsb`, or `/adsb off`.

### Commands

`/air` what is airborne · `/mil` military and display only · `/box` who is in the display box · `/track VH-SIC` detail on one aircraft, falling back to a live lookup if it is out of range · `/watch` and `/unwatch` · `/adsb` poller health.

> Note: `/mil` now means "what military is airborne". The callsign register is `/military`, which is otherwise unchanged.

### Map

Set `ADSB_WEB_ENABLED=1` for a live map at `http://localhost:8099` — everything tracked, the display box drawn, aircraft coloured by classification (orange military, red in the box, grey probable display, blue civil). It is read-only: GET and HEAD only, no filesystem serving, unknown paths 404.

Aircraft draw as their actual type: a C-17 gets the C-17 silhouette, a Hercules the Hercules, a Jet Provost a straight-wing trainer — rotated to the reported track, the same way [globe.adsbexchange.com](https://globe.adsbexchange.com/) does it. The shapes come from [tar1090](https://github.com/wiedehopf/tar1090)'s `markers.js` and live in `data/adsb_marker_shapes.json` (93 shapes, ~500 type mappings).

> **Licence note:** those shapes are **GPL-2.0-or-later**, tar1090's own licence — unlike the rest of this repo. Fine for personal use; worth knowing if you ever publish or distribute the repository. The full notice is inside the JSON file.

`data/adsb_marker_shapes.json` is plain data, so you can retype an aircraft by editing it — `"P40": ["hi_perf", 1.0]` points the Kittyhawk at the single-seat-fighter silhouette. Roughly 70 designators are this project's own additions (listed under `_extra_mappings`), mostly airshow warbirds and local GA types tar1090 leaves to the emitter category. `test_adsb.py` checks that every mapping names a shape that exists and that every type the classifier can flag has one, so a typo fails the tests rather than silently drawing nothing.

Set `ADSB_WEB_BIND=tailscale` (the default in `.env.example`) and it listens on this machine's Tailnet address **and** loopback — reachable from your phone, invisible to whatever network the machine is plugged into. The Tailscale address is found at startup by scanning for a `100.64/10` interface, so it works wherever the `tailscale` binary happens to be installed, and both URLs are printed by `run.command`, logged in the terminal, and posted to `#commands` when the tracker starts. `0.0.0.0` still works if you want it, with a warning. `ADSB_WEB_TOKEN` adds a shared secret on top.

## Auto-update

`run.command` is a supervisor. It starts the tracker as a child process and checks `origin/main` once a minute; when new commits land it pulls, posts a summary to `#commands`, and restarts the tracker. **The supervisor itself never restarts** — so a bad commit can't leave you with a launcher that won't start. A failed pull (dirty tree, diverged branch) leaves the running tracker alone and says so rather than killing it. It also restarts the tracker if it crashes, with a 10-second backoff.

```bash
AUTO_UPDATE=0 bash run.command      # supervise, never pull
NO_SUPERVISOR=1 bash run.command    # old behaviour: run the tracker directly
UPDATE_INTERVAL=300 bash run.command
```

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
| `.env` → `DISCORD_CHANNEL_MILITARY` / `DISCORD_CHANNEL_FLIGHTS` | ADS-B alert channels — tracking stays off until one is set |
| `.env` → `ADSB_CIVIL_REPORTING` | How much civil YBCG traffic reaches the flights channel (`airline` / `mil_only` / `all`) |
| `.env` → `ADSB_QUIET_HOURS` | Suppress non-emergency ADS-B alerts overnight |
| `.env` → `ADSB_LOST_SEC` / `ADSB_APPEAR_GAP_MIN` | Anti-flapping thresholds — raise the first if transponder-off alerts are chatty |
| `.env` → `ADSB_BOX_BBOX` / `ADSB_BOX_POLYGON` | The airshow display box |
| `.env` → `ADSB_MIL_CONFIDENCE` / `ADSB_ALERT_MIN_CONFIDENCE` | How much corroboration before an aircraft counts as military |
| `.env` → `ADSB_WEB_ENABLED` / `ADSB_WEB_BIND` / `ADSB_WEB_TOKEN` | Local live map (see the Tailscale note above) |
| `data/adsb_types.json` | Military / warbird / aerobatic ICAO type codes and operator keywords — re-read on change |
| `data/adsb_hex_blocks.json` | Military ICAO hex address ranges — re-read on change |
| `data/adsb_marker_shapes.json` | Aircraft silhouettes for the map, and which type designator draws which shape (GPL-2.0-or-later, from tar1090) |
| `config.py` → `MILITARY_PREFIX_BLOCKLIST` | ATC-ID prefixes the source page misparses, excluded from ADS-B callsign lookups |
