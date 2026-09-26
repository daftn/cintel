"""
verify.py - check encoded output against the plan that produced it.

Every defect found in the previous library was silent: films with 3:2 pulldown
baked in, episodes carrying 30% duplicate frames, untagged colour. Nothing
announced itself, and a size-based sanity check passed all of it. This stage
exists to make that class of failure loud.

Checks, in rough order of how badly they bit us before:

  fps        Output framerate matches what the plan decided. Catches the entire
             pulldown class on its own.
  duplicates Near-duplicate frame ratio. Catches a cadence filter that ran but
             did not achieve anything, which a framerate check alone misses.
  duration   Within tolerance of the source. Catches truncation, which a size
             floor does not - a fragment of a feature can still be large.
  dimensions Match the planned crop exactly.
  colour     Tags present and matching the plan. Untagged HEVC is read as
             BT.709 and shifts colour on SD content.
  streams    Expected audio and subtitle track counts survived the mux.

VMAF is deliberately not run for SD content. The default model is trained at
1080p and returns invalid scores at 480p; --vmaf scales both inputs to 1080p
first, which is the documented workaround, but it is slow and off by default.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

FPS_TOL = 0.15        # absolute fps
# Looser than FPS_TOL: both sides are sampled 20s windows of a source whose
# rate genuinely varies, so the two measurements do not land on the same
# stretch of content. Wide enough to absorb that, far too tight to hide a
# dropped-frame error - decimation would show as roughly -4.8fps.
VARIABLE_FPS_TOL = 0.60
DURATION_TOL = 0.01   # fraction
DUPLICATE_TOL = 0.02             # output ratio that triggers a source comparison
DUPLICATE_INTRODUCED_TOL = 0.05  # how much more than the source is a failure
# Deinterlacing confounds this metric, so a plan carrying a deint filter is
# held to a much looser bound. mpdecimate counts NEAR-duplicates by a
# difference threshold, and bwdif's interpolated frames are softer, so they
# read as more similar to their neighbours whether or not any frame actually
# repeats. Measured on Buffy: +0.6% introduced on s02e05, but +17.8% on s03e01,
# from the same filter on the same kind of content - a spread that says the
# number is measuring softness as much as repetition. Deliberately not
# switched off altogether: at this bound a gross regression still fails, while
# the ordinary consequence of a repair the owner asked for does not.
DUPLICATE_INTRODUCED_TOL_DEINT = 0.25
SYNC_TOL = 0.100                 # seconds of A/V drift we are willing to introduce
SPAN_RATIO_TOL = 0.02            # video/audio span may differ by this fraction
SAMPLE_SECONDS = 30

_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def parse_remap(pairs: list[str]) -> list[tuple[str, str]]:
    """Parse OLD=NEW path-prefix rewrites.

    Plans record absolute source paths, so a plan written on a machine that
    mounts the NAS at /Volumes/nas is useless on one that mounts it at
    /mnt/nas. Rather than regenerate, rewrite the prefix.
    """
    out = []
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"--remap needs OLD=NEW, got {p!r}")
        old, new = p.split("=", 1)
        out.append((old.rstrip("/"), new.rstrip("/")))
    return out


def apply_remap(value: str, remap: list[tuple[str, str]]) -> str:
    for old, new in remap:
        if value.startswith(old):
            return new + value[len(old):]
    return value


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", timeout=timeout)


def probe(path: Path) -> dict:
    cp = run(["ffprobe", "-v", "error", "-print_format", "json",
              "-show_streams", "-show_format", str(path)])
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip()[:200])
    return json.loads(cp.stdout)


def decoded_fps(path: Path, start: int, dur: int = SAMPLE_SECONDS) -> float | None:
    cp = run(["ffmpeg", "-nostdin", "-v", "quiet", "-ss", str(start),
              "-t", str(dur), "-i", str(path), "-an", "-f", "null", "-",
              "-stats"])
    m = _FRAME_RE.findall(cp.stderr.replace("\r", "\n"))
    return int(m[-1]) / dur if m else None


def duplicate_ratio(path: Path, start: int,
                    dur: int = SAMPLE_SECONDS) -> float | None:
    base = run(["ffmpeg", "-nostdin", "-v", "quiet", "-ss", str(start),
                "-t", str(dur), "-i", str(path), "-an", "-f", "null", "-",
                "-stats"])
    dedup = run(["ffmpeg", "-nostdin", "-v", "quiet", "-ss", str(start),
                 "-t", str(dur), "-i", str(path), "-vf", "mpdecimate",
                 "-an", "-f", "null", "-", "-stats"])
    b = _FRAME_RE.findall(base.stderr.replace("\r", "\n"))
    d = _FRAME_RE.findall(dedup.stderr.replace("\r", "\n"))
    if not b or not d or int(b[-1]) == 0:
        return None
    return (int(b[-1]) - int(d[-1])) / int(b[-1])


TAIL_WINDOW = 30      # seconds before the end to start the tail probe
# Shorter than run()'s 300s default: these probes finish in seconds on any
# normal file, so a long wait means the seek is pathological (see _pts_bounds)
# and waiting longer will not help. Bounds a 143-title verify run.
PTS_PROBE_TIMEOUT = 120


def _pts_bounds(path: Path, stream: str) -> tuple[float | None, float | None]:
    """First and last presentation timestamp of a stream, cheaply.

    Reads a few packets at the head and a time-bounded window at the tail
    rather than demuxing the whole file. Packets arrive in decode order, so
    min/max is taken rather than first/last - with B-frames the largest PTS is
    not the last packet.

    The tail seek MUST be expressed in seconds, derived from the container
    duration. In ffprobe's -read_intervals syntax `%` is the START/END
    SEPARATOR, not a percent sign: an interval of "99%+#99999" does not mean
    "the last 1% of the file", it means "start at 99 SECONDS, then read 99999
    packets" - which stops wherever those packets run out. That was bug 11.
    It read the true end only by luck, on files short enough for 99999 packets
    to overshoot, and reported nonsense otherwise: on a 6600s DVD the video
    burst ended at 4270s and the audio at 3299s, which the caller then
    subtracted into -970 SECONDS of invented drift. Short-framed codecs make
    it worse - DTS packets are 10.67ms, so 99999 of them span only 1067s.
    """
    def vals(cp: subprocess.CompletedProcess) -> list[float]:
        out = []
        for line in cp.stdout.splitlines():
            try:
                out.append(float(line.strip().rstrip(",")))
            except ValueError:
                pass
        return out

    try:
        duration = float(probe(path)["format"]["duration"])
    except (KeyError, ValueError, RuntimeError):
        return (None, None)

    # A timeout here must DEGRADE, not raise. Seeking to an audio packet late
    # in a very large MKV can take minutes, because Matroska cue points index
    # the video track and little else - measured on a 62GB 4h13m concatenation,
    # where video seeks returned instantly and audio seeks exceeded 300s. If
    # that propagated, verify would report a perfectly good encode as failed,
    # which is cardinal rule 10's exact failure. Returning None instead makes
    # the caller say "could not compare", which is the truth.
    def probe_pts(interval: str) -> list[float]:
        try:
            return vals(run([
                "ffprobe", "-v", "error", "-select_streams", stream,
                "-read_intervals", interval, "-show_entries", "packet=pts_time",
                "-of", "csv=p=0", str(path)], timeout=PTS_PROBE_TIMEOUT))
        except subprocess.TimeoutExpired:
            return []

    h = probe_pts("%+#40")
    # "<seconds>%+#N" - from that point, capped BOTH ways. The time bound stops
    # a short-framed codec running out of packets before the end (that was bug
    # 11); the packet cap stops an enormous file reading to EOF. 40k packets is
    # ~7 minutes of 96ms DTS frames and ~27 minutes of video, so on any sane
    # stream the window closes on TAIL_WINDOW long before the cap bites.
    t = probe_pts(f"{max(0.0, duration - TAIL_WINDOW):.3f}%+#40000")
    return (min(h) if h else None, max(t) if t else None)


def audio_position(streams: list[dict], source_index: int) -> int | None:
    """Position of an absolute stream index among a file's own audio streams.

    Needed to build an ffprobe stream specifier ("a:N") that names the SAME
    physical track in two different files. A plan can reorder audio - Friends
    s10e17e18 promotes a secondary 2-channel track (source_index 2) to be the
    output's first audio stream, while in the source that same track is
    second (a:1). Comparing "a:0 of output" to "a:0 of source" then silently
    compares two different tracks: the promoted secondary track against the
    source's main track, which was never desynced. Matching by source_index
    instead of container position compares each track to itself.
    """
    audio = sorted((s for s in streams if s.get("codec_type") == "audio"),
                   key=lambda s: s.get("index", 0))
    for i, s in enumerate(audio):
        if s.get("index") == source_index:
            return i
    return None


def av_drift(path: Path, v: str = "v:0", a: str = "a:0") -> float | None:
    """How far audio and video pull apart across a file, in seconds.

    Computed as (audio-video skew at the end) minus (the same skew at the
    start), so a constant offset cancels and only ACCUMULATING desync is
    reported. That is the distinction that matters: discs are authored with
    small fixed offsets and MakeMKV preserves them faithfully, whereas drift
    that grows over the runtime means a frame-rate or cadence assumption is
    wrong somewhere.

    `v`/`a` let the caller name a specific stream rather than assume "a:0" -
    see audio_position and the caller in verify_one for why that assumption
    is not safe in general.

    Only valid on a file that was NOT produced with an input seek - see
    av_span_ratio for why, and use that one for samples.

    Returns None when there is no audio stream, or timestamps are unreadable.
    """
    v_start, v_end = _pts_bounds(path, v)
    a_start, a_end = _pts_bounds(path, a)
    if None in (v_start, v_end, a_start, a_end):
        return None
    return (a_end - v_end) - (a_start - v_start)


def av_span_ratio(path: Path, v: str = "v:0", a: str = "a:0") -> float | None:
    """Video timespan divided by audio timespan. 1.0 means they cover the
    same stretch of time; below 1.0 the video is short against its audio.

    Needed because av_drift is not usable on a sampled encode. An input seek
    lands on the first decodable frame after the seek point, so the video's
    first PTS can sit several hundred ms after the audio's - measured at
    416ms on a real test clip. av_drift reads that head offset as drift and
    fails a perfectly good sample. This is the same trap as bug 8.

    A ratio ignores the head offset entirely and measures the thing that
    actually goes wrong: video running short against audio that was copied
    untouched. Measured on a deliberately mis-encoded clip - decimate applied
    to already-progressive 23.976 content - the ratio was 0.797, i.e. exactly
    the 4/5 that dropping one frame in five produces, against 0.996 for the
    correct encode of the same source.
    """
    v_start, v_end = _pts_bounds(path, v)
    a_start, a_end = _pts_bounds(path, a)
    if None in (v_start, v_end, a_start, a_end):
        return None
    a_span = a_end - a_start
    if a_span <= 0:
        return None
    return (v_end - v_start) / a_span


def expected_fps(plan: dict) -> float | None:
    """What the plan's cadence decision should produce.

    Returns None where no constant applies: a plan carrying fps_from_source
    describes a source whose own rate is not uniform, so the only honest
    reference is the source itself - see the framerate check in verify_one.
    """
    if plan.get("fps_from_source"):
        return None
    cadence = plan.get("cadence")
    if cadence in ("film", "telecine"):
        return 24000 / 1001
    if cadence == "video":
        return 30000 / 1001
    return None  # review / unknown: nothing to assert


def verify_one(plan_file: Path, out_root: Path, flatten: bool,
               vmaf: bool, sample: bool = False,
               remap: list[tuple[str, str]] | None = None
               ) -> tuple[bool, list[str]]:
    plan = json.loads(plan_file.read_text())
    problems: list[str] = []

    src = Path(apply_remap(plan["path"], remap or []))
    name = src.stem + ".mkv"
    dst = out_root / name if flatten else out_root / src.parent.name / name
    if not dst.exists():
        return (False, [f"output missing: {dst}"])

    try:
        p_out = probe(dst)
    except Exception as exc:  # noqa: BLE001
        return (False, [f"output unreadable: {exc}"])

    vout = next((s for s in p_out["streams"]
                 if s.get("codec_type") == "video"), None)
    if not vout:
        return (False, ["output has no video stream"])

    mid = 600
    try:
        dur_out = float(p_out["format"]["duration"])
        # Start the measurement window so it fits inside the video with margin.
        # Container duration can exceed video duration - a copied audio track
        # often runs a second or two past the last frame - and a window that
        # overruns the end silently undercounts frames, which reads as a
        # framerate failure on an otherwise perfect encode.
        mid = max(0, min(int(dur_out / 2),
                         int(dur_out) - SAMPLE_SECONDS - 2))
    except (KeyError, ValueError):
        dur_out = None

    # A sampled encode covers a window from the middle of the source, so the
    # equivalent source timestamp is offset by where the sample began.
    try:
        sample_start = int((p_out["format"].get("tags") or {})
                           .get("ENCODE_SAMPLE_START", 0))
    except (TypeError, ValueError):
        sample_start = 0
    src_mid = mid + sample_start

    # --- duration -----------------------------------------------------------
    if sample:
        pass  # a deliberate short sample will never match the source
    elif src.exists() and dur_out:
        try:
            dur_src = float(probe(src)["format"]["duration"])
            drift = abs(dur_out - dur_src) / dur_src
            if drift > DURATION_TOL:
                problems.append(
                    f"duration drift {drift*100:.1f}% "
                    f"(source {dur_src:.0f}s, output {dur_out:.0f}s)")
        except Exception:  # noqa: BLE001
            problems.append("could not compare duration to source")

    # --- framerate ----------------------------------------------------------
    want = expected_fps(plan)
    got = decoded_fps(dst, mid)
    if want and got is None:
        problems.append("could not measure output framerate")
    elif want and abs(got - want) > FPS_TOL:
        # Before failing, ask whether the SOURCE holds that constant either.
        # Measured on Buffy 2026-09-15: five `film` episodes decode at
        # 24.0-24.9 depending on the window, so asserting 23.976 failed
        # faithful, unfiltered encodes. What matters is whether we CHANGED the
        # rate - a wrong cadence filter shows as ~-4.8fps against the source,
        # which this still catches. Same reasoning as the duplicate and drift
        # checks, and the same trap as cardinal rule 10.
        src_fps = decoded_fps(src, src_mid) if src.exists() else None
        if src_fps is not None and abs(got - src_fps) <= VARIABLE_FPS_TOL:
            pass  # output tracks its source; the plan's constant is what is wrong
        else:
            problems.append(
                f"framerate {got:.2f} != expected {want:.2f} "
                f"(cadence={plan.get('cadence')}"
                + (f", source {src_fps:.2f}" if src_fps is not None else "")
                + ")")
    elif plan.get("fps_from_source"):
        # No constant describes this output, because the source is not
        # uniform. Compare against the SOURCE at the same offset instead -
        # the same reasoning as the duplicate and drift checks. What matters
        # is that we did not CHANGE the rate, not what the rate happens to be.
        if got is None:
            problems.append("could not measure output framerate")
        elif src.exists():
            src_fps = decoded_fps(src, src_mid)
            if src_fps is None:
                problems.append(
                    f"framerate {got:.2f} and source unavailable "
                    "for comparison")
            elif abs(got - src_fps) > VARIABLE_FPS_TOL:
                problems.append(
                    f"framerate {got:.2f} vs source {src_fps:.2f} - "
                    f"{got - src_fps:+.2f} introduced "
                    f"(cadence={plan.get('cadence')})")

    # --- duplicate frames ---------------------------------------------------
    # The question is whether WE introduced duplicates, not whether any exist.
    # Very static cinematography produces genuinely near-identical consecutive
    # frames: one Blu-ray source here measures 35% under mpdecimate with
    # nothing wrong. So an absolute threshold gives false positives, and the
    # source rate is the only meaningful baseline. Measuring the source costs
    # two extra decode passes, so only pay for it when the output looks high.
    dup = duplicate_ratio(dst, mid)
    if dup is None:
        problems.append("could not measure duplicate frames")
    elif dup > DUPLICATE_TOL:
        src_dup = duplicate_ratio(src, src_mid) if src.exists() else None
        if src_dup is None:
            problems.append(
                f"duplicate frames {dup*100:.1f}% and source unavailable "
                "for comparison")
        else:
            deint = "bwdif" in (plan.get("cadence_filter") or "")
            tol = (DUPLICATE_INTRODUCED_TOL_DEINT if deint
                   else DUPLICATE_INTRODUCED_TOL)
            if dup - src_dup > tol:
                problems.append(
                    f"duplicate frames {dup*100:.1f}% vs source "
                    f"{src_dup*100:.1f}% - {(dup - src_dup)*100:.1f}% "
                    f"introduced (tolerance {tol*100:.0f}%"
                    + (", deinterlaced" if deint else "") + ")")

    # --- A/V sync -----------------------------------------------------------
    # Like the duplicate check, the question is whether WE introduced desync,
    # not whether any exists. A DVD rip can carry a small authored offset that
    # is entirely correct to preserve. So the output's drift is compared
    # against the source's, and only the difference is a failure.
    #
    # This is the check that catches a wrong cadence filter end-to-end:
    # decimating already-progressive 23.976 content drops one real frame in
    # five, so the video runs short against an audio track that was copied
    # untouched, and the gap grows all the way through the file.
    #
    # Which audio stream to use for the comparison matters. Two failure modes
    # measured on Friends 2026-09-19, both false positives - the encode was
    # fine in both:
    #
    # 1. A plan can reorder audio: s10e17e18 promotes a secondary 2-channel
    #    track (source_index 2) to the output's first audio stream, while in
    #    the source that same track is second (a:1). Naive "a:0 vs a:0"
    #    compares the promoted secondary track against the source's DIFFERENT
    #    main track - it read a -2328ms "failure" that was really the fact
    #    that the secondary track was never in sync with the main one to
    #    begin with. audio_position fixes this by matching source_index, not
    #    container position.
    #
    # 2. A re-encoded track (the AAC downmix) re-derives its PTS from decoded
    #    sample count, so it cannot carry forward whatever small container-
    #    level PTS quirk the source's raw track has - it measured -5333ms and
    #    -965ms on two Friends sources that were, on direct inspection, fine.
    #    A copied track has no such gap: it is bit-identical to the source,
    #    so it is the only stream a source comparison is actually meaningful
    #    for. Prefer it when the plan has one.
    audio_plan = plan.get("audio", [])
    copy_pos = next((i for i, a in enumerate(audio_plan)
                      if a.get("codec") == "copy"), None)
    a_out = f"a:{copy_pos if copy_pos is not None else 0}"
    a_src = "a:0"
    if audio_plan:
        idx = copy_pos if copy_pos is not None else 0
        if idx < len(audio_plan):
            src_index = audio_plan[idx].get("source_index")
            if src_index is not None and src.exists():
                try:
                    pos = audio_position(probe(src)["streams"], src_index)
                    if pos is not None:
                        a_src = f"a:{pos}"
                except Exception:  # noqa: BLE001
                    pass

    if sample:
        # A sampled encode is cut with an input seek, which offsets the
        # video's first PTS from the audio's and makes av_drift meaningless.
        # The span ratio is immune to that.
        ratio = av_span_ratio(dst, a=a_out)
        if ratio is not None and abs(1.0 - ratio) > SPAN_RATIO_TOL:
            problems.append(
                f"video covers {ratio*100:.1f}% of the audio timespan "
                "- video is running short against its audio")
        drift_out = None
    else:
        drift_out = av_drift(dst, a=a_out)
    if drift_out is None:
        pass  # sample (handled above), no audio, or unreadable timestamps
    elif src.exists():
        drift_src = av_drift(src, a=a_src)
        if drift_src is None:
            # The source could not be measured - on a very large Matroska the
            # audio tail probe hits PTS_PROBE_TIMEOUT (measured: every Blu-ray
            # film here, sources 20-60GB). Comparing against the source is the
            # better test, but its absence is not evidence of a fault: fall
            # back to an absolute bound. Drift under SYNC_TOL is inaudible
            # whether we introduced it or inherited it.
            #
            # The gap this leaves: a source carrying a large AUTHORED offset
            # would be reported here even though the encode is faithful. That
            # is the honest failure mode - it says "could not compare", not
            # "the encode is broken".
            if abs(drift_out) <= SYNC_TOL:
                pass
            else:
                problems.append(
                    f"A/V drift {drift_out*1000:+.0f}ms exceeds {SYNC_TOL*1000:.0f}ms "
                    "and the source could not be measured for comparison")
        elif abs(drift_out - drift_src) > SYNC_TOL:
            problems.append(
                f"A/V drift {drift_out*1000:+.0f}ms vs source "
                f"{drift_src*1000:+.0f}ms - "
                f"{(drift_out - drift_src)*1000:+.0f}ms introduced")

    # --- dimensions ---------------------------------------------------------
    if plan.get("crop"):
        cw, ch = plan["crop"][0], plan["crop"][1]
        ow, oh = int(vout.get("width") or 0), int(vout.get("height") or 0)
        if (ow, oh) != (cw, ch):
            problems.append(f"dimensions {ow}x{oh} != planned {cw}x{ch}")

    # --- colour tags --------------------------------------------------------
    want_col = plan.get("color") or {}
    if want_col:
        got_prim = vout.get("color_primaries")
        got_space = vout.get("color_space")
        if not got_prim or not got_space:
            problems.append("output is missing colour tags")
        elif (got_prim != want_col.get("primaries")
              or got_space != want_col.get("space")):
            problems.append(
                f"colour tags {got_prim}/{got_space} != planned "
                f"{want_col.get('primaries')}/{want_col.get('space')}")

    # --- streams ------------------------------------------------------------
    n_aud = sum(1 for s in p_out["streams"] if s.get("codec_type") == "audio")
    if n_aud != len(plan.get("audio", [])):
        problems.append(
            f"{n_aud} audio streams, plan specified {len(plan.get('audio', []))}")

    # --- optional VMAF ------------------------------------------------------
    if vmaf and src.exists():
        score = run_vmaf(src, dst, src_mid, plan.get("tier") == "dvd", plan.get("crop"))
        if score is None:
            problems.append("VMAF could not be computed")
        elif score < 90:
            problems.append(f"VMAF {score:.1f} below 90")
    return (not problems, problems)


def run_vmaf(src: Path, dst: Path, start: int, upscale: bool,
              crop: list[int] | None = None) -> float | None:
    """SD content is upscaled to 1080p first: the default VMAF model is trained
    at 1080p and scores are not meaningful at 480p without it.

    Bug (found 2026-09-25, on Bourne Ultimatum): this never applied the plan's
    own crop to the source before comparing. The source still carries its
    letterbox bars; the output does not. For any title with non-trivial crop
    that's a severe framing mismatch, not a quality measurement - it produced
    single-digit VMAF on a visually clean encode. `dst` is never cropped here;
    it was already cropped by the plan it was encoded from.
    """
    src_pre = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
    if upscale:
        fc = (f"[0:v]{src_pre}scale=1920:1080:flags=bicubic,setsar=1[ref];"
              "[1:v]scale=1920:1080:flags=bicubic,setsar=1[dis];"
              "[dis][ref]libvmaf")
    else:
        fc = f"[0:v]{src_pre}null[ref];[1:v][ref]libvmaf"
    cp = run(["ffmpeg", "-nostdin", "-ss", str(start), "-t", "20", "-i", str(src),
              "-ss", str(start), "-t", "20", "-i", str(dst),
              "-filter_complex", fc, "-f", "null", "-"], timeout=900)
    m = re.search(r"VMAF score:\s*([0-9.]+)", cp.stderr)
    return float(m.group(1)) if m else None


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("plans", nargs="+",
                    help="plan .json file(s) or a directory of them")
    ap.add_argument("--out", required=True,
                    help="destination root the encodes were written to")
    ap.add_argument("--flatten", action="store_true",
                    help="output was written flat into --out")
    ap.add_argument("--vmaf", action="store_true",
                    help="also compute VMAF (slow; upscales SD to 1080p)")
    ap.add_argument("--quiet", action="store_true",
                    help="only report failures")
    ap.add_argument("--sample", action="store_true",
                    help="output is a short sample; skip the duration check")
    ap.add_argument("--remap", action="append", metavar="OLD=NEW", default=[],
                    help="rewrite a path prefix, e.g. /Volumes/nas=/mnt/nas")


def run_cmd(args: argparse.Namespace) -> int:
    plan_files: list[Path] = []
    for p in args.plans:
        path = Path(p)
        if path.is_dir():
            plan_files.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            plan_files.append(path)
    if not plan_files:
        print("error: no plans found", file=sys.stderr)
        return 1

    remap = parse_remap(args.remap)
    out_root = Path(args.out)
    passed = failed = 0
    for pf in plan_files:
        plan = json.loads(pf.read_text())
        if plan.get("needs_review") or not plan.get("ok", True):
            continue
        try:
            ok, problems = verify_one(pf, out_root, args.flatten, args.vmaf,
                                      args.sample, remap)
        except Exception as exc:  # noqa: BLE001
            ok, problems = False, [repr(exc)]
        if ok:
            passed += 1
            if not args.quiet:
                print(f"PASS  {pf.stem}")
        else:
            failed += 1
            print(f"FAIL  {pf.stem}")
            for p in problems:
                print(f"        {p}")

    print(f"\n{passed} passed, {failed} failed", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    _ap = argparse.ArgumentParser(description=__doc__)
    add_args(_ap)
    sys.exit(run_cmd(_ap.parse_args()))
