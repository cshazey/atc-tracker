import os

# Load .env automatically so credentials work whether you use run.command
# or call python3 atc_tracker.py directly.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en;q=0.9",
    "cache-control": "no-cache",
    "icy-metadata": "0",
    "origin": "https://www.liveatc.net",
    # Cloudflare fronts d.liveatc.net and 403s requests without a browser-ish
    # referer/UA pair. Both headers are required for the redirector to answer.
    "referer": "https://www.liveatc.net/",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "sec-gpc": "1",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
}

# LiveATC's redirector. Given a mount name it 302s to whichever edge host is
# currently serving that mount, which is what the .pls playlists on the site
# point at. Hardcoding an edge (s1-bos / s1-fmt2) breaks silently whenever
# LiveATC moves a mount, so the redirector is the default and the edges are
# only used as fallbacks (see STREAM_FALLBACK_HOSTS).
LIVEATC_REDIRECTOR = "http://d.liveatc.net"

# Tried in order when the redirector itself is unreachable.
STREAM_FALLBACK_HOSTS = [
    "https://s1-bos.liveatc.net",
    "https://s1-fmt2.liveatc.net",
]

# Per-station Whisper prompt. These MUST stay short.
#
# Whisper's prompt window is n_ctx // 2 - 1 = 223 tokens and mlx-whisper keeps
# only the TAIL of an oversized prompt. A single 232-token prompt shared by all
# stations previously got truncated down to its Southport section, so every
# station was conditioned on Southport CTAF phraseology and echoed it verbatim
# into transcripts (78% of YSPT lines, ~26% of YBBN/YBCG lines). Keep each
# prompt under MAX_PROMPT_TOKENS and station-specific. The prompt's real job is
# to hold number formatting ("one seven two", not "172") — it does not need to
# enumerate every phrase the station might use.
MAX_PROMPT_TOKENS = 200

_PROMPT_YBCG = (
    "Brisbane Centre, Yankee Bravo Whiskey, squawk four two one six, "
    "QNH one zero one three, descend flight level one eight zero, "
    "cleared ILS approach runway one four, contact Gold Coast Tower "
    "one one eight decimal seven, wilco."
)

_PROMPT_YSPT = (
    "Southport traffic, Golf Kilo Delta, Cessna one seven two, five miles north, "
    "inbound Southport, joining crosswind runway one four, turning base, "
    "full stop, Southport."
)

_PROMPT_YBBN = (
    "Brisbane Tower, Qantas four one two, runway one nine left, cleared for takeoff, "
    "wind three two zero degrees eight knots, QNH one zero two four, "
    "contact Ground one two one decimal seven."
)

# Each entry is one monitored ATC feed. "mount" is the LiveATC mount name; the
# default URL is built from it via the redirector. The URL can still be
# overridden without a code change via STREAM_URL_<ICAO> in .env, or at runtime
# via the Discord /seturl <ICAO> <url> command (see README.md).
STREAMS = [
    {
        "icao": "YBCG",
        "name": "Brisbane Centre",
        "mount": "ybcg3_centre",
        "headers": _HEADERS,
        "prompt": _PROMPT_YBCG,
        "vad_threshold": 0.003,
    },
    {
        "icao": "YSPT",
        "name": "Southport",
        "mount": "yspt2",
        "headers": _HEADERS,
        "prompt": _PROMPT_YSPT,
        "vad_threshold": 0.003,
    },
    {
        "icao": "YBBN",
        "name": "Brisbane Tower",
        "mount": "ybbn7_twr",
        "headers": _HEADERS,
        "prompt": _PROMPT_YBBN,
        "vad_threshold": 0.003,
    },
]

for _s in STREAMS:
    _s["url"] = os.environ.get(
        f"STREAM_URL_{_s['icao']}",
        f"{LIVEATC_REDIRECTOR}/{_s['mount']}",
    )
    _s["fallback_urls"] = [f"{host}/{_s['mount']}" for host in STREAM_FALLBACK_HOSTS]

# Cheap import-time guard. The exact tokeniser check runs at startup in
# atc_tracker._check_prompt_lengths(); this catches an obviously-too-long prompt
# without paying for a tokeniser import here.
for _s in STREAMS:
    assert len(_s["prompt"]) < MAX_PROMPT_TOKENS * 3, (
        f"{_s['icao']} prompt looks too long for Whisper's {MAX_PROMPT_TOKENS}-token "
        "budget — see the comment above _PROMPT_YBCG"
    )

