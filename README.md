# FFVII Realtime

[![PyPI version](https://img.shields.io/pypi/v/ffvii-realtime)](https://pypi.org/project/ffvii-realtime/)
[![Python versions](https://img.shields.io/pypi/pyversions/ffvii-realtime)](https://pypi.org/project/ffvii-realtime/)
[![License: AGPL v3](https://img.shields.io/pypi/l/ffvii-realtime)](LICENSE)

**Remove Tactical Mode slow-motion from Final Fantasy VII Rebirth combat captures so the whole fight plays at uniform real-time speed.**

In Rebirth, opening the Tactical Mode command menu drops the game into heavy slow-motion while you pick your actions. It's great to play, but it makes a *recording* drag — the footage constantly stutters between real-time action and long slow-motion stretches. FFVII Realtime automatically finds those slow-motion segments and speeds only them back up, leaving the rest of the fight untouched, so the whole thing flows at one natural pace.

> Example: a 1:55:00 capture became ~1:07:00 of continuous, full-speed combat — ~700 Tactical Mode segments detected and sped up, fully audio-synced.

---

## How it works (short version)

1. **Detect** — a computer-vision pass (OpenCV) scans every frame and recognizes Tactical Mode by the on-screen **L2/R2 button prompts** at their fixed positions, made robust to bright/gray/busy backgrounds by combining color + white-mask + black-mask matching. A **motion check** confirms the scene is actually in slow-motion (so a stray badge match during fast action can't trigger a false speed-up).
2. **Render** — FFmpeg re-times each detected segment (`setpts` for video, `atempo` for audio, kept exactly in sync), speeding up the slow-motion while normal-speed combat passes through untouched, then stitches it all back together.

Detection normalizes any 16:9 resolution to 1080p internally, so the bundled templates work at 1080p / 1440p / 4K. Rendering happens at your source's native resolution.

---

## Install

**One line, everything bundled:**

```bash
pipx install ffvii-realtime     # installs the `ffvii-realtime` command, isolated
ffvii-realtime gui              # opens the browser app
```

That's the whole setup: **FFmpeg ships with it** — nothing else to install. Drag your
video into the app (or click **Browse…**, or paste its full path), set the speed-up
factor, click **Start** — the finished file is saved next to the original.

Requires **Python 3.8+**. No `pipx`? [Install it](https://pipx.pypa.io/stable/how-to/install-pipx/),
or just use `pip install ffvii-realtime` (pipx only adds isolation). On Windows, check
*"Add python.exe to PATH"* when installing Python.

Prefer the command line? After installing, the one-shot is:

```bash
ffvii-realtime fix my-fight.mp4 -o my-fight.realtime.mp4
```

See [Command-line usage](#command-line-usage) for previews, ranges, and other games.

---

## Command-line usage

> **Specify the game your footage is from.** Detection is HUD-specific and defaults to **Rebirth**. For Remake or Revelation captures you must pass `--game remake` or `--game revelation` (in the browser UI, choose it from the **Game** dropdown). The wrong game finds 0 segments.

```bash
# already installed via `pipx install ffvii-realtime` (contributors: `pip install -e .`)

# the usual one-shot: detect slow-mo and render the real-time version
ffvii-realtime fix my-fight.mp4 -o my-fight.realtime.mp4

# verify settings on a short window first (recommended)
ffvii-realtime preview my-fight.mp4 --range 4:40-5:20 -o test.mp4

# process only a section of the video (also makes detection faster),
# and mute the sped-up slow-mo audio
ffvii-realtime fix my-fight.mp4 --range 24:00-26:30 --tac-vol 0%

# pick the game (default rebirth)
ffvii-realtime fix my-remake-fight.mp4 --game remake

# separate steps
ffvii-realtime detect my-fight.mp4 -o intervals.json
ffvii-realtime render my-fight.mp4 -i intervals.json -o out.mp4

# launch the browser UI
ffvii-realtime gui
```

### Options

- `--game rebirth|remake|revelation` — which game's HUD to detect. **Set this to match your footage** — detection is HUD-specific, so the wrong value finds 0 segments. Defaults to `rebirth`.
- `--range MM:SS-MM:SS` — process only that section of the video; also speeds up detection since only that span is scanned.
- `--tac-vol` — volume of the sped-up Tactical-Mode audio, as a percentage (`10%`, `0%` for silent, `100%` for full) or a 0–1 fraction. Default `10%`. (Ignored when seam bridging is on, since bridging replaces that audio.)
- `--no-bridge-sound` — turn **off** seam audio bridging. By default, speeding up a Tactical segment would make its audio cut out and jump; bridging instead crossfades the real before/after ambient across the seam so the sound stays continuous. On by default. `--bridge-width` (seconds, default `0.35`) tunes the crossfade half-width.
- `--lead` — start the speed-up this many seconds *before* the menu is detected, to cover the panel slide-in. Default `0.2`.

### The speed-up factor

`--factor` defaults to **100**, which matches the game's **default** "Tactical Mode Slowdown" setting. If you changed that setting, your slow-motion is faster or slower, so pick a different factor. The easiest way to dial it in: run `preview` on a stretch with a long Tactical Mode and try a couple of values — when the sped-up sections look like normal-speed combat, that's your number. (Higher = snappier; the slow-mo is aggressive, so values in the 50–150 range are typical.)

> **Note:** the default of **100×** was arrived at by trial and error — rendering a tactical segment at several factors and picking the one that looks like real-time. It is *not* an officially documented figure, and the actual slowdown almost certainly differs per game and per in-game "Tactical Mode Slowdown" setting. If you know (or have measured) the real slowdown factors for any of the selectable Tactical Mode speed settings in any of these games, please open an issue — that input is very welcome and would let the tool ship accurate per-setting defaults.

---

## Caveats

- **Tested on one capture so far** (1080p, PS5, default HUD). The detection keys on the game's own UI, so it should transfer to any Rebirth footage — but please run `preview` on your own video before committing to a full render.
- **16:9 only.** Ultrawide/non-16:9 captures will warn and may misdetect (the HUD anchors differently).
- **Custom HUD settings** (scale/opacity accessibility options) could shift the badge positions and break detection. Standard HUD is assumed.
- **Pauses / loading / results screens** are static (never slow-motion), so they're correctly left alone — they'll appear at full length in the output.
- A real character movement that happens *during* a slow-motion stretch becomes near-instantaneous when compressed — occasionally a character may appear to "jump." That's inherent to compressing slow-motion that contains motion, not a glitch.

---

## Requirements

- Python 3.8+
- Everything else is installed automatically: `numpy`, `opencv-python-headless`, and FFmpeg (bundled via `imageio-ffmpeg`). If you already have your own FFmpeg on PATH — or set `$FFVII_FFMPEG` / `$FFVII_FFPROBE` — it's used in preference to the bundled build.

## License

ffvii-realtime is **dual-licensed** (see [LICENSING.md](LICENSING.md)):

- **GNU AGPL-3.0** for open-source use (see [LICENSE](LICENSE)) — free to use and
  modify, but if you distribute it or run a modified version as a network service,
  your version must also be released under the AGPL (source available).
- **Commercial license** for proprietary/commercial use without the AGPL's
  copyleft obligations — contact Rob Esris (open an issue on this repo).

Copyright © 2026 Rob Esris.
