# Media Encode Pipeline — Handoff

**Status:** working pipeline, validated, not yet run at scale
**Last updated:** 2026-09-12
**Supersedes:** `docs/media-pipeline-plan.md` (kept for its forensic analysis of the old
library; its recommendations are outdated and partly retracted — read the banner at its top)

This document is the authoritative current state. It is written so another session, or
another person, can pick the work up cold. It records not just what was decided but **why**,
and what evidence supports each decision, because several conclusions here are
counter-intuitive and were reached only after measurement contradicted reasoning.

---

## 1. The one-paragraph version

A four-stage pipeline (`analyze → encode → verify → publish`) converts DVD and Blu-ray rips
to HEVC 10-bit MKV for Jellyfin. Every encode decision is **measured per file**, never
inferred from labels or filenames. The plan produced by `analyze` contains both the evidence
and the literal ffmpeg command, and `verify` checks the output against that plan before
anything reaches the library. Roughly 1,200 titles are queued; none have been bulk-encoded
yet. Tooling lives in `scripts/transcode/`, is plain Python 3 with zero dependencies, and
runs anywhere `python3` + `ffmpeg` exist.

---

## 2. Current state

### 2.1 Tooling

```
scripts/transcode/          ~1,570 lines, stdlib only, no install step
  __main__.py               CLI dispatcher
  analyze.py                probe sources -> plan JSON (READ-ONLY)
  encode.py                 execute a plan
  verify.py                 check output against its plan
  publish.py                move verified output into the library
  README.md
scripts/encode_audit        standalone x265-SEI fingerprinter (pre-dates the pipeline)
```

Run with `python3 scripts/transcode <stage>`. Nothing is committed to git yet.

### 2.2 Library

| Location | Files | Meaning |
|---|---:|---|
| `raw/dvd/movies` | 436 | queued |
| `raw/dvd/tv` | 751 | queued (incl. 184 Office — **drop, Blu-rays purchased**) |
| `raw/bluray/movies` | 20 | queued |
| `raw/bluray/tv` | 16 | queued |
| `movies/` | 247 | library (220 old HEVC encodes + 27 mp4 downloads) |
| `tv/` | 1,932 | library (547 old HEVC + h264/av1 downloads) |

**~1,039 titles to encode** after dropping Office.

### 2.3 Plans generated

`plans/charmed/` (173), `plans/strat/` (25), `plans/coverage/` (4).

**All are stale** — the analyzer changed after they were written. Regenerate with
`--force` before use. See §5.6 for why staleness is tracked.

---

## 3. Decisions, and the evidence for them

### 3.1 ffmpeg, not HandBrake

The old library was produced by a HandBrake bash script that silently damaged files:
28 movies with 3:2 pulldown baked in, 173 Charmed episodes at up to 32% duplicate frames.
Neither announced itself.

The decisive argument is not that ffmpeg encodes better — it's that **ffmpeg can express
exactly one decision with nothing filled in by default**, which is what makes verification
possible. You cannot assert that HandBrake's output matches a plan, because HandBrake never
tells you what it decided.

Honest cost: two of the nine bugs below (colour tagging, audio track duplication) were
things HandBrake did correctly for free.

**This only pays off because `verify` exists.** Raw ffmpeg without verification would be
strictly worse than HandBrake — all of its conveniences lost, and your own new bugs silent
instead of its.

### 3.2 Plain Python, not uv

The project has zero dependencies. A uv project meant installing uv on the encode box to run
a stdlib-only script — adding a dependency to manage the absence of dependencies. The
packaging was removed. The directory is a runnable module: `python3 scripts/transcode`.

### 3.3 Cadence: classify by DECODED rate, never the container

**This is the single most important finding in the project.**

A DVD container claiming 29.97 usually decodes at 23.976 — ffmpeg already honours the
soft-telecine flags, so the coded frames are film. Running `decimate` on such a source drops
one *real* frame in five and yields **19.2 fps**.

Both this assistant and an independent model (Gemini) initially prescribed filter chains
that would have destroyed these sources. Measurement caught it; reasoning did not.

Survey of 1,035 sources:

