"""
publish.py - move verified encodes into the library.

The last stage, and the only one that touches the live library. Its whole job
is to be boring and reversible.

  * Nothing is published unless it passes verify first. There is no
    --skip-verify; if a file should not be checked, it should not be published.
  * A displaced library file is retired, never deleted. Rollback is a mv.
  * TV output is placed into Season NN/ derived from the sNNeNN in the
    filename, which is what media servers expect.
  * Moves are staged then renamed, so an interrupted publish cannot leave a
    truncated file where the library expects a whole one.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import verify

_SEASON_RE = re.compile(r"[sS](\d{1,2})[eE]\d{1,2}")


def season_dir(name: str) -> str | None:
    m = _SEASON_RE.search(name)
    return f"Season {int(m.group(1)):02d}" if m else None


def destination(name: str, dest_root: Path, use_seasons: bool) -> Path:
    if use_seasons:
        sd = season_dir(name)
        if sd:
            return dest_root / sd / name
    return dest_root / name


def atomic_place(src: Path, dst: Path) -> None:
    """Copy or move into place without ever exposing a partial file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    staged = dst.with_name(dst.name + ".incoming")
    shutil.move(str(src), str(staged))
    staged.replace(dst)


def publish_one(plan_file: Path, staging: Path, dest_root: Path,
                retire: Path | None, use_seasons: bool, flatten: bool,
                dry_run: bool, remap: list[tuple[str, str]]
                ) -> tuple[str, str]:
    plan = json.loads(plan_file.read_text())
    if plan.get("needs_review"):
        return ("review", plan.get("review_reason") or "flagged for review")
    if not plan.get("ok", True):
        return ("skipped", "plan not ok")

    src_media = Path(verify.apply_remap(plan["path"], remap))
    name = src_media.stem + ".mkv"
    staged_file = staging / name if flatten else staging / src_media.parent.name / name
    if not staged_file.exists():
        return ("missing", f"no encode found at {staged_file}")

    dst = destination(name, dest_root, use_seasons)

    ok, problems = verify.verify_one(plan_file, staging, flatten, False,
                                     False, remap)
    if not ok:
        return ("failed", "; ".join(problems)[:200])

    if dry_run:
        return ("would-publish", str(dst))

    if dst.exists():
        if retire is None:
            return ("skipped",
                    f"destination exists and no --retire given: {dst}")
        rel = dst.relative_to(dest_root)
        retired = retire / rel
        retired.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(retired))

    atomic_place(staged_file, dst)
    return ("published", str(dst))


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("plans", nargs="+",
                    help="plan .json file(s) or a directory of them")
    ap.add_argument("--from", dest="staging", required=True,
                    help="directory the encodes were written to")
    ap.add_argument("--to", dest="dest", required=True,
                    help="library destination root")
    ap.add_argument("--retire", metavar="DIR",
                    help="move displaced library files here instead of "
                         "refusing to overwrite")
    ap.add_argument("--seasons", action="store_true",
                    help="place TV output into Season NN/ from the filename")
    ap.add_argument("--flatten", action="store_true",
                    help="encodes were written flat into --from")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen; move nothing")
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

    remap = verify.parse_remap(args.remap)
    staging = Path(args.staging)
    dest_root = Path(args.dest)
    retire = Path(args.retire) if args.retire else None

    counts: dict[str, int] = {}
    for i, pf in enumerate(plan_files, 1):
        try:
            status, detail = publish_one(
                pf, staging, dest_root, retire, args.seasons, args.flatten,
                args.dry_run, remap)
        except Exception as exc:  # noqa: BLE001
            status, detail = "failed", repr(exc)
        counts[status] = counts.get(status, 0) + 1
        marker = {"published": "ok", "would-publish": "->", "review": "??",
                  "skipped": "--", "missing": "..", "failed": "XX"}.get(status, "  ")
        print(f"[{i}/{len(plan_files)}] {marker} {pf.stem}: {detail}")

    print("\n--- summary ---", file=sys.stderr)
    for k in sorted(counts):
        print(f"  {k:<14} {counts[k]}", file=sys.stderr)
    if retire and counts.get("published"):
        print(f"\n  displaced files retired to {retire}/ - delete once happy",
              file=sys.stderr)
    return 0 if not (counts.get("failed") or counts.get("missing")) else 1


if __name__ == "__main__":  # pragma: no cover
    _ap = argparse.ArgumentParser(description=__doc__)
    add_args(_ap)
    sys.exit(run_cmd(_ap.parse_args()))
