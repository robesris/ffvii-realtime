#!/bin/bash
# Double-click this file to set up and launch FFVII Realtime (no typing needed).
# It creates a private Python environment, installs the tool, fetches FFmpeg if
# you don't already have it, and opens the app in your browser.
cd "$(dirname "$0")" || exit 1
DIR="$HOME/.ffvii-realtime"
BIN="$DIR/bin"
mkdir -p "$BIN"

echo "=== FFVII Realtime setup ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/"
  echo "(double-click the downloaded installer), then run this again."
  read -p "Press Enter to close."
  exit 1
fi

if [ ! -d "$DIR/venv" ]; then
  echo "Creating Python environment (one-time)..."
  python3 -m venv "$DIR/venv"
fi
# shellcheck disable=SC1091
source "$DIR/venv/bin/activate"
python -m pip install -q --upgrade pip
echo "Installing/updating the tool..."
python -m pip install -q -e .

# FFmpeg: use system install if present, else fetch a static build into ~/.ffvii-realtime/bin
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -f "$BIN/ffmpeg" ]; then
  echo "Downloading FFmpeg (one-time, ~30 MB)..."
  curl -fL --retry 3 -o "$DIR/ffmpeg.zip" "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" \
    && unzip -oq "$DIR/ffmpeg.zip" -d "$BIN"
  curl -fL --retry 3 -o "$DIR/ffprobe.zip" "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip" \
    && unzip -oq "$DIR/ffprobe.zip" -d "$BIN"
  chmod +x "$BIN/ffmpeg" "$BIN/ffprobe" 2>/dev/null
  rm -f "$DIR/ffmpeg.zip" "$DIR/ffprobe.zip"
fi

echo "Starting FFVII Realtime — your browser will open shortly."
echo "(Keep this window open while you use the app. Close it when you're done.)"
ffvii-realtime gui
