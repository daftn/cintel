# Agent handoff

Rewritten 2026-09-28 for an agent picking this project up cold. `CLAUDE.md` and
`handoff.md` remain authoritative for rules and evidence; this file exists because their
**State** sections lag between updates, and because a few things are only learnable by
having broken them.

---

## What this is

Measurement-driven pipeline converting DVD and Blu-ray rips to HEVC 10-bit MKV for a
Jellyfin/Plex library. Runs in Proxmox LXC container 101. Zero dependencies — Python
stdlib plus `ffmpeg`/`ffprobe`.

```
analyze  →  plans/*.json  →  encode  →  verify  →  publish
```

## Read before touching anything

| File | Why |
|---|---|
| `CLAUDE.md` | Cardinal rules, current configuration, where things live |
| `docs/handoff.md` | Every decision and the evidence behind it; §5 is the bug log |

The cardinal rules are not style preferences. Each was learned by breaking something.
Three matter more than the rest:

**Rule 1 — classify cadence by DECODED frame rate, never the container.** A DVD
claiming 29.97 usually decodes at 23.976. Running `decimate` on it drops one *real*
frame in five. Two AI models independently reasoned their way to a filter chain that
would have destroyed the library; measurement caught it. If you find yourself reasoning
about what filter *should* be correct, stop and measure instead.

**Rule 10 — when a plan and reality disagree, suspect the checker.** Most bugs in this
project have been in `verify.py` / `analyze.py` measurement code, not in encodes. A
verify failure means *investigate*, not *the encode is broken*. That said — the most
recent investigation (below) found the checker was right and the encode was genuinely
broken. Suspecting the checker is the right first move, not an excuse to skip the
investigation.

**Rule new — `encode.py` marks a failed title `XX`, not `FAIL`.** A status-check grep for
`FAIL|error` will miss real failures silently. This cost a full day of "zero errors"
status reports on a batch that actually had 70 failures. Grep `XX` specifically, or use
`verify`'s own PASS/FAIL output, which is reliable.

---

## Actual state (the docs' State sections lag)

**Delivered and published**

| | Episodes/Titles |
|---|---:|
| Charmed | 173 |
| Buffy | 143 |
| Friends | 226 |
| The 100 | 100 (99 encoded + 1 pre-existing mp4) |
| Ash vs Evil Dead | 30 |
| Parks and Recreation | 122 files / 125 eps |
| The Office (Blu-ray) | 194 files / 202 eps — 70.5h encode + 1.1h verify at `--jobs 3` |
| `bluray-film` | 14/14 titles (9 later patched for a commentary-track bug, see below) |

**In flight — DVD movies, the first-ever run of the `dvd` tier on movie content**

| | Status |
|---|---|
| Batch 1 (100 titles) | Encoded, verified 100/100 PASS, renamed to Plex convention. **Not yet published** — waiting on the owner's audience-folder (`no_kids`/`older_kids`/`younger_kids`) sort. One title, `DeadByDawn.mkv`, could not be identified and was left unrenamed. |
| Batch 2 (336 titles) | Analyzed clean (0 needs_review). Encode surfaced a real incident, see next section. Recovery in progress as of 2026-09-28. |

**Staged, not started**

| What | Where | Count |
|---|---|---:|
| `bluray-standard` | `raw/bluray/movies/standard/` | 26 |
| `bluray-film` (more) | `raw/bluray/movies/film/` | 5 |
| Big Bang Theory | `ingest/`, unnamed `_tNN` discs, S1-4 | 8 discs |
| Wild Thornberrys | `raw/dvd/tv/` | blocked on measuring `tune=animation` |
| The Office DVD rip | `raw/dvd/tv/the office/`, 185 files | almost certainly redundant now that the Blu-ray Superfan version is done; probably archive without encoding |

---

## The DVD batch 2 incident (2026-09-28) — read this before touching that batch

70 of 336 titles failed with `ffmpeg exit 243` / permission denied reading the raw
source. This was a NAS-side ACL issue, not a pipeline defect — resolved by the owner,
confirmed by directly re-testing readability on all 70 sources before retrying. **The
real lesson is operational**: these 70 failures were invisible to status checks for the
better part of a day because the checks grepped for `FAIL`/`error`, and `encode.py`'s
actual failure marker is `XX`. Always verify which string a tool actually prints before
trusting a status check against it.

Two more failures, found investigating past the permission group, were **not**
permission-related and **not** checker bugs — genuine encode-time defects that `verify`
caught exactly as designed:

- **The Deer Hunter** — output video track ends ~100s before its audio track (confirmed
  via each stream's own container `DURATION` tag and by reading the literal last packet
  of each stream). Source is fine end to end. `ffmpeg` exited 0 anyway. Cause not
  identified; queued for a plain retry.
- **Shanghai Knights** — genuine +583ms of accumulating A/V drift (ruled out as a
  truncation artifact first: whole-file video/audio durations differ by only 24ms).
  Also queued for retry.

One more (`The Land Before Time: The Big Freeze`) failed on duplicate-frame ratio from
imperfect `fieldmatch,decimate` matching on its irregular cadence — deterministic, so a
retry won't change the result. The owner accepted the current output rather than
hand-tuning the filter for one lower-stakes title.

