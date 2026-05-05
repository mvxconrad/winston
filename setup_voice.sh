#!/usr/bin/env bash
# Sets up Winston's voice subsystem on WSL2 / Linux.
#
# Run from the repo root:
#     bash setup_voice.sh
#
# What it does:
# 1. Installs PortAudio system lib (sounddevice's C dependency).
# 2. Installs Python packages: sounddevice, faster-whisper, piper-tts.
# 3. Downloads the en_GB-alan-medium Piper voice model (~50MB).
#
# After this completes:
#     python3 test_audio.py        # verify hardware
#     python3 winston_presence.py  # run the full thing

set -e

# ──────────────── System deps ────────────────
echo "==> Installing system packages..."
sudo apt update
# libasound2-plugins is the ALSA→PulseAudio bridge. Without it,
# PortAudio probes ALSA, finds no devices, and sounddevice returns an
# empty device list on WSLg even though /mnt/wslg/PulseServer is up.
# pulseaudio-utils ships pactl/paplay/parecord for diagnostics.
sudo apt install -y libportaudio2 libsndfile1 libasound2-plugins pulseaudio-utils

# ──────────────── Python deps ────────────────
echo
echo "==> Installing Python packages..."
# faster-whisper pulls in CTranslate2 which has the Whisper inference.
# piper-tts pulls in onnxruntime for the voice model.
# sounddevice gives us PortAudio bindings.
pip install \
    sounddevice \
    numpy \
    faster-whisper \
    piper-tts

# ──────────────── Voice model ────────────────
VOICE_DIR="models/voices"
VOICE_NAME="en_GB-alan-medium"
VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"

mkdir -p "$VOICE_DIR"

if [ -f "$VOICE_DIR/$VOICE_NAME.onnx" ]; then
    echo
    echo "==> Voice model already present — skipping download."
else
    echo
    echo "==> Downloading Piper voice ($VOICE_NAME, ~50MB)..."
    curl -L -o "$VOICE_DIR/$VOICE_NAME.onnx" \
        "$VOICE_BASE/$VOICE_NAME.onnx"
    curl -L -o "$VOICE_DIR/$VOICE_NAME.onnx.json" \
        "$VOICE_BASE/$VOICE_NAME.onnx.json"
fi

echo
echo "==> Done."
echo
echo "Next steps:"
echo "  1. python3 test_audio.py        # verify mic + speakers"
echo "  2. python3 winston_presence.py  # run the orb"
echo
echo "Hold SPACE to talk. Press Q to quit."