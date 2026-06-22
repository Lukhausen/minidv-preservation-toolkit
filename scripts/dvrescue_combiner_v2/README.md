# DVRescue Combiner V2

This is the best DVRescue / double-capture combiner I kept from the project.
Use it when you captured the same tape or recording more than once and want one
best DV/AVI KEEP result per cluster.

This is not the final MP4 converter. It works on DV/AVI preservation material
and writes a `KEEP` folder plus a detailed JSON report.

Important: use `--v2`. The older mode still runs, but V2 is the improved
variant and the only one I would recommend now. It clusters by better DV timing
evidence, handles timecode more carefully, and stitches much more reliably.

## What It Does

- analyzes one cassette folder with ffprobe and DVRescue
- clusters files by embedded DV evidence, not by filename
- in `--v2`, uses recording date/time as the global cluster anchor
- stitches inside a cluster by DV timecode, with absolute-track checks at seams
- builds a rough raw DV master first, then lets DVRescue improve it only if it
  preserves the timeline/frame count and does not increase detected problems
- writes DV-derived AVI metadata and filesystem dates
- can deep-verify that output frames come from source frames

## Run

Dry run first:

```powershell
python .\scripts\dvrescue_combiner_v2\dvrescue_combiner_v2.py .\raw_recordings\example_tape --v2 --camera-model "Your Camera Model"
```

Write the `KEEP` outputs:

```powershell
python .\scripts\dvrescue_combiner_v2\dvrescue_combiner_v2.py .\raw_recordings\example_tape --v2 --apply --deep-verify --camera-model "Your Camera Model"
```

`--camera-model` only affects the public KEEP AVI title/comment metadata. The
default is a placeholder, so replace it for your own camera.

## Safety

By default it does not delete source AVI files. Source deletion exists only
behind explicit guarded flags, and the script blocks deletion if coverage,
duplicates, or deep verification are not proven safe.

Public KEEP names come from embedded DV recording time, the cassette folder, and
DV timecode. Input filenames are treated as labels, not truth.
