"""Local web UI for ffvii-realtime.

`ffvii-realtime gui` starts a small server on localhost and opens it in the
browser. The video never leaves your machine: you point the page at a local file
path and everything runs locally. Stdlib only, no Flask.
"""
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .detect import detect, LEAD
from .render import render
from .ffmpeg_util import probe, Cancelled

try:
    from . import __version__ as VERSION
except Exception:
    VERSION = "?"

STATE = {"running": False, "stage": "idle", "message": "", "pct": 0,
         "done": False, "output": None, "error": None, "log": []}
_LOCK = threading.Lock()
_CANCEL = threading.Event()   # set by /api/cancel to interrupt the running job


def _set(**kw):
    with _LOCK:
        STATE.update(kw)


def _log(line):
    with _LOCK:
        STATE["log"].append(line)
        del STATE["log"][:-300]   # keep the last 300 lines


def _secs(s):
    """Parse 'MM:SS', 'HH:MM:SS', or seconds -> float; '' / None -> None."""
    s = str(s or "").strip()
    if not s:
        return None
    if ":" in s:
        sec = 0.0
        for p in s.split(":"):
            sec = sec * 60 + float(p)
        return sec
    return float(s)


def _job(path, factor, tac_vol, out, start=0.0, duration=None, lead=LEAD, bridge_sound=True,
         game="rebirth"):
    _CANCEL.clear()
    try:
        _set(running=True, done=False, error=None, output=None, stage="detect",
             message="Scanning for Tactical Mode segments...", pct=2, log=[])
        _log(f"Detecting Tactical Mode segments ({game}) in {os.path.basename(path)} ...")
        info = probe(path)
        span = duration if duration else (info["duration"] - start)
        total_frames = max(1, int(span * info["fps"]))

        seen = {"stage": None}

        def dprog(stage, n):
            _set(stage="detect", message=f"Scanning ({stage}) {n:,} frames...",
                 pct=2 + int(28 * min(1.0, n / total_frames)))
            if stage != seen["stage"]:
                seen["stage"] = stage
                _log(f"Scanning ({stage}) ...")

        res = detect(path, game=game, start=start, duration=duration, lead=lead,
                     progress=dprog, cancel=_CANCEL)

        # nothing detected means nothing to speed up, so skip the render (it would just
        # copy the input) and stop here with a message.
        if res["n_segments"] == 0:
            msg = ("No Tactical Mode segments found, so no file was written. Make sure "
                   "the Game above matches your footage (the wrong game finds 0 segments).")
            _set(running=False, done=True, stage="empty", pct=100, output=None, message=msg)
            _log(msg)
            return

        _set(stage="render", pct=32,
             message=f"Found {res['n_segments']} slow-mo segments. Rendering...")
        _log(f"Found {res['n_segments']} slow-mo segments, "
             f"{res['tactical_seconds']:.0f}s tactical.")

        def rprog(i, t, status):
            _set(stage="render", pct=32 + int(66 * i / max(1, t)),
                 message=f"Rendering chunk {i}/{t}...")
            _log("Bridging audio across sped-up seams..." if status == "bridging audio"
                 else f"Rendering chunk {i}/{t} ...")

        window = None
        if start or duration:
            window = (start, start + duration if duration else info["duration"])
        _log(f"Rendering -> {out} (factor {factor}x) ...")
        render(path, res["intervals"], out, factor=factor, tac_vol=tac_vol,
                       window=window, progress=rprog, bridge_sound=bridge_sound, cancel=_CANCEL)
        _set(running=False, done=True, stage="done", pct=100, output=out,
             message=f"Done! {res['n_segments']} segments sped up.")
        _log(f"Done -> {out}")
    except Cancelled:
        # remove any half-written output so a cancel never leaves a corrupt file behind
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
        _set(running=False, done=False, error=None, stage="cancelled", pct=0,
             message="Cancelled.")
        _log("Cancelled.")
    except Exception as e:
        _set(running=False, done=False, error=str(e), stage="error",
             message=f"Error: {e}")
        _log(f"Error: {e}")


