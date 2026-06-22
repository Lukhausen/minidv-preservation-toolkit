#!/usr/bin/env python3
"""Verify final MP4 access-copy archive against immutable raw AVI sources.

The verifier is read-only with respect to source and output media. It writes
JSON/JSONL reports under TEMP so the check is resumable and inspectable.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_ROOT = ROOT / "raw_recordings"
DEFAULT_OUTPUT_ROOT = ROOT / "final_mp4_access_copies"
DEFAULT_STATE_ROOT = ROOT / "TEMP" / "mp4_batch_state"
DEFAULT_STITCH_DECISIONS = ROOT / "data" / "stitching" / "stitch_decisions_v1.json"
DEFAULT_REPORT_DIR = ROOT / "TEMP" / "final_mp4_archive_verification"

SOURCE_EXTENSIONS = {".avi", ".dv"}
DV_PAL_FPS = 25.0
MP4_TARGET_FPS = 50.0
FRAME_TOLERANCE = 0
DURATION_TOLERANCE_SECONDS = 0.08
FRAME_DURATION_MISMATCH_TOLERANCE_SECONDS = 0.2


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_json(cmd: list[str | Path], timeout: int = 120) -> dict:
    proc = subprocess.run(
        [str(part) for part in cmd],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout)[-4000:])
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON parse failed: {exc}; output={proc.stdout[-1000:]!r}") from exc


def parse_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    if "/" in value:
        num, den = value.split("/", 1)
        den_float = float(den)
        if den_float == 0:
            return None
        return float(num) / den_float
    return float(value)


def discover_sources(source_root: Path) -> list[Path]:
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in SOURCE_EXTENSIONS
    )


def output_for(source: Path, source_root: Path, output_root: Path) -> Path:
    return (output_root / source.relative_to(source_root)).with_suffix(".mp4")


def chain_id_for(sources: list[Path], source_root: Path) -> str:
    rels = [source.relative_to(source_root).as_posix().lower() for source in sources]
    return hashlib.sha1("\n".join(rels).encode("utf-8")).hexdigest()[:12]


def load_stitch_chains(decisions_path: Path, source_root: Path, stitch_output_root: Path) -> list[dict]:
    if not decisions_path.exists():
        return []
    data = json.loads(decisions_path.read_text(encoding="utf-8"))
    outgoing: dict[Path, tuple[Path, dict]] = {}
    incoming: dict[Path, tuple[Path, dict]] = {}
    nodes: set[Path] = set()
    for edge in data.get("stitch") or []:
        a = Path(edge["aPath"]).resolve()
        b = Path(edge["bPath"]).resolve()
        outgoing[a] = (b, edge)
        incoming[b] = (a, edge)
        nodes.add(a)
        nodes.add(b)

    starts = sorted((node for node in nodes if node not in incoming), key=lambda item: str(item).lower())
    visited: set[Path] = set()
    chains: list[dict] = []
    for start in starts:
        current = start
        sources = [current]
        edges: list[dict] = []
        while current in outgoing:
            if current in visited:
                raise RuntimeError(f"Cycle in stitch graph near {current}")
            visited.add(current)
            nxt, edge = outgoing[current]
            edges.append(edge)
            sources.append(nxt)
            current = nxt
        visited.add(current)
        chain_id = chain_id_for(sources, source_root)
        cassette = sources[0].relative_to(source_root).parts[0]
        out_name = f"{sources[0].stem}__stitched-{len(sources)}files__{chain_id}.mp4"
        chains.append(
            {
                "id": chain_id,
                "cassette": cassette,
                "sources": sources,
                "edges": edges,
                "output": stitch_output_root / cassette / out_name,
            }
        )
    if nodes - visited:
        raise RuntimeError("Stitch graph has unvisited nodes")
    return chains


def ffprobe_media(path: Path, count_frames_if_missing: bool = True) -> dict:
    base_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,duration,nb_frames,sample_rate,channels",
        "-of",
        "json",
        path,
    ]
    data = run_json(base_cmd)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    frames = video.get("nb_frames")
    if (frames is None or frames == "N/A") and count_frames_if_missing:
        count_data = run_json(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "json",
                path,
            ],
            timeout=600,
        )
        count_stream = (count_data.get("streams") or [{}])[0]
        frames = count_stream.get("nb_read_frames")
    fmt = data.get("format") or {}
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "format_duration_seconds": float(fmt.get("duration") or 0),
        "video_codec": video.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "pix_fmt": video.get("pix_fmt"),
        "r_frame_rate": video.get("r_frame_rate"),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "video_duration_seconds": float(video.get("duration") or fmt.get("duration") or 0),
        "video_frames": int(frames) if frames not in (None, "N/A") else None,
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": int(audio.get("sample_rate") or 0) if audio.get("sample_rate") else None,
        "audio_channels": int(audio.get("channels") or 0) if audio.get("channels") else None,
    }


def probe_one(path: Path) -> dict:
    try:
        return ffprobe_media(path)
    except Exception as exc:  # noqa: BLE001
        return {"path": str(path), "exists": path.exists(), "probe_error": str(exc)[-4000:]}


def count_video_frames(path: Path) -> int:
    count_data = run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "json",
            path,
        ],
        timeout=1800,
    )
    count_stream = (count_data.get("streams") or [{}])[0]
    frames = count_stream.get("nb_read_frames")
    if frames in (None, "N/A"):
        raise RuntimeError("ffprobe did not return nb_read_frames")
    return int(frames)


def decode_one(path: Path, timeout: int) -> dict:
    start = time.monotonic()
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return {
        "path": str(path),
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "seconds": round(time.monotonic() - start, 3),
        "stderr_tail": (proc.stderr or "")[-4000:],
    }


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def has_container_frame_mismatch(row: dict, expected_fps: float) -> bool:
    if row.get("probe_error"):
        return False
    duration = row.get("video_duration_seconds") or row.get("format_duration_seconds") or 0
    container_frames = row.get("video_frames")
    if container_frames is None:
        return True
    if not duration:
        return False
    container_duration = int(container_frames) / expected_fps
    return abs(container_duration - duration) > FRAME_DURATION_MISMATCH_TOLERANCE_SECONDS


def count_video_frames_for_row(row: dict) -> dict:
    try:
        return {"path": row["path"], "video_read_frames": count_video_frames(Path(row["path"]))}
    except Exception as exc:  # noqa: BLE001
        return {"path": row["path"], "video_read_count_error": str(exc)[-4000:]}


def enrich_probe_frame_counts(cache: dict[str, dict], cache_path: Path, expected_fps: float, workers: int) -> dict[str, dict]:
    needed = [
        row
        for row in cache.values()
        if row.get("video_read_frames") is None
        and row.get("video_read_count_error") is None
        and has_container_frame_mismatch(row, expected_fps)
    ]
    if needed:
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(count_video_frames_for_row, needed):
                key = result["path"].lower()
                cache[key].update(result)
        write_jsonl(cache_path, list(cache.values()))
    return cache


def probe_many(paths: list[Path], cache_path: Path, workers: int) -> dict[str, dict]:
    cache_rows = load_jsonl(cache_path)
    cache = {row["path"].lower(): row for row in cache_rows}
    missing = [path for path in paths if str(path).lower() not in cache]
    if missing:
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for row in pool.map(probe_one, missing):
                cache[row["path"].lower()] = row
        write_jsonl(cache_path, list(cache.values()))
    return cache


def summarize_probe(rows: list[dict]) -> dict:
    return {
        "count": len(rows),
        "probe_errors": sum(1 for row in rows if row.get("probe_error")),
        "total_size_gb": round(sum(row.get("size_bytes") or 0 for row in rows) / 1024**3, 3),
        "total_format_duration_seconds": round(sum(row.get("format_duration_seconds") or 0 for row in rows), 3),
        "total_container_video_frames": sum(row.get("video_frames") or 0 for row in rows),
        "total_read_video_frames": sum(row.get("video_read_frames") or 0 for row in rows),
    }


def expected_public_outputs(sources: list[Path], chains: list[dict], source_root: Path, output_root: Path) -> list[dict]:
    stitched_sources = {source.resolve() for chain in chains for source in chain["sources"]}
    entries = []
    for source in sources:
        if source.resolve() in stitched_sources:
            continue
        entries.append({"kind": "standalone", "sources": [source], "output": output_for(source, source_root, output_root)})
    for chain in chains:
        entries.append({"kind": "stitched", "chain_id": chain["id"], "sources": chain["sources"], "output": chain["output"]})
    return sorted(entries, key=lambda item: str(item["output"]).lower())


def trusted_frame_count(row: dict, expected_fps: float) -> tuple[int | None, str]:
    """Return the safest available exact frame count for this archive."""
    read_frames = row.get("video_read_frames")
    if read_frames is not None:
        return int(read_frames), "read_frames"
    duration = row.get("video_duration_seconds") or row.get("format_duration_seconds") or 0
    duration_frames = int(round(duration * expected_fps)) if duration else None
    container_frames = row.get("video_frames")
    if container_frames is None:
        return duration_frames, "duration"
    if duration_frames is None:
        return int(container_frames), "container"
    container_duration = int(container_frames) / expected_fps
    if abs(container_duration - duration) > FRAME_DURATION_MISMATCH_TOLERANCE_SECONDS:
        return duration_frames, "duration_container_mismatch"
    return int(container_frames), "container"


def compare_entry(entry: dict, source_probe: dict[str, dict], output_probe: dict[str, dict]) -> dict:
    out_path = entry["output"]
    out = output_probe.get(str(out_path).lower(), {"path": str(out_path), "exists": out_path.exists(), "probe_error": "missing output"})
    src_rows = [source_probe.get(str(source).lower(), {"path": str(source), "probe_error": "missing source probe"}) for source in entry["sources"]]
    src_trusted = [trusted_frame_count(row, DV_PAL_FPS) for row in src_rows]
    src_frames = sum(frames or 0 for frames, _method in src_trusted)
    src_duration = src_frames / DV_PAL_FPS if src_frames else sum(row.get("video_duration_seconds") or 0 for row in src_rows)
    expected_out_frames = src_frames * 2 if src_frames else None
    actual_out_frames, out_frame_method = trusted_frame_count(out, MP4_TARGET_FPS)
    expected_out_duration = src_duration
    actual_out_duration = (actual_out_frames / MP4_TARGET_FPS) if actual_out_frames else out.get("video_duration_seconds") or 0
    frame_delta = None if expected_out_frames is None or actual_out_frames is None else actual_out_frames - expected_out_frames
    duration_delta = actual_out_duration - expected_out_duration
    issues: list[str] = []
    if out.get("probe_error"):
        issues.append(f"output probe error: {out.get('probe_error')}")
    for row in src_rows:
        if row.get("probe_error"):
            issues.append(f"source probe error: {row.get('path')}: {row.get('probe_error')}")
    if out.get("video_codec") != "h264":
        issues.append(f"output video codec {out.get('video_codec')}, expected h264")
    if out.get("width") != 720 or out.get("height") != 576:
        issues.append(f"output dimensions {out.get('width')}x{out.get('height')}, expected 720x576")
    if out.get("audio_codec") != "aac":
        issues.append(f"output audio codec {out.get('audio_codec')}, expected aac")
    if out.get("audio_sample_rate") != 48000:
        issues.append(f"output audio sample rate {out.get('audio_sample_rate')}, expected 48000")
    if out.get("audio_channels") != 2:
        issues.append(f"output audio channels {out.get('audio_channels')}, expected 2")
    if frame_delta is None:
        issues.append("missing exact frame count")
    elif abs(frame_delta) > FRAME_TOLERANCE:
        issues.append(f"frame delta {frame_delta}, expected tolerance {FRAME_TOLERANCE}")
    if abs(duration_delta) > DURATION_TOLERANCE_SECONDS:
        issues.append(f"duration delta {duration_delta:.3f}s, expected tolerance {DURATION_TOLERANCE_SECONDS}s")
    return {
        "kind": entry["kind"],
        "output": str(out_path),
        "source_count": len(entry["sources"]),
        "sources": [str(source) for source in entry["sources"]],
        "source_frames_25fps": src_frames,
        "source_frame_methods": [method for _frames, method in src_trusted],
        "source_duration_seconds": round(src_duration, 3),
        "expected_output_frames_50fps": expected_out_frames,
        "actual_output_frames": actual_out_frames,
        "output_frame_method": out_frame_method,
        "frame_delta": frame_delta,
        "expected_output_duration_seconds": round(expected_out_duration, 3),
        "actual_output_duration_seconds": round(actual_out_duration, 3),
        "duration_delta_seconds": round(duration_delta, 6),
        "output_size_bytes": out.get("size_bytes"),
        "issues": issues,
    }


def decode_many(paths: list[Path], cache_path: Path, workers: int, timeout: int) -> dict:
    cache_rows = load_jsonl(cache_path)
    cache = {row["path"].lower(): row for row in cache_rows}
    missing = [path for path in paths if str(path).lower() not in cache]
    if missing:
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_path = {pool.submit(decode_one, path, timeout): path for path in missing}
            for future in futures.as_completed(future_to_path):
                row = future.result()
                cache[row["path"].lower()] = row
                with cache_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    failures = [row for row in cache.values() if not row.get("ok")]
    return {
        "checked": len(cache),
        "failures": len(failures),
        "failure_examples": failures[:20],
        "total_decode_seconds": round(sum(row.get("seconds") or 0 for row in cache.values()), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--stitch-decisions", type=Path, default=DEFAULT_STITCH_DECISIONS)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--decode", choices=["none", "public"], default="none")
    parser.add_argument("--decode-workers", type=int, default=2)
    parser.add_argument("--decode-timeout", type=int, default=7200)
    args = parser.parse_args()

    args.report_dir.mkdir(parents=True, exist_ok=True)
    sources = discover_sources(args.source_root)
    chains = load_stitch_chains(args.stitch_decisions, args.source_root.resolve(), args.output_root)
    expected_entries = expected_public_outputs(sources, chains, args.source_root, args.output_root)
    expected_outputs = [entry["output"] for entry in expected_entries]
    actual_outputs = sorted(args.output_root.rglob("*.mp4"), key=lambda item: str(item).lower())

    source_probe_cache = args.report_dir / "source_probe.jsonl"
    output_probe_cache = args.report_dir / "output_probe.jsonl"
    source_probe = probe_many(sources, source_probe_cache, args.workers)
    source_probe = enrich_probe_frame_counts(source_probe, source_probe_cache, DV_PAL_FPS, args.workers)
    output_probe = probe_many(actual_outputs, output_probe_cache, args.workers)
    output_probe = enrich_probe_frame_counts(output_probe, output_probe_cache, MP4_TARGET_FPS, args.workers)

    comparisons = [compare_entry(entry, source_probe, output_probe) for entry in expected_entries]
    comparison_issues = [row for row in comparisons if row["issues"]]
    write_jsonl(args.report_dir / "comparison_rows.jsonl", comparisons)
    write_json(args.report_dir / "comparison_issues.json", {"count": len(comparison_issues), "issues": comparison_issues[:1000]})

    expected_set = {str(path).lower() for path in expected_outputs}
    actual_set = {str(path).lower() for path in actual_outputs}
    missing_outputs = [str(path) for path in expected_outputs if str(path).lower() not in actual_set]
    extra_outputs = [str(path) for path in actual_outputs if str(path).lower() not in expected_set]

    public_decode = None
    if args.decode == "public":
        public_decode = decode_many(actual_outputs, args.report_dir / "public_decode.jsonl", args.decode_workers, args.decode_timeout)

    source_rows = list(source_probe.values())
    output_rows = list(output_probe.values())
    total_source_frames = sum((trusted_frame_count(row, DV_PAL_FPS)[0] or 0) for row in source_rows)
    total_output_frames = sum((trusted_frame_count(row, MP4_TARGET_FPS)[0] or 0) for row in output_rows)
    source_frame_methods = {}
    for row in source_rows:
        _frames, method = trusted_frame_count(row, DV_PAL_FPS)
        source_frame_methods[method] = source_frame_methods.get(method, 0) + 1
    output_frame_methods = {}
    for row in output_rows:
        _frames, method = trusted_frame_count(row, MP4_TARGET_FPS)
        output_frame_methods[method] = output_frame_methods.get(method, 0) + 1
    total_source_seconds = total_source_frames / DV_PAL_FPS
    total_output_seconds = total_output_frames / MP4_TARGET_FPS
    summary = {
        "created_at": now_iso(),
        "source_root": str(args.source_root),
        "output_root": str(args.output_root),
        "source_count": len(sources),
        "stitch_chain_count": len(chains),
        "expected_public_output_count": len(expected_outputs),
        "actual_public_output_count": len(actual_outputs),
        "missing_public_outputs": missing_outputs,
        "extra_public_outputs": extra_outputs,
        "source_probe": summarize_probe(source_rows),
        "public_output_probe": summarize_probe(output_rows),
        "total_source_frames_25fps": total_source_frames,
        "source_frame_count_methods": source_frame_methods,
        "expected_total_output_frames_50fps": total_source_frames * 2,
        "actual_total_output_frames": total_output_frames,
        "output_frame_count_methods": output_frame_methods,
        "total_output_frame_delta": total_output_frames - (total_source_frames * 2),
        "total_source_duration_seconds": round(total_source_seconds, 3),
        "total_public_output_duration_seconds": round(total_output_seconds, 3),
        "total_duration_delta_seconds": round(total_output_seconds - total_source_seconds, 6),
        "comparison_issue_count": len(comparison_issues),
        "comparison_issue_examples": comparison_issues[:20],
        "public_decode": public_decode,
        "report_files": {
            "source_probe": str(args.report_dir / "source_probe.jsonl"),
            "output_probe": str(args.report_dir / "output_probe.jsonl"),
            "comparison_rows": str(args.report_dir / "comparison_rows.jsonl"),
            "comparison_issues": str(args.report_dir / "comparison_issues.json"),
            "public_decode": str(args.report_dir / "public_decode.jsonl") if public_decode else None,
        },
    }
    write_json(args.report_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    has_errors = bool(missing_outputs or extra_outputs or comparison_issues or (public_decode and public_decode["failures"]))
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
