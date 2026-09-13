# Media Encoding Pipeline — Design & Migration Plan

**Status:** Draft for review
**Date:** 2026-09-11
**Repo:** `~/code/homelab`
> ## SUPERSEDED — see `docs/pipeline-handoff.md`
>
> This document is the original design plan plus the forensic analysis of the old library.
> Its evidence sections are still useful history; its recommendations are outdated and several
> are retracted. **`docs/pipeline-handoff.md` is the authoritative current state.**
>
> ## ⚠ CORRECTIONS PENDING — read before executing anything
>
> External review plus follow-up measurement invalidated parts of this document. **Do not run the
> §5.1 DVD command as originally written.** Known-wrong items, pending a v2 rewrite:
>
> 1. **§5.1 / §4.5 cadence handling was destructive.** Measured: archive DVD sources already
>    decode at 23.976 (ffmpeg honors the soft-telecine flags); the 29.97 is a container-level
>    claim only. Both `decimate` and `fieldmatch,decimate` therefore drop 1 real frame in 5,
>    yielding **19.2 fps**. ~94% of sampled sources need **no cadence filter at all**. Corrected
>    inline below.
> 2. **§5.1 / §5.2 omit colorimetry tags.** DVD sources are *untagged*; ffmpeg would emit untagged
>    HEVC, which decoders read as BT.709 → shifted colors. Must tag explicitly.
> 2b. **§2.3 "auto-crop is destroying picture" is WRONG — retracted.** Visual inspection of the two
>    worst cases (`Dr Strangelove` 668x480, `Goodfellas` 682x332) shows the crops removed **genuine
>    black bars**, not picture. Dr Strangelove is a 1.66:1 film on a 16:9 anamorphic disc, so ~26px
>    of pillarbox per side is correct. `cropdetect` found x=26 in 8 of 10 independent samples — a
>    consistent real bar, not a dark-scene artifact. The crop figures throughout §2.3 and the
>    "7 badly cropped files" item in §8 should be disregarded. Pinning crop is still worth doing for
>    determinism and reproducibility, but it is not repairing damage.
> 3. **§5.4 subtitle policy is wrong for Apple clients.** They cannot direct-play image-based
>    subs (VobSub/PGS); enabling one triggers a burn-in transcode. Requires OCR to SRT.
> 4. **§4.5 / §4.7 VMAF is invalid at 480p** without scaling both inputs to 1080p first.
> 5. **§5.1 `-af:a:0` silently applies to all audio streams.** Use `-filter:a:0`.
> 6. Minor: `yadif` → `bwdif`; E-AC3 768k → 640k; drop `-r 24000/1001`; AAC 160k → 192k.
>
> §9 items 1 and 3 are now resolved by measurement. Everything else in this document still stands.

**Author note:** This document is intended for independent technical review. Claims are tagged
`[VERIFIED]` (measured directly during analysis, evidence included), `[INFERRED]` (reasoned from
evidence but not directly tested), or `[UNVERIFIED]` (assumed, needs checking). Section 9 lists
the specific things most likely to be wrong. Please challenge those first.

---

## 0. Purpose and scope

Design a repeatable pipeline that takes physical DVD and Blu-ray discs and produces high-quality,
direct-play-friendly MKV files on a NAS for Jellyfin, while keeping the original rips archived.

Two goals, sometimes in tension:

1. **Storage efficiency** — ~500 titles, growing.
2. **Playback compatibility** — must direct-play on Jellyfin clients including older iPads,
   without server-side transcoding.

Secondary goal: the pipeline must record what it did, so that "which files are stale?" is a
metadata query and not a forensic exercise. (This requirement exists because answering that
question for the current library required extracting x265 SEI data from 491 files — see §2.1.)

---

## 1. Current state

### 1.1 Infrastructure

| Component | Detail |
|---|---|
| Encode/rip host | Proxmox machine (target for ARM VM + encoding) |
| Secondary rip station | Older MacBook (used opportunistically while working) |
| Analysis workstation | Apple Silicon Mac, 15 cores |
| Storage | NAS mounted at `/Volumes/nas` |
| Optical drive | External USB Blu-ray drive |

NAS layout (relevant subset):

```
/Volumes/nas/media/
  archive/        205 original source rips (pre-encode)
  encoded/        13 files (TV, appears to be a staging leftover)
  ingest/         dex/ (1.mkv … 8.mkv, raw untitled MakeMKV output)
                  dvd_ingest/{action,clean,gritty}/   <- profile-named folders, see §3
  movies/         no_kids/ older_kids/ younger_kids/ comedy_specials/(empty)
  tv/
  youtube/
```

Tooling present on the analysis Mac `[VERIFIED]`:
`ffmpeg`/`ffprobe` (with `libvmaf`, `libx265`, `libsvtav1`), `python3` 3.14.7, `jq`, `sqlite3`.
Absent: `mediainfo`, `mkvmerge`, `HandBrakeCLI`, `makemkvcon`, `go`, `ab-av1`.

### 1.2 Library inventory

491 video files under `/Volumes/nas/media/movies`, classified by bitstream fingerprint `[VERIFIED]`:

| Class | Count | Bitstream signature |
|---|---:|---|
| Current script (v3.0) | 5 | `no-sao`, `aq-mode=3` |
| Pre-v3.0, grain tune (`-g`) | 131 | `no-sao`, `rc-grain`, `aq-mode=0`, preset medium |
| Pre-v3.0, plain | 84 | `sao`, `aq-mode=2`, preset medium (1 outlier at preset slow) |
| Never encoded | 271 | 230 mpeg2video raw rips, 33 h264 mp4, 1 audio-only, rest misc |
| **Total** | **491** | |

By destination folder:

| Folder | Encoded (old) | Never encoded | v3.0 |
|---|---:|---:|---:|
| `no_kids` | 66 | 247 | 1 |
| `older_kids` | 56 | 8 | 4 |
| `younger_kids` | 93 | 16 | 0 |

**215 files need re-encoding. 271 have never been encoded at all.**

All sources are NTSC. No PAL (25fps / 576-line) content found `[VERIFIED]`.

### 1.3 Encode script history

`scripts/encode`, six commits:

| Commit | Date | Key change |
|---|---|---|
| `e5f62a7` | 2026-03-27 | `--encoder-preset slow` |
| `2f53870` | 2026-03-28 | preset → `medium` |
| `daa02f5` | 2026-04-13 | — |
| `ff4c708` | 2026-04-21 | `--encoder-tune grain` moved into profile logic |
| `e394561` | 2026-04-22 18:30 | **v3.0 rewrite**: profile engine, `--encopts="no-sao=1:aq-mode=3"`, `--deinterlace=yadif` |
| `5f21083` | 2026-04-22 18:45 | `--deinterlace=yadif` → `--deinterlace=mode=1` |

All five v3.0 output files have mtimes after 18:45 on 2026-04-22, so they were produced by the
current HEAD version, not the intermediate one `[VERIFIED]`.

---

## 2. Forensic findings

### 2.1 Method — x265 SEI fingerprinting

x265 writes its full resolved parameter list into an SEI NAL at the head of the bitstream.
Extraction:

```bash
ffmpeg -v quiet -i FILE -map 0:v:0 -c copy -bsf:v hevc_mp4toannexb -frames:v 2 -f hevc - \
  | LC_ALL=C tr -c '[:print:]' '\n' | grep -m1 'x265.*options:'
```

**Do not use `strings` for this** — it truncates at 1024 bytes and the option string is ~2331
bytes, cutting off `crf` and `aq-mode`. This produced a false negative during analysis `[VERIFIED]`.

Discriminator: v3.0 is the first version passing `--encopts`, and x265 defaults to `aq-mode=2`
(or `aq-mode=0` under `tune grain`). Therefore `aq-mode=3` is producible only by v3.0. Confirmed
against all six historical script versions `[VERIFIED]`.

A working audit tool implementing this lives at `scripts/encode_audit` (uncommitted).

### 2.2 Finding 1 — 3:2 pulldown baked into 28 files `[VERIFIED]`

28 of 220 HEVC encodes output at 29.97fps instead of 23.976.

Duplicate-frame measurement (90s sample at t=600s, `mpdecimate`):

| File | Output fps | Frames in | After dedup | Duplicates |
|---|---|---:|---:|---:|
| `Bourne Identity.mkv` | 30000/1001 | 2698 | 2059 | **23.7%** |
| `Black Hawk Down.mkv` | 30000/1001 | 2698 | 2135 | **20.9%** |
| `Bourne Ultimatum.mkv` | 24000/1001 | 2158 | 2149 | 0.4% |

Source analysis (`/Volumes/nas/media/archive/Black Hawk Down.mkv`):

- `mpeg2video`, 720x480, `field_order=tt`, 29.97fps
- `idet`: **960/960 frames Progressive**, `Repeated Fields: Neither: 960`
- `fieldmatch,decimate`: 960 frames → 768 frames = ratio **exactly 0.800**

**Interpretation:** the source is 23.976 film padded to 29.97 with whole duplicated progressive
frames — not interlaced combing. This is critical because it means `--comb-detect --decomb`
correctly does nothing (there is no combing to detect). Only decimation removes the duplicates.

Root cause, two paths:

1. The `clean` profile has **no `--detelecine` at all**, so telecined DVDs are never decimated.
2. The `action` profile *does* have `--detelecine`, and still failed. `[INFERRED]` `--rate auto`
   resolves to the source's 29.97, and `--cfr` then re-pads the decimated output back to 29.97.
   Supporting evidence: within the same batch and profile, Bourne Ultimatum/Legacy (soft-telecined
   sources, seen by HandBrake as 23.976) came out correct, while Identity/Supremacy
   (hard-telecined, seen as 29.97) came out wrong.

**Cost:** ~20% of the bitrate spent on duplicate frames, plus baked-in judder, on 28 films.

> Note: this mechanism was not directly tested — HandBrakeCLI is not installed on the analysis
> machine. See §9.1.

### 2.3 Finding 2 — unpinned auto-crop is destroying picture `[VERIFIED]`

HandBrake CLI auto-crops by default; the script never pins it. Width distribution across HEVC
encodes (source width is 720 for all DVDs, 1920 for Blu-ray):

```
668(1) 676(1) 678(1) 680(1) 682(1) 704(1) 706(5) 708(2) 710(8)
712(9) 714(8) 716(13) 718(26) 720(127) | 1874(1) 1918(1) 1920(14)
```

79 of 220 files were cropped horizontally (127 retained the full 720 width, 14 the full 1920).
Worst cases:

| File | Result | Lost |
|---|---|---|
| `Dr Strangelove.mkv` | 668x480 | 52px width |
| `Goodfellas (1990).mkv` | 682x332 | 38px width |
| `007 - Dr. No (1962).mkv` | 676x478 | 44px width |
| `Tarzan.mkv` | 678x478 | 42px width |
| `Shaun of the Dead (2004).mkv` | 1874x820 | 46px width |

`[INFERRED]` Single-pass `cropdetect` on a dark scene over-crops. `Dr Strangelove` is
black-and-white and heavily dark-graded, which fits. Vertical crops also vary by 2px across
otherwise identical sources, producing inconsistent framing library-wide. All irreversible.

### 2.4 Finding 3 — v3.0 silently dropped all subtitles `[VERIFIED]`