def _native_pick():
    """Open the OS's native file-open dialog and return the chosen absolute path (None
    if cancelled). The server runs on the user's own machine, so the Browse... button
    can pick a local file without uploading anything."""
    import sys
    import shutil
    import subprocess
    try:
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose file with prompt "Select a video")'],
                capture_output=True, text=True)
            return {"path": r.stdout.strip() or None, "error": None}  # rc!=0 -> cancelled
        if sys.platform.startswith("linux"):
            if not shutil.which("zenity"):
                return {"path": None, "error": "No native file picker found (install zenity); paste the path instead."}
            r = subprocess.run(["zenity", "--file-selection", "--title", "Select a video"],
                               capture_output=True, text=True)
            return {"path": r.stdout.strip() or None, "error": None}
        if sys.platform.startswith("win"):
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$f=New-Object System.Windows.Forms.OpenFileDialog;"
                  "if($f.ShowDialog() -eq 'OK'){[Console]::Out.Write($f.FileName)}")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                               capture_output=True, text=True)
            return {"path": r.stdout.strip() or None, "error": None}
    except FileNotFoundError:
        return {"path": None, "error": "No native file picker found on this system; paste the path instead."}
    except Exception as e:
        return {"path": None, "error": "File picker failed: %s" % e}
    return {"path": None, "error": "File picker not supported on this platform; paste the path."}


# the GUI page lives in gui.html (shipped as package data); __FFVII_VERSION__
# is substituted per request in do_GET.
_HTML = os.path.join(os.path.dirname(__file__), "gui.html")
with open(_HTML, encoding="utf-8") as _f:
    PAGE = _f.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")  # always serve a fresh page/state
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.replace("__FFVII_VERSION__", VERSION), "text/html; charset=utf-8")
        elif u.path == "/api/status":
            with _LOCK:
                self._send(200, json.dumps(STATE))
        elif u.path == "/api/pick":
            self._send(200, json.dumps(_native_pick()))
        elif u.path == "/api/open":
            p = parse_qs(u.query).get("path", [""])[0]
            if p and os.path.exists(p):
                import sys as _sys
                import subprocess
                if _sys.platform == "darwin":
                    cmd = ["open", "-R", p]              # reveal in Finder
                elif _sys.platform.startswith("win"):
                    cmd = ["explorer", "/select,", p]
                else:
                    cmd = ["xdg-open", os.path.dirname(p)]
                subprocess.run(cmd)                       # list args, so no shell quoting issues
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if urlparse(self.path).path == "/api/cancel":
            with _LOCK:
                running = STATE["running"]
            if running:
                _CANCEL.set()
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(409, json.dumps({"error": "not running"}))
            return
        if urlparse(self.path).path == "/api/run":
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or "{}")
            path = req.get("path", "")
            if not path or not os.path.exists(path):
                self._send(400, json.dumps({"error": "file not found"})); return
            with _LOCK:
                if STATE["running"]:
                    self._send(409, json.dumps({"error": "already running"})); return
            out = req.get("out") or os.path.splitext(path)[0] + ".realtime.mp4"
            if os.path.exists(out) and not req.get("overwrite"):
                self._send(409, json.dumps({"error": "output exists", "out": out})); return
            try:
                start = _secs(req.get("start")) or 0.0
                end = _secs(req.get("end"))
            except ValueError:
                self._send(400, json.dumps({"error": "bad start/end time"})); return
            duration = (end - start) if end is not None else None
            if duration is not None and duration <= 0:
                self._send(400, json.dumps({"error": "end must be after start"})); return
            game = req.get("game", "rebirth")
            if game not in ("rebirth", "remake", "revelation"):
                self._send(400, json.dumps({"error": "unknown game %r" % game})); return
            t = threading.Thread(target=_job, args=(path, float(req.get("factor", 100)),
                                                     float(req.get("tac_vol", 0.1)), out, start, duration,
                                                     float(req.get("lead", LEAD))),
                                 kwargs={"bridge_sound": bool(req.get("bridge_sound", True)),
                                         "game": game},
                                 daemon=True)
            t.start()
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def serve(port=8765, open_browser=True):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"FFVII Realtime UI running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
