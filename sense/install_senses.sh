#!/usr/bin/env bash
# ============================================================
#  Serenity Senses Installer — Linux / macOS
#  Installs Eyes, Ears, Voice (TTS), and Visual Memory.
#
#  Vision stack:
#    opencv-python  — camera frame grab only (VideoCapture)
#    mss            — fast screen capture (~5ms/frame)
#    Pillow         — image handling
#    imagehash      — perceptual change detection
#    MiniCPM-V 4.6  — all scene/face/emotion understanding via Ollama
#
#  Visual Memory stack:
#    SQLite (stdlib)  — episodic memory of what Serenity sees
#    imagehash        — perceptual hashing, change detection
#    mss              — fast screen capture
#
#  Audio stack:
#    Whisper small (faster-whisper), sounddevice
#
#  Voice (TTS):
#    Qwen3 TTS (local GPU), ElevenLabs, edge-tts
#
#  NVIDIA GPU recommended for Whisper / TTS only.
#  Vision runs entirely via Ollama — no extra VRAM beyond minicpm.
#  Linux Wayland note: mss requires XWayland for screen capture.
#
#  Usage:
#    chmod +x install_senses.sh
#    ./install_senses.sh
# ============================================================

set -euo pipefail

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
RESET="\033[0m"

info()  { echo -e " ${GREEN}[OK]${RESET}  $*"; }
warn()  { echo -e " ${YELLOW}[WARN]${RESET} $*"; }
error() { echo -e " ${RED}[ERR]${RESET} $*"; exit 1; }
step()  { echo -e "\n${BOLD}$*${RESET}"; }

echo ""
echo " ===================================================="
echo "  Serenity Senses Installer"
echo "  Eyes | Ears (Whisper small) | Voice (TTS)"
echo " ===================================================="
echo ""

# ── Sanity checks ─────────────────────────────────────────
command -v python3 &>/dev/null || error "python3 not found. Install Python 3.10+ first."
command -v pip    &>/dev/null || error "pip not found. Run: python3 -m ensurepip"

PY_VER=$(python3 --version)
echo " Python: $PY_VER"
echo ""

# ── Detect CUDA ───────────────────────────────────────────
CUDA_AVAILABLE=0
GPU_NAME="(none)"
if python3 -c "import torch; exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    CUDA_AVAILABLE=1
    GPU_NAME=$(python3 -c "import torch; print(torch.cuda.get_device_name(0))" 2>/dev/null || echo "Unknown GPU")
    echo " GPU:  $GPU_NAME"
    echo " CUDA: available  (used for Whisper / TTS only)"
else
    if command -v nvidia-smi &>/dev/null; then
        echo " GPU:  NVIDIA detected but CUDA PyTorch not installed yet."
        echo "       Will install CUDA 12.4 build."
        GPU_NAME="NVIDIA (needs torch install)"
    else
        echo " GPU:  No NVIDIA GPU found — CPU build will be used."
        warn "Whisper small will be slower on CPU."
    fi
fi
echo ""

OS_TYPE=$(uname -s)

# ── Step 1: PyTorch ───────────────────────────────────────
step "[1/5] Installing PyTorch..."
if [ "$CUDA_AVAILABLE" -eq 0 ]; then
    if command -v nvidia-smi &>/dev/null; then
        echo "       Installing CUDA 12.4 build..."
        pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
    else
        echo "       Installing CPU build..."
        pip install torch torchvision torchaudio
    fi
else
    echo "       CUDA PyTorch already present — skipping."
fi
info "PyTorch done."

# ── Step 2: Vision (Eyes) ─────────────────────────────────
step "[2/5] Installing Vision stack (Eyes)..."
echo "       opencv-python, mss, Pillow, imagehash"
echo "       MiniCPM-V 4.6 handles all analysis via Ollama — no extra packages."
echo ""

# System dependencies for OpenCV headless
if [ "$OS_TYPE" = "Linux" ]; then
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y \
            libgl1 \
            libglib2.0-0 \
            libsm6 \
            libxext6 \
            libxrender-dev \
            2>/dev/null || true
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y mesa-libGL glib2 2>/dev/null || true
    fi
fi

if [ "$OS_TYPE" = "Darwin" ]; then
    pip install opencv-python-headless mss Pillow imagehash || warn "Some vision packages failed."
else
    # Linux: install xlib for mss X11 screen capture support
    if [ "$OS_TYPE" = "Linux" ]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y python3-xlib scrot 2>/dev/null || true
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3-xlib scrot 2>/dev/null || true
        fi
        # Wayland users: mss needs XWayland
        if [ "${WAYLAND_DISPLAY:-}" != "" ] && [ "${DISPLAY:-}" = "" ]; then
            warn "Wayland detected without XWayland. Screen capture (mss) needs XWayland."
            echo "       Run: export DISPLAY=:0  or enable XWayland in your compositor."
        fi
    fi
    pip install opencv-python mss Pillow imagehash || warn "Some vision packages failed."
fi
info "Vision done."

# ── Step 3: Audio (Ears) ──────────────────────────────────
step "[3/5] Installing Audio stack (Ears)..."
echo "       sounddevice, scipy, faster-whisper, soundfile, transformers..."