The pre-v3.0 script probed for forced subtitle tracks and either burned them in or kept English
tracks. v3.0 passes **no subtitle flags at all**, so HandBrake includes none. All 5 v3.0 files
have zero subtitle streams.

Impact: foreign-language dialogue is unreadable (Dune/Fremen, Bond, LOTR/Elvish,
Inglourious Basterds). `[INFERRED]` this was collateral damage from the rewrite, not a decision.

Note: subtitle count is **not** a usable discriminator for script version — 82 pre-v3.0 files also
have zero subtitle tracks (sources lacking English subs, or forced subs burned in). Only
`aq-mode=3` is reliable.

### 2.5 Finding 4 — `--encopts` overrides `--encoder-tune grain` `[VERIFIED]`

HandBrake applies `--encopts` after `--encoder-tune`, so `aq-mode=3` overwrites what the grain
tune set. Direct A/B from the library:

| Group | aq-mode | aq-strength |
|---|---:|---:|
| 131 pre-v3.0 files with `-g` | 0 | 0.00 |
| `Vertigo.mkv` (v3.0 `gritty`) | **3** | **1.00** |

`tune grain` deliberately sets `aq-mode=0` so bits distribute uniformly and grain survives in flat
areas. `aq-mode=3` redistributes away from exactly those regions, partially defeating the profile's
purpose. `no-sao=1` is also redundant under `tune grain`, which already disables SAO.

### 2.6 Finding 5 — audio dominates Blu-ray file size `[VERIFIED]`

`Dune - Part 2 (2024).mkv`, 6.86 GB total, per-stream packet sums:

| Stream | Size | Share |
|---|---:|---:|
| Video (HEVC) | ~2.90 GB | 42% |
| TrueHD 7.1 (passthrough) | **3.77 GB** | **55%** |
| AAC stereo | 0.19 GB | 3% |

`--aencoder av_aac,copy` passes lossless TrueHD/DTS-HD through on every Blu-ray. ~19 titles in the
library carry `truehd` or `dts` tracks. `[INFERRED]` Transcoding to E-AC3 5.1 @ 768k would reduce
that track to roughly 0.9 GB, saving ~3 GB/title, with the lossless original retained in `archive/`.

Blu-ray video bitrates for comparison:

```
Army of Darkness      15.87 Mbps   (grain tune)
Shaun of the Dead     13.09 Mbps   (grain tune)
2 Fast 2 Furious      13.30 Mbps   (grain tune)
Hot Fuzz              12.04 Mbps   (grain tune)
Furious 7              7.81 Mbps   (no grain)
Dune - Part 2          5.93 Mbps   (grain tune)
Dune - Part 1          5.79 Mbps   (grain tune)
```

`[INFERRED]` The grain tune roughly doubles Blu-ray output size. Combined with `clean` forcing
`PRESET=fast` for Blu-ray (a further ~15–20% size penalty at equal quality), this is the wrong
lever for a permanent library — preset is a one-time CPU cost, file size is permanent.

### 2.7 Other defects in the current script

| # | Issue | Severity |
|---|---|---|
| 1 | `action` profile runs `--deinterlace=mode=1` ungated by `--comb-detect`, so yadif processes every frame including progressive ones (detail loss). `clean` correctly gates with `--comb-detect`. | High |
| 2 | Validation is `MIN_SIZE` only (250MB Blu-ray / 50MB DVD). A truncated 20-minute fragment passes. Should compare output duration to source. | High |
| 3 | `mv` from NVMe scratch to NAS is cross-filesystem (a copy). Interrupted, it leaves a partial file that the `[[ -f "$FINAL_PATH" ]]` skip check then treats as complete, permanently. | High |
| 4 | Source is archived (`mv`) *before* the encode is safely on the NAS. Trap firing in that window loses both copies. | Medium |
| 5 | `--audio 1,1` assumes track 1 is correct; no `--audio-lang-list`. A foreign dub or commentary in slot 1 wins silently. | Medium |
| 6 | Cleanup trap runs `rm -f "$SCRATCH_DIR"/*.*`, destroying a concurrent run's files. | Medium |
| 7 | HandBrakeCLI output no longer piped to the log (`| tee -a "$LOG_FILE"` dropped in v3.0), so encoder diagnostics are lost. `PIPESTATUS[0]` is now vestigial. | Low |
| 8 | `--encoder-profile main10` redundant with `--encoder x265_10bit`. | Cosmetic |
| 9 | Log path `/var/log/handbrake_pipeline.log` requires root. | Low |

---

## 3. Design principles

These are derived directly from the findings above and should be treated as the load-bearing
decisions of the design.

**P1. Decide from the file, not from a human label.**
Every P1 defect traces to this. `-p clean` on a telecined disc has no detelecine. The four Bourne
films received an *identical* label in the *same batch* and split 2/2 on output framerate because
the label described intent and the discs differed in structure.

Corollary: `ingest/dvd_ingest/{action,clean,gritty}` — which currently exists — is the `-p` flag
re-encoded as a filesystem path, and reproduces the same failure. Folder structure should carry
only what cannot be measured.

| Cannot be measured → path | Measurable → never in the path |
|---|---|
| Destination library (audience) | Source class (height ≤576 ⇒ DVD) |
| Movie vs TV episode | Telecine cadence |
| Title / year | Crop |
| Season / episode numbering | Grain, interlacing, audio layout, CRF |

**P2. Stamp provenance into the output.**
The entire forensic exercise in §2.1 existed because the script left no record. Every output must
carry script version, git SHA, profile, source hash, and the literal command line used.

**P3. Verify against the plan, not against a size floor.**
A 20%-duplicate-frame encode passed validation happily. Assertions must target the properties
actually cared about.

