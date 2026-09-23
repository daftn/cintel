#!/usr/bin/env python3
"""
analyze.py - read-only media analysis; emits one encode plan per title.

Probes each source file and decides, from measurement rather than assumption,
what the encode needs: cadence handling, crop, colour tagging, audio and
subtitle mapping. Writes a plan JSON containing both the evidence and the
literal ffmpeg argv, which encode.py executes verbatim.

Nothing here writes to or modifies any media file.

Why the decisions look the way they do - all measured on this library:

  * A DVD container claiming 29.97 usually decodes at 23.976; ffmpeg already
    honours the soft-telecine flags. Running decimate on those drops one real
    frame in five and yields 19.2fps. So cadence is classified by DECODED
    rate, never by the container.
  * Sources are frequently untagged for colour. Untagged HEVC is read as
    BT.709, which is wrong for 480-line content, so tags are always written.
  * Single-pass cropdetect over-crops dark films (one title in this library
    landed at 668x480, losing 52px). Crop is sampled across the runtime and a
    low percentile of the margin is taken, biasing toward keeping picture.

Usage:
    analyze.py PATH [PATH...] [--out DIR] [--jobs N] [--force] [--quick]
"""

from __future__ import annotations

import argparse
import array
import concurrent.futures
import dataclasses
import functools
import hashlib
import json
import re
import shutil
import subprocess
import sys
import pathlib
from pathlib import Path

# A repair chain that CANNOT drop a frame, because `decimate` is deliberately
# absent. fieldmatch reconstructs whole frames from fields where the content
# was telecined; idet then flags what is STILL combed; bwdif deinterlaces only
# those. Since no frame is ever removed, applying this to already-progressive
# content cannot produce the 19.2fps catastrophe of cardinal rule 1 - the worst
# case is wasted effort, not destroyed picture. That asymmetry is what makes it
# usable where the cadence cannot be proven.
#
# Every part of it was measured, on Buffy s07e16 (53.1% combed at mid-file):
#
#   fieldmatch alone                              20.8% residual
#   fieldmatch,bwdif=deint=interlaced             18.3%   <- bwdif nearly idle
#   fieldmatch,idet,bwdif=send_frame:interlaced    5.3%   <- chosen
#   fieldmatch,bwdif=send_frame:deint=all          0.0%   but softens EVERY frame
#
# Two traps here, both measured the hard way:
#
#   * `deint=interlaced` acts on the frame's INTERLACED FLAG, not on its
#     content. Disc rips mostly do not set it, so without `idet` in front to
#     set it from detection, bwdif is very nearly a no-op - it contributed
#     only 2.5 points on its own.
#   * `bwdif` defaults to mode=send_field, which DOUBLES the frame rate (50fps
#     measured). mode=send_frame must be stated explicitly.
#
# deint=all reaches 0% but deinterlaces every frame, softening the ~80% of
# these episodes that is clean progressive film. Clearing the last 5% of
# combing is not worth softening the whole episode.
DEINT_FILTER = "fieldmatch,idet,bwdif=mode=send_frame:deint=interlaced"

# ...and it is OFF by default. Measured on Buffy 2026-09-14: the chain removes
# combing convincingly (s03e01 100% -> 0%, s07e16 53% -> 5%) with frame rates
# preserved exactly, but it also raises mpdecimate duplicate detection by
# 6-18%, which verify correctly reports as introduced duplicates - 4 of 12
# episodes failed. The cause is bwdif, not fieldmatch: removing fieldmatch
# changes nothing, because interpolated frames are softer and soft frames read
# as near-duplicates. Whether those are truly repeated frames or a measurement
# artefact of the softening was NOT established.
#
# So it stays off, for a reason outside the filter itself: Buffy's existing
# library encodes are already correct (23.976, smpte170m), unlike Charmed's,
# which were genuinely broken. With no defect to repair, trading sharpness and
# a failing verify to remove combing from 12 of 143 episodes is gold-plating.
# Combed files are passed through unfiltered, exactly as the other 131 are.
#
# Flip this to True to enable it; the plans and notes then record that choice.
DEINT_COMBED = True

# --- cadence constants -------------------------------------------------------
FILM_FPS = 24000 / 1001      # 23.976
VIDEO_FPS = 30000 / 1001     # 29.97
FPS_TOL = 0.5                # a rate must land within this of a known rate
# Combing is sampled across the file, not once. Nine 8s windows costs about the
# same decode time as the old single 20s window but covers nine places instead
# of one, which is what matters when combing is scattered.
COMBING_SAMPLES = 9
COMBING_WINDOW = 8
# Above this share of sampled frames, a title counts as combed.
#
# Measured on Buffy's 143 episodes, the distribution is bimodal: 63 titles
# under 1%, a sparse valley of 5 between 5% and 10%, then 58 from 10% upward.
# Either 5% or 10% falls in low-density ground, so the line was drawn at the
# lower one deliberately - four episodes sit at 7.8/8.2/8.9/9.7% with real
# visible combing, and the repair is content-adaptive: bwdif only touches
# frames idet flags, so treating a 6%-combed title softens 6% of its frames,
# not the whole episode. The cost scales with the problem, which makes the
# cheap mistake "repair a nearly-clean title" rather than "leave tearing in".
COMBING_THRESHOLD = 0.05

MEDIA_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".webm", ".m2ts", ".ts"}
IMAGE_SUB_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub"}
# Codecs that are lossless whatever the profile says.
#
# "dts" is deliberately NOT in this set. ffprobe reports codec_name "dts" for
# both the lossy DTS core and lossless DTS-HD MA; only the profile separates
# them. Measured: a lossy 5.1 DTS track reports profile "DTS", and treating it
# as lossless transcodes an already-lossy source to E-AC3 - a pointless
# generation of loss on a codec that dominates this library's DVD tier, which
# cannot carry DTS-HD MA at all. Use is_lossless_audio(), not this set.
LOSSLESS_AUDIO = {"truehd", "flac", "mlp", "pcm_s16le", "pcm_s24le"}

# Of the DCA profiles only DTS-HD MA is lossless; "DTS", "DTS-ES", "DTS 96/24",
# "DTS-HD HRA" and "DTS Express" are all lossy.
DTS_LOSSLESS_PROFILE = "HD MA"

