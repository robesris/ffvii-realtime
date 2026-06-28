"""Locate ffmpeg/ffprobe and probe video metadata.

ffmpeg is resolved in this order:
  1. $FFVII_FFMPEG env var (explicit override)
  2. a binary bundled under ~/.ffvii-realtime/bin (fetched by the double-click launcher)
  3. whatever is on PATH
  4. the static build shipped with the `imageio-ffmpeg` wheel (pip-installed, so a
     plain `pip install ffvii-realtime` is self-contained with no external binary)

ffprobe is resolved the same way for items 1-3. `imageio-ffmpeg` ships ffmpeg but
NOT ffprobe, so when ffprobe is unavailable `probe()` falls back to OpenCV (already
a dependency) to read width/height/fps/duration.
"""
import os
import re
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
    return None


def _imageio_ffmpeg():
    """Path to the static ffmpeg bundled in the imageio-ffmpeg wheel, or None."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg():
    exe = _resolve("ffmpeg", "FFVII_FFMPEG")
    if exe:
        return exe
    exe = _imageio_ffmpeg()
    if exe:
        return exe
    raise FileNotFoundError(
        "Could not find 'ffmpeg'. Install FFmpeg (https://ffmpeg.org/download.html), "
        "or set $FFVII_FFMPEG to its path. (A normal `pip install ffvii-realtime` "
        "bundles ffmpeg via imageio-ffmpeg; this error means that dependency is missing.)"
    )


def ffprobe():
    exe = _resolve("ffprobe", "FFVII_FFPROBE")
    if exe:
        return exe
    raise FileNotFoundError(
        "Could not find 'ffprobe'. Install FFmpeg (https://ffmpeg.org/download.html), "
        "or set $FFVII_FFPROBE to its path."
    )


def _probe_ffprobe(video):
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


def _duration_via_ffmpeg(video):
    """Read the container duration from the ffmpeg binary's stderr banner. More
    reliable than OpenCV's frame-count estimate. Returns seconds, or None."""
    exe = _resolve("ffmpeg", "FFVII_FFMPEG") or _imageio_ffmpeg()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-i", video], capture_output=True, text=True)
    except Exception:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        return None
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def _probe_opencv(video):
    """Fallback when ffprobe is unavailable (e.g. a pip-only install where ffmpeg
    comes from imageio-ffmpeg, which has no ffprobe). OpenCV is already required.
    Dimensions/fps come from OpenCV; duration prefers the ffmpeg banner (OpenCV's
    frame-count estimate is unreliable on some containers)."""
    import cv2
    cap = cv2.VideoCapture(video)
    try:
        if not cap.isOpened():
            raise RuntimeError(f"could not open video: {video}")
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
        frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0.0
    finally:
        cap.release()
    if not (w and h and fps):
        raise RuntimeError(
            f"could not read video metadata from {video}; install FFmpeg/ffprobe for "
            "more reliable probing.")
    dur = _duration_via_ffmpeg(video)
    if dur is None:
        dur = (frames / fps) if fps else 0.0
    return {"width": w, "height": h, "fps": fps, "duration": dur}


def probe(video):
    """Return dict with width, height, fps, duration for the first video stream.
    Prefers ffprobe (most accurate); falls back to OpenCV when ffprobe is absent."""
    try:
        return _probe_ffprobe(video)
    except FileNotFoundError:
        return _probe_opencv(video)
