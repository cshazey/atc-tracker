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
    "pragma": "no-cache",
    "priority": "u=1, i",
    "sec-ch-ua": '"Brave";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "sec-gpc": "1",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
}

# Each entry is one monitored ATC feed.
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
        "url": "https://s1-fmt2.liveatc.net/yspt2",
        "headers": _HEADERS,
    },
    {
        "icao": "YSSY",
        "name": "Sydney Centre",
        "url": "https://s1-fmt2.liveatc.net/yssy1_ctr_128600",
        "headers": _HEADERS,
    },
    {
        "icao": "YBBN",
        "name": "Brisbane Tower",
        "url": "https://s1-fmt2.liveatc.net/ybbn7_twr",
        "headers": _HEADERS,
    },
]

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

WHISPER_INITIAL_PROMPT = (
    "Brisbane Centre, Alpha Bravo Charlie, squawk four two one six, QNH one zero one three, "
    "descend flight level one eight zero, cleared ILS approach runway one four, "
    "contact Gold Coast Tower one one eight decimal seven, wilco. "
    "Juliet Yankee Foxtrot, inbound Coolangatta, request traffic information. "
    "YSSY YBBN YBCG YMML YPPH YPAD YBTL Coolangatta Archerfield Williamtown Amberley Archerfield. "
    "RAAF Williamtown, Hornet formation, C130 Hercules, P-8 Poseidon, Dragon one, Roulette four. "
    "squawk seven thousand seven hundred, MAYDAY MAYDAY MAYDAY, affirm, negative, roger. "
    "Southport traffic, Golf Kilo Delta, Cessna one seven two, inbound Southport, "
    "CTAF one two six decimal seven, joining crosswind runway one three, circuits. "
    "Southport traffic, final runway one three, stop and go. Taxiing to holding point. "
    "Southport CTAF, turning base, turning final, clear of the runway, backtracking."
)

AUDIO_PREPROCESSING = True

VAD_SAMPLE_RATE = 16000
VAD_CHUNK_FRAMES = 320        # 20ms at 16kHz
VAD_RMS_THRESHOLD = 0.003     # tune with --calibrate
VAD_SILENCE_HANGOVER = 1.5    # seconds of trailing silence before flushing TX

MAX_TRANSMISSION_SEC = 60
RECONNECT_DELAY_SEC = 5

ATC_CORRECTIONS = [
    (r'\bQHD\b', 'QNH'),
    (r'\bDesmond\b', 'decimal'),
    (r'\bRager\b', 'roger'),
    (r'\bRaja\b', 'roger'),
    (r'\bSouth Port\b', 'Southport'),
]

KEYWORDS = [
    "MILITARY",
    "F18",
    "F-18",
    "18",
    "EIGHTEEN",
    "COASTAL",
    "RESTRICTED",
    "EMERGENCY",
    "MAYDAY",
    "PAN-PAN",
    "GUARD",
    "500",
    "SQUAWK 7700",
    "SQUAWK 7600",
    "SQUAWK 7500",
]

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
