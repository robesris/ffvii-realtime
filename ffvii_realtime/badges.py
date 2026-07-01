"""Per-game HUD geometry and template matching for Tactical Mode.

While the Tactical Mode command menu is open the game runs in slow-motion and
shows the L2/R2 button prompts at fixed positions, so we template-match those
badges. Each game lays out the HUD differently, so it gets its own Profile with
crop geometry and templates (see the profile definitions at the bottom).

Coordinates are in a 1920x1080 reference frame. detect.py scales every input to
1080p before cropping, so the same numbers work at any resolution.

Each badge scores as max(color, white-mask, black-mask): color for ordinary
scenes, the masks for the badge's bright or dark fill over busy backgrounds. Mask
scores are variance-guarded, since a flat mask makes TM_CCOEFF_NORMED return a
bogus 1.0.
"""
import os
from collections import namedtuple

import numpy as np
import cv2

REF_W, REF_H = 1920, 1080
_TPL_DIR = os.path.join(os.path.dirname(__file__), "templates")

# a crop or search rectangle, in 1080p reference pixels
Box = namedtuple("Box", "x y w h")

# --- HUD geometry, in 1080p reference pixels -------------------------------------
# Where each game draws the prompts we match. BAND is the strip decoded per frame;
# L2/R2/NR2/TAC are search windows within it. Tweak these if a capture's HUD sits
# differently (e.g. a non-standard HUD scale).
REBIRTH_BAND = Box(x=35, y=645, w=466, h=64)
REBIRTH_L2 = Box(x=0, y=0, w=64, h=52)
REBIRTH_R2 = Box(x=398, y=0, w=68, h=52)
REBIRTH_NR2 = Box(x=53, y=3, w=90, h=60)         # normal-menu R2, for the veto
REBIRTH_TAC = Box(x=60, y=606, w=360, h=70)      # "Tactical Mode" header text (solo fights)

REMAKE_BAND = Box(x=70, y=646, w=400, h=44)      # badge row only; taller catches bright scenery
REMAKE_L2 = Box(x=0, y=0, w=120, h=44)           # wide windows tolerate the slide-in + name length
REMAKE_R2 = Box(x=240, y=0, w=160, h=44)

REVELATION_BAND = Box(x=30, y=640, w=470, h=44)  # badge row only
REVELATION_L2 = Box(x=0, y=0, w=90, h=44)
REVELATION_R2 = Box(x=380, y=0, w=90, h=44)

# --- teal-mask tuning (the "Tactical Mode" header text is a distinct teal) -------
TEAL_MIN_G, TEAL_MIN_B = 120, 110        # green and blue must be at least this bright
TEAL_G_OVER_R, TEAL_B_OVER_R = 25, 10    # ...and this much brighter than red
TEAL_MAX_COVERAGE = 0.30                 # teal over more of the band than this is
                                         # background scenery, not the header text


def white_mask(bgr, thr):
    return (cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) > thr).astype(np.float32)


def black_mask(bgr, thr):
    return (cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) < thr).astype(np.float32)


def teal_mask(bgr):
    """Mask the teal 'Tactical Mode' header glyphs, ignoring the background.

    Over a very bright background, color and white-mask matching of the text fall
    apart because they correlate the whole patch. The glyph teal doesn't, so we
    threshold to it and match the shape instead. Teal here means green and blue both
    bright and clearly above red.
    """
    b = bgr[:, :, 0].astype(np.int16)
    g = bgr[:, :, 1].astype(np.int16)
    r = bgr[:, :, 2].astype(np.int16)
    return ((g > TEAL_MIN_G) & (b > TEAL_MIN_B) &
            (g - r > TEAL_G_OVER_R) & (b - r > TEAL_B_OVER_R)).astype(np.float32)


def _best(roi, templates):
    return max(float(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED).max()) for t in templates)


def _best_guarded(roi_mask, tpl_masks):
    """Like _best, but a flat (zero-variance) mask scores 0 instead of the bogus
    1.0 that TM_CCOEFF_NORMED returns for it."""
    if float(roi_mask.std()) < 1e-6:
        return 0.0
    vals = [float(cv2.matchTemplate(roi_mask, tm, cv2.TM_CCOEFF_NORMED).max())
            for tm in tpl_masks if float(tm.std()) >= 1e-6]
    return max(vals) if vals else 0.0


