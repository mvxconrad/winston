"""ElevenLabs TTS backend.

Drop-in replacement for tts.py — same `synthesize(text)`, `synthesize_stream(text)`,
and `warm_up()` interface so voice_engine doesn't care which backend
is in use.

Why ElevenLabs:
- Piper voices sound robotic. ElevenLabs Flash v2.5 produces speech that
  consistently passes for human in casual listening.
- Flash v2.5 streams the first chunk in ~75ms; perceptual gain over
  Piper's ~50ms is negligible per-call but the *quality* gain is huge.
- Hosted, so no GPU contention with Ollama.

What this module does NOT do:
- Streaming playback. The current voice_engine is "synthesize whole
  reply, then play". When we move to sentence-streaming (LLM emitting
  sentences while still generating, each sent to TTS as it lands), this
  module's `synthesize` will become a generator. For v1 we collect every
  chunk and return one numpy array — same contract as Piper.

Failure modes (all fall back to Piper, never crash voice mode):
- ELEVENLABS_API_KEY unset → return [] from synthesize, voice_engine
  catches the empty audio and tries the Piper backend.
- Network / 401 / quota exceeded → exception bubbles up, voice_engine's
  pipeline exception handler logs + falls back.
- elevenlabs SDK not installed → import-time guard so importing this
  module is safe; calls return empty.
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np

try:
    from elevenlabs.client import ElevenLabs
except ImportError:  # pragma: no cover
    ElevenLabs = None


# ──────────────── Module state ────────────────
_client: Optional["ElevenLabs"] = None
_voice_id: Optional[str] = None
_model_id: str = "eleven_flash_v2_5"
_target_sample_rate = 16_000


# ──────────────── Public configuration ────────────────
def configure(voice_id: str, model_id: str = "eleven_flash_v2_5"):
    """Set the voice + model used by subsequent synthesize() calls.
    Called by voice_engine at startup with values from config.py.

    The WINSTON_TTS_VOICE_ID env var, if set, overrides the configured
    voice_id — handy for trying Voice Library entries without editing
    config.py.
    """
    global _voice_id, _model_id, _client
    _voice_id = os.environ.get("WINSTON_TTS_VOICE_ID") or voice_id
    _model_id = model_id
    _client = None  # force lazy reload so a key change at runtime takes effect


def _ensure_client() -> "ElevenLabs":
    global _client
    if _client is not None:
        return _client
    if ElevenLabs is None:
        raise RuntimeError(
            "elevenlabs SDK not installed — `pip install elevenlabs`"
        )
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY env var not set. Get a key at "
            "elevenlabs.io → Profile → API keys, then `export "
            "ELEVENLABS_API_KEY=...`."
        )
    _client = ElevenLabs(api_key=api_key)
    return _client


# ──────────────── Public API (matches tts.py shape) ────────────────
def synthesize(text: str) -> np.ndarray:
    """Synthesize `text` to a 1-D float32 audio array at 16kHz.

    All-at-once path. Used for one-shot generation when you don't care
    about streaming latency. For real-time voice prefer
    `synthesize_stream` — it returns the first ~75ms of audio in
    ~75ms instead of waiting for the whole clip.

    PCM_16000 means raw signed 16-bit little-endian PCM at 16kHz mono —
    no headers, no resampling.
    """
    parts = [chunk for chunk in synthesize_stream(text)]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32, copy=False)


def synthesize_stream(text: str):
    """Yield float32 audio chunks (1-D, 16kHz mono) as ElevenLabs streams them.

    The whole point of Flash v2.5: the first audio chunk lands in
    ~75ms; without streaming we'd wait for the whole clip (~1s+).
    SpeakerPlayer.append() consumes whatever yield gives it, so
    playback can begin as soon as the first chunk arrives.

    Edge cases:
    - Bytes don't always align to int16 sample boundaries between
      chunks. We carry a single trailing byte across iterations when
      that happens.
    - Empty `text` yields nothing — caller must handle.
    - Network/auth errors propagate; voice_engine catches and falls
      back to Piper.
    """
    if not text or not text.strip():
        return
    if _voice_id is None:
        raise RuntimeError(
            "tts_elevenlabs not configured — call configure(voice_id) "
            "before synthesize_stream()."
        )
    client = _ensure_client()
    # API knobs tuned for "fastest reasonable response":
    #   - optimize_streaming_latency=3: ElevenLabs' aggressive latency
    #     mode. 0 = highest quality / 4 = fastest. 3 is the standard
    #     "real-time agent" recommendation; quality drop vs 0 is mild.
    #   - voice_settings: stability=0.3, similarity_boost=0.6, style=0
    #     (and use_speaker_boost off) speeds synthesis a few percent.
    #     Style at 0 means no exaggeration — replies sound calmer but
    #     synth is slightly faster + more consistent (no random
    #     "expressive" pauses that make TTS feel sluggish).
    convert_kwargs = dict(
        voice_id=_voice_id,
        text=text,
        model_id=_model_id,
        output_format="pcm_16000",
        optimize_streaming_latency=3,
    )
    # voice_settings is optional — pass via SDK type if available.
    try:
        from elevenlabs import VoiceSettings
        convert_kwargs["voice_settings"] = VoiceSettings(
            stability=0.3,
            similarity_boost=0.6,
            style=0.0,
            use_speaker_boost=False,
        )
    except Exception:
        # Older SDKs don't expose VoiceSettings — leave defaults.
        pass
    audio_iter = client.text_to_speech.convert(**convert_kwargs)

    # Buffer for an odd byte left over between chunks (PCM_16000 is
    # 2 bytes/sample; a chunk that ends mid-sample needs to combine
    # its tail with the head of the next chunk).
    leftover = b""
    for raw in audio_iter:
        if not raw:
            continue
        if leftover:
            raw = leftover + raw
            leftover = b""
        if len(raw) % 2:
            leftover = raw[-1:]
            raw = raw[:-1]
        if not raw:
            continue
        i16 = np.frombuffer(raw, dtype=np.int16)
        yield i16.astype(np.float32) / 32768.0


def warm_up():
    """Pre-flight the SDK + key check at startup so the first user
    interaction doesn't pay the auth round-trip and we surface any
    config errors loud and early instead of inside a voice pipeline."""
    _ensure_client()