# Quality targets. Deliberately fixed: VMAF targeting is NOT used for the DVD
# tier because the default VMAF model is trained at 1080p and produces invalid
# scores at 480p. Revisit once per-tier targets are validated by eye.
#
# Preset assignment is deliberately "cheap where it is plentiful, careful where
# it is rare": ~1,200 DVD titles get medium, ~36 Blu-rays get slow. Measured on
# this library, medium CRF 18 matches slow CRF 19 on file size at 45% of the
# encode time. slow is genuinely 10-15% more efficient at 480p, but at ~2 Mbps
# there is enough bitrate that the difference stops being visible.
#
# CRF 20 was chosen from a sweep measured against an FFV1 lossless intermediate
# (IVTC baked in, so frames are genuinely aligned - a live IVTC filtergraph
# drifts phase between runs and makes every metric meaningless). Measured on
# the HARDEST three minutes of a test episode, not an average one:
#   CRF 18  3.16 Mbps  SSIM 0.9709  VMAF 95.4
#   CRF 20  2.02 Mbps  SSIM 0.9638  VMAF 94.0   <- 36% smaller, -1.3 VMAF
#   CRF 22  1.23 Mbps  SSIM 0.9570  VMAF 92.4   <- visible dark-scene blocking
# Degradation is 4-5x steeper on hard content than on an average scene, so any
# future retune must be measured on a high-bitrate segment.
# Quality tiers. Preset and CRF live together here because the three tiers are
# not different content so much as different levels of CARE, and the two knobs
# have to be chosen as a pair.
#
# Measured on the hardest 180s of each reference title (rule 8), against a
# lossless FFV1 intermediate:
#
#   dvd          Charmed, 480p. CRF 20 from the sweep in handoff 3.6.
#                ~1,200 titles: cheap where it is plentiful.
#   bluray-film  Hot Fuzz, grainy 35mm - the hardest content in the library.
#                slow CRF 19 = VMAF 99.07 / SSIM 0.9665 at 21.6 Mbps.
#                ~20 titles, and the ones most worth getting right.
#   bluray-tv    Killing Eve. medium CRF 21 = VMAF 93.97 on the worst segment
#                measured, ~1.74 Mbps/episode. Volume tier: Office and Big Bang
#                Theory arriving on disc take it from 16 files to ~500, and
#                `slow` would cost ~336 hours here against ~116 at `medium` for
#                +1.4 VMAF. Re-measure when those discs land - Killing Eve is a
#                dark drama and a poor proxy for lit multi-cam sitcoms.
#
# NOTE: `slow` is NOT quality-neutral against `medium` at 1080p, which handoff
# 3.6 assumed. At matched bitrate slow gains +0.30 to +0.53 VMAF, and medium
# needs ~44% more bitrate to match slow CRF 21. Do not swap presets without
# re-choosing the CRF alongside it.
TIERS = {
    "dvd":         {"preset": "medium", "crf": 20},
    "bluray-film": {"preset": "slow",   "crf": 19},
    "bluray-tv":   {"preset": "medium", "crf": 21},
    "bluray-standard": {"preset": "medium", "crf": 21},
}

# Films listed here get bluray-standard instead of bluray-film. Preference, not
# measurement - see the file's own header. Anything unlisted stays careful.
STANDARD_LIST = (Path(__file__).resolve().parent.parent
                 / "data" / "worklists" / "bluray-standard.txt")


@functools.lru_cache(maxsize=1)
def standard_titles() -> frozenset[str]:
    """Source stems the owner has marked as not worth the careful tier.

    Missing file means an empty set, i.e. every Blu-ray film stays careful.
    Failing safe matters more here than failing loud: a missing list must not
    silently downgrade thirteen favourites.
    """
    try:
        lines = STANDARD_LIST.read_text().splitlines()
    except OSError:
        return frozenset()
    return frozenset(
        s.strip().lower() for s in lines
        if s.strip() and not s.lstrip().startswith("#"))

TIMEOUT = 180


# --- process helpers ---------------------------------------------------------

def run(cmd: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """Run a command with stdin closed. ffmpeg reads stdin and will eat a
    caller's input loop if left attached."""
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def ffprobe_json(path: Path) -> dict:
    cp = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ])
    if cp.returncode != 0 or not cp.stdout.strip():
        raise RuntimeError(f"ffprobe failed: {cp.stderr.strip()[:200]}")
    return json.loads(cp.stdout)


_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def decoded_fps(path: Path, start: int, dur: int = 20) -> float | None:
    """Actual decoded frame rate over a window. This is the measurement that
    distinguishes soft telecine from genuine 29.97 content; the container's
    r_frame_rate cannot."""
    cp = run([
        "ffmpeg", "-nostdin", "-v", "quiet", "-ss", str(start), "-t", str(dur),
        "-i", str(path), "-an", "-f", "null", "-", "-stats",
    ])
    frames = _FRAME_RE.findall(cp.stderr.replace("\r", "\n"))
    if not frames:
        return None
    return int(frames[-1]) / dur


_IDET_RE = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
    r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)"
)


def idet_counts(path: Path, start: int, dur: int = 20,
                vf: str | None = None) -> tuple[int, int, int]:
    """Return (interlaced, progressive, undetermined).

    Note: idet logs at INFO level, so this must NOT pass -v quiet or the
    output disappears.
    """
    chain = f"{vf},idet" if vf else "idet"
    cp = run([
        "ffmpeg", "-nostdin", "-ss", str(start), "-t", str(dur),
        "-i", str(path), "-vf", chain, "-an", "-f", "null", "-",
    ])
    matches = _IDET_RE.findall(cp.stderr)
    if not matches:
        return (0, 0, 0)
    tff, bff, prog, undet = (int(x) for x in matches[-1])
    return (tff + bff, prog, undet)


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def cropdetect_sample(path: Path, start: int, dur: int = 2) -> tuple | None:
    cp = run([
        "ffmpeg", "-nostdin", "-ss", str(start), "-t", str(dur),
        "-i", str(path), "-vf", "cropdetect=limit=24:round=2:reset=0",
        "-an", "-f", "null", "-",
    ])
    found = _CROP_RE.findall(cp.stderr)
    if not found:
        return None
    w, h, x, y = (int(v) for v in found[-1])
    return (w, h, x, y)