# ---------------------------------------------------------------------------
# Speech-to-text backend
# "whisper"  — mlx-whisper (default, proven)
# "parakeet" — NVIDIA Parakeet TDT via parakeet-mlx (pip install parakeet-mlx)
# Override with STT_BACKEND in .env or --stt on the command line.
# ---------------------------------------------------------------------------
STT_BACKEND = os.environ.get("STT_BACKEND", "whisper").strip().lower()

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

# ---------------------------------------------------------------------------
# Transcription quality gate
#
# mlx-whisper returns avg_logprob / no_speech_prob / compression_ratio per
# segment but does not reject on them — compression_ratio_threshold only makes
# it retry at a higher temperature, and it keeps the bad result once the
# temperature list is exhausted. A prompt-echo loop was observed returning
# compression_ratio 14.54 at avg_logprob -0.152 (i.e. highly "confident"), so
# these are applied post-hoc by the app.
# ---------------------------------------------------------------------------
MAX_COMPRESSION_RATIO = 2.4     # above this the text is a repetition loop
MIN_AVG_LOGPROB = -1.2          # below this the decode is guesswork
MAX_NO_SPEECH_PROB = 0.6        # combined with a weak logprob → silence
WEAK_AVG_LOGPROB = -1.0         # used together with MAX_NO_SPEECH_PROB

# Any 1..N word phrase repeated this many times marks the text as a
# hallucination loop. "North Carolina, " x70 and "turning base, " x12 are real
# examples from the logs that the old 1-4 character filter let through.
REPEAT_NGRAM_MAX_WORDS = 6
REPEAT_NGRAM_THRESHOLD = 4

# Fraction of the transcript's 5-grams that may overlap the station prompt
# before it is treated as prompt echo rather than speech.
PROMPT_ECHO_MAX_OVERLAP = 0.5

AUDIO_PREPROCESSING = True

VAD_SAMPLE_RATE = 16000
VAD_CHUNK_FRAMES = 320        # 20ms at 16kHz
VAD_RMS_THRESHOLD = 0.003     # default; per-station override in STREAMS. tune with --calibrate
VAD_SILENCE_HANGOVER = 0.7    # seconds of trailing silence before flushing TX
VAD_PREROLL_SEC = 0.30        # audio kept from *before* VAD trips, so callsign onsets survive

# Adaptive noise floor: threshold becomes max(configured, floor * multiplier)
# so a feed that turns hissy mid-event cannot latch permanently open.
VAD_ADAPTIVE = True
VAD_NOISE_FLOOR_MULT = 3.0
VAD_NOISE_WINDOW_FRAMES = 1500   # ~30s of 20ms frames

MAX_TRANSMISSION_SEC = 25
RECONNECT_DELAY_SEC = 5
RECONNECT_MAX_DELAY_SEC = 60
STREAM_STALL_TIMEOUT_SEC = 20    # no audio for this long → force reconnect

# ---------------------------------------------------------------------------
# Transmission recording
# Each detected transmission is saved as 16kHz mono WAV *before* preprocessing,
# so recordings stay a faithful source for re-transcription and benchmarking.
# ---------------------------------------------------------------------------
RECORDING_ENABLED = os.environ.get("RECORDING_ENABLED", "1").strip() not in ("0", "false", "False", "")
RECORDING_RETENTION_DAYS = int(os.environ.get("RECORDING_RETENTION_DAYS", "14"))

# Substitutions applied to every transcript. Add entries here as you spot a
# consistent misrecognition — the ones below were all observed in real YBCG /
# YBBN output. Keep them specific: a loose pattern will corrupt good text.
ATC_CORRECTIONS = [
    (r'\bQHD\b', 'QNH'),
    (r'\bDesmond\b', 'decimal'),
    (r'\bRager\b', 'roger'),
    (r'\bRaja\b', 'roger'),
    (r'\bSouth Port\b', 'Southport'),
    # Callsigns
    (r'\bJet ?bar\b', 'Jetstar'),
    (r'\bJet ?start\b', 'Jetstar'),
    (r'\bBresman\b', 'Brisbane'),
    (r'\bBridgman\b', 'Brisbane'),
    # RNP approaches — Whisper reliably mangles the three letters. Anchored on a
    # following approach designator so it can't fire on unrelated text.
    (r'\bR[MF]P (Yankee|Zulu|Xray|Whiskey|Alpha|Bravo)\b', r'RNP \1'),
]

# ---------------------------------------------------------------------------
# Keywords — two tiers.
#
# EMERGENCY pings @here in #alerts. INTEREST posts to #alerts without a ping.
# Bare "18" and "500" used to live here and fired on every "runway 18" and
# "500 feet", which buries the alerts that matter.
# ---------------------------------------------------------------------------
KEYWORDS_EMERGENCY = [
    "MAYDAY",
    "PAN PAN",
    "PAN-PAN",
    "EMERGENCY",
    "SQUAWK 7700",
    "SQUAWK 7600",
    "SQUAWK 7500",
    "7700",
    "7600",
    "7500",
    "GUARD",
    "DISTRESS",
    "FUEL EMERGENCY",
]

