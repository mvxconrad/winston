"""VoiceEngine — orchestrates the full voice-to-voice loop.

Mirrors the role of brain.commentary_engine.CommentaryEngine, but for
the audio path instead of text. Both engines speak to the same
brain.client and the same memory, so Winston has a single brain across
both interaction modes.

State machine:

    IDLE ──[start_listening]──▶ LISTENING
                                   │
                                   ├─[stop_listening, audio empty]──▶ IDLE
                                   │
                                   └─[stop_listening, audio captured]──▶ TRANSCRIBING
                                                                              │
                                                       ┌──────────────────────┘
                                                       ▼
                                                  THINKING ──[LLM done]──▶ SPEAKING
                                                                              │
                                                                              └─[playback ends]──▶ IDLE

The orb subscribes to amplitude(): a thread-safe float that reflects the
engine's current state — speaker RMS while SPEAKING, synthetic pulse
while THINKING, ambient drift while IDLE.

What this engine does NOT do:
- Wake-word detection. v1 is push-to-talk. Add OpenWakeWord later as a
  thin wrapper that calls start_listening() / stop_listening().
- LLM prompt building. We delegate to brain.prompt for that, with a
  voice-specific system prompt that nudges Winston toward shorter,
  spoken-word-friendly responses (no markdown, no bullet lists).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, Optional

import numpy as np

from . import audio as audio_mod
from . import stt
from . import tts as tts_piper
from . import tts_elevenlabs


# Module-level TTS dispatcher. Set once at engine construction by reading
# config.TTS_PROVIDER. Splitting "which backend" from "how to call it"
# means the pipeline code below stays one path — `_tts.synthesize(text)`
# — regardless of provider. Fallback to Piper happens at call time, not
# here, so we keep the ability to try ElevenLabs again later in the
# session if the network came back.
_tts = tts_piper       # active provider (overridden by configure_tts)
_tts_fallback = None   # used when the primary fails (network, key, etc.)


def configure_tts(provider: str, voice_id: str = None,
                  model_id: str = None):
    """Pick the active TTS backend. Called by VoiceEngine.__init__ from
    values in config.py.

    `provider` is "piper" or "elevenlabs". Anything else falls back to
    Piper with a warning. ElevenLabs config (voice_id, model_id) is
    ignored when provider == "piper"."""
    global _tts, _tts_fallback
    if provider == "elevenlabs":
        tts_elevenlabs.configure(
            voice_id=voice_id or "JBFqnCBsd6RMkjVDRZzb",  # George default
            model_id=model_id or "eleven_flash_v2_5",
        )
        _tts = tts_elevenlabs
        # Keep Piper hot as the fallback so a network blip mid-conversation
        # doesn't take Winston's voice down.
        _tts_fallback = tts_piper
    else:
        if provider != "piper":
            print(f"[voice] unknown TTS_PROVIDER={provider!r}, using piper")
        _tts = tts_piper
        _tts_fallback = None


def _synthesize_with_fallback(text):
    """Try primary TTS; on any failure, log + fall back to Piper if
    configured. Keeps voice mode resilient: a transient ElevenLabs error
    becomes one robotic Piper reply, not a red ERROR orb."""
    try:
        return _tts.synthesize(text)
    except Exception as e:
        if _tts_fallback is None:
            raise
        print(f"[voice] primary TTS failed ({e!r}); falling back to piper",
              flush=True)
        return _tts_fallback.synthesize(text)


def _stream_with_fallback(text):
    """Streaming version. Yields audio chunks as the provider produces
    them. Falls back to the secondary provider (also as a stream) on
    primary failure — but only BEFORE we've yielded anything, since we
    can't unwind a partial reply already in the speaker.

    If the primary errors mid-stream, the partial audio that did play
    is preserved and the rest is dropped. Better than crashing.
    """
    primary = _tts
    fallback = _tts_fallback
    yielded_any = False
    try:
        for chunk in primary.synthesize_stream(text):
            yielded_any = True
            yield chunk
    except Exception as e:
        if yielded_any or fallback is None:
            print(f"[voice] streaming TTS error mid-reply: {e!r}", flush=True)
            return
        print(f"[voice] primary TTS stream failed ({e!r}); falling back to piper",
              flush=True)
        for chunk in fallback.synthesize_stream(text):
            yield chunk


def _warm_up_tts():
    """Warm primary; if it fails, warm the fallback so the first reply
    doesn't pay the Piper-load cost on top of an ElevenLabs error."""
    try:
        _tts.warm_up()
    except Exception as e:
        print(f"[voice] primary TTS warm_up failed ({e!r})", flush=True)
        if _tts_fallback is not None:
            _tts_fallback.warm_up()