| Verdict | Count | Action |
|---|---:|---|
| soft telecine — already 23.976 | 840 (81%) | **no filter** |
| hard telecine — 29.97 + combing | 184 (18%) | `fieldmatch,decimate` |
| true 29.97 video | 6 | no filter |
| non-standard / changing rate | 18 | `needs_review` |

173 of the 184 hard-telecine files are Charmed. IVTC verified to recover exactly 24.00fps
with zero residual combing on both a Charmed episode and a movie (`The Deer Hunter`).

### 3.4 Crop: percentile across samples, never single-pass

`cropdetect` at ~10 intervals, take the 20th-percentile margin, discard near-black samples,
refuse width crops under 8px.

The two failure modes are **not symmetric**: under-cropping leaves black bars the player
letterboxes away; over-cropping destroys picture permanently. The percentile biases toward
keeping picture while remaining robust to a single noisy sample.

> **Retraction worth recording:** an earlier analysis claimed auto-crop had "destroyed
> picture" on 79 files. That was **wrong**. Visual inspection of the two worst cases
> (`Dr Strangelove` 668x480, `Goodfellas` 682x332) showed the crops removed **genuine black
> bars**. Dr Strangelove is a 1.66:1 film on a 16:9 anamorphic disc, so ~26px of pillarbox
> per side is correct. `cropdetect` found x=26 in 8 of 10 independent samples — a consistent
> real bar, not a dark-scene artifact. Crop pinning is worth doing for determinism, but it
> is not repairing damage.

### 3.5 Colour: always tag explicitly, inside `-x265-params`

Disc sources are frequently **untagged**. Untagged HEVC is read as BT.709, which shifts
colour badly on 480-line content that is really BT.601.

Measured, and non-obvious: ffmpeg's `-color_primaries` and `-color_trc` **never reach
libx265 at any argument position**. Only `-colorspace` survives. Colour must go inside the
x265 parameter string:

```
-x265-params aq-mode=3:no-sao=1:colorprim=smpte170m:transfer=smpte170m:colormatrix=smpte170m:range=limited
```

| Approach | Result |
|---|---|
| ffmpeg flags after `-x265-params` | `smpte170m, unknown, unknown` ✗ |
| **inside `-x265-params`** | `smpte170m, smpte170m, smpte170m` ✓ |
| ffmpeg flags before `-c:v` | `smpte170m, unknown, unknown` ✗ |

### 3.6 Preset and CRF

**DVD: `preset medium`, CRF 20. Blu-ray: `preset slow`, CRF 21.**

Two findings drove this:

**Preset does not buy quality — it buys compression efficiency.** CRF buys quality. So the
only thing `slow` purchases is a smaller file at the same quality. Measured on a Charmed
episode: `medium` CRF 18 matched `slow` CRF 19 on file size at **45% of the encode time**.

The tier assignment was originally inverted — DVD (1,187 files) got the expensive preset
while Blu-ray (36 files) got the cheap one. Flipped: cheap where it is plentiful, careful
where it is rare.

**CRF sweep, measured against an FFV1 lossless intermediate** (see §4.2 for why that matters)
on the **hardest** three minutes of a test episode, not an average one:

| CRF | Bitrate | SSIM | VMAF |
|---|---:|---:|---:|
| 18 | 3.16 Mbps | 0.9709 | 95.4 |
| **20** | **2.02 Mbps** | **0.9638** | **94.0** |
| 21 | 1.58 Mbps | 0.9603 | 93.3 |
| 22 | 1.23 Mbps | 0.9570 | 92.4 |

CRF 20 takes 36% of the size saving for a third of the quality cost.

> **Critical caveat for any future retune:** degradation is **4–5× steeper on hard content
> than on an average scene**. An initial sweep on an arbitrarily-chosen segment showed only a
> 0.3% SSIM drop from CRF 18→22 and nearly led to CRF 22. Re-running on the highest-bitrate
> segment showed 1.4%, with visible dark-scene blocking. **Always measure on a
> high-bitrate segment.** Find one by dumping per-30s bitrate from an existing encode.

Gemini independently estimated the medium→slow efficiency gap at 480p as **10–15%**, not the
1–3% assumed here — larger at low resolution, because `slow` enables `subme=3`, `rect` and
`rdoq-level=2`, which matter more when there is no pixel density to hide behind. The
conclusion still holds, because at ~2 Mbps there is enough bitrate that `medium` brute-forces
past its own algorithmic limits.

