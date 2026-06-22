# Final MP4 Access-Copy Pipeline

This is the script folder for making the final watchable MP4 archive from the
raw MiniDV captures.

It treats the raw files as input only. Heavy work happens in a temp/state folder,
then only validated finished MP4s are published to the output folder.

## Main Scripts

- `batch_convert_raw_to_mp4.py` - the real batch runner. It discovers source
  AVI/DV files, runs preflight checks, converts files, stitches approved MP4
  chains, validates metadata, writes reports, and can resume after interruption.
- `dv_to_mp4_access_copy.py` - converts one DV AVI to one QTGMC/H.264/AAC MP4.
  It reads embedded DV date/time with DVRescue and writes QuickTime/XMP metadata
  with ExifTool.
- `verify_final_mp4_archive.py` - read-only verifier. It compares source
  durations/frame counts to final MP4 durations/frame counts and can full-decode
  the MP4s to check for corruption.
- `render_backend/render_qtgmc_access_copy.py` - the VapourSynth/QTGMC renderer
  used by the converter.

## Typical Run

```powershell
python .\scripts\final_mp4_access_copy\batch_convert_raw_to_mp4.py --preflight-only --stitch-mode after --camera-make "Your Camera Make" --camera-model "Your Camera Model"
python .\scripts\final_mp4_access_copy\batch_convert_raw_to_mp4.py --output-root ".\final_mp4_access_copies" --stitch-mode after --camera-make "Your Camera Make" --camera-model "Your Camera Model"
```

Use `--output-root` to put the final MP4s on another drive. The temp/state work
still stays under `TEMP/mp4_batch_state` unless you pass `--state-root`.

## Metadata

The pipeline writes the recording date, title, camera make/model, source
reference, and processing history into QuickTime Keys and XMP fields. Replace
the camera placeholders with your own values using `--camera-make` and
`--camera-model`.

Contributor metadata is optional. If you want Windows `Mitwirkende
Interpreten`, pass `--contributor "Your Family Name"`. If you leave it empty,
no contributor/artist metadata is written.

## Stitching

MP4 stitching uses the human-reviewed decision file:

`data/stitching/stitch_decisions_v1.json`

Stitch components are internal temp files. Public output should be either one
standalone MP4 or one stitched MP4 in the cassette folder.
