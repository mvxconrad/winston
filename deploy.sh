#!/usr/bin/env bash
# deploy.sh — one-command WSL → Windows deployment for Winston.
#
# Run from the repo root inside WSL:
#
#     cd ~/projects/sysmonitor
#     ./deploy.sh
#
# What it does:
#   1. Syncs source files to C:\Users\<you>\Winston (via /mnt/c/...)
#   2. Creates a Python venv on the Windows side (if missing)
#   3. Installs / upgrades pip dependencies
#   4. Downloads the Piper fallback voice model (if missing)
#   5. Copies .env (ElevenLabs key) if present in the repo
#   6. Generates winston.vbs (no-console launcher)
#   7. Optionally adds a startup shortcut
#
# Re-runnable: safe to call after every edit cycle. rsync only copies
# changed files; venv + deps are skipped if already satisfied.
#
# Requirements:
#   - WSL2 with rsync installed (sudo apt install rsync)
#   - Windows Python 3.10+ on PATH (python.org installer, "Add to PATH")
#   - Ollama running on Windows (for LLM)
#
set -euo pipefail

# ──────────────── Config ────────────────
# Windows username — auto-detected from the /mnt/c/Users directory.
# Override: WIN_USER=someone ./deploy.sh
if [[ -z "${WIN_USER:-}" ]]; then
    # Pick the first non-system user dir under /mnt/c/Users
    for d in /mnt/c/Users/*/; do
        base="$(basename "$d")"
        case "$base" in
            Public|Default|"Default User"|"All Users") continue ;;
            *) WIN_USER="$base"; break ;;
        esac
    done
fi

if [[ -z "${WIN_USER:-}" ]]; then
    echo "ERROR: Could not detect Windows username."
    echo "Set it manually:  WIN_USER=YourName ./deploy.sh"
    exit 1
fi

WIN_DEST="/mnt/c/Users/${WIN_USER}/Winston"
WIN_DEST_NATIVE="C:\\Users\\${WIN_USER}\\Winston"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "═══════════════════════════════════════════════════════"
echo "  Winston deploy: WSL → Windows"
echo "  Source:  ${SRC_DIR}"
echo "  Target:  ${WIN_DEST_NATIVE}"
echo "═══════════════════════════════════════════════════════"
echo

# ──────────────── 1. Sync source files ────────────────
echo "==> [1/7] Syncing source files..."
mkdir -p "${WIN_DEST}"

rsync -av --delete \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.git/' \
    --exclude='logs/' \
    --exclude='models/' \
    --exclude='.env' \
    --exclude='winston.bat' \
    --exclude='winston.vbs' \
    --exclude='winston-debug.bat' \
    --exclude='Winston.lnk' \
    --exclude='deploy.sh' \
    "${SRC_DIR}/" "${WIN_DEST}/"

echo "    Synced."
echo

# ──────────────── 2. Find Windows Python ────────────────
echo "==> [2/7] Locating Windows Python..."

# Look for python.exe on the Windows side (not WSL's python)
WIN_PYTHON=""
for candidate in \
    "/mnt/c/Users/${WIN_USER}/AppData/Local/Programs/Python/Python312/python.exe" \
    "/mnt/c/Users/${WIN_USER}/AppData/Local/Programs/Python/Python311/python.exe" \
    "/mnt/c/Users/${WIN_USER}/AppData/Local/Programs/Python/Python310/python.exe" \
    "/mnt/c/Python312/python.exe" \
    "/mnt/c/Python311/python.exe" \
    "/mnt/c/Python310/python.exe" \
    "/mnt/c/Program Files/Python312/python.exe" \
    "/mnt/c/Program Files/Python311/python.exe" \
    "/mnt/c/Program Files/Python310/python.exe"; do
    if [[ -f "$candidate" ]]; then
        WIN_PYTHON="$candidate"
        break
    fi
done

# Fallback: try python.exe on PATH (might be Windows Python via interop)
if [[ -z "$WIN_PYTHON" ]]; then
    WIN_PYTHON="$(command -v python.exe 2>/dev/null || true)"
fi

if [[ -z "$WIN_PYTHON" ]]; then
    echo "ERROR: Windows Python not found."
    echo "Install Python 3.12 from https://python.org"
    echo "Make sure to check 'Add Python to PATH' during install."
    exit 1
fi

echo "    Found: $("$WIN_PYTHON" --version 2>&1)"
echo

# Windows Python needs native Windows paths (C:\...), not WSL paths
# (/mnt/c/...). Convert once here; use WIN_DEST_W for all python.exe calls.
WIN_DEST_W="$(wslpath -w "${WIN_DEST}")"

# ──────────────── 3. Create venv ────────────────
echo "==> [3/7] Setting up virtualenv..."
VENV_PYTHON="${WIN_DEST}/venv/Scripts/python.exe"

if [[ ! -f "$VENV_PYTHON" ]]; then
    "$WIN_PYTHON" -m venv "${WIN_DEST_W}\\venv"
    echo "    Created new venv."
else
    echo "    Venv already exists."
fi
echo

# ──────────────── 4. Install dependencies ────────────────
echo "==> [4/7] Installing Python packages..."
"$VENV_PYTHON" -m pip install --upgrade pip --quiet
"$VENV_PYTHON" -m pip install --quiet \
    PyQt6 \
    pyqtgraph \
    rich \
    textual \
    psutil \
    sounddevice \
    numpy \
    faster-whisper \
    piper-tts \
    elevenlabs \
    requests

echo "    Done."
echo

# ──────────────── 5. Piper voice model ────────────────
echo "==> [5/7] Checking Piper fallback voice..."
VOICE_DIR="${WIN_DEST}/models/voices"
VOICE_NAME="en_GB-alan-medium"
VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"

mkdir -p "$VOICE_DIR"
if [[ -f "${VOICE_DIR}/${VOICE_NAME}.onnx" ]]; then
    echo "    Already present."
else
    echo "    Downloading (~50 MB)..."
    curl -fsSL "${VOICE_BASE}/${VOICE_NAME}.onnx" -o "${VOICE_DIR}/${VOICE_NAME}.onnx"
    curl -fsSL "${VOICE_BASE}/${VOICE_NAME}.onnx.json" -o "${VOICE_DIR}/${VOICE_NAME}.onnx.json"
    echo "    Downloaded."
fi
echo

# ──────────────── 6. Copy .env ────────────────
echo "==> [6/7] Checking .env..."
if [[ -f "${SRC_DIR}/.env" ]]; then
    if [[ ! -f "${WIN_DEST}/.env" ]]; then
        cp "${SRC_DIR}/.env" "${WIN_DEST}/.env"
        echo "    Copied .env from repo."
    else
        echo "    .env already exists on Windows side (not overwriting)."
    fi
else
    if [[ ! -f "${WIN_DEST}/.env" ]]; then
        cat > "${WIN_DEST}/.env" << 'ENVEOF'
# Winston environment variables. NEVER commit this file.
# Get your key at https://elevenlabs.io -> Profile -> API Keys
ELEVENLABS_API_KEY=
ENVEOF
        echo "    Created .env scaffold. Edit it and paste your ELEVENLABS_API_KEY."
    else
        echo "    .env already exists."
    fi
fi
echo

# ──────────────── 7. Generate launchers ────────────────
echo "==> [7/7] Generating launchers..."

# winston.bat — console window, good for debugging
cat > "${WIN_DEST}/winston.bat" << 'BATEOF'
@echo off
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" winston.py %*
BATEOF

# winston.vbs — NO console window (the daily-driver launcher)
# Uses pythonw.exe so no black box flashes on screen.
cat > "${WIN_DEST}/winston.vbs" << 'VBSEOF'
Set WshShell = CreateObject("WScript.Shell")
strDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = strDir
WshShell.Run """" & strDir & "\venv\Scripts\pythonw.exe"" """ & strDir & "\winston.py""", 0, False
Set WshShell = Nothing
VBSEOF

# winston-debug.bat — presence mode with console (for troubleshooting)
cat > "${WIN_DEST}/winston-debug.bat" << 'DBGEOF'
@echo off
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" winston.py --presence
pause
DBGEOF

echo "    Created: winston.bat, winston.vbs, winston-debug.bat"
echo

# ──────────────── Optional: startup shortcut ────────────────
STARTUP_DIR="/mnt/c/Users/${WIN_USER}/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
SHORTCUT_VBS="${WIN_DEST}/_create_shortcut.vbs"

if [[ ! -f "${STARTUP_DIR}/Winston.lnk" ]]; then
    echo "==> Creating Windows Startup shortcut..."
    # Generate a temporary VBScript that creates the .lnk shortcut.
    # Windows shortcuts (.lnk) can't be created from bash directly.
    cat > "$SHORTCUT_VBS" << LNKEOF
Set WshShell = CreateObject("WScript.Shell")
Set Shortcut = WshShell.CreateShortcut("${WIN_DEST_NATIVE}\\Winston.lnk")
Shortcut.TargetPath = "${WIN_DEST_NATIVE}\\winston.vbs"
Shortcut.WorkingDirectory = "${WIN_DEST_NATIVE}"
Shortcut.Description = "Winston system monitor"
Shortcut.Save
Set Shortcut = Nothing
Set WshShell = Nothing
LNKEOF
    # Run the VBScript via Windows Script Host
    if command -v wscript.exe &>/dev/null; then
        wscript.exe "$(wslpath -w "$SHORTCUT_VBS")" 2>/dev/null || true
        # Copy the shortcut to the Startup folder
        if [[ -f "${WIN_DEST}/Winston.lnk" ]]; then
            cp "${WIN_DEST}/Winston.lnk" "${STARTUP_DIR}/Winston.lnk" 2>/dev/null || true
            echo "    Startup shortcut created. Winston will launch on login."
        else
            echo "    (Shortcut creation failed — you can drag winston.vbs to Startup manually)"
        fi
    else
        echo "    (wscript.exe not found — create startup shortcut manually)"
    fi
    rm -f "$SHORTCUT_VBS"
else
    echo "==> Startup shortcut already exists."
fi

echo
echo "═══════════════════════════════════════════════════════"
echo "  Deploy complete!"
echo ""
echo "  To run Winston (watchdog mode — sits in tray):"
echo "    Double-click ${WIN_DEST_NATIVE}\\winston.vbs"
echo ""
echo "  To run with console (debug):"
echo "    ${WIN_DEST_NATIVE}\\winston.bat"
echo ""
echo "  To run the old always-visible orb:"
echo "    ${WIN_DEST_NATIVE}\\winston.bat --presence"
echo ""
echo "  To re-deploy after editing source in WSL:"
echo "    ./deploy.sh"
echo "═══════════════════════════════════════════════════════"