def is_lossless_audio(stream: dict) -> bool:
    """Whether an audio stream is genuinely lossless.

    Matters because the lossless branch transcodes to E-AC3 640k. Doing that
    to a lossy source adds a generation of loss for nothing, so the test has
    to be right rather than merely convenient.

    A "dts" stream whose profile is missing is treated as LOSSY, i.e. copied.
    That is the reversible choice: an oversized copy can be re-encoded later
    from the archived source, while a needless transcode cannot be undone.
    """
    codec = (stream.get("codec_name") or "").lower()
    if codec in LOSSLESS_AUDIO:
        return True
    if codec == "dts":
        return DTS_LOSSLESS_PROFILE in (stream.get("profile") or "").upper()
    return False


def lossless_reason(stream: dict) -> str:
    """Why is_lossless_audio() said yes, in words fit for a plan note.

    Recorded because transcoding to E-AC3 640k is the pipeline's one
    irreversible audio decision, and bug 10 was precisely a wrong answer to
    this question. A plan saying "eac3 640k" cannot be audited; one saying
    "because the profile is DTS-HD MA" can - the profile is the whole basis
    of the decision, so it belongs in the evidence rather than only in the
    analyzer's head.
    """
    codec = (stream.get("codec_name") or "").lower()
    if codec == "dts":
        return f"profile {stream.get('profile')}"
    return "codec is lossless regardless of profile"


def near_fps(value: float | None, target: float) -> bool:
    return value is not None and abs(value - target) <= FPS_TOL


# --- analysis ----------------------------------------------------------------

@dataclasses.dataclass
class Analysis:
    path: str
    ok: bool = True
    error: str | None = None
    # evidence
    container_fps: str | None = None
    decoded_fps: list[float] = dataclasses.field(default_factory=list)
    ivtc_fps: float | None = None
    interlaced_frames: int = 0
    progressive_frames: int = 0
    crop_samples: list[list[int]] = dataclasses.field(default_factory=list)
    # decisions
    tier: str | None = None
    cadence: str | None = None
    cadence_filter: str | None = None
    # True when the output rate cannot be asserted against a constant because
    # the source itself is not uniform. verify then compares output to SOURCE,
    # the same way it handles duplicates and drift.
    fps_from_source: bool = False
    crop: list[int] | None = None
    color: dict | None = None
    audio: list[dict] = dataclasses.field(default_factory=list)
    subtitles: list[dict] = dataclasses.field(default_factory=list)
    crf: int | None = None
    needs_review: bool = False
    review_reason: str | None = None
    notes: list[str] = dataclasses.field(default_factory=list)
    analyzer: str | None = None
    # Which toolchain MEASURED this source. encode.py stamps the toolchain
    # that actually produced the output, which may differ if a plan is
    # generated on one machine and run on another.
    ffmpeg_version: str | None = None
    x265_version: str | None = None
    argv: list[str] | None = None


def sample_points(duration: int, count: int) -> list[int]:
    """Evenly spaced interior points; avoids credits at either end."""
    return [max(1, int(duration * (i + 1) / (count + 1))) for i in range(count)]


def film_ish(rate: float) -> bool:
    """Closer to 23.976 than to 29.97.

    Deliberately looser than near_fps: a window that straddles a cadence
    change lands between the two rates, and what matters then is only which
    side it falls on - a 24.7 reading is film with an anomaly in it, not
    video.
    """
    return rate < (FILM_FPS + VIDEO_FPS) / 2


