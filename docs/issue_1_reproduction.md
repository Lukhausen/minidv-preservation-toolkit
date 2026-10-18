# Issue #1: browser review displays no clips

Checked on 2026-10-18 against commit `6d62f46` and the unmodified
`scripts/stitch_human_review/serve_broad_visual_review.py`.
Issue: https://github.com/Lukhausen/minidv-preservation-toolkit/issues/1

## Result

Reproduced the reported symptom in Chrome using a clean Python environment
without NumPy/OpenCV. This establishes a real failure path, but does not establish
the reporter's cause: their recordings, environment, and scan logs were not
provided.

Eight synthetic PAL DV AVIs (720x576, 25 fps, PCM stereo audio, two seconds each)
were placed in an isolated `raw_recordings/band1/`. They repeat the same textured
test pattern so the seven adjacent boundaries should all qualify for review.
The script was copied unchanged into each isolated fixture directory because it
resolves input/output paths relative to its own location. No private footage was
used; source hashes matched before and after the scan.

| Environment / action | Successful comparisons | Review cases | Observed result |
| --- | --- | --- | --- |
| Clean Python, no NumPy/OpenCV | 0 of 7 | 0 | Page loads, blank player, `0 / 0`, empty status |
| Python with NumPy/OpenCV, fresh scan | 7 of 7 | 7 | First clip returns HTTP 200 and plays in Chrome |
| Dependencies available after failed scan, ordinary restart | 0 cached rows | 0 | Empty scan is reused |
| Dependencies available, force scan and case rebuild | 7 of 7 | 7 | Cases recover |

The failed build exits with code **0**, despite all seven comparisons failing
with `No module named 'numpy'`. `/api/state` returns an empty cases list. Chrome
reports no page error: the player simply has no video source. The working control
decodes a 720x576 clip, reaches video readyState 4, and advances playback time.

## Why it happens

`compare_pair()` and its helpers import NumPy and OpenCV at scan time. Neither
dependency is listed in the stitch-review README. `build_scan()` catches pair
exceptions, saves them to `data/stitch_human_review/scan_errors.json`, then caches
the successful rows, including an empty list when everything failed. It does not
fail startup. `build_cases()` consequently returns no cases, and the browser's
`renderCase()` returns immediately without explaining the empty state.

An ordinary restart after installing dependencies still reads the cached empty
`full_adjacent_pair_scan.json`; rebuilding cases alone does not rescan.

The source-quality-control report is independent of stitch review. The review
script does not read `dv_cassette_analysis*.json`, and is intended to show selected
adjacent stitch boundaries rather than every source recording or quality error.
The DVRescue quality-control step was not run in this reproduction.

## Diagnostic and recovery commands

Run these from the repository root, using the same Python as the review server:

```powershell
python -c "import numpy, cv2; print(numpy.__version__, cv2.__version__)"
Get-Content .\data\stitch_human_review\scan_errors.json
Get-Content .\data\stitch_human_review\summary.json
```

For the reproduced missing-dependency case, stop the server, then:

```powershell
python -m pip install numpy opencv-python
python .\scripts\stitch_human_review\serve_broad_visual_review.py --force-scan --force-build --port 8765
```

This is a targeted recovery for this failure path, not proof that dependencies
are missing on the reporter's machine. Existing review labels should be backed
up before a rebuild: current case IDs are positional and can change if the
selected candidate set changes.

## Local evidence

Generated evidence is under the ignored `data/issue_1_reproduction/` directory:
`reproduce.py`, `browser_check.cjs`, `results.json`, `browser_results.json`,
`recovery_results.json`, and screenshots `missing_dependencies.png` and
`installed_dependencies.png`. `results.json` preserves the original scan errors;
the failed fixture's scan files were subsequently replaced by the recovery test.
The reproduction harness starts two local servers; those were stopped after
verification. Synthetic capture files use hard links to one generated fixture.

Python 3.13.5; working control: NumPy 2.2.6, OpenCV 5.0.0; FFmpeg
`N-118301-g851a84650e-20250113`. This section records the reproduction before
the fix described below.

## Fix verification

The subsequent local fix adds dependency checks and installation guidance,
prevents completely failed scans from being cached as successes, automatically
retries legacy empty scans, and explains empty results and playback failures in
the page. `--force-scan` also rebuilds cases instead of returning existing cases
before the rescan can run.

Nine standard-library regression tests cover dependencies/tools, total and
partial failures, legacy-cache recovery, forced rescanning, genuine empty input,
reuse of cached cases without changing saved labels, and overlapping timing
saves. Run them with:

```powershell
python -m unittest discover -s scripts/stitch_human_review/tests -v
```

End-to-end validation against isolated synthetic fixtures verified nonzero
startup exits for missing dependencies and completely invalid AVIs, automatic
recovery to seven cases from an old empty cache, and unchanged source hashes.
Chrome checks verified actual playback and label saving, both genuine empty
states with hidden controls and safe keyboard handling, partial-scan warnings,
clip-generation error messages, and an intercepted HTTP-503 state-loading error.
No unhandled browser exceptions were observed. Evidence and screenshots are in
`data/issue_1_reproduction/fix_validation/`.

The subsequent UX cleanup replaces the empty review with one explanation panel,
hides review-only instructions/timers/metadata, and separates persistent scan,
playback, and save notices from save confirmations. Scan filenames and error
details are available inline in a disclosure. Browser validation checks that
warnings survive choice saves, failed saves do not advance, successful retries
clear only the relevant notice, and empty/error layouts work at 480px width.
Evidence: `data/issue_1_reproduction/ux_browser_results.json` and `ux_*.png`.
The suite now has nine passing tests, including overlapping timing saves after
the browser run exposed a Windows temporary-file collision.
