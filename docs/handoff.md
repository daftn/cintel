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

Run with `python3 cintel <stage>`. (This section's `scripts/transcode/` paths predate the
move into the `cintel` repo; the layout is otherwise unchanged.)

### 2.2 Library

Recounted on the encode box **2026-09-15** (AppleDouble `._*` sidecars excluded):

| Location | Files | Meaning |
|---|---:|---|
| `raw/dvd/movies` | 436 | queued |
| `raw/dvd/tv` | 620 | see breakdown below |
| `raw/bluray/movies` | 29 | queued (was 20; still growing as discs are ripped) |
| `raw/bluray/tv` | 16 | queued (Killing Eve S1) |
| `raw/processed/charmed` | 173 | **done** — sources moved here after publishing |
| `ingest/` | 64 | ripped, awaiting naming before analysis |

`raw/dvd/tv` breakdown:

| Show | Files | State |
|---|---:|---|
| the office | 185 | **drop** — Blu-rays purchased |
| buffy | 143 | **done**, published 2026-09-15 |
| parks and recreation | 122 | queued |
| the 100 | 99 | queued |
| the wild thornberrys | 41 | queued (S1 complete + S2 eps 1-21) |
| ash vs evil dead | 30 | queued |

**Convention:** sources move to `raw/processed/<title>/` once published, so `analyze` no
longer scans them and the queue count stays meaningful. Sources are never deleted — rule 9
depends on re-encoding remaining possible.

**Queue arithmetic as of 2026-09-15:**

```
done                    316   (Charmed 173 + Buffy 143)
queued now              773   (436 dvd movies + 292 dvd tv + 29 bd movies + 16 bd tv)
awaiting naming          64   (Dexter 33, Thornberrys S2P3+S3 31)
inbound on Blu-ray     ~480   (The Office ~200, Big Bang Theory ~280)
                     ------
remaining             ~1,317
```

The original "~1,039 titles" figure predates all of this. It excluded the Office Blu-rays,
the growing Blu-ray movie collection, and the cartoons.

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

Honest cost: two of the bugs below (colour tagging, audio track duplication) were
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
libx265 at any argument position**. Colour must go inside the
x265 parameter string:

```
-x265-params aq-mode=3:no-sao=1:colorprim=smpte170m:transfer=smpte170m:colormatrix=smpte170m:range=limited
```

| Approach | Result |
|---|---|
| ffmpeg flags after `-x265-params` | `smpte170m, unknown, unknown` ✗ |
| **inside `-x265-params`** | `smpte170m, smpte170m, smpte170m` ✓ |
| ffmpeg flags before `-c:v` | `smpte170m, unknown, unknown` ✗ |

Re-measured on Ubuntu 2026-09-12 (see §7.2). Two corrections to what was written here
before:

- An earlier draft of this section said "only `-colorspace` survives". That contradicts the
  table above and is **wrong**: it is `primaries` that survives, while transfer and matrix
  are lost. The operative conclusion — put colour inside `-x265-params` — is unchanged.
