import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "serve_broad_visual_review.py"
spec = importlib.util.spec_from_file_location("review", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ReviewScanTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(temp)
        out = root / "data/stitch_human_review"
        self.enterContext(patch.multiple(
            review, ROOT=root, RAW=root / "raw_recordings", OUT=out,
            SCAN=out / "full_adjacent_pair_scan.json", SCAN_ERRORS=out / "scan_errors.json",
            CLIPS=out / "clips", CASES_PATH=out / "cases.json", SUMMARY_PATH=out / "summary.json",
            RESULTS_JSON=out / "results.json", RESULTS_TXT=out / "results.txt",
            STATS_JSON=out / "review_stats.json",
        ))
        self.folder = review.RAW / "band1"
        self.folder.mkdir(parents=True)
        self.a = self.folder / "01.avi"
        self.b = self.folder / "02.avi"
        self.a.touch()
        self.b.touch()
        self.row = {
            "cassette": "band1", "a": self.a.name, "b": self.b.name,
            "aPath": str(self.a), "bPath": str(self.b),
            "score": 1.0, "first_corr": 1.0, "edge_median5_corr": 1.0,
            "likely_stitch": True,
        }

    def test_missing_dependencies_give_install_command(self):
        with patch.object(review.importlib, "import_module", side_effect=ImportError("missing")), \
                patch.object(review.shutil, "which", return_value="tool"):
            with self.assertRaises(RuntimeError) as raised:
                review.check_dependencies()
        message = str(raised.exception)
        for expected in ("numpy", "cv2", "pip install", "requirements.txt"):
            self.assertIn(expected, message)
        self.assertFalse(review.OUT.exists())

    def test_missing_ffmpeg_tools_are_actionable(self):
        with patch.object(review.importlib, "import_module"), \
                patch.object(review.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "PATH: ffmpeg, ffprobe"):
                review.check_dependencies()

    def test_total_failure_writes_diagnostics_but_no_success_cache(self):
        with patch.object(review, "compare_pair", side_effect=RuntimeError("decode failed")):
            with self.assertRaisesRegex(RuntimeError, "All 1.*failed.*decode failed"):
                review.build_cases()
        self.assertEqual(review.read_scan_errors()[0]["error"], "decode failed")
        self.assertFalse(review.SCAN.exists())
        self.assertFalse(review.CASES_PATH.exists())

    def test_legacy_empty_failed_cache_recovers_without_force(self):
        review.OUT.mkdir(parents=True)
        review.SCAN.write_text("[]")
        review.CASES_PATH.write_text("[]")
        review.SCAN_ERRORS.write_text('[{"error": "No module named numpy"}]')
        with patch.object(review, "compare_pair", return_value=self.row):
            cases = review.build_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(review.read_scan_errors(), [])

    def test_force_scan_rebuilds_previously_nonempty_cases(self):
        with patch.object(review, "compare_pair", return_value=self.row):
            self.assertEqual(len(review.build_cases()), 1)
        no_match = {**self.row, "score": 0, "first_corr": 0, "edge_median5_corr": 0,
                    "likely_stitch": False}
        with patch.object(review, "compare_pair", return_value=no_match) as compare:
            self.assertEqual(review.build_cases(force_scan=True), [])
        compare.assert_called_once()
        summary = json.loads(review.SUMMARY_PATH.read_text())
        self.assertIn("none matched", summary["empty_reason"])

    def test_no_adjacent_pairs_is_valid_and_explained(self):
        self.b.unlink()
        self.assertEqual(review.build_cases(), [])
        summary = json.loads(review.SUMMARY_PATH.read_text())
        self.assertIn("at least two .avi", summary["empty_reason"])
        self.assertEqual(summary["scan_error_count"], 0)

    def test_partial_failure_keeps_reviewable_pairs_and_reports_errors(self):
        (self.folder / "03.avi").touch()
        with patch.object(review, "compare_pair", side_effect=[self.row, RuntimeError("bad capture")]):
            cases = review.build_cases(scan_workers=1)
        self.assertEqual(len(cases), 1)
        summary = json.loads(review.SUMMARY_PATH.read_text())
        self.assertEqual(summary["scan_error_count"], 1)
        self.assertEqual(review.read_scan_errors()[0]["error"], "bad capture")

    def test_cached_cases_preserve_labels_and_skip_rescan(self):
        with patch.object(review, "compare_pair", return_value=self.row):
            cases = review.build_cases()
        review.write_results({cases[0]["id"]: "works"})
        with patch.object(review, "compare_pair", side_effect=AssertionError("unexpected scan")):
            self.assertEqual(review.build_cases(), cases)
        self.assertEqual(review.read_results(), {cases[0]["id"]: "works"})

    def test_overlapping_stats_saves_leave_valid_stats_and_no_temp_file(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            saved = list(pool.map(lambda value: review.write_stats({"activeMs": value}), range(40)))
        self.assertEqual(len(saved), 40)
        self.assertIn(review.read_stats()["activeMs"], range(40))
        self.assertFalse(review.STATS_JSON.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
