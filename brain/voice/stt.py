"""Speech-to-text via faster-whisper.

faster-whisper is a CTranslate2 port of OpenAI Whisper. ~5x faster than
the reference implementation, runs on CPU or GPU. We default to base.en
because:
- english-only (smaller + faster than multilingual)
- base is the right speed/quality trade for *push-to-talk* use —
  small.en is more accurate but adds 500-900ms per transcription.
  In voice mode that's the difference between "snappy" and "laggy".
  The hotword bias below covers most of base.en's accuracy gap on
  domain vocabulary.
- 75MB download, fits in RAM, no GPU contention with the LLM.

Hotword bias: `transcribe()` accepts an `initial_prompt` argument that
nudges Whisper toward expected vocabulary. We seed it with the user's
known apps + Winston-domain terms (Ollama, ArkAscended, etc.) so
base.en doesn't mis-hear the names that matter ("ark" → "ork").

If you really need higher STT accuracy and don't mind the latency hit,
set WHISPER_MODEL_SIZE in config.py (or env) to "small.en" or
"medium.en" before launch.

CPU vs GPU: CPU default keeps STT off the GPU so it doesn't fight
Ollama. base.en on CPU transcribes ~3s of audio in ~0.4s on a Ryzen 7
7800X3D.

Model is loaded lazily on first call so importing this module is free.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import numpy as np

try:
    from faster_whisper import WhisperModel
except ImportError:  # pragma: no cover
    WhisperModel = None


# ──────────────── Module-level singleton model ────────────────
# Loading the model takes ~2-3s. We share one across the process so
# every transcribe() call after the first is instant. Lock guards the
# load so two simultaneous starts don't double-load.
_model: Optional["WhisperModel"] = None
_model_lock = threading.Lock()
_model_size = "base.en"
_model_device = "cpu"
_model_compute_type = "int8"   # int8 quant on CPU is the fast/clean default

# Hotword prompt fed to every transcribe() call as `initial_prompt`.
# Whisper conditions its decoder on this text; words appearing here
# get a probability boost so "ark" isn't heard as "ork", "Ollama"
# isn't heard as "Olama", etc. Keep it short — this also burns
# context tokens on every transcription.
HOTWORD_PROMPT = (
    "Winston, Ollama, ArkAscended, qwen, WSL, "
    "GPU, CPU, RAM, CPU temperature, "
    "Firefox, Chrome, Discord, Spotify, Steam"
)


def configure(size: str = "base.en", device: str = "cpu",
              compute_type: str = "int8"):
    """Override defaults before the first transcribe() call. After the
    model loads, changes have no effect for this process."""
    global _model_size, _model_device, _model_compute_type
    _model_size = size
    _model_device = device
    _model_compute_type = compute_type


def _ensure_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        if WhisperModel is None:
            raise RuntimeError(
                "faster-whisper not installed — `pip install faster-whisper`"
            )
        # Allow runtime override without editing source: set
        # WINSTON_WHISPER_MODEL=small.en (or medium.en, etc.) in .env if
        # you want better accuracy at the cost of latency.
        size = os.environ.get("WINSTON_WHISPER_MODEL") or _model_size
        # Local cache lives in ~/.cache/huggingface; first load downloads
        # the model (75MB for base.en, 244MB for small.en).
        _model = WhisperModel(
            size,
            device=_model_device,
            compute_type=_model_compute_type,
        )
    return _model


# ──────────────── Public API ────────────────
def transcribe(audio: np.ndarray, sample_rate: int = 16_000) -> str:
    """Transcribe a 1-D float32 audio array to text.

    Returns the raw transcribed string, stripped of leading/trailing
    whitespace. Empty string if nothing was said.

    `audio` must be float32 in [-1, 1]. Whisper internally resamples to
    16kHz so a non-16k input works but adds a tiny cost — we always feed
    16k to skip that.
    """
    if audio is None or len(audio) == 0:
        return ""
    if sample_rate != 16_000:
        # Trivial linear resample. Faster-whisper has its own resampler
        # but doing it here keeps the contract simple.
        ratio = 16_000 / sample_rate
        new_len = int(len(audio) * ratio)
        x_old = np.linspace(0, 1, len(audio))
        x_new = np.linspace(0, 1, new_len)
        audio = np.interp(x_new, x_old, audio).astype(np.float32)

    # Skip near-silent recordings — Whisper will hallucinate "Thanks for
    # watching!" or similar if you give it pure noise. RMS threshold
    # tuned so very quiet speech still passes but room tone doesn't.
    rms = float(np.sqrt(np.mean(audio * audio)) + 1e-9)
    if rms < 0.005:
        return ""

    model = _ensure_model()
    # `vad_filter` runs Silero VAD to drop silence — meaningful speedup
    # on recordings with leading/trailing pause. `language="en"` skips
    # the language-detection pass. `initial_prompt` biases the decoder
    # toward Winston's vocabulary — the difference between "ark" and
    # "ork" on a fresh model.
    segments, _info = model.transcribe(
        audio,
        beam_size=1,                # greedy is fine for short utterances
        language="en",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        initial_prompt=HOTWORD_PROMPT,
    )
    # `segments` is a generator — concatenate the text fields.
    text = "".join(s.text for s in segments).strip()
    return text


def warm_up():
    """Force-load the model. Call this at startup so the first user
    interaction doesn't pay the 2-3s load cost."""
    _ensure_model()