**P4. Make every implicit default explicit.**
Two of three P1 defects came from HandBrake defaults that were never stated (auto-crop) or flag
interactions that were never examined (`--rate auto` + `--cfr`).

**P5. Never destroy the source until the output verifies.**
The existing `archive/` policy is why this situation is recoverable at all. Formalize it.

---

## 4. Target architecture

### 4.1 Topology

```
  MacBook (USB BD drive) ── makemkvcon loop ──┐
                                              ├──▶ /nas/media/ingest/_raw/
  Proxmox VM (USB BD drive) ── ARM ───────────┘            │
                                                           │  (human: one drag + rename)
                                                           ▼
                                            /nas/media/ingest/movies/<audience>/Title (Year)/
                                                           │
                                        ┌──────────────────┴──────────────────┐
                                        │  Proxmox encode host                │
                                        │  analyze → encode → verify → publish│
                                        └──────────────────┬──────────────────┘
                                                           ▼
                                     /nas/media/movies/<audience>/   + archive/ source
```

Rationale for the split: **ripping is optical-I/O-bound** (DVD 20–40 min, Blu-ray 1–2 h, near-zero
CPU) and does not interfere with using the MacBook. **Encoding is CPU-bound** and stays on Proxmox.
Two drives feeding one queue roughly doubles ingest throughput.

### 4.2 Stage 1 — Ripping on Proxmox (ARM)

**Decision: full Debian VM with USB passthrough, not an LXC container.**

Reasoning:

- `[VERIFIED]` ARM in a Proxmox LXC requires a **privileged** container plus
  `lxc.apparmor.profile: unconfined` and `lxc.cap.drop:` (nothing dropped) — effectively
  host-root-equivalent. Unacceptable for a host running other services.
- `[VERIFIED]` MakeMKV needs **both** `/dev/srN` and `/dev/sgN` (SCSI generic, for raw SCSI
  commands and Blu-ray decryption). Passing only `sr0` makes DVDs work while Blu-ray fails.
- `[INFERRED]` ARM's disc detection is a **udev rule**. LXC containers do not run their own udevd —
  they share the host kernel and see devices via bind mounts, so the rule must live on the Proxmox
  host and reach into the container. A VM has real udev.
- `[VERIFIED]` There is no official `community-scripts` ARM helper script (`ct/arm.sh` and
  `ct/automatic-ripping-machine.sh` both return HTTP 404). The `community-scripts` Docker script
  creates an **unprivileged Debian 13 LXC, 2 cores / 2 GB / 4 GB disk** — it installs Docker but
  does nothing about device passthrough, and 4 GB of disk cannot hold a 45 GB Blu-ray rip.
- The drive is USB, which makes VM passthrough a single command.

**Host setup:**

```bash
lsusb                                     # e.g. ID 13fd:3940 Initio Corporation
qm set <vmid> -usb0 host=13fd:3940,usb3=1 # bind by vendor:product, not port
```

Binding by vendor:product (rather than bus/port) means the drive still works if moved between
ports, or taken to the MacBook and back.

**VM spec:** Debian 13, 2 vCPU, 4 GB RAM, **150 GB disk**. The disk is working space: ARM rips to a
local `RAW_PATH` before moving to the final path, and Blu-ray rips are routinely 30–45 GB.

**In the VM:**

```bash
lsscsi -g                                  # must show BOTH /dev/srN and /dev/sgN
curl -fsSL https://get.docker.com | sh
# mount NAS ingest share at /mnt/nas-ingest (NFS or SMB via fstab)

sudo useradd -m arm && sudo usermod -aG cdrom,video,render arm
sudo -u arm mkdir -p /home/arm/{logs,music,media,config}

docker run -d \
  --name arm-rippers \
  --device="/dev/sr0:/dev/sr0" \
  --device="/dev/sg0:/dev/sg0" \
  -p 8080:8080 \
  -v "/home/arm:/home/arm" \
  -v "/home/arm/logs:/home/arm/logs" \
  -v "/home/arm/media:/home/arm/media" \
  -v "/home/arm/config:/etc/arm/config" \
  -v "/mnt/nas-ingest:/mnt/nas-ingest" \
  -e ARM_UID="$(id -u arm)" -e ARM_GID="$(id -g arm)" \
  -e TZ="America/Denver" \
  automaticrippingmachine/automatic-ripping-machine:latest
```

Web UI on `:8080`; first-run wizard at `/setup`; default credentials `admin` / `password` — change
immediately.

**Critical config** in `/home/arm/config/arm.yaml` (key names `[VERIFIED]` against
`setup/arm.yaml` upstream):

```yaml
RIPMETHOD: "mkv"          # REQUIRED for SKIP_TRANSCODE to function (also the default)
SKIP_TRANSCODE: true      # disables ARM's built-in HandBrake stage entirely
VIDEOTYPE: "auto"         # ARM determines movie vs series from metadata lookup
AUTO_EJECT: true
MINLENGTH: "600"          # seconds; see note below
RAW_PATH: "/home/arm/media/raw/"            # local scratch on the VM disk
COMPLETED_PATH: "/mnt/nas-ingest/_raw/"     # output where the analyzer looks
```

`SKIP_TRANSCODE: true` is the essential setting — ARM's default flow runs HandBrake over every
ripped track with fixed settings, which is precisely the failure mode documented in §2.

`MINLENGTH` notes: default 600s (10 min). Leave at 600 for TV discs, since dropping a real episode
is worse than picking up a stray trailer. Raise to ~2400–3600 for movie batches to skip menus and
extras. Upstream config comments warn that with `SKIP_TRANSCODE` the **largest file** is treated as
the main feature, and recommend setting `EXTRAS_SUB` to something other than `None` to avoid losing
tracks.

