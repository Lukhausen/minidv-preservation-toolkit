"""
Deinterlacer comparison server — adaptive Elo edition.

Each comparison shows the SAME randomly-chosen (video, frame) rendered through
TWO different deinterlacer modes, with the mode identities blinded as A / B and
the sides randomised. You pick the better one. The next comparison
draws a FRESH random (video, frame) so you never judge the same image twice.

Modes are ranked by Elo. Pairing is adaptive: it samples under-tested modes
first, then focuses comparisons on modes with similar ratings (where the
ranking is most uncertain). The live leaderboard is always available — stop
whenever the top looks settled (usually ~20-40 votes).

Frames are rendered on demand by qtgmc_extract.py (subprocess) and cached as
PNGs in frames/. Optional three-second comparison clips are cached as MP4s in
clips/. The next pair is pre-rendered in the background while you vote, so you
rarely wait in image mode.

API:
  GET  /api/pair            -> current pair + per-side render status
  POST /api/vote {winner}   -> record vote, advance to next pair
  POST /api/undo            -> revert last vote
  POST /api/reset           -> wipe ratings + votes
  GET  /api/results         -> Elo ranking, games, vote log, win matrix
  GET  /frames/<file>.png   -> a rendered frame

Run:
    python server.py            # http://127.0.0.1:8765/
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from video_catalog import SAMPLE_VIDEO_DIR, discover_videos

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
FRAMES_DIR = HERE / "frames"
CLIPS_DIR = HERE / "clips"
COMPRESSION_CLIPS_DIR = HERE / "compression_clips"
PREVIEW_UPLOADS_DIR = HERE / "preview_uploads"
PREVIEW_CLIPS_DIR = HERE / "preview_clips"
FINAL_SAMPLES_DIR = HERE / "final_samples"
FINAL_SAMPLES_MANIFEST = FINAL_SAMPLES_DIR / "samples.json"
CONFIG_PATH = HERE / "config.json"
VOTES_PATH = HERE / "votes.json"
EXTRACT_SCRIPT = HERE / "qtgmc_extract.py"
ORIGINAL_MODE = "_original"
CLIP_SECONDS = 1.0
COMPRESSION_CLIP_SECONDS = 4.0
PREFETCH_PAIRS = 5
TOP_COMPARISON_MODE_COUNT = 10
TOP_COMPARISON_MODE_IDS = [
    "jet_qtgmc_clean_sharp",
    "vslow_default",
    "slower_default",
    "jet_qtgmc_default",
]
COMPRESSION_BASE_MODE = "slower_default"
COMPRESSION_DARK_PROBABILITY = 0.82
COMPRESSION_DARK_LUMA = 76
COMPRESSION_VARIANTS = [
    {
        "id": "x264_slower_crf18",
        "label": "x264 slower CRF 18",
        "preset": "slower",
        "crf": 18,
    },
    {
        "id": "x264_slower_crf17",
        "label": "x264 slower CRF 17",
        "preset": "slower",
        "crf": 17,
    },
    {
        "id": "x264_slower_crf16",
        "label": "x264 slower CRF 16",
        "preset": "slower",
        "crf": 16,
    },
    {
        "id": "x264_slower_crf18_grain",
        "label": "x264 slower CRF 18 tune grain",
        "preset": "slower",
        "crf": 18,
        "tune": "grain",
    },
    {
        "id": "x264_slower_crf17_grain",
        "label": "x264 slower CRF 17 tune grain",
        "preset": "slower",
        "crf": 17,
        "tune": "grain",
    },
    {
        "id": "x264_slow_crf18",
        "label": "x264 slow CRF 18",
        "preset": "slow",
        "crf": 18,
    },
]
COMPRESSION_BY_ID = {variant["id"]: variant for variant in COMPRESSION_VARIANTS}

K_FACTOR = 32
START_ELO = 1500.0
MEDIA_TYPES = {"photo", "video"}
PAIRING_MODES = {"adaptive", "random"}
DEFAULT_PAIRING_MODE = "adaptive"
PAIRING_LOGIC_VERSION = 7

LOCK = threading.RLock()
CPU_EXECUTOR = ThreadPoolExecutor(max_workers=4)
AI_EXECUTOR = ThreadPoolExecutor(max_workers=1)

# media path(str) -> "rendering" | "done" | "error:<msg>"
RENDER_STATUS: dict[str, str] = {}
RENDER_STATUS_LOCK = threading.Lock()
LUMA_CACHE: dict[tuple[str, int], int | None] = {}

# In-memory preview jobs. Uploaded preview inputs are server-side copies; the
# browser's original file is never modified or deleted.
PREVIEW_JOBS: dict[str, dict] = {}


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["videos"] = discover_videos(SAMPLE_VIDEO_DIR, include_frames=True)
    if not cfg["videos"]:
        raise RuntimeError(f"No .avi videos found in {SAMPLE_VIDEO_DIR}")
    return cfg


def load_final_samples() -> list[dict]:
    if not FINAL_SAMPLES_MANIFEST.exists():
        return []
    try:
        with FINAL_SAMPLES_MANIFEST.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    samples = data.get("samples") if isinstance(data, dict) else data
    if not isinstance(samples, list):
        return []
    clean_samples: list[dict] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        original = sample.get("original_file")
        final = sample.get("final_file")
        if not original or not final:
            continue
        original_path = FINAL_SAMPLES_DIR / Path(original).name
        final_path = FINAL_SAMPLES_DIR / Path(final).name
        item = dict(sample)
        item["original_file"] = original_path.name
        item["final_file"] = final_path.name
        item["original_url"] = f"/final-samples/{original_path.name}" if original_path.exists() else None
        item["final_url"] = f"/final-samples/{final_path.name}" if final_path.exists() else None
        if original_path.exists():
            item["original_size_bytes"] = original_path.stat().st_size
        if final_path.exists():
            item["final_size_bytes"] = final_path.stat().st_size
        item["ready"] = bool(item["original_url"] and item["final_url"])
        clean_samples.append(item)
    return clean_samples


CFG: dict = {}
MODES: list[str] = []
MODE_BY_ID: dict[str, dict] = {}
VIDEO_MODES: list[str] = []
VIDEO_BY_ID: dict[str, dict] = {}
STATE: dict = {}


def initialize_runtime() -> None:
    global CFG, MODES, MODE_BY_ID, VIDEO_MODES, VIDEO_BY_ID, STATE
    CFG = load_config()
    MODES = [d["id"] for d in CFG["deinterlacers"]]
    MODE_BY_ID = {d["id"]: d for d in CFG["deinterlacers"]}
    VIDEO_MODES = [d["id"] for d in CFG["deinterlacers"] if d.get("supports_video", True)]
    VIDEO_BY_ID = {v["id"]: v for v in CFG["videos"]}
    if len(VIDEO_MODES) < 2:
        raise RuntimeError("At least two video-capable deinterlacer modes are required")
    STATE = load_state()


def top_comparison_mode_count() -> int:
    return len(top_comparison_mode_ids())


def top_comparison_mode_ids() -> list[str]:
    focused = [mode_id for mode_id in TOP_COMPARISON_MODE_IDS if mode_id in VIDEO_MODES]
    if len(focused) >= 2:
        return focused
    return VIDEO_MODES[:min(TOP_COMPARISON_MODE_COUNT, len(VIDEO_MODES))]


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def fresh_state() -> dict:
    return {
        "elo": {m: START_ELO for m in MODES},
        "games": {m: 0 for m in MODES},
        "votes": [],
        "current_pair": None,
        "pair_queue": [],
        "top3_comparison": None,
        "top3_queue": [],
        "top3_pair_cursor": 0,
        "compression_comparison": None,
        "compression_queue": [],
        "compression_pair_cursor": 0,
        "pairing_mode": DEFAULT_PAIRING_MODE,
        "pairing_logic_version": PAIRING_LOGIC_VERSION,
        "counter": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def valid_pair(pair: dict | None) -> bool:
    if not pair:
        return False
    return (
        pair.get("video_id") in VIDEO_BY_ID
        and pair.get("left_mode") in MODE_BY_ID
        and pair.get("right_mode") in MODE_BY_ID
    )


def pair_uses_only(pair: dict | None, modes: set[str]) -> bool:
    return bool(
        valid_pair(pair)
        and pair["left_mode"] in modes
        and pair["right_mode"] in modes
    )


def pair_matches_media(pair: dict | None, media_type: str) -> bool:
    return valid_pair(pair) and pair.get("media_type") == media_type


def valid_top3(comp: dict | None) -> bool:
    if not comp:
        return False
    modes = comp.get("modes") or []
    pair_modes = comp.get("pair_modes") or []
    return (
        comp.get("video_id") in VIDEO_BY_ID
        and isinstance(comp.get("frame"), int)
        and len(modes) == top_comparison_mode_count()
        and len(set(modes)) == top_comparison_mode_count()
        and all(mode in VIDEO_MODES for mode in modes)
        and len(pair_modes) == 2
        and len(set(pair_modes)) == 2
        and all(mode in modes for mode in pair_modes)
    )


def valid_compression(comp: dict | None) -> bool:
    if not comp:
        return False
    variants = comp.get("variants") or []
    return (
        comp.get("video_id") in VIDEO_BY_ID
        and isinstance(comp.get("frame"), int)
        and len(variants) == 2
        and len(set(variants)) == 2
        and all(variant in COMPRESSION_BY_ID for variant in variants)
    )


def load_state() -> dict:
    if VOTES_PATH.exists():
        try:
            with VOTES_PATH.open("r", encoding="utf-8") as fh:
                st = json.load(fh)
            # Heal against config changes: ensure every mode has elo/games.
            for m in MODES:
                st.setdefault("elo", {}).setdefault(m, START_ELO)
                st.setdefault("games", {}).setdefault(m, 0)
            if st.get("pairing_mode") not in PAIRING_MODES:
                st["pairing_mode"] = DEFAULT_PAIRING_MODE
            if st.get("pairing_logic_version") != PAIRING_LOGIC_VERSION:
                st["pairing_logic_version"] = PAIRING_LOGIC_VERSION
                st["current_pair"] = None
                st["next_pair"] = None
                st["pair_queue"] = []
            if not valid_pair(st.get("current_pair")):
                st["current_pair"] = None
            queue = st.get("pair_queue")
            if not isinstance(queue, list):
                queue = []
            if valid_pair(st.get("next_pair")):
                queue.insert(0, st["next_pair"])
            st["pair_queue"] = [pair for pair in queue if valid_pair(pair)]
            st["next_pair"] = None
            if not valid_top3(st.get("top3_comparison")):
                st["top3_comparison"] = None
            top3_queue = st.get("top3_queue")
            if not isinstance(top3_queue, list):
                top3_queue = []
            st["top3_queue"] = [comp for comp in top3_queue if valid_top3(comp)]
            st.setdefault("top3_pair_cursor", 0)
            if not valid_compression(st.get("compression_comparison")):
                st["compression_comparison"] = None
            compression_queue = st.get("compression_queue")
            if not isinstance(compression_queue, list):
                compression_queue = []
            st["compression_queue"] = [comp for comp in compression_queue if valid_compression(comp)]
            st.setdefault("compression_pair_cursor", 0)
            return st
        except (json.JSONDecodeError, OSError):
            pass
    return fresh_state()


def save_state(st: dict) -> None:
    tmp = VOTES_PATH.with_name(f"{VOTES_PATH.stem}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2)
    for attempt in range(5):
        try:
            tmp.replace(VOTES_PATH)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def png_name(video_id: str, frame: int, mode_id: str) -> str:
    return f"{video_id}__f{frame}__{mode_id}.png"


def png_path(video_id: str, frame: int, mode_id: str) -> Path:
    return FRAMES_DIR / png_name(video_id, frame, mode_id)


def clip_name(video_id: str, frame: int, mode_id: str) -> str:
    duration = f"{CLIP_SECONDS:g}".replace(".", "p")
    return f"{video_id}__f{frame}__{mode_id}__{duration}s.mp4"


def clip_path(video_id: str, frame: int, mode_id: str) -> Path:
    return CLIPS_DIR / clip_name(video_id, frame, mode_id)


def compression_clip_name(video_id: str, frame: int, variant_id: str) -> str:
    duration = f"{COMPRESSION_CLIP_SECONDS:g}".replace(".", "p")
    return f"{video_id}__f{frame}__{COMPRESSION_BASE_MODE}__{variant_id}__{duration}s.mp4"


def compression_clip_path(video_id: str, frame: int, variant_id: str) -> Path:
    return COMPRESSION_CLIPS_DIR / compression_clip_name(video_id, frame, variant_id)


def compression_master_name(video_id: str, frame: int) -> str:
    duration = f"{COMPRESSION_CLIP_SECONDS:g}".replace(".", "p")
    return f"{video_id}__f{frame}__{COMPRESSION_BASE_MODE}__master__{duration}s.mkv"


def compression_master_path(video_id: str, frame: int) -> Path:
    return COMPRESSION_CLIPS_DIR / compression_master_name(video_id, frame)


def temp_render_path(out: Path) -> Path:
    return out.with_name(f"{out.stem}__tmp_{threading.get_ident()}{out.suffix}")


def ffmpeg_exe() -> str:
    override = os.environ.get("FFMPEG")
    if override:
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    return "ffmpeg"


def mode_uses_ai(mode_id: str | None) -> bool:
    if not mode_id or mode_id == ORIGINAL_MODE:
        return False
    return "deepdeinterlace" in MODE_BY_ID.get(mode_id, {})


def executor_for_modes(*mode_ids: str | None) -> ThreadPoolExecutor:
    return AI_EXECUTOR if any(mode_uses_ai(mode_id) for mode_id in mode_ids) else CPU_EXECUTOR


def clean_filename(name: str) -> str:
    name = Path(name or "preview").name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name or "preview"


def preview_upload_path(job_id: str, filename: str) -> Path:
    suffix = Path(filename).suffix
    if not suffix or len(suffix) > 12:
        suffix = ".video"
    return PREVIEW_UPLOADS_DIR / f"{job_id}{suffix.lower()}"


def preview_clip_name(job_id: str, mode_id: str) -> str:
    return f"{job_id}__{mode_id}__full.mp4"


def preview_clip_path(job_id: str, mode_id: str) -> Path:
    return PREVIEW_CLIPS_DIR / preview_clip_name(job_id, mode_id)


def run_preview_subrender(upload: Path, out: Path, mode_id: str, frame: int) -> tuple[bool, str]:
    tmp = temp_render_path(out)
    tmp.unlink(missing_ok=True)
    cmd = [
        sys.executable, str(EXTRACT_SCRIPT),
        "--input", str(upload),
        "--mode", mode_id,
        "--frame", str(frame),
        "--full-video",
        "--out", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if proc.returncode == 0 and tmp.exists():
            tmp.replace(out)
            return True, ""
        tmp.unlink(missing_ok=True)
        return False, (proc.stderr or proc.stdout or "render failed")[-1000:]
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return False, str(exc)


def _image_render_job(video_id: str, frame: int, mode_id: str) -> None:
    out = png_path(video_id, frame, mode_id)
    tmp = temp_render_path(out)
    key = str(out)
    if out.exists():
        RENDER_STATUS[key] = "done"
        return
    tmp.unlink(missing_ok=True)
    RENDER_STATUS[key] = "rendering"
    cmd = [
        sys.executable, str(EXTRACT_SCRIPT),
        "--video", video_id,
        "--mode", mode_id,
        "--frame", str(frame),
        "--out", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and tmp.exists():
            tmp.replace(out)
            RENDER_STATUS[key] = "done"
        else:
            tmp.unlink(missing_ok=True)
            tail = (proc.stderr or proc.stdout or "")[-400:]
            RENDER_STATUS[key] = f"error:{tail}"
            sys.stderr.write(f"[render fail] {key}\n{tail}\n")
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = "error:timeout"
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = f"error:{exc}"


def _clip_render_job(video_id: str, frame: int, mode_id: str) -> None:
    out = clip_path(video_id, frame, mode_id)
    tmp = temp_render_path(out)
    key = str(out)
    if out.exists():
        RENDER_STATUS[key] = "done"
        return
    tmp.unlink(missing_ok=True)
    RENDER_STATUS[key] = "rendering"
    cmd = [
        sys.executable, str(EXTRACT_SCRIPT),
        "--video", video_id,
        "--mode", mode_id,
        "--frame", str(frame),
        "--clip-seconds", str(CLIP_SECONDS),
        "--out", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if proc.returncode == 0 and tmp.exists():
            tmp.replace(out)
            RENDER_STATUS[key] = "done"
        else:
            tmp.unlink(missing_ok=True)
            tail = (proc.stderr or proc.stdout or "")[-400:]
            RENDER_STATUS[key] = f"error:{tail}"
            sys.stderr.write(f"[clip render fail] {key}\n{tail}\n")
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = "error:timeout"
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = f"error:{exc}"


def ensure_compression_master(video_id: str, frame: int) -> tuple[bool, str]:
    out = compression_master_path(video_id, frame)
    key = str(out)
    if out.exists():
        RENDER_STATUS[key] = "done"
        return True, ""

    with RENDER_STATUS_LOCK:
        status = RENDER_STATUS.get(key)
        if status == "rendering":
            should_wait = True
        else:
            RENDER_STATUS[key] = "rendering"
            should_wait = False

    if should_wait:
        deadline = time.time() + 1800
        while time.time() < deadline:
            if out.exists():
                RENDER_STATUS[key] = "done"
                return True, ""
            status = RENDER_STATUS.get(key)
            if status and status.startswith("error"):
                return False, status
            time.sleep(0.5)
        return False, "error:master timeout"

    tmp = temp_render_path(out)
    tmp.unlink(missing_ok=True)
    cmd = [
        sys.executable, str(EXTRACT_SCRIPT),
        "--video", video_id,
        "--mode", COMPRESSION_BASE_MODE,
        "--frame", str(frame),
        "--clip-seconds", str(COMPRESSION_CLIP_SECONDS),
        "--encode-preset", "ultrafast",
        "--crf", "0",
        "--pix-fmt", "yuv444p",
        "--out", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode == 0 and tmp.exists():
            tmp.replace(out)
            RENDER_STATUS[key] = "done"
            return True, ""
        tmp.unlink(missing_ok=True)
        tail = (proc.stderr or proc.stdout or "")[-600:]
        RENDER_STATUS[key] = f"error:{tail}"
        sys.stderr.write(f"[compression master render fail] {key}\n{tail}\n")
        return False, RENDER_STATUS[key]
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = "error:timeout"
        return False, RENDER_STATUS[key]
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = f"error:{exc}"
        return False, RENDER_STATUS[key]


def _compression_render_job(video_id: str, frame: int, variant_id: str) -> None:
    out = compression_clip_path(video_id, frame, variant_id)
    tmp = temp_render_path(out)
    key = str(out)
    if out.exists():
        RENDER_STATUS[key] = "done"
        return
    variant = COMPRESSION_BY_ID[variant_id]
    tmp.unlink(missing_ok=True)
    RENDER_STATUS[key] = "rendering"
    master_ok, master_error = ensure_compression_master(video_id, frame)
    if not master_ok:
        RENDER_STATUS[key] = f"error:{master_error}"
        return
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(compression_master_path(video_id, frame)),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        variant["preset"],
        "-crf",
        str(variant["crf"]),
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.0",
    ]
    if variant.get("tune"):
        cmd.extend(["-tune", variant["tune"]])
    cmd.extend(["-movflags", "+faststart", str(tmp)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and tmp.exists():
            tmp.replace(out)
            RENDER_STATUS[key] = "done"
        else:
            tmp.unlink(missing_ok=True)
            tail = (proc.stderr or proc.stdout or "")[-600:]
            RENDER_STATUS[key] = f"error:{tail}"
            sys.stderr.write(f"[compression render fail] {key}\n{tail}\n")
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = "error:timeout"
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        RENDER_STATUS[key] = f"error:{exc}"


def _preview_render_job(job_id: str) -> None:
    with LOCK:
        job = PREVIEW_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "rendering"
        upload = Path(job["upload_path"])
        out = Path(job["clip_path"])
        original_out = Path(job["original_clip_path"])
        mode_id = job["mode_id"]
        mode_b_id = job.get("mode_b_id")
        out_b = Path(job["clip_b_path"]) if job.get("clip_b_path") else None
        frame = int(job.get("frame", 0))

    selected_ok, selected_error = run_preview_subrender(upload, out, mode_id, frame)
    if not selected_ok:
        with LOCK:
            job = PREVIEW_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "error"
            job["error"] = selected_error
            sys.stderr.write(f"[preview render fail] {job_id} selected\n{selected_error}\n")
        return

    with LOCK:
        job = PREVIEW_JOBS.get(job_id)
        if job:
            job["selected_done"] = True
            job["status"] = "rendering_b" if mode_b_id else "rendering_original"

    if mode_b_id:
        if mode_b_id == mode_id:
            with LOCK:
                job = PREVIEW_JOBS.get(job_id)
                if job:
                    job["clip_b_reuses_a"] = True
        else:
            assert out_b is not None
            selected_b_ok, selected_b_error = run_preview_subrender(upload, out_b, mode_b_id, frame)
            if not selected_b_ok:
                with LOCK:
                    job = PREVIEW_JOBS.get(job_id)
                    if not job:
                        return
                    job["status"] = "error"
                    job["error"] = selected_b_error
                    sys.stderr.write(f"[preview render fail] {job_id} selected B\n{selected_b_error}\n")
                return

        with LOCK:
            job = PREVIEW_JOBS.get(job_id)
            if job:
                job["selected_b_done"] = True
                job["status"] = "rendering_original"

    original_ok, original_error = run_preview_subrender(upload, original_out, ORIGINAL_MODE, frame)
    if not original_ok:
        with LOCK:
            job = PREVIEW_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "error"
            job["error"] = original_error
            sys.stderr.write(f"[preview render fail] {job_id} original\n{original_error}\n")
        return

    with LOCK:
        job = PREVIEW_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "done"
        job["clip"] = f"/preview-clips/{preview_clip_name(job_id, mode_id)}"
        if mode_b_id:
            clip_b_mode = mode_id if mode_b_id == mode_id else mode_b_id
            job["clip_b"] = f"/preview-clips/{preview_clip_name(job_id, clip_b_mode)}"
        job["original_clip"] = f"/preview-clips/{preview_clip_name(job_id, ORIGINAL_MODE)}"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()


def start_preview_render(job: dict) -> None:
    if job.get("status") == "rendering":
        return
    job["status"] = "rendering"
    executor_for_modes(job.get("mode_id"), job.get("mode_b_id")).submit(_preview_render_job, job["job_id"])


def ensure_render(video_id: str, frame: int, mode_id: str) -> None:
    out = png_path(video_id, frame, mode_id)
    key = str(out)
    if out.exists():
        RENDER_STATUS[key] = "done"
        return
    status = RENDER_STATUS.get(key)
    if status == "rendering":
        return
    if status and status.startswith("error"):
        # allow retry on next request
        pass
    RENDER_STATUS[key] = "rendering"
    executor_for_modes(mode_id).submit(_image_render_job, video_id, frame, mode_id)


def ensure_clip_render(video_id: str, frame: int, mode_id: str) -> None:
    out = clip_path(video_id, frame, mode_id)
    key = str(out)
    if out.exists():
        RENDER_STATUS[key] = "done"
        return
    status = RENDER_STATUS.get(key)
    if status == "rendering":
        return
    if status and status.startswith("error"):
        # allow retry on next request
        pass
    RENDER_STATUS[key] = "rendering"
    executor_for_modes(mode_id).submit(_clip_render_job, video_id, frame, mode_id)


def ensure_compression_render(video_id: str, frame: int, variant_id: str) -> None:
    out = compression_clip_path(video_id, frame, variant_id)
    key = str(out)
    if out.exists():
        RENDER_STATUS[key] = "done"
        return
    status = RENDER_STATUS.get(key)
    if status == "rendering":
        return
    RENDER_STATUS[key] = "rendering"
    CPU_EXECUTOR.submit(_compression_render_job, video_id, frame, variant_id)


def render_state(video_id: str, frame: int, mode_id: str) -> str:
    out = png_path(video_id, frame, mode_id)
    if out.exists():
        return "done"
    return RENDER_STATUS.get(str(out), "pending")


def clip_render_state(video_id: str, frame: int, mode_id: str) -> str:
    out = clip_path(video_id, frame, mode_id)
    if out.exists():
        return "done"
    return RENDER_STATUS.get(str(out), "pending")


def compression_render_state(video_id: str, frame: int, variant_id: str) -> str:
    out = compression_clip_path(video_id, frame, variant_id)
    if out.exists():
        return "done"
    return RENDER_STATUS.get(str(out), "pending")


# ---------------------------------------------------------------------------
# Adaptive pairing
# ---------------------------------------------------------------------------

def pick_random_video_frame() -> tuple[str, int]:
    video = random.choice(CFG["videos"])
    n = int(video["frames"])
    # Clamp away from the very edges so QTGMC has temporal neighbours.
    lo, hi = (1, n - 2) if n > 3 else (0, max(0, n - 1))
    frame = random.randint(lo, hi)
    return video["id"], frame


def estimate_luma(video_id: str, frame: int) -> int | None:
    key = (video_id, frame)
    if key in LUMA_CACHE:
        return LUMA_CACHE[key]
    video = VIDEO_BY_ID.get(video_id)
    if not video:
        LUMA_CACHE[key] = None
        return None
    video_path = SAMPLE_VIDEO_DIR / video["file"]
    timestamp = max(0.0, frame / 25.0)
    cmd = [
        ffmpeg_exe(),
        "-v", "error",
        "-ss", f"{timestamp:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", "scale=1:1:flags=area,format=gray",
        "-f", "rawvideo",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=8)
        if proc.returncode == 0 and proc.stdout:
            LUMA_CACHE[key] = proc.stdout[0]
        else:
            LUMA_CACHE[key] = None
    except Exception:  # noqa: BLE001
        LUMA_CACHE[key] = None
    return LUMA_CACHE[key]


def pick_compression_video_frame() -> tuple[str, int, int | None, str]:
    want_dark = random.random() < COMPRESSION_DARK_PROBABILITY
    fallback: tuple[str, int, int | None] | None = None
    for _ in range(18 if want_dark else 5):
        video_id, frame = pick_random_video_frame()
        luma = estimate_luma(video_id, frame)
        fallback = fallback or (video_id, frame, luma)
        if luma is None:
            continue
        if want_dark and luma <= COMPRESSION_DARK_LUMA:
            return video_id, frame, luma, "dark"
        if not want_dark and luma > COMPRESSION_DARK_LUMA:
            return video_id, frame, luma, "light"
    if fallback:
        video_id, frame, luma = fallback
        return video_id, frame, luma, "dark-target" if want_dark else "light-target"
    video_id, frame = pick_random_video_frame()
    return video_id, frame, None, "unknown"


def modes_for_media(media_type: str) -> list[str]:
    return VIDEO_MODES if media_type == "video" else MODES


def game_counts_for_media(st: dict, media_type: str, mode_pool: list[str]) -> dict[str, int]:
    counts = {m: 0 for m in mode_pool}
    for vote in st.get("votes", []):
        if vote_media(vote) != media_type:
            continue
        left, right = vote.get("left_mode"), vote.get("right_mode")
        if left in counts:
            counts[left] += 1
        if right in counts:
            counts[right] += 1
    return counts


def lowest_game_class_pair(
    mode_pool: list[str],
    games: dict[str, int],
    avoid: tuple[str, str] | None = None,
) -> tuple[str, str]:
    groups: dict[int, list[str]] = {}
    for mode_id in mode_pool:
        groups.setdefault(games[mode_id], []).append(mode_id)
    lowest_counts = sorted(groups)
    primary = groups[lowest_counts[0]][:]
    random.shuffle(primary)

    a = primary[0]
    # Always pull one side from the lowest game-count bucket. The opponent is
    # a plain random draw from every other eligible mode.
    candidates = [m for m in mode_pool if m != a]

    if avoid and len(candidates) > 1:
        filtered = [m for m in candidates if {a, m} != set(avoid)]
        if filtered:
            candidates = filtered

    b = random.choice(candidates)

    if random.random() < 0.5:
        return a, b
    return b, a


def pick_modes(st: dict, avoid: tuple[str, str] | None = None, media_type: str = "photo") -> tuple[str, str]:
    mode_pool = modes_for_media(media_type)
    games = game_counts_for_media(st, media_type, mode_pool)
    return lowest_game_class_pair(mode_pool, games, avoid=avoid)


def build_pair(st: dict, avoid: tuple[str, str] | None = None, media_type: str = "photo") -> dict:
    video_id, frame = pick_random_video_frame()
    left, right = pick_modes(st, avoid=avoid, media_type=media_type)
    st["counter"] += 1
    return {
        "pair_id": f"p{st['counter']}__{video_id}__f{frame}__{left}__vs__{right}",
        "video_id": video_id,
        "frame": frame,
        "media_type": media_type,
        "left_mode": left,
        "right_mode": right,
    }


def start_pair_render(pair: dict) -> None:
    ensure_render(pair["video_id"], pair["frame"], pair["left_mode"])
    ensure_render(pair["video_id"], pair["frame"], pair["right_mode"])
    ensure_render(pair["video_id"], pair["frame"], ORIGINAL_MODE)


def start_pair_original_render(pair: dict) -> None:
    ensure_render(pair["video_id"], pair["frame"], ORIGINAL_MODE)


def start_pair_clip_render(pair: dict) -> None:
    ensure_clip_render(pair["video_id"], pair["frame"], pair["left_mode"])
    ensure_clip_render(pair["video_id"], pair["frame"], pair["right_mode"])


def pair_clips_ready(pair: dict) -> bool:
    return (
        clip_render_state(pair["video_id"], pair["frame"], pair["left_mode"]) == "done"
        and clip_render_state(pair["video_id"], pair["frame"], pair["right_mode"]) == "done"
    )


def valid_pair_for_media(pair: dict | None, media_type: str, allowed_modes: set[str]) -> bool:
    return (
        pair_matches_media(pair, media_type)
        and pair_uses_only(pair, allowed_modes)
    )


def pair_avoid(pair: dict | None) -> tuple[str, str] | None:
    if not valid_pair(pair):
        return None
    return pair["left_mode"], pair["right_mode"]


def start_video_clip_work(st: dict) -> None:
    """Render current clips first, then keep the video comparison buffer warm."""
    current = st.get("current_pair")
    if not current:
        return
    start_pair_clip_render(current)
    start_pair_original_render(current)
    if pair_clips_ready(current):
        for pair in st.get("pair_queue", []):
            start_pair_clip_render(pair)


def ensure_current_and_next(st: dict, media_type: str = "photo", render_images: bool | None = None) -> None:
    """Guarantee current_pair plus a small queue of prefetched comparisons."""
    if render_images is None:
        render_images = media_type == "photo"
    allowed_modes = set(modes_for_media(media_type))
    if not valid_pair_for_media(st.get("current_pair"), media_type, allowed_modes):
        st["current_pair"] = build_pair(st, media_type=media_type)
    if render_images:
        start_pair_render(st["current_pair"])

    queue = [
        pair
        for pair in st.get("pair_queue", [])
        if valid_pair_for_media(pair, media_type, allowed_modes)
    ]
    while len(queue) < PREFETCH_PAIRS:
        avoid = pair_avoid(queue[-1] if queue else st["current_pair"])
        queue.append(build_pair(st, avoid=avoid, media_type=media_type))
    st["pair_queue"] = queue[:PREFETCH_PAIRS]
    if render_images:
        for pair in st["pair_queue"]:
            start_pair_render(pair)


def advance_pair(st: dict, media_type: str) -> None:
    allowed_modes = set(modes_for_media(media_type))
    queue = [
        pair
        for pair in st.get("pair_queue", [])
        if valid_pair_for_media(pair, media_type, allowed_modes)
    ]
    st["current_pair"] = queue.pop(0) if queue else None
    st["pair_queue"] = queue
    ensure_current_and_next(st, media_type=media_type)


# ---------------------------------------------------------------------------
# Elo
# ---------------------------------------------------------------------------

def expected(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def vote_media(vote: dict) -> str:
    media = vote.get("media_type") or vote.get("media") or vote.get("compare_mode")
    if media == "compression":
        return "compression"
    if media in {"video", "clip"}:
        return "video"
    return "photo"


def media_weight(media_type: str) -> float:
    return 2.0 if media_type == "video" else 1.0


def is_top_pair_vote(vote: dict) -> bool:
    return vote.get("source") == "top3" and "top3pair" in str(vote.get("top3_id") or vote.get("pair_id") or "")


def is_compression_vote(vote: dict) -> bool:
    return vote.get("source") == "compression"


def apply_vote_to_tables(
    elo: dict[str, float],
    games: dict[str, float],
    left: str,
    right: str,
    winner: str,
    weight: float = 1.0,
) -> None:
    ea = expected(elo[left], elo[right])
    eb = 1.0 - ea
    if winner == "left":
        sa, sb = 1.0, 0.0
    elif winner == "right":
        sa, sb = 0.0, 1.0
    else:  # tie
        sa, sb = 0.5, 0.5
    elo[left] += K_FACTOR * weight * (sa - ea)
    elo[right] += K_FACTOR * weight * (sb - eb)
    games[left] += weight
    games[right] += weight


def apply_vote_to_elo(st: dict, left: str, right: str, winner: str, media_type: str = "photo") -> None:
    apply_vote_to_tables(st["elo"], st["games"], left, right, winner, media_weight(media_type))


def recompute_elo(st: dict) -> None:
    st["elo"] = {m: START_ELO for m in MODES}
    st["games"] = {m: 0 for m in MODES}
    for v in st["votes"]:
        if v["left_mode"] in st["elo"] and v["right_mode"] in st["elo"]:
            apply_vote_to_elo(st, v["left_mode"], v["right_mode"], v["winner"], vote_media(v))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def compute_results(st: dict) -> dict:
    def ranking_for(kind: str) -> dict:
        mode_ids = VIDEO_MODES if kind == "video" else MODES
        elo = {m: START_ELO for m in mode_ids}
        games = {m: 0.0 for m in mode_ids}
        vote_count = 0
        weighted_votes = 0.0
        for v in st["votes"]:
            left, right = v["left_mode"], v["right_mode"]
            if left not in elo or right not in elo:
                continue
            media = vote_media(v)
            if kind in MEDIA_TYPES and media != kind:
                continue
            weight = media_weight(media) if kind == "combined" else 1.0
            apply_vote_to_tables(elo, games, left, right, v["winner"], weight)
            vote_count += 1
            weighted_votes += weight

        rows = []
        for m in mode_ids:
            rows.append({
                "id": m,
                "label": MODE_BY_ID[m]["label"],
                "elo": round(elo[m], 1),
                "games": games[m],
            })
        rows.sort(key=lambda r: (-r["elo"], -r["games"], r["label"]))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return {
            "ranking": rows,
            "vote_count": vote_count,
            "weighted_votes": weighted_votes,
        }

    def top_pair_ranking() -> dict:
        mode_ids = VIDEO_MODES
        elo = {m: START_ELO for m in mode_ids}
        games = {m: 0.0 for m in mode_ids}
        vote_count = 0
        for v in st["votes"]:
            if not is_top_pair_vote(v):
                continue
            left, right = v["left_mode"], v["right_mode"]
            if left not in elo or right not in elo:
                continue
            apply_vote_to_tables(elo, games, left, right, v["winner"], 1.0)
            vote_count += 1

        rows = []
        for m in mode_ids:
            if games[m] == 0:
                continue
            rows.append({
                "id": m,
                "label": MODE_BY_ID[m]["label"],
                "elo": round(elo[m], 1),
                "games": games[m],
            })
        rows.sort(key=lambda r: (-r["elo"], -r["games"], r["label"]))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return {
            "ranking": rows,
            "vote_count": vote_count,
            "weighted_votes": float(vote_count),
            "mode_count": top_comparison_mode_count(),
        }

    def compression_ranking() -> dict:
        variant_ids = [variant["id"] for variant in COMPRESSION_VARIANTS]
        elo = {variant_id: START_ELO for variant_id in variant_ids}
        games = {variant_id: 0.0 for variant_id in variant_ids}
        vote_count = 0
        for v in st["votes"]:
            if not is_compression_vote(v):
                continue
            left, right = v["left_mode"], v["right_mode"]
            if left not in elo or right not in elo:
                continue
            apply_vote_to_tables(elo, games, left, right, v["winner"], 1.0)
            vote_count += 1

        rows = []
        for variant_id in variant_ids:
            if games[variant_id] == 0:
                continue
            rows.append({
                "id": variant_id,
                "label": COMPRESSION_BY_ID[variant_id]["label"],
                "elo": round(elo[variant_id], 1),
                "games": games[variant_id],
            })
        rows.sort(key=lambda r: (-r["elo"], -r["games"], r["label"]))
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return {
            "ranking": rows,
            "vote_count": vote_count,
            "weighted_votes": float(vote_count),
            "variant_count": len(variant_ids),
        }

    # Head-to-head win matrix (wins[a][b] = times a beat b; ties count 0.5 each).
    wins: dict[str, dict[str, float]] = {a: {b: 0.0 for b in MODES} for a in MODES}
    media_counts = {"photo": 0, "video": 0, "top_pairs": 0, "compression": 0, "combined_weight": 0.0}
    for v in st["votes"]:
        l, r, w = v["left_mode"], v["right_mode"], v["winner"]
        media = vote_media(v)
        if media in media_counts:
            media_counts[media] += 1
        if is_top_pair_vote(v):
            media_counts["top_pairs"] += 1
        if l not in wins or r not in wins[l]:
            continue
        weight = media_weight(media)
        media_counts["combined_weight"] += weight
        if w == "left":
            wins[l][r] += weight
        elif w == "right":
            wins[r][l] += weight
        else:
            wins[l][r] += 0.5 * weight
            wins[r][l] += 0.5 * weight

    rankings = {
        "combined": ranking_for("combined"),
        "top_pairs": top_pair_ranking(),
        "compression": compression_ranking(),
        "video": ranking_for("video"),
        "photo": ranking_for("photo"),
    }

    return {
        "ranking": rankings["combined"]["ranking"],
        "rankings": rankings,
        "vote_count": len(st["votes"]),
        "media_counts": media_counts,
        "modes": [
            {
                "id": m,
                "label": MODE_BY_ID[m]["label"],
                "supports_video": MODE_BY_ID[m].get("supports_video", True),
            }
            for m in MODES
        ],
        "win_matrix": wins,
        "votes": st["votes"][-200:],
    }


def top3_mode_ids(st: dict) -> list[str]:
    return top_comparison_mode_ids()


def build_top3_comparison(st: dict) -> dict:
    modes = top3_mode_ids(st)
    if len(modes) < 2:
        raise RuntimeError("need at least two modes for top comparison")
    pair_options = [
        (i, j)
        for i in range(len(modes))
        for j in range(i + 1, len(modes))
    ]
    cursor = st.get("top3_pair_cursor", 0)
    pair_indices = pair_options[cursor % len(pair_options)]
    st["top3_pair_cursor"] = cursor + 1
    pair_modes = [modes[i] for i in pair_indices]
    random.shuffle(pair_modes)
    video_id, frame = pick_random_video_frame()
    st["counter"] += 1
    return {
        "top3_id": f"t{st['counter']}__{video_id}__f{frame}__top3pair",
        "video_id": video_id,
        "frame": frame,
        "modes": modes,
        "pair_modes": pair_modes,
    }


def start_top3_clip_render(comp: dict) -> None:
    for mode_id in comp["pair_modes"]:
        ensure_clip_render(comp["video_id"], comp["frame"], mode_id)


def top3_matches_modes(comp: dict | None, top_modes: set[str]) -> bool:
    return valid_top3(comp) and set(comp["modes"]) == top_modes


def ensure_top3_comparison(st: dict, render_queue: bool = True) -> None:
    top_modes = set(top3_mode_ids(st))
    comp = st.get("top3_comparison")
    if not top3_matches_modes(comp, top_modes):
        comp = build_top3_comparison(st)
        st["top3_comparison"] = comp
    start_top3_clip_render(comp)

    queue = [
        queued
        for queued in st.get("top3_queue", [])
        if top3_matches_modes(queued, top_modes)
    ]
    while len(queue) < PREFETCH_PAIRS:
        queue.append(build_top3_comparison(st))
    st["top3_queue"] = queue[:PREFETCH_PAIRS]
    if render_queue:
        for queued in st["top3_queue"]:
            start_top3_clip_render(queued)


def advance_top3_comparison(st: dict) -> None:
    top_modes = set(top3_mode_ids(st))
    queue = [
        queued
        for queued in st.get("top3_queue", [])
        if top3_matches_modes(queued, top_modes)
    ]
    st["top3_comparison"] = queue.pop(0) if queue else None
    st["top3_queue"] = queue


def build_compression_comparison(st: dict) -> dict:
    variant_ids = [variant["id"] for variant in COMPRESSION_VARIANTS]
    pair_options = [
        (i, j)
        for i in range(len(variant_ids))
        for j in range(i + 1, len(variant_ids))
    ]
    cursor = st.get("compression_pair_cursor", 0)
    pair_indices = pair_options[cursor % len(pair_options)]
    st["compression_pair_cursor"] = cursor + 1
    variants = [variant_ids[i] for i in pair_indices]
    random.shuffle(variants)
    video_id, frame, luma, scene_kind = pick_compression_video_frame()
    st["counter"] += 1
    return {
        "compression_id": f"c{st['counter']}__{video_id}__f{frame}__compression",
        "video_id": video_id,
        "frame": frame,
        "variants": variants,
        "base_mode": COMPRESSION_BASE_MODE,
        "luma": luma,
        "scene_kind": scene_kind,
    }


def compression_matches_variants(comp: dict | None) -> bool:
    return valid_compression(comp) and set(comp["variants"]).issubset(COMPRESSION_BY_ID)


def start_compression_render(comp: dict) -> None:
    for variant_id in comp["variants"]:
        ensure_compression_render(comp["video_id"], comp["frame"], variant_id)


def ensure_compression_comparison(st: dict, render_queue: bool = True) -> None:
    comp = st.get("compression_comparison")
    if not compression_matches_variants(comp):
        comp = build_compression_comparison(st)
        st["compression_comparison"] = comp
    start_compression_render(comp)

    queue = [
        queued
        for queued in st.get("compression_queue", [])
        if compression_matches_variants(queued)
    ]
    while len(queue) < PREFETCH_PAIRS:
        queue.append(build_compression_comparison(st))
    st["compression_queue"] = queue[:PREFETCH_PAIRS]
    if render_queue:
        for queued in st["compression_queue"]:
            start_compression_render(queued)


def advance_compression_comparison(st: dict) -> None:
    queue = [
        queued
        for queued in st.get("compression_queue", [])
        if compression_matches_variants(queued)
    ]
    st["compression_comparison"] = queue.pop(0) if queue else None
    st["compression_queue"] = queue
    ensure_compression_comparison(st, render_queue=True)


# ---------------------------------------------------------------------------
# Pair serialisation
# ---------------------------------------------------------------------------

def serialize_pair(pair: dict, include_clips: bool = False) -> dict:
    v, f = pair["video_id"], pair["frame"]
    lmode, rmode = pair["left_mode"], pair["right_mode"]
    ls = render_state(v, f, lmode)
    rs = render_state(v, f, rmode)
    os = render_state(v, f, ORIGINAL_MODE)
    lcs = clip_render_state(v, f, lmode) if include_clips else "pending"
    rcs = clip_render_state(v, f, rmode) if include_clips else "pending"
    both_ready = ls == "done" and rs == "done"
    clips_ready = lcs == "done" and rcs == "done"
    left_err = ls.startswith("error") if isinstance(ls, str) else False
    right_err = rs.startswith("error") if isinstance(rs, str) else False
    original_err = os.startswith("error") if isinstance(os, str) else False
    left_clip_err = lcs.startswith("error") if isinstance(lcs, str) else False
    right_clip_err = rcs.startswith("error") if isinstance(rcs, str) else False
    return {
        "pair_id": pair["pair_id"],
        "media_type": pair.get("media_type", "photo"),
        "video": {"id": v, "label": VIDEO_BY_ID[v]["label"]},
        "frame": f,
        "ready": both_ready,
        "video_ready": clips_ready,
        "clip_seconds": CLIP_SECONDS,
        "left": {
            "key": "A",
            "mode_id": lmode,
            "mode_label": MODE_BY_ID[lmode]["label"],
            "status": ls,
            "image": f"/frames/{png_name(v, f, lmode)}" if ls == "done" else None,
            "error": ls if left_err else None,
            "clip_status": lcs,
            "clip": f"/clips/{clip_name(v, f, lmode)}" if lcs == "done" else None,
            "clip_error": lcs if left_clip_err else None,
        },
        "right": {
            "key": "B",
            "mode_id": rmode,
            "mode_label": MODE_BY_ID[rmode]["label"],
            "status": rs,
            "image": f"/frames/{png_name(v, f, rmode)}" if rs == "done" else None,
            "error": rs if right_err else None,
            "clip_status": rcs,
            "clip": f"/clips/{clip_name(v, f, rmode)}" if rcs == "done" else None,
            "clip_error": rcs if right_clip_err else None,
        },
        "original": {
            "key": "O",
            "status": os,
            "image": f"/frames/{png_name(v, f, ORIGINAL_MODE)}" if os == "done" else None,
            "error": os if original_err else None,
        },
    }


def serialize_top3(comp: dict) -> dict:
    v, f = comp["video_id"], comp["frame"]
    sides = []
    ready = True
    keys = ["A", "B"]
    for key, mode_id in zip(keys, comp["pair_modes"], strict=True):
        status = clip_render_state(v, f, mode_id)
        is_error = status.startswith("error") if isinstance(status, str) else False
        ready = ready and status == "done"
        sides.append({
            "key": key,
            "mode_id": mode_id,
            "mode_label": MODE_BY_ID[mode_id]["label"],
            "clip_status": status,
            "clip": f"/clips/{clip_name(v, f, mode_id)}" if status == "done" else None,
            "clip_error": status if is_error else None,
        })
    return {
        "top3_id": comp["top3_id"],
        "video": {"id": v, "label": VIDEO_BY_ID[v]["label"]},
        "frame": f,
        "ready": ready,
        "top_modes": comp["modes"],
        "sides": sides,
        "clip_seconds": CLIP_SECONDS,
    }


def serialize_compression(comp: dict) -> dict:
    v, f = comp["video_id"], comp["frame"]
    sides = []
    ready = True
    for key, variant_id in zip(["A", "B"], comp["variants"], strict=True):
        status = compression_render_state(v, f, variant_id)
        is_error = status.startswith("error") if isinstance(status, str) else False
        ready = ready and status == "done"
        variant = COMPRESSION_BY_ID[variant_id]
        sides.append({
            "key": key,
            "variant_id": variant_id,
            "variant_label": variant["label"],
            "status": status,
            "clip": f"/compression-clips/{compression_clip_name(v, f, variant_id)}" if status == "done" else None,
            "error": status if is_error else None,
            "settings": {
                "preset": variant["preset"],
                "crf": variant["crf"],
                "tune": variant.get("tune"),
                "pix_fmt": "yuv420p",
                "profile": "high",
                "level": "4.0",
            },
        })
    return {
        "compression_id": comp["compression_id"],
        "video": {"id": v, "label": VIDEO_BY_ID[v]["label"]},
        "frame": f,
        "ready": ready,
        "base_mode": comp.get("base_mode", COMPRESSION_BASE_MODE),
        "base_label": MODE_BY_ID[COMPRESSION_BASE_MODE]["label"],
        "clip_seconds": COMPRESSION_CLIP_SECONDS,
        "scene": {
            "kind": comp.get("scene_kind", "unknown"),
            "luma": comp.get("luma"),
            "dark_threshold": COMPRESSION_DARK_LUMA,
        },
        "sides": sides,
    }


def serialize_preview(job: dict) -> dict:
    status = job.get("status", "pending")
    payload = {
        "job_id": job["job_id"],
        "status": status,
        "file_name": job["file_name"],
        "mode_id": job["mode_id"],
        "mode_label": MODE_BY_ID[job["mode_id"]]["label"],
        "mode_b_id": job.get("mode_b_id"),
        "mode_b_label": MODE_BY_ID[job["mode_b_id"]]["label"] if job.get("mode_b_id") else None,
        "frame": job.get("frame", 0),
        "clip_seconds": None,
        "full_video": True,
        "clip": job.get("clip") if status == "done" else None,
        "clip_b": job.get("clip_b") if status == "done" else None,
        "original_clip": job.get("original_clip") if status == "done" else None,
        "error": job.get("error") if status == "error" else None,
    }
    return payload


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str, *, cache_control: str = "no-store", ranges: bool = False) -> None:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self.send_error(404, "not found")
            return
        range_header = self.headers.get("Range") if ranges else None
        start, end = 0, size - 1
        status = 200
        if range_header:
            try:
                unit, spec = range_header.split("=", 1)
                if unit.strip().lower() != "bytes":
                    raise ValueError
                first, _, last = spec.partition("-")
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    suffix = int(last)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, size - suffix)
                    end = size - 1
                if start < 0 or end < start or start >= size:
                    raise ValueError
                end = min(end, size - 1)
                status = 206
            except (ValueError, AttributeError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _save_raw_upload(self, target: Path, length: int) -> None:
        remaining = length
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError("upload ended early")
                fh.write(chunk)
                remaining -= len(chunk)

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/api/pair":
            query = parse_qs(parsed.query)
            include_clips = query.get("media", ["image"])[0] == "video"
            media_type = "video" if include_clips else "photo"
            with LOCK:
                ensure_current_and_next(STATE, media_type=media_type)
                if include_clips:
                    start_video_clip_work(STATE)
                save_state(STATE)
                payload = serialize_pair(STATE["current_pair"], include_clips=include_clips)
                payload["pairing_mode"] = STATE.get("pairing_mode", DEFAULT_PAIRING_MODE)
                payload["progress"] = {"votes": len(STATE["votes"])}
                payload["prefetch"] = {
                    "target": PREFETCH_PAIRS,
                    "queued": len(STATE.get("pair_queue", [])),
                }
            self._json(200, payload)
            return
        if path == "/api/results":
            with LOCK:
                self._json(200, compute_results(STATE))
            return
        if path == "/api/top3":
            with LOCK:
                ensure_top3_comparison(STATE, render_queue=True)
                save_state(STATE)
                payload = serialize_top3(STATE["top3_comparison"])
                payload["progress"] = {"votes": len(STATE["votes"])}
                payload["prefetch"] = {
                    "target": PREFETCH_PAIRS,
                    "queued": len(STATE.get("top3_queue", [])),
                }
            self._json(200, payload)
            return
        if path == "/api/compression":
            with LOCK:
                ensure_compression_comparison(STATE, render_queue=True)
                save_state(STATE)
                payload = serialize_compression(STATE["compression_comparison"])
                payload["progress"] = {"votes": len(STATE["votes"])}
                payload["prefetch"] = {
                    "target": PREFETCH_PAIRS,
                    "queued": len(STATE.get("compression_queue", [])),
                }
            self._json(200, payload)
            return
        if path.startswith("/api/preview/"):
            job_id = path[len("/api/preview/"):]
            with LOCK:
                job = PREVIEW_JOBS.get(job_id)
                if not job:
                    self.send_error(404, "preview job not found")
                    return
                self._json(200, serialize_preview(job))
            return
        if path == "/api/state":
            with LOCK:
                media_counts = {"photo": 0, "video": 0, "top_pairs": 0, "compression": 0}
                for vote in STATE["votes"]:
                    media = vote_media(vote)
                    if media in media_counts:
                        media_counts[media] += 1
                    if is_top_pair_vote(vote):
                        media_counts["top_pairs"] += 1
                self._json(200, {
                    "votes": len(STATE["votes"]),
                    "media_counts": media_counts,
                    "pairing_mode": STATE.get("pairing_mode", DEFAULT_PAIRING_MODE),
                    "modes": len(MODES),
                    "video_modes": len(VIDEO_MODES),
                    "prefetch_target": PREFETCH_PAIRS,
                    "prefetch_queued": len(STATE.get("pair_queue", [])),
                    "top3_prefetch_queued": len(STATE.get("top3_queue", [])),
                    "compression_prefetch_queued": len(STATE.get("compression_queue", [])),
                    "videos": len(CFG["videos"]),
                    "video_ids": [v["id"] for v in CFG["videos"]],
                })
            return
        if path == "/api/final-samples":
            self._json(200, {"samples": load_final_samples()})
            return
        if path.startswith("/frames/"):
            target = (FRAMES_DIR / path[len("/frames/"):]).resolve()
            if FRAMES_DIR not in target.parents:
                self.send_error(403, "forbidden")
                return
            self._file(target, "image/png")
            return
        if path.startswith("/clips/"):
            target = (CLIPS_DIR / path[len("/clips/"):]).resolve()
            if CLIPS_DIR not in target.parents:
                self.send_error(403, "forbidden")
                return
            self._file(target, "video/mp4", cache_control="public, max-age=31536000, immutable", ranges=True)
            return
        if path.startswith("/compression-clips/"):
            target = (COMPRESSION_CLIPS_DIR / path[len("/compression-clips/"):]).resolve()
            if COMPRESSION_CLIPS_DIR not in target.parents:
                self.send_error(403, "forbidden")
                return
            self._file(target, "video/mp4", cache_control="public, max-age=31536000, immutable", ranges=True)
            return
        if path.startswith("/preview-clips/"):
            target = (PREVIEW_CLIPS_DIR / path[len("/preview-clips/"):]).resolve()
            if PREVIEW_CLIPS_DIR not in target.parents:
                self.send_error(403, "forbidden")
                return
            self._file(target, "video/mp4", cache_control="no-store", ranges=True)
            return
        if path.startswith("/final-samples/"):
            target = (FINAL_SAMPLES_DIR / path[len("/final-samples/"):]).resolve()
            if FINAL_SAMPLES_DIR not in target.parents:
                self.send_error(403, "forbidden")
                return
            self._file(target, guess_type(target), cache_control="no-store", ranges=True)
            return
        if path.startswith("/web/"):
            target = (WEB_DIR / path[len("/web/"):]).resolve()
            if WEB_DIR not in target.parents and target != WEB_DIR:
                self.send_error(403, "forbidden")
                return
            self._file(target, guess_type(target))
            return
        self.send_error(404, "not found")

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/preview":
            mode_id = self.headers.get("X-Mode-Id", "").strip()
            if mode_id not in MODE_BY_ID:
                self.send_error(400, "bad mode")
                return
            mode_b_id = self.headers.get("X-Mode-B-Id", "").strip() or None
            if mode_b_id and mode_b_id not in MODE_BY_ID:
                self.send_error(400, "bad mode B")
                return
            try:
                frame = max(0, int(self.headers.get("X-Frame", "0")))
            except ValueError:
                self.send_error(400, "bad frame")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                self.send_error(400, "empty upload")
                return

            raw_name = unquote(self.headers.get("X-File-Name", "preview"))
            file_name = clean_filename(raw_name)
            job_id = uuid.uuid4().hex
            upload = preview_upload_path(job_id, file_name)
            clip = preview_clip_path(job_id, mode_id)
            clip_b = preview_clip_path(job_id, mode_b_id) if mode_b_id else None
            original_clip = preview_clip_path(job_id, ORIGINAL_MODE)
            try:
                self._save_raw_upload(upload, length)
            except OSError as exc:
                upload.unlink(missing_ok=True)
                self.send_error(400, f"upload failed: {exc}")
                return

            job = {
                "job_id": job_id,
                "status": "uploaded",
                "file_name": file_name,
                "mode_id": mode_id,
                "mode_b_id": mode_b_id,
                "frame": frame,
                "upload_path": str(upload),
                "clip_path": str(clip),
                "clip_b_path": str(clip_b) if clip_b else None,
                "original_clip_path": str(original_clip),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            with LOCK:
                PREVIEW_JOBS[job_id] = job
                start_preview_render(job)
                payload = serialize_preview(job)
            self._json(202, payload)
            return
        if path == "/api/vote":
            try:
                body = self._body()
            except json.JSONDecodeError:
                self.send_error(400, "bad json")
                return
            pair_id = body.get("pair_id")
            winner = body.get("winner")
            media_type = body.get("media_type") or body.get("compare_mode") or "photo"
            if media_type == "image":
                media_type = "photo"
            if winner not in {"left", "right", "tie"} or not pair_id:
                self.send_error(400, "bad payload")
                return
            if media_type not in MEDIA_TYPES:
                self.send_error(400, "bad media_type")
                return
            with LOCK:
                cur = STATE.get("current_pair")
                if not cur or cur["pair_id"] != pair_id:
                    # Stale tab; just resync.
                    ensure_current_and_next(STATE, media_type=media_type)
                    self._json(200, {"ok": False, "resync": True})
                    return
                apply_vote_to_elo(STATE, cur["left_mode"], cur["right_mode"], winner, media_type)
                STATE["votes"].append({
                    "pair_id": pair_id,
                    "media_type": media_type,
                    "video_id": cur["video_id"],
                    "frame": cur["frame"],
                    "left_mode": cur["left_mode"],
                    "right_mode": cur["right_mode"],
                    "winner": winner,
                    "at": datetime.now(timezone.utc).isoformat(),
                })
                STATE["top3_comparison"] = None
                advance_pair(STATE, media_type=media_type)
                if media_type == "video":
                    start_video_clip_work(STATE)
                save_state(STATE)
                self._json(200, {"ok": True, "votes": len(STATE["votes"])})
            return
        if path == "/api/top3-vote":
            try:
                body = self._body()
            except json.JSONDecodeError:
                self.send_error(400, "bad json")
                return
            top3_id = body.get("top3_id")
            winner_key = body.get("winner_key")
            if winner_key not in {"A", "B", "skip", "idk"} or not top3_id:
                self.send_error(400, "bad payload")
                return
            with LOCK:
                comp = STATE.get("top3_comparison")
                if not valid_top3(comp) or comp["top3_id"] != top3_id:
                    ensure_top3_comparison(STATE, render_queue=True)
                    self._json(200, {"ok": False, "resync": True})
                    return
                if winner_key in {"skip", "idk"}:
                    advance_top3_comparison(STATE)
                    ensure_top3_comparison(STATE, render_queue=True)
                    save_state(STATE)
                    self._json(200, {"ok": True, "skipped": True, "votes": len(STATE["votes"])})
                    return
                key_to_mode = dict(zip(["A", "B"], comp["pair_modes"], strict=True))
                winner_mode = key_to_mode[winner_key]
                at = datetime.now(timezone.utc).isoformat()
                for loser_key, loser_mode in key_to_mode.items():
                    if loser_key == winner_key:
                        continue
                    apply_vote_to_elo(STATE, winner_mode, loser_mode, "left", "video")
                    STATE["votes"].append({
                        "pair_id": f"{top3_id}__{winner_key}_beats_{loser_key}",
                        "top3_id": top3_id,
                        "source": "top3",
                        "choice_key": winner_key,
                        "media_type": "video",
                        "video_id": comp["video_id"],
                        "frame": comp["frame"],
                        "left_mode": winner_mode,
                        "right_mode": loser_mode,
                        "winner": "left",
                        "at": at,
                    })
                advance_top3_comparison(STATE)
                ensure_top3_comparison(STATE, render_queue=True)
                save_state(STATE)
                self._json(200, {"ok": True, "votes": len(STATE["votes"])})
            return
        if path == "/api/compression-vote":
            try:
                body = self._body()
            except json.JSONDecodeError:
                self.send_error(400, "bad json")
                return
            compression_id = body.get("compression_id")
            winner_key = body.get("winner_key")
            if winner_key not in {"A", "B", "skip", "idk"} or not compression_id:
                self.send_error(400, "bad payload")
                return
            with LOCK:
                comp = STATE.get("compression_comparison")
                if not valid_compression(comp) or comp["compression_id"] != compression_id:
                    ensure_compression_comparison(STATE, render_queue=True)
                    self._json(200, {"ok": False, "resync": True})
                    return
                if winner_key in {"skip", "idk"}:
                    advance_compression_comparison(STATE)
                    save_state(STATE)
                    self._json(200, {"ok": True, "skipped": True, "votes": len(STATE["votes"])})
                    return
                key_to_variant = dict(zip(["A", "B"], comp["variants"], strict=True))
                winner_variant = key_to_variant[winner_key]
                loser_key = "B" if winner_key == "A" else "A"
                loser_variant = key_to_variant[loser_key]
                STATE["votes"].append({
                    "pair_id": f"{compression_id}__{winner_key}_beats_{loser_key}",
                    "compression_id": compression_id,
                    "source": "compression",
                    "choice_key": winner_key,
                    "media_type": "compression",
                    "video_id": comp["video_id"],
                    "frame": comp["frame"],
                    "scene_kind": comp.get("scene_kind"),
                    "luma": comp.get("luma"),
                    "base_mode": comp.get("base_mode", COMPRESSION_BASE_MODE),
                    "left_mode": winner_variant,
                    "right_mode": loser_variant,
                    "winner": "left",
                    "at": datetime.now(timezone.utc).isoformat(),
                })
                advance_compression_comparison(STATE)
                save_state(STATE)
                self._json(200, {"ok": True, "votes": len(STATE["votes"])})
            return
        if path == "/api/pairing-mode":
            try:
                body = self._body()
            except json.JSONDecodeError:
                self.send_error(400, "bad json")
                return
            mode = body.get("mode") or body.get("pairing_mode")
            media_type = body.get("media_type") or "photo"
            if media_type == "image":
                media_type = "photo"
            if mode not in PAIRING_MODES:
                self.send_error(400, "bad pairing mode")
                return
            if media_type not in MEDIA_TYPES:
                self.send_error(400, "bad media_type")
                return
            with LOCK:
                if STATE.get("pairing_mode", DEFAULT_PAIRING_MODE) != mode:
                    STATE["pairing_mode"] = mode
                    STATE["pair_queue"] = []
                ensure_current_and_next(STATE, media_type=media_type)
                if media_type == "video":
                    start_video_clip_work(STATE)
                save_state(STATE)
                self._json(200, {
                    "ok": True,
                    "pairing_mode": STATE.get("pairing_mode", DEFAULT_PAIRING_MODE),
                    "current_pair": STATE["current_pair"]["pair_id"],
                    "prefetch_pairs": [pair["pair_id"] for pair in STATE.get("pair_queue", [])],
                })
            return
        if path == "/api/undo":
            with LOCK:
                media_type = "photo"
                if STATE["votes"]:
                    last = STATE["votes"].pop()
                    media_type = vote_media(last)
                    top3_id = last.get("top3_id") if last.get("source") == "top3" else None
                    if top3_id:
                        while STATE["votes"] and STATE["votes"][-1].get("top3_id") == top3_id:
                            STATE["votes"].pop()
                    recompute_elo(STATE)
                    STATE["top3_comparison"] = None
                    if media_type == "compression":
                        STATE["compression_comparison"] = None
                        STATE["compression_queue"] = []
                    elif top3_id:
                        STATE["current_pair"] = None
                    else:
                        # Put that exact pairing back as current (PNGs are cached).
                        STATE["counter"] += 1
                        undo_pair = {
                            "pair_id": f"p{STATE['counter']}__undo__{last['video_id']}__f{last['frame']}__{last['left_mode']}__vs__{last['right_mode']}",
                            "video_id": last["video_id"],
                            "frame": last["frame"],
                            "media_type": media_type,
                            "left_mode": last["left_mode"],
                            "right_mode": last["right_mode"],
                        }
                        STATE["current_pair"] = undo_pair if valid_pair(undo_pair) else None
                    if media_type != "compression":
                        STATE["pair_queue"] = []
                if media_type == "compression":
                    ensure_compression_comparison(STATE, render_queue=True)
                else:
                    ensure_current_and_next(STATE, media_type=media_type)
                    if media_type == "video":
                        start_video_clip_work(STATE)
                save_state(STATE)
                self._json(200, {"ok": True, "votes": len(STATE["votes"])})
            return
        if path == "/api/reset":
            with LOCK:
                STATE.clear()
                STATE.update(fresh_state())
                ensure_current_and_next(STATE, media_type="photo")
                save_state(STATE)
                self._json(200, {"ok": True})
            return
        self.send_error(404, "not found")


def guess_type(path: Path) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".png": "image/png",
        ".mp4": "video/mp4",
    }.get(path.suffix.lower(), "application/octet-stream")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    try:
        initialize_runtime()
    except RuntimeError as exc:
        ap.error(str(exc))

    FRAMES_DIR.mkdir(exist_ok=True)
    CLIPS_DIR.mkdir(exist_ok=True)
    COMPRESSION_CLIPS_DIR.mkdir(exist_ok=True)
    PREVIEW_UPLOADS_DIR.mkdir(exist_ok=True)
    PREVIEW_CLIPS_DIR.mkdir(exist_ok=True)
    FINAL_SAMPLES_DIR.mkdir(exist_ok=True)
    with LOCK:
        ensure_current_and_next(STATE, media_type="photo")
        save_state(STATE)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving on http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    print(f"  {len(MODES)} deinterlacer modes, {len(CFG['videos'])} videos")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        srv.server_close()
        CPU_EXECUTOR.shutdown(wait=False)
        AI_EXECUTOR.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
