# ATC Tracker

Real-time speech-to-text transcription of YBCG Brisbane Centre (Gold Coast) ATC audio from [LiveATC.net](https://www.liveatc.net).

Streams the live MP3 feed, detects each radio transmission via voice activity detection, transcribes with [Whisper](https://github.com/openai/whisper) running locally on Apple Silicon via MLX, and logs the result to your terminal. Keywords like MILITARY, MAYDAY, F-18 etc. are highlighted in red.

## Requirements

- macOS on Apple Silicon (M1/M2/M3/M4)
- Python 3.10+

## Setup

```bash
pip install miniaudio mlx-whisper requests numpy rich
```

The Whisper model (`whisper-small.en`, ~230 MB) downloads automatically from HuggingFace on first run and is cached in `~/.cache/huggingface/`.

## Usage

```bash
python atc_tracker.py
```

### Controls (while running)

| Key | Action |
|-----|--------|
| `K` | Toggle keyword highlighting on/off |
| `Q` or `Ctrl+C` | Quit |

### Options

```
--model REPO       Whisper model to use (default: mlx-community/whisper-small.en)
                   Faster but less accurate: mlx-community/whisper-tiny.en
--no-keywords      Start with keyword highlighting disabled
--calibrate        Print live RMS values for 15s to tune VAD threshold
```

## Terminal output

```
──────────────────── ATC Tracker ────────────────────
Station : YBCG Brisbane Centre (Gold Coast)
Stream  : https://s1-bos.liveatc.net/ybcg3_centre
Model   : mlx-community/whisper-small.en
Keywords: ON  (press K to toggle)
──────────────────────────────────────────────────────
[10:42:31] Connected — YBCG Brisbane Centre | Keywords: ON | Press K to toggle, Q to quit
[10:42:38] TX   Golf Bravo Charlie cleared COASTAL two departure runway two eight
[10:43:15] TX   MAYDAY MAYDAY MAYDAY Sunstate 654 engine failure
[10:44:01] TX   F-18 formation track RESTRICTED area 7 Delta
```

Keywords (`MILITARY`, `F18`, `F-18`, `18`, `EIGHTEEN`, `COASTAL`, `RESTRICTED`, `EMERGENCY`, `MAYDAY`, `PAN-PAN`, `GUARD`, `500`, `SQUAWK 7700/7600/7500`) appear in **bold red** inline.

All transmissions are always logged. Keyword highlighting is cosmetic only — toggling it does not filter any output.

## VAD calibration

If you're seeing too many false positives (spurious short transcriptions during silence) or missing transmissions, run calibrate mode to check the RMS levels on this stream:

```bash
python atc_tracker.py --calibrate
```

Then adjust `VAD_RMS_THRESHOLD` in `config.py`. A typical pre-squelched ATC stream sits near `0.000` in silence and spikes to `0.010+` during transmissions.

## Configuration

Edit `config.py` to change:

- `STREAM_URL` — point at a different LiveATC feed
- `KEYWORDS` — add or remove keyword strings
- `WHISPER_MODEL` — swap Whisper model
- `VAD_RMS_THRESHOLD` — sensitivity of transmission detection
- `VAD_SILENCE_HANGOVER` — how many seconds of silence before a TX is considered finished
