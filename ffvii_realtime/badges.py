"""Tactical Mode detection geometry + badge matching, per game.

Both FFVII Remake and Rebirth slow time to a crawl while the Tactical Mode
command menu is open, and both show the L2 / R2 controller-button prompts at
fixed on-screen positions while it is. We detect Tactical Mode by
template-matching those badges. But the two games lay the HUD out differently,
so each game is a `Profile` with its own geometry and templates:

  - REBIRTH: badges are white-on-black; L2 far-left, R2 FAR-RIGHT of the prompt
    row; the normal-play prompt is also lower-left, so it needs a veto window
    (NR2) to avoid mistaking the static normal menu for Tactical.
  - REMAKE: badges are dark-text-on-white shields; the `L2 <name> R2` cluster is
    compact on the left (R2 just right of the name); the normal-play prompt is
    TOP-left, nowhere near the tactical band, so no veto is needed.

All geometry is in REFERENCE 1920x1080 coordinates. Detection normalizes any
16:9 input to 1080p first (see detect.py), so these constants apply to every
resolution.

Each badge is scored as max(color, white-mask, [black-mask]) so no background
type defeats it: color handles ordinary scenes, the white mask handles the
badge's bright fill/outline, the black mask (Rebirth only) the badge's black
fill on bright busy scenes. Mask matches are variance-guarded: a flat (all-same)
mask region makes TM_CCOEFF_NORMED degenerate to 1.0, so such signals are
dropped rather than trusted.
"""
import os
import numpy as np
import cv2

REF_W, REF_H = 1920, 1080
_TPL_DIR = os.path.join(os.path.dirname(__file__), "templates")


def white_mask(bgr, thr):
    return (cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) > thr).astype(np.float32)


def black_mask(bgr, thr):
    return (cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) < thr).astype(np.float32)


def _best(roi, templates):
    return max(float(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED).max()) for t in templates)


def _best_guarded(roi_mask, tpl_masks):
    """Like _best but skips zero-variance masks. A flat mask (e.g. a window with
    no near-white pixels at all) makes TM_CCOEFF_NORMED return a meaningless 1.0;
    treat that as no signal (0.0)."""
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
        # High-confidence override: if BOTH badges match this strongly, it's Tactical
        # even if motion is high (the slow_cap motion gate is meant to reject *fluke*
        # badge matches during fast action, not crisp ~1.0 matches during a bright,
        # particle-heavy summon). A fluke never lands both badges this high.
        self.conf_l2 = conf_l2
        self.conf_r2 = conf_r2
        # Frozen-gap bridge: if a near-frozen (slow-mo) gap up to `bridge_gap` seconds
        # sits between two detected Tactical segments, fill it. Rescues stretches where
        # the badges are momentarily unreadable (e.g. a white-flash whiteout) but the
        # menu is clearly still open (slow-mo, badges seen just before and after).
        # 0 disables. Off by default; on for games that need it.
        self.bridge_gap = bridge_gap
        self.bridge_motion = bridge_motion
        self.l2_col = [_load(tpl_subdir, "l2_a.png"), _load(tpl_subdir, "l2_b.png")]
        self.r2_col = [_load(tpl_subdir, "r2_a.png"), _load(tpl_subdir, "r2_b.png")]
        self.l2_wht = [white_mask(t, white_thr) for t in self.l2_col]
        self.r2_wht = [white_mask(t, white_thr) for t in self.r2_col]
        self.l2_blk = [black_mask(t, black_thr) for t in self.l2_col]
        self.r2_blk = [black_mask(t, black_thr) for t in self.r2_col]
        # Optional "Tactical Mode" header-text signal (top-left). The L2/R2 badges
        # are an *allies* prompt and vanish in solo boss fights (one playable
        # character, no party) -> badge-only detection finds nothing there. But the
        # "Tactical Mode" text above the command menu is shown whenever the menu is
        # open, solo or party, so matching it rescues solo fights and reinforces
        # party ones. Decoded as its own small band in detect.py (see TAC_BAND).
        self.TAC_BAND = tac_band      # (x, y, w, h) top-left text band, or None to disable
        self.TAC_SUB = tac_sub if tac_sub is not None else (
            (0, 0, tac_band[2], tac_band[3]) if tac_band is not None else None)
        self.tac_thr = tac_thr        # "Tactical Mode" text match threshold
        self.tac_conf = tac_conf      # high-confidence text match: bypasses the motion gate
        if tac_band is not None:
            self.tac_col = [_load(tpl_subdir, "tac_a.png"), _load(tpl_subdir, "tac_b.png")]
            self.tac_wht = [white_mask(t, white_thr) for t in self.tac_col]
        else:
            self.tac_col = self.tac_wht = None

    def _score(self, band, sub, col, wht, blk):
        x, y, w, h = sub
        roi = band[y:y + h, x:x + w]
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
        """Match the R2 glyph at the NORMAL-menu position (Rebirth veto). 0.0 if the
        profile has no veto window."""
        if self.NR2_SUB is None:
            return 0.0
        return self._score(band, self.NR2_SUB, self.r2_col, self.r2_wht, self.r2_blk)

    def score_tac(self, tac_band):
        """Match the 'Tactical Mode' header text in its top-left band. 0.0 if the
        profile has no tac band. Scored as max(color, guarded white-mask) so a busy
        or bright background behind the text can't defeat it."""
        if self.tac_col is None:
            return 0.0
        x, y, w, h = self.TAC_SUB
        roi = tac_band[y:y + h, x:x + w]
        c = _best(roi, self.tac_col)
        m = _best_guarded(white_mask(roi, self.white_thr), self.tac_wht)
        return max(c, m)


