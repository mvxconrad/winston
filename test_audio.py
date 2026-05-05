"""Audio hardware sanity check — runs before anything else.

WSL2 audio routes through WSLg's PulseAudio bridge to Windows. It works
out of the box on most setups but occasionally needs poking. Run this
FIRST. If it doesn't work, the full presence script won't either.

What it does:
1. Lists available input + output devices.
2. Records 3 seconds of mic input.
3. Plays it back through your speakers.
4. Tells you the peak amplitude so you know the mic is actually picking
   up sound (not just zero-arrays).

If you hear yourself: hardware works, move on to running winston_presence.py.
If you hear silence or get an error: see troubleshooting below.

Troubleshooting:
- "PortAudio not found" → `sudo apt install libportaudio2`
- No devices listed → check `pactl info` from inside WSL; should show
  a PulseAudio server. If not, restart WSL: `wsl --shutdown` from
  Windows, then reopen.
- Recording is silent (peak ~0.000) → check Windows mic privacy
  settings (Settings → Privacy → Microphone → "Allow apps to access").
- Choppy playback → reduce CHUNK_SAMPLES in brain/voice/audio.py to 512
  (lower latency, more callback churn).
"""
import sys
import time

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    print("ERROR: sounddevice not installed.")
    print("  pip install sounddevice")
    sys.exit(1)


SAMPLE_RATE = 16_000
DURATION = 3.0  # seconds


def main():
    print("=" * 60)
    print("WINSTON :: audio hardware check")
    print("=" * 60)

    print("\nAvailable devices:")
    print(sd.query_devices())

    print(f"\nDefault input:  {sd.default.device[0]}")
    print(f"Default output: {sd.default.device[1]}")

    print(f"\nRecording {DURATION}s of audio at {SAMPLE_RATE}Hz...")
    print("→ Speak now (say something so we can verify the mic).")
    print()
    for i in range(3, 0, -1):
        print(f"  {i}...", end=" ", flush=True)
        time.sleep(1)
    print("RECORDING")

    audio = sd.rec(
        int(DURATION * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    audio = audio.reshape(-1)

    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio * audio)))

    print(f"\nRecorded {len(audio)} samples.")
    print(f"  Peak amplitude: {peak:.4f}  (>0.05 means you spoke)")
    print(f"  RMS:            {rms:.4f}")

    if peak < 0.005:
        print("\n  ⚠  Peak is near zero — mic probably isn't capturing.")
        print("     Check Windows mic privacy settings + try again.")
    elif peak < 0.05:
        print("\n  ⚠  Peak is low — mic working but quiet. Try speaking louder")
        print("     or moving closer to the mic.")
    else:
        print("\n  ✓ Mic is picking up audio fine.")

    print("\nPlaying back what you said...")
    sd.play(audio, samplerate=SAMPLE_RATE)
    sd.wait()
    print("Playback done.")

    print("\n" + "=" * 60)
    print("If you heard yourself: audio is working. Run winston_presence.py")
    print("If silent: see troubleshooting at the top of this file.")
    print("=" * 60)


if __name__ == "__main__":
    main()