# Linux: portaudio for sounddevice
if [ "$OS_TYPE" = "Linux" ]; then
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y portaudio19-dev libsndfile1 ffmpeg 2>/dev/null || \
            warn "Could not install system audio libs — sounddevice may fail."
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y portaudio-devel libsndfile ffmpeg 2>/dev/null || true
    fi
fi

# macOS: portaudio + ffmpeg via brew
if [ "$OS_TYPE" = "Darwin" ]; then
    if command -v brew &>/dev/null; then
        brew install portaudio ffmpeg 2>/dev/null || true
    fi
fi

pip install sounddevice scipy faster-whisper soundfile transformers accelerate || \
    warn "Some audio packages failed."
info "Audio done."

# ── Step 4: Voice / TTS base packages ─────────────────────
step "[4/5] Installing TTS base packages..."
echo "       edge-tts (free, no key), pydub (audio conversion), pygame (playback)"
echo ""
echo "       NOTE: Provider-specific packages are installed separately."
echo "       The setup wizard shows the exact pip command after you pick"
echo "       your TTS engine.  Quick reference:"
echo ""
echo "         Qwen3 local    pip install -U qwen-tts"
echo "         Kokoro         pip install kokoro soundfile"
echo "         Coqui XTTS-v2  pip install TTS"
echo "         Bark           pip install suno-bark"
echo "         ElevenLabs     pip install httpx  (built-in, no extra package)"
echo "         Google TTS     pip install google-cloud-texttospeech"
echo "         Amazon Polly   pip install boto3"
echo "         Cartesia       pip install cartesia"
echo "         PlayHT         pip install pyht"
echo "         Deepgram       pip install deepgram-sdk"
echo ""

pip install pydub edge-tts || warn "Some TTS base packages failed."

if [ "$OS_TYPE" = "Linux" ]; then
    pip install pygame 2>/dev/null || \
        { command -v apt-get &>/dev/null && sudo apt-get install -y mpg123 2>/dev/null || true; }
else
    pip install pygame 2>/dev/null || true
fi
info "TTS base done."

# ── Step 5: Download models ────────────────────────────────
step "[5/5] Downloading models..."
echo ""

# Faster Whisper small
echo "       Downloading Faster Whisper small (~460 MB)..."

python3 - <<'PYEOF'
from faster_whisper import WhisperModel

device  = "cpu"
compute = "int8"
print(f"  Loading on {device} / {compute}...")
model = WhisperModel("small", device=device, compute_type=compute)
print("  Whisper small ready.")
PYEOF
info "Whisper small downloaded."

echo ""

# MiniCPM-V 4.6 via Ollama
if ! command -v ollama &>/dev/null; then
    warn "Ollama not found on PATH."
    echo "       Install Ollama from https://ollama.com/download then run:"
    echo "         ollama pull openbmb/minicpm-v4.6"
    echo "       MiniCPM-V 4.6 (vision) will not be available until then."
else
    read -rp "  Pull MiniCPM-V 4.6 now via Ollama? (~1.6 GB) [Y/n]: " DL_MINICPM
    DL_MINICPM="${DL_MINICPM:-Y}"
    if [[ "$DL_MINICPM" =~ ^[Yy]$ ]]; then
        echo "       Pulling openbmb/minicpm-v4.6..."
        echo "       Handles all vision: faces, emotions, scene understanding, OCR."
        if ollama pull openbmb/minicpm-v4.6; then
            info "MiniCPM-V 4.6 ready."
        else
            warn "MiniCPM-V 4.6 pull failed — run manually: ollama pull openbmb/minicpm-v4.6"
        fi
    else
        echo "       Skipped — run 'ollama pull openbmb/minicpm-v4.6' when ready."
    fi
fi

info "Models done."

# ── Summary ───────────────────────────────────────────────
echo ""
echo " ===================================================="
echo "  All done! Serenity senses installed:"
echo ""
echo "  EYES:"
echo "    - opencv-python   camera frame grab"
echo "    - mss             screen capture (~5ms per frame)"
echo "    - MiniCPM-V 4.6   all vision analysis via Ollama"
echo "                      (faces, emotions, scene, OCR - 1.6B params)"
echo ""
echo "  VISUAL MEMORY:"
echo "    - mss             screen capture"
echo "    - imagehash       perceptual change detection"
echo "    - MiniCPM-V 4.6   captions only when screen changes"
echo "    - SQLite          episodic store (~/.serenity/visual_memory.db)"
echo ""
echo "  EARS:"
echo "    - Whisper small   speech-to-text (CPU/int8)"
echo ""
echo "  VOICE:"
echo "    - Qwen3 TTS (local GPU) + ElevenLabs + edge-tts"
echo ""
echo "  Usage — tell Serenity:"
echo "    'open your eyes'           starts camera"
echo "    'close your eyes'          stops camera, frees RAM"
echo "    'what do you see'          MiniCPM-V snapshot (camera)"
echo "    'look at my screen'        MiniCPM-V snapshot (screen)"
echo "    'what did you see earlier' recall from visual memory"
echo ""
echo "  Config: ~/.serenity/config.json"
echo " ===================================================="
echo ""
