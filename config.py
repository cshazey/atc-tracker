import os

# Load .env automatically so credentials work whether you use run.command
# or call python3 atc_tracker.py directly.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _str(name: str, default: str) -> str:
    """Env var as a string, falling back to the default when blank.

    A blank value must mean "not set", not "set to empty". .env.example ships
    optional keys as bare `KEY=` so they are discoverable, and os.environ.get's
    default only applies when the key is *absent* — so copying the example
    verbatim would otherwise hand every one of these an empty string. That is
    not hypothetical: an empty STREAM_URL_YBCG produced a station with no URL
    at all, and empty numeric keys crashed the import outright on int("").
    """
    value = os.environ.get(name, "").strip()
    return value if value else default


def _flag(name: str, default: str = "1") -> bool:
    """Env var as a boolean, using the project's usual truthiness rules.

    Note a blank value is False here, not the default — unlike _str. That is
    the existing convention across the project and .env.example always gives
    these an explicit 0 or 1.
    """
    return os.environ.get(name, default).strip() not in ("0", "false", "False", "")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return float(default)


def _int(name: str, default: int) -> int:
    return int(_num(name, default))


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


# Local timezone. Lives here rather than in atc_tracker so the adsb_* modules
# can render AEST timestamps without importing the main app (which would be a
# circular import, and would drag in mlx/miniaudio for no reason).
TIMEZONE = _str("TIMEZONE", "Australia/Brisbane")

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

