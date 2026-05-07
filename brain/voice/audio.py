"""Audio I/O — mic capture, speaker playback, RMS amplitude envelope.

Why a dedicated module: PortAudio (via sounddevice) has gotchas around
device selection, sample rates, and threading. Concentrating that here
means voice_engine.py and the orb don't have to know about audio
hardware.

Hardware constraints on this project's setup:
- Runs in WSL2 with WSLg providing PulseAudio passthrough to Windows.
  Mic and speaker both work, but device names look weird (e.g. "PulseAudio
  pulse"). We don't pin device names — just use defaults and let the OS
  figure it out.
- 16kHz mono is the right sample rate: matches what Whisper and Piper
  both want, halves the buffer size vs 44.1kHz, and the speech band
  doesn't need more.

The "amplitude envelope" is the magic value the orb reads to pulse on
Winston's voice. It's just RMS over a small rolling window of the most
recent output frames, exposed as a thread-safe atomic float.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None


# ──────────────── Constants ────────────────
SAMPLE_RATE = 16_000           # Hz — speech band, matches Whisper + Piper
CHANNELS = 1                    # mono
# 2048-sample blocks (~128ms) — larger than the latency-purist 1024 we
# started with, but PortAudio's ALSA backend on WSLg flakes with
# `paTimedOut` at 1024 (the realtime callback thread can't keep up
# crossing the WSL→Windows audio bridge). 128ms is still imperceptible
# for push-to-talk and conversational TTS.
CHUNK_SAMPLES = 8192           # ~512ms — large headroom against GIL hiccups
ENVELOPE_BIN_SAMPLES = 480     # ~30ms — granularity of the pre-computed amplitude envelope

# PortAudio latency hint. 'high' lets the backend pick a comfortable
# buffer size; on WSLg ALSA this is the difference between "works" and
# "callback thread times out". 'low' is what was failing before.
PA_LATENCY = "high"


# ──────────────── Mic capture ────────────────
class MicRecorder:
    """Records audio from the default input device into an in-memory buffer.

    Push-to-talk usage:
        rec = MicRecorder()
        rec.open()         # once at startup — keep the stream alive forever
        rec.start()        # while space held — flips a flag, captures begin
        ...
        audio = rec.stop() # numpy float32 array, normalized to [-1, 1]
        # hand `audio` to STT

    Why "persistent" stream:
      The naive design opens InputStream on every push-to-talk and closes
      it on release. On WSLg, PortAudio's ALSA→Pulse plugin reliably hits
      `paTimedOut` after the first or second cycle — its callback thread
      can't be created repeatedly. The fix is to open the stream ONCE at
      startup and never close it. The audio thread always runs; we just
      gate whether to keep the chunks via a `_recording` flag inside the
      callback.

      Cost: the audio thread runs continuously and the envelope updates
      even when not recording. Both are ~free at 16kHz mono. Benefit:
      no more `paTimedOut`, sub-millisecond start latency.

    Threading: sounddevice's InputStream uses its own audio callback thread.
    We accumulate chunks into a list under a lock and concatenate on stop().
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        if sd is None:
            raise RuntimeError(
                "sounddevice not installed — `pip install sounddevice`"
            )
        self.sample_rate = sample_rate
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: Optional[sd.InputStream] = None
        self._recording = False
        # Live RMS so the orb can pulse while the user is talking.
        # Updated only on the chunk we just received — no separate
        # envelope buffer. Same reason as SpeakerPlayer's callback:
        # heavy Python in the audio callback causes underruns on
        # the OUTPUT side because both callbacks share GIL pressure.
        self._amplitude = 0.0

    def _callback(self, indata, frames, t, status):
        # `indata` is shape (frames, channels). We're mono — flatten.
        # Don't print on `status` — overruns happen on WSL audio and
        # spamming stdout would flood the terminal.
        flat = indata.reshape(-1)
        with self._lock:
            # Cheap chunk-RMS, smoothed. This is what the orb reads
            # via .amplitude(). No envelope buffer to iterate — that
            # was killing audio timing on long replies.
            chunk_rms = float(np.sqrt(np.mean(flat * flat)) + 1e-9)
            self._amplitude = 0.7 * self._amplitude + 0.3 * chunk_rms
            # Only buffer when recording is armed. This is the gate that
            # makes "persistent stream + push-to-talk" work.
            if self._recording:
                self._chunks.append(flat.copy())

    def open(self):
        """Open the persistent input stream. Call once at startup.

        Retries once on `paTimedOut` because that error sometimes hits
        cold on first WSL boot — the second try almost always succeeds.
        After this, the stream stays open for the rest of the process
        lifetime; start()/stop() just flip the recording flag.
        """
        if self._stream is not None:
            return  # already open

        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=CHANNELS,
                    dtype="float32",
                    blocksize=CHUNK_SAMPLES,
                    latency=PA_LATENCY,
                    callback=self._callback,
                )
                self._stream.start()
                return
            except Exception as e:
                last_err = e
                # Tear down the half-open stream so the retry isn't
                # poisoned by a dangling InputStream object.
                if self._stream is not None:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                if attempt == 0:
                    time.sleep(0.1)  # let the previous stream's threads die
        raise RuntimeError(f"mic stream failed after retry: {last_err}")

    def close(self):
        """Tear down the persistent stream. Only call at process shutdown
        — this is what we tried to avoid doing per-press."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None

    def start(self):
        """Arm recording. Lazy-opens the stream if it wasn't pre-opened
        (so a frontend that forgot to call open() at boot still works,
        just at the cost of a paTimedOut risk on first press)."""
        if self._stream is None:
            self.open()
        with self._lock:
            self._chunks.clear()
            self._recording = True

    def stop(self) -> np.ndarray:
        """Disarm recording and return the captured audio as a 1-D
        float32 array in [-1, 1]. Stream stays open."""
        with self._lock:
            if not self._recording:
                return np.zeros(0, dtype=np.float32)
            self._recording = False
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            audio = np.concatenate(self._chunks).astype(np.float32, copy=False)
            self._chunks.clear()
        return audio

    def amplitude(self) -> float:
        """RMS of the most recent ~100ms of mic input. Read by the orb so
        it pulses on the user's voice. Updates whenever the stream is
        open, so callers should gate on the appropriate engine state."""
        return self._amplitude

    @property
    def is_recording(self) -> bool:
        return self._recording


# ──────────────── Speaker playback + envelope ────────────────
class SpeakerPlayer:
    """Plays audio through the default output device and exposes an RMS
    amplitude envelope for the orb to pulse on.

    Cross-platform via sounddevice — uses WASAPI on Windows (rock
    solid), ALSA→PulseAudio on Linux/WSL (the WSL path has chop
    issues; this is the primary reason for moving Winston to native
    Windows).

    Threading: the OutputStream callback runs on PortAudio's audio
    thread. We pre-compute the amplitude envelope in `play()` so the
    callback only does a numpy slice + cursor advance — keeping it
    fast enough to never miss its deadline.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        if sd is None:
            raise RuntimeError(
                "sounddevice not installed — `pip install sounddevice`"
            )
        self.sample_rate = sample_rate

        # Pending audio is held as one big numpy array; the callback
        # shaves CHUNK_SAMPLES off the front each tick. Simpler than a
        # queue for v1 since we always know the full TTS clip up front.
        self._pending: np.ndarray = np.zeros(0, dtype=np.float32)
        self._cursor = 0
        self._lock = threading.Lock()

        self._stream: Optional[sd.OutputStream] = None
        self._on_finished: Optional[Callable[[], None]] = None
        self._finished_fired = False
        # Pre-computed amplitude envelope. Filled in play() — one RMS
        # value per ENVELOPE_BIN_SAMPLES of audio. The audio callback
        # NEVER computes RMS; it just copies bytes. amplitude() does
        # an O(1) lookup into this array based on the cursor.
        # This is the single biggest reason mid-speech stuttering went
        # away: the audio callback's wall-clock time dropped from
        # ~5-10ms (with the old envelope deque iteration) to ~100µs
        # (just a numpy slice copy). PortAudio's audio thread is no
        # longer GIL-blocked by other Python threads.
        self._envelope: np.ndarray = np.zeros(0, dtype=np.float32)
        # Streaming flag: True while we're still expecting more audio
        # via append(). The callback won't fire on_finished until the
        # caller flips this False with mark_complete() AND the buffer
        # drains. Without this, a streamed clip would prematurely fire
        # on_finished the first time the buffer briefly emptied between
        # incoming chunks.
        self._stream_complete = True

    def _callback(self, outdata, frames, t, status):
        # CRITICAL: runs on PortAudio's audio thread. MUST complete in
        # << CHUNK_SAMPLES / SAMPLE_RATE seconds (now 512ms at 16kHz)
        # or ALSA underruns and the user hears "u- a - o" speech.
        #
        # ABSOLUTE MINIMUM here:
        #   - acquire lock briefly
        #   - one numpy slice + copy
        #   - advance cursor
        #   - end-of-stream check
        #
        # NO RMS / amplitude work. That's pre-computed in play() and
        # looked up by amplitude() on a different thread.
        if status:
            pass  # underruns happen, not catastrophic for speech
        with self._lock:
            remaining = len(self._pending) - self._cursor
            if remaining <= 0:
                outdata.fill(0)
                if (self._stream_complete
                        and not self._finished_fired
                        and self._on_finished is not None):
                    self._finished_fired = True
                    cb = self._on_finished
                    threading.Thread(target=cb, daemon=True).start()
                return

            n = min(frames, remaining)
            outdata[:n, 0] = self._pending[self._cursor : self._cursor + n]
            if n < frames:
                outdata[n:, 0] = 0
            self._cursor += n

    def open(self):
        """Open the persistent output stream. Call once at startup.

        Same logic as MicRecorder.open: WSLg's PulseAudio bridge
        sometimes refuses to (re)open a stream — `PulseAudio: Unable to
        connect: Timeout` — when called repeatedly inside a session.
        Keeping ONE OutputStream alive for the process lifetime
        sidesteps that. The callback always runs; when nothing's queued
        it just fills the output buffer with zeros (silence).

        Retries once on first-cold failure for the same reason
        MicRecorder does.
        """
        if self._stream is not None:
            return
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                self._stream = sd.OutputStream(
                    samplerate=self.sample_rate,
                    channels=CHANNELS,
                    dtype="float32",
                    blocksize=CHUNK_SAMPLES,
                    latency=PA_LATENCY,
                    callback=self._callback,
                )
                self._stream.start()
                return
            except Exception as e:
                last_err = e
                if self._stream is not None:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                if attempt == 0:
                    time.sleep(0.1)
        raise RuntimeError(f"speaker stream failed after retry: {last_err}")

    def close(self):
        """Tear down the persistent stream. Only call at process shutdown."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None

    def play(self, audio: np.ndarray, on_finished: Optional[Callable[[], None]] = None):
        """Queue `audio` for playback. Replaces anything currently playing.

        `audio` must be a 1-D float32 array in [-1, 1] at self.sample_rate.
        `on_finished` fires once when the buffer drains.

        Builds the amplitude envelope here (once, off the audio
        thread) so the callback never has to compute RMS. The envelope
        gets one RMS value per ENVELOPE_BIN_SAMPLES of audio (~30ms
        each at 16kHz — fine granularity for the orb's ~30Hz pulse).
        """
        if self._stream is None:
            self.open()
        audio = audio.astype(np.float32, copy=False)
        # Pre-compute the amplitude envelope. Reshape into bins of
        # ENVELOPE_BIN_SAMPLES, take RMS per bin. Tail samples that
        # don't fill a bin get one extra padded bin — better than
        # dropping them. This is ~1ms of work for a 10s clip; happens
        # ONCE per reply on the calling thread, never on the audio
        # thread.
        n = len(audio)
        if n > 0:
            full_bins = n // ENVELOPE_BIN_SAMPLES
            envelope = np.zeros(full_bins + 1, dtype=np.float32)
            if full_bins > 0:
                trimmed = audio[:full_bins * ENVELOPE_BIN_SAMPLES]
                shaped = trimmed.reshape(full_bins, ENVELOPE_BIN_SAMPLES)
                envelope[:full_bins] = np.sqrt(np.mean(shaped * shaped, axis=1))
            tail = audio[full_bins * ENVELOPE_BIN_SAMPLES:]
            if len(tail) > 0:
                envelope[full_bins] = float(np.sqrt(np.mean(tail * tail)))
        else:
            envelope = np.zeros(0, dtype=np.float32)
        with self._lock:
            self._pending = audio
            self._cursor = 0
            self._on_finished = on_finished
            self._finished_fired = False
            # All-at-once: stream is "complete" the moment we hand it
            # over, so the callback can fire on_finished as soon as the
            # buffer drains.
            self._stream_complete = True
            self._envelope = envelope

    def play_streaming(self, on_finished: Optional[Callable[[], None]] = None):
        """Open a streaming playback session.

        Use this when audio arrives in chunks (e.g. from a streaming TTS
        provider). After this call:
            - Call `append(chunk)` for each new chunk as it arrives.
            - Call `mark_complete()` once you've handed over the final
              chunk. on_finished will fire when the buffer drains AFTER
              that point, not before — so transient empty-buffer moments
              between chunks don't prematurely end the session.

        Replaces any currently-playing audio.
        """
        if self._stream is None:
            self.open()
        with self._lock:
            self._pending = np.zeros(0, dtype=np.float32)
            self._cursor = 0
            self._on_finished = on_finished
            self._finished_fired = False
            self._stream_complete = False

    def append(self, audio: np.ndarray):
        """Concatenate `audio` to the playback queue. Pairs with
        `play_streaming` — silently no-ops on an empty input.

        Trims already-consumed samples off the front so memory usage
        stays bounded across long replies. Cheap (numpy slice + concat;
        speech utterances are seconds, not minutes).

        Rebuilds the amplitude envelope after each append so the orb
        pulses correctly during sentence-streamed playback. Sub-ms even
        for a 10s buffer — the reshape+mean is trivial numpy."""
        if audio is None or len(audio) == 0:
            return
        with self._lock:
            if self._cursor > 0:
                self._pending = self._pending[self._cursor:]
                self._cursor = 0
            self._pending = np.concatenate([
                self._pending,
                audio.astype(np.float32, copy=False),
            ])
            # Rebuild the envelope so amplitude() tracks the new buffer
            # layout. Without this, amplitude() returns 0 during streamed
            # playback because play_streaming() starts with an empty
            # envelope and the old append() never updated it.
            n = len(self._pending)
            if n > 0:
                full_bins = n // ENVELOPE_BIN_SAMPLES
                env = np.zeros(full_bins + 1, dtype=np.float32)
                if full_bins > 0:
                    trimmed = self._pending[:full_bins * ENVELOPE_BIN_SAMPLES]
                    shaped = trimmed.reshape(full_bins, ENVELOPE_BIN_SAMPLES)
                    env[:full_bins] = np.sqrt(np.mean(shaped * shaped, axis=1))
                tail = self._pending[full_bins * ENVELOPE_BIN_SAMPLES:]
                if len(tail) > 0:
                    env[full_bins] = float(np.sqrt(np.mean(tail * tail)))
                self._envelope = env
            else:
                self._envelope = np.zeros(0, dtype=np.float32)

    def mark_complete(self):
        """Signal the streaming session is done — no more append() calls.

        on_finished is now allowed to fire from the callback when the
        buffer next drains. If the buffer is already empty when this
        runs (synthesis finished faster than playback), the next
        callback tick will fire on_finished immediately."""
        with self._lock:
            self._stream_complete = True

    def stop(self):
        """Hard-stop playback. Drops queued samples + clears the on_finished
        callback. Stream stays open — the callback continues firing and
        outputs silence until the next play() call.

        on_finished does NOT fire here; it's reserved for natural
        completion (buffer drains naturally on its own)."""
        with self._lock:
            self._pending = np.zeros(0, dtype=np.float32)
            self._cursor = 0
            self._on_finished = None
            # Clear the envelope so amplitude() returns 0 immediately
            # after an interrupt instead of showing the last bin until
            # the next play().
            self._envelope = np.zeros(0, dtype=np.float32)
        # NOTE: stream NOT closed — see open() docstring for why.

    def shutdown(self):
        """Process-shutdown: hard-stop + close the stream. Use this when
        the program is exiting; otherwise prefer stop()."""
        self.stop()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def amplitude(self) -> float:
        """O(1) lookup into the pre-computed amplitude envelope based
        on the current playback cursor. Called by the orb at ~30Hz on
        the UI thread. Cheap — no numpy work, no RMS computation, just
        a single array index.

        Returns 0.0 when the speaker is idle or the envelope is empty.
        """
        with self._lock:
            if len(self._envelope) == 0:
                return 0.0
            # Bin index = how many ENVELOPE_BIN_SAMPLES we've played past.
            idx = self._cursor // ENVELOPE_BIN_SAMPLES
            if idx >= len(self._envelope):
                return 0.0
            return float(self._envelope[idx])

    def progress(self):
        """Return (samples_played, samples_known_total, stream_complete).

        - `samples_played`: how many samples have been emitted to the
          speaker so far.
        - `samples_known_total`: how many samples are currently buffered
          (grows during streaming as more chunks arrive).
        - `stream_complete`: True once `mark_complete()` has fired —
          after that, samples_known_total is the FINAL total and a
          progress fraction (played/total) is meaningful.

        Used by the caption typewriter so on-screen text reveals in
        lockstep with the audio playback cursor instead of guessing
        from a fixed chars-per-second rate.
        """
        with self._lock:
            return self._cursor, len(self._pending), self._stream_complete

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._cursor < len(self._pending)


# ──────────────── Synthetic envelope for non-speaking states ────────────────
@dataclass
class IdleEnvelope:
    """Generates a slow ambient pulse for the orb when Winston isn't
    actively speaking. Drift between min and max with a sine wave so the
    orb feels alive instead of static."""
    min_value: float = 0.02
    max_value: float = 0.08
    period_sec: float = 4.0

    def value(self, now: Optional[float] = None) -> float:
        if now is None:
            now = time.monotonic()
        phase = (now % self.period_sec) / self.period_sec
        # Sine wave from min to max
        s = (np.sin(phase * 2 * np.pi) + 1) * 0.5
        return float(self.min_value + (self.max_value - self.min_value) * s)


@dataclass
class ThinkingEnvelope:
    """Faster, irregular pulse — Winston is processing. Drives the orb
    differently from speaking (no real audio) and from idle (visibly
    more activity)."""
    min_value: float = 0.05
    max_value: float = 0.20
    period_sec: float = 0.8

    def value(self, now: Optional[float] = None) -> float:
        if now is None:
            now = time.monotonic()
        phase = (now % self.period_sec) / self.period_sec
        # Two overlapping sines for a less metronomic feel
        a = np.sin(phase * 2 * np.pi)
        b = np.sin(phase * 6 * np.pi) * 0.3
        s = ((a + b) + 1.3) / 2.6
        return float(self.min_value + (self.max_value - self.min_value) * s)