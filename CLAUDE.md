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
   never reach libx265 at any argument position. Without this, transfer and matrix are
   tagged "unspecified" and rendered as BT.709 — wrong for SD content.
   **Do not re-test this rule on ffmpeg 6.1.1.** Measured: the failure does not reproduce
   there (the flags work), but does on 8.1+/x265 4.2. A re-test on Ubuntu's stock package
   will tell you the rule is unnecessary, and it is wrong. This box runs ffmpeg 9.0.1.

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

10. **When a plan and reality disagree, suspect the checker.** Eight of the last nine bugs
    were in `verify.py` or in what `analyze` measured, not in the encodes. A verify failure means *investigate*, not *the encode
    is broken*. Most recently: a drift check reporting −970 *seconds* on a file that was
    fine. If a measurement is absurd on its face, measure the measurement.

---

11. **A/V sync: compare drift to the source, and never use `av_drift` on a sample.**
    A sampled encode is cut with an input `-ss`, so the video's first PTS lands after the
    audio's — measured at 189–416ms. `av_drift` reads that as desync and fails a good
    sample. Samples use `av_span_ratio` instead. Source skew is real and must be preserved
    (one Charmed episode is authored −168ms); only *introduced* drift is a failure.

---

## Architecture

```
analyze  →  plans/*.json  →  encode  →  verify  →  publish  →  library
```

| Stage | Role |
|---|---|
| `analyze.py` | **Read-only.** Probes each source, emits a plan containing the evidence, the decisions, and the literal ffmpeg argv. |
| `encode.py` | Executes a plan verbatim. **Makes no decisions.** If a plan is wrong, fix `analyze` and regenerate. |
| `verify.py` | Checks output against its plan: fps, duplicates-vs-source, **A/V drift-vs-source**, duration, dimensions, colour tags, stream counts. |
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
python3 cintel analyze /nas/media/raw/dvd/tv/charmed --out plans/charmed --jobs 4

# validate a plan cheaply - 90s from mid-file, exercises every code path
python3 cintel encode plans/charmed --out /nas/media/encoded/charmed --work /nas/media/encoded/charmed-work \
    --flatten --sample 90

# encode for real (nice/ionice: Jellyfin and Plex share this host)
nice -n 15 ionice -c3 python3 cintel encode plans/charmed \
    --out /nas/media/encoded/charmed --work /nas/media/encoded/charmed-work --flatten --jobs 3

# verify (add --sample if the output is a sample)
python3 cintel verify plans/charmed --out /nas/media/encoded/charmed --flatten

# publish
python3 cintel publish plans/charmed --from /nas/media/encoded/charmed \
    --to /nas/media/tv/kids/Charmed --retire /nas/media/encoded/charmed-retired --seasons
