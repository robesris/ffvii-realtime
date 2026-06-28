"""Local web UI for ffvii-realtime.

`ffvii-realtime gui` starts a small server on localhost and opens it in the
browser. The video never leaves your machine — you point the page at a local
file path and the processing runs locally. Stdlib only (no Flask/etc.).
"""
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .detect import detect, LEAD
from .render import render
from .ffmpeg_util import probe

STATE = {"running": False, "stage": "idle", "message": "", "pct": 0,
         "done": False, "output": None, "error": None}
_LOCK = threading.Lock()


def _set(**kw):
    with _LOCK:
        STATE.update(kw)


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


def _job(path, factor, tac_vol, out, start=0.0, duration=None, lead=LEAD):
    try:
        _set(running=True, done=False, error=None, output=None, stage="detect",
             message="Scanning for Tactical Mode segments...", pct=2)
        info = probe(path)
        span = duration if duration else (info["duration"] - start)
        total_frames = max(1, int(span * info["fps"]))

        def dprog(stage, n):
            _set(stage="detect", message=f"Scanning ({stage}) {n:,} frames...",
                 pct=2 + int(28 * min(1.0, n / total_frames)))

        res = detect(path, start=start, duration=duration, lead=lead, progress=dprog)
        _set(stage="render", pct=32,
             message=f"Found {res['n_segments']} slow-mo segments. Rendering...")

        def rprog(i, t, status):
            _set(stage="render", pct=32 + int(66 * i / max(1, t)),
                 message=f"Rendering chunk {i}/{t}...")

        window = None
        if start or duration:
            window = (start, start + duration if duration else info["duration"])
        render(path, res["intervals"], out, factor=factor, tac_vol=tac_vol,
                       window=window, progress=rprog)
        _set(running=False, done=True, stage="done", pct=100, output=out,
             message=f"Done! {res['n_segments']} segments sped up. Saved to {out}")
    except Exception as e:
        _set(running=False, done=False, error=str(e), stage="error",
             message=f"Error: {e}")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>FFVII Realtime</title><style>
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:40px auto;padding:0 20px;color:#222}
h1{font-size:22px} .sub{color:#666;margin-top:-8px}
label{display:block;margin:14px 0 4px;font-weight:600}
input{width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
.row{display:flex;gap:14px}.row>div{flex:1}
button{margin-top:18px;padding:10px 18px;font-size:15px;border:0;border-radius:6px;background:#2b6cb0;color:#fff;cursor:pointer}
button:disabled{background:#9bb}
#bar{height:14px;background:#eee;border-radius:7px;overflow:hidden;margin-top:18px;display:none}
#fill{height:100%;width:0;background:#2b6cb0;transition:width .4s}
#msg{margin-top:10px;color:#444;min-height:20px}
.note{font-size:13px;color:#777;margin-top:6px}
a.dl{display:inline-block;margin-top:14px}
</style></head><body>
<h1>FFVII Realtime</h1>
<p class="sub">Speeds up Final Fantasy VII Rebirth Tactical Mode slow-motion so the fight plays in real time.</p>
<label>Video file (full path on this computer)</label>
<input id="path" placeholder="/Users/you/Movies/my-fight.mp4">
<div class="row">
  <div><label>Speed-up factor</label><input id="factor" type="number" value="100" min="2" step="1">
    <div class="note">100 = default in-game slowdown. Higher if your "Tactical Mode Slowdown" setting is stronger.</div></div>
  <div><label>Tactical audio volume</label><input id="vol" type="text" value="10%">
    <div class="note">Percentage of normal volume for sped-up segments: 0% = silent, 100% = full.</div></div>
  <div><label>Lead-in (seconds)</label><input id="lead" type="number" value="0.2" min="0" step="0.05">
    <div class="note">Start the speed-up this many seconds before the menu is detected, to cover the panel slide-in.</div></div>
</div>
<div class="row">
  <div><label>Start (optional)</label><input id="start" placeholder="0:00">
    <div class="note">Process only from here, e.g. 24:00. Blank = beginning.</div></div>
  <div><label>End (optional)</label><input id="end" placeholder="(end of video)">
    <div class="note">Process only up to here, e.g. 26:30. Blank = end.</div></div>
</div>
<label>Output file (optional)</label>
<input id="out" placeholder="(defaults to <input>.realtime.mp4)">
<button id="go" onclick="run()">Start</button>
<div id="bar"><div id="fill"></div></div>
<div id="msg"></div>
<div id="result"></div>
<script>
let timer=null;
async function run(){
  const path=document.getElementById('path').value.trim();
  if(!path){document.getElementById('msg').textContent='Enter the video file path.';return;}
  let pct=parseFloat(document.getElementById('vol').value.replace('%',''));
  if(isNaN(pct)||pct<0)pct=10;
  document.getElementById('go').disabled=true;
  document.getElementById('bar').style.display='block';
  document.getElementById('result').innerHTML='';
  await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path,factor:+document.getElementById('factor').value,
      tac_vol:pct/100,
      lead:+document.getElementById('lead').value,
      start:document.getElementById('start').value.trim(),
      end:document.getElementById('end').value.trim(),
      out:document.getElementById('out').value.trim()})});
  timer=setInterval(poll,1000);
}
async function poll(){
  const s=await (await fetch('/api/status')).json();
  document.getElementById('fill').style.width=s.pct+'%';
  document.getElementById('msg').textContent=s.message;
  if(s.done||s.error){clearInterval(timer);document.getElementById('go').disabled=false;
    if(s.done)document.getElementById('result').innerHTML='<a class="dl" href="/api/open?path='+encodeURIComponent(s.output)+'">Reveal output file</a>';}
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/status":
            with _LOCK:
                self._send(200, json.dumps(STATE))
        elif u.path == "/api/open":
            p = parse_qs(u.query).get("path", [""])[0]
            if p and os.path.exists(p):
                os.system(f'open -R "{p}"' if os.uname().sysname == "Darwin" else f'xdg-open "{os.path.dirname(p)}"')
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
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
            try:
                start = _secs(req.get("start")) or 0.0
                end = _secs(req.get("end"))
            except ValueError:
                self._send(400, json.dumps({"error": "bad start/end time"})); return
            duration = (end - start) if end is not None else None
            if duration is not None and duration <= 0:
                self._send(400, json.dumps({"error": "end must be after start"})); return
            t = threading.Thread(target=_job, args=(path, float(req.get("factor", 100)),
                                                     float(req.get("tac_vol", 0.1)), out, start, duration,
                                                     float(req.get("lead", LEAD))),
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
