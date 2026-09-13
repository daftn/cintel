# cintel

Measurement-driven encode pipeline for DVD and Blu-ray rips.

Named for the [Rank Cintel](https://en.wikipedia.org/wiki/Telecine) — the telecine machine
whose artifacts this pipeline spends most of its effort undoing.

> **Working on this with an AI agent?** Read `CLAUDE.md` first. It carries the cardinal rules,
> each of which was learned by breaking something.

The guiding rule: **decide from the file, not from a label.** Every stage measures
the source and records what it decided, so the output can be audited later without
reverse-engineering it.

## Pipeline

```
rip ──▶ ingest/ ──▶ analyze ──▶ plans/*.json ──▶ encode ──▶ verify ──▶ publish ──▶ library
```

| Stage | Does |
|---|---|
| `analyze` | Probes each source, writes a plan containing the evidence, the decisions, and the literal ffmpeg argv. **Read-only.** |
| `encode` | Executes a plan verbatim. **Makes no decisions** — if a plan is wrong, fix `analyze` and regenerate. Refuses stale plans and skips `needs_review`. |
| `verify` | Checks output against its plan: framerate, duplicate frames vs source, duration, dimensions, colour tags, stream counts. |
| `publish` | Moves verified output into the library. Runs verify itself; there is deliberately no `--skip-verify`. Displaced files are retired, not deleted. |

All four stages are implemented and validated. A 25-file stratified sample spanning 12 movies,
four TV series, and Blu-ray film and TV was analyzed, encoded and verified end to end.

## Run

No install step, no virtualenv, no dependencies — the standard library plus the
`ffmpeg` and `ffprobe` binaries. The package directory is a runnable Python module.

```bash
git clone <this repo> && cd cintel
python3 cintel analyze /path/to/sources --out plans
```

Works on any box with `python3` (3.10+) and ffmpeg. To put it on your PATH:

```bash
#!/usr/bin/env bash
exec python3 "$HOME/code/cintel/cintel" "$@"
```

## Documentation

| Document | Contents |
|---|---|
| `CLAUDE.md` | Cardinal rules, architecture, current state — read this first |
| `docs/handoff.md` | Every decision with its evidence; all 10 bugs; validation performed |
| `docs/history/` | The original design plan (superseded) and the adversarial review prompt |
| `data/` | Measured surveys of 1,035 and 1,219 sources |

## analyze

```bash
python3 cintel analyze PATH [PATH...] [--out DIR] [--jobs N] [--force] [--quick]
```

- `--out` plan output directory (default `plans`)
- `--jobs` parallel workers (default 4; NAS reads degrade above ~4)
- `--force` re-analyze titles that already have a plan
- `--quick` fewer samples — faster, less reliable

Resumable: titles with an existing plan are skipped unless `--force`. Never writes
to or modifies a media file.

### What it decides

| Decision | Measured by |
|---|---|
| Cadence filter | Decoded fps at several points + `idet` combing counts |
| Crop | `cropdetect` across the runtime, low percentile of each margin |
| Colour tags | Source tags, falling back to resolution |
| Audio mapping | Codec and channel count per stream |
| Subtitle mapping | Codec (image vs text) and forced disposition |
| Tier / CRF | Frame height |

Plans land as one JSON per title, holding both the evidence and the resulting
command. The `{OUTPUT}` token in `argv` is substituted by `encode` at run time.

## encode

```bash
python3 cintel encode PLANS... --out DIR --work DIR [options]
```

`PLANS` is a plan file, a directory of them, or any mix. Encodes to a `.partial` keyed to a
hash of the destination — so concurrent encodes cannot collide — and moves into place only on
success. A killed encode can never leave something a later run mistakes for finished output.

- `--sample N` encode N seconds from mid-file; validates a plan in ~30s instead of hours
- `--limit N` stop after N successes
- `--flatten` write output directly into `--out` rather than mirroring the source's parent
- `--replace` overwrite an existing destination
- `--dry-run` print the command, encode nothing
- `--progress` stream ffmpeg output instead of capturing it
- `--remap OLD=NEW` rewrite source path prefixes (only needed if the plan was written on
  another machine)
- `--ignore-stale` run plans that predate the current `analyze.py`
- `--jobs N` encode N titles concurrently (default 1). At 480p one encode cannot
  saturate many cores, so 2–4 beats one wide encode; past ~4 you contend on NAS reads

Refuses stale plans, skips `needs_review`, and will not overwrite without `--replace`.

## verify

```bash
python3 cintel verify PLANS... --out DIR [--sample] [--vmaf]
```

Checks output against its plan: framerate matches the cadence decision, duplicate frames
relative to the **source**, A/V drift relative to the **source**, duration within 1%,
dimensions match the planned crop, colour tags present and correct, audio track count as
planned.

A/V sync is measured as drift — how far audio and video pull apart between the start and end
of the file — and compared against the source, because discs carry real authored offsets that
should be preserved. Sampled encodes use a video/audio timespan ratio instead, since the
input seek that makes a sample offsets the first video PTS and would otherwise read as
desync.

`--sample` skips the duration check for deliberately-short sample encodes. `--vmaf` is slow
and off by default; it upscales SD to 1080p first, since the default VMAF model is invalid at
480p.

## publish

```bash
python3 cintel publish PLANS... --from DIR --to DIR [--retire DIR] [--seasons]
```

Runs verify itself and refuses to move anything that fails — there is no `--skip-verify`.
`--retire DIR` moves a displaced library file aside rather than deleting it, so rollback is a
`mv`. `--seasons` derives `Season NN/` from the `sNNeNN` in the filename.

## Why the decisions look the way they do

Each of these was measured on this library, and several contradict the obvious
assumption. They are the reason the pipeline measures rather than infers.

**Container framerate is not the frame rate.** A DVD reporting 29.97 usually
decodes at 23.976 — ffmpeg already honours the soft-telecine flags, so the coded
frames are film. Running `decimate` on that drops one *real* frame in five and
yields 19.2fps. 81% of sampled sources need no cadence filter at all. Cadence is
therefore classified by decoded rate, never by the container.

**Hard telecine is real but rare, and concentrated.** 18% of sources decode at
29.97 with heavy combing, and `fieldmatch,decimate` recovers clean 23.976 with
zero residual combing. Of those, the overwhelming majority were a single TV
series.

**Sources are often untagged for colour.** Untagged HEVC is interpreted as
BT.709, which is wrong for 480-line content that is really BT.601. Tags are
always written explicitly.

**Mixed cadence exists and must not be guessed at.** A small number of episodes
change cadence mid-file; sample points disagree. Those are flagged
`needs_review` rather than filtered, because any fixed-cycle decimation damages
them.

**Lossless audio passthrough dominates Blu-ray file size.** One title's TrueHD
track was 55% of the file — larger than the video. Lossless surround is
transcoded to E-AC3 rather than copied.

**Image-based subtitles break direct play on Apple clients.** Enabling a
VobSub/PGS track makes the media server burn it in, forcing a full video
transcode. They are mapped through but flagged; OCR to SRT is a separate stage.

**VMAF is not used for the SD tier.** The default model is trained at 1080p and
returns invalid scores at 480p. CRF is fixed per tier until per-tier targets are
validated by eye.

## Gotchas encoded in the source

Each cost real debugging time and is commented where it matters:

- `strings` truncates at 1024 bytes; the x265 parameter stamp is ~2.3KB. Use `tr`.
- ffmpeg reads stdin and will consume a caller's input loop — always `-nostdin`.
- `idet` logs at INFO level, so `-v quiet` silently discards its output.
- `-af:a:0` parses but applies to *every* audio stream. Use `-filter:a:0`.
- `codec_name` is `dts` for both lossy DTS and lossless DTS-HD MA; only `profile`
  tells them apart, so "is it lossless" cannot be a codec-name lookup.
- The colour-tag bug (above) does not reproduce on ffmpeg 6.1.1, only on 8.1+.
- macOS writes `._*` AppleDouble sidecars on SMB shares; they match media
  extensions but are not media.
- BSD `xargs` has neither `-a` nor `-d`.