**MakeMKV licensing:** Blu-ray requires the free beta key (expires roughly monthly) or a purchased
license. Configured through the ARM UI / MakeMKV settings inside the container, not `arm.yaml`
`[UNVERIFIED — exact location not confirmed]`. Existing library rips were made with
`libmakemkv v1.9.9 darwin(x86-release)`, so any prior key is long expired.

**Acceptance test:** insert a throwaway DVD. Expect: disc identified and named, MakeMKV rips, disc
ejects, named files appear in `/mnt/nas-ingest/_raw/`, and **no HandBrake process ever starts**. If
transcoding runs, `SKIP_TRANSCODE` did not take or `RIPMETHOD` is not `mkv`.

### 4.3 Stage 1b — Ripping on the MacBook

ARM is Linux-only (udev dependency), so the MacBook uses `makemkvcon` directly. This is proven
ground: the existing library was ripped this way (`libmakemkv … darwin`).

```bash
#!/bin/bash
MKV="/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"
DEST="/Volumes/nas/media/ingest/_raw"

while true; do
  if "$MKV" -r --cache=1 info disc:0 2>/dev/null | grep -q '^DRV:0,2'; then
    LABEL=$("$MKV" -r --cache=1 info disc:0 | awk -F'"' '/^DRV:0,2/{print $(NF-1)}')
    OUT="$DEST/${LABEL:-disc_$(date +%s)}"
    mkdir -p "$OUT"
    "$MKV" --robot --minlength=3600 mkv disc:0 all "$OUT"   # 1200 for TV
    drutil eject
  fi
  sleep 30
done
```

`[UNVERIFIED]` The `DRV:` line field layout should be confirmed against actual
`makemkvcon -r info` output before relying on the label parsing.

Limitation: MacBook rips do not get ARM's metadata identification — only the disc volume label
(e.g. `DUNE_PART_TWO`). That is sufficient for the human routing step in §4.4.

### 4.4 Stage 2 — Ingest and routing

```
/nas/media/ingest/
  _raw/                                  <- both rippers write here
  movies/
    no_kids/Alien (1979)/
    older_kids/Dune - Part 2 (2024)/
    younger_kids/...
  tv/
    The 100/Season 01/
```

The only human action in the entire pipeline: move a title from `_raw/` into the correct
destination folder and give it a real name. That is the one input a person has that the file does
not — per P1, everything else is measured.

Naming convention: `Title (Year).mkv`, enforced at this step. Jellyfin metadata matching depends on
it, and the current library is inconsistent (`007 - Skyfall (2012)` vs `Bourne Identity` vs
`The Land Before TIme` typos vs `Black Sheep (1996) .mkv` with a trailing space).

### 4.5 Stage 3 — Analyze

Read-only. Probes each title, emits one `plan.json` per title containing both measurements and
decisions, including the **literal ffmpeg argv** that will be executed (this is the reproducibility
and provenance artifact).

```
probe (ffprobe -print_format json)
  → streams, duration, height, SAR, audio languages/codecs/channels, subtitle dispositions

tier:     height ≤ 576  → DVD tier          else → Blu-ray tier

cadence:  fieldmatch,decimate frame ratio over 3 × 30s samples
          ≈ 0.80                       → 3:2 telecine → IVTC chain, target 23.976
          ≈ 1.00 + idet progressive    → no filter
          ≈ 1.00 + idet TFF/BFF        → deinterlace, keep source rate
          inconsistent between samples → MIXED CADENCE → park in needs_review/

crop:     cropdetect at ~10 intervals spread across the runtime
          → take the UNION (smallest crop / largest retained area), round to mod 2
          → reject width crops under ~8px (edge noise, not letterboxing)

grain:    SSIM(original, hqdn3d-denoised) on samples; low score ⇒ grainy
          → tune grain on Blu-ray only, with the size cost recorded in the plan

crf:      VMAF bisection on 3 × 30s samples, target ≈95

audio:    English tracks; lossless/DTS-HD → E-AC3 5.1; AC3 ≤640k → copy
          always plus AAC stereo with an explicit downmix matrix
subs:     English tracks, set forced disposition, never burn in
```

Two details carry disproportionate weight:

- **Crop union, not mode.** Any single sample taken from a dark scene over-crops. Taking the
  union across samples is the specific fix for the `Dr Strangelove` 668x480 case (§2.3).
- **The mixed-cadence branch.** `[INFERRED]` roughly 5% of DVDs have cadence breaks at reel changes
  or video-sourced credits. Parking them for manual handling beats silently producing another
  Bourne Identity.

### 4.6 Stage 4 — Encode

Executes the plan verbatim. No decisions remain at this stage.

### 4.7 Stage 5 — Verify

Fail the job and retain the source if any assertion misses:

| Assertion | Catches |
|---|---|
| Output duration within 1% of source | Truncation |
| **Output fps == planned fps** | The entire pulldown class (§2.2) |
| Duplicate-frame rate < 2% (`mpdecimate`) | IVTC failure |
| Dimensions == planned crop | Crop drift |
| VMAF ≥ target on 3 × 60s samples | Filter damage, over-compression |

`libvmaf` is present in the installed ffmpeg `[VERIFIED]`, so the VMAF check needs no new tooling.

### 4.8 Stage 6 — Publish

1. Write to `$FINAL_PATH.partial` on the NAS, then **rename** (atomic within the target filesystem).
   This is the fix for defect §2.7-3.
2. Stamp provenance metadata.
3. **Only then** move the source to `archive/`.

Provenance (P2):

```
-metadata ENCODE_VERSION=4.0
-metadata ENCODE_GIT_SHA=<short sha>
-metadata ENCODE_PROFILE=<tier/decisions>
-metadata ENCODE_CROP=<crop>
-metadata ENCODE_CMDLINE=<literal argv>
```