**Takeaway for whoever picks this up next**: a clean exit code and a plausible file size
are still not proof of a correct encode, exactly as this project's founding motivation
(`handoff.md` §1) says. `verify` earned its keep again this week.

---

## Recent bugs (detail in `handoff.md` §5)

**#19 — fixed.** `tracks_correlate` returning `None` (unmeasurable) was treated as a
pass. Found on Ash vs Evil Dead: a 31-packet degenerate "English Stereo" stub track
would have shipped as near-silent default audio. `None` now takes the same branch as a
measured failure.

**#20 — fixed.** `verify.py`'s `run_vmaf()` never applied a plan's own crop to the
source before comparing against the (already-cropped) output — a severe framing
mismatch on any title with real letterboxing, producing single-digit VMAF on a visibly
clean encode. Found on Bourne Ultimatum. Fixed in both the SD-upscale and non-upscale
branches; validated via the real `--vmaf` CLI path post-fix (reads a believable 81.9
now). One caveat: an unrelated anomaly hit during that investigation (a hand-built
lossless test that still scored ~68) was never root-caused and isn't attributed to this
bug — treat `--vmaf` as improved, not fully trusted; prefer the §4.2 FFV1-reference
method for anything that matters.

**#17 — still OPEN.** `classify_cadence` only detects mixed cadence from its 3-point
decoded-fps sample, so a short locally-progressive stretch inside an otherwise-telecined
file goes unnoticed. Hit Friends s05e04; fixed by hand there, code gap remains.

---

## A recurring methodology finding — not a code bug

The disc-order + duration-outlier method used to spot merged double episodes has been
wrong twice in the same specific way: a single "super-sized" episode (long, but never
actually split into two broadcast numbers) gets mistaken for a real two-parter, because
nothing in the disc structure distinguishes them — only real episode-count knowledge
does. Hit both Office S7 (`e22e23`) and S9 (`e16e17`); both fixed by cross-checking
Wikipedia's actual episode list rather than trusting duration alone. If you're naming
Big Bang Theory next, expect the same trap and corroborate any duration-outlier double
against a real source before it ships — the way Season 3's mapping was originally
corroborated, which is why that one specific case is trusted without a recheck.

---

## Operational gotchas

- **`encode.py` failures print `XX`, not `FAIL`.** See above — this one is worth
  repeating because it cost real time.
- **Never use `pgrep`/`pkill -f` with a pattern that appears in your own command line.**
  It self-matches. Use explicit PIDs.
- **`analyze` and `verify` buffer their output.** Poll the log file for content or an
  `END` marker; do not poll process lists.
- **Scratch belongs on `/data`** (1.8 TB NVMe), never `/` (20 GB rootfs). A single
  Blu-ray `.partial` is 8–10 GB.
- **Point `--work` at the NAS for large files** so final placement is a same-filesystem
  rename rather than a cross-filesystem copy. The copy path caused an OOM.
- **Always `nice -n 15 ionice -c3`.** Jellyfin (CT100), Plex (CT103) and a Minecraft
  server share this host. `nice` only sorts priority *within* a container; across
  containers use `pct set 101 --cpuunits N` on the Proxmox host.
- **Tier comes from the path**: `raw/bluray/tv` → `bluray-tv`, `movies/film` →
  `bluray-film`, `movies/standard` → `bluray-standard`, `raw/dvd/**` → `dvd`. `--tier
  NAME` overrides for a single run, but folder placement is what survives the
  re-analysis that every change to `analyze.py` forces.
- **Retire, never delete.** Replaced files go to a `*-retired` directory and are not
  pruned automatically.
- **The owner commits their own work, but will sometimes ask you to.** Confirm before
  committing; always confirm before pushing.
- **This LXC can see the Proxmox host's real hardware sensors** (`/sys/class/hwmon`,
  thermal zones) even though it can't see other containers' processes — LXC shares the
  host kernel, and hwmon isn't namespaced. Useful for real thermal monitoring without
  needing host access; don't assume the same is true for CPU/process-level detail on
  sibling containers (e.g. CT103/Plex) — that needs the Proxmox host itself.

---

## Open decisions awaiting the owner

- `DeadByDawn.mkv` (DVD batch 1) — genuinely unidentified, needs the owner to look at it.
- **Killing Eve.** The 16 existing library episodes came from an old HandBrake script
  that applied `sao`/`aq-mode=2` despite being configured for `no-sao=1:aq-mode=3` —
  weaker than intended. Re-encode recommended, never decided.
- The 100 `s02e08` source carries 33 concealed decode errors (well under a second of
  artifacts). Re-rip only if the disc is handy.
- Audience-folder sort for both DVD movie batches (136 titles once batch 2's recovery
  finishes) before either can be published.

## Suggested first task

Check whether the DVD batch 2 recovery finished (permission retry → chained Deer
Hunter/Shanghai Knights retry → re-verify, see the incident section above). If it has,
the next real step is the owner's audience sort followed by publish for both batches.
