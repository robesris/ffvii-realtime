"""Bridge the audio across sped-up (Tactical) seams.

When a Tactical segment is sped up, its audio becomes a sub-frame "chipmunk" blip
and the sound seems to cut out, with a hard jump from the before-ambient to the
after-ambient. Bridging replaces each seam with an equal-power crossfade between the
REAL before- and after-ambient pulled from the original source audio, so the sound
connects seamlessly instead of dropping out.

This rebuilds only the AUDIO track and remuxes it onto the already-rendered video
with -c:v copy (no video re-encode), tempo-matching the audio to the video's exact
duration (a ~0.01% correction, inaudible) so the two stay locked end to end.
"""
import os
import subprocess
import tempfile
import wave

import numpy as np

from .ffmpeg_util import ffmpeg, probe
from .render import build_segments


def build_bridged_track(src, sr, segs, factor, m_max=0.35, src_t0=0.0):
    """Int16 stereo track for the output timeline of `segs`, each Tactical seam
    replaced by an equal-power crossfade between the real before/after ambient.

    src       : int16 [N, 2] source samples
    src_t0    : source time (seconds) of src[0] (for windowed extraction)
    """
    out_len, src_a, src_b, is_tac = [], [], [], []
    for a, b, t in segs:
        out_len.append(round((b - a) / factor * sr) if t else round((b - a) * sr))
        src_a.append(round((a - src_t0) * sr)); src_b.append(round((b - src_t0) * sr))
        is_tac.append(t)
    out_off = np.cumsum([0] + out_len)
    out = np.zeros((int(out_off[-1]), 2), dtype=np.float64)
    N = len(src)

    def grab(s0, n):                                    # source[s0:s0+n], zero-padded
        buf = np.zeros((n, 2), dtype=np.float64)
        a, b = max(0, s0), min(N, s0 + n)
        if b > a:
            buf[a - s0:b - s0] = src[a:b]
        return buf

    for i, t in enumerate(is_tac):                      # 1) real-time segments 1:1
        if not t:
            o0 = int(out_off[i]); out[o0:o0 + out_len[i]] = grab(src_a[i], out_len[i])

    mmax_s = int(round(m_max * sr))                     # 2) bridge each tactical run
    i = 0
    while i < len(segs):
        if not is_tac[i]:
            i += 1; continue
        j = i
        while j < len(segs) and is_tac[j]:
            j += 1
        p, nx = i - 1, j
        if p >= 0 and nx < len(segs):
            seam = int(out_off[i]); g_s = int(out_off[j] - out_off[i])
            m_s = min(mmax_s, out_len[p] // 2, out_len[nx] // 2)
            if m_s >= 2:
                L = 2 * m_s + g_s
                pre = grab(src_b[p] - m_s, L)
                post = grab(src_a[nx] + m_s - L, L)
                w = np.linspace(0.0, 1.0, L)[:, None]
                out[seam - m_s:seam - m_s + L] = pre * np.cos(0.5 * np.pi * w) + \
                                                 post * np.sin(0.5 * np.pi * w)
        i = j

    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def _write_wav(path, samples, sr):
    w = wave.open(path, "wb")
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(samples.tobytes()); w.close()


def bridge_audio(rendered, source, intervals, factor, window=None, m_max=0.35, sr=44100):
    """Replace `rendered`'s audio with a seam-bridged track built from `source`.

    Edits `rendered` in place (audio-only remux; video stream is copied).
    window=(lo, hi) limits to that source span (previews); None = whole video.
    """
    dur = probe(source)["duration"]
    lo, hi = window if window else (0.0, dur)
    segs = build_segments(intervals, lo, hi)
    if not any(t for *_, t in segs):                    # nothing to bridge
        return rendered

    work = tempfile.mkdtemp(prefix="ffvii_bridge_")
    pcm = os.path.join(work, "src.pcm"); wav = os.path.join(work, "a.wav")
    try:
        # extract source audio for the window (+1s margin for crossfade reach)
        t0 = max(0.0, lo - 1.0); t1 = min(dur, hi + 1.0)
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}",
                        "-i", source, "-map", "0:a", "-ac", "2", "-ar", str(sr),
                        "-f", "s16le", pcm], check=True)
        src = np.frombuffer(open(pcm, "rb").read(), dtype=np.int16).reshape(-1, 2)
        track = build_bridged_track(src, sr, segs, factor, m_max, src_t0=t0)
        _write_wav(wav, track, sr)

        # tempo-match to the rendered video duration so a/v stay locked
        vdur = probe(rendered)["duration"]; adur = len(track) / sr
        tempo = max(0.5, min(2.0, adur / vdur))
        af = f"[1:a]atempo={tempo:.8f},aresample={sr}[a]"
        tmp = rendered + ".bridge.mp4"
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", rendered, "-i", wav,
                        "-filter_complex", af, "-map", "0:v:0", "-map", "[a]",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", tmp], check=True)
        os.replace(tmp, rendered)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return rendered
