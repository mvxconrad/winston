"""Text-to-speech.

v1 ships with Piper (local, free, ~50MB voice models). The provider
interface is intentionally minimal so we can swap in ElevenLabs or
Coqui XTTS later without touching voice_engine.

Why Piper for v1: zero ongoing cost, no network round-trip, voices are
robotic-but-passable. Good enough to validate the audio loop. Once you
hear Winston speak through the orb, decide if the voice quality is
worth the upgrade.

When you decide to upgrade:
- ElevenLabs: ~30 lines change in this file. Streams chunks, drop-in
  replacement for synthesize().
- Coqui XTTS-v2: ~50 lines, can clone a voice from 6s reference audio.

Voice file location: we look for `models/voices/en_GB-alan-medium.onnx`
relative to the project root by default. en_GB-alan is the closest
Piper has to a "British butler" — calm, slightly clipped, makes Winston
feel like Winston. Override with set_voice_path() if you want a
different voice.

Piper API note: piper-tts 2.x changed `voice.synthesize(text, wav)` —
which used to write a WAV file — to `voice.synthesize(text)` returning
an iterable of AudioChunk objects with `.audio_float_array`. We use
that path here so we skip a wasteful WAV encode/decode roundtrip.
`synthesize_wav()` still exists as a sibling method if you ever want
the old WAV-file behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

try:
    from piper import PiperVoice
except ImportError:  # pragma: no cover
    PiperVoice = None


# ──────────────── Module state ────────────────
_voice_path: Optional[Path] = None
_voice: Optional["PiperVoice"] = None
_target_sample_rate = 16_000  # match audio.SAMPLE_RATE


def set_voice_path(path: str | Path):
    """Override the default voice .onnx path. Must be called before
    synthesize()."""
    global _voice_path, _voice
    _voice_path = Path(path)
    _voice = None  # force reload


def _default_voice_path() -> Path:
    # Project layout: <repo>/models/voices/<voice>.onnx
    here = Path(__file__).resolve().parent
    repo = here.parent.parent
    return repo / "models" / "voices" / "en_GB-alan-medium.onnx"


def _ensure_voice():
    global _voice, _voice_path
    if _voice is not None:
        return _voice
    if PiperVoice is None:
        raise RuntimeError(
            "piper-tts not installed — `pip install piper-tts`"
        )
    if _voice_path is None:
        _voice_path = _default_voice_path()
    if not _voice_path.exists():
        raise FileNotFoundError(
            f"Piper voice model not found at {_voice_path}.\n"
            f"Download from https://github.com/rhasspy/piper/releases\n"
            f"Recommended: en_GB-alan-medium (the .onnx + .onnx.json files)"
        )
    _voice = PiperVoice.load(str(_voice_path))
    return _voice


# ──────────────── Public API ────────────────
def synthesize(text: str) -> np.ndarray:
    """Synthesize `text` to a 1-D float32 audio array at 16kHz.

    All-at-once path. Internally just collects everything from
    `synthesize_stream` so both paths share one resample/convert
    implementation.
    """
    parts = [chunk for chunk in synthesize_stream(text)]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32, copy=False)


def synthesize_stream(text: str):
    """Yield float32 audio chunks at 16kHz as Piper produces them.

    Piper natively yields AudioChunks during synthesis (one per
    phoneme group, roughly), so we get genuine streaming for free.
    Each chunk is resampled to 16kHz before yielding so SpeakerPlayer
    doesn't need to know about Piper's native rate (22050Hz at medium).

    Same shape as `tts_elevenlabs.synthesize_stream` so voice_engine
    can swap providers without caring.
    """
    if not text or not text.strip():
        return
    voice = _ensure_voice()
    for chunk in voice.synthesize(text):
        audio = chunk.audio_float_array.astype(np.float32, copy=False)
        src_rate = chunk.sample_rate
        if src_rate and src_rate != _target_sample_rate:
            ratio = _target_sample_rate / src_rate
            new_len = int(len(audio) * ratio)
            x_old = np.linspace(0, 1, len(audio))
            x_new = np.linspace(0, 1, new_len)
            audio = np.interp(x_new, x_old, audio).astype(np.float32)
        yield audio


def warm_up():
    """Pre-load the voice model so the first synthesize() call doesn't
    pay the load cost."""
    _ensure_voice()