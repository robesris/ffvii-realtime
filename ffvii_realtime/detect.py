"""Detect Tactical Mode (slow-motion) segments in an FFVII Rebirth capture.

Two passes over the video:
  1. Badge pass  - normalize each frame to 1920x1080, crop the badge band, score L2/R2.
  2. Motion pass - downscaled gray frames, mean abs diff between consecutive frames.

A frame is Tactical if the badge match is strong, OR the menu (L2) is present and
the scene is nearly frozen (the slow-motion is underway but the badge hasn't fully
slid in / is washed out by a busy background).

Segments are merged across brief gaps, short blips dropped, and each segment's
START is extended by `lead` to cover the panel's slide-in animation.
"""
import subprocess
import numpy as np

from . import badges
from .ffmpeg_util import ffmpeg, probe, Cancelled

# defaults
THRESH = 0.48        # strong badge match (max of color/white/black)
L2_FROZEN = 0.55     # L2 present, for the frozen clause
MOTION = 1.5         # "frozen" if mean frame-diff below this
SLOW_CAP = 6.0       # Tactical is slow-mo: reject high-motion frames even if the
                     # badge matches (guards against fluke matches during fast action)
NR2 = 0.50           # veto: reject frames where R2 matches the NORMAL-menu position
                     # (the normal "Issue Commands to Allies" menu, not Tactical)
MERGE_GAP = 0.5      # merge segments separated by less than this (s)
MIN_DUR = 0.2        # drop segments shorter than this (s)
LEAD = 0.2           # extend each segment start earlier (panel slide-in)
MOT_W, MOT_H = 384, 216


def _intervals(flags, fps, merge_gap, min_dur, lead, t0=0.0):
    runs, i, n = [], 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            runs.append([i, j - 1]); i = j
        else:
            i += 1
    gap = merge_gap * fps
    merged = []
    for r in runs:
        if merged and (r[0] - merged[-1][1] - 1) <= gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    out, mf = [], min_dur * fps
    for a, b in merged:
        if (b - a + 1) >= mf:
            out.append([t0 + a / fps, t0 + (b + 1) / fps])
    if lead > 0:
        for i, iv in enumerate(out):
            prev_end = out[i - 1][1] if i > 0 else t0
            iv[0] = max(iv[0] - lead, prev_end)
    return [(round(a, 3), round(b, 3)) for a, b in out]