```

Useful flags: `--force` (re-analyze), `--limit N`, `--dry-run`, `--progress`,
`--ignore-stale`, `--replace`, `--remap OLD=NEW`, `--jobs N` (encode concurrency),
`--tier NAME` (analyze; force one tier for the whole run).

**Tier comes from the path; `--tier` is the exception.** Drop a rip into
`raw/bluray/tv/`, `raw/bluray/movies/film/` or `raw/bluray/movies/standard/` and the tier
follows from where it sits (anything left loose in `raw/bluray/movies/` falls to
`bluray-film`, the careful side) — which survives the re-analysis that every change to
`analyze.py` forces, whereas a flag typed once does not. `--tier` is for a homogeneous
batch you would rather not file by hand. Either way the plan records which one decided,
and resolution still wins: an SD source cannot be forced into a Blu-ray tier.

**Scratch belongs on `/data`, never on `/`.** The container rootfs is 20 GB; `/data` is
1.8 TB of NVMe. A single Blu-ray `.partial` can be 8–10 GB.

`--remap` rewrites source path prefixes recorded in a plan (e.g.
`/Volumes/nas=/nas` — the NAS is mounted at `/nas` in this container, **not**
`/mnt/nas`). **Not needed if `analyze` runs on the same machine as `encode`** — plans
record absolute source paths, so generating them where they will be used avoids the issue
entirely.

---

## Where this runs

Proxmox LXC **container 101** (`ubuntu`, unprivileged) on an AMD Ryzen 7 8845HS —
**8 physical cores / 16 threads**, 28 GB host RAM. The container is allocated 16 cores,
16 GB RAM, 2 GB swap. LXC `cores` is a ceiling, not a reservation, so unused capacity
returns to the host.

| Path | What | Notes |
|---|---|---|
| `/nas` | host `/mnt/nas` → Synology NFS, 9.1 TB | sources in `raw/`, library in `movies/`, `tv/` |
| `/data` | host `/mnt/data` → 1.8 TB NVMe | **scratch and staging belong here** |
| `/` | 20 GB LVM | never put scratch here; two Blu-ray partials would fill it |

Jellyfin (CT 100) and Plex (CT 103) share the host, so run encodes under
`nice -n 15 ionice -c3` — x265 yields instantly when a stream needs CPU, at a cost of a few
percent throughput.

`/dev/dri` is passed into the container, but hardware encoding is deliberately unused
(§3.7 of the handoff: measurably worse quality per bit than software x265).

---

## The `/nas/media` layout

Counts below are as of 2026-09-21. The shape matters more than the numbers: **movies and
TV are each split by audience**, and a count taken from one subdirectory is not the
library total. Getting this wrong is how a doc ended up claiming "86 films."

```
/nas/media/
├── movies/          223 films, split by audience
│   ├── no_kids/         72      <- the tier this pipeline mostly feeds
│   ├── older_kids/      58
│   └── younger_kids/    93
├── tv/              split the same way
│   ├── no_kids/         11 shows
│   └── kids/             3 shows
├── raw/             SOURCES - never deleted, tier decided by path
│   ├── bluray/
│   │   ├── movies/      film/ -> bluray-film, standard/ -> bluray-standard
│   │   └── tv/          -> bluray-tv
│   ├── dvd/
│   │   ├── movies/      436 titles
│   │   └── tv/          4 shows
│   └── processed/       sources already encoded, kept as archive
├── ingest/          raw MakeMKV dumps, _tNN names, awaiting identification
├── encoded/         staging and work dirs; empty between batches
├── audio/           .m4a listening extracts (NOT produced by cintel)
│   ├── TV/              per-show, per-season
│   └── Movies/
├── photos/          not ours
└── youtube/         not ours
```

`raw/` is the archive of record: a source is moved to `raw/processed/<name>/` after its
encode is verified and shelved, never deleted, so any title can be re-encoded when a
decision changes. Replaced library files follow the same habit — they move to a
`*-retired` directory rather than being deleted, though those directories are pruned by
hand once the replacement has been checked, so do not expect them to exist.

---

## Current configuration

```
dvd tier          preset medium, CRF 20, BT.601 tags
bluray-film tier  preset slow,   CRF 19, BT.709 tags  (~20 movies)
bluray-tv tier    preset medium, CRF 21, BT.709 tags  (~500 eps once Office/BBT land)
cadence       measured per file: none / fieldmatch,decimate / needs_review
crop          cropdetect x10, 20th-percentile margin, 8px width floor
audio         copy where possible; lossless surround -> E-AC3 640k, 5.1 max; English only
              "lossless" is decided by PROFILE - dts is lossy unless DTS-HD MA
