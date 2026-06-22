# Render Backend

This is the small VapourSynth/QTGMC backend used by the MP4 converter. Most
people should not run it directly; `dv_to_mp4_access_copy.py` calls it for you.

It loads PAL DV, forces BFF/BT.601 metadata, fixes PAL DV chroma handling by
promoting to YUV444P16 before deinterlacing, then renders either a PNG frame, a
short MP4 clip, or a full deinterlaced video stream.

## Files

- `render_qtgmc_access_copy.py` - renderer called with `--input`, `--mode`,
  `--frame 0`, and usually `--full-video`.
- `config.json` - the same QTGMC and baseline deinterlacer mode definitions
  used by the comparison suite.

The full browser comparison app is not here. That lives in
`scripts/deinterlacer_comparison_suite`.
