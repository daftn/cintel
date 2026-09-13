"""
encode.py - execute plans produced by analyze.

Makes no decisions. The plan already contains the literal ffmpeg argv; this
stage substitutes the output path, runs it, and handles the file lifecycle
safely. If a plan looks wrong, fix analyze and regenerate it - do not add
judgement here.

Safety properties that matter at fleet scale:

  * Encodes to a .partial file in the work directory, never directly to the
    destination. A killed encode can therefore never leave something that a
    later run mistakes for finished output.
  * Refuses to overwrite an existing destination unless --replace is given.
  * Skips plans marked needs_review. Those are the mixed-cadence files where
    any fixed recipe does damage.
  * Never touches the source.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import analyze
import verify

OUTPUT_TOKEN = "{OUTPUT}"


def probe_duration(path: Path) -> float | None:
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    try:
        return float(cp.stdout.strip())
    except ValueError:
        return None


def load_plan(p: Path) -> dict:
    with p.open() as fh:
        return json.load(fh)


def destination(plan: dict, out_root: Path, flatten: bool,
                remap: list[tuple[str, str]] | None = None) -> Path:
    src = Path(verify.apply_remap(plan["path"], remap or []))
    name = src.stem + ".mkv"
    if flatten:
        return out_root / name
    # Mirror the immediate parent directory (a show folder, typically).
    return out_root / src.parent.name / name


def human(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def apply_sample(argv: list[str], seconds: int, source_dur: float | None
                 ) -> list[str]:
    """Trim the encode to a short window from the middle of the file.

    Used to validate that a plan is CORRECT - colour tags, crop, audio
    mapping, cadence - without paying for a full encode. A 90-second sample
    exercises every code path a two-hour encode does.
    """
    start = int(source_dur / 2) if source_dur and source_dur > 4 * seconds else 0
    out = list(argv)
    i = out.index("-i")
    if start:
        out[i:i] = ["-ss", str(start)]
        i += 2
    # Record where in the source this sample came from. Without it, verify
    # compares the sample's mid-film content against the source's opening
    # credits and reports invented duplicate frames.
    out.insert(len(out) - 1, "-metadata")
    out.insert(len(out) - 1, f"ENCODE_SAMPLE_START={start}")
    # -t belongs with the output, i.e. at the end before the destination.
    out.insert(len(out) - 1, "-t")
    out.insert(len(out) - 1, str(seconds))
    return out


def stamp_toolchain(argv: list[str]) -> list[str]:
    """Record the toolchain that ACTUALLY produced this output.

    Not a decision - provenance, in the same spirit as substituting {OUTPUT}
    or injecting -ss/-t for a sample. The plan records what MEASURED the
    source, but a plan may legitimately be generated on one machine and
    executed on another; several of this pipeline's rules are
    ffmpeg-version-specific, so a library encoded across two toolchains is
    unattributable without this. Inserted before the output path, which is
    always the final argument.
    """
    out = list(argv)
    for key, value in (("ENCODE_FFMPEG", analyze.ffmpeg_version()),
                       ("ENCODE_X265", analyze.x265_version())):
        out.insert(len(out) - 1, "-metadata")
        out.insert(len(out) - 1, f"{key}={value}")
    return out


def encode_one(plan_file: Path, out_root: Path, work_dir: Path,
               flatten: bool, replace: bool, dry_run: bool,
               progress: bool, sample: int | None = None,
               expected_analyzer: str | None = None,
               remap: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    """Returns (status, detail). Status is one of:
    encoded, skipped, review, failed."""
    plan = load_plan(plan_file)

    if not plan.get("ok", False):
        return ("failed", f"plan marked not ok: {plan.get('error')}")
    if plan.get("needs_review"):
        return ("review", plan.get("review_reason") or "flagged for review")
    argv = plan.get("argv")
    if not argv:
        return ("failed", "plan has no argv")
    if expected_analyzer and plan.get("analyzer") != expected_analyzer:
        return ("stale", "plan predates the current analyze.py; re-run "
                         "analyze --force")

    src = Path(verify.apply_remap(plan["path"], remap or []))
    if not src.exists():
        return ("failed", f"source missing: {src}")

    dst = destination(plan, out_root, flatten, remap)
    if dst.exists() and not replace:
        return ("skipped", "destination exists (use --replace to overwrite)")

    work_dir.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # The partial name is keyed to the DESTINATION, not just the title. Two
    # concurrent encodes of the same title to different destinations (a preset
    # comparison, say) would otherwise write to the same scratch file and
    # corrupt each other.
    tag = hashlib.sha1(str(dst.resolve()).encode()).hexdigest()[:8]
    partial = work_dir / f"{dst.stem}.{tag}.partial.mkv"

    argv = [verify.apply_remap(a, remap or []) for a in argv]
    cmd = [partial.as_posix() if a == OUTPUT_TOKEN else a for a in argv]
    if OUTPUT_TOKEN not in argv:
        # Older plans ended with a literal path rather than the token.
        cmd = argv[:-1] + [partial.as_posix()]
    if sample:
        cmd = apply_sample(cmd, sample, probe_duration(src))
    cmd = stamp_toolchain(cmd)
    if replace:
        cmd.insert(1, "-y")

    if dry_run:
        return ("skipped", " ".join(cmd))

    partial.unlink(missing_ok=True)
    started = time.time()
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None if progress else subprocess.PIPE,
        text=True,
        errors="replace",
    )
    elapsed = time.time() - started

    if proc.returncode != 0:
        tail = ""
        if proc.stderr:
            tail = " | ".join(proc.stderr.strip().splitlines()[-3:])[:300]
        partial.unlink(missing_ok=True)
        return ("failed", f"ffmpeg exit {proc.returncode}: {tail}")

    if not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        return ("failed", "ffmpeg succeeded but produced no output")

    # Move into place only once the encode has completed successfully. Across
    # filesystems this is a copy, so write to a neighbouring temp name first
    # and rename - rename within a filesystem is atomic.
    staged = dst.with_suffix(".incoming.mkv")
    shutil.move(str(partial), str(staged))
    staged.replace(dst)

    size_gb = dst.stat().st_size / 1024**3
    return ("encoded", f"{human(elapsed)}, {size_gb:.2f} GB")


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("plans", nargs="+",
                    help="plan .json file(s) or a directory of them")
    ap.add_argument("--out", required=True,
                    help="destination root for encoded files")
    ap.add_argument("--work", default="work",
                    help="scratch directory for in-progress encodes")
    ap.add_argument("--limit", type=int,
                    help="stop after N successful encodes")
    ap.add_argument("--flatten", action="store_true",
                    help="write all output directly into --out")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite existing destination files")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the command that would run; encode nothing")
    ap.add_argument("--progress", action="store_true",
                    help="stream ffmpeg output instead of capturing it")
    ap.add_argument("--remap", action="append", metavar="OLD=NEW", default=[],
                    help="rewrite a path prefix, e.g. /Volumes/nas=/mnt/nas")
    ap.add_argument("--ignore-stale", action="store_true",
                    help="run plans even if they predate the current analyzer")
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="encode N titles concurrently (default 1). At 480p a "
                         "single encode cannot saturate many cores, so 2-4 "
                         "beats one wide encode; past ~4 you contend on NAS "
                         "reads rather than gaining throughput")
    ap.add_argument("--sample", type=int, metavar="SECONDS",
                    help="encode only a short window from the middle; "
                         "validates a plan without a full encode")


def run_cmd(args: argparse.Namespace) -> int:
    plan_files: list[Path] = []
    for p in args.plans:
        path = Path(p)
        if path.is_dir():
            plan_files.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            plan_files.append(path)
        else:
            print(f"warning: skipping {p}", file=sys.stderr)
    if not plan_files:
        print("error: no plans found", file=sys.stderr)
        return 1

    expected = None
    if not args.ignore_stale:
        try:
            expected = analyze.analyzer_fingerprint()
        except Exception:  # noqa: BLE001
            expected = None

    remap = verify.parse_remap(args.remap)
    out_root = Path(args.out)
    work_dir = Path(args.work)
    counts: dict[str, int] = {}
    encoded = 0
    batch_started = time.time()

    jobs = max(1, args.jobs)
    print(f"{len(plan_files)} plans; output -> {out_root}"
          + (f"; {jobs} concurrent" if jobs > 1 else ""), file=sys.stderr)

    # Concurrency is safe here only because each encode writes to a .partial
    # keyed to a hash of its DESTINATION (see encode_one). That was bug 3;
    # without it, parallel encodes corrupt each other.
    stop = threading.Event()
    marker = {"encoded": "ok", "skipped": "--", "review": "??",
              "stale": "!!", "failed": "XX"}

    def work(i: int, pf: Path):
        if stop.is_set():
            return (i, pf, None, None)
        try:
            status, detail = encode_one(
                pf, out_root, work_dir, args.flatten, args.replace,
                args.dry_run, args.progress, args.sample, expected, remap)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            status, detail = "failed", repr(exc)
        return (i, pf, status, detail)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(work, i, pf)
                       for i, pf in enumerate(plan_files, 1)]
            for fut in concurrent.futures.as_completed(futures):
                i, pf, status, detail = fut.result()
                if status is None:  # skipped after --limit was reached
                    continue
                counts[status] = counts.get(status, 0) + 1
                print(f"[{i}/{len(plan_files)}] {marker.get(status, '  ')} "
                      f"{pf.stem}: {detail}", file=sys.stderr)
                if status == "encoded":
                    encoded += 1
                    if args.limit and encoded >= args.limit and not stop.is_set():
                        stop.set()
                        print(f"\nreached --limit {args.limit}; "
                              "no further titles will start", file=sys.stderr)
    except KeyboardInterrupt:
        # ffmpeg shares this process group, so it has already taken the same
        # SIGINT; encode_one unlinks the .partial on a non-zero exit.
        stop.set()
        print("\ninterrupted; partial output discarded", file=sys.stderr)
        return 130

    print("\n--- summary ---", file=sys.stderr)
    for k in sorted(counts):
        print(f"  {k:<9} {counts[k]}", file=sys.stderr)
    if encoded:
        print(f"  total time {human(time.time() - batch_started)}",
              file=sys.stderr)
    return 0 if not counts.get("failed") else 1


if __name__ == "__main__":  # pragma: no cover
    _ap = argparse.ArgumentParser(description=__doc__)
    add_args(_ap)
    sys.exit(run_cmd(_ap.parse_args()))
