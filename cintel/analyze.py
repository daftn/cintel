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
import concurrent.futures
import dataclasses
import hashlib
import json
import re
import shutil
import subprocess
import sys
import pathlib
from pathlib import Path

# --- cadence constants -------------------------------------------------------
FILM_FPS = 24000 / 1001      # 23.976
VIDEO_FPS = 30000 / 1001     # 29.97
FPS_TOL = 0.5                # a rate must land within this of a known rate

MEDIA_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".webm", ".m2ts", ".ts"}
IMAGE_SUB_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub"}
LOSSLESS_AUDIO = {"truehd", "dts", "flac", "mlp", "pcm_s16le", "pcm_s24le"}

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
CRF_DVD = 20
CRF_BLURAY = 21

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
    crop: list[int] | None = None
    color: dict | None = None
    audio: list[dict] = dataclasses.field(default_factory=list)
    subtitles: list[dict] = dataclasses.field(default_factory=list)
    crf: int | None = None
    needs_review: bool = False
    review_reason: str | None = None
    notes: list[str] = dataclasses.field(default_factory=list)
    analyzer: str | None = None
    argv: list[str] | None = None


def sample_points(duration: int, count: int) -> list[int]:
    """Evenly spaced interior points; avoids credits at either end."""
    return [max(1, int(duration * (i + 1) / (count + 1))) for i in range(count)]


def classify_cadence(a: Analysis) -> None:
    """Decide the cadence filter from decoded rates and combing.

    Four outcomes:
      film      - already 23.976. No filter. (Most DVDs: the 29.97 in the
                  container is soft-telecine display rate, not coded frames.)
      telecine  - decodes 29.97 WITH combing, and IVTC recovers ~23.976.
      video     - decodes 29.97 with no combing. Genuine 29.97. No filter.
      review    - rates disagree between sample points, or sit between the
                  two valid rates. Mixed cadence; never guess.
    """
    rates = [r for r in a.decoded_fps if r]
    if not rates:
        a.needs_review, a.review_reason = True, "could not measure decoded rate"
        a.cadence = "review"
        return

    film = [near_fps(r, FILM_FPS) for r in rates]
    video = [near_fps(r, VIDEO_FPS) for r in rates]

    # Any rate that matches neither known rate means the window straddled a
    # cadence change. Buffy season 2 is full of these.
    if not all(f or v for f, v in zip(film, video)):
        a.cadence = "review"
        a.needs_review = True
        a.review_reason = f"non-standard decoded rate(s): {rates}"
        return

    if any(film) and any(video):
        a.cadence = "review"
        a.needs_review = True
        a.review_reason = f"rate changes between sample points: {rates}"
        return

    if all(film):
        a.cadence = "film"
        a.cadence_filter = None
        a.notes.append("already 23.976 progressive; no cadence filter applied")
        return

    # All samples read 29.97. Combing decides whether it is telecined film.
    total = a.interlaced_frames + a.progressive_frames
    combed_ratio = a.interlaced_frames / total if total else 0.0
    if combed_ratio > 0.10:
        if near_fps(a.ivtc_fps, FILM_FPS):
            a.cadence = "telecine"
            a.cadence_filter = "fieldmatch,decimate"
            a.notes.append(
                f"hard telecine; IVTC verified to {a.ivtc_fps:.2f}fps")
        else:
            a.cadence = "review"
            a.needs_review = True
            a.review_reason = (
                f"interlaced but IVTC yields {a.ivtc_fps}fps, not 23.976")
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


def resolve_audio(a: Analysis, streams: list[dict]) -> None:
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
        if codec in LOSSLESS_AUDIO:
            a.audio.append({
                "role": "stereo", "source_index": idx, "codec": "aac",
                "bitrate": "192k", "channels": 2, "downmix": False,
            })
            a.notes.append(f"lossless {codec} stereo transcoded to AAC 192k")
        else:
            a.audio.append({
                "role": "stereo", "source_index": idx, "codec": "copy",
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
    dedicated = [s for s in preferred
                 if int(s.get("channels") or 0) == 2
                 and s["index"] != idx]
    if dedicated:
        stereo_src = dedicated[0]
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
    if codec in LOSSLESS_AUDIO:
        a.audio.append({
            "role": "surround", "source_index": idx, "codec": "eac3",
            "bitrate": "640k", "channels": min(channels, 6),
        })
        a.notes.append(f"{codec} {channels}ch transcoded to E-AC3 640k")
    else:
        a.audio.append({
            "role": "surround", "source_index": idx, "codec": "copy",
        })


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
        "-c:v", "libx265", "-preset", "medium" if a.tier == "dvd" else "slow",
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
            quick: bool = False) -> Analysis:
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
    a.tier = "dvd" if height <= 576 else "bluray"
    a.crf = CRF_DVD if a.tier == "dvd" else CRF_BLURAY

    # --- cadence evidence ---
    n_rate = 2 if quick else 3
    for t in sample_points(duration, n_rate):
        r = decoded_fps(path, t)
        if r:
            a.decoded_fps.append(round(r, 2))

    mid = sample_points(duration, 1)[0]
    inter, prog, _ = idet_counts(path, mid)
    a.interlaced_frames, a.progressive_frames = inter, prog

    # Only pay for the IVTC probe when combing suggests it is relevant.
    total = inter + prog
    if total and inter / total > 0.10:
        a.ivtc_fps = decoded_fps(path, mid)
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
    resolve_color(a, v, height)
    resolve_audio(a, streams)
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
            pool.submit(analyze, f, out_dir, "4.0", sha, args.quick): f
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