- **The failure is version-specific in a way that can mislead a re-test.** On ffmpeg 6.1.1
  (Ubuntu 24.04's stock package, x265 3.5) ffmpeg's own colour flags *do* work, so the bug
  does not reproduce at all. It reproduces exactly as tabulated on 8.1.2 and 9.0.1. Anyone
  re-testing this rule on a stock Ubuntu ffmpeg will wrongly conclude the flags are fine.
  Never relax this rule on the strength of a 6.1.1 measurement.

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

### 3.5a Irregular cadence: repair without decimating (2026-09-13)

Mixed and irregular cadence used to be refused outright — `needs_review`, no encode. On
Buffy that was **20 of 143 episodes**. Refusing is safer than guessing, but it is not better
than a treatment that provably cannot do harm, and the old pre-pipeline script had shipped
all 143 without incident.

**The insight is that `decimate` is the only destructive step.** A chain of

```
fieldmatch,bwdif=deint=interlaced        # note: NO decimate
```

reconstructs whole frames from fields where content was telecined, then deinterlaces only
what is still flagged combed (`bwdif` defaults to `mode=send_frame`, preserving frame
count). Because no frame is ever removed, applying it to already-progressive content
**cannot** produce cardinal rule 1's 19.2fps catastrophe. The worst case is wasted effort,
not destroyed picture, and that asymmetry is what makes it safe where cadence cannot be
proven.

Five cadence verdicts replace the blanket refusal. **Combing, not frame rate, decides**: if
nothing is combed there is nothing to repair, so passthrough is correct whatever the rate
reads.

| Verdict | Condition | Filter |
|---|---|---|
| `film_variable` | film-ish rates, no real combing | none |
| `mixed_variable` | rates span film and video, no combing | none |
| `film_deint` | film-ish rates WITH combing (IVTC probe returns ~19.2, proving already decimated) | `DEINT_FILTER` |
| `mixed_deint` | rates span both, combed | `DEINT_FILTER` |
| `video_deint` | combed 29.97 where IVTC does not recover film | `DEINT_FILTER` |

`review` now means only that the rate could not be measured at all.

All five set `fps_from_source`, because no constant describes their output. `verify` then
compares the output rate against the **source** rather than a constant, exactly as the
duplicate and drift checks do — the question is whether we CHANGED the rate, not what the
rate happens to be. Tolerance `VARIABLE_FPS_TOL = 0.60`: wide enough for two sampled windows
of a genuinely varying source, far too tight to hide decimation, which shows as ~-4.8fps.

**Validated on `Buffy s03e01 - Anne`**, whose combing is localised (clean at 60/540/780s,
heavily combed at ~300s, ~1020s and ~1347s):

| | interlaced | progressive | combed | fps |
|---|---:|---:|---:|---:|
| source @1347s | 1438 | 291 | **83.2%** | 30.0 |
| output | **0** | 1699 | **0.0%** | **30.0** |

Combing eliminated, frame rate preserved, `verify --sample` passes. The existing library
encode of this episode passed the combing straight through, so this is a genuine improvement
on it.

**Switched ON after two analyzer bugs were found and the owner compared the output by eye
(2026-09-14).** The history below is kept because the reasoning was wrong in an instructive
way, twice.

#### Why the chain was nearly abandoned

Two bugs made Buffy look far cleaner than it is:

1. **Combing was sampled from ONE 20s window per title.** The decoded rate was already
   sampled three times; combing was sampled once, and that single number decided the cadence
   verdict for a whole episode. On `s02e22` one window reported **3.2%**; twenty-one windows
   across the same episode reported **42.2%**, 13 of them over 10%. Combing on these discs is
   scattered, so one window is a coin flip. Now `COMBING_SAMPLES = 9`.
2. **The `film` branch never read the combing measurement at all.** If every sampled rate sat
   near 23.976 it returned `film` on the rate alone. So a soft-telecined episode carrying
   interlaced content inside its frames was declared clean: `s02e05` at **58%** combed,
   `s02e19` at 49%, `s02e06` at 44% — all classified `film`, no filter, no flag.

Together these reported **12 combed episodes**. The real figure is **58 of 143**, with a
median of 15.8% among them and 85 genuinely clean (median 0.0%). Every argument for leaving
the combing alone — "affects 8% of the series", "no defect to repair" — rested on those two
bugs.

#### What decided it

The owner watched `s02e05 Reptile Boy` (the worst episode, 40% combed at source) repaired
against unrepaired and reported the repaired version "definitely better on the lines". That
is the only evidence that settled it, because **the metrics could not separate the two**:

| version | combed (idet) | dupes | size |
|---|---:|---:|---:|
| source | 40.1% | 3.9% | 2.01 GB |
| unfiltered encode | 2.0% | 4.4% | 0.37 GB |
| deinterlaced encode | 1.3% | 5.0% | 0.36 GB |

> **`idet` on ENCODED output under-reports combing badly.** The unfiltered encode reads 2.0%
> against a 40.1% source, yet the combing is plainly visible in it. x265 alters the comb
> pattern enough to fool the detector without removing it from the picture. **Measure combing
> on the SOURCE.** Every "output combing" figure gathered before this was realised is
> unreliable, including the ones used to choose which episodes to eyeball.

#### Scope: Buffy is an outlier, not the library

A 40-title stratified sample re-analyzed with the fixed detection:

| class | titles | median combing | >10% |
|---|---:|---:|---:|
| DVD movies | 25 | 0.0% | 1 (The Road to El Dorado, 12.9%) |
| DVD TV | 13 | 0.0% | 0 |
| Blu-ray | 2 | 0.0% | 0 |

All four already-encoded diversity titles measure **0.0%** — nothing shipped is affected, and
the ~1,000-title queue needs no reconsideration. `DEINT_COMBED = True` is therefore safe
globally: the filter only attaches above 10% measured combing, which in practice is Buffy.

`verify`'s duplicate check uses `DUPLICATE_INTRODUCED_TOL_DEINT` (25%) for deinterlaced
plans, because mpdecimate counts near-duplicates and interpolated frames read as similar
whether or not anything repeats — measured +0.6% introduced on one episode and +17.8% on
another from the same filter.

#### Earlier reasoning, superseded

**The chain was measured and switched off (2026-09-14, before the bugs were found).** It removes combing
convincingly, but `verify` rejected 4 of 12 episodes for introduced duplicates:

| episode | output dupes | source dupes | introduced |
|---|---:|---:|---:|
| s03e01 Anne | 21.6% | 3.8% | **+17.8%** |
| s03e20 The Prom | 23.9% | 16.9% | +7.0% |
| s03e10 Amends | 12.3% | 5.5% | +6.8% |
| s07e16 | 21.2% | 15.1% | +6.1% |

The cause is **`bwdif`, not `fieldmatch`** — removing `fieldmatch` changes nothing, because
interpolated frames are softer and soft frames read as near-duplicates to `mpdecimate`.
Whether those are truly repeated frames or an artefact of the softening was **not
established**.

Two further measurements worth keeping, both contradicting what was first written here:

- `bwdif` defaults to **`mode=send_field`, which DOUBLES the frame rate** (50fps measured).
  `mode=send_frame` must be stated explicitly.
- `deint=interlaced` acts on the frame's **interlaced flag, not its content**. Disc rips
  rarely set it, so without `idet` in front bwdif is nearly idle — it contributed only 2.5
  points on s07e16 (20.8% → 18.3%), where adding `idet` reached 5.3%.

`DEINT_COMBED = False` in `analyze.py` turns the whole thing off, and combed files are
passed through unfiltered. The reason is outside the filter: **Buffy's existing library
encodes are already correct** (23.976, smpte170m), unlike Charmed's, which were genuinely
broken. With no defect to repair, trading sharpness and a failing verify to remove combing
from 12 of 143 episodes is gold-plating. Flip the constant to re-enable; plans record which
way it was set.

Result across Buffy: **143 of 143 now encode**, against 123 before — all unfiltered, 0
refused. The verdicts are `film_combed` / `mixed_combed` / `video_combed` rather than
`*_deint`, since nothing is being deinterlaced.

> **Honest limit.** `bwdif` interpolates the frames `fieldmatch` could not reconstruct, so
> those are softer than a true field match would be. Running `fieldmatch` first minimises how
> many frames need it. This repairs combing; it does not recover 24p from mixed content the
> way VapourSynth VIVTC would. VIVTC remains the better tool and remains unjustified — it
> would mean abandoning the zero-dependency property for what is now **zero** refused files.

### 3.6a The Blu-ray tier, measured (2026-09-13)

Everything in §3.6 was measured on a **480p Charmed episode**. The Blu-ray row of the config
— `slow`, CRF 21 — was a row lifted from that DVD sweep, never measured on Blu-ray content.
Two sweeps against lossless FFV1 references fixed that, and both findings contradict §3.6.

**Finding 1: `slow` is far more efficient than §3.6 assumes, and CRF is not comparable
across presets.** On the hardest 180s of Hot Fuzz (grainy 35mm, the hardest content in the
library), at *matched bitrate*:

| bitrate | slow VMAF | medium VMAF | slow advantage |
|---:|---:|---:|---:|
| 12.49 Mbps | 98.58 | 98.05 | +0.53 |
| 15.14 | 98.78 | 98.34 | +0.44 |
| 18.17 | 98.94 | 98.58 | +0.36 |
| 21.62 | 99.07 | 98.77 | +0.30 |

`medium` needs **+44% bitrate** to match `slow` CRF 21, and cannot reach `slow` CRF 20's
quality at any CRF in 17–22. §3.6 assumed the gap was 1–3% and treated Gemini's 10–15%
estimate as the pessimistic case; at 1080p it is ~44%. Note also that at the *same* CRF,
`slow` produces a **larger, better** file than `medium` — so the two presets cannot be
compared row-by-row, only as rate-quality curves. §3.6's "preset does not buy quality" is
the idealised statement and is misleading in practice.

Measured cost: `slow` is **3.2×** the encode time of `medium` (634s vs 201s for 180s of
1080p at CRF 20; 610s vs 188s at CRF 21).

**Finding 2: content matters more than tier.** Killing Eve encodes ~10× smaller than Hot
Fuzz at identical settings (2.10 vs 24.47 Mbps at medium CRF 20) despite having a *higher*
source bitrate (31 vs 25.6 Mbps). Clean digital TV is cheap; grainy 35mm is not.

**Resulting tiers** (see `analyze.TIERS`):

| tier | preset | CRF | evidence |
|---|---|---:|---|
| `dvd` | medium | 20 | §3.6, unchanged |
| `bluray-film` | slow | **19** | VMAF 99.07 / SSIM 0.9665 on Hot Fuzz's hardest 180s; ~12 GB/film, ~240 GB for 20 titles |
| `bluray-tv` | medium | **21** | VMAF 93.97 worst-segment on Killing Eve; ~1.74 Mbps/ep, ~143 GB for ~500 eps |

The film/tv split exists because **Blu-ray stopped being a proxy for "rare."** The Office
(~200 eps) and Big Bang Theory (~280) arriving on disc take Blu-ray TV from 16 files to
~500 — about 170 hours of content against 40 hours of Blu-ray film. Holding that tier at
`slow` would cost ~336 hours against ~116 at `medium`, for +1.4 VMAF on shows the owner has
explicitly deprioritised. This is §3.6's own "cheap where it is plentiful, careful where it
is rare," re-applied now that the inventory has changed underneath it.

> **The split is decided from the PATH, not the file** — `raw/bluray/tv` vs
> `raw/bluray/movies` — which is a deliberate exception to principle 1. The distinction is
> not a property of the content: it records how much the owner cares about a title, and no
> measurement can recover that. `resolve_tier()` returns its reason so every plan carries
> the inference as evidence. Anything outside the known layout falls to `bluray-film`, the
> careful side.

**Metrics are not comparable across content.** Killing Eve scores SSIM 0.986 with VMAF 94;
Hot Fuzz scores SSIM 0.968 with VMAF 98.9. Grain depresses SSIM at any bitrate; detail loss
depresses VMAF even where structure survives. Both are valid *within* one title's curve and
meaningless *between* titles.

**Open:** re-measure `bluray-tv` when the Office and Big Bang Theory discs arrive. Killing
Eve is a dark prestige drama and a poor proxy for brightly-lit multi-cam sitcoms.

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

- **"Lossless" is decided by profile, not codec name.** ffprobe reports `codec_name: dts`
  for both the lossy DTS core and lossless DTS-HD MA; only `profile` separates them. The
  analyzer originally treated every `dts` stream as lossless, which sent ordinary lossy DTS
  5.1 — common on DVD, a tier that cannot carry DTS-HD MA at all — through an E-AC3
  re-encode for no benefit. `analyze.is_lossless_audio()` now checks the profile, and a
  `dts` stream with no profile is treated as **lossy and copied**, that being the
  reversible choice.
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

### 3.8a A/V sync: measure DRIFT, and never on a sample with av_drift

DVD rips do go out of sync, so `verify` checks it — but the check has to separate two
different things.

**Skew already in the source is correct to preserve.** Discs are authored with small fixed
audio offsets and MakeMKV passes them through faithfully. `Charmed - s01e01` carries
**−168 ms**, and "correcting" that would be the bug. So, exactly as with duplicate frames
(§4.3), the question is whether the *encode* introduced desync, not whether any exists.

**Absolute lip-sync is not checkable** without content analysis (SyncNet-class models). No
attempt is made. What *is* cheaply checkable is **progressive drift**, which is the failure
mode that actually occurs here, because it means a frame-rate or cadence assumption is
wrong.

Two measurements, because one method does not cover both cases:

| | Method | Why |
|---|---|---|
| Full encode | `av_drift` — (a−v skew at end) − (same at start), compared against the source | A constant offset cancels; only accumulating desync survives |
| Sample | `av_span_ratio` — video timespan ÷ audio timespan | `av_drift` is **unusable** on a sample (see below) |

> **The trap, found while validating this.** A sampled encode is cut with an input `-ss`,
> which lands on the first decodable frame after the seek point. The video's first PTS can
> therefore sit hundreds of ms after the audio's — measured at **416 ms** on a test clip and
> **189 ms** on a real `--sample 90` of a Charmed episode. `av_drift` reads that head offset
> as drift and fails a perfectly good sample. This is bug 8 in a new costume, and it is why
> samples use the ratio instead: a ratio ignores the head entirely.

Validated by deliberately committing cardinal rule 1's catastrophe — `decimate` applied to
`Parks and Recreation - s01e01`, which reports 29.97 in the container but **decodes at
24.00**:

| Encode of the same source | span ratio | drift | verdict |
|---|---:|---:|---|
| correct (no cadence filter) | 0.9964 | — | pass |
| `decimate` on progressive content | **0.7972** | +24,413 ms | **FAIL** |

0.7972 is exactly the 4/5 that dropping one frame in five produces. On a full-length
episode that would be roughly eight minutes of accumulated desync.

Measured on the four real full encodes: **−9 ms introduced**, against a 100 ms tolerance.

**Then bug 11 (2026-09-13).** The first long movie and the first DTS source both failed this
check with absurd numbers — `−970,320 ms` on Goldfinger, `+1,066,699 ms` on Killing Eve.
Cardinal rule 10 applied exactly as written: the encodes were fine and the checker was
wrong. `_pts_bounds` bounded its tail probe by *packet count* (`99%+#99999`), which on
ffprobe's syntax means "from 99 seconds, 99999 packets" — so the burst stopped wherever
those packets ran out. On Goldfinger the video burst ended at 4,270s and the audio at
3,299s of a 6,600s file, and the difference became the reported drift. Charmed passed only
because 99999 packets overshoot the end of a 43-minute episode. Short-framed codecs make it
sharper still: DTS packets are 10.67 ms, so 99999 of them span 1,067s.

Fixed by seeking to `duration − 30s` and reading to EOF, bounding the window by time
instead. Re-measured on the same three encodes: **−17 ms, 0 ms, +22 ms introduced**, and
the four samples still pass — now for the right reason.

That makes **five of the last six bugs live in `verify.py`**, not in the encodes.

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

**`idet` on an ENCODED file under-reports combing.** Compression alters the comb pattern
enough to defeat the detector while leaving it visible on screen: a 40.1%-combed source
encoded unfiltered measured 2.0%. Judge combing from the SOURCE, and judge a repair by eye.

**Combing must be sampled at several points, and the `film` verdict must consult it.** Both
were bugs on 2026-09-14; see §3.5a. A single window misreported 3.2% where nine reported
42%, and the `film` branch returned on frame rate without reading combing at all.

**Seeking an AUDIO packet late in a very large MKV can take minutes.** Matroska cue points
index the video track and little else. Measured on a 62GB 4h13m file: the video tail probe
returned in **2s**, the audio one exceeded **300s**. `verify._pts_bounds` therefore bounds
each probe with `PTS_PROBE_TIMEOUT` and returns empty on timeout, so `av_drift` reports
`None` and verify says "could not compare" instead of raising - a timeout must never fail a
good encode (cardinal rule 10).

The consequence is real: **very large titles lose the A/V drift check.** Matroska's
per-track `DURATION` tag would give the answer instantly, but it is **not reliably present**
- the ffmpeg-written concatenation carries it, MakeMKV rips do not - and using tags for one
file while packet-probing the other would compare two different measurements, which is how
bug 11 happened. Every other check still runs on these titles.

On Matroska remuxes, **`pts_time` is `N/A` on a large share of video packets** — 54% of
Hot Fuzz's, which carry DTS only. Any per-packet timing or bitrate analysis must fall back
to `dts_time`, or those packets silently collapse to t=0. Caught only because the resulting
bin read 3,775 Mbps, which is impossible; a subtler error would have passed.

**Pick a tuning segment by where quality is worst, not where bitrate is highest.** At
constant CRF x265 spends bits exactly where content is hard, so the highest-bitrate segment
is often the one it handled *successfully*. Measured on Killing Eve: the top-bitrate 180s
scored VMAF 96.84 at medium CRF 21 while an average-bitrate segment scored 93.97, and
degradation across CRF 20→24 was slightly *steeper* on the cheap segment (2.11 vs 1.80
points). This does not reproduce §3.6's DVD finding; on one title it inverts it. Safest
practice is to measure both and take the worst. Note also that the source's bitrate profile
is useless for this on a near-CBR master — Killing Eve's source is flat at peak/mean 1.09
while its *encoded* profile ranges 2.75×.

In `ffprobe -read_intervals`, **`%` separates start from end — it is not a percent sign.**
`99%+#99999` means "start at 99 *seconds*, read 99999 packets", not "the last 1% of the
file". There is no percentage form; seek by time, computed from the container duration. Bug
11 lived here for a day because the wrong reading happens to give the right answer on files
short enough for the packet budget to overshoot the end.

---

## 5. Bugs found, and why each matters

All were found by measurement. None by reasoning.

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
| 10 | Every `dts` stream classified as lossless | Lossy DTS 5.1 re-encoded to E-AC3 for nothing — a gratuitous generation of loss, on a codec common across the DVD tier |
| 11 | `av_drift`'s tail probe seeked by packet count, not time | **Invented drift of up to −970 SECONDS**, failing good encodes. Passed on 43-min episodes by luck; bit the first long movie and the first DTS source |
| 12 | Combing sampled from ONE window per title | 123 of 143 Buffy episodes classified `film` (clean) when 58 were combed. One window read 3.2% where nine read 42.2% |
| 13 | The `film` verdict never read the combing measurement | Rate alone decided it, so soft-telecined episodes at 44-58% combed were declared clean and shipped unrepaired |
| 14 | Framerate asserted against a constant with no source fallback | 5 faithful, unfiltered Buffy encodes failed verify for tracking sources that are not uniformly 23.976 |
| 15 | A/V drift check failed when the SOURCE could not be measured | Would have failed all 14 Blu-ray films: sources are 20-60GB, the audio tail probe times out, and "no reference" was treated as a fault. Hot Fuzz failed at +62ms against a 100ms tolerance |
| 16 | `av_drift` compared audio streams by container position (`a:0`), not identity | False failures when a plan reorders audio: 4 of 208 Friends episodes flagged with up to −2328ms "introduced" drift; the promoted secondary track was compared against the source's different, undesynced main track. Fixed by matching `source_index` (`audio_position`) |
| 17 | `classify_cadence` only detects mixed film/video content from the 3-point decoded-fps sample | Missed a ~16s locally-progressive stretch in Friends s05e04 that the combing scan itself had flagged (one window read 52% progressive against 0% everywhere else) — the file was classified uniform `telecine`, and `fieldmatch,decimate` dropped ~225 real frames there. Still open; s05e04 was fixed by hand (whole-file `DEINT_FILTER`, no decimation) rather than by a code fix |
| 18 | `resolve_audio` trusted any secondary 2-channel English track as "the disc's own stereo mix" | **A commentary track shipped as the default stereo audio**, found by the owner mid-episode. No metadata distinguishes commentary from a genuine alternate mix — MakeMKV had even tagged one commentary track "Stereo" with `disposition.comment` unset. 27 of 226 Friends episodes hit this; all 27 were commentary. Fixed with `tracks_correlate`: cross-correlate the candidate track against the main mix, reject below 0.15. First version used max-of-2-windows and still passed 3 of the 27 - a commentary track goes quiet exactly when the commentator does, leaving only the ducked show audio, which correlates fine for that stretch (one window hit 0.21). Same trap as bug 12; fixed the same way, with the same fix: 7 windows spread across the runtime, median not max |
| 19 | `tracks_correlate` returning `None` (unmeasurable) was treated as "pass" | Fixed by 18 on Ash vs Evil Dead, then re-broken by its own edge case: 4 of 30 episodes' "English Stereo" track was a 31-packet stub covering about one second of runtime, not real audio - the same failure shape as bug 7's degenerate subtitle track, on audio instead. Correlation correctly found nothing to measure and returned `None`; the caller read `corr is not None and corr < THRESHOLD` as false and trusted the track anyway, which would have shipped near-silent audio as the default. `None` now takes the same branch as a measured failure |
| 20 | `run_vmaf` never applied the plan's own crop to the source before comparing | Found investigating a quality complaint on Bourne Ultimatum (DVD movie tier, first real use of `--vmaf` on movie content): the source still carries its full letterbox bars, the output does not, so at any non-trivial crop (Bourne's removes 26% of frame height) the two frames being "compared" show different portions of the picture stretched to fill the same box. Produced single-digit VMAF on a visually clean encode - measured by eye, frame-by-frame, before the score was trusted. Fixed by cropping the source in the filter graph before scaling, in both the SD-upscale and non-upscale branches. Validated end to end via the real `--vmaf` CLI path post-fix: Bourne Ultimatum now reads a believable 81.9 at its file midpoint, consistent with the owner's own playback impression ("way good compared to the old DVD script"). Ad-hoc deeper testing during the investigation (a hand-built lossless x265 round-trip that still read ~68 after the crop fix) never got a clean explanation and is **not** attributed to this bug - visual inspection of that exact case showed frames identical to source, so it reflects some artifact of that one-off test construction, not the real `verify.py` code path. Treat `--vmaf`'s output as directionally useful now, not yet as fully trusted as the §4.2 FFV1-reference method |

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
| **Full encodes on Ubuntu/9.0.1** | 4 real Charmed episodes + 1 sample | **5/5 verified**; −9 ms A/V drift introduced |
| **A/V sync check** | correct vs deliberately `decimate`-broken clip | ratio 0.9964 pass / 0.7972 fail |
| **Diversity set** (2026-09-13) | long DVD movie, Blu-ray film, Blu-ray TV, degenerate-sub show | 4 samples + 3 full encodes verified; exposed bug 11 |
| Preset comparison | slow vs medium, full episodes | medium = same size, 45% of the time |
| CRF sweep | easy + hard segments, FFV1 reference | CRF 20 selected |

