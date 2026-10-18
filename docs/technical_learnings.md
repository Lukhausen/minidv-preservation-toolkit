# Technical learnings

## Stitch-review empty page (2026-10-18, issue #1)

With eight synthetic PAL DV AVIs and the unmodified review script, a clean Python
environment without NumPy produces seven caught comparison errors, a cached empty
scan, exit code 0, and a Chrome page showing an empty player with `0 / 0` and no
error message. The same inputs with NumPy/OpenCV available produce seven cases
and playable HTTP-200 MP4 clips. Installing dependencies alone does not recover
an existing failed scan: both `--force-scan --force-build` regenerate it. The
quality-control JSON is not an input to stitch review. This confirms a failure
path, not the original reporter's environment; see `issue_1_reproduction.md` for
the experiment, evidence, and recovery commands.

The fix validates Python imports and FFmpeg tools before using caches, retries
legacy empty scan caches, and refuses to cache a new scan when every comparison
fails. Successful partial scans remain reviewable with visible diagnostics.
`--force-scan` must bypass the case cache as well as the scan cache; otherwise a
nonempty `cases.json` silently defeats a requested rescan. Genuine empty states
hide playback/review actions and ignore keyboard shortcuts so they cannot access
an undefined current case. CLI startup builds cases once to avoid rescanning an
empty result twice.

For the lean UX follow-up, keep empty/loading/error content outside the review
container so all review-only instructions, metadata, timers, and controls hide
together. Scan diagnostics, playback errors, choice-save errors, and timing-save
errors need independent notices: only a successful retry of the relevant action
should clear its warning. Check HTTP response status before confirming a saved
choice or advancing. Keyboard shortcuts must yield to focused buttons and
disclosure summaries so Enter can open diagnostic details without recording a
stitch decision. Chrome checks covered these interactions and the narrow layout.
The browser run also exposed concurrent timing saves sharing one `.json.tmp`
file on Windows (`WinError 32`). A reentrant lock around timing reads/writes
serializes the read-modify-replace operation, including nested reads; a concurrent
save regression test verifies valid output with no leftover temporary file.