subtitles     English only, degenerate tracks excluded, NO burn-in
codec         HEVC 10-bit (not AV1 - older iPads lack hardware decode)
toolchain     ffmpeg n9.0.1 / x265 4.2, static build in /usr/local/bin (not apt's 6.1.1)
```

Preset assignment is deliberately "cheap where it is plentiful, careful where it is rare":
~1,200 DVD titles get `medium`, ~20 Blu-ray films get `slow`. Blu-ray **TV** gets `medium`
too — Office and Big Bang Theory arriving on disc take that tier from 16 files to ~500, so
Blu-ray is no longer a proxy for "rare" (`docs/handoff.md` §3.6a).

**`slow` and `medium` are not interchangeable at a fixed CRF.** Measured at 1080p: `slow`
gains +0.30 to +0.53 VMAF at matched bitrate, and `medium` needs ~44% more bitrate to match
`slow` CRF 21. Changing a preset means re-choosing the CRF with it. The film/tv split is
decided from the **path**, a deliberate exception to "decide from the file" — it encodes how
much a title is worth to its owner, which nothing can measure.

---

## State as of 2026-09-26

**Seven shows delivered and published, plus the movie library's first DVD-tier batch.**

| | Episodes |
|---|---:|
| Charmed | 173 |
| Buffy | 143 |
| Friends | 226 |
| The 100 | 100 (99 encoded + 1 pre-existing mp4 for s06e05) |
| Ash vs Evil Dead | 30 |
| Parks and Recreation | 122 files / 125 eps |
| The Office (Blu-ray) | 194 files / 202 eps |

The **`bluray-film` tier is complete** — all 14 titles, though 9 of them needed a follow-up
audio-only remux (below). Those sit among the 72 films in `movies/no_kids/`; the movie
library as a whole is 223 across all three audience tiers, most of which predate this
pipeline — except now the DVD movie queue is also underway: **100 of 436 encoded and
verified** (batch 1), **336 analyzing/encoding** (batch 2). This is the first time the `dvd`
tier has ever been run on movie content rather than a TV show.

- Charmed fixed a genuinely broken library: the old encodes ran at **25.833 fps** with a
  **bt709 transfer on SD content**. Both corrected.
- Buffy: 63 of 143 episodes deinterlaced, 80 passed through clean. Enabled after the owner
  compared a repaired episode against an unrepaired one by eye.
- Parks and Office each turned up a real episode-numbering error before publish — see
  `docs/handoff.md` §7.1. Not code bugs; a disc-order/duration-outlier identification method
  that needs a real episode-count source to corroborate against, not just duration.
- 9 `bluray-film` titles shipped a commentary track mislabeled as the disc's stereo mix
  (analyzed before the bug-18 fix existed). Fixed with an **audio-only remux** — video
  stream-copied untouched, no re-encode — confirmed by measured `av_drift` to introduce no
  timing drift.
- **20 bugs found by measurement (19 fixed, 1 open).** Bug 20: `verify.py`'s `--vmaf` never
  applied a plan's own crop to the source before comparing, producing single-digit scores on
  a visually clean encode. Fixed; validated via the real `--vmaf` CLI path. See bug log.
- Throughput measured: **5.08 min** per 43.5-min DVD episode at `--jobs 3` (TV); DVD movies
  run **~5.6 titles/hour** at `--jobs 3`, far cheaper than any Blu-ray tier.
- Blu-ray tiers split and each CRF measured against a lossless FFV1 reference (§3.6a).

**Next** (detail in `docs/handoff.md` §7.1):

1. Finish DVD movies batch 2 (336 titles), verify, owner's audience-folder sort, publish.
2. `bluray-standard`, 26 titles staged, never analyzed.
3. `bluray-film`, 5 more staged in `raw/bluray/movies/film/`.
4. Big Bang Theory — 8 discs (S1-4) unnamed in `ingest/`. Same disc-order naming work Office
   needed; also the next real test of whether `bluray-tv` CRF 21 holds up on bright
   multi-cam sitcoms (flagged, not yet measured).
5. Wild Thornberrys (DVD) — `tune=animation` still unmeasured, don't encode blind.
6. `raw/dvd/tv/the office/`, a 185-file DVD rip — almost certainly superseded by the
   Blu-ray Superfan version now finished; probably archive without encoding.

**Known open items:**

- **Bug 17 (OPEN):** `classify_cadence` misses short progressive stretches in telecined files.
- Killing Eve: existing episodes used weak old HandBrake script (`sao`, `aq-mode=2`), re-encode recommended.
- ~~13 zero-byte macOS `._` AppleDouble stubs in `tv/no_kids/The 100/` root~~ —
  **resolved**, removed and reconfirmed clean 2026-09-26.
- The 100 `s02e08` source has 33 concealed decode errors — well under a second of
  artifacts, auto-concealed. Re-rip only if that disc is already to hand.
- Per-episode crop varies within a series (cosmetic); a `--uniform-crop` mode would fix it
- Plan filenames derive from the source stem, so identical basenames in different
  directories would collide silently
- Very large titles (60 GB+) lose the A/V drift check — audio seeks are pathological in big
  Matroska files; every other check still runs
- `--vmaf` is improved (bug 20 fixed) but not fully trusted — an unexplained anomaly during
  that investigation was never root-caused. Prefer the §4.2 FFV1-reference method for any
  CRF/quality decision that matters.
- `bluray-tv` was re-measured against real Office content (vertical slice before the
  194-episode run) rather than resting solely on one Killing Eve episode — but Big Bang
  Theory (next item 4) is a better proxy still for bright multi-cam sitcoms specifically.

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

- **Measure before concluding.** Every one of the eleven bugs was found by measurement; none by
  reasoning. Two AI models independently reasoned their way to a destructive filter chain.
- **Vertical slices.** Run one title end-to-end through all four stages before analyzing
  thousands. Then a *stratified* slice across content types — one TV show is not
  representative of 436 movies.
- **Validate on real files.** `--sample 90` makes this cheap: full path coverage in minutes.
- **Record reasoning, not just outcomes.** "CRF 20" is useless without "measured on the
  hardest three minutes, because degradation is 4–5× steeper there."