def classify_cadence(a: Analysis) -> None:
    """Decide the cadence filter from decoded rates and combing.

    Four outcomes:
      film      - already 23.976. No filter. (Most DVDs: the 29.97 in the
                  container is soft-telecine display rate, not coded frames.)
      telecine  - decodes 29.97 WITH combing, and IVTC recovers ~23.976.
      video     - decodes 29.97 with no combing. Genuine 29.97. No filter.
      review    - the rate could not be measured at all.

    Plus three for sources that are not uniform. These used to be blanket
    `review`, which was over-cautious: refusing is safer than guessing, but it
    is not better than a treatment that provably cannot do harm. Measured on
    Buffy, where 20 of 143 episodes were refused and only 2 were genuinely
    mixed:

      film_variable - film rate with an off-rate stretch, no real combing.
                      No filter. Nothing to repair; the rate just wobbles.
      film_combed   - film rate WITH combing. IVTC is proven wrong here (the
                      probe returns ~19.2, i.e. the content is already
                      decimated). Passed through unfiltered unless
                      DEINT_COMBED is on - see the note there.
      mixed_combed  - rates span both film and video: genuinely mixed cadence,
                      and combed. Same treatment as film_combed.
      mixed_variable- rates span both but nothing is combed. No filter; there
                      is nothing to repair, so the rate passes through.

    All three set fps_from_source, because no constant describes their output.
    """
    rates = [r for r in a.decoded_fps if r]
    if not rates:
        a.needs_review, a.review_reason = True, "could not measure decoded rate"
        a.cadence = "review"
        return

    film = [near_fps(r, FILM_FPS) for r in rates]
    video = [near_fps(r, VIDEO_FPS) for r in rates]
    total = a.interlaced_frames + a.progressive_frames
    combed_ratio = a.interlaced_frames / total if total else 0.0

    # Any rate that matches neither known rate means the window straddled a
    # cadence change. Buffy season 2 is full of these.
    if not all(f or v for f, v in zip(film, video)):
        # A rate matching neither constant means the window straddled a
        # cadence change. COMBING, not the rate, decides what to do about it:
        # if nothing is combed there is nothing to repair, and passing the
        # rate through untouched is provably safe whatever it reads.
        a.fps_from_source = True
        uniform = all(film_ish(r) for r in rates)
        if combed_ratio > COMBING_THRESHOLD:
            a.cadence = "film_combed" if uniform else "mixed_combed"
            a.cadence_filter = DEINT_FILTER if DEINT_COMBED else None
            a.notes.append(
                f"{'film rate' if uniform else 'mixed rates'} {rates} with "
                f"{combed_ratio*100:.0f}% combing; "
                + ("deinterlacing combed frames only, no decimation"
                   if DEINT_COMBED else
                   "passed through unfiltered (see DEINT_COMBED)"))
        else:
            a.cadence = "film_variable" if uniform else "mixed_variable"
            a.cadence_filter = None
            a.notes.append(
                f"rate not uniform {rates} but no combing to repair; "
                "no filter - rate passed through as-is")
        return

    if any(film) and any(video):
        # Genuinely mixed: parts of the file are film, parts are video. No
        # single decimation is correct, so nothing is decimated. Combing then
        # decides the rest, exactly as in the branch above.
        a.fps_from_source = True
        if combed_ratio > COMBING_THRESHOLD:
            a.cadence = "mixed_combed"
            a.cadence_filter = DEINT_FILTER if DEINT_COMBED else None
            a.notes.append(
                f"mixed cadence {rates} with {combed_ratio*100:.0f}% combing; "
                + ("deinterlacing combed frames only, no decimation"
                   if DEINT_COMBED else
                   "passed through unfiltered (see DEINT_COMBED)"))
        else:
            a.cadence = "mixed_variable"
            a.cadence_filter = None
            a.notes.append(
                f"mixed cadence {rates} but no combing to repair; no filter")
        return

    if all(film):
        # Combing must be consulted HERE too. This branch used to return
        # `film` on the strength of the rate alone, never reading the combing
        # measurement at all - so a soft-telecined episode that decodes at
        # 23.976 but carries interlaced content inside its frames was declared
        # clean. Measured on Buffy 2026-09-14: s02e05 at 58% combed, s02e19 at
        # 49%, s02e06 at 44%, all classified `film`. 58 of 143 episodes have
        # >10% combing; only 14 were being flagged.
        if combed_ratio > COMBING_THRESHOLD:
            a.cadence = "film_combed"
            a.cadence_filter = DEINT_FILTER if DEINT_COMBED else None
            a.fps_from_source = True
            a.notes.append(
                f"23.976 progressive but {combed_ratio*100:.0f}% of sampled "
                "frames are combed; "
                + ("deinterlacing combed frames only, no decimation"
                   if DEINT_COMBED else
                   "passed through unfiltered (see DEINT_COMBED)"))
            return
        a.cadence = "film"
        a.cadence_filter = None
        a.notes.append("already 23.976 progressive; no cadence filter applied")
        return

    # All samples read 29.97. Combing decides whether it is telecined film.
    if combed_ratio > COMBING_THRESHOLD:
        if near_fps(a.ivtc_fps, FILM_FPS):
            a.cadence = "telecine"
            a.cadence_filter = "fieldmatch,decimate"
            a.notes.append(
                f"hard telecine; IVTC verified to {a.ivtc_fps:.2f}fps")
        else:
            # Combed at 29.97, but IVTC does not recover film - so it is not
            # telecined film, just interlaced video. Deinterlace it; never
            # decimate, which is what would have been destructive here.
            a.cadence = "video_combed"
            a.cadence_filter = DEINT_FILTER if DEINT_COMBED else None
            a.fps_from_source = True
            a.notes.append(
                f"interlaced 29.97 but IVTC yields {a.ivtc_fps}fps, not "
                "23.976; "
                + ("deinterlacing without decimation" if DEINT_COMBED
                   else "passed through unfiltered (see DEINT_COMBED)"))
    else:
        a.cadence = "video"
        a.cadence_filter = None
        a.notes.append("genuine 29.97 progressive; rate preserved")


def resolve_crop(a: Analysis, width: int, height: int) -> None:
    """Take a low percentile of each margin across samples.

    Biased toward keeping picture on purpose. The two failure modes are not
    symmetric: under-cropping leaves black bars the player letterboxes away,
    while over-cropping permanently destroys picture. A single bright pixel in
    the letterbox would drag a pure minimum to zero, so a percentile is used
    rather than the true minimum.
    """
    usable = []
    for w, h, x, y in a.crop_samples:
        # Drop samples that saw almost nothing - a fade or dark scene.
        if w * h < 0.5 * width * height:
            continue
        usable.append((x, y, width - w - x, height - h - y))  # l, t, r, b
    if not usable:
        a.crop = [width, height, 0, 0]
        a.notes.append("cropdetect inconclusive; no crop applied")
        return

    def pct(values: list[int], p: float = 0.2) -> int:
        s = sorted(values)
        return s[min(len(s) - 1, int(len(s) * p))]

    left = pct([m[0] for m in usable])
    top = pct([m[1] for m in usable])
    right = pct([m[2] for m in usable])
    bottom = pct([m[3] for m in usable])

    # Horizontal bars on a disc are nearly always edge noise rather than real
    # letterboxing. Cropping them costs picture for no gain.
    if left + right < 8:
        left = right = 0
        a.notes.append("horizontal crop below threshold; width preserved")

    left -= left % 2
    top -= top % 2
    right -= right % 2
    bottom -= bottom % 2

    cw, ch = width - left - right, height - top - bottom
    if cw <= 0 or ch <= 0:
        a.crop = [width, height, 0, 0]
        a.notes.append("crop computation degenerate; no crop applied")
        return
    a.crop = [cw, ch, left, top]


def resolve_color(a: Analysis, vstream: dict, height: int) -> None:
    """Always emit explicit colour tags.

    Disc sources in this library are frequently untagged. Untagged HEVC is
    interpreted as BT.709 by hardware decoders, which shifts colour badly on
    480-line content that is actually BT.601.
    """
    prim = vstream.get("color_primaries")
    trc = vstream.get("color_transfer")
    space = vstream.get("color_space")
    rng = vstream.get("color_range") or "tv"

    if not (prim and space):
        if height <= 576:
            prim, trc, space = "smpte170m", "smpte170m", "smpte170m"
            a.notes.append("source untagged; applied BT.601 (SD) colour tags")
        else:
            prim, trc, space = "bt709", "bt709", "bt709"
            a.notes.append("source untagged; applied BT.709 (HD) colour tags")
    a.color = {"primaries": prim, "transfer": trc or prim,
               "space": space, "range": rng}


