@echo off
:: ============================================================
::  Serenity Senses Installer — Windows
::  Installs Eyes, Ears, Voice (TTS), and Visual Memory.
::
::  Vision stack:
::    opencv-python  — camera frame grab only (VideoCapture)
::    mss            — fast screen capture (~5ms/frame)
::    Pillow         — image handling
::    imagehash      — perceptual change detection
::    MiniCPM-V 4.6  — all scene/face/emotion understanding via Ollama
::
::  Visual Memory stack:
::    SQLite (stdlib)  — episodic memory, what Serenity saw
::    imagehash        — perceptual hashing for change detection
::    mss              — fast screen capture
::
::  Audio stack:
::    Whisper small (faster-whisper), sounddevice
::
::  Voice (TTS):
::    Qwen3 TTS (local GPU), ElevenLabs, edge-tts
::
::  RTX / CUDA GPU recommended for audio (Whisper).
::  Vision runs entirely via Ollama — no extra VRAM beyond minicpm.
::
::  Run from anywhere — just double-click or call from CMD.
:: ============================================================
setlocal enabledelayedexpansion

echo.
echo  ====================================================
echo   Serenity Senses Installer
echo   Eyes ^| Ears (Whisper small) ^| Voice (TTS)
echo  ====================================================
echo.

:: ── Sanity check ──────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install Python 3.10+ and try again.
    echo          https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  Python: %PY_VER%
echo.

:: ── Detect CUDA GPU ───────────────────────────────────────
set CUDA_AVAILABLE=0
python -c "import torch; exit(0 if torch.cuda.is_available() else 1)" >nul 2>&1
if not errorlevel 1 (
    set CUDA_AVAILABLE=1
    for /f "tokens=*" %%g in ('python -c "import torch; print(torch.cuda.get_device_name(0))"') do set GPU_NAME=%%g
    echo  GPU detected: !GPU_NAME!
    echo  CUDA:         available  (used for Whisper / TTS only)
) else (
    echo  GPU: No CUDA GPU found - CPU mode for all components.
    echo  NOTE: Whisper small will be slower on CPU.
)
echo.

:: ── Step 1: PyTorch (CUDA or CPU) ─────────────────────────
echo  [1/5] Installing PyTorch...
if "!CUDA_AVAILABLE!"=="0" (
    echo        Installing CUDA 12.4 build...
    echo        (If you have no NVIDIA GPU this still works via CPU fallback)
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
) else (
    echo        PyTorch with CUDA already present - skipping reinstall.
)
if errorlevel 1 (
    echo  [ERROR] PyTorch install failed.
    pause & exit /b 1
)
echo  Done.
echo.

:: ── Step 2: Vision (Eyes) ─────────────────────────────────
echo  [2/5] Installing Vision stack (Eyes)...
echo        opencv-python, mss, Pillow, imagehash
echo        MiniCPM-V 4.6 handles all analysis via Ollama - no extra packages.
echo.
pip install opencv-python mss Pillow imagehash
if errorlevel 1 (
    echo  [WARN] Some vision packages failed - check output above.
)
echo  Done.
echo.

:: ── Step 3: Audio (Ears) ──────────────────────────────────
echo  [3/5] Installing Audio stack (Ears)...
echo        sounddevice, scipy, faster-whisper, soundfile, transformers...
pip install ^
    sounddevice ^
    scipy ^
    faster-whisper ^
    soundfile ^
    transformers ^
    accelerate
if errorlevel 1 (
    echo  [WARN] Some audio packages failed - check output above.
)
echo  Done.
echo.

