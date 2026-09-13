# cintel

Measurement-driven pipeline that converts DVD and Blu-ray rips to HEVC 10-bit MKV for
Jellyfin. Named for the Rank Cintel — the telecine machine whose artifacts this pipeline
spends most of its effort undoing.

**Read `docs/handoff.md` before making changes.** It records every decision and the evidence
behind it. Several conclusions here are counter-intuitive and were only reached after
measurement contradicted careful reasoning.

---

## Cardinal rules

These are not style preferences. Each one was learned by breaking something.

1. **Classify cadence by DECODED frame rate, never the container.** A DVD container claiming
   29.97 usually decodes at 23.976 — ffmpeg already honours soft-telecine flags. Running
   `decimate` on such a source drops one *real* frame in five and yields 19.2 fps. Two
   independent AI models reasoned their way to a filter chain that would have destroyed the
   library. Measurement caught it.

2. **Colour must be set inside `-x265-params`.** ffmpeg's `-color_primaries` and `-color_trc`
   never reach libx265 at any argument position; only `-colorspace` survives. Without this,
   every file is tagged "unspecified" and rendered as BT.709 — wrong for SD content.

3. **Never `strings` the x265 SEI.** It truncates at 1024 bytes; the parameter stamp is
   ~2.3KB. Use `LC_ALL=C tr -c '[:print:]' '\n'`.

4. **Always `-nostdin` on ffmpeg.** It reads stdin and will eat a calling shell loop's input.

5. **Never `-v error` when parsing `idet`, `ssim` or `libvmaf`.** They log at INFO; `-v error`
   silently discards their output. This has bitten twice.

6. **Duplicate frames are only meaningful relative to the source.** Static cinematography
   legitimately produces 35% near-duplicates (measured on Dune Part 1). Ask whether the
   encode *introduced* duplicates, not whether any exist.

7. **VMAF against a live IVTC filtergraph is meaningless.** `decimate` drops different frames
   on different runs, so frames desync and every metric collapses. Render a lossless FFV1
   intermediate once and measure against that.

8. **Measure quality on HARD content.** Degradation is 4–5× steeper on a high-bitrate segment
   than an average one. A CRF sweep on an arbitrary segment nearly selected a setting with
   visible dark-scene blocking.

9. **Prefer reversible decisions.** Burn-in, aggressive cropping and lossy audio re-encoding
   are permanent. Subtitle selection and CRF are not. Sources are archived in
   `raw/` — re-encoding is always available, so never gamble on an irreversible operation.

10. **When a plan and reality disagree, suspect the checker.** Four of the last five bugs were
    in `verify.py`, not in the encodes. A verify failure means *investigate*, not *the encode
    is broken*.

---

## Architecture

```
analyze  →  plans/*.json  →  encode  →  verify  →  publish  →  library
```

| Stage | Role |
|---|---|
| `analyze.py` | **Read-only.** Probes each source, emits a plan containing the evidence, the decisions, and the literal ffmpeg argv. |
| `encode.py` | Executes a plan verbatim. **Makes no decisions.** If a plan is wrong, fix `analyze` and regenerate. |
| `verify.py` | Checks output against its plan: fps, duplicates-vs-source, duration, dimensions, colour tags, stream counts. |
| `publish.py` | Moves verified output into the library. Runs verify itself; there is deliberately no `--skip-verify`. |

**The plan is the contract.** It carries an `analyzer` fingerprint (a hash of `analyze.py`'s
own source). When the analyzer changes, every existing plan becomes stale and `encode` refuses
it. This exists because 172 of 173 plans were once silently stale and would have shipped with
broken colour.

Zero dependencies — standard library plus `ffmpeg`/`ffprobe`. No install step. The directory
is a runnable module.

---

## Usage

