"""Pluggable speech-to-text backends.

Two local backends are supported:

  whisper  — mlx-whisper (default). Accepts a text prompt, which is what keeps
             ATC numbers spelled out ("one seven two" rather than "172").
  parakeet — NVIDIA Parakeet TDT via parakeet-mlx. A transducer: it reads the
             audio once instead of decoding token-by-token, and having no text
             prompt it cannot echo one. Optional dependency.

Both return a list of Segment, so the quality gate in atc_tracker.py works
against either. Use bench_stt.py to compare them on real recorded audio.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from typing import Optional

import numpy as np

import config


@dataclass
class Segment:
    """One decoded span, with whatever confidence signals the backend exposes.

    Fields are Optional because not every backend produces every signal; the
    quality gate skips a check whose signal is None rather than guessing.
    """

    text: str
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    compression_ratio: Optional[float] = None


def text_compression_ratio(text: str) -> float:
    """Whisper's own repetition metric, recomputed so it is available for any
    backend. Looping text compresses extremely well — a prompt-echo loop
    observed in the wild scored 14.54 against a sane ceiling of ~2.4."""
    if not text:
        return 0.0
    data = text.encode("utf-8")
    return len(data) / len(zlib.compress(data))


class TranscriptionBackend:
    name = "base"

    def ensure_ready(self, log=print) -> None:
        """Download/warm the model. Called once before streaming starts."""
        raise NotImplementedError

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[Segment]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Whisper (mlx-whisper)
# ---------------------------------------------------------------------------

class WhisperBackend(TranscriptionBackend):
    name = "whisper"

    def __init__(self, model: str):
        self.model = model

    def ensure_ready(self, log=print) -> None:
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import disable_progress_bars, enable_progress_bars

        log(f"Checking model {self.model}...")
        try:
            snapshot_download(repo_id=self.model, local_files_only=True)
            log("✓ Model already cached — ready")
            return
        except Exception:
            pass
        log(f"Downloading {self.model} (~1.6 GB) — please wait…")
        try:
            disable_progress_bars()
            snapshot_download(repo_id=self.model)
            log("✓ Model downloaded successfully")
        except Exception as exc:
            log(
                f"Download failed: {exc}\n"
                "  Make sure HUGGINGFACE_TOKEN is set in .env and you have internet access."
            )
        finally:
            enable_progress_bars()

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[Segment]:
        import mlx_whisper

        kwargs = {}
        if prompt:
            kwargs["initial_prompt"] = prompt
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language="en",
            verbose=None,
            temperature=(0.0, 0.2, 0.4),
            compression_ratio_threshold=2.0,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
            **kwargs,
        )
        segments = result.get("segments") or []
        if not segments:
            text = (result.get("text") or "").strip()
            if not text:
                return []
            return [Segment(text=text, compression_ratio=text_compression_ratio(text))]
        return [
            Segment(
                text=(s.get("text") or "").strip(),
                avg_logprob=s.get("avg_logprob"),
                no_speech_prob=s.get("no_speech_prob"),
                compression_ratio=s.get("compression_ratio"),
            )
            for s in segments
        ]


# ---------------------------------------------------------------------------
# Parakeet (parakeet-mlx)
# ---------------------------------------------------------------------------

class ParakeetBackend(TranscriptionBackend):
    name = "parakeet"

    def __init__(self, model: str):
        self.model = model
        self._model = None
        self._get_logmel = None
        self._mx = None

    def ensure_ready(self, log=print) -> None:
        try:
            import mlx.core as mx
            from parakeet_mlx import from_pretrained
            from parakeet_mlx.audio import get_logmel
        except ImportError as exc:
            raise RuntimeError(
                f"parakeet backend requires parakeet-mlx ({exc}). "
                "Install it with: venv/bin/pip install parakeet-mlx"
            ) from exc

        log(f"Loading {self.model} (first run downloads ~600 MB)…")
        self._mx = mx
        self._get_logmel = get_logmel
        self._model = from_pretrained(self.model)
        log("✓ Parakeet ready")

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> list[Segment]:
        # prompt is accepted for interface parity and deliberately unused —
        # a transducer has no text conditioning, which is precisely why it
        # cannot reproduce the prompt-echo failure.
        if self._model is None:
            self.ensure_ready(log=lambda *_: None)
        mel = self._get_logmel(self._mx.array(audio), self._model.preprocessor_config)
        results = self._model.generate(mel)
        segments: list[Segment] = []
        for result in results:
            for sentence in result.sentences or []:
                text = (sentence.text or "").strip()
                if not text:
                    continue
                # Map confidence onto the same log scale Whisper's avg_logprob
                # uses, so one MIN_AVG_LOGPROB threshold governs both backends.
                # -1.2 corresponds to a confidence of about 0.30.
                conf = max(float(getattr(sentence, "confidence", 1.0) or 0.0), 1e-6)
                segments.append(Segment(
                    text=text,
                    avg_logprob=math.log(conf),
                    compression_ratio=text_compression_ratio(text),
                ))
            if not result.sentences and (result.text or "").strip():
                text = result.text.strip()
                segments.append(Segment(
                    text=text,
                    compression_ratio=text_compression_ratio(text),
                ))
        return segments


# ---------------------------------------------------------------------------

def build_backend(name: str, model: Optional[str] = None) -> TranscriptionBackend:
    name = (name or "whisper").strip().lower()
    if name == "whisper":
        return WhisperBackend(model or config.WHISPER_MODEL)
    if name == "parakeet":
        return ParakeetBackend(model or config.PARAKEET_MODEL)
    raise ValueError(f"Unknown STT backend '{name}' — expected 'whisper' or 'parakeet'")