:: ── Step 4: Voice / TTS base packages ─────────────────────
echo  [4/5] Installing TTS base packages...
echo        edge-tts (free, no key), pydub (audio conversion), pygame (playback)
echo.
echo        NOTE: Provider-specific packages are installed separately.
echo        The setup wizard will show the exact pip command after you pick
echo        your TTS engine.  Examples:
echo.
echo          Qwen3 local    pip install -U qwen-tts
echo          Kokoro         pip install kokoro soundfile
echo          Coqui XTTS-v2  pip install TTS
echo          Bark           pip install suno-bark
echo          ElevenLabs     pip install httpx  (built-in, no extra package)
echo          Google TTS     pip install google-cloud-texttospeech
echo          Amazon Polly   pip install boto3
echo          Cartesia        pip install cartesia
echo          PlayHT         pip install pyht
echo          Deepgram       pip install deepgram-sdk
echo.
pip install pydub edge-tts pygame
if errorlevel 1 (
    echo  [WARN] Some TTS base packages failed - check output above.
)
echo  Done.
echo.

:: ── Step 5: Download models ────────────────────────────────
echo  [5/5] Downloading models...
echo.

:: Faster Whisper small
echo        Downloading Faster Whisper small (~460 MB)...
(
echo from faster_whisper import WhisperModel
echo model = WhisperModel^('small', device='cpu', compute_type='int8'^)
echo print^('  Whisper small ready.'^)
) > "%TEMP%\serenity_whisper.py"
python "%TEMP%\serenity_whisper.py"
del "%TEMP%\serenity_whisper.py" >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Whisper small download failed. Check your internet connection.
    pause & exit /b 1
)
echo.

:: MiniCPM-V 4.6 via Ollama
echo        Checking for Ollama...
where ollama >nul 2>&1
if errorlevel 1 (
    echo  [WARN] Ollama not found on PATH.
    echo         Install Ollama from https://ollama.com/download then run:
    echo           ollama pull openbmb/minicpm-v4.6
    echo         MiniCPM-V 4.6 will not be available until Ollama is installed.
    goto :skip_minicpm
)

set /p DL_MINICPM="  Pull MiniCPM-V 4.6 now via Ollama? (~1.6 GB) [Y/n]: "
if /i "!DL_MINICPM!"=="n" goto :skip_minicpm
if /i "!DL_MINICPM!"=="N" goto :skip_minicpm

echo        Pulling MiniCPM-V 4.6 (openbmb/minicpm-v4.6)...
echo        Handles all vision: faces, emotions, scene understanding, OCR.
ollama pull openbmb/minicpm-v4.6
if errorlevel 1 (
    echo  [WARN] MiniCPM-V 4.6 pull failed.
    echo         Run manually: ollama pull openbmb/minicpm-v4.6
) else (
    echo  MiniCPM-V 4.6 ready.
)

:skip_minicpm
echo  Done.
echo.

:: ── Summary ───────────────────────────────────────────────
echo  ====================================================
echo   All done! Serenity senses installed:
echo.
echo   EYES:
echo     - opencv-python   camera frame grab
echo     - mss             screen capture (~5ms per frame)
echo     - MiniCPM-V 4.6   all vision analysis via Ollama
echo                       (faces, emotions, scene, OCR - 1.6B params)
echo.
echo   VISUAL MEMORY:
echo     - mss             screen capture
echo     - imagehash       perceptual change detection
echo     - MiniCPM-V 4.6   captions only when screen changes
echo     - SQLite          episodic store (~/.serenity/visual_memory.db)
echo.
echo   EARS:
echo     - Whisper small   speech-to-text (CPU/int8)
echo.
echo   VOICE:
echo     - Qwen3 TTS (local GPU) + ElevenLabs + edge-tts
echo.
echo   Usage — tell Serenity:
echo     "open your eyes"     starts camera
echo     "close your eyes"    stops camera, frees RAM
echo     "what do you see"    MiniCPM-V snapshot (camera)
echo     "look at my screen"  MiniCPM-V snapshot (screen)
echo     "what did you see earlier" recall from visual memory
echo.
echo   Config: %%USERPROFILE%%\.serenity\config.json
echo  ====================================================
echo.
pause
