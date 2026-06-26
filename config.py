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
        "url": "https://s1-bos.liveatc.net/yspt2",
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

WHISPER_MODEL = "mlx-community/whisper-small"

VAD_SAMPLE_RATE = 16000
VAD_CHUNK_FRAMES = 320        # 20ms at 16kHz
VAD_RMS_THRESHOLD = 0.003     # tune with --calibrate
VAD_SILENCE_HANGOVER = 1.5    # seconds of trailing silence before flushing TX

MAX_TRANSMISSION_SEC = 60
RECONNECT_DELAY_SEC = 5

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

# HuggingFace token — required to download Whisper models.
# Get a free token at https://huggingface.co/settings/tokens (read-only is fine).
HUGGINGFACE_TOKEN = os.environ.get("HUGGINGFACE_TOKEN", "")