# Gold Coast Ground 121.800 + Tower 118.700 on one mount, so the prompt has to
# cover both: taxi/clearance phraseology and takeoff/landing phraseology.
_PROMPT_YBCG_TWR = (
    "Gold Coast Tower, Yankee Bravo Whiskey, runway one four, cleared for takeoff, "
    "wind zero nine zero at twelve, QNH one zero one seven, taxi via alpha, "
    "hold short runway three two, contact Ground one two one decimal eight."
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
        # Gold Coast Ground/Tower. Distinct from the YBCG entry above, which is
        # Brisbane Centre sector audio hosted under a ybcg mount — this is the
        # aerodrome's own frequencies (121.800 / 118.700) and the pair the
        # Pacific Airshow display aircraft actually work.
        "icao": "YBCG_TWR",
        "name": "Gold Coast Tower",
        "mount": "ybcg3_gnd_twr",
        "headers": _HEADERS,
        "prompt": _PROMPT_YBCG_TWR,
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
    # _str, not os.environ.get: a bare `STREAM_URL_YBCG=` in .env would
    # otherwise give this station an empty URL and silently kill the feed.
    _s["url"] = _str(
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
STT_BACKEND = _str("STT_BACKEND", "whisper").lower()

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
RECORDING_RETENTION_DAYS = _int("RECORDING_RETENTION_DAYS", 14)

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
MILITARY_CALLSIGN_URL = _str(
    "MILITARY_CALLSIGN_URL",
    "https://www.swld.com.au/pages/aus_raaf_callsigns.htm",
)
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MILITARY_CALLSIGN_DB = _str(
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
MILITARY_REFRESH_HOURS = _num("MILITARY_REFRESH_HOURS", 24)
MILITARY_REFRESH_CHECK_SEC = 3600
MILITARY_REFRESH_STARTUP_DELAY_SEC = 45

# A scrape returning fewer rows than this is treated as a layout change and the
# existing database is kept rather than overwritten. The page carries ~480.
MILITARY_MIN_SCRAPE_ROWS = 300

# Shorter than this and a callsign is too easy to hit by accident.
MILITARY_MIN_CALLSIGN_LEN = 4

# --- ATC ID prefixes (ADS-B idents, not transcripts) ------------------------
#
# The source table has an "ATC ID PREFIX" column holding exactly the form an
# aircraft broadcasts as its ADS-B callsign: TROJAN transmits TROJ23, BLACKCAT
# transmits BLKT10. Registry.lookup_prefix() indexes that column so the ADS-B
# side can name what it sees.
#
# Note this is a SEPARATE policy from MILITARY_CALLSIGN_BLOCKLIST above. That
# one guards against Whisper mishearing ordinary speech; an ADS-B ident arrives
# as exact text so none of it applies. What DOES go wrong here is the scrape:
# multi-word rows like "AWES OAKEY" leak their ordinary words into the prefix
# column. Those are what this blocklist removes.
#
# ARMY and NAVY are deliberately kept. Both map to misparsed rows, so the
# *label* they resolve to is wrong — but an aircraft transmitting "ARMY12" as
# its ident really is military, which is the signal we care about. The
# classifier only prints a record's aircraft/squadron when they look sane.
MILITARY_MIN_PREFIX_LEN = 3
MILITARY_PREFIX_BLOCKLIST = {
    "AND", "CENTRE", "EAST", "FLIGHT", "FLYING", "OAKEY", "SALE", "SCHOOL",
    "SEE", "SQN", "TARGET", "TEST", "TOWING", "UNIT",
}

MILITARY_FUZZY = True
MILITARY_MIN_CONFIDENCE = _num("MILITARY_MIN_CONFIDENCE", 0.70)
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

# ---------------------------------------------------------------------------
# Live ADS-B tracking (adsb_source / adsb_classify / adsb_tracker)
#
# Polls a free community ADS-B feed for aircraft around the Gold Coast, works
# out which of them are military or airshow display aircraft, and posts events
# to Discord. Complements the audio side: the radio tells you what was said,
# this tells you what is actually in the air — including the aircraft that are
# NOT talking.
#
# Data comes from adsb.fi (https://adsb.fi), which is free, needs no API key,
# and is licensed for personal non-commercial use only. Two conditions come
# with that and are honoured in code: a hard 1 request/second rate limit
# (see adsb_source.AdsbSource._get) and mandatory attribution (see the board
# footer and event embeds in atc_tracker.py). adsb.lol is the failover.
# ---------------------------------------------------------------------------
_ADSB_ON = _flag("ADSB_ENABLED", "1")

ADSB_PRIMARY_URL = _str("ADSB_PRIMARY_URL", "https://opendata.adsb.fi/api/v2")
ADSB_FALLBACK_URL = _str("ADSB_FALLBACK_URL", "https://api.adsb.lol/v2")
ADSB_ATTRIBUTION_URL = "https://adsb.fi"
# Identify ourselves properly. These are volunteer-run endpoints; the LiveATC
# browser-spoof headers above would be both rude and useless here.
ADSB_USER_AGENT = _str(
    "ADSB_USER_AGENT",
    "atc-tracker/1.0 (+https://github.com/cshazey/atc-tracker; personal non-commercial)",
)
# 1.1s against a published 1 req/s ceiling — 10% margin for clock jitter.
ADSB_MIN_REQUEST_INTERVAL_SEC = _num("ADSB_MIN_REQUEST_INTERVAL_SEC", 1.1)
ADSB_TIMEOUT_SEC = _num("ADSB_TIMEOUT_SEC", 12)
ADSB_FAILOVER_ERRORS = _int("ADSB_FAILOVER_ERRORS", 3)
ADSB_FAILBACK_SEC = _num("ADSB_FAILBACK_SEC", 300)

# Poll cadence. One area request per ADSB_POLL_SEC is 0.1 req/s — well inside
# the budget, and a 250kt display aircraft still only moves 0.7 NM per poll.
ADSB_POLL_SEC = _num("ADSB_POLL_SEC", 10)
# The global /v2/mil sweep exists to catch an ADF transit still outside the
# radius, so it does not need fine granularity.
ADSB_MIL_POLL_SEC = _num("ADSB_MIL_POLL_SEC", 60)
ADSB_MIL_SWEEP_ENABLED = _flag("ADSB_MIL_SWEEP_ENABLED", "1")

# Gold Coast Airport YBCG/OOL — the reference point for arrival/departure work.
ADSB_HOME_LAT = _num("ADSB_HOME_LAT", -28.1644)
ADSB_HOME_LON = _num("ADSB_HOME_LON", 153.5047)
ADSB_HOME_ELEV_FT = _num("ADSB_HOME_ELEV_FT", 21)
ADSB_HOME_ICAO = _str("ADSB_HOME_ICAO", "YBCG")
# 60 NM from YBCG reaches RAAF Amberley (52 NM), where the heavy military
# transits into a Gold Coast airshow originate.
ADSB_RADIUS_NM = _num("ADSB_RADIUS_NM", 60)

# Airshow display box — the offshore strip between Narrowneck and Broadbeach.
# Either a bbox "lat_min,lat_max,lon_min,lon_max" or a polygon
# "lat,lon;lat,lon;..."; the polygon wins when both are set.
ADSB_BOX_BBOX = _str("ADSB_BOX_BBOX", "-28.06,-27.94,153.42,153.52")
ADSB_BOX_POLYGON = os.environ.get("ADSB_BOX_POLYGON", "")
ADSB_BOX_MAX_ALT_FT = _num("ADSB_BOX_MAX_ALT_FT", 6000)
# The box is a volume, not a footprint: an aircraft above this height is
# overflying on the airway, not displaying, and does not count as inside
# however its ground track reads. Without a ceiling every airliner routed over
# the strip triggers a box entry.
ADSB_BOX_CEILING_FT = _num("ADSB_BOX_CEILING_FT", 10000)
# Hysteresis: "inside" uses the box, "outside" uses it grown by this much, and
# between the two the previous state holds. Without it an aircraft orbiting the
# boundary emits an enter/exit pair every poll.
ADSB_BOX_HYST_NM = _num("ADSB_BOX_HYST_NM", 0.5)
ADSB_BOX_DWELL_SEC = _num("ADSB_BOX_DWELL_SEC", 20)


def _parse_bbox(raw: str):
    try:
        parts = [float(p) for p in raw.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    lat_a, lat_b, lon_a, lon_b = parts
    return (min(lat_a, lat_b), max(lat_a, lat_b), min(lon_a, lon_b), max(lon_a, lon_b))


def _parse_polygon(raw: str):
    pts = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            lat, lon = (float(x) for x in chunk.split(","))
        except ValueError:
            return None
        pts.append((lat, lon))
    return pts if len(pts) >= 3 else None


ADSB_BOX = _parse_bbox(ADSB_BOX_BBOX) or (-28.06, -27.94, 153.42, 153.52)
ADSB_BOX_POLY = _parse_polygon(ADSB_BOX_POLYGON)

# Presence state machine. See the module docstring in adsb_tracker.py for why
# each of these is where it is — in short, low aircraft over water sit at the
# edge of receiver coverage and every one of these numbers exists to stop that
# turning into an alert storm.
ADSB_SEED_POLLS = _int("ADSB_SEED_POLLS", 2)
ADSB_APPEAR_CONFIRM_POLLS = _int("ADSB_APPEAR_CONFIRM_POLLS", 2)
ADSB_APPEAR_GAP_MIN = _num("ADSB_APPEAR_GAP_MIN", 30)
ADSB_FADE_SEC = _num("ADSB_FADE_SEC", 45)
ADSB_LOST_SEC = _num("ADSB_LOST_SEC", 120)
ADSB_LOST_SEC_BOX = _num("ADSB_LOST_SEC_BOX", 60)
ADSB_MIN_SEEN_FOR_LOSS = _int("ADSB_MIN_SEEN_FOR_LOSS", 6)
ADSB_PRUNE_SEC = _num("ADSB_PRUNE_SEC", 900)
# Above this altitude a vanished target is reported as "transponder off";
# below it as the softer "signal lost", because low-level coverage over water
# is genuinely unreliable and we should not claim more than we know.
ADSB_TXPDR_OFF_MIN_ALT_FT = _num("ADSB_TXPDR_OFF_MIN_ALT_FT", 5000)

# YBCG-relative flight phases.
ADSB_DEP_ALT_FT = _num("ADSB_DEP_ALT_FT", 700)
ADSB_DEP_RANGE_NM = _num("ADSB_DEP_RANGE_NM", 6)
ADSB_ARR_RANGE_NM = _num("ADSB_ARR_RANGE_NM", 25)
ADSB_ARR_ALT_FT = _num("ADSB_ARR_ALT_FT", 8000)
ADSB_ARR_CLOSING_KT = _num("ADSB_ARR_CLOSING_KT", 60)
ADSB_PHASE_CONFIRM_POLLS = _int("ADSB_PHASE_CONFIRM_POLLS", 2)

# Classification thresholds. Confidence is a noisy-OR combination of the
# per-signal weights in adsb_classify.
ADSB_MIL_CONFIDENCE = _num("ADSB_MIL_CONFIDENCE", 0.60)
ADSB_PROBABLE_CONFIDENCE = _num("ADSB_PROBABLE_CONFIDENCE", 0.35)
ADSB_ALERT_MIN_CONFIDENCE = _num("ADSB_ALERT_MIN_CONFIDENCE", 0.60)

# Alert volume control.
ADSB_EVENT_COOLDOWN_SEC = _num("ADSB_EVENT_COOLDOWN_SEC", 600)
# Token bucket. An airshow launch is ~20 aircraft in 3 minutes; the Discord
# outbox drains at ~4 msg/s shared with transcripts, so without a cap the
# ADS-B side would delay the audio side — exactly what the outbox exists to
# prevent. Emergencies bypass this.
ADSB_MAX_ALERTS_PER_MIN = _int("ADSB_MAX_ALERTS_PER_MIN", 12)
ADSB_POS_UPDATE_SEC = _num("ADSB_POS_UPDATE_SEC", 300)
ADSB_POS_UPDATE_SEC_BOX = _num("ADSB_POS_UPDATE_SEC_BOX", 120)
# "22:00-06:00" suppresses non-emergency ADS-B alerts overnight. The live board
# keeps refreshing regardless. Empty means never quiet.
ADSB_QUIET_HOURS = os.environ.get("ADSB_QUIET_HOURS", "").strip()
# How much civil traffic reaches the flights channel:
#   airline  — scheduled airline movements, military, and anything anomalous
#   mil_only — nothing civil at all
#   all      — every ADS-B contact arriving or departing YBCG
ADSB_CIVIL_REPORTING = _str("ADSB_CIVIL_REPORTING", "airline").lower()

# Always alert on these, whatever the classifier thinks.
ADSB_WATCH_HEX = _csv("ADSB_WATCH_HEX")
ADSB_WATCH_CALLSIGN = _csv("ADSB_WATCH_CALLSIGN")

# Live board.
ADSB_BOARD_REFRESH_SEC = _num("ADSB_BOARD_REFRESH_SEC", 30)
ADSB_BOARD_MAX_ROWS = _int("ADSB_BOARD_MAX_ROWS", 20)

# Live "actively spotted" cards. Instead of a fresh Discord post every time a
# tracked military aircraft moves — which is what turned two KC-30As into
# sixteen notifications in ten minutes — each aircraft (or formation) gets one
# message that is edited in place with its current position. It reads as a
# living picture rather than a scrolling feed, and it collapses the per-poll
# position spam to zero new messages.
ADSB_LIVE_CARDS_ENABLED = _flag("ADSB_LIVE_CARDS_ENABLED", "1")
# Safety valve: if more than this many distinct contacts/formations are up at
# once, stop opening new cards and let the pinned board carry the overflow.
ADSB_LIVE_CARDS_MAX = _int("ADSB_LIVE_CARDS_MAX", 12)
# A card whose aircraft has been gone this long is finalised (edited to its
# last-known state) and retired, so a re-appearance opens a fresh sighting.
ADSB_CARD_RETIRE_SEC = _num("ADSB_CARD_RETIRE_SEC", 300)

# Formation consolidation. Aircraft of the same type flying together — the six
# Roulettes PC-21s, a pair of KC-30As — collapse into a single card and a
# single board row instead of one each. "Together" means same type designator,
# within ADSB_FORMATION_RADIUS_NM of each other, and inside a shared altitude
# band (a high transit and a low display of the same type stay separate).
ADSB_FORMATION_ENABLED = _flag("ADSB_FORMATION_ENABLED", "1")
ADSB_FORMATION_RADIUS_NM = _num("ADSB_FORMATION_RADIUS_NM", 12)
ADSB_FORMATION_ALT_BAND_FT = _num("ADSB_FORMATION_ALT_BAND_FT", 5000)
ADSB_FORMATION_MIN = _int("ADSB_FORMATION_MIN", 2)

# Flight-path trail. How many recent positions to keep per aircraft for the
# map to draw a breadcrumb line. 30 points at a 10s poll is the last ~5 min.
ADSB_TRAIL_LEN = _int("ADSB_TRAIL_LEN", 30)

# ATC transcript feed shown on the web map. The dashboard pulls the most recent
# radio calls so the map and the voice picture sit side by side. This only caps
# the in-memory ring; transcripts still go to Discord/Telegram as before.
ADSB_WEB_NOTES = _int("ADSB_WEB_NOTES", 40)

ADSB_HEX_BLOCKS_FILE = _str(
    "ADSB_HEX_BLOCKS_FILE", os.path.join(_DATA_DIR, "adsb_hex_blocks.json")
)
ADSB_TYPES_FILE = _str(
    "ADSB_TYPES_FILE", os.path.join(_DATA_DIR, "adsb_types.json")
)
ADSB_STATE_DB = _str(
    "ADSB_STATE_DB", os.path.join(_DATA_DIR, "adsb_state.db")
)
# Aircraft silhouettes for the web map, so a C-17 draws as a C-17. Extracted
# from tar1090 and therefore GPL-2.0-or-later — see the notice inside the file.
ADSB_MARKER_SHAPES_FILE = _str(
    "ADSB_MARKER_SHAPES_FILE", os.path.join(_DATA_DIR, "adsb_marker_shapes.json")
)

DISCORD_CHANNEL_MILITARY = os.environ.get("DISCORD_CHANNEL_MILITARY", "")
DISCORD_CHANNEL_FLIGHTS = os.environ.get("DISCORD_CHANNEL_FLIGHTS", "")

# Read-only local dashboard. Off by default; see README before binding this to
# anything other than loopback.
ADSB_WEB_ENABLED = _flag("ADSB_WEB_ENABLED", "0")
ADSB_WEB_BIND = _str("ADSB_WEB_BIND", "127.0.0.1")
ADSB_WEB_PORT = _int("ADSB_WEB_PORT", 8099)
ADSB_WEB_TOKEN = os.environ.get("ADSB_WEB_TOKEN", "")

# Same composition rule as DISCORD_ENABLED: the feature is on only when it has
# somewhere to send its output.
ADSB_ENABLED = bool(
    _ADSB_ON
    and (
        (DISCORD_ENABLED and (DISCORD_CHANNEL_MILITARY or DISCORD_CHANNEL_FLIGHTS))
        or ADSB_WEB_ENABLED
    )
)

# HuggingFace token — required to download Whisper models.
# Get a free token at https://huggingface.co/settings/tokens (read-only is fine).
HUGGINGFACE_TOKEN = os.environ.get("HUGGINGFACE_TOKEN", "")
