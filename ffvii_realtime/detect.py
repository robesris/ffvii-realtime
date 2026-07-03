"""Find the Tactical Mode (slow-motion) segments in a capture.

Several passes over the video: badge (scale each frame to 1080p, crop the badge band,
score L2/R2), header-text and command-panel passes for solo fights that show no L2/R2
prompt, and a motion pass (mean abs diff between downscaled gray frames). A frame
counts as Tactical if the badges match strongly, the header text or command panel is
up in slow-mo, or the menu is up and the scene is nearly frozen (the badge hasn't slid
in yet or is washed out).

Runs of Tactical frames become intervals: merged across short gaps, tiny blips
dropped, and each start nudged earlier by `lead` to cover the panel slide-in.
"""
import subprocess
import numpy as np

from . import badges
from .ffmpeg_util import ffmpeg, probe, Cancelled

# defaults
THRESH = 0.48        # strong badge match (max of color/white/black)
L2_FROZEN = 0.55     # L2 present, for the frozen clause
MOTION = 1.5         # "frozen" if mean frame-diff below this
SLOW_CAP = 6.0       # reject high-motion frames even if the badge matches; Tactical
                     # is slow, so a match during fast action is a fluke
NR2 = 0.50           # veto: R2 matching the normal-menu position ("Issue Commands to
                     # Allies") means we're not in Tactical Mode
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


def _merge_ranges(ranges):
    """Sort (start, end) ranges and merge overlapping/touching ones."""
    out = []
    for a, b in sorted((a, b) for a, b in ranges if b > a):
        if out and a <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def _subtract_ranges(ivs, cuts):
    """Remove the cut ranges from ivs, splitting an interval when a cut lands inside it."""
    cuts = _merge_ranges(cuts)
    out = []
    for a, b in ivs:
        seg = [(a, b)]
        for ca, cb in cuts:
            nxt = []
            for sa, sb in seg:
                if cb <= sa or ca >= sb:            # no overlap
                    nxt.append((sa, sb))
                    continue
                if sa < ca:                          # keep the part before the cut
                    nxt.append((sa, ca))
                if cb < sb:                          # keep the part after the cut
                    nxt.append((cb, sb))
            seg = nxt
        out.extend(seg)
    # drop sub-frame slivers left when a cut's edge nearly coincides with a segment's
    return [(round(a, 3), round(b, 3)) for a, b in out if b - a > 0.05]


def _bridge_frozen_gaps(ivs, ms, fps, start, max_gap, motion_thr):
    """Fill near-frozen gaps between consecutive segments. A slow-motion gap with
    Tactical on both sides is almost always the same menu with the badges briefly
    unreadable (a white flash); real-time action is never frozen that long."""
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


def _check_cancel(cancel, proc):
    if cancel is not None and cancel.is_set():
        proc.terminate()
        raise Cancelled()


def _scan_badges(pipe, profile, cancel, progress):
    """Pass 1: score L2/R2 and the normal-menu veto on each frame's badge band."""
    bx, by, bw, bh = profile.BAND
    proc = pipe(f"scale={badges.REF_W}:{badges.REF_H},crop={bw}:{bh}:{bx}:{by}", "bgr24")
    fb = bw * bh * 3
    l2s, r2s, nr2s = [], [], []
    while True:
        buf = proc.stdout.read(fb)
        if len(buf) < fb:
            break
        _check_cancel(cancel, proc)
        band = np.frombuffer(buf, np.uint8).reshape(bh, bw, 3)
        l2s.append(profile.score_l2(band))
        r2s.append(profile.score_r2(band))
        nr2s.append(profile.score_nr2(band))
        if progress and len(l2s) % 10000 == 0:
            progress("badges", len(l2s))
    proc.wait()
    return l2s, r2s, nr2s