KEYWORDS_INTEREST = [
    # Military / display types
    "MILITARY",
    "RAAF",
    "ROULETTE",
    "ROULETTES",
    "HORNET",
    "SUPER HORNET",
    "F18",
    "F-18",
    "F/A-18",
    "F35",
    "F-35",
    "GROWLER",
    "WEDGETAIL",
    "POSEIDON",
    "HERCULES",
    "C-130",
    "C130",
    "C-17",
    "C17",
    "GLOBEMASTER",
    "TROJAN",
    "SPITFIRE",
    "MUSTANG",
    "WARBIRD",
    # Airshow operations
    "AIRSHOW",
    "DISPLAY",
    "AEROBATIC",
    "AEROBATICS",
    "FORMATION",
    "PAUL BENNET",
    "SKY ACES",
    # Airspace
    "RESTRICTED",
    "COASTAL",
    "TEMPORARY RESTRICTED",
]

# Union, kept for terminal highlighting and any code that wants one flat list.
KEYWORDS = KEYWORDS_EMERGENCY + KEYWORDS_INTEREST

# ---------------------------------------------------------------------------
# Military callsign registry (military_callsigns.py)
#
# The ADF callsign table at swld.com.au is scraped into a local SQLite database
# roughly once a day and every transcript is checked against it, so a Growler
# calling "FALCON one one" is flagged even though FALCON is nowhere in
# KEYWORDS_INTEREST. Matching is fuzzy because Whisper has never been trained on
# these words and reliably mangles them.
#
# Tune for precision, not recall: this feed says "runway one four" and "Golf
# Kilo Delta" all day, and a false alert costs more than a missed one.
# ---------------------------------------------------------------------------
MILITARY_CALLSIGN_URL = os.environ.get(
    "MILITARY_CALLSIGN_URL",
    "https://www.swld.com.au/pages/aus_raaf_callsigns.htm",
)
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MILITARY_CALLSIGN_DB = os.environ.get(
    "MILITARY_CALLSIGN_DB", os.path.join(_DATA_DIR, "military_callsigns.db")
)
# Snapshot committed to the repo, loaded when the database is empty so detection
# works on first run and offline. Refresh it with:
#   venv/bin/python military_callsigns.py refresh --force && \
#   venv/bin/python military_callsigns.py export-seed
MILITARY_CALLSIGN_SEED = os.path.join(_DATA_DIR, "military_callsigns_seed.json")

MILITARY_DETECTION_ENABLED = os.environ.get(
    "MILITARY_DETECTION_ENABLED", "1"
).strip() not in ("0", "false", "False", "")

# Hours between scrapes, and how often the background thread wakes to check.
MILITARY_REFRESH_HOURS = float(os.environ.get("MILITARY_REFRESH_HOURS", "24"))
MILITARY_REFRESH_CHECK_SEC = 3600
MILITARY_REFRESH_STARTUP_DELAY_SEC = 45

# A scrape returning fewer rows than this is treated as a layout change and the
# existing database is kept rather than overwritten. The page carries ~480.
MILITARY_MIN_SCRAPE_ROWS = 300

# Shorter than this and a callsign is too easy to hit by accident.
MILITARY_MIN_CALLSIGN_LEN = 4

MILITARY_FUZZY = True
MILITARY_MIN_CONFIDENCE = float(os.environ.get("MILITARY_MIN_CONFIDENCE", "0.70"))
MILITARY_PHONETIC_CONFIDENCE = 0.72
# Soundex collapses vowels, so short words collide constantly ("there's" and
# TEREK share a skeleton). Only long callsigns may match on phonetics alone.
MILITARY_MIN_PHONETIC_LEN = 6
# Fuzzy hits annotate the transcript but do not by themselves raise an alert
# unless the transmission is militarily flavoured some other way. Flip to True
# to alert on every fuzzy hit — expect roughly one false alert per 100
# transmissions on these feeds.
MILITARY_ALERT_ON_FUZZY = False

# English words close enough to a callsign to be mistaken for one. Regenerate
# after the callsign list changes with:
#   venv/bin/python military_callsigns.py build-lookalikes
MILITARY_LOOKALIKE_FILE = os.path.join(_DATA_DIR, "english_lookalikes.txt")