Measured encode throughput on Apple M-series (15 core): **7m40s–10m37s per 43-minute DVD
episode** at `medium` CRF 20, roughly 4–5× realtime. Charmed ≈ 25 hours single-threaded,
10–12 hours at 2–3 concurrent.

**Proxmox throughput is unmeasured** and will differ.

---

## 7. What is left

### 7.1 Immediate (rewritten 2026-09-26)

Everything in the 2026-09-15 list is done, and considerably more besides: `bluray-film`
finished all 14 titles, three more shows landed (Ash vs Evil Dead, Parks and Recreation, the
full 194-episode Office Blu-ray "Superfan"), `bluray-tv` got its real re-measurement against
Office instead of resting on one Killing Eve episode, and the DVD queue's *movie* half has
started - the first time this pipeline has ever touched a DVD movie rather than a DVD show.

**Delivered**

| | Episodes/Titles | Notes |
|---|---:|---|
| Charmed | 173 | fixed 25.833fps timing + bt709 transfer on SD |
| Buffy | 143 | 63 episodes deinterlaced, 80 clean |
| Friends | 226 | bugs 16-18 found here (audio identity, cadence gap, commentary) |
| The 100 | 100 | 99 encoded + 1 pre-existing mp4 (s06e05) |
| Ash vs Evil Dead | 30 | bug 19 found here (degenerate-track None-handling) |
| Parks and Recreation | 122 files / 125 eps | pre-existing library was 100% macOS junk, rebuilt from raw; a real numbering error (S5's finale was mislabeled e13, duplicate-numbered against "Emergency Response") found and fixed by cross-checking Wikipedia |
| The Office (Blu-ray) | 194 files / 202 eps | full pipeline incl. a deliberate vertical-slice validation of `bluray-tv` before committing to the run; two seasons (7, 9) each had one spuriously-doubled single episode, found and fixed the same way as Parks - see below |
| `bluray-film` | 14/14 | tier complete. 9 of the 14 were later found to ship a commentary track mislabeled as the disc's stereo mix (analyzer predated the bug-18 fix) - patched with an **audio-only remux**, not a re-encode: video stream-copied untouched, bad track dropped, fresh AAC downmix built from the archived raw's lossless surround. Confirmed via `av_drift`/`av_span_ratio` that this introduced no timing drift |
| DVD movies, batch 1 | 100/436 | **first-ever DVD movie run** on this pipeline - every prior DVD batch was a TV show. Verified 100/100 PASS; stratified vertical slice (1962-2006, colour and B&W) run first per §3.12. Awaiting the owner's audience-folder sort before publish |
| DVD movies, batch 2 | 336/436 | in progress, same tier, no vertical slice needed - already validated on batch 1 |

**A recurring methodology finding, not a code bug:** the disc-order + duration-outlier method
used to spot merged double episodes (§3.12-adjacent, established on Friends/Office) has now
been wrong twice in the same specific way - a single "super-sized" episode (long, but never
split into two broadcast numbers) gets mistaken for a real two-parter because nothing in the
disc structure distinguishes them, only real episode-count knowledge does. Office S7's
`e22e23` and S9's `e16e17` were both this - fixed by cross-checking Wikipedia's actual
episode list, which also resolved two counts this project's own docs had previously flagged
as unconfirmed ("26 vs 27" for S7). Corroborate any future duration-outlier double-episode
guess against a real source before it ships, the way S3's was originally corroborated -
duration alone is not sufficient evidence, it was just the only evidence available at the
time.

**Next, in order of value**

1. **Finish DVD movies batch 2** (336 titles, ~2.3 days at last measurement), then verify,
   then the owner's audience sort, then publish.
2. **`bluray-standard`**, 26 titles staged, never analyzed. Settings measured on Fast Five
   (§3.6a) but never run on real bluray-standard content.
3. **`bluray-film`**, 5 more titles staged in `raw/bluray/movies/film/`.
4. **Big Bang Theory**, 8 discs (S1-4) sitting unnamed in `ingest/`. Same disc-order naming
   work Office needed - watch for the same spurious-double trap above. Also the next real
   test of whether `bluray-tv`'s CRF 21 is too conservative for bright multi-cam sitcoms,
   flagged but not measured during the Bourne Ultimatum VMAF investigation (bug 20).
5. **Wild Thornberrys** (DVD) - `tune=animation` still unmeasured, don't encode blind.
6. **Killing Eve** re-encode - still just a recommendation, never decided.
7. `raw/dvd/tv/the office/`, a 185-file DVD rip - almost certainly superseded by the
   Blu-ray Superfan version now finished; probably archive without encoding rather than
   duplicate the effort.

**Known open, carried forward**

- Per-episode crop varies within a series (cosmetic; `--uniform-crop` unbuilt). Measured on
  Charmed: 9 distinct crops, 6 in season 1 alone, spanning 708-720px wide.
- Plan filenames derive from the source stem, so two sources with identical basenames in
  different directories collide silently. Still zero collisions, but the `ingest/` files
  named `1.mkv` would have caused one.
- Very large titles lose the A/V drift check (§4 gotchas, Kill Bill at 62 GB).
- **`--vmaf`'s reliability is now improved but not fully trusted** (bug 20) - the crop
  mismatch is fixed and validated, but an unexplained anomaly during that investigation
  (a controlled lossless test that should have scored ~99 and didn't) was never root-caused.
  Prefer the §4.2 FFV1-reference method for any CRF/quality decision that matters.

### 7.1a Encode concurrency

`encode --jobs N` now exists (default 1). Concurrency is safe only because each encode
writes to a `.partial` keyed to a hash of its **destination** — that was bug 3, and without
it parallel encodes corrupt each other.

Sizing, on 8 physical cores: a single 480p encode cannot saturate them (WPP yields maybe
6–8 useful rows at that frame height), so 2–4 concurrent titles beat one wide encode. Past
~4 you contend on NAS reads rather than gaining throughput — the same ceiling `analyze`
hits at `--jobs 4`.

`--progress` streams raw ffmpeg output and becomes unreadable above one job.

### 7.2 Proxmox migration

No macOS-specific code in the pipeline. The blocker was that plans bake in absolute paths
(`/Volumes/nas/...`); `--remap` is implemented on encode, verify and publish, so plans
generated on either machine work on both. **The container mounts the NAS at `/nas`, not
`/mnt/nas`** — so the rewrite is `--remap /Volumes/nas=/nas`. Running `analyze` on the
encode box avoids needing it at all.

Verify on the Ubuntu box before bulk running:

1. ~~ffmpeg build has `libx265`, `libvmaf`, and the `fieldmatch`/`decimate`/`idet`/`ssim`
   filters~~ — **done 2026-09-12**, see §7.2.1
2. ~~The colour-in-`x265-params` behaviour (§3.5) still holds~~ — **done 2026-09-12**,
   and the result changed what §3.5 says; read the correction there
3. ~~Measure throughput~~ — **done 2026-09-12**, see §7.2.2.

#### 7.2.1 Toolchain on the Ubuntu box (resolved 2026-09-12)

The stock Ubuntu 24.04 ffmpeg is **6.1.1 with x265 3.5 (2021)**, and the suspicion in item 1
was right: **libvmaf is not packaged in Ubuntu 24.04 at all** — not in main, universe or
multiverse, so apt cannot supply it by any route.

That mattered less than what the check turned up alongside it. The Mac this pipeline was
built and tuned on runs **ffmpeg 9.0.1**. Bulk-encoding here on the stock package would have
applied a CRF 20 tuning validated against **x265 4.2** to a five-year-older encoder —
silently, since nothing in the pipeline recorded the encoder version.

Resolved by installing the static BtbN **n9.0.1** build (x265 4.2) to `/usr/local/bin`,
which precedes `/usr/bin` on PATH. cintel calls bare `ffmpeg`/`ffprobe`, so it picks this up
with no code change; apt's 6.1.1 remains installed underneath and rollback is deleting two
files. A static build was preferred over the savoury1 PPA, which would upgrade system
libraries wholesale on a machine whose only job is encoding.

Verified on n9.0.1 by running the full pipeline against a synthetic DVD-shaped title:

| Check | Result |
|---|---|
| analyze → encode → verify | PASS, no code changes |
| Colour tags on output (rule 2) | `smpte170m/smpte170m/smpte170m` ✓ |
| ffmpeg's own colour flags (control) | `smpte170m/unknown/unknown` — failure reproduced |
| x265 SEI stamp size (rule 3) | **2,303 bytes**, confirming `strings` would truncate it |
| `libvmaf` through `verify --vmaf` | works; scored 95.83 |
| `fieldmatch`/`decimate`/`idet`/`ssim`/`mpdecimate`/`cropdetect` | all present |

**Provenance now records the toolchain.** Plans carry `ffmpeg_version` and `x265_version`
as evidence of what *measured* the source; `encode` separately stamps `ENCODE_FFMPEG` and
`ENCODE_X265` for the toolchain that actually *produced* the output, since a plan may be
generated on one machine and run on another. x265's version was always recoverable from the
SEI stamp, but that costs a bitstream extraction to read; a container tag is far cheaper to
query across a library.

#### 7.2.2 Measured throughput (2026-09-12)

On CT 101 (Ryzen 7 8845HS, 8 physical cores), `medium` / CRF 20, real Charmed episodes
(43.5 min, hard telecine so `fieldmatch,decimate` is in the chain — the expensive path):

| Concurrency | Per episode | Realtime factor | Aggregate |
|---|---:|---:|---|
| `--jobs 1` | ~8.5 min | 5.1× | 1 episode / 8.5 min |
| `--jobs 3` | **4.88 min** | **8.9×** | 3 episodes / 14m38s |

Three-way concurrency buys **1.74×**, not 3×, because a single 480p encode already pulls
~7.3 of the 8 physical cores (measured). The three jobs finished within 18s of each other,
so nothing was starved. CPU held ~90% at a sustained 4,116 MHz all-core — above the 3.8 GHz
base clock, i.e. no thermal throttling on this mobile-class part.

The `--jobs 1` figure is reconstructed from file timestamps (the run's log was lost) and is
good to about ±1 min; the `--jobs 3` figure is precise.

Projected for the queue — 567 TV episodes + 436 movies + 36 Blu-rays = **1,749
episode-equivalents** by runtime:

```
~142 hours = 5.9 days continuous at --jobs 3
             ~10 days at 60% duty (overnight / idle-only)
```

Treat as an order-of-magnitude figure: it extrapolates from TV episodes, and movies and
Blu-rays are not simply longer episodes.

Output size, measured on four episodes: **1.58 GB → 371–479 MB**, roughly **3.5:1**. Across
the 3.8 TB of `raw/`, that projects to ~1.1 TB of output against 3.3 TB free on the NAS.

Run encodes under `nice -n 15 ionice -c3` — Jellyfin (CT 100) and Plex (CT 103) share the
host. Note `nice` is *relative* to the parent, so the observed niceness may exceed what was
asked for.

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
  adding `bwdif` made idet scores 5–8× worse. Shipped as-is 2026-09-15 with plain
  `fieldmatch,decimate`, a large improvement over 26–32% duplicates.
  **The suspicion that "idet is being confused rather than the picture degrading" was
  later confirmed** — see §3.5a: `idet` on an encoded file under-reports combing badly, so
  these 3–8% figures are not comparable to source measurements and may understate or
  overstate what is visible. If this is revisited, measure the SOURCE and judge the repair
  by eye.
- ~~**15 Buffy episodes have genuine mixed cadence** and are flagged `needs_review`~~ —
  **resolved 2026-09-15.** The count was wrong (two analyzer bugs, §3.5a); the real figure is
  58 episodes above 10% combing out of 143. All 143 now encode, and 63 are deinterlaced with
  a chain that never decimates, so the 19.2fps failure is structurally impossible.
  VapourSynth VIVTC would still recover true 24p from genuinely mixed content and remains
  the better tool — but with zero refused files it is no longer justified against the
  zero-dependency property.
- **13 movies have no surviving source.** 4 of those are damaged (the real four are `U571`,
  `Land Before Time 6`, `Land Before Time 7`, and one other) - those need a re-rip, not a
  re-encode. `007 - Dr. No` was previously listed here as sourceless; it is not - its raw
  sat in `raw/dvd/movies/` and was analyzed, encoded, and verified clean in the first DVD
  movie batch (2026-09-26), part of a stratified vertical slice specifically chosen for its
  age. Corrected here rather than left to mislead the next reader.
- **Bazarr is not set up.** It is the recommended answer for day-to-day subtitles.

---

## 8. Quick reference

Paths below are the **Proxmox container** (CT 101): NAS at `/nas`, NVMe scratch at
`/data`. On the Mac they were `/Volumes/nas` and `/tmp`.

```bash
# analyze (read-only; safe to run on anything)
python3 cintel analyze /nas/media/raw/dvd/tv/charmed --out plans/charmed --jobs 4

# validate a plan cheaply - 90s from mid-file, exercises every code path
python3 cintel encode plans/charmed --out /data/cintel-stage --work /data/cintel-work \
    --flatten --sample 90

# encode for real - nice/ionice because Jellyfin and Plex share this host
nice -n 15 ionice -c3 python3 cintel encode plans/charmed \
    --out /data/cintel-stage --work /data/cintel-work --flatten --jobs 3

# verify (add --sample if the output is a sample)
python3 cintel verify plans/charmed --out /data/cintel-stage --flatten

# publish (runs verify itself; refuses anything that fails)
python3 cintel publish plans/charmed --from /data/cintel-stage \
    --to "/nas/media/tv/kids/Charmed" --retire /data/cintel-retired --seasons

# only if a plan was written on the Mac:  --remap /Volumes/nas=/nas
```

Useful flags: `--force` (re-analyze), `--limit N`, `--dry-run`, `--progress`,
`--ignore-stale`, `--replace`, `--jobs N`.

### Final config

```
DVD tier      preset medium, CRF 20, BT.601 tags
Blu-ray tier  preset slow,   CRF 21, BT.709 tags
cadence       measured per file: none / fieldmatch,decimate / needs_review
crop          cropdetect x10, 20th-percentile margin, 8px width floor
audio         copy where possible; lossless surround -> E-AC3 640k, 5.1 max; English only
subtitles     English only, degenerate tracks excluded, no burn-in
provenance    analyzer fingerprint + literal argv + ffmpeg/x265 version stamped in
toolchain     ffmpeg n9.0.1 / x265 4.2 (static, /usr/local/bin) - NOT apt's 6.1.1
```

---

## 9. Principles, if you only read one section

1. **Decide from the file, not from a label.** Every defect in the old library traced to a
   human-supplied profile being applied to content it didn't fit.
2. **Measure; do not reason.** Fifteen bugs, all found by measurement. Two independent AI models
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
