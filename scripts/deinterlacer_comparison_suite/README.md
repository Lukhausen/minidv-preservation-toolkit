# Deinterlacer Comparison Suite

This is the browser shootout I used to decide how to deinterlace my MiniDV
footage.

It is a practical testing lab: still-frame A/B votes, one-second clip votes,
focused top-mode comparisons, MP4 compression comparisons, single-file previews,
and final sample review. Votes are saved locally and ranked with Elo.

## Run

Put a few short DV AVI samples here:

`scripts/deinterlacer_comparison_suite/sample_videos/`

Then start the server:

```powershell
python .\scripts\deinterlacer_comparison_suite\server.py --port 8765
```

To use another folder:

```powershell
$env:MINIDV_SAMPLE_DIR = ".\my_sample_videos"
python .\scripts\deinterlacer_comparison_suite\server.py --port 8765
```

## What It Writes

Generated frames, clips, preview uploads, final samples, frame-count caches, and
`votes.json` are written next to this suite. They are ignored by git.

## Setup Note

You need Python, ffmpeg/ffprobe, VapourSynth, and the relevant VapourSynth
plugins for the modes you enable. The setup has moving parts, so this is a
good thing to hand to an AI coding agent.

The portable VapourSynth installer is public and official:
[VapourSynth install docs](https://www.vapoursynth.com/doc/installation.html)
and
[`Install-Portable-VapourSynth-R76.zip`](https://github.com/vapoursynth/vapoursynth/releases/download/R76/Install-Portable-VapourSynth-R76.zip).

For my footage, I did 504 blind comparisons. My Elo ranking was:

1. `slower_default` - 1983.5
2. `vslow_default` - 1868.2
3. `jet_qtgmc_refined` - 1695.9
4. `jet_qtgmc_default` - 1688.1
5. `ai_ddd` - 1567.0

`slower_default` gave me the best mix of clean deinterlacing, a little
sharpening, and not flattening the image noise too hard.