### 3.7 HEVC 10-bit, not AV1

AV1 would save 20–30% and `libsvtav1` is available. **Rejected on direct-play grounds:** AV1
hardware decode begins at A17 Pro / M-series, so older iPads would force server-side
transcoding, costing more quality than the bitrate saved. HEVC Main10 hardware-decodes on
every iPad since the A9.

Revisit later — sources are archived, so re-encoding is always available.

Hardware encoding (QSV/NVENC/VideoToolbox) rejected: meaningfully worse quality per bit than
software x265, and throughput is not the binding constraint for a one-time pass.

### 3.8 Audio

| Source | Result |
|---|---|
| Stereo, lossy | **copy** — one track |
| Stereo, lossless (e.g. PCM) | AAC 192k |
| Surround **+ disc has a stereo mix** | **copy both** — no transcoding at all |
| Surround, no stereo mix, lossy | AAC 192k downmix + copy surround |
| Surround, no stereo mix, lossless | AAC 192k downmix + **E-AC3 640k, capped at 5.1** |

Four of five branches avoid re-encoding audio entirely.

- **Lossless is transcoded, not copied.** Dune Part 1's TrueHD track was **3.77 GB of a
  6.86 GB file — larger than the video.** E-AC3 640k reduces that to ~0.75 GB.
- **7.1 is capped to 5.1.** At a fixed 640k, eight channels get ~80k each versus ~107k for
  six; if the receiver folds down to 5.1 anyway, preserving 7.1 gives *worse* sound. Only 6
  files in the library are 8-channel, and the lossless originals are archived, so going 7.1
  later is an afternoon's re-encode.
- **A disc's own stereo mix beats a synthesized downmix** — it is mixed for stereo rather
  than folded down, and copying it is free.
- **Non-English audio is dropped.** 45% of files carry it (avg 0.7 foreign tracks each).
- ffmpeg's default `-ac 2` downmix is quiet and dialogue-light; an explicit `pan` matrix is
  used when synthesizing.

### 3.9 Subtitles: English only, and **no automated burn-in**

Storage is not the issue — subtitles are <1% of output for most titles, 5.4% worst case.

Non-English tracks are dropped. One title (`007 - For Your Eyes Only`) carried 17 tracks
across Chinese, Korean, Thai, Spanish and French; it now keeps 2.

**Burn-in was explicitly rejected.** It requires detecting which subtitles are "forced", and
**zero of 305 sampled files have a forced-flagged subtitle track**. There is no signal to
automate against. An irreversible operation driven by a guess, across 1,000 files, is a bad
trade.

Discs handle foreign dialogue in at least three incompatible ways:

| Pattern | Example | Correct action |
|---|---|---|
| Forced track flagged on the disc | none found in 305 files | copy it |
| **Burned into the picture by the studio** | **Inglourious Basterds** | **nothing — just don't crop it off** |
| Only a full subtitle track | — | Bazarr forced SRT |

Inglourious Basterds is the instructive case. Its foreign-dialogue translations are burned
into the picture by the studio (confirmed: yellow text at y≈820–940, Cb ≈46 where neutral is
128). It has only one English PGS track (2,516 cues — a full SDH track, not a forced subset).
**Had a rule like "burn in the smallest English track" been applied, the film would have
ended up with two sets of subtitles on screen, permanently.**

Note the crop interaction: those burned-in subs sit inside the 2.40:1 active picture
(y=140–940), so the crop preserves them. A more aggressive crop would destroy them
irreversibly. This is a further argument for the percentile crop's bias toward keeping
picture.

**Recommended companion tool: Bazarr.** It fetches SRT (text-based, so it direct-plays
everywhere with no transcode), handles forced variants, and can use Whisper for titles with
no online subtitles. Subtitle selection at playback time is reversible and per-title;
encode time is the wrong place to make that decision.

### 3.10 Image-based subtitles and Apple clients

VobSub and PGS are pictures of text. Apple clients cannot render them, so enabling one makes
Jellyfin **transcode the whole video** to burn it in. They cost nothing when off, so they are
kept as an archival fallback — but Bazarr SRT is what should actually get used.

