# Agent handoff

Written 2026-09-21 for an agent picking this project up cold. `CLAUDE.md` and
`handoff.md` remain authoritative for rules and evidence; this file exists because
their **State** sections lag behind reality, and because a few things are only
learnable by having broken them.

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
Two matter more than the rest:

**Rule 1 — classify cadence by DECODED frame rate, never the container.** A DVD
claiming 29.97 usually decodes at 23.976. Running `decimate` on it drops one *real*
frame in five. Two AI models independently reasoned their way to a filter chain that
would have destroyed the library; measurement caught it. If you find yourself reasoning
about what filter *should* be correct, stop and measure instead.

**Rule 10 — when a plan and reality disagree, suspect the checker.** Most bugs in this
project have been in `verify.py` / `analyze.py` measurement code, not in encodes. All
three bugs found in the most recent session were measurement defects. A verify failure
means *investigate*, not *the encode is broken*.

---

## Actual state (the docs' State sections are stale)

`CLAUDE.md` still reads "State as of 2026-09-15 — two shows delivered, 15 bugs."

**Delivered**

| | Episodes |
|---|---:|
| Charmed | 173 |
| Buffy | 143 |
| Friends | 226 |
| The 100 | 100 (99 encoded + 1 pre-existing mp4 for s06e05) |

Plus 72 films in `movies/no_kids/`. The **`bluray-film` tier is complete** — all 14 titles.

**Staged but never started**

| What | Where | Count |
|---|---|---:|
| The Office Blu-ray "Superfan" | `raw/bluray/tv/the office/` | 147 (S1–S7) |
| `bluray-standard` | `raw/bluray/movies/standard/` | 23 |
| `bluray-film` | `raw/bluray/movies/film/` | 5 |
| DVD movies | `raw/dvd/movies/` | 436 |
| DVD shows | `raw/dvd/tv/` | 4 shows |

The Office raws are renamed and reconciled against episode counts, but have **never been
analyzed or encoded**.

> **Trap: two different Office sources exist.**
> `raw/bluray/tv/the office/` — 147 Superfan **extended** cuts, 30–40 min per episode.
> `raw/dvd/tv/the office/` — 185 files, a separate DVD rip.
> Do not conflate them.

---

## Recent bugs (detail in `handoff.md` §5)

**#16 — fixed.** `av_drift` compared audio streams by container position (`a:0`) rather
than identity, so any plan that reorders audio was compared against the wrong source
track. Fixed with `audio_position()`, matching on `source_index`.

**#17 — OPEN.** `classify_cadence` only detects mixed cadence from its 3-point decoded-fps
sample, so a short locally-progressive stretch inside an otherwise-telecined file goes
unnoticed and `decimate` eats real frames. Hit Friends s05e04 (~225 frames lost). That
one file was repaired by hand; the code gap remains.

**#18 — fixed.** `resolve_audio` trusted any secondary 2-channel English track as "the
disc's own stereo mix." It was **commentary**, and it shipped as the default audio on 27
of 226 Friends episodes. No metadata distinguishes commentary from a genuine alternate
mix — MakeMKV had even tagged one "Stereo" with `disposition.comment` unset — so the fix
correlates audio *content* (`tracks_correlate`). Note the first version used
max-of-2-windows and still passed 3 of the 27, because a commentary track goes quiet
exactly when the commentator does, leaving only the ducked show audio. Median of 7
windows, reject below 0.15.

---

## Operational gotchas

- **Never use `pgrep`/`pkill -f` with a pattern that appears in your own command line.**
  It self-matches. This killed a running script twice in one session. Use explicit PIDs.
- **`analyze` and `verify` buffer their output.** Poll the log file for content or an
  `END` marker; do not poll process lists.
- **Scratch belongs on `/data`** (1.8 TB NVMe), never `/` (20 GB rootfs). A single
  Blu-ray `.partial` is 8–10 GB.
- **Point `--work` at the NAS for large files** so final placement is a same-filesystem
  rename rather than a cross-filesystem copy. The copy path caused an OOM.
- **Always `nice -n 15 ionice -c3`.** Jellyfin (CT100), Plex (CT103) and a Minecraft
  server share this host. Note that `nice` only sorts priority *within* a container;
  across containers use `pct set 101 --cpuunits N` on the Proxmox host.
- **Tier comes from the path**: `raw/bluray/tv` → `bluray-tv`, `movies/film` →
  `bluray-film`, `movies/standard` → `bluray-standard`. `--tier NAME` overrides for a
  single run, but folder placement is what survives the re-analysis that every change to
  `analyze.py` forces.
- **Retire, never delete.** Replaced files go to a `*-retired` directory and are not
  pruned automatically. NAS sits at 78% (2.1 TB free).
- **The owner commits their own work.** Leave finished changes in the working tree.

---

## Open decisions awaiting the owner

- Episode titles for S1/S3/S4/S5/S7 Office audio — currently plain `sNNeNN`; S2 and S6
  have titles. Renaming `.m4a` costs nothing.
- **Season 7 numbering.** Its three long files were mapped as doubles, giving 27 episode
  numbers; some listings say 26. A rename fixes it — not a re-encode. Season 3's
  equivalent mapping *was* independently corroborated (its two long files landed exactly
  on the two real hour-long episodes), which is why the method is trusted.
- **Killing Eve.** The 16 existing library episodes came from an old HandBrake script.
  Extracting the x265 stamp shows it applied `sao` and `aq-mode=2` despite the script
  specifying `no-sao=1:aq-mode=3` — weaker than intended. Re-encode recommended, never
  decided.
- 13 zero-byte macOS `._` AppleDouble stubs in `tv/no_kids/The 100/` root — a Plex
  scanner hazard.
- The 100 `s02e08` source carries 33 concealed decode errors (well under a second of
  artifacts). Re-rip only if the disc is handy.

## Suggested first task

Bring `CLAUDE.md`'s **State as of** section and `handoff.md` §7.1 current. They are the
stalest thing in the repo, and every future reader is misled by them.