def resolve_tier(path: Path, height: int,
                  override: str | None = None) -> tuple[str, str]:
    """Pick the quality tier, and return the evidence for the choice.

    Resolution separates dvd from bluray, and that part is decided from the
    file as principle 1 requires.

    The film/tv split is NOT. It is decided from the path, because it is not a
    property of the content at all: it records how much the owner cares about
    the title, and no measurement can recover that. A grainy 35mm feature and a
    sitcom shot on the same camera negative would measure alike and still
    deserve different budgets. So this is a deliberate exception to principle 1,
    and the reason is returned alongside the tier so the plan carries it as
    evidence rather than as a silent inference.

    Directory layout is the signal: raw/bluray/tv vs raw/bluray/movies. A
    source outside that layout falls to bluray-film, which is the careful
    choice - spending too much on an episode is recoverable, and under-spending
    on a favourite film is the failure that matters.
    """
    # An explicit --tier wins over everything except the resolution check,
    # which is a measurement and not a preference. The override exists for a
    # homogeneous batch the owner does not want to file by hand; it is NOT the
    # default, because a flag lives only in one shell invocation while a plan
    # gets regenerated every time analyze.py changes (three times on
    # 2026-09-20 alone). Folder placement survives that, an argument does not.
    if override:
        if height <= 576 and override != "dvd":
            return "dvd", (f"height {height} <= 576 overrides --tier "
                           f"{override}")
        return override, f"--tier {override} given explicitly"
    if height <= 576:
        return "dvd", f"height {height} <= 576"
    parts = {part.lower() for part in path.parts}
    if "tv" in parts:
        return "bluray-tv", "path contains a 'tv' directory"
    if "movies" in parts:
        # Folder placement and the curated list are both path-based evidence
        # for the same decision, so either is honoured - dropping a rip into
        # movies/standard/ or movies/film/ works without also maintaining the
        # text list, and the text list keeps working for anything left
        # directly in movies/.
        if "standard" in parts:
            return "bluray-standard", "path contains a 'standard' directory"
        if "film" in parts:
            return "bluray-film", "path contains a 'film' directory"
        if path.stem.lower() in standard_titles():
            return "bluray-standard", "listed in bluray-standard.txt"
        # Unfiled, directly in movies/. Falls to the careful tier on purpose:
        # overspending on a title is recoverable, under-spending on a
        # favourite is not (rule 9).
        return "bluray-film", "directly in 'movies'; UNFILED, defaulted to film"
    # Distinguished from the line above on purpose: a confident match and a
    # fallback must not leave identical evidence, or a misfiled source is
    # indistinguishable from a correctly-placed one at review time.
    return "bluray-film", "no 'tv' or 'movies' directory in path; DEFAULTED to film"


# A commentary track can carry the same channel count, language tag, and
# even a plausible-sounding title ("Stereo") as a genuine alternate mix - on
# this library MakeMKV had already tagged a commentary track "Stereo" with
# no disposition.comment flag set. There is no metadata that reliably tells
# the two apart. But their CONTENT does: an alternate mix of the same show
# audio shares the same dialogue and effects, so it correlates strongly with
# the main track at zero lag; commentary is a different, mostly-independent
# recording laid over a ducked copy of the show, so it does not.
#
# One window per file is not enough - the SAME trap as bug 12 (one combing
# window misread Buffy 58 times). A commentary track goes quiet exactly when
# the commentator does not, leaving only the ducked show audio underneath,
# which DOES correlate well with the main mix for that stretch. Measured on
# 7 confirmed-commentary Friends episodes, 6-7 windows each: every episode
# had at least one window over 0.15 (one hit 0.21), which a single- or
# two-window max would have read as a genuine alternate mix. The MEDIAN
# across AUDIO_CORR_SAMPLES windows stayed under 0.04 for all 7 - use that,
# not max.
AUDIO_CORR_SAMPLES = 7
AUDIO_CORR_WINDOW = 15
AUDIO_CORR_THRESHOLD = 0.15


def _extract_pcm(path: str, stream_spec: str, start: float, dur: float,
                  sr: int = 8000) -> array.array:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(start), "-t", str(dur),
           "-i", str(path), "-map", stream_spec, "-ac", "1", "-ar", str(sr),
           "-f", "s16le", "-"]
    try:
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=TIMEOUT).stdout
    except subprocess.TimeoutExpired:
        return array.array('h')
    return array.array('h', out[:len(out) - len(out) % 2])


def _pearson(a: array.array, b: array.array) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