Idempotency is then keyed on *"output exists AND its stamped version matches current settings"*,
not on filename existence. This simultaneously fixes the partial-file trap and generates the
stale-file worklist for free.

---

## 5. Encode specifications

### 5.1 DVD tier (720x480 anamorphic NTSC, mostly telecined film)

**SUPERSEDED — the original chain here used `fieldmatch,yadif=deint=interlaced,decimate`, which
destroys soft-telecined sources (23.976 → 19.2 fps). Corrected version:**

```bash
# DEFAULT CASE (~94% of sampled DVDs): soft-telecined, ffmpeg already decodes 23.976.
# NO cadence filter. Crop only.
ffmpeg -i "$SRC" \
  -map 0:v:0 -map 0:a:0 -map 0:a:0 -map "0:s?" \
  -vf "crop=$CROP" \
  -c:v libx265 -preset slow -pix_fmt yuv420p10le \
  -crf "$CRF" -x265-params "aq-mode=3:no-sao=1" \
  -color_primaries smpte170m -color_trc smpte170m -colorspace smpte170m -color_range tv \
  -c:a:0 aac -ac:a:0 2 -b:a:0 192k \
  -filter:a:0 "pan=stereo|FL=0.5*FC+0.707*FL+0.707*BL|FR=0.5*FC+0.707*FR+0.707*BR" \
  -c:a:1 copy \
  -c:s copy \
  "$OUT"

# MINORITY CASE (~6%): genuinely interlaced / hard-telecined (decodes at 29.97 WITH combing).
# Analyzer must confirm which by testing, not assume:
#   hard telecine  -> -vf "fieldmatch,decimate,crop=$CROP"      (verify result is 23.976)
#   true 29.97 video -> -vf "bwdif=mode=send_frame:deint=interlaced,crop=$CROP"  (keep 29.97)
```

Detection rule: decode a 30s sample and compare actual decoded fps against the container's
`r_frame_rate`. If decoded ≈23.976 while the container claims 29.97, it is soft telecine and
**no filter is correct**. Only if it decodes at 29.97 *and* `idet` reports substantial TFF/BFF
does any cadence filter apply.

- `fieldmatch,yadif=deint=interlaced,decimate` is the key chain. `deint=interlaced` means yadif
  touches **only** frames fieldmatch could not repair (orphaned fields), rather than every frame —
  this is the gating the `action` profile was missing (§2.7-1).
- `preset slow` without hesitation: it is 480p, CPU is effectively free at this resolution.
- SAR is preserved automatically — cropping does not change pixel shape, so DAR stays correct.
  Source DVDs carry SAR 853:720, DAR 853:480 `[VERIFIED]`.
- Keep AC3 5.1 passthrough here (~450 kbps); the Blu-ray audio argument does not apply.

### 5.2 Blu-ray tier (1080p progressive)

```bash
  -vf "crop=$CROP" \
  -c:v libx265 -preset medium -pix_fmt yuv420p10le -crf "$CRF" \
  -x265-params "aq-mode=3:no-sao=1" \
  -c:a:0 aac -ac:a:0 2 -b:a:0 192k \
  -c:a:1 eac3 -ac:a:1 6 -b:a:1 768k
```

- IVTC normally unnecessary (Blu-ray film is already 23.976 progressive), but the cadence check
  still runs — some older TV-on-Blu-ray is 29.97 interlaced.
- Grain tune only when measured grainy, with the size cost accepted knowingly (§2.6).
- Run 2–3 encodes concurrently at `medium` rather than one at `fast`. `[INFERRED]` this beats the
  current setup on both throughput and quality, and reframes the "preset fast to save the server"
  tradeoff — the answer is concurrency, not a worse preset.

### 5.3 Audio policy

| Source track | Action | Rationale |
|---|---|---|
| TrueHD / DTS-HD MA | → E-AC3 5.1 @ 768k | ~3 GB/title saved; lossless retained in `archive/` |
| AC3 ≤ 640k | copy | Already small |
| Any | + AAC stereo @ 160–192k | iPad direct play, explicit downmix matrix |

**Gotcha `[VERIFIED as a known ffmpeg behavior, not measured here]`:** ffmpeg's default `-ac 2`
downmix is quiet and dialogue-light. Use the explicit `pan` matrix shown in §5.1, and spot-check
levels on the first few outputs.

E-AC3 chosen over AC3 (better quality at equal rate), AAC 5.1 (weaker passthrough over ARC/optical
on some receivers), and Opus 5.1 (weakest direct-play support in this client mix).

### 5.4 Subtitle policy

Mux English tracks and set the `forced` disposition; **do not burn in**. This is better than the
pre-v3.0 burn-in approach — reversible, and Jellyfin honors the forced flag.

### 5.5 Codec choice — HEVC 10-bit, not AV1

`libsvtav1` is available `[VERIFIED]` and AV1 would save ~20–30% at equal quality, with particular
strength at DVD resolution. **Rejected on direct-play grounds:** AV1 hardware decode begins at
A17 Pro / M-series, so older iPads would force server-side transcoding — costing more quality than
the bitrate saved. HEVC Main10 hardware-decodes on every iPad since the A9.

Revisit in a few years. Because sources are archived, a future re-encode is cheap — this is the
principal payoff of the archive policy.

Hardware encoding (QSV/NVENC/VideoToolbox) rejected: meaningfully worse quality per bit than
software x265 at slow presets, and throughput is not the binding constraint for a one-time pass.

---

## 6. Tool evaluation

### 6.1 HandBrakeCLI — rejected for the encode stage