def _load(*parts):
    path = os.path.join(_TPL_DIR, *parts)
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"missing bundled template: {os.path.join(*parts)}")
    return img


class Profile:
    """Per-game HUD geometry + badge templates."""

    def __init__(self, name, band, l2_sub, r2_sub, tpl_subdir,
                 white_thr=200, black_thr=50, use_black=True, guard=False, nr2_sub=None,
                 thresh=0.48, l2_frozen=0.55, frozen_mode="l2",
                 conf_l2=0.90, conf_r2=0.60, bridge_gap=0.0, bridge_motion=2.0,
                 tac_band=None, tac_sub=None, tac_thr=0.55, tac_conf=0.80):
        self.name = name
        self.BAND = band              # (x, y, w, h) of the band decoded in pass 1
        self.L2_SUB = l2_sub          # L2 search window within the band
        self.R2_SUB = r2_sub          # R2 search window within the band
        self.NR2_SUB = nr2_sub        # normal-menu R2 veto window (Rebirth only), or None
        self.white_thr = white_thr
        self.black_thr = black_thr
        self.use_black = use_black
        self.guard = guard
        # detection tuning (detect() falls back to these when the caller passes None)
        self.thresh = thresh          # strong-match threshold for both badges
        self.l2_frozen = l2_frozen    # badge threshold for the "frozen" rescue clause
        self.frozen_mode = frozen_mode  # 'l2' (L only), 'both' (L and R), or 'off'
        # if both badges match this strongly, count it as Tactical even at high motion.
        # the motion gate only exists to drop stray single-badge matches during fast
        # action, and those never land both badges near 1.0.
        self.conf_l2 = conf_l2
        self.conf_r2 = conf_r2
        # fill a near-frozen gap of up to bridge_gap seconds between two detected
        # segments (the badges go briefly unreadable mid-menu, e.g. a white flash, but
        # the menu is clearly still open). 0 disables it.
        self.bridge_gap = bridge_gap
        self.bridge_motion = bridge_motion
        self.l2_col = [_load(tpl_subdir, "l2_a.png"), _load(tpl_subdir, "l2_b.png")]
        self.r2_col = [_load(tpl_subdir, "r2_a.png"), _load(tpl_subdir, "r2_b.png")]
        self.l2_wht = [white_mask(t, white_thr) for t in self.l2_col]
        self.r2_wht = [white_mask(t, white_thr) for t in self.r2_col]
        self.l2_blk = [black_mask(t, black_thr) for t in self.l2_col]
        self.r2_blk = [black_mask(t, black_thr) for t in self.r2_col]
        # optional "Tactical Mode" header-text signal (top-left). the L2/R2 badges are
        # an allies prompt, so they never show in solo boss fights; the header text is
        # always up while the menu is open, so it covers solo fights and backs up party
        # ones. decoded as its own small band in detect.py (see TAC_BAND).
        self.TAC_BAND = tac_band      # (x, y, w, h) top-left text band, or None to disable
        self.TAC_SUB = tac_sub if tac_sub is not None else (
            Box(0, 0, tac_band.w, tac_band.h) if tac_band is not None else None)
        self.tac_thr = tac_thr        # "Tactical Mode" text match threshold
        self.tac_conf = tac_conf      # high-confidence text match: bypasses the motion gate
        if tac_band is not None:
            self.tac_col = [_load(tpl_subdir, "tac_a.png"), _load(tpl_subdir, "tac_b.png")]
            self.tac_wht = [white_mask(t, white_thr) for t in self.tac_col]
            self.tac_teal = [teal_mask(t) for t in self.tac_col]
        else:
            self.tac_col = self.tac_wht = self.tac_teal = None

    def _score(self, band, sub, col, wht, blk):
        roi = band[sub.y:sub.y + sub.h, sub.x:sub.x + sub.w]
        c = _best(roi, col)
        if self.guard:
            m = _best_guarded(white_mask(roi, self.white_thr), wht)
            k = _best_guarded(black_mask(roi, self.black_thr), blk) if self.use_black else 0.0
        else:
            m = _best(white_mask(roi, self.white_thr), wht)
            k = _best(black_mask(roi, self.black_thr), blk) if self.use_black else 0.0
        return max(c, m, k)

    def score_l2(self, band):
        return self._score(band, self.L2_SUB, self.l2_col, self.l2_wht, self.l2_blk)

    def score_r2(self, band):
        return self._score(band, self.R2_SUB, self.r2_col, self.r2_wht, self.r2_blk)

    def score_nr2(self, band):
        """Match the R2 glyph at the normal-menu position (the Rebirth veto), or 0.0
        if this profile has no veto window."""
        if self.NR2_SUB is None:
            return 0.0
        return self._score(band, self.NR2_SUB, self.r2_col, self.r2_wht, self.r2_blk)

    def score_tac(self, tac_band):
        """Match the 'Tactical Mode' header text in its band, or 0.0 if this profile
        has no tac band. Scored as max(color, white-mask, teal-mask); color and white
        cover ordinary backgrounds, the teal mask covers very bright ones."""
        if self.tac_col is None:
            return 0.0
        sub = self.TAC_SUB
        roi = tac_band[sub.y:sub.y + sub.h, sub.x:sub.x + sub.w]
        c = _best(roi, self.tac_col)
        m = _best_guarded(white_mask(roi, self.white_thr), self.tac_wht)
        # the glyphs cover only ~6% of the band. if teal covers a lot more of it, it's
        # a teal background (bright water and such), not text, so ignore the teal match.
        tm = teal_mask(roi)
        tl = _best_guarded(tm, self.tac_teal) if float(tm.mean()) < TEAL_MAX_COVERAGE else 0.0
        return max(c, m, tl)


