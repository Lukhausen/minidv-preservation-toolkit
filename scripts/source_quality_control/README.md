# Source Quality Control

This is a cassette-level checker for raw DV AVI captures.

It does not repair anything. It audits one folder, prints a summary, and writes
a timestamped JSON report into that same cassette folder.

## What It Checks

- ffprobe stream structure, codec, duration, frame/packet readability, bitrate,
  resolution, and audio format
- DVRescue frame-level errors, concealment, timecode breaks, and audio/video
  problem frames
- ffmpeg full-decode filters for frozen frames, black sections, and silence
- a top impacted-files list for manual review

## Run

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\source_quality_control\dv_cassette_check.ps1 .\raw_recordings\example_tape
```

It expects ffprobe/ffmpeg in PATH and `DVRescue.exe` in the repo root, the
parent folder, or PATH.
