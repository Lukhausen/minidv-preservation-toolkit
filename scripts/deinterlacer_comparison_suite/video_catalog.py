"""Discover comparison videos for the deinterlacer comparison suite.

By default the app uses the local sample_videos folder next to this script.
Set MINIDV_SAMPLE_DIR to point at another directory of AVI samples.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE_DIR = HERE / "sample_videos"
SAMPLE_VIDEO_DIR = Path(os.environ.get("MINIDV_SAMPLE_DIR", SAMPLE_DIR)).expanduser().resolve()
VIDEO_EXTENSIONS = {".avi"}
FRAME_CACHE_PATH = HERE / "video_frame_cache.json"


def _safe_id(path: Path, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._-") or "video"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _ffprobe_exe() -> str | None:
    override = os.environ.get("FFPROBE")
    if override:
        return override
    found = shutil.which("ffprobe")
    if found:
        return found
    return None


def _parse_fps(raw: str | None) -> float | None:
    if not raw or raw == "0/0":
        return None
    try:
        return float(Fraction(raw))
    except (ValueError, ZeroDivisionError):
        try:
            return float(raw)
        except ValueError:
            return None


def _frame_count_from_stream(stream: dict) -> int | None:
    for key in ("nb_frames", "nb_read_frames"):
        raw = stream.get(key)
        if raw and raw != "N/A":
            try:
                count = int(float(raw))
            except ValueError:
                continue
            if count > 0:
                return count
    duration = stream.get("duration")
    fps = _parse_fps(stream.get("r_frame_rate"))
    if duration and fps:
        try:
            count = int(round(float(duration) * fps))
        except ValueError:
            return None
        if count > 0:
            return count
    return None


def _frame_count_ffprobe(path: Path) -> int | None:
    exe = _ffprobe_exe()
    if not exe:
        return None
    cmd = [
        exe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_frames,duration,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    count = _frame_count_from_stream(streams[0])
    if count is not None:
        return count

    counted_cmd = [
        exe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames,nb_frames,duration,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(counted_cmd, capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        return None
    try:
        counted_data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    counted_streams = counted_data.get("streams") or []
    return _frame_count_from_stream(counted_streams[0]) if counted_streams else None


def _frame_count_vapoursynth(path: Path) -> int | None:
    try:
        import vapoursynth as vs

        clip = vs.core.lsmas.LWLibavSource(str(path))
        return int(clip.num_frames)
    except Exception:
        return None


def frame_count(path: Path) -> int:
    count = _frame_count_ffprobe(path)
    if count is None:
        count = _frame_count_vapoursynth(path)
    if count is None or count <= 0:
        raise RuntimeError(f"could not determine frame count for {path}")
    return count


def _load_frame_cache() -> dict:
    if not FRAME_CACHE_PATH.exists():
        return {}
    try:
        with FRAME_CACHE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_frame_cache(cache: dict) -> None:
    tmp = FRAME_CACHE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2)
    tmp.replace(FRAME_CACHE_PATH)


def _cached_frame_count(path: Path, cache: dict) -> tuple[int, bool]:
    stat = path.stat()
    key = path.name
    cached = cache.get(key)
    signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if (
        isinstance(cached, dict)
        and cached.get("size") == signature["size"]
        and cached.get("mtime_ns") == signature["mtime_ns"]
        and isinstance(cached.get("frames"), int)
        and cached["frames"] > 0
    ):
        return cached["frames"], False
    count = frame_count(path)
    cache[key] = {**signature, "frames": count}
    return count, True


def discover_videos(test_dir: Path = SAMPLE_VIDEO_DIR, include_frames: bool = True) -> list[dict]:
    """Return video entries for AVI files directly inside the sample directory."""

    used: set[str] = set()
    videos: list[dict] = []
    cache = _load_frame_cache() if include_frames else {}
    dirty_cache = False
    for path in sorted(test_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        entry = {
            "id": _safe_id(path, used),
            "file": path.name,
            "label": path.stem,
        }
        if include_frames:
            entry["frames"], dirty = _cached_frame_count(path, cache)
            dirty_cache = dirty_cache or dirty
        videos.append(entry)
    if include_frames and dirty_cache:
        _save_frame_cache(cache)
    return videos
