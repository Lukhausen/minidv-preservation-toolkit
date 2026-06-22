# Stitch Human Review

This is the browser tool I used to label soft-cut stitch candidates by hand.

The script scans adjacent videos in `raw_recordings`, uses a broad visual net to
find likely or suspicious pairs, then serves a keyboard review page. Regular
pairs show the last second of A and the first second of B. Black/low-texture
fade candidates show five seconds each.

## Run

```powershell
python .\scripts\stitch_human_review\serve_broad_visual_review.py --port 8765
```

Useful options:

```powershell
python .\scripts\stitch_human_review\serve_broad_visual_review.py --force-scan --force-build
python .\scripts\stitch_human_review\serve_broad_visual_review.py --build-only
```

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
