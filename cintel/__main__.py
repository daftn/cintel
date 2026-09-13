"""Top-level CLI: `python3 cintel <stage> ...`."""

from __future__ import annotations

import argparse
import shutil
import sys

import analyze
import encode
import verify
import publish

__version__ = "0.1.0"

# Stages that do not exist yet. Listed so `--help` shows the intended shape of
# the pipeline and a typo on a future stage gives a useful message.
PLANNED: dict[str, str] = {}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cintel",
        description="Measurement-driven media encode pipeline.",
        epilog=("Planned stages: " + ", ".join(sorted(PLANNED))
                if PLANNED else "Stages run in order: "
                "analyze -> encode -> verify -> publish"),
    )
    ap.add_argument("--version", action="version",
                    version=f"cintel {__version__}")
    sub = ap.add_subparsers(dest="stage", metavar="STAGE")

    p_analyze = sub.add_parser(
        "analyze",
        help="probe sources and write encode plans (read-only)",
        description=analyze.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze.add_args(p_analyze)
    p_analyze.set_defaults(func=analyze.run_cmd)

    p_encode = sub.add_parser(
        "encode",
        help="execute plans produced by analyze",
        description=encode.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    encode.add_args(p_encode)
    p_encode.set_defaults(func=encode.run_cmd)

    p_verify = sub.add_parser(
        "verify",
        help="check encoded output against its plan",
        description=verify.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify.add_args(p_verify)
    p_verify.set_defaults(func=verify.run_cmd)

    p_publish = sub.add_parser(
        "publish",
        help="move verified encodes into the library",
        description=publish.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    publish.add_args(p_publish)
    p_publish.set_defaults(func=publish.run_cmd)

    for name, desc in sorted(PLANNED.items()):
        p = sub.add_parser(name, help=f"(not implemented) {desc}")
        p.set_defaults(func=_not_implemented, stage_name=name)

    return ap


def _not_implemented(args: argparse.Namespace) -> int:
    print(f"error: '{args.stage_name}' is not implemented yet",
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help(sys.stderr)
        return 1

    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        print(f"error: not found on PATH: {', '.join(missing)}",
              file=sys.stderr)
        return 1

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