### 3.11 Ripping: ARM on a Debian VM, not an LXC

- MakeMKV needs **both** `/dev/srN` and `/dev/sgN`; passing only `sr0` makes DVDs work while
  Blu-ray fails.
- ARM in a Proxmox LXC requires a **privileged** container plus `lxc.apparmor.profile:
  unconfined` and no capability drops — effectively host-root.
- ARM's disc detection is a **udev rule**, and LXC containers do not run their own udevd.
- There is no official `community-scripts` ARM helper (`ct/arm.sh` 404s); its Docker script
  creates an unprivileged Debian 13 LXC with a 4 GB disk, which cannot hold a 45 GB rip.
- The drive is USB, so `qm set <vmid> -usb0 host=<vendor>:<product>` is a one-liner.

Config (verified against upstream `setup/arm.yaml`):

```yaml
RIPMETHOD: "mkv"          # required for SKIP_TRANSCODE
SKIP_TRANSCODE: true      # ARM must NOT run its own HandBrake stage
VIDEOTYPE: "auto"
AUTO_EJECT: true
MINLENGTH: "600"          # raise to ~2400-3600 for movie batches
RAW_PATH: "/home/arm/media/raw/"
COMPLETED_PATH: "/mnt/nas-ingest/_raw/"
```

MacBook is a second ripping station via a `makemkvcon` loop (ARM is Linux-only). Ripping is
optical-I/O-bound and doesn't interfere with using the laptop; encoding is CPU-bound and
stays on Proxmox.

### 3.12 Testing method: vertical slices, and measure hard content

Two process decisions that repeatedly paid off:

**Vertical slice over horizontal layers.** Running one show end-to-end (analyze → encode →
verify) found bugs that analyzing 1,039 files first would have baked into every plan.

**Stratified data slice.** After Charmed, a 25-file sample spanning 12 movies (shortest to
longest), four untested shows, Blu-ray film and TV, and known-awkward files. Four of the
first eight bugs came from Charmed alone — one show is not representative.

---

## 4. Measurement methods that are easy to get wrong

### 4.1 `strings` truncates at 1024 bytes

The x265 parameter stamp is ~2.3KB. Use `LC_ALL=C tr -c '[:print:]' '\n'` instead. This
produced a false negative during the original forensic work.

### 4.2 VMAF against an IVTC'd source is meaningless

`decimate` drops one frame per cycle based on a sliding window; two separate ffmpeg runs, or
different seek points, drop *different* frames. One frame of desync destroys every objective
metric — VMAF, SSIM, PSNR, Butteraugli alike.

Symptom: four encodes of visibly different quality all scoring ~42.8, within 0.23 of each
other, with per-timestamp swings from 26 to 58.

**The fix (credit: Gemini):** run the IVTC chain once into a **lossless FFV1 intermediate**,
then use that file as both the encode source and the metric reference. Frames are hard-baked,
so phase drift is impossible.

```bash
ffmpeg -ss 900 -t 180 -i SRC -map 0:v:0 -an -sn \
  -vf "fieldmatch,decimate,crop=..." -c:v ffv1 -level 3 -pix_fmt yuv420p10le ref.mkv
```

Also: **SSIM is more appropriate than VMAF at 480p.** The default VMAF model is trained at
1080p; SSIM is resolution-agnostic.

### 4.3 Duplicate frames must be compared to the source

`mpdecimate` measures *near*-duplicates. Static cinematography legitimately produces them —
Dune Part 1's source measures **35.4%** with nothing wrong. The question is whether the
encode *introduced* duplicates, so the source rate is the only meaningful baseline.

### 4.4 ffmpeg logs metrics at INFO level

`idet`, `ssim` and `libvmaf` all print at INFO. Passing `-v error` silently discards their
output. This bit twice.

### 4.5 Detecting burned-in subtitles

Look at **chroma, not luma**. Yellow subtitle text has luma ≈226 — close enough to bright
picture content to be dismissed — but Cb ≈46 against a neutral 128 is unmistakable.

### 4.6 ffmpeg eats stdin

Always `-nostdin`, or it will consume a calling shell loop's input.

---

## 5. Bugs found, and why each matters

All nine were found by measurement. None by reasoning.

