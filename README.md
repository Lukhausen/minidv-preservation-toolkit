# MiniDV Memory Preservation Tools

I created this repo while digitizing 55 MiniDV cassettes from my dad's old Panasonic
camcorder, the one my dad used to record our full childhood with.

While doing that, I built a few tools that I thought might be handy for other
people too. Use them, change them, break them, improve them - whatever helps
you save your own memories in the most pristine quality possible.

## What Is In Here

- `scripts/final_mp4_access_copy` - final batch runner for deinterlacing,
  encoding, stitching approved MP4s, and fixing metadata for photo apps.
- `scripts/deinterlacer_comparison_suite` - the full test suite I used to
  compare deinterlacers and QTGMC settings.
- `scripts/dvrescue_combiner_v2` - the best double-capture / DVRescue combiner
  from this project.
- `scripts/stitch_human_review` - a small browser tool to manually approve
  stitch candidates.
- `scripts/source_quality_control` - quick checks for source DV captures.

## My Main Lessons

Before capturing, rewind the tape fully to the end and then fully back to the
start. For me, tapes that had been lying around for years had way more artifacts
when I skipped this.

Get a quality PCI Express FireWire card. To avoid dropped frames, I recommend
Texas Instruments chipsets, especially:

- `TSB43AB23 / TSB43AB22`
- `TSB82AA2`
- `XIO2213B / XIO2213BZAY`

On Windows, some cameras/cards need the `1394 OHCI Compliant Host Controller
(Legacy)` driver:
[Microsoft legacy 1394 driver](https://download.microsoft.com/download/6/c/d/6cdafba0-054a-44fa-b627-bc1f1e1c6772/1394_ohci_legacydriver.msi).

Download the external tools yourself; I do not include binaries in this repo:

- [DVRescue download page](https://mediaarea.net/DVRescue/Download)
- [WinDV maintained mirror on GitHub](https://github.com/hfiggs/WinDV)
- [Original WinDV site](https://windv.mourek.cz/)

WinDV was the best capture tool for me. It is simple and worked well.
Scenalyzer gave me bad results because it buffered frames weirdly and produced
hanging frames at the start. It could create missing frames without me noticing,
and it did not mark those gaps as errors in the `.dv` data. That is really bad,
because DV normally carries error information that tools can use to detect or
repair damage.

DVRescue can be very useful, but one important thing: it uses file A as the
reference and file B as the fixed file. So if A has the start and middle, and B
has the middle and end, DVRescue cannot just elongate that into one longer file.
That is why I also built stitching tools.

## Deinterlacing

I did loads of tests for deinterlacing. If you want to try it yourself, use the
comparison suite in `scripts/deinterlacer_comparison_suite`. It may be a bit of
a hassle to set up, so honestly, just ask your AI agent to set it up for you.

For my footage, QTGMC `slower_default` was the best mix: a little sharpening,
really clean deinterlacing, but without flattening the image noise as hard as
`vslow_default`. I did 504 blind comparisons. My Elo ranking was:

1. `slower_default` - 1983.5
2. `vslow_default` - 1868.2
3. `jet_qtgmc_refined` - 1695.9
4. `jet_qtgmc_default` - 1688.1
5. `ai_ddd` - 1567.0

## Metadata

WinDV captured the cleanest files for me, but it did not write the final date
metadata the way Google Photos, Apple Photos, QuickTime, and similar apps expect.
The final MP4 script restores the recording date, camera info, titles, and
QuickTime/XMP metadata so the finished files can be uploaded directly with the
right dates. Replace the camera placeholders with your own camera make/model;
contributor/artist metadata is empty unless you explicitly pass it.

## Final Note

This repo is not a perfect product. It is the toolkit I built while trying to
save my family's tapes properly. Give your AI assistant access to these files if
you want help adapting the workflow, especially for double captures where you
record the same tape twice and take the best frames from each.

Have fun with it. I hope it helps you save your memories.