# ──────────────── States ────────────────
STATE_IDLE = "IDLE"
STATE_LISTENING = "LISTENING"
STATE_TRANSCRIBING = "TRANSCRIBING"
STATE_THINKING = "THINKING"
STATE_SPEAKING = "SPEAKING"
STATE_ERROR = "ERROR"


# ──────────────── System prompt for voice mode ────────────────
# Critical: voice responses must be spoken-word-friendly. No markdown,
# no bullet lists, no code blocks (TTS would read them literally),
# no long answers (3-4 sentences is the sweet spot for back-and-forth).
VOICE_SYSTEM_PROMPT = """\
You are Winston, a calm and observant AI butler watching over Max's
machine. You can see real-time CPU, RAM, GPU, disk, network,
temperature, and process data — it's provided in each prompt.

You speak aloud through a TTS system. Therefore:
- Keep responses to 1-3 short sentences. You are conversational,
  not a report writer.
- Never use markdown, bullet points, asterisks, or code blocks.
- Read numbers naturally ("forty percent" not "40%") only when
  it would sound weird otherwise. "Fifty-three Celsius" is fine.
- Address Max as "Max" sparingly — once per conversation, not
  every reply.
- Be direct. If asked "how's my CPU?", answer with the percent and
  one sentence of context. Don't preamble.
- If you don't know something, say so briefly.
"""


def build_voice_prompt(user_text: str, sections: list) -> str:
    """Construct the LLM prompt with current panel snapshots + user query.

    We embed a compact text dump of the dashboard so Winston can answer
    "what's my CPU at" without any tool-calling. The format mirrors what
    brain/prompt.py does for the text loop, just trimmed for voice.
    """
    snapshot_lines = []
    for panel in sections:
        cls = type(panel).__name__
        # Cherry-pick the panels Winston cares about. Skipping CpuGraphPanel
        # (history-only, no point sending) and ProcessesPanel for now
        # (tokens budget — add back if needed).
        if cls == "CpuPanel":
            vals = getattr(panel, "values", []) or []
            if vals:
                avg = sum(vals) / len(vals)
                snapshot_lines.append(
                    f"CPU: {avg:.1f}% avg across {len(vals)} cores, "
                    f"max core {max(vals):.0f}%"
                )
        elif cls == "RamPanel":
            pct = getattr(panel, "value", 0) or 0
            used = getattr(panel, "used", 0) or 0
            total = getattr(panel, "total", 1) or 1
            gb = lambda b: b / (1024 ** 3)
            snapshot_lines.append(
                f"RAM: {pct:.0f}% — {gb(used):.1f}GB of {gb(total):.1f}GB"
            )
        elif cls == "GpuPanel":
            gpus = getattr(panel, "gpus", []) or []
            if gpus:
                g = gpus[0]
                util = g.get("util", 0) or 0
                mem_u = g.get("mem_used", 0) or 0
                mem_t = g.get("mem_total", 1) or 1
                vram_pct = mem_u / mem_t * 100
                temp = g.get("temp")
                t_str = f", {temp:.0f}°C" if temp else ""
                snapshot_lines.append(
                    f"GPU: {util:.0f}% util, VRAM {vram_pct:.0f}% "
                    f"({mem_u/(1024**3):.1f}GB of {mem_t/(1024**3):.1f}GB)"
                    f"{t_str} — {g.get('name', 'GPU')}"
                )
        elif cls == "TempsPanel":
            readings = getattr(panel, "readings", []) or []
            if readings:
                temp_str = ", ".join(
                    f"{label} {current:.0f}°C"
                    for label, current, _ in readings
                )
                snapshot_lines.append(f"Temperatures: {temp_str}")
        elif cls == "DiskPanel":
            disks = getattr(panel, "disks", []) or []
            for label, _kind, pct, used, total in disks:
                gb = lambda b: b / (1024 ** 3) if b > 1024 ** 3 else b / (1024 ** 2)
                snapshot_lines.append(
                    f"Disk {label}: {pct:.0f}% used"
                )
        elif cls == "NetworkPanel":
            rx = getattr(panel, "rx_rate", 0) or 0
            tx = getattr(panel, "tx_rate", 0) or 0
            rx_mbps = (rx * 8) / 1_000_000
            tx_mbps = (tx * 8) / 1_000_000
            snapshot_lines.append(
                f"Network: down {rx_mbps:.1f} Mbps, up {tx_mbps:.1f} Mbps"
            )
        elif cls == "SystemPanel":
            up = getattr(panel, "uptime_seconds", 0) or 0
            d, h = up // 86400, (up % 86400) // 3600
            snapshot_lines.append(
                f"Uptime: {int(d)}d {int(h)}h, {getattr(panel, 'proc_count', 0)} processes"
            )
        elif cls == "ProcessesPanel":
            procs = getattr(panel, "procs", []) or []
            top = procs[:3]
            if top:
                top_str = ", ".join(
                    f"{name} ({cpu:.0f}%)" for cpu, _mem, name, _pid in top
                )
                snapshot_lines.append(f"Top processes: {top_str}")

    snapshot = "\n".join(snapshot_lines) if snapshot_lines else "(no metrics available)"
    return (
        f"Current system state:\n{snapshot}\n\n"
        f"Max said: {user_text}\n\n"
        f"Reply briefly, in spoken style."
    )


