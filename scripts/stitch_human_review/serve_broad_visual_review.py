from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "raw_recordings"
OUT = ROOT / "data" / "stitch_human_review"
SCAN = OUT / "full_adjacent_pair_scan.json"
CLIPS = OUT / "clips"
CASES_PATH = OUT / "cases.json"
SUMMARY_PATH = OUT / "summary.json"
RESULTS_JSON = OUT / "results.json"
RESULTS_TXT = OUT / "results.txt"
STATS_JSON = OUT / "review_stats.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CLIP_SECONDS = 5.0

BUILD_VERSION = 2
W = 160
H = 120
HEAD_FRAMES = 25
TAIL_WINDOW_FRAMES = 25
TAIL_COMPARE_FRAMES = 8

NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2}).*?__tc-(?P<tc>\d{2}-\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def ffprobe_duration(path: Path) -> float:
    proc = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    data = json.loads(proc.stdout)
    return float(data["format"]["duration"])


def parse_name_time(path: Path) -> dt.datetime | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    stamp = f"{m.group('date')} {m.group('time').replace('-', ':')}"
    try:
        return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def sort_key(path: Path) -> tuple[int, dt.datetime | str, str]:
    parsed = parse_name_time(path)
    if parsed is not None:
        return (0, parsed, path.name.casefold())
    return (1, path.name.casefold(), path.name.casefold())


def cassette_dirs() -> list[Path]:
    if not RAW.exists():
        raise FileNotFoundError(f"raw recordings folder not found: {RAW}")
    folders: list[Path] = []
    for path in sorted(RAW.iterdir(), key=lambda p: p.name.casefold()):
        if not path.is_dir():
            continue
        if path.name in {"KEEP", "STITCH", "_dvrescue_best_work_tmp", "__pycache__"}:
            continue
        if path.name.startswith("_"):
            continue
        if any(path.glob("*.avi")):
            folders.append(path)
    return folders


def read_gray_frames(path: Path, mode: str):
    import numpy as np

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if mode == "tail":
        cmd.extend(["-sseof", "-1.0"])
    cmd.extend(
        [
            "-i",
            str(path),
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            str(TAIL_WINDOW_FRAMES if mode == "tail" else HEAD_FRAMES),
            "-vf",
            f"scale={W}:{H},format=gray",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip())
    frame_size = W * H
    count = len(proc.stdout) // frame_size
    if count <= 0:
        raise RuntimeError("no frames decoded")
    arr = np.frombuffer(proc.stdout[: count * frame_size], dtype=np.uint8).reshape(count, H, W)
    if mode == "tail":
        arr = arr[-TAIL_COMPARE_FRAMES:]
    return arr


def corr(a, b) -> float:
    import numpy as np

    af = a.astype(np.float32).ravel()
    bf = b.astype(np.float32).ravel()
    av = af - float(af.mean())
    bv = bf - float(bf.mean())
    denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
    if denom <= 1e-6:
        return 0.0
    return float(np.dot(av, bv) / denom)


def edge_image(frame):
    import cv2

    return cv2.Canny(frame, 50, 150)


def compare_pair(a_path: Path, b_path: Path, cassette: str) -> dict[str, Any]:
    import numpy as np

    a_tail = read_gray_frames(a_path, "tail")
    b_head = read_gray_frames(b_path, "head")
    if len(a_tail) == 0 or len(b_head) == 0:
        raise RuntimeError("empty comparison window")

    a_last = a_tail[-1]
    b_count = min(len(b_head), HEAD_FRAMES)
    corr_series = [corr(a_last, b_head[i]) for i in range(b_count)]
    edge_a = edge_image(a_last)
    edge_series = [corr(edge_a, edge_image(b_head[i])) for i in range(b_count)]

    tail_to_b0 = [corr(a_tail[i], b_head[0]) for i in range(len(a_tail))]
    best_tail_b0 = max(tail_to_b0)
    best_tail_offset = len(a_tail) - 1 - tail_to_b0.index(best_tail_b0)

    first = corr_series[0]
    c1 = corr_series[1] if b_count > 1 else first
    median3 = float(np.median(corr_series[: min(3, b_count)]))
    median5 = float(np.median(corr_series[: min(5, b_count)]))
    median10 = float(np.median(corr_series[: min(10, b_count)]))
    median25 = float(np.median(corr_series[:b_count]))
    edge_first = edge_series[0]
    edge_median5 = float(np.median(edge_series[: min(5, b_count)]))
    immediate_drop = first - c1
    drop5 = first - median5
    drop25 = first - median25
    luma_delta_10 = float(b_head[min(9, b_count - 1)].mean() - b_head[0].mean())
    std_delta_10 = float(b_head[min(9, b_count - 1)].std() - b_head[0].std())

    stale_like = (
        first >= 0.72
        and immediate_drop >= 0.24
        and median5 <= max(0.58, first - 0.28)
        and edge_median5 <= max(0.45, edge_first - 0.25)
    )
    smooth_fade_like = (
        first >= 0.72
        and median3 >= 0.58
        and immediate_drop < 0.24
        and (abs(luma_delta_10) >= 4.0 or abs(std_delta_10) >= 3.0 or median5 >= 0.68)
    )
    strong_like = (
        (median3 >= 0.82 and edge_median5 >= 0.40)
        or (first >= 0.90 and median5 >= 0.72)
        or best_tail_b0 >= 0.93
    )

    score = (
        0.34 * first
        + 0.28 * median3
        + 0.18 * median10
        + 0.12 * edge_median5
        + 0.08 * best_tail_b0
        - max(0.0, immediate_drop - 0.18) * 0.55
    )
    likely_stitch = (strong_like or smooth_fade_like or score >= 0.70) and not stale_like
    near_threshold = 0.58 <= score < 0.70 and not stale_like

    a_dt = parse_name_time(a_path)
    b_dt = parse_name_time(b_path)
    start_delta_s = None
    if a_dt is not None and b_dt is not None:
        start_delta_s = (b_dt - a_dt).total_seconds()

    return {
        "cassette": cassette,
        "a": a_path.name,
        "b": b_path.name,
        "aPath": str(a_path),
        "bPath": str(b_path),
        "first_corr": round(first, 4),
        "corr_b1": round(c1, 4),
        "median3_corr": round(median3, 4),
        "median5_corr": round(median5, 4),
        "median10_corr": round(median10, 4),
        "median25_corr": round(median25, 4),
        "edge_first_corr": round(edge_first, 4),
        "edge_median5_corr": round(edge_median5, 4),
        "immediate_drop": round(immediate_drop, 4),
        "drop5": round(drop5, 4),
        "drop25": round(drop25, 4),
        "luma_delta_10": round(luma_delta_10, 2),
        "std_delta_10": round(std_delta_10, 2),
        "best_tail_b0": round(best_tail_b0, 4),
        "best_tail_offset_frames": int(best_tail_offset),
        "score": round(score, 4),
        "stale_like": bool(stale_like),
        "smooth_fade_like": bool(smooth_fade_like),
        "strong_like": bool(strong_like),
        "likely_stitch": bool(likely_stitch),
        "near_threshold": bool(near_threshold),
        "start_delta_s": start_delta_s,
    }


def all_adjacent_pairs() -> list[tuple[Path, Path, str]]:
    pairs: list[tuple[Path, Path, str]] = []
    for folder in cassette_dirs():
        files = sorted(folder.glob("*.avi"), key=sort_key)
        for a, b in zip(files, files[1:]):
            pairs.append((a, b, folder.name))
    return pairs


def build_scan(force: bool = False, workers: int = 6) -> list[dict[str, Any]]:
    OUT.mkdir(parents=True, exist_ok=True)
    if SCAN.exists() and not force:
        return json.loads(SCAN.read_text(encoding="utf-8"))

    pairs = all_adjacent_pairs()
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    print(f"Scanning {len(pairs)} adjacent pairs with {workers} workers")
    with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_map = {
            pool.submit(compare_pair, a, b, cassette): (a, b, cassette)
            for a, b, cassette in pairs
        }
        for index, future in enumerate(futures.as_completed(future_map), start=1):
            a, b, cassette = future_map[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"cassette": cassette, "a": a.name, "b": b.name, "error": str(exc)})
            if index % 100 == 0 or index == len(pairs):
                print(f"  {index}/{len(pairs)} pairs scanned; rows={len(rows)} errors={len(errors)}", flush=True)

    rows.sort(key=lambda row: (row["cassette"].casefold(), row["a"].casefold(), row["b"].casefold()))
    SCAN.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    (OUT / "scan_errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")
    return rows


def value(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    item = row.get(key)
    if isinstance(item, (int, float)):
        return float(item)
    return default


def pair_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (row["cassette"], row["a"], row["b"])


def load_reviewed_keys() -> set[tuple[str, str, str]]:
    return set()


def time_ok(row: dict[str, Any], limit: float = 300.0) -> bool:
    start_delta = row.get("start_delta_s")
    return start_delta is None or float(start_delta) <= limit


def stale_or_decay(row: dict[str, Any]) -> bool:
    return bool(row.get("stale_like")) or value(row, "immediate_drop") >= 0.10 or value(row, "drop25") >= 0.18


def black_low_texture_trap(row: dict[str, Any]) -> bool:
    return value(row, "first_corr") >= 0.80 and value(row, "edge_median5_corr", 1.0) <= 0.04


def large_gap_visual(row: dict[str, Any]) -> bool:
    start_delta = row.get("start_delta_s")
    return (
        start_delta is not None
        and float(start_delta) > 300.0
        and (
            value(row, "first_corr") >= 0.72
            or value(row, "median10_corr") >= 0.70
            or value(row, "score") >= 0.62
        )
    )


def chain_context_keys(rows: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    by_cassette: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cassette[row["cassette"]].append(row)
    out: set[tuple[str, str, str]] = set()
    for cassette_rows in by_cassette.values():
        for prev, cur in zip(cassette_rows, cassette_rows[1:]):
            if prev["b"] != cur["a"]:
                continue
            prev_good = bool(prev.get("likely_stitch")) and time_ok(prev, 300.0)
            cur_good = bool(cur.get("likely_stitch")) and time_ok(cur, 300.0)
            if (prev_good or cur_good) and (
                0.55 <= value(prev, "score") <= 0.90
                or 0.55 <= value(cur, "score") <= 0.90
            ):
                out.add(pair_key(prev))
                out.add(pair_key(cur))
    return out


def broad_visual_net(row: dict[str, Any], chain_keys: set[tuple[str, str, str]]) -> bool:
    return (
        time_ok(row, 300.0)
        and (
            value(row, "score") >= 0.32
            or value(row, "first_corr") >= 0.60
            or value(row, "median10_corr") >= 0.55
            or value(row, "best_tail_b0") >= 0.65
            or value(row, "edge_median5_corr") >= 0.12
            or bool(row.get("near_threshold"))
            or stale_or_decay(row)
        )
    ) or large_gap_visual(row) or black_low_texture_trap(row) or pair_key(row) in chain_keys


def category_for(row: dict[str, Any], chain_keys: set[tuple[str, str, str]]) -> str:
    if bool(row.get("likely_stitch")) and time_ok(row, 300.0):
        return "Likely Stitch"
    if bool(row.get("near_threshold")):
        return "Near Threshold"
    if pair_key(row) in chain_keys:
        return "Chain Context"
    if stale_or_decay(row):
        return "Stale Or Decay Trap"
    if large_gap_visual(row):
        return "Large Gap Visual Match"
    if black_low_texture_trap(row):
        return "Black Or Low Texture Trap"
    return "Broad Visual Fringe"


def reason_for(row: dict[str, Any], chain_keys: set[tuple[str, str, str]]) -> str:
    reasons: list[str] = []
    if bool(row.get("likely_stitch")):
        reasons.append("old likely")
    if bool(row.get("near_threshold")):
        reasons.append("near-threshold")
    if pair_key(row) in chain_keys:
        reasons.append("chain-context")
    if stale_or_decay(row):
        reasons.append("stale/decay")
    if large_gap_visual(row):
        reasons.append("large-gap visual")
    if black_low_texture_trap(row):
        reasons.append("black/low-texture")
    if value(row, "score") >= 0.32:
        reasons.append("score>=0.32")
    if value(row, "first_corr") >= 0.60:
        reasons.append("first>=0.60")
    if value(row, "median10_corr") >= 0.55:
        reasons.append("med10>=0.55")
    if value(row, "best_tail_b0") >= 0.65:
        reasons.append("tail-offset>=0.65")
    if value(row, "edge_median5_corr") >= 0.12:
        reasons.append("edge>=0.12")
    return ", ".join(reasons)


def clip_seconds_for(row: dict[str, Any]) -> float:
    possible_black_fade = value(row, "first_corr") >= 0.80 and value(row, "edge_median5_corr", 1.0) <= 0.08
    if black_low_texture_trap(row) or possible_black_fade:
        return 5.0
    return 1.0


def build_cases(force: bool = False, force_scan: bool = False, scan_workers: int = 6) -> list[dict[str, Any]]:
    OUT.mkdir(parents=True, exist_ok=True)
    CLIPS.mkdir(parents=True, exist_ok=True)
    if CASES_PATH.exists() and not force:
        existing = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        if existing and all(case.get("buildVersion") == BUILD_VERSION and "clipSeconds" in case for case in existing):
            return existing

    rows = build_scan(force=force_scan, workers=scan_workers)
    reviewed = load_reviewed_keys()
    chain_keys = chain_context_keys(rows)

    selected: list[dict[str, Any]] = []
    skipped_missing = 0
    for row in rows:
        if pair_key(row) in reviewed:
            continue
        if not broad_visual_net(row, chain_keys):
            continue
        if not Path(row["aPath"]).exists() or not Path(row["bPath"]).exists():
            skipped_missing += 1
            continue
        selected.append(row)

    cases: list[dict[str, Any]] = []
    for index, row in enumerate(selected, start=1):
        case_id = f"bv-{index:04d}"
        clip_seconds = clip_seconds_for(row)
        clip_tag = str(int(clip_seconds))
        cases.append(
            {
                "id": case_id,
                "buildVersion": BUILD_VERSION,
                "index": index - 1,
                "category": category_for(row, chain_keys),
                "clipSeconds": clip_seconds,
                "cassette": row["cassette"],
                "a": row["a"],
                "b": row["b"],
                "aPath": row["aPath"],
                "bPath": row["bPath"],
                "aClip": f"clips/{case_id}_a_tail{clip_tag}.mp4",
                "bClip": f"clips/{case_id}_b_head{clip_tag}.mp4",
                "why": reason_for(row, chain_keys),
                "metrics": {
                    "score": row.get("score"),
                    "first_corr": row.get("first_corr"),
                    "median10_corr": row.get("median10_corr"),
                    "median25_corr": row.get("median25_corr"),
                    "edge_median5_corr": row.get("edge_median5_corr"),
                    "immediate_drop": row.get("immediate_drop"),
                    "drop25": row.get("drop25"),
                    "start_delta_s": row.get("start_delta_s"),
                    "likely_stitch": row.get("likely_stitch"),
                    "near_threshold": row.get("near_threshold"),
                    "stale_like": row.get("stale_like"),
                },
            }
        )

    CASES_PATH.write_text(json.dumps(cases, indent=2), encoding="utf-8")
    summary = {
        "source_safety": f"Source AVIs are read-only. This tool writes only review metadata, labels, and access clips under {OUT}.",
        "build_version": BUILD_VERSION,
        "scan_pairs": len(rows),
        "already_labeled_pairs_excluded": len(reviewed),
        "selected_count": len(cases),
        "skipped_missing_source_paths": skipped_missing,
        "default_clip_seconds_each_side": 1.0,
        "black_fade_clip_seconds_each_side": 5.0,
        "black_fade_candidate_count": sum(1 for case in cases if case["clipSeconds"] == 5.0),
        "selector": "Broad visual net with score>=0.32 fringe to target roughly 1800-2000 manual labels.",
        "selected_by_category": dict(Counter(case["category"] for case in cases)),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not RESULTS_JSON.exists():
        write_results({})
    return cases


def read_results() -> dict[str, str]:
    if not RESULTS_JSON.exists():
        return {}
    try:
        raw = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return {str(item["id"]): str(item["decision"]) for item in raw if "id" in item and "decision" in item}
    return {}


def write_results(results: dict[str, str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=2), encoding="utf-8")
    os.replace(tmp, RESULTS_JSON)
    rows = [{"id": case_id, "decision": decision} for case_id, decision in sorted(results.items())]
    tmp_txt = RESULTS_TXT.with_suffix(".txt.tmp")
    tmp_txt.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.replace(tmp_txt, RESULTS_TXT)


def read_stats() -> dict[str, Any]:
    default = {"activeMs": 0, "updatedAt": None, "reviewedCount": 0}
    if not STATS_JSON.exists():
        return default
    try:
        raw = json.loads(STATS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return default
    if not isinstance(raw, dict):
        return default
    active_ms = raw.get("activeMs", 0)
    reviewed_count = raw.get("reviewedCount", 0)
    return {
        "activeMs": max(0, int(active_ms)) if isinstance(active_ms, (int, float)) else 0,
        "updatedAt": raw.get("updatedAt"),
        "reviewedCount": max(0, int(reviewed_count)) if isinstance(reviewed_count, (int, float)) else 0,
    }


def write_stats(stats: dict[str, Any]) -> dict[str, Any]:
    current = read_stats()
    active_ms = stats.get("activeMs", current["activeMs"])
    reviewed_count = stats.get("reviewedCount", current["reviewedCount"])
    saved = {
        "activeMs": max(0, int(active_ms)) if isinstance(active_ms, (int, float)) else current["activeMs"],
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "reviewedCount": max(0, int(reviewed_count)) if isinstance(reviewed_count, (int, float)) else current["reviewedCount"],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STATS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    os.replace(tmp, STATS_JSON)
    return saved


def clip_path(case: dict[str, Any], side: str) -> Path:
    if side == "a":
        return OUT / case["aClip"]
    if side == "b":
        return OUT / case["bClip"]
    raise ValueError(f"bad side: {side}")


def source_path(case: dict[str, Any], side: str) -> Path:
    if side == "a":
        return Path(case["aPath"])
    if side == "b":
        return Path(case["bPath"])
    raise ValueError(f"bad side: {side}")


IN_PROGRESS: set[Path] = set()
IN_PROGRESS_LOCK = threading.Lock()


def make_clip(case: dict[str, Any], side: str, force: bool = False) -> Path:
    dst = clip_path(case, side)
    if dst.exists() and dst.stat().st_size > 0 and not force:
        return dst
    with IN_PROGRESS_LOCK:
        if dst in IN_PROGRESS:
            while dst in IN_PROGRESS:
                IN_PROGRESS_LOCK.release()
                time.sleep(0.1)
                IN_PROGRESS_LOCK.acquire()
            return dst
        IN_PROGRESS.add(dst)
    try:
        src = source_path(case, side)
        clip_seconds = float(case.get("clipSeconds", CLIP_SECONDS))
        dst.parent.mkdir(parents=True, exist_ok=True)
        duration = ffprobe_duration(src)
        start = max(0.0, duration - clip_seconds) if side == "a" else 0.0
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(src),
            "-t",
            f"{clip_seconds:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            "scale=720:-2,setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(dst),
        ]
        proc = run(cmd)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return dst
    finally:
        with IN_PROGRESS_LOCK:
            IN_PROGRESS.discard(dst)


def case_map(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in cases}


def warm_cases(cases: list[dict[str, Any]], start_index: int, count: int) -> None:
    for case in cases[start_index : min(len(cases), start_index + count)]:
        for side in ["a", "b"]:
            try:
                make_clip(case, side)
            except Exception:
                continue


def render_index() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Broad Visual Stitch Review</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: Arial, Helvetica, sans-serif;
      line-height: 1.35;
    }
    body {
      margin: 0;
      background: #f6f6f4;
      color: #20201d;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 5;
      background: #ffffff;
      border-bottom: 1px solid #d8d5cc;
      padding: 12px 18px;
    }
    h1 {
      margin: 0;
      font-size: 20px;
    }
    .sub {
      margin: 4px 0 0;
      color: #5c5a54;
      font-size: 13px;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px;
    }
    .review {
      background: #fff;
      border: 1px solid #d8d5cc;
      border-radius: 8px;
      overflow: hidden;
    }
    .meta {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      padding: 12px;
      border-bottom: 1px solid #ebe8df;
    }
    .pill {
      display: inline-block;
      border-radius: 999px;
      padding: 4px 8px;
      background: #ece9df;
      color: #34332e;
      font-size: 12px;
      margin-right: 6px;
    }
    .files {
      font-family: Consolas, Menlo, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
      margin-top: 8px;
      color: #424039;
    }
    .player {
      padding: 12px;
      position: relative;
    }
    video {
      width: 100%;
      max-height: 66vh;
      background: #111;
      border-radius: 6px;
      display: block;
    }
    .phase {
      margin-top: 8px;
      font-size: 18px;
      font-weight: bold;
      min-height: 26px;
    }
    .timer {
      margin-top: 8px;
      padding: 10px;
      border: 1px solid #d8d5cc;
      border-radius: 8px;
      background: #faf9f4;
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 10px;
    }
    .timer strong {
      display: block;
      font-size: 18px;
    }
    .controls {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    button {
      border: 1px solid #bbb6aa;
      border-radius: 6px;
      background: #fff;
      color: #20201d;
      padding: 8px 10px;
      font: inherit;
      cursor: pointer;
    }
    button.primary {
      font-weight: bold;
      border-color: #44724d;
    }
    button.danger {
      border-color: #9d3838;
    }
    button.warn {
      border-color: #9a7126;
    }
    .paused {
      color: #9a7126;
    }
    .status {
      margin-top: 8px;
      color: #5c5a54;
      font-size: 13px;
    }
    .feedback {
      position: absolute;
      right: 22px;
      top: 22px;
      display: none;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 999px;
      border: 2px solid transparent;
      background: rgba(255, 255, 255, 0.94);
      box-shadow: 0 8px 24px rgba(0,0,0,0.18);
      font-weight: bold;
      font-size: 16px;
      z-index: 4;
    }
    .feedback.show {
      display: flex;
      animation: flashPulse 560ms ease-out;
    }
    .feedback::before {
      content: "";
      width: 18px;
      height: 18px;
      border-radius: 999px;
      display: inline-block;
      background: currentColor;
      box-shadow: 0 0 18px currentColor;
    }
    .feedback.works {
      color: #14813a;
      border-color: #14813a;
    }
    .feedback.bad {
      color: #b92f2f;
      border-color: #b92f2f;
    }
    .feedback.unsure {
      color: #b78110;
      border-color: #b78110;
    }
    @keyframes flashPulse {
      0% { transform: scale(0.92); opacity: 0; }
      18% { transform: scale(1.04); opacity: 1; }
      100% { transform: scale(1); opacity: 1; }
    }
    @media (max-width: 760px) {
      .meta, .timer {
        grid-template-columns: 1fr;
      }
    }
    @media (prefers-color-scheme: dark) {
      body { background: #1f1f1b; color: #f2f0ea; }
      header, .review, button { background: #282821; color: #f2f0ea; }
      header, .review, .meta, .timer { border-color: #4a473e; }
      .sub, .files, .status { color: #c8c1b2; }
      .pill { background: #3a372f; color: #f2f0ea; }
      .timer { background: #24231e; }
      .feedback { background: rgba(35, 34, 29, 0.94); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Broad Visual Stitch Review</h1>
    <p class="sub">Regular candidates play 1 second from A and 1 second from B; black/low-texture fade candidates play 5 seconds each. Keys: Enter = good stitch, Delete/Entf/Backspace = wrong stitch, A = unsure, Space = replay current pair, P = pause/resume timer and playback. Every label is saved immediately.</p>
  </header>
  <main>
    <section class="review">
      <div class="meta">
        <div>
          <div id="badges"></div>
          <div class="files" id="files"></div>
        </div>
        <div>
          <span class="pill" id="decisionPill">unreviewed</span>
        </div>
      </div>
      <div class="player">
        <div class="feedback" id="feedback"></div>
        <video id="video" controls preload="metadata"></video>
        <div class="phase" id="phase"></div>
        <div class="timer">
          <div><span>Progress</span><strong id="progress">0 / 0</strong></div>
          <div><span>Active time</span><strong id="activeTime">00:00</strong></div>
          <div><span>Speed</span><strong id="speed">-</strong></div>
          <div><span>Estimated left</span><strong id="eta">-</strong></div>
        </div>
        <div class="controls">
          <button class="primary" onclick="mark('works')">Enter Good Stitch</button>
          <button class="danger" onclick="mark('bad')">Delete/Entf Wrong Stitch</button>
          <button class="warn" onclick="mark('unsure')">A Unsure</button>
          <button onclick="replay(true)">Space Replay</button>
          <button onclick="togglePause()">P Pause/Resume</button>
          <button onclick="go(-1)">Previous</button>
          <button onclick="go(1)">Next</button>
        </div>
        <div class="status" id="status"></div>
      </div>
    </section>
  </main>
  <script>
    let cases = [];
    let decisions = {};
    let index = 0;
    let phase = 'a';
    let paused = true;
    let activeMs = 0;
    let activeStartedAt = null;
    let sessionDecisionCount = 0;
    let sessionStartDecisions = 0;
    let lastStatsSaveMs = 0;
    const video = document.getElementById('video');

    function fmtDuration(ms) {
      if (!isFinite(ms) || ms < 0) return '-';
      const total = Math.round(ms / 1000);
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
      return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    }

    function reviewedCount() {
      return Object.values(decisions).filter(v => ['works','bad','unsure'].includes(v)).length;
    }

    function effectiveActiveMs() {
      if (!paused && activeStartedAt !== null) return activeMs + (Date.now() - activeStartedAt);
      return activeMs;
    }

    function updateTimer() {
      const done = reviewedCount();
      const remaining = Math.max(0, cases.length - done);
      const elapsed = effectiveActiveMs();
      const speed = done > 0 && elapsed > 0 ? done / (elapsed / 60000) : 0;
      document.getElementById('progress').textContent = `${done} / ${cases.length}`;
      document.getElementById('activeTime').textContent = fmtDuration(elapsed);
      document.getElementById('speed').textContent = speed > 0 ? `${speed.toFixed(1)} / min` : '-';
      document.getElementById('eta').textContent = speed > 0 ? fmtDuration((remaining / speed) * 60000) : '-';
      document.getElementById('phase').classList.toggle('paused', paused);
    }

    function current() {
      return cases[index];
    }

    function decisionFor(id) {
      return decisions[id] || 'unreviewed';
    }

    async function loadState() {
      const response = await fetch('/api/state');
      const data = await response.json();
      cases = data.cases;
      decisions = data.decisions || {};
      activeMs = Math.max(0, Number(data.stats?.activeMs || 0));
      sessionStartDecisions = reviewedCount();
      const firstOpen = cases.findIndex(c => !['works','bad','unsure'].includes(decisionFor(c.id)));
      index = firstOpen >= 0 ? firstOpen : 0;
      renderCase();
      updateTimer();
      setInterval(updateTimer, 500);
      setInterval(saveStatsIfNeeded, 2000);
    }

    function clipUrl(side) {
      return `/clip/${current().id}/${side}.mp4`;
    }

    function warmAhead() {
      fetch(`/api/warm?index=${index}&count=4`).catch(() => {});
    }

    async function saveStats(force = false) {
      const now = Date.now();
      if (!force && now - lastStatsSaveMs < 1500) return;
      lastStatsSaveMs = now;
      const payload = {
        activeMs: effectiveActiveMs(),
        reviewedCount: reviewedCount()
      };
      try {
        await fetch('/api/stats', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
      } catch (error) {
        document.getElementById('status').textContent = 'Stats save failed; labels still save separately.';
      }
    }

    function saveStatsIfNeeded() {
      if (!paused) saveStats(false);
    }

    function flash(value) {
      const node = document.getElementById('feedback');
      const label = value === 'works' ? 'WORKS' : value === 'bad' ? 'BAD' : 'UNSURE';
      node.textContent = label;
      node.className = `feedback ${value}`;
      void node.offsetWidth;
      node.classList.add('show');
      window.clearTimeout(node._hideTimer);
      node._hideTimer = window.setTimeout(() => {
        node.classList.remove('show');
      }, 650);
    }

    function renderCase() {
      if (!cases.length) return;
      const c = current();
      const d = decisionFor(c.id);
      document.getElementById('badges').innerHTML =
        `<span class="pill">${c.id}</span><span class="pill">${index + 1} / ${cases.length}</span><span class="pill">${c.clipSeconds}s clips</span><span class="pill">${c.cassette}</span><span class="pill">${c.category}</span><span class="pill">${c.why}</span>`;
      document.getElementById('decisionPill').textContent = d;
      document.getElementById('files').innerHTML =
        `<strong>A tail</strong><br>${c.a}<br><strong>B head</strong><br>${c.b}`;
      phase = 'a';
      setVideoSource();
      warmAhead();
      updateTimer();
    }

    function setVideoSource() {
      const c = current();
      video.onended = null;
      video.src = clipUrl(phase);
      video.load();
      const seconds = current().clipSeconds || 1;
      const label = phase === 'a' ? `A tail: last ${seconds} second${seconds === 1 ? '' : 's'}` : `B head: first ${seconds} second${seconds === 1 ? '' : 's'}`;
      document.getElementById('phase').textContent = paused ? `${label} - paused` : label;
      video.onended = () => {
        if (phase === 'a') {
          phase = 'b';
          setVideoSource();
          if (!paused) video.play().catch(() => {});
        } else {
          document.getElementById('phase').textContent = paused ? 'Finished - paused' : 'Finished. Label it.';
        }
      };
      if (!paused) video.play().catch(() => {});
    }

    function startTimerIfNeeded() {
      if (!paused) return;
      paused = false;
      activeStartedAt = Date.now();
      video.play().catch(() => {});
      updateTimer();
    }

    function pauseTimer() {
      if (paused) return;
      activeMs = effectiveActiveMs();
      activeStartedAt = null;
      paused = true;
      video.pause();
      updateTimer();
      saveStats(true);
      const seconds = current().clipSeconds || 1;
      const label = phase === 'a' ? `A tail: last ${seconds} second${seconds === 1 ? '' : 's'}` : `B head: first ${seconds} second${seconds === 1 ? '' : 's'}`;
      document.getElementById('phase').textContent = `${label} - paused`;
    }

    function togglePause() {
      if (paused) {
        paused = false;
        activeStartedAt = Date.now();
        video.play().catch(() => {});
      } else {
        pauseTimer();
      }
      updateTimer();
    }

    async function mark(value) {
      const c = current();
      const old = decisionFor(c.id);
      decisions[c.id] = value;
      if (!['works','bad','unsure'].includes(old)) sessionDecisionCount += 1;
      flash(value);
      await fetch('/api/decision', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: c.id, decision: value})
      });
      await saveStats(true);
      document.getElementById('status').textContent = `Saved ${c.id}: ${value}`;
      goToNextUnreviewed();
    }

    function goToNextUnreviewed() {
      for (let i = index + 1; i < cases.length; i++) {
        if (!['works','bad','unsure'].includes(decisionFor(cases[i].id))) {
          index = i;
          renderCase();
          return;
        }
      }
      for (let i = 0; i < index; i++) {
        if (!['works','bad','unsure'].includes(decisionFor(cases[i].id))) {
          index = i;
          renderCase();
          return;
        }
      }
      pauseTimer();
      saveStats(true);
      document.getElementById('phase').textContent = 'All done.';
      updateTimer();
    }

    function replay(startIfPaused = false) {
      if (startIfPaused && paused) {
        paused = false;
        activeStartedAt = Date.now();
      }
      phase = 'a';
      setVideoSource();
    }

    function go(delta) {
      index = Math.max(0, Math.min(cases.length - 1, index + delta));
      renderCase();
    }

    document.addEventListener('keydown', (event) => {
      if (event.repeat) return;
      if (event.key === 'Enter') {
        event.preventDefault();
        startTimerIfNeeded();
        mark('works');
      } else if (event.key === 'Delete' || event.key === 'Backspace' || event.key === 'Entf') {
        event.preventDefault();
        startTimerIfNeeded();
        mark('bad');
      } else if (event.key === 'a' || event.key === 'A') {
        event.preventDefault();
        startTimerIfNeeded();
        mark('unsure');
      } else if (event.code === 'Space') {
        event.preventDefault();
        replay(true);
      } else if (event.key === 'p' || event.key === 'P') {
        event.preventDefault();
        togglePause();
      } else if (event.key === 'r' || event.key === 'R') {
        event.preventDefault();
        replay(true);
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        go(1);
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault();
        go(-1);
      }
    });

    window.addEventListener('beforeunload', () => {
      const payload = JSON.stringify({activeMs: effectiveActiveMs(), reviewedCount: reviewedCount()});
      navigator.sendBeacon('/api/stats', new Blob([payload], {type: 'application/json'}));
    });

    loadState();
  </script>
</body>
</html>
"""


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "BroadVisualReview/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def cases(self) -> list[dict[str, Any]]:
        return self.server.cases  # type: ignore[attr-defined]

    @property
    def case_by_id(self) -> dict[str, dict[str, Any]]:
        return self.server.case_by_id  # type: ignore[attr-defined]

    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(data).encode("utf-8"), "application/json", status)

    def send_file(self, path: Path) -> None:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_bytes(render_index().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self.send_json({"cases": self.cases, "decisions": read_results(), "stats": read_stats()})
            return
        if path == "/api/warm":
            query = urllib.parse.parse_qs(parsed.query)
            start = int(query.get("index", ["0"])[0])
            count = int(query.get("count", ["4"])[0])
            threading.Thread(target=warm_cases, args=(self.cases, start, count), daemon=True).start()
            self.send_json({"ok": True})
            return
        if path.startswith("/clip/") and path.endswith(".mp4"):
            parts = path.strip("/").split("/")
            if len(parts) != 3:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            _, case_id, filename = parts
            side = filename.split(".")[0]
            if side not in {"a", "b"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            case = self.case_by_id.get(case_id)
            if case is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                clip = make_clip(case, side)
                self.send_file(clip)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/stats":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                self.send_json({"error": "invalid JSON"}, status=400)
                return
            saved = write_stats(data)
            self.send_json({"ok": True, "stats": saved})
            return

        if parsed.path != "/api/decision":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_json({"error": "invalid JSON"}, status=400)
            return
        case_id = str(data.get("id", ""))
        decision = str(data.get("decision", ""))
        if case_id not in self.case_by_id:
            self.send_json({"error": "unknown id"}, status=404)
            return
        if decision not in {"works", "bad", "unsure", "unreviewed"}:
            self.send_json({"error": "bad decision"}, status=400)
            return
        results = read_results()
        if decision == "unreviewed":
            results.pop(case_id, None)
        else:
            results[case_id] = decision
        write_results(results)
        write_stats({"reviewedCount": len(results)})
        self.send_json({"ok": True, "saved": {case_id: decision}, "count": len(results)})


def serve(port: int, force_build: bool = False, force_scan: bool = False, scan_workers: int = 6) -> None:
    cases = build_cases(force=force_build, force_scan=force_scan, scan_workers=scan_workers)
    server = ThreadingHTTPServer((HOST, port), ReviewHandler)
    server.cases = cases  # type: ignore[attr-defined]
    server.case_by_id = case_map(cases)  # type: ignore[attr-defined]
    print(f"Broad visual review server running: http://{HOST}:{port}/")
    print(f"Cases: {len(cases)}")
    print(f"Results: {RESULTS_TXT}")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a keyboard-driven broad visual stitch review page.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--force-build", action="store_true")
    parser.add_argument("--force-scan", action="store_true")
    parser.add_argument("--scan-workers", type=int, default=6)
    args = parser.parse_args()
    cases = build_cases(force=args.force_build, force_scan=args.force_scan, scan_workers=args.scan_workers)
    if args.build_only:
        print(json.dumps(json.loads(SUMMARY_PATH.read_text(encoding="utf-8")), indent=2))
        return 0
    serve(args.port, force_build=False, force_scan=False, scan_workers=args.scan_workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
