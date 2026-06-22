# Scripts

This folder is the reusable part of the MiniDV project. I removed the one-off
experiments and kept the tools that could actually help someone else save old
tapes.

## What To Use

- `final_mp4_access_copy` - the final watchable MP4 pipeline. It deinterlaces,
  encodes, stitches approved MP4 chains, writes photo-app metadata, resumes
  after crashes, and verifies the result.
- `deinterlacer_comparison_suite` - the browser shootout I used to compare
  deinterlacers, QTGMC modes, and MP4 compression settings.
- `dvrescue_combiner_v2` - the best DVRescue / double-capture combiner. This is
  for building better DV/AVI KEEP masters before making MP4 view copies.
- `stitch_human_review` - the keyboard-driven browser tool I used to label
  visual stitch candidates by hand.
- `source_quality_control` - a cassette-level DV quality checker using
  ffprobe, ffmpeg filters, and DVRescue.

## Important

The MP4 pipeline is for watchable access copies. The DVRescue combiner is for
DV/AVI source cleanup. They are related, but they are not the same tool.

Most scripts write reports, temp files, or generated outputs. Read the README in
each folder before running anything on your own captures.
