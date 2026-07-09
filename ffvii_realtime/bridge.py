"""Bridge the audio across sped-up (Tactical) seams.

Speeding up a Tactical segment turns its audio into a sub-frame blip, so the sound
seems to cut out and jump straight from the before-ambient to the after-ambient.
Bridging replaces each seam with an equal-power crossfade between the real before-
and after-ambient from the source audio, so the sound stays continuous.

Only the audio track is rebuilt; it's remuxed onto the already-rendered video with
-c:v copy and tempo-matched to the video's exact duration (a tiny, inaudible
correction) so the two stay locked.
"""
import os
import shutil
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
    # per segment, gather: its length on the OUTPUT timeline (Tactical shrinks by
    # `factor`, real-time stays 1:1), and its start/end as SOURCE sample indices.
    output_lengths, source_starts, source_ends, is_tactical = [], [], [], []
    for start, end, tactical in segs:
        output_lengths.append(round((end - start) / factor * sr) if tactical
                              else round((end - start) * sr))
        source_starts.append(round((start - src_t0) * sr))
        source_ends.append(round((end - src_t0) * sr))
        is_tactical.append(tactical)
    output_offsets = np.cumsum([0] + output_lengths)   # where each segment starts in the track
    track = np.zeros((int(output_offsets[-1]), 2), dtype=np.float64)
    n_src = len(src)

    def grab(at, count):                                # source[at:at+count], zero-padded
        buf = np.zeros((count, 2), dtype=np.float64)
        lo, hi = max(0, at), min(n_src, at + count)
        if hi > lo:
            buf[lo - at:hi - at] = src[lo:hi]
        return buf

    for i, tactical in enumerate(is_tactical):          # 1) real-time segments copied 1:1
        if not tactical:
            at = int(output_offsets[i])
            track[at:at + output_lengths[i]] = grab(source_starts[i], output_lengths[i])

    max_half = int(round(m_max * sr))                   # 2) bridge each tactical run
    i = 0
    while i < len(segs):
        if not is_tactical[i]:
            i += 1; continue
        # collapse a whole contiguous run of Tactical segments [i, run_end) to one seam,
        # with `prev`/`nxt` the real-time segments immediately before and after it.
        run_end = i
        while run_end < len(segs) and is_tactical[run_end]:
            run_end += 1
        prev, nxt = i - 1, run_end
        if prev >= 0 and nxt < len(segs):               # skip a run at the very start/end
            # seam: where the run sits on the OUTPUT timeline (samples).
            # sped_up_len: the run's length AFTER speed-up - a sub-frame sliver (4s / 100x ~= 0.04s).
            seam = int(output_offsets[i])
            sped_up_len = int(output_offsets[run_end] - output_offsets[i])
            # crossfade half-width, clamped so it never eats more than half of either
            # neighbour (two Tactical runs close together share little real-time audio).
            fade_half = min(max_half, output_lengths[prev] // 2, output_lengths[nxt] // 2)
            if fade_half >= 2:
                # the crossfade STRADDLES the seam: it starts `fade_half` before it (over the
                # tail of the previous real-time audio), spans the sliver, and laps `fade_half`
                # into the next real-time audio. total width `fade_len`.
                fade_len = 2 * fade_half + sped_up_len
                # `entering`: source audio around where the slow-mo BEGAN. reads from
                # `fade_half` before that point forward, so entering[0] equals the real-time
                # audio already at the crossfade's left edge (-> that edge is a seamless copy).
                entering = grab(source_ends[prev] - fade_half, fade_len)
                # `leaving`: source audio around where the slow-mo ENDED, offset so its LAST
                # sample lands `fade_half` past the sliver, matching the real-time audio
                # already at the right edge (-> that edge is seamless too).
                leaving = grab(source_starts[nxt] + fade_half - fade_len, fade_len)
                # equal-power crossfade: cos^2 + sin^2 = 1, so loudness stays flat (no dip).
                # `ramp` sweeps 0->1 over `fade_len`, so it's pure `entering` at the left edge
                # and pure `leaving` at the right; because the sliver is tiny the seam itself
                # falls near ramp=0.5, i.e. a ~50/50 blend of the audio entering vs. leaving
                # the slow-mo. both edges match the audio they overwrite, so only the
                # ~2*fade_half swell in the middle is audible - and the further apart the two
                # excerpts are in the source (a long run), the more they differ and beat.
                ramp = np.linspace(0.0, 1.0, fade_len)[:, None]
                track[seam - fade_half:seam - fade_half + fade_len] = \
                    entering * np.cos(0.5 * np.pi * ramp) + leaving * np.sin(0.5 * np.pi * ramp)
        i = run_end

    return np.clip(np.round(track), -32768, 32767).astype(np.int16)


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
        shutil.rmtree(work, ignore_errors=True)
    return rendered