Two of three P1 defects came from HandBrake's implicit behavior (auto-crop default) and one from
flag interaction (`--rate auto` + `--cfr` vs `--detelecine`). The three things HandBrake was
assumed to be doing for us do not hold up:

| Assumed benefit | Reality |
|---|---|
| Anamorphic SAR handling | ffmpeg carries SAR through `crop` untouched. Non-issue. |
| Audio selection / downmix | `-map 0:a:m:language:eng`, explicit `pan` matrix. One gotcha (§5.3). |
| Forced subtitles | HandBrake's answer was burn-in; muxing with the forced flag is strictly better. |

**Middle path if the full rewrite is undesirable:** keep HandBrake but make every implicit default
explicit — pin crop, pin rate, pin every filter. Rule: no HandBrake default goes unstated.

### 6.2 Tdarr / Unmanic / FileFlows — evaluated, not adopted

| Tool | Model | Relevant capability | License |
|---|---|---|---|
| Tdarr | Node, server + distributed nodes, Flows + JS plugins | Tiered CRF **by resolution**, not VMAF; described as not lightweight | Freemium/proprietary |
| FileFlows | Visual flow editor, agents/runners | **VMAF-optimized encoding**, automated crop, `Flow.Execute` for arbitrary processes | Freemium/proprietary; free tier 1 agent / 5 runners / 30 flow elements / SQLite only |
| Unmanic | Python, simplest | Basic library scan + task queue + plugins | Properly open source (only one of the three) |

**Decisive gap `[VERIFIED by search; absence of evidence]`:** none of them perform inverse telecine.
This is the single biggest defect in a DVD-heavy library (§2.2). FileFlows' feature list covers
crop, upscale, subtitles, watermarking, VMAF and Dolby Vision, and does not mention deinterlacing
or IVTC.

Their crop handling is also suspect — FileFlows advertises "automated crop" but nothing indicates
multi-interval union sampling, and single-pass `cropdetect` is what produced the 668x480 case.

**Credit where due:** FileFlows' built-in VMAF-targeted encoding is real and covers a piece this
plan proposes to build.

**Verdict:** these tools provide the *orchestration* layer (queue, retry, state, UI, distributed
workers) but not the *decision* layer, which is where all the value and all the bugs live. For a
~500-file one-time backlog, a Python pipeline with inspectable plans is worth more than a queue UI.
`Flow.Execute` makes a hybrid viable if a UI is later wanted — their runner, our decisions.

**Condition that would flip this:** if the workload becomes ongoing library maintenance across
multiple encode boxes, FileFlows earns adoption, calling the same analyzer.

### 6.3 Implementation language — Python, not bash

Bash is the wrong tool once the pipeline needs structured data (`ffprobe -print_format json`),
floating-point comparison (cadence ratios, VMAF bisection), resumable state across a multi-day run,
and safe handling of filenames like `Bourne Identity(1).mkv` and
`Charlie's Angels - Full Throttle (2003) .mkv` (trailing space). `subprocess.run([...])` with an
arg list has no quoting layer at all.

Testability matters specifically here: the `--rate auto` + `--cfr` interaction is exactly what a
unit test on the decision function catches, and a bash script shelling out to a transcoder cannot
be meaningfully tested.

**Stack:** Python 3 stdlib only (`json`, `subprocess`, `pathlib`, `dataclasses`,
`concurrent.futures`, `sqlite3`). Python 3.14.7 present `[VERIFIED]`. Zero dependencies, no build
step. Bash retained for cron entry points. Go considered (the repo contains a Go service,
`services/marvin`) but rejected — a compile step hurts while the tuning logic is being iterated.

**State storage:** JSON per title rather than SQLite — greppable, diffable, reviewable, and `jq` is
already installed. 500 records does not justify a database.

---

## 7. Implementation plan

| # | Deliverable | Notes |
|---|---|---|
| 1 | `analyze.py` | Read-only. Run over all 215 stale files first and read the output before encoding anything. |
| 2 | `verify.py` | Also read-only. Doubles as an auditor for existing files. |
| 3 | `encode.py` + `publish.py` | Straightforward once the plan is a literal argv. |
| 4 | ARM VM build | §4.2 |
| 5 | MacBook rip loop | §4.3 |
| 6 | Ripping automation polish | Lowest value — discs are fed by hand anyway. |

Module layout — single entry point with subcommands (`transcode analyze|encode|verify|publish|run`)
rather than separate scripts, to reduce ceremony.

Existing asset: `scripts/encode_audit` (uncommitted) already implements x265 SEI fingerprinting,
preset back-mapping from `rd=`, and profile inference. Reusable as the basis for `verify.py`.

---

## 8. Migration plan for the existing library

Ranked by impact:

| Priority | Action | Scope | Cost |
|---|---|---|---|
| 1 | Remux lossless audio → E-AC3 5.1 | ~19 Blu-rays | Minutes each, **video untouched** |
| 2 | Re-encode pulldown-damaged files | 28 files | Full encode |
| 3 | Re-encode badly-cropped files | 7 worst | Full encode |
| 4 | Restore subtitles | 5 v3.0 files | Full encode |
| 5 | Re-encode remaining stale files | ~180 | Full encode, low urgency |
| 6 | Encode the never-encoded backlog | 271 files | Full encode |

**Sources survive in `/Volumes/nas/media/archive/` (205 files), so items 2–5 are recoverable.**
Verify source availability per title before re-encoding; coverage is ~205 of 215.

Item 1 is the highest value-per-minute action available and requires none of the pipeline work.

---

## 9. Open questions and claims requiring verification

**These are the parts most likely to be wrong. Please scrutinize them first.**