# ──────────────── The engine ────────────────
class VoiceEngine:
    """Owns mic, speaker, STT, TTS, and the voice state machine.

    Usage from a Qt frontend:

        engine = VoiceEngine(sections, llm_config, memory)
        engine.warm_up()                    # pre-load STT + TTS

        # On space-key press:
        engine.start_listening()

        # On space-key release:
        engine.stop_listening()             # spawns the rest of the pipeline

        # Subscribe to state changes via callbacks (Qt frontend connects
        # these to signals that marshal onto the UI thread):
        engine.on_state_change = lambda s: ...
        engine.on_user_text = lambda t: ...        # transcription result
        engine.on_winston_text = lambda t: ...     # LLM reply

        # Orb reads at 60fps:
        amplitude = engine.amplitude()      # 0.0 - ~0.3
    """

    def __init__(self, sections: list, llm_config: dict, memory):
        self.sections = sections
        self.llm_config = llm_config or {}
        self.memory = memory

        # Resolve TTS backend from project config. Doing this here (not
        # at module import) means the dispatcher reads the user's config
        # exactly once per engine, and a unit test can construct an
        # engine with a different provider without poking globals.
        try:
            import config as project_config
            configure_tts(
                provider=getattr(project_config, "TTS_PROVIDER", "piper"),
                voice_id=getattr(project_config, "TTS_VOICE_ID", None),
                model_id=getattr(project_config, "TTS_MODEL_ID", None),
            )
        except Exception as e:
            print(f"[voice] TTS configure failed ({e!r}); using piper")
            configure_tts("piper")

        self._mic = audio_mod.MicRecorder()
        self._speaker = audio_mod.SpeakerPlayer()
        self._idle_env = audio_mod.IdleEnvelope()
        self._thinking_env = audio_mod.ThinkingEnvelope()

        # Open BOTH persistent audio streams now so the first
        # interaction never has to open a stream mid-pipeline. WSLg's
        # PulseAudio bridge reliably refuses to open a stream the second
        # time it's asked in a session — opening once at boot and
        # keeping the stream alive forever sidesteps that.
        try:
            self._mic.open()
        except Exception as e:
            print(f"[voice] mic open failed: {e!r} (will retry on first use)")
        try:
            self._speaker.open()
        except Exception as e:
            print(f"[voice] speaker open failed: {e!r} (will retry on first use)")

        self._state = STATE_IDLE
        self._lock = threading.Lock()

        # Callbacks — set by the frontend.
        self.on_state_change: Optional[Callable[[str], None]] = None
        self.on_user_text: Optional[Callable[[str], None]] = None
        self.on_winston_text: Optional[Callable[[str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Delegation hook. When set, push-to-talk hands off to an external
        # brain (e.g. PresenceFace driving CommentaryEngine) after STT —
        # the engine becomes pure I/O. When None, _run_pipeline does its
        # own LLM call (used for standalone testing of the voice loop).
        # The owner of the delegate is responsible for eventually calling
        # `speak_text(reply)` to drive Winston's voice and end the cycle.
        self.on_user_text_complete: Optional[Callable[[str], None]] = None

        # Last interaction times for orb decay.
        self._last_state_change = time.monotonic()

    # ──────────────── Lifecycle ────────────────
    def warm_up(self):
        """Pre-load STT + TTS models. Run on a background thread at
        startup so the first interaction is snappy.

        TTS warm-up uses the dispatcher (`_warm_up_tts`) so ElevenLabs
        gets its auth round-trip done now and any config error surfaces
        loud + early instead of inside the first user pipeline."""
        def _bg():
            try:
                stt.warm_up()
                _warm_up_tts()
            except Exception as e:
                self._set_error(f"warm-up failed: {e}")
        threading.Thread(target=_bg, daemon=True).start()

    @property
    def state(self) -> str:
        return self._state

    def speech_progress(self):
        """Forward to SpeakerPlayer.progress(). Used by the presence
        face's caption typewriter to sync on-screen text reveal to
        audio playback. Returns (samples_played, samples_total,
        stream_complete)."""
        return self._speaker.progress()

    def amplitude(self) -> float:
        """Read by the orb at paint time. Returns 0.0–~0.3.

        Source per state:
          SPEAKING    → live speaker RMS (Winston's voice)
          LISTENING   → live mic RMS (the user's voice) so the orb
                        reacts visibly while they talk
          THINKING/   → synthetic pulse — no real audio to sample
          TRANSCRIBING
          IDLE/other  → slow ambient breathing
        """
        s = self._state
        if s == STATE_SPEAKING:
            return self._speaker.amplitude()
        if s in (STATE_TRANSCRIBING, STATE_THINKING):
            return self._thinking_env.value()
        if s == STATE_LISTENING:
            # Real mic RMS plus a small floor so an absolutely quiet mic
            # still shows "I'm hearing" instead of dropping to ambient.
            return max(0.05, self._mic.amplitude())
        return self._idle_env.value()

    # ──────────────── State transitions ────────────────
    def _set_state(self, new_state: str):
        with self._lock:
            old = self._state
            if old == new_state:
                return
            self._state = new_state
            self._last_state_change = time.monotonic()
        if self.on_state_change is not None:
            try:
                self.on_state_change(new_state)
            except Exception:
                pass

    def _set_error(self, msg: str):
        # Print first — the GUI caption fades after 6s and the user often
        # misses it. Stdout sticks around so we can see what failed after
        # the fact. Strip this once the voice pipeline is reliable.
        print(f"[voice] ERROR: {msg}", flush=True)
        self._set_state(STATE_ERROR)
        if self.on_error is not None:
            try:
                self.on_error(msg)
            except Exception:
                pass

    # ──────────────── Public API: push-to-talk ────────────────
    def start_listening(self):
        """User pressed and is holding the talk key."""
        if self._state not in (STATE_IDLE, STATE_ERROR):
            # Don't start a new recording mid-pipeline. If Winston is
            # speaking, interrupt him first.
            if self._state == STATE_SPEAKING:
                self._speaker.stop()
            else:
                return
        try:
            self._mic.start()
            self._set_state(STATE_LISTENING)
        except Exception as e:
            self._set_error(f"mic start failed: {e}")

    def stop_listening(self):
        """User released the talk key. Captures the audio and runs the
        rest of the pipeline on a background thread."""
        if self._state != STATE_LISTENING:
            return
        audio = self._mic.stop()
        if len(audio) < 800:  # < 50ms — fat-fingered the key
            self._set_state(STATE_IDLE)
            return
        # Heavy work on a thread so the UI stays responsive.
        threading.Thread(
            target=self._run_pipeline, args=(audio,), daemon=True
        ).start()

    # ──────────────── Pipeline (background thread) ────────────────
    def _run_pipeline(self, audio: np.ndarray):
        """STT → (delegate brain | own brain) → TTS → playback. Runs
        entirely on a worker thread.

        Two paths after STT:
          A) Delegated (`on_user_text_complete` set): hand the transcript
             off to the external brain (PresenceFace → CommentaryEngine)
             and stop. The delegate is expected to call `speak_text(reply)`
             when ready. State stays at THINKING until that happens.
          B) Standalone (no delegate): call our own LLM with the simple
             voice prompt and TTS the reply inline. Used for quick
             smoke-testing the voice loop in isolation.
        """
        t_pipeline_start = time.monotonic()
        try:
            # 1) STT
            self._set_state(STATE_TRANSCRIBING)
            t_stt_start = time.monotonic()
            user_text = stt.transcribe(audio)
            stt_ms = (time.monotonic() - t_stt_start) * 1000
            print(f"[t] STT            +{stt_ms:.0f}ms (\"{(user_text or '')[:60]}\")",
                  flush=True)
            if not user_text:
                # No speech detected (or just silence). Bail quietly.
                self._set_state(STATE_IDLE)
                return
            if self.on_user_text is not None:
                try:
                    self.on_user_text(user_text)
                except Exception:
                    pass

            # 2) Hand-off point — delegated brain takes over from here.
            if self.on_user_text_complete is not None:
                self._set_state(STATE_THINKING)
                try:
                    self.on_user_text_complete(user_text)
                except Exception as e:
                    self._set_error(f"delegate brain failed: {e}")
                return

            # 2b) Standalone: own brain, own TTS.
            self._set_state(STATE_THINKING)
            prompt = build_voice_prompt(user_text, self.sections)
            reply = self._call_llm(prompt)
            if not reply:
                self._set_error("LLM returned no reply")
                return
            if self.on_winston_text is not None:
                try:
                    self.on_winston_text(reply)
                except Exception:
                    pass
            self.speak_text(reply)
        except Exception as e:
            self._set_error(f"pipeline error: {e}")

    # ──────────────── Public API: unprompted speech ────────────────
    def speak_text(self, text: str, on_done: Optional[Callable[[], None]] = None):
        """Synthesize `text` and play it through the speaker. Used by
        external orchestrators (PresenceFace + CommentaryEngine) when
        Winston needs to say something he wasn't asked — trigger
        commentary, heartbeat observations, etc.

        Sets state to SPEAKING for the duration of playback; returns to
        IDLE on drain. `on_done` fires once after the buffer drains —
        the face uses this to release the commentary engine's cooldown
        so the next event can fire.

        Resilience: if either TTS synthesis OR speaker playback fails
        (e.g. PulseAudio went away), surface the error to the caption
        but still call `on_done` — otherwise the commentary engine's
        cooldown stays armed and the entire trigger loop locks up.
        Winston should keep observing even when his voice is muted.

        Safe to call from any thread; the speaker callback is itself
        off-thread.
        """
        def _release(state=STATE_IDLE):
            self._set_state(state)
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

        if not text or not text.strip():
            _release()
            return

        if self.on_winston_text is not None:
            try:
                self.on_winston_text(text)
            except Exception:
                pass

        # NON-streaming path: synthesize the whole reply first, THEN
        # play it. Trades a longer pre-speech wait for guaranteed
        # smooth playback — once we have all the bytes locally,
        # playback can't underrun from network jitter.
        #
        # Why we abandoned streaming: ElevenLabs Flash returns audio
        # chunks over the network at ~realtime rate. A short network
        # hiccup (200-500ms gap between chunks) was draining the
        # speaker buffer mid-playback, producing the "u- a - o"
        # stuttering on longer 2nd+ replies. Pre-buffer cushions help
        # for the first gap but can't cover repeated jitter across a
        # 5-10s reply.
        #
        # Trade-off: time-to-first-audio jumps from ~700ms to ~1.5-2s
        # depending on reply length. User explicitly said latency is
        # less important than smooth speech.
        def _finished():
            _release()

        t_speak_start = time.monotonic()

        def _synth_and_play():
            """Off the UI thread: download the whole TTS reply, hand
            the buffer to the speaker as one shot. Once handed over,
            playback walks a local numpy array — no network on the
            audio path, no chance of mid-speech underrun."""
            try:
                audio_out = _synthesize_with_fallback(text)
            except Exception as e:
                self._set_error(f"TTS synth failed: {e}")
                _release()
                return
            if len(audio_out) == 0:
                _release()
                return
            dt = (time.monotonic() - t_speak_start) * 1000
            print(f"[t] TTS first audio +{dt:.0f}ms "
                  f"(non-streaming, {len(audio_out)/audio_mod.SAMPLE_RATE:.2f}s clip)",
                  flush=True)
            try:
                self._set_state(STATE_SPEAKING)
                self._speaker.play(audio_out, on_finished=_finished)
            except Exception as e:
                self._set_error(f"speaker play failed: {e}")
                _release()

        threading.Thread(target=_synth_and_play, daemon=True).start()

    def _on_speaker_finished(self):
        """Fires from the speaker callback when playback drains. Runs on
        a fresh thread (see SpeakerPlayer._callback)."""
        self._set_state(STATE_IDLE)

    # ──────────────── LLM ────────────────
    def _call_llm(self, prompt: str) -> str:
        """Synchronous LLM call — we're already on a worker thread, so
        we don't need brain.client's async path. Just wait for the full
        reply.

        Falls back gracefully if brain.client isn't importable (test mode).
        """
        try:
            from brain.client import generate
        except ImportError:
            # Test fallback when running standalone — return a fixed reply
            # so you can validate the audio loop without Ollama running.
            return f"I heard you say: {prompt[-100:]}. Ollama is not loaded."
        model = self.llm_config.get("model_quality") or self.llm_config.get("model") or "qwen2.5:7b-instruct"
        keep_alive = self.llm_config.get("quality_keep_alive_sec", 0)
        try:
            return generate(
                prompt,
                system=VOICE_SYSTEM_PROMPT,
                model=model,
                keep_alive=keep_alive,
            ).strip()
        except Exception as e:
            return f"I had trouble thinking. {e}"