| # | Bug | Consequence if shipped |
|---|---|---|
| 1 | Stereo sources got an AAC track **and** a copy of the same track | Lossy re-encode of lossy source, plus a duplicate track, on every stereo file (most of TV) |
| 2 | Colour tags never reached x265 | **Every file** tagged "unspecified", rendered as BT.709 — wrong colour on all SD content |
| 3 | Concurrent encodes wrote to the same scratch file | Mutual corruption once parallel encoding starts, which is the plan for 1,039 files |
| 4 | Duplicate-frame check used an absolute threshold | False failures on any slow-paced film (Dune measured 35% legitimately) |
| 5 | VMAF measured against a live IVTC filtergraph | All quality measurements invalid; nearly drove a wrong CRF choice |
| 6 | Plans had no link to the analyzer that made them | 172 of 173 Charmed plans were silently stale; would have shipped with broken colour |
| 7 | A degenerate subtitle track stalls the matroska muxer | **56 frames of video in a file reporting 84 minutes.** ffmpeg exits 0, size and duration look right. Affects **46 of 1,219 files** |
| 8 | `verify --sample` compared the sample against the source's opening credits | False failures reporting invented duplicate frames |
| 9 | Initial CRF sweep ran on an easy segment | Nearly selected CRF 22; degradation is 4–5× steeper on hard content |

### 5.1 Bug 7 in detail — the one to understand

A subtitle track containing one packet at t=0 with no duration makes the muxer wait for
packets that never arrive. It flushes, the output clock jumps ~47 minutes, and the **video
encode terminates** — while ffmpeg exits 0 and writes a file with the correct duration and a
plausible size, because the audio copied in full.

Nothing short of counting frames catches it. Not exit code, not size, not duration, not
playing the first few seconds. The old bash pipeline's 250 MB size floor would have passed
it.

Fixed by mapping subtitle streams explicitly by index and excluding degenerate ones, rather
than a blanket `-map 0:s?`.

### 5.2 Structural scan

All 1,219 raw sources were scanned for known failure signatures:

```
1,173  OK
   46  DEGENERATE_SUB   (42 Charmed, 4 Ash vs Evil Dead — all TV, zero movies)
```

No missing video, multi-video, missing audio, or bad durations.

---

## 6. Validation performed

| Test | Scope | Result |
|---|---|---|
| Cadence survey | 1,035 sources | 81/18/0.5/1.7% split; recipes verified |
| Structural scan | 1,219 sources | 46 degenerate-sub files found |
| Coverage test | 4 files, all code paths | 3 pass, 1 correctly skipped as `review` |
| Stratified sample | 25 files (12 movies, 4 shows, Blu-ray film+TV) | **25/25 analyzed, encoded, verified** |
| Charmed batch | 4 full episodes | 3 pass, 1 exposed bug 7 |
| Preset comparison | slow vs medium, full episodes | medium = same size, 45% of the time |
| CRF sweep | easy + hard segments, FFV1 reference | CRF 20 selected |

Measured encode throughput on Apple M-series (15 core): **7m40s–10m37s per 43-minute DVD
episode** at `medium` CRF 20, roughly 4–5× realtime. Charmed ≈ 25 hours single-threaded,
10–12 hours at 2–3 concurrent.

**Proxmox throughput is unmeasured** and will differ.

---

## 7. What is left

### 7.1 Immediate

1. **Regenerate all plans** — every existing plan is stale (analyzer fingerprint changed).
2. **Four full encodes** — a long movie, a Blu-ray, an untested show, a degenerate-sub file.
   Samples validate plan correctness but cannot catch mid-file failures, which is exactly how
   bug 7 presented.
3. **Drop Office** from the queue (184 files; Blu-rays purchased).

### 7.2 Proxmox migration

No macOS-specific code in the pipeline. The blocker was that plans bake in absolute paths
(`/Volumes/nas/...`); `--remap /Volumes/nas=/mnt/nas` is implemented on encode, verify and
publish, so plans generated on either machine work on both.

Verify on the Ubuntu box before bulk running:

1. ffmpeg build has `libx265`, `libvmaf`, and the `fieldmatch`/`decimate`/`idet`/`ssim`
   filters (Ubuntu packages are sometimes built without libvmaf)
