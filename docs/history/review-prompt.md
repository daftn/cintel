# Cross-Review Prompt

Prompt for having an independent model (Gemini, GPT, etc.) adversarially review
`media-pipeline-plan.md`.

**Usage:** attach or paste `docs/media-pipeline-plan.md`, then paste everything below the
horizontal rule as the prompt. The whole file is safe to paste as-is if that's easier — this
header is harmless context.

**Design notes on this prompt** (not part of the prompt itself):

- Instruction 4 ("give me what §9 missed") is the load-bearing line. Without it, the reviewer
  tends to paraphrase the document's own uncertainty list back with mild agreement, which
  carries no information.
- Instruction 6 targets the dominant failure mode in this domain: plausible-but-invented
  ffmpeg/HandBrake/x265 flags, which are hard to spot without testing.
- Question C is the highest-value one. The plan leans on VMAF targeting throughout, but VMAF's
  training data skews toward higher resolutions and its behavior on 480p and on grainy content
  is a genuine weak spot. The library is DVD-heavy, so if VMAF is wrong there, a meaningful
  part of the plan needs rework.

---

You are a senior video encoding engineer with deep experience in DVD/Blu-ray
restoration (telecine, IVTC, MPEG-2 artifacts), x265/ffmpeg tuning, and
self-hosted media infrastructure (Proxmox, Jellyfin, MakeMKV).

I'm attaching a design document for a media encoding pipeline. It was produced
by another AI agent after forensic analysis of an existing 491-file library. I
want an ADVERSARIAL technical review, not validation.

IMPORTANT — how to review this:

1. Do NOT summarize the document back to me. I wrote it; I know what it says.
   Go straight to critique.

2. Disagree where you actually disagree. If a recommendation is wrong,
   suboptimal, or rests on a faulty premise, say so directly and explain the
   correct approach. Do not soften real objections. Do not open with praise.

3. The doc tags claims as [VERIFIED] (measured, evidence shown), [INFERRED]
   (reasoned, untested), and [UNVERIFIED] (assumed). Audit those tags. Flag
   anything tagged VERIFIED whose stated evidence doesn't actually support the
   conclusion, and anything tagged INFERRED that you believe is flatly wrong.

4. Section 9 lists 12 things the author already knows are uncertain. Engage with
   those, but DO NOT simply restate them. I want the problems section 9 MISSED.

5. You cannot run commands. Where a claim needs empirical testing to settle,
   say what test would settle it rather than guessing.

6. Verify all command/flag syntax against your knowledge of ffmpeg, x265,
   HandBrake, MakeMKV, and Proxmox qm/pct. Explicitly mark any syntax you are
   not confident is correct — invented flags are the failure mode I most want
   caught.

Specific questions I want answered, in addition to whatever you find:

A. Is the inverse-telecine diagnosis correct? The evidence is: sources are
   MPEG-2 720x480 flagged field_order=tt at 29.97, idet reports 100%
   progressive with zero repeated-field flags, and fieldmatch+decimate reduces
   frames by exactly 0.800. Is "23.976 film padded with duplicate progressive
   frames" the right read? Is `fieldmatch,yadif=deint=interlaced,decimate` the
   correct chain, or is something else better for mixed-cadence NTSC DVD?

B. VFR vs forcing 23.976 CFR after IVTC — which is actually safer across a
   mixed library of film and video-sourced DVDs, and why?

C. Is VMAF a valid quality target at DVD resolution (720x480)? I'm aware it was
   trained largely on higher-resolution content. If it's unreliable here, what
   should the DVD tier target instead? Is a target score of 95 sensible?

D. Is the crop strategy sound — cropdetect at ~10 intervals, take the UNION
   (largest retained area), reject width crops under 8px? What breaks it?

E. Audio: is E-AC3 5.1 @ 768k the right target to replace TrueHD/DTS-HD
   passthrough for a Jellyfin + Apple TV + older-iPad client mix? Better
   options at similar size?

F. Is HEVC 10-bit over AV1 the right call given those clients, or is that
   overly conservative in 2026?

G. Are the x265 parameters good? Specifically `aq-mode=3` and `no-sao=1`, and
   preset slow for DVD / medium for Blu-ray.

H. Is abandoning HandBrakeCLI for raw ffmpeg justified, or is the "keep
   HandBrake but pin every implicit default" middle path (section 6.1) the
   better engineering decision?

I. What is missing from this plan entirely?

Output format:
- CRITICAL — errors that would damage files or produce wrong output
- SIGNIFICANT — suboptimal decisions worth changing
- MINOR — polish
- MISSING — gaps in the plan
- WHERE I'M UNCERTAIN — your own low-confidence calls, stated plainly

For each item: what's wrong, why, and the specific fix. Prioritize technical
correctness over completeness — five well-reasoned objections beat thirty
shallow ones.
