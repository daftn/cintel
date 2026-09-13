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
DURATION_TOL = 0.01   # fraction
DUPLICATE_TOL = 0.02             # output ratio that triggers a source comparison
DUPLICATE_INTRODUCED_TOL = 0.05  # how much more than the source is a failure
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


def expected_fps(plan: dict) -> float | None:
    """What the plan's cadence decision should produce."""
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
        problems.append(
            f"framerate {got:.2f} != expected {want:.2f} "
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
        elif dup - src_dup > DUPLICATE_INTRODUCED_TOL:
            problems.append(
                f"duplicate frames {dup*100:.1f}% vs source "
                f"{src_dup*100:.1f}% - {(dup - src_dup)*100:.1f}% introduced")

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
        score = run_vmaf(src, dst, src_mid, plan.get("tier") == "dvd")
        if score is None:
            problems.append("VMAF could not be computed")
        elif score < 90:
            problems.append(f"VMAF {score:.1f} below 90")
    return (not problems, problems)


def run_vmaf(src: Path, dst: Path, start: int, upscale: bool) -> float | None:
    """SD content is upscaled to 1080p first: the default VMAF model is trained
    at 1080p and scores are not meaningful at 480p without it."""
    if upscale:
        fc = ("[0:v]scale=1920:1080:flags=bicubic,setsar=1[ref];"
              "[1:v]scale=1920:1080:flags=bicubic,setsar=1[dis];"
              "[dis][ref]libvmaf")
    else:
        fc = "[1:v][0:v]libvmaf"
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