# Rebirth: white-on-black badges, R2 far-right, lower-left normal menu -> needs veto.
# (Numeric path identical to the original single-profile detector: max(color,
# white@200, black@50), no variance guard.)
REBIRTH = Profile(
    "rebirth",
    band=(35, 645, 466, 64),
    l2_sub=(0, 0, 64, 52),
    r2_sub=(398, 0, 68, 52),
    tpl_subdir="rebirth",
    white_thr=200, black_thr=50, use_black=True, guard=False,
    nr2_sub=(53, 3, 90, 60),
    # "Tactical Mode" header text, top-left. Rescues solo boss fights (Cloud vs.
    # Rufus etc.) where there is no party, so no L2/R2 allies prompt ever appears.
    # Band spans the text with slack for the slide-in; template is the glyphs only.
    tac_band=(60, 606, 360, 70),
)

# Remake: dark-text-on-white shield badges, compact L2 <name> R2 cluster on the
# left; normal menu is top-left (out of band) so no veto. White-mask is the
# reliable signal; search windows are wide to tolerate the slide-in animation and
# party/name-length shifts. Variance-guarded (the white badge can blow out the
# black mask, and empty windows would otherwise read 1.0).
REMAKE = Profile(
    "remake",
    # Band hugs the badge ROW only (badges ~y650-686). A taller band reached up into
    # the dialogue-subtitle zone and down into decorative scenery (闘/鳳 banners),
    # whose white glyphs matched the badge white-mask -> false positives.
    band=(70, 646, 400, 44),
    l2_sub=(0, 0, 120, 44),
    r2_sub=(240, 0, 160, 44),
    tpl_subdir="remake",
    white_thr=180, use_black=False, guard=True,
    nr2_sub=None,
    # White-flash effects can wash the badges out completely mid-menu; bridge
    # near-frozen gaps (up to 12s) between detected segments to cover them.
    bridge_gap=12.0, bridge_motion=2.0,
)

# Revelation: Rebirth-like layout (left badge far-left, right badge far-right) but
# the badges are black "- -" glyphs (white outline + dashes) and are mirror images
# of each other; a 3-portrait character switcher (+ arrows) sits between them, and
# the active name is below the badge row. Normal play has no badges at the band (a
# "Commands Menu" bar lives lower), so no veto. The black badge body can match dark
# background blobs at the LEFT position alone, so BOTH badges are required (strong
# clause and a both-badge frozen clause); the left-only frozen rescue is unsafe here.
REVELATION = Profile(
    "revelation",
    # Band hugs the badge row only (badges ~y643-679) so dialogue subtitles above the
    # menu and scenery below can't match — same rule as the other profiles.
    band=(30, 640, 470, 44),
    l2_sub=(0, 0, 90, 44),
    r2_sub=(380, 0, 90, 44),
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