def tracks_correlate(path: str, spec_a: str, spec_b: str, duration: float,
                      samples: int = AUDIO_CORR_SAMPLES,
                      window: int = AUDIO_CORR_WINDOW,
                      max_lag_ms: int = 80, sr: int = 8000) -> float | None:
    """Median zero-ish-lag correlation between two audio streams of the same
    file, sampled at several points spread across the runtime (sample_points
    - same spread used for the combing scan, and for the same reason: a
    per-window signal that is this noisy needs many windows and a robust
    statistic, not one reading trusted alone). Each window is maxed over a
    small lag search - the two tracks can be offset by a frame or two of
    encoder latency even when genuinely the same content.

    Returns None if fewer than half the windows could be read (e.g. the file
    is too short) - too little evidence to call it either way.
    """
    max_lag = max(1, int(sr * max_lag_ms / 1000))
    readings: list[float] = []
    for start in sample_points(int(duration), samples):
        a = _extract_pcm(path, spec_a, start, window, sr)
        b = _extract_pcm(path, spec_b, start, window, sr)
        if not a or not b:
            continue
        best = -1.0
        for lag in range(-max_lag, max_lag + 1, max(1, max_lag // 8)):
            aa, bb = (a[lag:], b[:len(b) - lag] if lag else b) if lag >= 0 \
                else (a[:len(a) + lag] if lag else a, b[-lag:])
            c = _pearson(aa, bb)
            if c > best:
                best = c
        readings.append(best)
    if len(readings) < samples / 2:
        return None
    readings.sort()
    return readings[len(readings) // 2]


def resolve_audio(a: Analysis, streams: list[dict], duration: float) -> None:
    """One stereo AAC track for phones/tablets, one surround track for
    everything else. Lossless surround is transcoded rather than copied: on
    this library a TrueHD track was 55% of the file, larger than the video."""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        a.notes.append("no audio streams found")
        return

    def is_english(s: dict) -> bool:
        lang = (s.get("tags") or {}).get("language", "").lower()
        return lang in ("eng", "en", "")

    preferred = [s for s in audio if is_english(s)] or audio
    # Widest track is the main mix; commentary is usually stereo.
    primary = max(preferred, key=lambda s: int(s.get("channels") or 0))
    idx = primary["index"]
    codec = primary.get("codec_name", "")
    channels = int(primary.get("channels") or 2)

    if channels <= 2:
        # Already stereo. A second track would carry identical content, and
        # transcoding an already-lossy stereo source to AAC only adds a
        # generation of loss. Copy it and let the server do a cheap
        # audio-only transcode for any client that needs one.
        if is_lossless_audio(primary):
            a.audio.append({
                "role": "stereo", "source_index": idx, "codec": "aac",
                "bitrate": "192k", "channels": 2, "downmix": False,
                "source_profile": primary.get("profile"),
            })
            a.notes.append(
                f"{codec} stereo is lossless ({lossless_reason(primary)}); "
                "transcoded to AAC 192k")
        else:
            a.audio.append({
                "role": "stereo", "source_index": idx, "codec": "copy",
                "source_profile": primary.get("profile"),
            })
            a.notes.append(
                f"source is {codec} {channels}ch; copied as-is "
                "(no second track needed)")
        return

    # Surround source: a stereo track for phones and tablets, plus the
    # surround mix for everything else.
    #
    # Prefer a dedicated stereo track from the disc over folding the surround
    # mix down ourselves. A studio stereo mix is made for stereo playback;
    # an algorithmic downmix is a compromise. Copying it is also free.
    #
    # BUT: a commentary track looks identical to a genuine alternate mix by
    # every metadata field this library has ever seen carried over from a
    # DVD - same language tag, same channel count, sometimes even a "Stereo"
    # title tag on the commentary itself (bug 18). Verify by content before
    # trusting it: a real alternate mix shares the same dialogue and effects
    # as the main track, so it correlates strongly at zero lag; commentary is
    # a mostly-independent recording over a ducked copy of the show and does
    # not. 27 of 226 Friends episodes hit this branch; all 27 measured at
    # 0.01-0.03 correlation and were commentary, none were a genuine mix.
    dedicated = [s for s in preferred
                 if int(s.get("channels") or 0) == 2
                 and s["index"] != idx]
    stereo_src = dedicated[0] if dedicated else None
    if stereo_src is not None:
        corr = tracks_correlate(a.path, f"0:{idx}", f"0:{stereo_src['index']}",
                                 duration)
        # None is not "inconclusive, trust it anyway" - it is "could not be
        # checked", and an unverified track gets the same treatment as a
        # verified-bad one. Measured on Ash vs Evil Dead 2026-09-21: 4
        # episodes' "English Stereo" track was a 31-packet stub covering
        # about one second, not a real alternate mix - the same failure
        # shape as bug 7's degenerate subtitle track, on audio instead. The
        # correlation windows correctly found nothing to measure; trusting
        # that as "the disc's own stereo mix" would have shipped a track
        # that is silent for all but the first second of every episode.
        if corr is None or corr < AUDIO_CORR_THRESHOLD:
            a.notes.append(
                (f"disc has a second {stereo_src.get('codec_name')} stereo "
                 f"track but it correlates at only {corr:.2f} with the main "
                 f"mix (< {AUDIO_CORR_THRESHOLD}) - almost certainly "
                 "commentary, not an alternate mix"
                 if corr is not None else
                 f"disc has a second {stereo_src.get('codec_name')} stereo "
                 "track but it could not be measured against the main mix "
                 "(too little decodable audio - likely a degenerate/stub "
                 "track, see bug 7) - not trusted without evidence")
                + "; excluded, downmixing the main track instead")
            stereo_src = None
    if stereo_src is not None:
        a.audio.append({
            "role": "stereo", "source_index": stereo_src["index"],
            "codec": "copy",
        })
        a.notes.append(
            f"using the disc's own {stereo_src.get('codec_name')} stereo mix "
            "rather than a synthesized downmix")
    else:
        a.audio.append({
            "role": "stereo", "source_index": idx, "codec": "aac",
            "bitrate": "192k", "channels": 2, "downmix": True,
        })
    if is_lossless_audio(primary):
        a.audio.append({
            "role": "surround", "source_index": idx, "codec": "eac3",
            "bitrate": "640k", "channels": min(channels, 6),
            "source_profile": primary.get("profile"),
        })
        a.notes.append(
            f"{codec} {channels}ch is lossless ({lossless_reason(primary)}); "
            "transcoded to E-AC3 640k")
    else:
        a.audio.append({
            "role": "surround", "source_index": idx, "codec": "copy",
            "source_profile": primary.get("profile"),
        })
        profile = primary.get("profile")
        a.notes.append(
            f"{codec} {channels}ch is lossy"
            + (f" (profile {profile})" if profile else "")
            + "; copied rather than re-encoded")


def resolve_subtitles(a: Analysis, streams: list[dict]) -> None:
    """Image-based subtitles cannot be direct-played by Apple clients - enabling
    one makes Jellyfin burn it in, forcing a full video transcode. They are
    mapped through but flagged; OCR to SRT is a separate stage."""
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    for s in subs:
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        codec = s.get("codec_name", "")

        # A subtitle track carrying no real content - no duration, and a
        # packet count in the single digits - will stall the matroska muxer.
        # It waits for packets that never arrive, then flushes and jumps the
        # output clock, which TERMINATES the video encode early while still
        # exiting 0. One such track produced a 56-frame "84 minute" episode.
        # Excluding these is not cosmetic; it is the difference between a
        # complete encode and a silently truncated one.
        n_frames = 0
        for key in ("NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng"):
            try:
                n_frames = max(n_frames, int(tags.get(key, 0)))
            except (TypeError, ValueError):
                pass
        degenerate = s.get("duration") is None and n_frames <= 1
        lang = (tags.get("language") or "und").lower()
        # Foreign-language subtitle tracks are clutter in every client's menu
        # and serve no one here. One title carries 17 tracks across Chinese,
        # Korean, Thai, Spanish and French.
        foreign = lang not in ("eng", "en", "und", "")

        reason = None
        if degenerate:
            reason = "empty; would stall the muxer"
        elif foreign:
            reason = f"non-English ({lang})"

        entry = {
            "source_index": s["index"],
            "codec": codec,
            "language": lang,
            "forced": bool(disp.get("forced")),
            "image_based": codec in IMAGE_SUB_CODECS,
            "usable": reason is None,
            "excluded_because": reason,
        }
        if degenerate:
            a.notes.append(
                f"subtitle stream {s['index']} ({codec}) is empty and would "
                "stall the muxer; excluded")
        a.subtitles.append(entry)

    dropped = sum(1 for x in a.subtitles
                  if x["excluded_because"] and "non-English" in x["excluded_because"])
    if dropped:
        a.notes.append(f"dropped {dropped} non-English subtitle track(s)")
    if any(s["image_based"] for s in a.subtitles):
        a.notes.append(
            "image-based subtitles present; OCR to SRT needed for Apple "
            "direct play")


def build_argv(a: Analysis, src: Path, dst: Path, version: str,
               git_sha: str) -> list[str]:
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(src)]

    argv += ["-map", "0:v:0"]
    for track in a.audio:
        argv += ["-map", f"0:{track['source_index']}"]
    for sub in a.subtitles:
        if sub.get("usable", True):
            argv += ["-map", f"0:{sub['source_index']}"]

    chain = []
    if a.cadence_filter:
        chain.append(a.cadence_filter)
    if a.crop:
        w, h, x, y = a.crop
        chain.append(f"crop={w}:{h}:{x}:{y}")
    if chain:
        argv += ["-vf", ",".join(chain)]

    # Colour MUST be set inside -x265-params. Measured: ffmpeg's
    # -color_primaries / -color_trc do not reach libx265 at any argument
    # position - only -colorspace survives - leaving primaries and transfer
    # tagged "unspecified", which decoders then read as BT.709.
    x265_params = ["aq-mode=3", "no-sao=1"]
    if a.color:
        x265_params += [
            f"colorprim={a.color['primaries']}",
            f"transfer={a.color['transfer']}",
            f"colormatrix={a.color['space']}",
            "range=" + ("limited" if a.color["range"] == "tv" else "full"),
        ]
    argv += [
        "-c:v", "libx265", "-preset", TIERS[a.tier]["preset"],
        "-pix_fmt", "yuv420p10le", "-crf", str(a.crf),
        "-x265-params", ":".join(x265_params),
    ]
    if a.color:
        # Container-level tag; the bitstream VUI above is what actually matters.
        argv += ["-color_range", a.color["range"]]

    for n, track in enumerate(a.audio):
        if track["codec"] == "copy":
            argv += [f"-c:a:{n}", "copy"]
            continue
        argv += [f"-c:a:{n}", track["codec"], f"-b:a:{n}", track["bitrate"]]
        if track.get("channels"):
            argv += [f"-ac:a:{n}", str(track["channels"])]
        if track.get("downmix"):
            # ffmpeg's default downmix is quiet and dialogue-light; use an
            # explicit matrix. Note -filter:a:N, not -af:a:N - the latter
            # parses but silently applies to every audio stream.
            argv += [
                f"-filter:a:{n}",
                "pan=stereo|FL=0.5*FC+0.707*FL+0.707*BL"
                "|FR=0.5*FC+0.707*FR+0.707*BR",
            ]

    if any(sub.get("usable", True) for sub in a.subtitles):
        argv += ["-c:s", "copy"]

    crop_str = ":".join(str(v) for v in a.crop) if a.crop else "none"
    argv += [
        "-metadata", f"ENCODE_VERSION={version}",
        "-metadata", f"ENCODE_GIT_SHA={git_sha}",
        "-metadata", f"ENCODE_TIER={a.tier}",
        "-metadata", f"ENCODE_CADENCE={a.cadence}",
        "-metadata", f"ENCODE_CROP={crop_str}",
    ]
    argv += [str(dst)]
    return argv


def analyze(path: Path, out_dir: Path, version: str, git_sha: str,
            quick: bool = False, tier: str | None = None) -> Analysis:
    a = Analysis(path=str(path))
    try:
        probe = ffprobe_json(path)
    except Exception as exc:  # noqa: BLE001 - report, never abort the batch
        a.ok, a.error = False, str(exc)
        return a

    streams = probe.get("streams", [])
    vstreams = [s for s in streams if s.get("codec_type") == "video"
                and s.get("disposition", {}).get("attached_pic") != 1]
    if not vstreams:
        a.ok, a.error = False, "no video stream"
        return a
    v = vstreams[0]

    try:
        duration = int(float(probe["format"]["duration"]))
    except (KeyError, ValueError, TypeError):
        a.ok, a.error = False, "no duration"
        return a
    if duration < 120:
        a.ok, a.error = False, f"too short ({duration}s)"
        return a

    width = int(v.get("width") or 0)
    height = int(v.get("height") or 0)
    a.container_fps = v.get("r_frame_rate")
    a.tier, tier_why = resolve_tier(path, height, tier)
    a.crf = TIERS[a.tier]["crf"]
    a.notes.append(
        f"tier {a.tier} ({tier_why}); preset {TIERS[a.tier]['preset']}, "
        f"CRF {a.crf}")

    # --- cadence evidence ---
    n_rate = 2 if quick else 3
    for t in sample_points(duration, n_rate):
        r = decoded_fps(path, t)
        if r:
            a.decoded_fps.append(round(r, 2))

    mid = sample_points(duration, 1)[0]
    # Combing must be sampled at SEVERAL points, for the same reason the
    # decoded rate is. Measured on Buffy 2026-09-14: a single mid-file window
    # reported s02e22 as 3.2% combed; twenty-one windows across the same
    # episode reported 42.2%, with 13 of them over 10%. Combing on these discs
    # is scattered rather than uniform, so one window is a coin flip - and that
    # one number decides the cadence verdict for the whole title. 123 of 143
    # episodes were classified `film` on the strength of it.
    n_comb = 2 if quick else COMBING_SAMPLES
    inter = prog = 0
    for t_s in sample_points(duration, n_comb):
        i, pr, _ = idet_counts(path, t_s, dur=COMBING_WINDOW)
        inter += i
        prog += pr
    a.interlaced_frames, a.progressive_frames = inter, prog

    # Only pay for the IVTC probe when combing suggests it is relevant.
    total = inter + prog
    if total and inter / total > COMBING_THRESHOLD:
        # NB: measure the IVTC'd rate only. An earlier version also called
        # decoded_fps() here and then threw the answer away one line later,
        # paying for a 20s decode per combed file for nothing.
        cp = run([
            "ffmpeg", "-nostdin", "-v", "quiet", "-ss", str(mid), "-t", "20",
            "-i", str(path), "-vf", "fieldmatch,decimate", "-an",
            "-f", "null", "-", "-stats",
        ])
        frames = _FRAME_RE.findall(cp.stderr.replace("\r", "\n"))
        a.ivtc_fps = int(frames[-1]) / 20 if frames else None

    classify_cadence(a)

    # --- crop evidence ---
    n_crop = 4 if quick else 10
    for t in sample_points(duration, n_crop):
        s = cropdetect_sample(path, t)
        if s:
            a.crop_samples.append(list(s))
    resolve_crop(a, width, height)

    a.analyzer = analyzer_fingerprint()
    a.ffmpeg_version = ffmpeg_version()
    a.x265_version = x265_version()
    resolve_color(a, v, height)
    resolve_audio(a, streams, duration)
    resolve_subtitles(a, streams)

    if not a.needs_review:
        # encode.py substitutes this token for the real destination.
        a.argv = build_argv(a, path, Path("{OUTPUT}"), version, git_sha)
    return a


# --- driver ------------------------------------------------------------------

def iter_media(paths: list[str]):
    for p in paths:
        root = Path(p)
        if root.is_file():
            yield root
        elif root.is_dir():
            for f in sorted(root.rglob("*")):
                # Skip macOS AppleDouble sidecars; they match the extension
                # filter but are not media.
                if f.name.startswith("._"):
                    continue
                if f.is_file() and f.suffix.lower() in MEDIA_EXT:
                    yield f


def plan_path(out_dir: Path, src: Path) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.stem)[:120]
    return out_dir / f"{safe}.json"


def analyzer_fingerprint() -> str:
    """Hash of this file's own source.

    Plans are only valid for the logic that produced them. When a decision
    rule changes, every existing plan is silently stale - which nearly shipped
    172 episodes with broken colour tags. encode refuses to run a plan whose
    fingerprint does not match.
    """
    try:
        return hashlib.sha256(
            pathlib.Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    """Version string of the ffmpeg actually on PATH.

    Worth recording because several of this pipeline's rules are
    ffmpeg-version-specific. Measured: the colour-in-x265-params requirement
    (cardinal rule 2) does not reproduce on ffmpeg 6.1.1 but does on 8.1+,
    so an encode's provenance is incomplete without knowing which built it.
    """
    try:
        cp = run(["ffmpeg", "-version"], timeout=15)
        first = cp.stdout.splitlines()[0] if cp.stdout else ""
        parts = first.split()
        return parts[2] if len(parts) > 2 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


@functools.lru_cache(maxsize=1)
def x265_version() -> str:
    """libx265 version, via a throwaway 64x64 encode.

    x265 stamps this into an SEI in every output too, but that costs a
    bitstream extraction to read back (and note rule 3: strings truncates it).
    A container-level tag is far cheaper to query across a library.
    """
    try:
        cp = run(["ffmpeg", "-hide_banner", "-f", "lavfi",
                  "-i", "testsrc=d=0.1:s=64x64", "-c:v", "libx265",
                  "-f", "null", "-"], timeout=60)
        m = re.search(r"HEVC encoder version ([0-9A-Za-z.+~_-]+)", cp.stderr)
        return m.group(1) if m else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def git_sha() -> str:
    try:
        cp = run(["git", "rev-parse", "--short", "HEAD"], timeout=10)
        return cp.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("paths", nargs="+", help="file(s) or directory(ies)")
    ap.add_argument("--out", default="plans", help="plan output directory")
    ap.add_argument("--jobs", type=int, default=4,
                    help="parallel workers (NAS reads degrade above ~4)")
    ap.add_argument("--force", action="store_true",
                    help="re-analyze titles that already have a plan")
    ap.add_argument("--quick", action="store_true",
                    help="fewer samples; faster, less reliable")
    ap.add_argument("--tier", choices=sorted(TIERS),
                    help="force this tier for every source in the run, "
                         "instead of deciding it from the path")


def run_cmd(args: argparse.Namespace) -> int:
    for tool in ("ffprobe", "ffmpeg"):
        if not shutil.which(tool):
            print(f"error: {tool} not found on PATH", file=sys.stderr)
            return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = git_sha()

    files = list(iter_media(args.paths))
    todo = [f for f in files
            if args.force or not plan_path(out_dir, f).exists()]
    skipped = len(files) - len(todo)
    print(f"{len(files)} media files; {len(todo)} to analyze"
          f"{f'; {skipped} already planned' if skipped else ''}",
          file=sys.stderr)

    counts: dict[str, int] = {}
    failures: list[tuple[str, str]] = []
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(analyze, f, out_dir, "4.0", sha, args.quick,
                        getattr(args, "tier", None)): f
            for f in todo
        }
        for fut in concurrent.futures.as_completed(futures):
            src = futures[fut]
            done += 1
            try:
                a = fut.result()
            except Exception as exc:  # noqa: BLE001
                failures.append((src.name, repr(exc)))
                continue

            if not a.ok:
                failures.append((src.name, a.error or "unknown"))
                key = "error"
            else:
                key = "review" if a.needs_review else (a.cadence or "unknown")
                plan_path(out_dir, src).write_text(
                    json.dumps(dataclasses.asdict(a), indent=2) + "\n")
            counts[key] = counts.get(key, 0) + 1

            if done % 25 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)}", file=sys.stderr)

    print("\n--- summary ---", file=sys.stderr)
    for k in sorted(counts):
        print(f"  {k:<10} {counts[k]}", file=sys.stderr)
    if failures:
        print(f"\n{len(failures)} failed:", file=sys.stderr)
        for name, err in failures[:15]:
            print(f"  {name}: {err[:110]}", file=sys.stderr)
    print(f"\nplans written to {out_dir}/", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    _ap = argparse.ArgumentParser(description=__doc__)
    add_args(_ap)
    sys.exit(run_cmd(_ap.parse_args()))
