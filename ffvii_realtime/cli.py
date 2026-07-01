"""Command-line interface for ffvii-realtime.

  ffvii-realtime detect  INPUT [-o intervals.json]      # find Tactical segments
  ffvii-realtime preview INPUT --range MM:SS-MM:SS      # quick verify on a window
  ffvii-realtime fix     INPUT [-o output.mp4]          # detect + render (the usual command)
  ffvii-realtime render  INPUT -i intervals.json        # render from existing intervals
  ffvii-realtime gui                                    # launch the local web UI
"""
import argparse
import json
import os
import sys

from .detect import detect, LEAD, THRESH, SLOW_CAP, NR2, MERGE_GAP, MIN_DUR
from .render import render


def parse_volume(s):
    """Parse a Tactical-audio volume into a 0..1 multiplier.

    Accepts a percentage ('10%', '0%', '100%' -> 0.10, 0.0, 1.0) or a bare
    fraction ('0.1' -> 0.1) for backwards compatibility. 0(%) is silent.
    """
    s = str(s).strip()
    if s.endswith("%"):
        v = float(s[:-1]) / 100.0
    else:
        v = float(s)
    if v < 0:
        raise argparse.ArgumentTypeError(f"volume must be >= 0 (got {s!r})")
    return v


def _parse_range(s):
    """'MM:SS-MM:SS' or 'secs-secs' -> (start, end) in seconds."""
    def t(x):
        x = x.strip()
        if ":" in x:
            m, s = x.split(":")
            return int(m) * 60 + float(s)
        return float(x)
    a, b = s.split("-")
    return t(a), t(b)


def _add_range_opt(p, required=False):
    p.add_argument("--range", required=required,
                   help="only process a section of the video, MM:SS-MM:SS "
                        "(e.g. 24:00-26:30); also speeds up detection")


def _range_args(args):
    """-> (start, duration, window) from optional args.range; whole video if absent."""
    rng = getattr(args, "range", None)
    if not rng:
        return 0.0, None, None
    lo, hi = _parse_range(rng)
    return lo, hi - lo, (lo, hi)


def _add_detect_opts(p):
    p.add_argument("--game", choices=["rebirth", "remake", "revelation"], default="rebirth",
                   help="which game's HUD to detect (default rebirth)")
    p.add_argument("--lead", type=float, default=LEAD,
                   help="extend each segment start earlier to cover the panel slide-in (s)")
    p.add_argument("--thresh", type=float, default=None,
                   help="badge-match threshold (default: per-game)")
    p.add_argument("--slow-cap", type=float, default=SLOW_CAP,
                   help="max motion for a frame to count as slow-mo (rejects fluke matches)")
    p.add_argument("--nr2", type=float, default=NR2,
                   help="veto threshold: reject frames where R2 matches the normal "
                        "command-menu position (not Tactical)")
    p.add_argument("--merge-gap", type=float, default=MERGE_GAP,
                   help="merge Tactical segments separated by less than this gap (s)")
    p.add_argument("--min-dur", type=float, default=MIN_DUR,
                   help="discard detected segments shorter than this (s)")


def _add_render_opts(p):
    p.add_argument("--factor", type=float, default=100.0,
                   help="speed-up factor for Tactical segments (default 100; depends on your "
                        "in-game Tactical Mode Slowdown setting)")
    p.add_argument("--tac-vol", type=parse_volume, default=0.1,
                   help="volume of Tactical-segment audio: a percentage ('10%%', '0%%' "
                        "for silent) or a 0-1 fraction (default 10%%)")
    p.add_argument("--crf", type=int, default=18, help="x264 quality (lower=better, 18=near-lossless)")
    p.add_argument("--preset", default="slow",
                   help="x264 speed/efficiency preset (slower = smaller file; default slow)")
    p.add_argument("--no-bridge-sound", dest="bridge_sound", action="store_false",
                   help="keep the raw sped-up audio at each Tactical seam instead of "
                        "crossfading the real before/after ambient across it (bridging is on "
                        "by default, so the sound never cuts out)")
    p.add_argument("--bridge-width", type=float, default=0.35,
                   help="seam crossfade half-width in seconds (total crossfade = 2x this)")
    p.add_argument("--force", action="store_true",
                   help="overwrite the output file if it already exists (default: refuse)")


def _progress_detect(stage, n):
    print(f"  scanning ({stage}): {n} frames...", file=sys.stderr)


def _progress_render(i, total, status):
    print(f"  chunk {i}/{total} {status}", file=sys.stderr)


def _abort_if_empty(res):
    """Nothing detected means nothing to speed up, so skip the render (it would just
    copy the input) and exit with an actionable message."""
    if res.get("n_segments", 0) == 0:
        sys.stderr.write(
            "No Tactical Mode segments detected; nothing to speed up, so no video was "
            "written.\n"
            "  - Check that --game matches your footage (rebirth/remake/revelation); "
            "the wrong game finds 0 segments.\n"
            "  - If you passed --range, make sure that span actually contains Tactical Mode.\n")
        raise SystemExit(3)


def _guard_out(out, force):
    """Refuse to clobber an existing output unless --force was given."""
    if os.path.exists(out) and not force:
        sys.stderr.write(
            "Error: output already exists: %s\n"
            "       pass --force to overwrite, or -o to write to a different path.\n" % out)
        raise SystemExit(2)


