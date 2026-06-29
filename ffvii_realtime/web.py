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
         "done": False, "output": None, "error": None, "log": []}
_LOCK = threading.Lock()


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

        res = detect(path, game=game, start=start, duration=duration, lead=lead, progress=dprog)
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
                       window=window, progress=rprog, bridge_sound=bridge_sound)
        _set(running=False, done=True, stage="done", pct=100, output=out,
             message=f"Done! {res['n_segments']} segments sped up. Saved to {out}")
        _log(f"Done -> {out}")
    except Exception as e:
        _set(running=False, done=False, error=str(e), stage="error",
             message=f"Error: {e}")
        _log(f"Error: {e}")


def _native_pick():
    """Open the OS's native file-open dialog on this machine and return the chosen
    absolute path (None if cancelled). Lets the GUI offer a real Browse... button
    without uploading the file -- the server runs on the user's own computer."""
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


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>FFVII Realtime</title><style>
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:40px auto;padding:0 20px;color:#222}
h1{font-size:22px} .sub{color:#666;margin-top:-8px}
label{display:block;margin:14px 0 4px;font-weight:600}
input:not([type=checkbox]){width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
input[type=checkbox]{width:auto;margin:0 6px 0 0;vertical-align:middle}
label.check{font-weight:600;display:flex;align-items:center}
.row{display:flex;gap:14px}.row>div{flex:1}
button{margin-top:18px;padding:10px 18px;font-size:15px;border:0;border-radius:6px;background:#2b6cb0;color:#fff;cursor:pointer}
button:disabled{background:#9bb}
#bar{height:14px;background:#eee;border-radius:7px;overflow:hidden;margin-top:18px;display:none}
#fill{height:100%;width:0;background:#2b6cb0;transition:width .4s}
#msg{margin-top:10px;color:#444;min-height:20px}
.note{font-size:13px;color:#777;margin-top:6px}
select{width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box;background:#fff}
.drop{display:flex;gap:8px;align-items:stretch}
.drop input{flex:1;width:auto}
.browse{margin-top:0;padding:8px 14px;font-size:14px;background:#555;white-space:nowrap}
.drop.drag{outline:2px dashed #2b6cb0;outline-offset:4px;border-radius:8px;background:#eef5ff}
#log{margin-top:14px;background:#1e1e1e;color:#d4d4d4;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;padding:10px 12px;border-radius:6px;height:170px;overflow:auto;white-space:pre-wrap;display:none}
a.dl{display:inline-block;margin-top:14px}
</style></head><body>
<h1>FFVII Realtime</h1>
<p class="sub">Speeds up Final Fantasy VII Rebirth Tactical Mode slow-motion so the fight plays in real time.</p>
<label>Video file</label>
<div id="drop" class="drop">
  <input id="path" placeholder="/Users/you/Movies/my-fight.mp4">
  <button type="button" id="browse" class="browse" onclick="pick()">Browse&hellip;</button>
</div>
<div class="note">Drag a video onto this box, click <b>Browse&hellip;</b>, or paste its full path. The file stays on your computer &mdash; it isn't uploaded.</div>
<label>Game</label>
<select id="game">
  <option value="remake">Final Fantasy VII Remake</option>
  <option value="rebirth" selected>Final Fantasy VII Rebirth</option>
  <option value="revelation">Final Fantasy VII Revelation</option>
</select>
<div class="note">Which game's HUD to detect &mdash; <b>you must pick the one your footage is from</b>, or detection finds 0 segments. Defaults to Rebirth.</div>
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
<div class="row">
  <div><label class="check"><input id="bridge" type="checkbox" checked> Smooth audio across sped-up sections</label>
    <div class="note">Crossfades the real before/after sound across each sped-up segment so the audio doesn't cut out. Recommended.</div></div>
</div>
<label>Output file (optional)</label>
<input id="out" placeholder="(defaults to <input>.realtime.mp4)">
<button id="go" onclick="run()">Start</button>
<div id="bar"><div id="fill"></div></div>
<div id="msg"></div>
<pre id="log"></pre>
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
      game:document.getElementById('game').value,
      lead:+document.getElementById('lead').value,
      bridge_sound:document.getElementById('bridge').checked,
      start:document.getElementById('start').value.trim(),
      end:document.getElementById('end').value.trim(),
      out:document.getElementById('out').value.trim()})});
  timer=setInterval(poll,1000);
}
const $=id=>document.getElementById(id);
async function pick(){
  $('browse').disabled=true; $('browse').textContent='Choosing…';
  try{
    const j=await (await fetch('/api/pick')).json();
    if(j.path){$('path').value=j.path; $('msg').textContent='';}
    else if(j.error){$('msg').textContent=j.error;}
  }catch(e){$('msg').textContent='Could not open the file picker: '+e;}
  $('browse').disabled=false; $('browse').textContent='Browse…';
}
(function(){
  const drop=$('drop'); if(!drop)return;
  const stop=e=>{e.preventDefault();e.stopPropagation();};
  ['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{stop(e);drop.classList.add('drag');}));
  ['dragleave','dragend'].forEach(ev=>drop.addEventListener(ev,e=>{stop(e);drop.classList.remove('drag');}));
  drop.addEventListener('drop',e=>{
    stop(e); drop.classList.remove('drag');
    const dt=e.dataTransfer;
    let uri=(dt.getData('text/uri-list')||dt.getData('text/plain')||'').split(String.fromCharCode(10))[0].trim();
    let p='';
    if(uri.indexOf('file://')===0){
      p=uri.slice(7);
      if(p.indexOf('localhost')===0)p=p.slice(9);
      try{p=decodeURIComponent(p);}catch(_){}
      if(p.length>2&&p.charAt(0)==='/'&&p.charAt(2)===':')p=p.slice(1);
    }
    if(p){$('path').value=p; $('msg').textContent='';}
    else if(dt.files&&dt.files.length){$('msg').textContent="Your browser hid the file's location — click Browse… or paste the full path.";}
  });
})();
async function poll(){
  const s=await (await fetch('/api/status')).json();
  document.getElementById('fill').style.width=s.pct+'%';
  document.getElementById('msg').textContent=s.message;
  if(s.log&&s.log.length){const L=document.getElementById('log');
    const atBottom=L.scrollHeight-L.scrollTop-L.clientHeight<30;
    L.style.display='block';L.textContent=s.log.join(String.fromCharCode(10));
    if(atBottom)L.scrollTop=L.scrollHeight;}
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
                subprocess.run(cmd)                       # list args -> no shell, no escaping issues
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
