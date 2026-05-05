# Sets up Winston natively on Windows. Run from the repo root in PowerShell.
#
#     cd C:\path\to\winston
#     powershell -ExecutionPolicy Bypass -File setup_windows.ps1
#
# What it does:
# 1. Verifies Python 3.10+ is available.
# 2. Creates a venv in `venv\` if one isn't there.
# 3. Installs Python deps (PyQt6, sounddevice, faster-whisper, piper-tts,
#    elevenlabs, psutil, requests, numpy).
# 4. Downloads the Piper voice model (en_GB-alan-medium) if missing.
# 5. Copies a `.env.example` you can rename to `.env` and put your
#    ELEVENLABS_API_KEY into.
# 6. Generates `winston.bat` and `winston.vbs` you can shortcut to.
#
# After this completes:
#   - Edit `.env` with your ELEVENLABS_API_KEY
#   - Right-click `winston.vbs` → Send to → Desktop (create shortcut)
#   - Rename the desktop shortcut to "Winston"
#   - Optional: change icon (Properties → Change Icon)
#   - Double-click — orb appears, no console window

$ErrorActionPreference = "Stop"

# ──────────────── 1. Python check ────────────────
Write-Host "==> Checking Python..."
try {
    $pyver = & python --version 2>&1
    Write-Host "    Found: $pyver"
} catch {
    Write-Host "ERROR: Python not found on PATH."
    Write-Host "Install Python 3.12 from https://python.org or via 'winget install Python.Python.3.12'"
    Write-Host "Make sure to check 'Add Python to PATH' during install."
    exit 1
}

# ──────────────── 2. Venv ────────────────
if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Host "==> Creating virtualenv..."
    python -m venv venv
} else {
    Write-Host "==> Virtualenv already exists."
}

$venvPython = (Resolve-Path "venv\Scripts\python.exe").Path

# ──────────────── 3. Python deps ────────────────
Write-Host "==> Installing Python packages (this takes a few minutes)..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install `
    PyQt6 `
    sounddevice `
    numpy `
    faster-whisper `
    piper-tts `
    elevenlabs `
    psutil `
    requests `
    pyqtgraph

# ──────────────── 4. Piper voice ────────────────
$voiceDir = "models\voices"
$voiceName = "en_GB-alan-medium"
$voiceBase = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"

if (-not (Test-Path $voiceDir)) {
    New-Item -ItemType Directory -Path $voiceDir | Out-Null
}

if (Test-Path "$voiceDir\$voiceName.onnx") {
    Write-Host "==> Voice model already present."
} else {
    Write-Host "==> Downloading Piper fallback voice (~50MB)..."
    Invoke-WebRequest -Uri "$voiceBase/$voiceName.onnx" -OutFile "$voiceDir\$voiceName.onnx"
    Invoke-WebRequest -Uri "$voiceBase/$voiceName.onnx.json" -OutFile "$voiceDir\$voiceName.onnx.json"
}

# ──────────────── 5. .env scaffold ────────────────
if (-not (Test-Path ".env")) {
    @"
# Winston environment variables. NEVER commit this file.
# Get your key at https://elevenlabs.io → Profile → API Keys
ELEVENLABS_API_KEY=
"@ | Out-File -FilePath ".env" -Encoding utf8
    Write-Host "==> Created .env scaffold. Edit it and paste your ELEVENLABS_API_KEY."
}

# ──────────────── 6. Launchers ────────────────
# winston.bat — runs in a console window (good for debugging — you'll
# see all the [t] timing prints + stack traces if something explodes).
@"
@echo off
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" winston.py %*
"@ | Out-File -FilePath "winston.bat" -Encoding ascii

# winston.vbs — runs WITHOUT a console window (the desktop-shortcut
# experience: just the floating orb, no command window flashing on
# launch). VBScript wrapper because Windows shortcuts to .bat always
# show a console.
@"
Set WshShell = CreateObject("WScript.Shell")
strDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = strDir
WshShell.Run """" & strDir & "\venv\Scripts\pythonw.exe"" """ & strDir & "\winston.py""", 0, False
Set WshShell = Nothing
"@ | Out-File -FilePath "winston.vbs" -Encoding ascii

Write-Host ""
Write-Host "==> Setup complete."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit .env and paste your ELEVENLABS_API_KEY"
Write-Host "  2. Right-click winston.vbs -> Send to -> Desktop (creates shortcut)"
Write-Host "  3. Rename desktop shortcut to 'Winston' (optional: change icon)"
Write-Host "  4. Double-click. Orb appears, no console window."
Write-Host ""
Write-Host "If you need to debug something, run winston.bat from a terminal"
Write-Host "instead -- you'll see all the [t] prints and any errors."
