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
python3 cintel encode plans/charmed --out /data/cintel-stage --work /data/cintel-work \
    --flatten --sample 90

# encode for real (nice/ionice: Jellyfin and Plex share this host)
nice -n 15 ionice -c3 python3 cintel encode plans/charmed \
    --out /data/cintel-stage --work /data/cintel-work --flatten --jobs 3

# verify (add --sample if the output is a sample)
python3 cintel verify plans/charmed --out /data/cintel-stage --flatten

# publish
python3 cintel publish plans/charmed --from /data/cintel-stage \
    --to /nas/media/tv/kids/Charmed --retire /data/cintel-retired --seasons
```

Useful flags: `--force` (re-analyze), `--limit N`, `--dry-run`, `--progress`,
`--ignore-stale`, `--replace`, `--remap OLD=NEW`, `--jobs N` (encode concurrency).

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

## State as of 2026-09-15

**Two shows delivered and published.**

| | Episodes | Output | Replaced |
|---|---:|---:|---:|
| Charmed | 173 | 63 GB | 132 GB |
| Buffy | 143 | 47 GB | 136 GB |

- Charmed fixed a genuinely broken library: the old encodes ran at **25.833 fps** with a
  **bt709 transfer on SD content**. Both corrected.
- Buffy: 63 of 143 episodes deinterlaced, 80 passed through clean. Enabled after the owner
  compared a repaired episode against an unrepaired one by eye.
- **14 bugs found and fixed, all by measurement.** Bugs 12-14 were *measurement* defects,
  not encode defects — see `docs/handoff.md` §5.
- Throughput measured: **5.08 min** per 43.5-min DVD episode at `--jobs 3`; **4.78 min** for
  Buffy unfiltered, **6.15 min** with the deint chain.
- Blu-ray tiers split and each CRF measured against a lossless FFV1 reference (§3.6a).

**In flight:** Hot Fuzz at `slow` CRF 19 — first end-to-end run of the `bluray-film` tier.

**Next** (detail in `docs/handoff.md` §7.1):

1. Verify Hot Fuzz, then the remaining 13 `bluray-film` titles (~60 h)
2. `bluray-standard`, 15 titles (~24 h)
3. Name the `ingest/` backlog — Dexter (33) needs disc mapping; Thornberrys S2P3/S3 (31) does not
4. Measure `tune=animation` before encoding the cartoons
5. The DVD queue: parks and rec, the 100, ash vs evil dead, then 436 movies

**Known open items:**

- Per-episode crop varies within a series (cosmetic); a `--uniform-crop` mode would fix it
- Plan filenames derive from the source stem, so identical basenames in different
  directories would collide silently
- Very large titles (60 GB+) lose the A/V drift check — audio seeks are pathological in big
  Matroska files; every other check still runs
- `bluray-tv` rests on one Killing Eve episode; re-measure when Office/BBT discs arrive

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