def _run_detect(args):
    start, dur, _ = _range_args(args)
    where = f" ({args.range})" if getattr(args, "range", None) else ""
    print(f"Detecting Tactical Mode segments in {args.input}{where} ...", file=sys.stderr)
    res = detect(args.input, game=args.game, thresh=args.thresh, slow_cap=args.slow_cap, nr2=args.nr2,
                         merge_gap=args.merge_gap, min_dur=args.min_dur, lead=args.lead,
                         start=start, duration=dur, progress=_progress_detect)
    if res.get("warning"):
        print(f"WARNING: {res['warning']}", file=sys.stderr)
    # only the `detect` command names the intervals file via -o; for fix/preview
    # -o is the video output, so write intervals to a derived path instead.
    out = (args.out if args.cmd == "detect" else None) \
        or os.path.splitext(args.input)[0] + ".intervals.json"
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    span = dur if dur else res["duration"]
    print(f"{res['n_segments']} segments, {res['tactical_seconds']:.0f}s tactical "
          f"({100 * res['tactical_seconds'] / span:.1f}%) -> {out}")
    return res, out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ffvii-realtime", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            _ver = version("ffvii-realtime")
        except PackageNotFoundError:
            _ver = "dev"
    except Exception:
        _ver = "dev"
    ap.add_argument("--version", action="version", version=f"%(prog)s {_ver}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    VID = "recorded gameplay video (e.g. capture.mp4)"
    d = sub.add_parser("detect", help="find Tactical Mode segments -> intervals.json")
    d.add_argument("input", help=VID)
    d.add_argument("-o", "--out", help="intervals JSON output (default: INPUT.intervals.json)")
    _add_detect_opts(d); _add_range_opt(d)

    f = sub.add_parser("fix", help="detect + render (the usual one-shot command)")
    f.add_argument("input", help=VID)
    f.add_argument("-o", "--out", help="output video (default: INPUT.realtime.mp4)")
    _add_detect_opts(f); _add_render_opts(f); _add_range_opt(f)

    r = sub.add_parser("render", help="render from an existing intervals.json")
    r.add_argument("input", help=VID)
    r.add_argument("-i", "--intervals", required=True, help="intervals JSON from `detect`")
    r.add_argument("-o", "--out", help="output video (default: INPUT.realtime.mp4)")
    _add_render_opts(r); _add_range_opt(r)

    pv = sub.add_parser("preview", help="detect+render a short window to verify settings")
    pv.add_argument("input", help=VID)
    pv.add_argument("-o", "--out", help="output video (default: INPUT.preview.mp4)")
    _add_detect_opts(pv); _add_render_opts(pv)
    _add_range_opt(pv, required=True)

    g = sub.add_parser("gui", help="launch the local web UI in your browser")
    g.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)

    # check the input path up front; a common gotcha is a pasted path that wrapped in
    # the terminal and picked up a newline
    if getattr(args, "input", None) is not None:
        args.input = args.input.strip()
        if not os.path.isfile(args.input):
            hint = ""
            if "\n" in args.input:
                hint = ("  (the path has a line break in it; re-enter it on one line, "
                        "or drag the file into the terminal)")
            sys.stderr.write("Error: no such file: %r%s\n" % (args.input, hint))
            raise SystemExit(2)

    if args.cmd == "detect":
        _run_detect(args)

    elif args.cmd == "fix":
        out = args.out or os.path.splitext(args.input)[0] + ".realtime.mp4"
        _guard_out(out, args.force)
        res, ipath = _run_detect(args)
        _abort_if_empty(res)
        _, _, window = _range_args(args)
        print(f"Rendering -> {out} (factor {args.factor}x) ...", file=sys.stderr)
        render(args.input, res["intervals"], out, factor=args.factor,
                       tac_vol=args.tac_vol, crf=args.crf, preset=args.preset,
                       window=window, progress=_progress_render,
                       bridge_sound=args.bridge_sound, bridge_width=args.bridge_width)
        print(f"Done -> {out}")

    elif args.cmd == "render":
        out = args.out or os.path.splitext(args.input)[0] + ".realtime.mp4"
        _guard_out(out, args.force)
        data = json.load(open(args.intervals))
        ivs = data["intervals"] if isinstance(data, dict) else data
        _, _, window = _range_args(args)
        render(args.input, ivs, out, factor=args.factor, tac_vol=args.tac_vol,
                       crf=args.crf, preset=args.preset, window=window, progress=_progress_render,
                       bridge_sound=args.bridge_sound, bridge_width=args.bridge_width)
        print(f"Done -> {out}")

    elif args.cmd == "preview":
        out = args.out or os.path.splitext(args.input)[0] + ".preview.mp4"
        _guard_out(out, args.force)
        # detect only the window (fast), then render it; interval times are absolute
        res, _ = _run_detect(args)
        _abort_if_empty(res)
        _, _, window = _range_args(args)
        render(args.input, res["intervals"], out, factor=args.factor, tac_vol=args.tac_vol,
                       crf=args.crf, preset=args.preset, window=window,
                       progress=_progress_render,
                       bridge_sound=args.bridge_sound, bridge_width=args.bridge_width)
        print(f"Preview ({args.range}) -> {out}")

    elif args.cmd == "gui":
        from .web import serve
        serve(port=args.port)


if __name__ == "__main__":
    main()