```bash
# analyze: takes files and/or directories (recursive), writes one plan per title
python3 cintel analyze /mnt/nas/media/raw/dvd/tv/charmed --out plans/charmed --jobs 4

# validate a plan cheaply - 90s from mid-file, exercises every code path
python3 cintel encode plans/charmed --out /tmp/stage --work /tmp/work --flatten --sample 90

# encode for real
python3 cintel encode plans/charmed --out /tmp/stage --work /tmp/work --flatten

# verify (add --sample if the output is a sample)
python3 cintel verify plans/charmed --out /tmp/stage --flatten

# publish
python3 cintel publish plans/charmed --from /tmp/stage \
    --to /mnt/nas/media/tv/kids/Charmed --retire /tmp/retired --seasons
```

Useful flags: `--force` (re-analyze), `--limit N`, `--dry-run`, `--progress`,
`--ignore-stale`, `--replace`, `--remap OLD=NEW`.

`--remap` rewrites source path prefixes recorded in a plan (e.g.
`/Volumes/nas=/mnt/nas`). **Not needed if `analyze` runs on the same machine as `encode`** —
plans record absolute source paths, so generating them where they will be used avoids the
issue entirely.

---

## Current configuration

```
DVD tier      preset medium, CRF 20, BT.601 tags
Blu-ray tier  preset slow,   CRF 21, BT.709 tags
cadence       measured per file: none / fieldmatch,decimate / needs_review
crop          cropdetect x10, 20th-percentile margin, 8px width floor
audio         copy where possible; lossless surround -> E-AC3 640k, 5.1 max; English only
subtitles     English only, degenerate tracks excluded, NO burn-in
codec         HEVC 10-bit (not AV1 - older iPads lack hardware decode)
```

Preset assignment is deliberately "cheap where it is plentiful, careful where it is rare":
~1,200 DVD titles get `medium`, ~36 Blu-rays get `slow`.

---

## State as of handoff (2026-09-12)

**Validated, not yet run at scale.**

- 25-file stratified sample: 25 analyzed, encoded, verified ✅
- Coverage test across all code paths ✅
- ~99% of library paths exercised
- 10 bugs found and fixed, all by measurement

**Next steps** (detail in `docs/handoff.md` §7):

1. Regenerate plans — any plan predating the current `analyze.py` is stale
2. Four **full** encodes (not samples) — samples cannot catch mid-file failures, which is how
   the muxer-stall bug presented
3. On a new machine, confirm ffmpeg has `libx265`, `libvmaf`, and the
   `fieldmatch`/`decimate`/`idet`/`ssim` filters, and that rule #2 above still holds (it is
   ffmpeg-version-specific)
4. Bulk run: ~1,039 titles

**Known open items:**

- 15 Buffy episodes have genuine mixed cadence → flagged `needs_review`; VapourSynth VIVTC is
  the right tool
- ~10 Charmed episodes show sporadic residual combing after IVTC; unresolved, ships anyway
  since it is a large improvement over 26–32% duplicate frames
- Per-episode crop varies within a series (cosmetic); a `--uniform-crop` mode would fix it
- `encode` has no `--jobs` flag — concurrency currently means splitting the plan list across
  processes
- Plan filenames derive from the source stem only, so two sources with identical basenames in
  different directories would collide silently (zero collisions in the current 1,219 files)

---

## Data

`data/` holds measured evidence, not derived artifacts. Regenerating it costs hours.

| File | Contents |
|---|---|
| `cadence-survey.tsv` | 1,035 sources: decoded rate, interlacing, cadence verdict |
| `structural-scan.tsv` | 1,219 sources: structural failure signatures |
| `worklists/` | Per-episode lists for Buffy and Charmed handling |

`tools/encode_audit` fingerprints existing HEVC files by extracting the x265 parameter stamp.
It pre-dates the pipeline and was used to forensically classify the old library.

---

## Working style for this project

- **Measure before concluding.** Every one of the ten bugs was found by measurement; none by
  reasoning. Two AI models independently reasoned their way to a destructive filter chain.
- **Vertical slices.** Run one title end-to-end through all four stages before analyzing
  thousands. Then a *stratified* slice across content types — one TV show is not
  representative of 436 movies.
- **Validate on real files.** `--sample 90` makes this cheap: full path coverage in minutes.
- **Record reasoning, not just outcomes.** "CRF 20" is useless without "measured on the
  hardest three minutes, because degradation is 4–5× steeper there."
