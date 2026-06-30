"""Render the sped-up video from detected intervals.

Tactical segments are sped up by `factor` (video setpts + audio atempo, exact
per-segment so audio/video stay locked); normal segments pass through at 1x.

Done in chunks (~chunk_secs of source each): one giant filtergraph over all
segments runs at a tiny fraction of real-time because the split filter fans every
decoded frame out to every segment branch. Per-chunk filtergraphs keep that fan-out
small. Each chunk is encoded with identical settings and forced to an exact
duration (audio apad + output -t) so no a/v drift accumulates, then the chunks are
joined with the concat demuxer (-c copy, no re-encode).

Works at any resolution (retiming is resolution-independent).
"""
import os
import json
import shutil
import tempfile

from .ffmpeg_util import ffmpeg, probe, run_cancellable, Cancelled


def atempo_chain(factor):
    """Decompose a tempo factor into atempo filters each in [0.5, 2.0]."""
    parts, x = [], float(factor)
    while x > 2.0 + 1e-9:
        parts.append(2.0); x /= 2.0
    parts.append(round(x, 6))
    return ",".join(f"atempo={p}" for p in parts)


def build_segments(intervals, lo, hi):
    """Ordered [(start, end, tactical?)] covering [lo, hi]."""
    segs, cur = [], lo
    for iv in intervals:
        a, b = max(iv["start"], lo), min(iv["end"], hi)
        if a >= b:
            continue
        if a > cur:
            segs.append((cur, a, False))
        segs.append((a, b, True))
        cur = b
    if cur < hi:
        segs.append((cur, hi, False))
    return [s for s in segs if s[1] - s[0] > 1e-3]


def _video_graph(segs, cs, factor):
    vl, vin = [], []
    for i, (a, b, tac) in enumerate(segs):
        a, b = a - cs, b - cs
        pts = f"(PTS-STARTPTS)/{factor}" if tac else "PTS-STARTPTS"
        vl.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts={pts}[v{i}];")
        vin.append(f"[v{i}]")
    return "\n".join(vl + ["".join(vin) + f"concat=n={len(segs)}:v=1:a=0[v]"])


def _audio_graph(segs, cs, factor, atempo, tac_vol):
    al, ain = [], []
    for i, (a, b, tac) in enumerate(segs):
        a, b = a - cs, b - cs
        if tac:
            vol = f",volume={tac_vol}" if abs(tac_vol - 1.0) > 1e-9 else ""
            al.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,{atempo}{vol}[a{i}];")
        else:
            al.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}];")
        ain.append(f"[a{i}]")
    # apad + the caller's output -t pins audio to exactly the chunk's video length, so
    # no a/v drift accumulates across the concatenated chunks.
    return "\n".join(al + ["".join(ain) + f"concat=n={len(segs)}:v=0:a=1[ac];[ac]apad[a]"])


def render(video, intervals, out, factor=100.0, tac_vol=0.1, crf=18, preset="slow",
           chunk_secs=180.0, work_dir=None, keep_work=False, window=None, progress=None,
           bridge_sound=True, bridge_width=0.35, cancel=None):
    """Render `video` -> `out` using `intervals` (list of {start,end}).

    window=(lo, hi) renders only that source span (used for previews); None = whole video.
    bridge_sound: replace the sped-up seam audio with a crossfade so it never cuts out.
    cancel: optional threading.Event; if set mid-render, the current ffmpeg is killed
    and `Cancelled` is raised (the GUI's Cancel button).
    """
    info = probe(video)
    fps = info["fps"]
    dur = info["duration"]
    atempo = atempo_chain(factor)
    lo, hi = window if window else (0.0, dur)
    segs = build_segments(intervals, lo, hi)

    # group whole segments into ~chunk_secs (source) chunks
    chunks, cur, acc = [], [], 0.0
    for s in segs:
        cur.append(s); acc += s[1] - s[0]
        if acc >= chunk_secs:
            chunks.append(cur); cur, acc = [], 0.0
    if cur:
        chunks.append(cur)

    work = work_dir or tempfile.mkdtemp(prefix="ffvii_realtime_")
    os.makedirs(work, exist_ok=True)
    graphpath = os.path.join(work, "graph.txt")
    paths = []
    try:
        for ci, chunk in enumerate(chunks):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            outp = os.path.join(work, f"c{ci:04d}.mp4")
            paths.append(outp)
            if os.path.exists(outp):                 # resume
                if progress: progress(ci + 1, len(chunks), "skip")
                continue
            cs, ce = chunk[0][0], chunk[-1][1]
            target = sum((b - a) if not t else (b - a) / factor for a, b, t in chunk)
            seek = ["-ss", f"{cs:.3f}", "-t", f"{ce - cs + 1.0:.3f}", "-i", video]
            # Render video and audio in SEPARATE passes, then mux. A single graph that
            # produces both [v] and [a] from one input deadlocks when a chunk mixes long
            # 1x segments with heavily sped-up ones: the two output streams advance at
            # wildly different rates and ffmpeg's interleaver stalls forever. Each stream
            # alone is fine, so we build them independently and join with -c copy.
            vtmp, atmp = outp[:-4] + "_v.mp4", outp[:-4] + "_a.m4a"
            with open(graphpath, "w") as f:
                f.write(_video_graph(chunk, cs, factor))
            run_cancellable([ffmpeg(), "-y", "-v", "error", *seek, "-/filter_complex", graphpath,
                             "-map", "[v]", "-an", "-t", f"{target:.3f}", "-r", str(fps),
                             "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
                             "-pix_fmt", "yuv420p", vtmp], cancel=cancel, check=True)
            with open(graphpath, "w") as f:
                f.write(_audio_graph(chunk, cs, factor, atempo, tac_vol))
            run_cancellable([ffmpeg(), "-y", "-v", "error", *seek, "-/filter_complex", graphpath,
                             "-map", "[a]", "-vn", "-t", f"{target:.3f}",
                             "-c:a", "aac", "-b:a", "192k", atmp], cancel=cancel, check=True)
            run_cancellable([ffmpeg(), "-y", "-v", "error", "-i", vtmp, "-i", atmp,
                             "-map", "0:v", "-map", "1:a", "-c", "copy", outp], cancel=cancel, check=True)
            os.remove(vtmp); os.remove(atmp)
            if progress: progress(ci + 1, len(chunks), "done")

        if cancel is not None and cancel.is_set():
            raise Cancelled()
        listpath = os.path.join(work, "list.txt")
        with open(listpath, "w") as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        run_cancellable([ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                         "-i", listpath, "-c", "copy", out], cancel=cancel, check=True)
        if bridge_sound:
            if progress: progress(len(chunks), len(chunks), "bridging audio")
            from .bridge import bridge_audio
            bridge_audio(out, video, intervals, factor, window=window, m_max=bridge_width)
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
    return out