def _bridge_frozen_gaps(ivs, ms, fps, start, max_gap, motion_thr):
    """Fill near-frozen gaps between consecutive detected segments. A slow-motion
    gap bracketed by Tactical on both sides is almost certainly the same menu with
    the badges momentarily unreadable (e.g. a white-flash whiteout); real-time
    action is never frozen for that long."""
    if not ivs or max_gap <= 0:
        return ivs
    ms = np.asarray(ms, np.float32)
    out = [list(ivs[0])]
    for a, b in ivs[1:]:
        ga, gb = out[-1][1], a
        i0 = max(0, int((ga - start) * fps))
        i1 = min(len(ms), int((gb - start) * fps))
        frozen = i1 > i0 and float(ms[i0:i1].mean()) < motion_thr
        if 0 < (gb - ga) <= max_gap and frozen:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def detect(video, game="rebirth", thresh=None, l2_frozen=None, motion=MOTION,
           slow_cap=SLOW_CAP, nr2=NR2, merge_gap=MERGE_GAP, min_dur=MIN_DUR, lead=LEAD,
           start=0.0, duration=None, progress=None, cancel=None):
    """Scan `video`, return dict with intervals and metadata. `game` selects the
    HUD profile ('rebirth', 'remake', or 'revelation'). thresh/l2_frozen fall back
    to the profile's recommended values when None. start/duration limit the scan to
    a section of the video (seconds); returned interval times are still absolute.
    cancel: optional threading.Event; if set mid-scan, the decode is stopped and
    `Cancelled` is raised (the GUI's Cancel button)."""
    profile = badges.get_profile(game)
    if thresh is None:
        thresh = profile.thresh
    if l2_frozen is None:
        l2_frozen = profile.l2_frozen
    info = probe(video)
    fps = info["fps"]
    aspect = info["width"] / info["height"]
    warning = None
    if abs(aspect - 16 / 9) > 0.05:
        warning = (f"input is {info['width']}x{info['height']} (aspect {aspect:.2f}); "
                   "detection is tuned for 16:9 and may be unreliable on other aspect ratios.")

    def pipe(vf, pix):
        # -ss/-t before -i: fast seek that is still frame-accurate in modern ffmpeg,
        # so frame index i maps to absolute time start + i/fps.
        seek = []
        if start:
            seek += ["-ss", f"{start:.3f}"]
        if duration is not None:
            seek += ["-t", f"{duration:.3f}"]
        return subprocess.Popen(
            [ffmpeg(), "-v", "error", *seek, "-i", video, "-vf", vf,
             "-f", "rawvideo", "-pix_fmt", pix, "-"],
            stdout=subprocess.PIPE, bufsize=10 ** 8)

    # pass 1: badges (normalize to 1080p, crop the band)
    bx, by, bw, bh = profile.BAND
    p1 = pipe(f"scale={badges.REF_W}:{badges.REF_H},crop={bw}:{bh}:{bx}:{by}", "bgr24")
    fb = bw * bh * 3
    l2s, r2s, nr2s, idx = [], [], [], 0
    while True:
        buf = p1.stdout.read(fb)
        if len(buf) < fb:
            break
        if cancel is not None and cancel.is_set():
            p1.terminate(); raise Cancelled()
        band = np.frombuffer(buf, np.uint8).reshape(bh, bw, 3)
        l2s.append(profile.score_l2(band))
        r2s.append(profile.score_r2(band))
        nr2s.append(profile.score_nr2(band))  # normal-menu R2 (veto; 0 if profile has none)
        idx += 1
        if progress and idx % 10000 == 0:
            progress("badges", idx)
    p1.wait()

    # pass 1b: "Tactical Mode" header text (optional; rescues solo boss fights that
    # have no party and so never show the L2/R2 allies prompt). Decoded as its own
    # small top-left band, scored per frame, OR'd into the flags below.
    tacs = [0.0] * idx
    if profile.TAC_BAND is not None:
        tx, ty, tw, th = profile.TAC_BAND
        pt = pipe(f"scale={badges.REF_W}:{badges.REF_H},crop={tw}:{th}:{tx}:{ty}", "bgr24")
        ftb = tw * th * 3
        k = 0
        while k < idx:
            buf = pt.stdout.read(ftb)
            if len(buf) < ftb:
                break
            if cancel is not None and cancel.is_set():
                pt.terminate(); raise Cancelled()
            tband = np.frombuffer(buf, np.uint8).reshape(th, tw, 3)
            tacs[k] = profile.score_tac(tband)
            k += 1
            if progress and k % 10000 == 0:
                progress("tactical-text", k)
        pt.wait()

    # pass 2: motion
    p2 = pipe(f"scale={MOT_W}:{MOT_H}", "gray")
    mb = MOT_W * MOT_H
    motion_v = [0.0] * idx
    prev, j = None, 0
    while True:
        buf = p2.stdout.read(mb)
        if len(buf) < mb:
            break
        if cancel is not None and cancel.is_set():
            p2.terminate(); raise Cancelled()
        fr = np.frombuffer(buf, np.uint8).reshape(MOT_H, MOT_W).astype(np.int16)
        if prev is not None and j - 1 < idx:
            motion_v[j - 1] = float(np.abs(fr - prev).mean())
        prev = fr; j += 1
        if progress and j % 10000 == 0:
            progress("motion", j)
    p2.wait()
    if idx >= 2:
        motion_v[idx - 1] = motion_v[idx - 2]

    # Smooth motion with a windowed mean. Real slow-mo is SUSTAINED low motion; fast
    # action that judders (near-duplicate frame pairs, e.g. a camera swing during a
    # character switch) alternates low/high and averages high. Judging "slow" per-frame
    # let the low-diff duplicate frames trip the frozen clause -> false positives.
    if idx:
        k = 9
        ms = np.convolve(np.asarray(motion_v, np.float32), np.ones(k) / k, mode="same").tolist()
    else:
        ms = motion_v

    flags = []
    for i in range(idx):
        strong = r2s[i] > thresh and l2s[i] > thresh and ms[i] < slow_cap
        # both badges match strongly -> Tactical regardless of motion (rescues
        # summon/flash frames where bright effects inflate the motion proxy)
        confident = l2s[i] > profile.conf_l2 and r2s[i] > profile.conf_r2
        if profile.frozen_mode == "both":
            frozen = l2s[i] > l2_frozen and r2s[i] > l2_frozen and ms[i] < motion
        elif profile.frozen_mode == "off":
            frozen = False
        else:  # 'l2': menu up (L present) and scene frozen
            frozen = l2s[i] > l2_frozen and ms[i] < motion
        # "Tactical Mode" header text present and the scene is slow-mo. A very strong
        # text match bypasses the motion gate (like `confident` for badges), to ride
        # through bright effects that would inflate the motion proxy.
        tac = tacs[i] > profile.tac_thr and (ms[i] < slow_cap or tacs[i] > profile.tac_conf)
        normal_menu = nr2s[i] > nr2   # R2 at the normal-menu position -> not Tactical
        flags.append((strong or confident or frozen or tac) and not normal_menu)

    ivs = _intervals(flags, fps, merge_gap, min_dur, lead, t0=start)
    if profile.bridge_gap > 0:
        ivs = _bridge_frozen_gaps(ivs, ms, fps, start, profile.bridge_gap, profile.bridge_motion)
    total = sum(b - a for a, b in ivs)
    return {
        "video": video, "game": game, "fps": fps,
        "width": info["width"], "height": info["height"],
        "duration": round(info["duration"], 3), "frames": idx,
        "params": {"thresh": thresh, "l2_frozen": l2_frozen, "motion": motion,
                   "slow_cap": slow_cap, "nr2": nr2, "merge_gap": merge_gap,
                   "min_dur": min_dur, "lead": lead},
        "n_segments": len(ivs),
        "tactical_seconds": round(total, 2),
        "warning": warning,
        "intervals": [{"start": a, "end": b, "dur": round(b - a, 3)} for a, b in ivs],
    }
