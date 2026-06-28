"""Locate ffmpeg/ffprobe and probe video metadata.

ffmpeg/ffprobe are resolved in this order:
  1. $FFVII_FFMPEG / $FFVII_FFPROBE env vars (explicit override)
  2. a binary bundled under ~/.ffvii-realtime/bin (auto-downloaded by the launcher)
  3. whatever is on PATH
"""
import os
import shutil
import subprocess

_HOME_BIN = os.path.expanduser("~/.ffvii-realtime/bin")


def _resolve(name, env):
    p = os.environ.get(env)
    if p and os.path.exists(p):
        return p
    local = os.path.join(_HOME_BIN, name)
    if os.path.exists(local):
        return local
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"Could not find '{name}'. Install FFmpeg (https://ffmpeg.org/download.html), "
        f"or set ${env} to its path. The double-click launcher fetches it automatically."
    )


def ffmpeg():
    return _resolve("ffmpeg", "FFVII_FFMPEG")


def ffprobe():
    return _resolve("ffprobe", "FFVII_FFPROBE")


def probe(video):
    """Return dict with width, height, fps, duration for the first video stream."""
    out = subprocess.run(
        [ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:format=duration",
         "-of", "default=noprint_wrappers=1:nokey=0", video],
        capture_output=True, text=True).stdout
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v
    n, d = info["r_frame_rate"].split("/")
    return {
        "width": int(info["width"]),
        "height": int(info["height"]),
        "fps": float(n) / float(d),
        "duration": float(info["duration"]),
    }
