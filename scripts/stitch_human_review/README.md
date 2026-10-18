# Stitch Human Review

This is the browser tool I used to label soft-cut stitch candidates by hand.

The script scans adjacent videos in `raw_recordings`, uses a broad visual net to
find likely or suspicious pairs, then serves a keyboard review page. Regular
pairs show the last second of A and the first second of B. Black/low-texture
fade candidates show five seconds each.

## Run

Requires Python, NumPy, OpenCV, and the `ffmpeg` and `ffprobe` tools in PATH.
Install the Python dependencies using the same Python that runs the server:

```powershell
python -m pip install -r .\scripts\stitch_human_review\requirements.txt
```

Put at least two `.avi` files directly in each `raw_recordings/<tape>/` folder.
This tool reviews selected adjacent stitch candidates, not every recording.
The source-quality-control report (`dv_cassette_analysis*.json`) is separate and
is not needed by this tool.

```powershell
python .\scripts\stitch_human_review\serve_broad_visual_review.py --port 8765
```

Useful options:

```powershell
python .\scripts\stitch_human_review\serve_broad_visual_review.py --force-scan --force-build
python .\scripts\stitch_human_review\serve_broad_visual_review.py --build-only
```

`--force-scan` also rebuilds the cases. Use it after changing the input files or
fixing scan errors. Back up existing review labels before changing the candidate
set: case IDs are positional.

## If No Clips Appear

Startup checks the required dependencies and stops with installation guidance
if any are missing. If every pair fails to scan, startup stops and reports the
first error; details are in `data/stitch_human_review/scan_errors.json`. Restart
after fixing the problem. Empty scan caches from older versions are retried
automatically.

If there are no adjacent pairs or no pairs match the stitch criteria, the page
shows one explanation panel and hides the review instructions, player, timers,
and controls. Partial scan failures stay visible while you review; expand the
warning for affected filenames, error details, and next steps. Playback and save
errors have separate notices, so saving a choice cannot hide them. Notices clear
when the relevant action succeeds. Fix scan errors and restart with `--force-scan`
to retry skipped pairs.

## What It Writes

It writes generated review clips, cases, labels, timing stats, and summaries
under:

`data/stitch_human_review`

The source videos are only read. Labels are saved immediately in the backend, so
closing the browser does not lose your work.

## Keys

- `Enter` - good stitch
- `Delete`, `Entf`, or `Backspace` - wrong stitch
- `A` - unsure
- `Space` - replay the current pair
- `P` - pause or resume the active-time timer