def _scan_tac_text(pipe, profile, n, cancel, progress):
    """Pass 1b: score the 'Tactical Mode' header text, aligned with the n badge
    frames. All-zeros if the profile has no tac band."""
    tacs = [0.0] * n
    if profile.TAC_BAND is None:
        return tacs
    tx, ty, tw, th = profile.TAC_BAND
    proc = pipe(f"scale={badges.REF_W}:{badges.REF_H},crop={tw}:{th}:{tx}:{ty}", "bgr24")
    ftb = tw * th * 3
    k = 0
    while k < n:
        buf = proc.stdout.read(ftb)
        if len(buf) < ftb:
            break
        _check_cancel(cancel, proc)
        tband = np.frombuffer(buf, np.uint8).reshape(th, tw, 3)
        tacs[k] = profile.score_tac(tband)
        k += 1
        if progress and k % 10000 == 0:
            progress("tactical-text", k)
    proc.wait()
    return tacs


def _scan_menu(pipe, profile, n, cancel, progress):
    """Pass 1c: score the command-menu panel, aligned with the n badge frames.
    All-zeros if the profile has no menu band."""
    menus = [0.0] * n
    if profile.MENU_BAND is None:
        return menus
    mx, my, mw, mh = profile.MENU_BAND
    proc = pipe(f"scale={badges.REF_W}:{badges.REF_H},crop={mw}:{mh}:{mx}:{my}", "bgr24")
    fmb = mw * mh * 3
    k = 0
    while k < n:
        buf = proc.stdout.read(fmb)
        if len(buf) < fmb:
            break
        _check_cancel(cancel, proc)
        mband = np.frombuffer(buf, np.uint8).reshape(mh, mw, 3)
        menus[k] = profile.score_menu(mband)
        k += 1
        if progress and k % 10000 == 0:
            progress("menu", k)
    proc.wait()
    return menus


def _scan_motion(pipe, n, cancel, progress):
    """Pass 2: mean abs frame-diff on downscaled gray frames, then a windowed-mean
    smooth. slow-mo is sustained low motion, whereas juddery fast action (near-dup
    frames during a camera swing) alternates low/high and averages high, so a
    per-frame test would trip on those dips."""
    proc = pipe(f"scale={MOT_W}:{MOT_H}", "gray")
    mb = MOT_W * MOT_H
    motion_v = [0.0] * n
    prev, j = None, 0
    while True:
        buf = proc.stdout.read(mb)
        if len(buf) < mb:
            break
        _check_cancel(cancel, proc)
        fr = np.frombuffer(buf, np.uint8).reshape(MOT_H, MOT_W).astype(np.int16)
        if prev is not None and j - 1 < n:
            motion_v[j - 1] = float(np.abs(fr - prev).mean())
        prev = fr; j += 1
        if progress and j % 10000 == 0:
            progress("motion", j)
    proc.wait()
    if n >= 2:
        motion_v[n - 1] = motion_v[n - 2]
    if not n:
        return motion_v
    return np.convolve(np.asarray(motion_v, np.float32), np.ones(9) / 9, mode="same").tolist()