1. **The `--cfr` / `--detelecine` interaction mechanism (§2.2).** The *observation* is verified
   (28 files at 29.97 with 20–24% duplicate frames; sources are 23.976 film with exactly 0.800
   decimation ratio). The *mechanism* — that `--rate auto` resolves to 29.97 and `--cfr` re-pads
   after detelecine — is inferred and was **not tested**, because HandBrakeCLI is not installed on
   the analysis machine. An alternative explanation is that HandBrake's detelecine filter simply
   failed to lock cadence on those sources. The proposed fix (explicit IVTC chain + explicit rate)
   is correct under either explanation, but the diagnosis should be confirmed.

2. **`--deinterlace=mode=1` semantics.** The `action` profile uses a raw custom mode value. The
   previous value, `--deinterlace=yadif`, is not valid HandBrake syntax (the deinterlace filter
   takes preset names like `default`/`skip-spatial-check`/`bob`, or `mode=N`), which is presumably
   why it changed. What `mode=1` actually does was not confirmed. If it means bob/double-rate, the
   interaction with `--cfr` needs separate analysis.

3. **`--crop-mode none` flag spelling.** `--crop-mode` landed in HandBrake 1.7 and the script
   targets 1.7.2, but this was not verified against `HandBrakeCLI --help` on the actual install.
   Only relevant if the "keep HandBrake" middle path (§6.1) is taken.

4. **VMAF target of 95.** Chosen as a reasonable prior, not validated. Recommend testing on 3–4
   representative discs per tier before committing ~500 files. Similarly, RF 19 (DVD) / RF 21
   (Blu-ray) from the current script were never validated.

5. **Grain detection method.** `SSIM(original, hqdn3d)` as a grain proxy is proposed but untested.
   Alternatives: `signalstats` high-frequency energy, or a sample encode comparison. This decision
   roughly doubles Blu-ray file size, so it deserves a measured threshold.

6. **Mixed-cadence prevalence.** The estimate of ~5% of DVDs needing manual VapourSynth handling is
   a guess. The analyzer's per-sample consistency check will produce the real number.

7. **E-AC3 768k as the surround target.** Bitrate and codec chosen for the Apple + Jellyfin client
   mix. Not listening-tested. AC3 640k is the more conservative alternative.

8. **`makemkvcon -r info` DRV line parsing** (§4.3) — field layout not confirmed against real
   output.

9. **MakeMKV key configuration location** in the ARM container — not confirmed.

10. **ARM udev-in-LXC claim** (§4.2). The reasoning that LXC cannot cleanly host ARM's udev trigger
    is inferred from LXC architecture, not tested. Community guides do report working LXC
    installs, presumably via host-side udev rules. The VM recommendation rests on several
    independent arguments, so this one being wrong does not change the conclusion.

11. **Whether `clean`-profile Blu-rays were also affected by cadence issues.** Analysis focused on
    DVD sources. Blu-ray content is assumed 23.976 progressive; only 16 Blu-ray-sourced encodes
    exist, and their framerates were not individually audited.

12. **FileFlows/Tdarr IVTC absence** is based on documentation search returning nothing, which is
    weaker than a direct test. If either has an undocumented IVTC capability, §6.2's conclusion
    weakens considerably.

---

## 10. Appendix — reference data

### 10.1 Reproducible commands used in analysis

```bash
# Extract x265 parameter stamp (NOT with `strings` - truncates at 1024 bytes)
ffmpeg -v quiet -i FILE -map 0:v:0 -c copy -bsf:v hevc_mp4toannexb -frames:v 2 -f hevc - \
  | LC_ALL=C tr -c '[:print:]' '\n' | grep -m1 'x265.*options:'

# Duplicate-frame rate over a 90s sample
IN=$(ffmpeg -v quiet -ss 600 -t 90 -i FILE -f null - -stats 2>&1 \
     | tr '\r' '\n' | grep -oE 'frame=[ ]*[0-9]+' | tail -1 | tr -dc 0-9)
DE=$(ffmpeg -v quiet -ss 600 -t 90 -i FILE -vf mpdecimate -f null - -stats 2>&1 \
     | tr '\r' '\n' | grep -oE 'frame=[ ]*[0-9]+' | tail -1 | tr -dc 0-9)

# Telecine detection on a source
ffmpeg -ss 600 -t 40 -i SRC -vf idet -an -f null - 2>&1 | grep -iE 'Repeated|Single|Multi'
ffmpeg -v quiet -ss 600 -t 40 -i SRC -vf 'fieldmatch,decimate' -an -f null - -stats
# ratio of 0.800 == clean 3:2 pulldown

# Per-stream size (identifies audio-dominated files)
ffprobe -v error -select_streams 2 -show_entries packet=size -of csv=p=0 FILE \
  | awk '{s+=$1} END{printf "%.2f GB\n", s/1073741824}'
```

### 10.2 x265 preset back-mapping from the parameter stamp

| `rd=` | Preset | Corroborating |
|---|---|---|
| 2 | fast | `ref=3`, `early-skip` off |
| 3 | medium | `no-rect`, `rdoq-level=0` |
| 4 | slow | `rect`, `rdoq-level=2`, `subme=3`, `ref=4` |

### 10.3 Current script profile → observed bitstream mapping

| Profile | Preset | Tune | Observed signature |
|---|---|---|---|
| `action` | fast | — | `rd=2`, `aq-mode=3`, no `rc-grain` |
| `gritty` | medium | grain | `rd=3`, `aq-mode=3`, `rc-grain` |
| `clean` (br) | fast | — | indistinguishable from `action` in-bitstream |
| `clean` (dvd) | slow | — | `rd=4`, `aq-mode=3` |

Note the ambiguity: `action` and `clean`-on-Blu-ray both resolve to preset `fast`, and the only
difference is filter flags, which leave no bitstream trace.