2. The colour-in-`x265-params` behaviour (§3.5) still holds — it is ffmpeg-version-specific.
   Run one `--sample 60` encode and check the tags land.
3. Measure throughput; it drives the whole schedule.

All three are answered by one `--sample 60` encode plus a `verify`.

### 7.3 Publishing strategy

Do **not** encode over the live library. Stage → verify the batch → swap, preserving the
`Season NN/` structure. `publish --retire DIR` moves displaced files aside rather than
deleting them, so rollback is a `mv`.

### 7.4 Open questions

- **Per-episode crop varies within a series** (720:480 vs 720:478 vs 712:480 on Charmed).
  Per-file correct, series-wise inconsistent. A `--uniform-crop` mode would compute one value
  across a sample and apply it show-wide. Cosmetic.
- **~10 Charmed episodes show sporadic residual combing** after IVTC (3–8% of frames by
  idet). `fieldmatch=combmatch=full` fixed one episode and made three others much worse;
  adding `bwdif` made idet scores 5–8× worse, which may mean idet is being confused rather
  than the picture degrading. Unresolved; plain `fieldmatch,decimate` ships regardless since
  it is a large improvement over 26–32% duplicates.
- **15 Buffy episodes have genuine mixed cadence** and are flagged `needs_review`. IVTC would
  take them to 19.2–22.2 fps. VapourSynth VIVTC is the right tool; 15 episodes is one
  evening.
- **13 movies have no surviving source.** 4 of those are damaged (`007 - Dr. No` has no
  source but is *not* damaged — see the §3.4 retraction; the real four are `U571`,
  `Land Before Time 6`, `Land Before Time 7`, and one other). Those need a re-rip, not a
  re-encode.
- **Bazarr is not set up.** It is the recommended answer for day-to-day subtitles.

---

## 8. Quick reference

```bash
# analyze (read-only; safe to run on anything)
python3 scripts/transcode analyze /Volumes/nas/media/raw/dvd/tv/charmed \
    --out plans/charmed --jobs 4

# validate a plan cheaply - 90s from mid-file, exercises every code path
python3 scripts/transcode encode plans/charmed --out /tmp/stage --work /tmp/work \
    --flatten --sample 90

# encode for real
python3 scripts/transcode encode plans/charmed --out /tmp/stage --work /tmp/work --flatten

# verify (add --sample if the output is a sample)
python3 scripts/transcode verify plans/charmed --out /tmp/stage --flatten

# publish (runs verify itself; refuses anything that fails)
python3 scripts/transcode publish plans/charmed --from /tmp/stage \
    --to "/Volumes/nas/media/tv/kids/Charmed" --retire /tmp/retired --seasons

# on Proxmox, add:  --remap /Volumes/nas=/mnt/nas
```

Useful flags: `--force` (re-analyze), `--limit N`, `--dry-run`, `--progress`,
`--ignore-stale`, `--replace`.

### Final config

```
DVD tier      preset medium, CRF 20, BT.601 tags
Blu-ray tier  preset slow,   CRF 21, BT.709 tags
cadence       measured per file: none / fieldmatch,decimate / needs_review
crop          cropdetect x10, 20th-percentile margin, 8px width floor
audio         copy where possible; lossless surround -> E-AC3 640k, 5.1 max; English only
subtitles     English only, degenerate tracks excluded, no burn-in
provenance    analyzer fingerprint + literal argv stamped into every output
```

---

## 9. Principles, if you only read one section

1. **Decide from the file, not from a label.** Every defect in the old library traced to a
   human-supplied profile being applied to content it didn't fit.
2. **Measure; do not reason.** Nine bugs, all found by measurement. Two independent AI models
   reasoned their way to a filter chain that would have destroyed the library.
3. **Verify against an explicit plan.** This is what makes the whole approach viable — it
   turns silent corruption into a loud failure.
4. **Test on hard content, not average content.** The CRF sweep on an easy segment nearly
   picked a setting with visible artifacting.
5. **Prefer reversible decisions.** Burn-in, aggressive cropping and lossy audio re-encoding
   are permanent; subtitle selection and CRF are not. Keep the sources.
6. **A slice through the stages AND a slice through the data.** One show end-to-end found
   bugs that bulk-analysis would have baked in; a stratified sample found what one show
   could not.