def detect(video, game="rebirth", thresh=None, l2_frozen=None, motion=MOTION,
           slow_cap=SLOW_CAP, nr2=NR2, merge_gap=MERGE_GAP, min_dur=MIN_DUR, lead=LEAD,
           badge_mode="auto", include=None, exclude=None,
           start=0.0, duration=None, progress=None, cancel=None):
    """Scan `video`, return dict with intervals and metadata. `game` selects the
    HUD profile ('rebirth', 'remake', or 'revelation'). thresh/l2_frozen fall back
    to the profile's recommended values when None. start/duration limit the scan to
    a section of the video (seconds); returned interval times are still absolute.
    If `cancel` (a threading.Event) is set mid-scan, decoding stops and Cancelled is
    raised.

    `badge_mode` says what to assume about the L2/R2 allies prompt:
      'auto'     - use every signal (badges plus the "Tactical Mode" text and command
                   panel). Best general choice, including party fights over bright
                   scenery where a washed-out badge is caught by the other signals.
      'none'     - a solo fight with no L2/R2 prompt: ignore the badges (and their veto)
                   and detect from the text and command panel only.
      'required' - badges are always clearly visible while Tactical: flag only frames
                   where a badge shows, and drop the text/panel signals. Highest
                   precision, but loses any frame where a badge does wash out.

    `include`/`exclude` are lists of (start, end) second ranges: `include` forces those
    spans to count as Tactical (for segments detection missed), `exclude` removes them
    (for false positives). Both are applied after detection."""
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

    l2s, r2s, nr2s = _scan_badges(pipe, profile, cancel, progress)
    idx = len(l2s)
    # 'required' trusts the badges alone, so the text/panel passes are skipped as unused.
    if badge_mode == "required":
        tacs = menus = [0.0] * idx
    else:
        tacs = _scan_tac_text(pipe, profile, idx, cancel, progress)
        menus = _scan_menu(pipe, profile, idx, cancel, progress)
    ms = _scan_motion(pipe, idx, cancel, progress)

    flags = []
    for i in range(idx):
        strong = r2s[i] > thresh and l2s[i] > thresh and ms[i] < slow_cap
        # both badges very strong: Tactical no matter the motion, for summon/flash
        # frames where bright effects inflate the motion proxy
        confident = l2s[i] > profile.conf_l2 and r2s[i] > profile.conf_r2
        if profile.frozen_mode == "both":
            frozen = l2s[i] > l2_frozen and r2s[i] > l2_frozen and ms[i] < motion
        elif profile.frozen_mode == "off":
            frozen = False
        else:  # 'l2': menu up (L present) and scene frozen
            frozen = l2s[i] > l2_frozen and ms[i] < motion
        badge_hit = strong or confident or frozen
        # header text present and the scene slow-mo. a very strong text match skips the
        # motion gate, same idea as `confident` above.
        tac = tacs[i] > profile.tac_thr and (ms[i] < slow_cap or tacs[i] > profile.tac_conf)
        # command panel up and the scene slow-mo, same shape as the text clause. this
        # is the signal that catches solo Tactical Mode over bright floors, where the
        # thin header text fragments but the panel outline holds.
        menu = menus[i] > profile.menu_thr and (ms[i] < slow_cap or menus[i] > profile.menu_conf)
        # the normal menu also shows an R2 badge, so an R2 match there vetoes a hit -
        # but the normal menu never shows the Tactical Mode text or command panel, so a
        # clear match on either overrides the veto (which otherwise trips on the party
        # switcher over bright backgrounds).
        normal_menu = (nr2s[i] > nr2 and tacs[i] <= profile.tac_thr
                       and menus[i] <= profile.menu_thr)
        if badge_mode == "none":      # solo: no badges, so no badge signals and no veto
            hit = tac or menu
        elif badge_mode == "required":  # badges guaranteed: a badge must be present
            hit = badge_hit and not normal_menu
        else:                         # auto: every signal, additive
            hit = (badge_hit or tac or menu) and not normal_menu
        flags.append(hit)

    ivs = _intervals(flags, fps, merge_gap, min_dur, lead, t0=start)
    if profile.bridge_gap > 0:
        ivs = _bridge_frozen_gaps(ivs, ms, fps, start, profile.bridge_gap, profile.bridge_motion)
    # manual overrides: force `include` spans in, cut `exclude` spans out. clamp to the
    # scanned window so a stray range can't extend past what was decoded.
    if include:
        hi = start + duration if duration else info["duration"]
        clamped = [(max(start, a), min(hi, b)) for a, b in include]
        ivs = _merge_ranges([tuple(iv) for iv in ivs] + clamped)
    if exclude:
        ivs = _subtract_ranges(ivs, exclude)
    total = sum(b - a for a, b in ivs)
    return {
        "video": video, "game": game, "fps": fps,
        "width": info["width"], "height": info["height"],
        "duration": round(info["duration"], 3), "frames": idx,
        "params": {"thresh": thresh, "l2_frozen": l2_frozen, "motion": motion,
                   "slow_cap": slow_cap, "nr2": nr2, "merge_gap": merge_gap,
                   "min_dur": min_dur, "lead": lead, "badges": badge_mode},
        "n_segments": len(ivs),
        "tactical_seconds": round(total, 2),
        "warning": warning,
        "intervals": [{"start": a, "end": b, "dur": round(b - a, 3)} for a, b in ivs],
    }