# Never matched. The NATO phonetic alphabet is excluded in code; these are the
# entries on the source page that are ordinary words, formation colours, or —
# worse — civil aircraft types that are routinely spoken with a number after
# them ("Dash 8", "King Air 350", "Baron 58", "Archer 28").
MILITARY_CALLSIGN_BLOCKLIST = {
    # Formation colours
    "AMBER", "BLACK", "BLUE", "BROWN", "GOLD", "GREEN", "SILVER", "TEAL",
    "WHITE", "YELLOW",
    # Ordinary words / ATC vocabulary
    # CHANNEL and CENTURY are both what Whisper turns "Centre" into, and the
    # Gold Coast has a Channel and a Century tower to boot.
    "BUSH", "CANTER", "CASTLE", "CENTRAL", "CENTURY", "CHANNEL", "CHECK",
    "CHECKER", "CLASSIC",
    "CODE", "DEEP-V", "DIVER", "EASY", "EMBER", "FAIRWAY", "FARMER", "GASSER",
    "HAT-TRICK", "HERO", "HIGHRISE", "MINOR", "NIGHT", "OPAL", "PACK", "PHAT",
    "RAMP", "RUBY", "SALTY", "SANDY", "SHADE", "SHOT", "SPUD", "SPUR", "STEEL",
    "TUG",
    # Civil aircraft types and operators heard on these feeds
    "ARCHER", "ARROW", "BARON", "CALTEX", "CHEETAH", "CHIEFTAIN", "CHOPPER",
    "COLT", "CONCORDE", "CRUISER", "DASH", "DIAMOND", "DUKE", "HAWK", "KING",
    "PORTER", "WARRIOR",
}

# Matched only on an exact hit that is either followed by a flight number
# ("ARMY two one") or sits in a transmission that is otherwise clearly military.
# Never fuzzy-matched.
#
# ARMY, NAVY and AIR FORCE are deliberately NOT here: they are everyday words,
# so the flight-number rule already covers them, and "Navy one three" deserves
# a full alert.
MILITARY_CALLSIGN_AMBIGUOUS = {
    "ANGEL", "ANGRY", "BEAR", "BLADE", "BOLT", "BRADY", "CLAW", "CROWE",
    "EAGLE", "EMPIRE", "FANG", "FURY", "GARRET", "HALO", "HELMUT", "HERITAGE",
    "HOGAN", "HOWLETTE", "HUDSON", "HUNTER", "IRON", "JACKSON", "JUDGE",
    "JUSTICE", "LIBERTY", "LION", "LODY", "MAGIC", "MENTOR", "MIDNIGHT",
    "MITCHELL", "MONARCH", "OGGY", "OUTBACK", "REACH", "REGENT", "RIDER",
    "ROLLER", "SCOUT", "SHARK", "SPIDER", "STORM", "SURFER", "SWORD", "TESTER",
    "THUNDER", "TIGER", "TORCH", "TROOPER", "TWISTER", "WEDGE", "WHEELER",
}

# Callsigns to add on top of whatever the scrape returns, e.g. a visiting
# display team the source page has not listed yet. name -> description.
MILITARY_EXTRA_CALLSIGNS: dict = {}

# ---------------------------------------------------------------------------
# Telegram integration
# Set credentials via environment variables or edit the values below directly.
# TELEGRAM_ENABLED is automatically True when both are non-empty.
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ---------------------------------------------------------------------------
# Discord integration (dual-send alongside Telegram — see DISCORD.md)
# One channel per station + one #alerts channel + one #commands channel.
# Each station's channel id is looked up via DISCORD_CHANNEL_<ICAO> and merged
# into its STREAMS entry, so adding a station only means one more STREAMS
# dict + one more env var.
# ---------------------------------------------------------------------------
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_ALERTS_CHANNEL_ID = os.environ.get("DISCORD_ALERTS_CHANNEL_ID", "")
DISCORD_COMMANDS_CHANNEL_ID = os.environ.get("DISCORD_COMMANDS_CHANNEL_ID", "")

for _s in STREAMS:
    _s["discord_channel_id"] = os.environ.get(f"DISCORD_CHANNEL_{_s['icao']}", "")

DISCORD_ENABLED = bool(
    DISCORD_BOT_TOKEN
    and DISCORD_ALERTS_CHANNEL_ID
    and DISCORD_COMMANDS_CHANNEL_ID
    and any(_s["discord_channel_id"] for _s in STREAMS)
)

# HuggingFace token — required to download Whisper models.
# Get a free token at https://huggingface.co/settings/tokens (read-only is fine).
HUGGINGFACE_TOKEN = os.environ.get("HUGGINGFACE_TOKEN", "")