# Rebirth: white-on-black badges, L2 far-left and R2 far-right. The normal-play
# prompt sits lower-left too, so we need the NR2 veto window to tell it apart.
# max(color, white@200, black@50), no variance guard.
REBIRTH = Profile(
    "rebirth",
    band=REBIRTH_BAND, l2_sub=REBIRTH_L2, r2_sub=REBIRTH_R2,
    tpl_subdir="rebirth",
    white_thr=200, black_thr=50, use_black=True, guard=False,
    nr2_sub=REBIRTH_NR2,
    # the header text covers solo boss fights (Cloud vs. Rufus) that have no party
    # and so never show the L2/R2 prompt.
    tac_band=REBIRTH_TAC,
)

# Remake: dark-text-on-white shield badges in a compact "L2 <name> R2" cluster on
# the left. The normal menu is top-left, out of the band, so no veto. White-mask is
# the reliable signal; the windows are wide to tolerate the slide-in and name length.
# Variance-guarded (the white badge can blow out the black mask).
REMAKE = Profile(
    "remake",
    band=REMAKE_BAND, l2_sub=REMAKE_L2, r2_sub=REMAKE_R2,
    tpl_subdir="remake",
    white_thr=180, use_black=False, guard=True,
    nr2_sub=None,
    # white flashes can wash the badges out mid-menu, so bridge near-frozen gaps
    # (up to 12s) between detected segments.
    bridge_gap=12.0, bridge_motion=2.0,
)

# Revelation: Rebirth-like layout, but the badges are black "- -" glyphs (white
# outline + dashes), mirror images of each other, with a portrait switcher between
# them. Normal play shows no badges in the band, so no veto. The black badge body
# can match dark background blobs at the left position on its own, so both badges are
# required (strong clause and a both-badge frozen clause); left-only frozen is unsafe.
REVELATION = Profile(
    "revelation",
    band=REVELATION_BAND, l2_sub=REVELATION_L2, r2_sub=REVELATION_R2,
    tpl_subdir="revelation",
    white_thr=200, black_thr=50, use_black=True, guard=True,
    nr2_sub=None,
    thresh=0.6, l2_frozen=0.6, frozen_mode="both",
)

PROFILES = {"rebirth": REBIRTH, "remake": REMAKE, "revelation": REVELATION}


def get_profile(game):
    try:
        return PROFILES[game]
    except KeyError:
        raise ValueError(f"unknown game {game!r}; choose from {sorted(PROFILES)}")
