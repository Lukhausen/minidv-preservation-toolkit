#!/usr/bin/env python3
"""Build best DVRescue outputs from duplicated/partial DV AVI captures.

The script runs against one explicit cassette folder. It never deletes or moves
source AVI files unless the guarded delete mode is requested. Its final product
is one folder named KEEP, containing one recommended video file per cluster plus
one JSON report.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - fallback for bare Python installs.
    tqdm = None


SCRIPT_ROOT = Path(__file__).resolve().parents[2]
ROOT = SCRIPT_ROOT
DVRESCUE = SCRIPT_ROOT / "DVRescue.exe"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
RAW_DV_FRAME_BYTES_PAL = 144_000
DV_FPS = 25
DV_ABST_STEP_PAL = 12
RANGE_COUNT_TOLERANCE_FRAMES = 2
BAD_FRAME_EXCLUSION_RATIO = 0.98
MIN_KEEP_DURATION_SECONDS = 0.5
MICRO_GAP_STITCH_MAX_SECONDS = 0.1
V2_TIMELINE_CLUSTER_GAP_SECONDS = 0.4
CAMERA_MODEL = os.environ.get("MINIDV_CAMERA_MODEL", "Camera Model")
METADATA_SCHEMA = "dvrescue_keep_metadata_v1"
EMBEDDED_DATETIME_TOLERANCE_SECONDS = 2.0
V2_UNPLACEABLE_START_ALIGNMENT_TOLERANCE_FRAMES = 2
V2_STABLE_RDT_ANCHOR_LOOKAHEAD_FRAMES = 50
V2_STABLE_RDT_ANCHOR_TOLERANCE_FRAMES = 2

# Pressure-tested against the real footage: absolute-track (abst) is the reliable
# coordinate WITHIN a single recording/cluster (98.7% of multi-file clusters form
# one coherent abst band), but it loops/resets across recordings just like
# timecode, so it must never be used as a global identity key. It is therefore
# the preferred *within-cluster* coverage basis only. recording_datetime_timecode
# and timecode remain as fallbacks for the rare clusters where abst is locally
# incoherent or missing.
ABST_SEAM_TOLERANCE_FRAMES = 2          # max |abst delta| (in frames) accepted at a stitch seam
SEAM_TOLERANCE_FRAMES = 2               # coverage slivers <= this are treated as covered (abst rounding at seams)

PROBLEM_ATTRS = {
    "abst_r",
    "tc_r",
    "rdt_r",
    "seqn_r",
    "abst_nc",
    "tc_nc",
    "rdt_nc",
    "seqn_nc",
    "full_conceal_aud",
    "full_conceal_vid",
    "conceal_aud_type",
    "conceal_vid_type",
    "conceal_aud_value",
    "conceal_vid_value",
    "source_error",
}
PROBLEM_CHILDREN = {"sta", "aud", "vid"}
AUDIO_PROBLEM_ATTRS = {
    "full_conceal_aud",
    "conceal_aud_type",
    "conceal_aud_value",
}
AUDIO_PROBLEM_CHILDREN = {"aud"}
VIDEO_PROBLEM_ATTRS = {
    "full_conceal_vid",
    "conceal_vid_type",
    "conceal_vid_value",
}
VIDEO_PROBLEM_CHILDREN = {"vid"}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def ensure_target_safe() -> None:
    if not ROOT.exists() or not ROOT.is_dir():
        raise SystemExit(f"Target folder does not exist: {ROOT}")
    if ROOT == SCRIPT_ROOT:
        raise SystemExit("Refusing to run over the script root. Pass one cassette folder, e.g. raw_recordings/example_tape.")
    if SCRIPT_ROOT not in ROOT.parents:
        raise SystemExit(f"Refusing to run outside the cassette root: {ROOT}")
    if not DVRESCUE.exists():
        raise SystemExit(f"DVRescue.exe was not found at {DVRESCUE}")


def require_inside_target(path: Path) -> Path:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"Refusing to touch path outside target folder: {resolved}")
    return resolved


def remove_tree_inside_target(path: Path) -> None:
    resolved = require_inside_target(path)
    if resolved.exists():
        def clear_readonly_and_retry(func: Any, failed_path: str, exc_info: Any) -> None:
            try:
                os.chmod(failed_path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            except OSError:
                pass
            func(failed_path)

        try:
            shutil.rmtree(resolved, onexc=clear_readonly_and_retry)
        except TypeError:
            shutil.rmtree(resolved, onerror=clear_readonly_and_retry)


def delete_file_inside_target(path: Path) -> None:
    resolved = require_inside_target(path)
    if resolved.exists():
        resolved.unlink()


def run_cmd(args: list[str | Path], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(a) for a in args],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "unnamed"


def operating_cassette_id() -> str:
    """Stable public cassette id: the folder being processed, never a source filename."""
    return safe_name(ROOT.name)


def first_xml_document(text: str) -> str:
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<dvrescue")
    if start < 0:
        return text
    return text[start:]


def frame_is_problem(frame: ET.Element) -> bool:
    if any(name in frame.attrib for name in PROBLEM_ATTRS):
        return True
    return any(local_name(child.tag) in PROBLEM_CHILDREN for child in frame)


def frame_has_audio_problem(frame: ET.Element) -> bool:
    return any(name in frame.attrib for name in AUDIO_PROBLEM_ATTRS) or any(
        local_name(child.tag) in AUDIO_PROBLEM_CHILDREN for child in frame
    )


def frame_has_video_problem(frame: ET.Element) -> bool:
    return any(name in frame.attrib for name in VIDEO_PROBLEM_ATTRS) or any(
        local_name(child.tag) in VIDEO_PROBLEM_CHILDREN for child in frame
    )


def problem_frame_sample(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return frames[:10] + frames[-10:] if len(frames) > 20 else frames


def problem_frame_ranges(frames: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Compact problem frame numbers into report-friendly inclusive ranges."""
    numbers = sorted({int(item["n"]) for item in frames if int_or_none(item.get("n")) is not None})
    ranges: list[dict[str, int]] = []
    start: int | None = None
    previous: int | None = None
    for number in numbers:
        if start is None:
            start = previous = number
            continue
        assert previous is not None
        if number == previous + 1:
            previous = number
            continue
        ranges.append({"start": start, "end": previous, "frames": previous - start + 1})
        start = previous = number
    if start is not None and previous is not None:
        ranges.append({"start": start, "end": previous, "frames": previous - start + 1})
    return ranges


def report_ranges_to_tuples(ranges: Any) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for item in ranges or []:
        if not isinstance(item, dict):
            continue
        start = int_or_none(item.get("start"))
        end = int_or_none(item.get("end"))
        if start is None or end is None or end < start:
            continue
        result.append((start, end))
    return normalize_ranges(result)


def frame_attributes_for_report(frame: ET.Element | None) -> dict[str, Any]:
    if frame is None:
        return {}
    attrs = frame.attrib
    keys = [
        "n",
        "pts",
        "abst",
        "tc",
        "rdt",
        "rec_start",
        "seqn",
        "abst_r",
        "tc_r",
        "rdt_r",
        "seqn_r",
        "abst_nc",
        "tc_nc",
        "rdt_nc",
        "seqn_nc",
    ]
    result: dict[str, Any] = {}
    for key in keys:
        if key not in attrs:
            continue
        result[key] = int_or_none(attrs[key]) if key in {"n", "abst"} else attrs[key]
    children = sorted({local_name(child.tag) for child in frame})
    if children:
        result["children"] = children
    return result


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def parse_timecode(tc: Any) -> int | None:
    if tc is None:
        return None
    match = re.match(r"^(\d{2}):(\d{2}):(\d{2}):(\d{2})$", str(tc))
    if not match:
        return None
    hours, minutes, seconds, frames = (int(part) for part in match.groups())
    if frames >= DV_FPS:
        return None
    return (((hours * 60 + minutes) * 60 + seconds) * DV_FPS) + frames


def format_timecode_frame(value: int) -> str:
    frames = value % DV_FPS
    total_seconds = value // DV_FPS
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def timecode_frame_part(tc: Any) -> int | None:
    parsed = parse_timecode(tc)
    if parsed is None:
        return None
    return parsed % DV_FPS


def parse_dv_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    return None


def datetime_timecode_frame(rdt: Any, tc: Any) -> int | None:
    parsed_rdt = parse_dv_datetime(rdt)
    frame_part = timecode_frame_part(tc)
    if parsed_rdt is None or frame_part is None:
        return None
    naive_seconds = (
        parsed_rdt.toordinal() * 24 * 60 * 60
        + parsed_rdt.hour * 60 * 60
        + parsed_rdt.minute * 60
        + parsed_rdt.second
    )
    return naive_seconds * DV_FPS + frame_part


def datetime_from_datetime_timecode_frame(value: int | None) -> dt.datetime | None:
    if value is None:
        return None
    try:
        total_seconds = value // DV_FPS
        ordinal = total_seconds // (24 * 60 * 60)
        second_of_day = total_seconds % (24 * 60 * 60)
        return dt.datetime.fromordinal(ordinal) + dt.timedelta(seconds=second_of_day)
    except (OverflowError, ValueError):
        return None


def clean_range(start: int | None, end: int | None, count: int | None = None) -> tuple[int, int] | None:
    if start is None:
        return None
    if end is None or end < start:
        if count is None or count <= 0:
            return None
        end = start + count - 1
    return start, end


def range_is_plausible_for_count(rng: tuple[int, int], count: int | None) -> bool:
    if count is None or count <= 0:
        return True
    start, end = rng
    actual = end - start + 1
    return abs(actual - count) <= RANGE_COUNT_TOLERANCE_FRAMES


def normalize_ranges(ranges: list[tuple[int, int]], join_gap: int = 0) -> list[tuple[int, int]]:
    cleaned = sorted((min(start, end), max(start, end)) for start, end in ranges)
    merged: list[tuple[int, int]] = []
    for start, end in cleaned:
        if not merged or start > merged[-1][1] + join_gap + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def range_total(ranges: list[tuple[int, int]]) -> int:
    return sum(end - start + 1 for start, end in normalize_ranges(ranges))


def ranges_overlap(left: list[tuple[int, int]], right: list[tuple[int, int]], min_overlap: int = 1) -> bool:
    left_norm = normalize_ranges(left)
    right_norm = normalize_ranges(right)
    i = j = 0
    while i < len(left_norm) and j < len(right_norm):
        a_start, a_end = left_norm[i]
        b_start, b_end = right_norm[j]
        overlap = min(a_end, b_end) - max(a_start, b_start) + 1
        if overlap >= min_overlap:
            return True
        if a_end < b_end:
            i += 1
        else:
            j += 1
    return False


def ranges_cover(covering: list[tuple[int, int]], required: list[tuple[int, int]]) -> tuple[bool, list[tuple[int, int]]]:
    cover = normalize_ranges(covering)
    missing: list[tuple[int, int]] = []
    for req_start, req_end in normalize_ranges(required):
        cursor = req_start
        for cov_start, cov_end in cover:
            if cov_end < cursor:
                continue
            if cov_start > req_end:
                break
            if cov_start > cursor:
                missing.append((cursor, min(cov_start - 1, req_end)))
            cursor = max(cursor, cov_end + 1)
            if cursor > req_end:
                break
        if cursor <= req_end:
            missing.append((cursor, req_end))
    return not missing, missing


def subtract_ranges(required: list[tuple[int, int]], covering: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return ranges_cover(covering, required)[1]


def intersection_total(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> int:
    total = 0
    left_norm = normalize_ranges(left)
    right_norm = normalize_ranges(right)
    i = j = 0
    while i < len(left_norm) and j < len(right_norm):
        a_start, a_end = left_norm[i]
        b_start, b_end = right_norm[j]
        total += max(0, min(a_end, b_end) - max(a_start, b_start) + 1)
        if a_end < b_end:
            i += 1
        else:
            j += 1
    return total


def micro_gap_frame_limit(seconds: float) -> int:
    if seconds <= 0:
        return 0
    return max(0, int(math.floor(seconds * DV_FPS)))


def ranges_have_micro_gap(
    left: list[tuple[int, int]],
    right: list[tuple[int, int]],
    max_gap_frames: int,
) -> bool:
    if max_gap_frames < 0:
        return False
    for left_start, left_end in normalize_ranges(left):
        for right_start, right_end in normalize_ranges(right):
            if right_start > left_end:
                gap = right_start - left_end - 1
            elif left_start > right_end:
                gap = left_start - right_end - 1
            else:
                continue
            if 0 <= gap <= max_gap_frames:
                return True
    return False


def plausible_single_range_for_stitch(
    source: SourceInfo,
    basis: str,
    max_gap_frames: int,
) -> tuple[int, int] | None:
    ranges = source_ranges(source, basis)
    if len(ranges) != 1:
        return None
    start, end = ranges[0]
    frame_count = source.frame_count
    if frame_count <= 0:
        return None
    range_frames = end - start + 1
    tolerance = max(2, max_gap_frames)
    if abs(range_frames - frame_count) > tolerance:
        return None
    return start, end


def sources_have_micro_gap(
    left: SourceInfo,
    right: SourceInfo,
    max_gap_frames: int,
) -> bool:
    return bool(source_stitch_adjacency(left, right, max_gap_frames).get("ok"))


def source_abst_step(source: SourceInfo) -> int | None:
    meta = source_meta(source)
    first_abst = int_or_none(meta.get("first_abst"))
    last_abst = int_or_none(meta.get("last_abst"))
    frames = int_or_none(meta.get("frames_count")) or source.frame_count
    if first_abst is None or last_abst is None or frames is None or frames <= 1:
        return None
    delta = last_abst - first_abst
    if delta <= 0:
        return None
    if delta % (frames - 1) != 0:
        return None
    step = delta // (frames - 1)
    if step <= 0 or step > 100:
        return None
    return step


def source_abst_ranges(source: SourceInfo) -> list[tuple[int, int]]:
    meta = source_meta(source)
    first_abst = int_or_none(meta.get("first_abst"))
    last_abst = int_or_none(meta.get("last_abst"))
    frames = int_or_none(meta.get("frames_count")) or source.frame_count
    if first_abst is None or last_abst is None or frames is None or frames <= 0:
        return []
    if first_abst < 0 or last_abst < first_abst:
        return []
    if frames > 1 and source_abst_step(source) is None:
        expected = (frames - 1) * DV_ABST_STEP_PAL
        tolerance = max(DV_ABST_STEP_PAL * DV_FPS * 2, int(expected * 0.05))
        if abs((last_abst - first_abst) - expected) > tolerance:
            return []
    return [(first_abst, last_abst)]


def source_recording_datetime_window(source: SourceInfo) -> tuple[dt.datetime, dt.datetime] | None:
    meta = source_meta(source)
    start = parse_dv_datetime(meta.get("first_rdt"))
    end = parse_dv_datetime(meta.get("last_rdt"))
    if start is None or end is None:
        return None
    tolerance = dt.timedelta(seconds=EMBEDDED_DATETIME_TOLERANCE_SECONDS)
    if end + tolerance < start:
        return None
    if end < start:
        end = start
    # Robustness: a single corrupt rdt frame can blow the window open by days
    # (observed: a 209-frame micro-capture reporting a 293-day span). If the
    # window is grossly larger than the frame count can justify, the recording
    # datetime is unreliable for identity/coverage; refuse to use it. A 1-hour
    # slack beyond the nominal duration keeps normal variance safe.
    frames = int_or_none(meta.get("frames_count")) or source.frame_count
    if frames and frames > 0:
        nominal_seconds = frames / DV_FPS
        if (end - start).total_seconds() > nominal_seconds + 3600.0:
            return None
    return start, end


def datetime_windows_overlap(
    left: tuple[dt.datetime, dt.datetime],
    right: tuple[dt.datetime, dt.datetime],
    tolerance_seconds: float = EMBEDDED_DATETIME_TOLERANCE_SECONDS,
) -> bool:
    latest_start = max(left[0], right[0])
    earliest_end = min(left[1], right[1])
    return (latest_start - earliest_end).total_seconds() <= tolerance_seconds


def sources_have_recording_datetime_overlap(left: SourceInfo, right: SourceInfo) -> bool:
    left_window = source_recording_datetime_window(left)
    right_window = source_recording_datetime_window(right)
    if left_window is None or right_window is None:
        return False
    return datetime_windows_overlap(left_window, right_window)


def sources_have_physical_identity_overlap(left: SourceInfo, right: SourceInfo) -> bool:
    return sources_have_recording_datetime_overlap(left, right) and sources_have_physical_overlap(left, right)


def sources_have_recording_datetime_timecode_overlap(left: SourceInfo, right: SourceInfo) -> bool:
    """True when embedded recording date/time and DV timecode identify overlap.

    This intentionally does not use filenames. DV recording datetime gives the
    session/date context, and DV timecode gives frame position inside that
    context. Either signal alone can repeat; together they are the stable
    identity for duplicate or partial recaptures.
    """
    if not sources_have_recording_datetime_overlap(left, right):
        return False
    left_tc = source_ranges(left, "timecode") or loose_identity_timecode_ranges(left)
    right_tc = source_ranges(right, "timecode") or loose_identity_timecode_ranges(right)
    return bool(left_tc and right_tc and ranges_overlap(left_tc, right_tc))


def sources_have_embedded_identity_overlap(left: SourceInfo, right: SourceInfo) -> bool:
    if ranges_overlap(
        source_ranges(left, "recording_datetime_timecode"),
        source_ranges(right, "recording_datetime_timecode"),
    ):
        return True
    if sources_have_recording_datetime_timecode_overlap(left, right):
        return True
    if sources_have_physical_identity_overlap(left, right):
        return True
    return False


def v2_unplaceable_duplicate_identity_evidence(
    left: SourceInfo,
    right: SourceInfo,
    max_gap_frames: int,
) -> dict[str, Any]:
    """Strict fallback identity proof for v2 sources that cannot be globally placed.

    Normal v2 clustering is anchored on a sane recording datetime timeline. When
    last_rdt/last_tc are corrupt, two duplicate captures can be unplaceable even
    though their first embedded anchors and physical/timecode spans prove they
    are the same tape interval. This fallback joins only overlapping duplicate
    evidence, not adjacent fragments.
    """
    left_unplaceable = source_true_timeline_range(left) is None
    right_unplaceable = source_true_timeline_range(right) is None
    result: dict[str, Any] = {
        "ok": False,
        "left": left.path.name,
        "right": right.path.name,
        "left_unplaceable": left_unplaceable,
        "right_unplaceable": right_unplaceable,
    }
    if not (left_unplaceable or right_unplaceable):
        result["reason"] = "both sources already have sane v2 timeline placement"
        return result

    left_meta = source_meta(left)
    right_meta = source_meta(right)
    left_rdt = parse_dv_datetime(left_meta.get("first_rdt"))
    right_rdt = parse_dv_datetime(right_meta.get("first_rdt"))
    left_tc = parse_timecode(left_meta.get("first_tc"))
    right_tc = parse_timecode(right_meta.get("first_tc"))
    if left_rdt is None or right_rdt is None or left_tc is None or right_tc is None:
        result["reason"] = "missing first_rdt or first_tc anchor"
        return result

    rdt_delta_seconds = (right_rdt - left_rdt).total_seconds()
    rdt_delta_frames = int(round(rdt_delta_seconds * DV_FPS))
    tc_delta_frames = right_tc - left_tc
    alignment_delta_frames = abs(rdt_delta_frames - tc_delta_frames)
    alignment_tolerance = max(V2_UNPLACEABLE_START_ALIGNMENT_TOLERANCE_FRAMES, max_gap_frames)
    start_alignment_ok = alignment_delta_frames <= alignment_tolerance

    abst_step = source_abst_step(left) or source_abst_step(right) or DV_ABST_STEP_PAL
    left_abst = int_or_none(left_meta.get("first_abst"))
    right_abst = int_or_none(right_meta.get("first_abst"))
    first_abst_delta_units = (
        abs(right_abst - left_abst)
        if left_abst is not None and right_abst is not None
        else None
    )
    first_abst_delta_frames = (
        first_abst_delta_units / abst_step
        if first_abst_delta_units is not None and abst_step > 0
        else None
    )
    first_abst_ok = (
        True
        if first_abst_delta_frames is None
        else first_abst_delta_frames <= max(ABST_SEAM_TOLERANCE_FRAMES, alignment_tolerance)
    )
    same_start_anchor = (
        abs(rdt_delta_seconds) <= EMBEDDED_DATETIME_TOLERANCE_SECONDS
        and abs(tc_delta_frames) <= alignment_tolerance
        and first_abst_ok
    )

    strict_rdt_tc_overlap = ranges_overlap(
        source_ranges(left, "recording_datetime_timecode"),
        source_ranges(right, "recording_datetime_timecode"),
    )
    strict_tc_overlap = ranges_overlap(source_ranges(left, "timecode"), source_ranges(right, "timecode"))
    loose_tc_overlap = ranges_overlap(loose_identity_timecode_ranges(left), loose_identity_timecode_ranges(right))
    recording_datetime_overlap = sources_have_recording_datetime_overlap(left, right)
    physical_overlap = sources_have_physical_overlap(left, right)
    overlap_ok = strict_rdt_tc_overlap or loose_tc_overlap
    contextual_ok = recording_datetime_overlap or same_start_anchor
    physical_or_anchor_ok = physical_overlap or same_start_anchor or strict_rdt_tc_overlap

    result.update(
        {
            "recording_datetime_delta_seconds": rdt_delta_seconds,
            "recording_datetime_delta_frames": rdt_delta_frames,
            "timecode_delta_frames": tc_delta_frames,
            "start_alignment_delta_frames": alignment_delta_frames,
            "start_alignment_tolerance_frames": alignment_tolerance,
            "start_alignment_ok": start_alignment_ok,
            "same_start_anchor": same_start_anchor,
            "first_abst_delta_frames": first_abst_delta_frames,
            "recording_datetime_overlap": recording_datetime_overlap,
            "strict_recording_datetime_timecode_overlap": strict_rdt_tc_overlap,
            "strict_timecode_overlap": strict_tc_overlap,
            "loose_identity_timecode_overlap": loose_tc_overlap,
            "physical_identity_overlap": physical_overlap,
        }
    )

    if start_alignment_ok and overlap_ok and contextual_ok and physical_or_anchor_ok:
        result["ok"] = True
        result["reason"] = (
            "unplaceable v2 source grouped by embedded first_rdt/first_tc alignment "
            "plus overlapping same-interval DV evidence"
        )
    else:
        reasons: list[str] = []
        if not start_alignment_ok:
            reasons.append("first_rdt and first_tc deltas do not describe the same timeline")
        if not overlap_ok:
            reasons.append("no overlapping timecode/recording_datetime_timecode identity span")
        if not contextual_ok:
            reasons.append("no recording datetime context overlap or same-start anchor")
        if not physical_or_anchor_ok:
            reasons.append("no physical overlap, same-start anchor, or strict rdt/timecode overlap")
        result["reason"] = "; ".join(reasons) or "insufficient fallback identity evidence"
    return result


def sources_have_embedded_stitch_adjacency(
    left: SourceInfo,
    right: SourceInfo,
    max_gap_frames: int,
) -> bool:
    return (
        source_stitch_adjacency(left, right, max_gap_frames).get("ok")
        or source_stitch_adjacency(right, left, max_gap_frames).get("ok")
    )


def sources_have_physical_overlap(left: SourceInfo, right: SourceInfo) -> bool:
    left_abst = source_abst_ranges(left)
    right_abst = source_abst_ranges(right)
    if not left_abst or not right_abst or not ranges_overlap(left_abst, right_abst):
        return False
    left_tc = source_ranges(left, "timecode")
    right_tc = source_ranges(right, "timecode")
    if not left_tc or not right_tc:
        return True
    return ranges_overlap(left_tc, right_tc)


def source_starts_recording(source: SourceInfo) -> bool:
    frame = source_meta(source).get("first_frame") or {}
    return str(frame.get("rec_start") or "") == "1"


def source_rdt_span_sanity(source: SourceInfo) -> dict[str, Any]:
    meta = source_meta(source)
    first_rdt = parse_dv_datetime(meta.get("first_rdt"))
    last_rdt = parse_dv_datetime(meta.get("last_rdt"))
    frames = int_or_none(meta.get("frames_count")) or source.frame_count
    result: dict[str, Any] = {
        "source": source.path.name,
        "first_rdt": meta.get("first_rdt"),
        "last_rdt": meta.get("last_rdt"),
        "frames": frames,
        "ok": False,
    }
    if first_rdt is None or last_rdt is None or frames is None or frames <= 0:
        result["reason"] = "missing recording datetime or frame count"
        return result
    actual_seconds = (last_rdt - first_rdt).total_seconds()
    expected_seconds = max(0.0, (frames - 1) / DV_FPS)
    delta_seconds = abs(actual_seconds - expected_seconds)
    tolerance_seconds = 2.0
    result.update(
        {
            "actual_seconds": actual_seconds,
            "expected_seconds": expected_seconds,
            "delta_seconds": delta_seconds,
            "tolerance_seconds": tolerance_seconds,
            "ok": actual_seconds >= -tolerance_seconds and delta_seconds <= tolerance_seconds,
        }
    )
    if not result["ok"]:
        result["reason"] = "recording datetime span is inconsistent with frame count"
    return result


def source_stable_timeline_anchor(source: SourceInfo) -> dict[str, Any]:
    """Find the v2 global recording-time anchor, skipping tiny stale lead-ins.

    DV capture starts can preserve one or a few stale frames from the previous
    recording. Those frames are still real source material, so this only changes
    the timeline anchor used for clustering; it does not discard frames.
    """
    meta = source_meta(source)
    frame_count = int_or_none(meta.get("frames_count")) or source.frame_count
    original_start = datetime_timecode_frame(meta.get("first_rdt"), meta.get("first_tc"))
    span_sanity = source_rdt_span_sanity(source)
    result: dict[str, Any] = {
        "ok": False,
        "source": source.path.name,
        "frame_count": frame_count,
        "first_rdt": meta.get("first_rdt"),
        "first_tc": meta.get("first_tc"),
        "last_rdt": meta.get("last_rdt"),
        "last_tc": meta.get("last_tc"),
        "first_frame_span_sanity": span_sanity,
        "method": None,
        "timeline_start": None,
        "timeline_end": None,
        "leading_frames_before_anchor": None,
        "anchor_frame": None,
        "candidates": [],
    }
    if frame_count is None or frame_count <= 0:
        result["reason"] = "missing or invalid frame count"
        return result
    if original_start is not None and span_sanity.get("ok"):
        result.update(
            {
                "ok": True,
                "method": "first_frame",
                "reason": "absolute first frame has a sane recording datetime span",
                "timeline_start": original_start,
                "timeline_end": original_start + frame_count - 1,
                "leading_frames_before_anchor": 0,
                "anchor_frame": {
                    "n": 0,
                    "rdt": meta.get("first_rdt"),
                    "tc": meta.get("first_tc"),
                    "abst": meta.get("first_abst"),
                },
            }
        )
        return result

    last_anchor = datetime_timecode_frame(meta.get("last_rdt"), meta.get("last_tc"))
    first_abst = int_or_none(meta.get("first_abst"))
    abst_step = source_abst_step(source) or DV_ABST_STEP_PAL
    abst_tolerance_frames = max(ABST_SEAM_TOLERANCE_FRAMES, V2_STABLE_RDT_ANCHOR_TOLERANCE_FRAMES)
    max_n = min(frame_count - 1, V2_STABLE_RDT_ANCHOR_LOOKAHEAD_FRAMES)
    seen_n: set[int] = set()
    samples = meta.get("leading_frame_samples") or []
    for frame in samples:
        n = int_or_none(frame.get("n"))
        if n is None or n < 0 or n > max_n or n in seen_n:
            continue
        seen_n.add(n)
        anchor = datetime_timecode_frame(frame.get("rdt"), frame.get("tc"))
        candidate: dict[str, Any] = {
            "n": n,
            "rdt": frame.get("rdt"),
            "tc": frame.get("tc"),
            "abst": frame.get("abst"),
            "timeline_start": None,
            "timeline_end": None,
            "last_delta_frames": None,
            "first_abst_delta_frames": None,
            "ok": False,
            "issues": [],
        }
        if anchor is None:
            candidate["issues"].append("missing candidate rdt/tc anchor")
            result["candidates"].append(candidate)
            continue
        if n == 0:
            candidate["issues"].append("absolute first frame was already proven span-inconsistent")
        timeline_start = anchor - n
        timeline_end = timeline_start + frame_count - 1
        candidate["timeline_start"] = timeline_start
        candidate["timeline_end"] = timeline_end
        if last_anchor is None:
            candidate["issues"].append("missing final rdt/tc anchor for validation")
        else:
            last_delta = abs(last_anchor - timeline_end)
            candidate["last_delta_frames"] = last_delta
            if last_delta > V2_STABLE_RDT_ANCHOR_TOLERANCE_FRAMES:
                candidate["issues"].append(
                    f"candidate end differs from final rdt/tc by {last_delta} frame(s)"
                )

        frame_abst = int_or_none(frame.get("abst"))
        if first_abst is not None and frame_abst is not None and abst_step > 0:
            candidate_first_abst = frame_abst - n * abst_step
            abst_delta_frames = abs(candidate_first_abst - first_abst) / abst_step
            candidate["first_abst_delta_frames"] = abst_delta_frames
            if abst_delta_frames > abst_tolerance_frames:
                candidate["issues"].append(
                    f"candidate absolute-track back-projection differs by {abst_delta_frames:.3f} frame(s)"
                )

        if not candidate["issues"]:
            candidate["ok"] = True
            result["candidates"].append(candidate)
            result.update(
                {
                    "ok": True,
                    "method": "stable_leading_frame",
                    "reason": (
                        "absolute first frame has stale/inconsistent recording datetime; "
                        "a later leading frame gives a sane anchor and back-projects over the lead-in"
                    ),
                    "timeline_start": timeline_start,
                    "timeline_end": timeline_end,
                    "leading_frames_before_anchor": n,
                    "anchor_frame": {
                        "n": n,
                        "rdt": frame.get("rdt"),
                        "tc": frame.get("tc"),
                        "abst": frame.get("abst"),
                        "children": frame.get("children"),
                    },
                }
            )
            return result
        result["candidates"].append(candidate)

    if original_start is None:
        result["reason"] = "missing embedded first_rdt or first_tc frame position"
    else:
        result["reason"] = "recording datetime span is inconsistent and no stable leading anchor proved safe"
    return result


def source_stitch_adjacency(left: SourceInfo, right: SourceInfo, max_gap_frames: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "left": left.path.name,
        "right": right.path.name,
        "max_gap_frames": max_gap_frames,
    }
    if max_gap_frames < 0:
        result["reason"] = "micro-gap stitching is disabled"
        return result
    if source_starts_recording(right):
        result["reason"] = "right source starts a camera recording"
        return result

    left_tc = plausible_single_range_for_stitch(left, "timecode", max_gap_frames)
    right_tc = plausible_single_range_for_stitch(right, "timecode", max_gap_frames)
    if left_tc is None or right_tc is None:
        result["reason"] = "timecode ranges are not plausible single-fragment ranges"
        return result
    if right_tc[0] <= left_tc[1]:
        result["reason"] = "timecode ranges overlap or are out of order"
        return result
    gap_frames = right_tc[0] - left_tc[1] - 1
    if gap_frames < 0 or gap_frames > max_gap_frames:
        result["reason"] = f"timecode gap is {gap_frames} frames"
        return result

    left_meta = source_meta(left)
    right_meta = source_meta(right)
    left_last_abst = int_or_none(left_meta.get("last_abst"))
    right_first_abst = int_or_none(right_meta.get("first_abst"))
    abst_step = source_abst_step(left) or source_abst_step(right) or DV_ABST_STEP_PAL
    abst_exact = False
    abst_within_tol = False
    expected_right_abst = None
    abst_delta = None
    # abst_gap_frames is the PHYSICAL gap implied by the absolute track. When
    # timecode and abst disagree at a seam (real capture splits show ~1-2 frame
    # slop), abst is the truth, so the synthetic-hold count is derived from it.
    abst_gap_frames = None
    abst_tol_units = ABST_SEAM_TOLERANCE_FRAMES * abst_step
    if left_last_abst is not None and right_first_abst is not None:
        expected_right_abst = left_last_abst + abst_step * (gap_frames + 1)
        abst_delta = right_first_abst - expected_right_abst
        abst_exact = abst_delta == 0
        abst_within_tol = abs(abst_delta) <= abst_tol_units
        # physical gap = advance beyond one contiguous step, in frames, >= 0
        abst_gap_frames = max(0, round((right_first_abst - left_last_abst) / abst_step) - 1)

    left_rdt_sanity = source_rdt_span_sanity(left)
    right_rdt_sanity = source_rdt_span_sanity(right)
    result["recording_datetime_span_sanity"] = {
        "left": left_rdt_sanity,
        "right": right_rdt_sanity,
    }
    left_start_dt = parse_dv_datetime(left_meta.get("first_rdt"))
    right_start_dt = parse_dv_datetime(right_meta.get("first_rdt"))
    datetime_delta = None
    datetime_ok = False
    if left_rdt_sanity.get("ok") and right_rdt_sanity.get("ok") and left_start_dt is not None and right_start_dt is not None:
        expected_right_start = left_start_dt + dt.timedelta(seconds=(left.frame_count + gap_frames) / DV_FPS)
        datetime_delta = abs((right_start_dt - expected_right_start).total_seconds())
        datetime_ok = datetime_delta <= 2.0
        if not datetime_ok:
            result.update(
                {
                    "reason": f"recording datetime gap is {datetime_delta:.3f}s from expected continuity",
                    "timecode_gap_frames": gap_frames,
                    "expected_right_recording_datetime": expected_right_start.isoformat(timespec="seconds"),
                    "actual_right_recording_datetime": right_start_dt.isoformat(timespec="seconds"),
                    "recording_datetime_delta_seconds": datetime_delta,
                }
            )
            return result

    score = 0
    reasons: list[str] = []
    if gap_frames <= max_gap_frames:
        score += 3
        reasons.append(f"timecode gap {gap_frames} frame(s)")
    if abst_exact:
        score += 5
        reasons.append("absolute-track continuity is exact")
    elif abst_within_tol:
        score += 4
        reasons.append(f"absolute-track continuity within {ABST_SEAM_TOLERANCE_FRAMES} frame(s) (delta {abst_delta})")
    elif left_last_abst is not None and right_first_abst is not None:
        reasons.append(f"absolute-track continuity mismatch {abst_delta}")
    if datetime_ok:
        score += 1
        reasons.append("recording datetime is compatible")

    # Acceptance requires real physical adjacency evidence: the absolute track
    # must line up within a tight tolerance (a seam may legitimately disagree
    # with timecode by 1-2 frames). This is gated together with the earlier
    # checks already passed above: no rec_start on the right, a small timecode
    # gap, and (when evaluable) compatible recording datetime. Four independent
    # signals must agree, so a relaxed abst tolerance cannot fuse unrelated
    # content.
    accepted = abst_within_tol and gap_frames <= max_gap_frames
    # The gap used for synthetic hold frames: trust abst over timecode at seams.
    effective_gap_frames = abst_gap_frames if abst_gap_frames is not None else gap_frames
    result.update(
        {
            "ok": accepted,
            "score": score,
            "reasons": reasons,
            "reason": "accepted" if accepted else "insufficient physical adjacency evidence",
            "basis": "timecode",
            "gap_frames": gap_frames,
            "gap_seconds": gap_frames / DV_FPS,
            "effective_gap_frames": effective_gap_frames,
            "missing_timecodes": [
                format_timecode_frame(frame)
                for frame in range(left_tc[1] + 1, right_tc[0])
            ],
            "left_timecode_range": {"start": left_tc[0], "end": left_tc[1]},
            "right_timecode_range": {"start": right_tc[0], "end": right_tc[1]},
            "absolute_track": {
                "step": abst_step,
                "left_last_abst": left_last_abst,
                "right_first_abst": right_first_abst,
                "expected_right_first_abst": expected_right_abst,
                "delta": abst_delta,
                "exact": abst_exact,
                "within_tolerance": abst_within_tol,
                "tolerance_frames": ABST_SEAM_TOLERANCE_FRAMES,
                "abst_gap_frames": abst_gap_frames,
            },
            "recording_datetime": {
                "delta_seconds": datetime_delta,
                "compatible": datetime_ok,
            },
            "right_rec_start": source_starts_recording(right),
        }
    )
    return result


def partition_sources_into_stitch_runs(
    sources: list[SourceInfo],
    max_gap_frames: int,
) -> list[list[SourceInfo]]:
    """Group sources into runs of frame-adjacent fragments for safe stitching.

    Sources are first ordered by their single-fragment timecode start. Within
    the resulting list, every consecutive pair inside a run passes
    source_stitch_adjacency, so each multi-source run can be stitched on its
    own. Sources without a plausible single-fragment timecode range, and any
    source whose adjacency to its predecessor fails, become their own
    singleton run so the caller can preserve them as separate KEEP files.
    """
    if not sources:
        return []
    orderable: list[tuple[int, SourceInfo]] = []
    unorderable: list[SourceInfo] = []
    for source in sources:
        coverage = plausible_single_range_for_stitch(source, "timecode", max_gap_frames)
        if coverage is None:
            unorderable.append(source)
        else:
            orderable.append((coverage[0], source))
    orderable.sort(key=lambda item: (item[0], item[1].path.name))

    runs: list[list[SourceInfo]] = []
    current: list[SourceInfo] = []
    for _, source in orderable:
        if not current:
            current = [source]
            continue
        if source_stitch_adjacency(current[-1], source, max_gap_frames).get("ok"):
            current.append(source)
        else:
            runs.append(current)
            current = [source]
    if current:
        runs.append(current)
    for source in unorderable:
        runs.append([source])
    return runs


def source_pair_is_stitch_repair_candidate(left: SourceInfo, right: SourceInfo, adjacency: dict[str, Any]) -> bool:
    if not adjacency.get("ok"):
        return False
    span = adjacency.get("recording_datetime_span_sanity") or {}
    left_span = span.get("left") or {}
    right_span = span.get("right") or {}
    rdt_compatible = bool((adjacency.get("recording_datetime") or {}).get("compatible"))
    if rdt_compatible:
        return True
    if not left_span.get("ok") or not right_span.get("ok"):
        return True
    if left.issue_count > 0 or right.issue_count > 0:
        return True
    left_duration = source_duration_seconds(left)
    right_duration = source_duration_seconds(right)
    both_short = (
        left_duration is not None
        and right_duration is not None
        and left_duration <= 3.0
        and right_duration <= 3.0
    )
    return both_short and rdt_compatible


def analyze_dvrescue(path: Path) -> dict[str, Any]:
    # DVRescue can hang on pathologically corrupt micro-captures (e.g.
    # 1-video-frame fragments at tape-section boundaries). Cap at 600s and
    # treat a timeout as "file unanalyzable" so the run can continue. The
    # short-duration policy will then exclude the file from KEEP.
    try:
        proc = run_cmd([DVRESCUE, path, "-v", "9"], timeout=600)
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stderr": "DVRescue analysis exceeded 600s timeout; file treated as unanalyzable",
            "xml_error": "timeout",
            "frames_count": 0,
            "frames_segments": [],
            "first_abst": None,
            "last_abst": None,
            "first_tc": None,
            "last_tc": None,
            "first_rdt": None,
            "last_rdt": None,
            "video_rate": None,
            "audio_rate": None,
            "channels": None,
            "speed_values": [],
            "first_frame": {},
            "last_frame": {},
            "problem_frame_count": 0,
            "problem_frames_sample": [],
            "problem_frame_ranges": [],
            "audio_problem_frame_count": 0,
            "audio_problem_frames_sample": [],
            "audio_problem_frame_ranges": [],
            "video_problem_frame_count": 0,
            "video_problem_frames_sample": [],
            "video_problem_frame_ranges": [],
            "tail_repeated_problem_count": 0,
            "leading_frame_samples": [],
            "dvrescue_timeout": True,
        }
    result: dict[str, Any] = {
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip(),
        "xml_error": None,
        "frames_count": None,
        "frames_segments": [],
        "first_abst": None,
        "last_abst": None,
        "first_tc": None,
        "last_tc": None,
        "first_rdt": None,
        "last_rdt": None,
        "video_rate": None,
        "audio_rate": None,
        "channels": None,
        "speed_values": [],
        "first_frame": {},
        "last_frame": {},
        "problem_frame_count": 0,
        "problem_frames_sample": [],
        "problem_frame_ranges": [],
        "audio_problem_frame_count": 0,
        "audio_problem_frames_sample": [],
        "audio_problem_frame_ranges": [],
        "video_problem_frame_count": 0,
        "video_problem_frames_sample": [],
        "video_problem_frame_ranges": [],
        "tail_repeated_problem_count": 0,
        "leading_frame_samples": [],
    }
    xml_text = first_xml_document(proc.stdout)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        result["xml_error"] = str(exc)
        return result

    frames_elems = [elem for elem in root.iter() if local_name(elem.tag) == "frames"]
    frame_elems = [elem for elem in root.iter() if local_name(elem.tag) == "frame"]
    if frame_elems:
        result["first_frame"] = frame_attributes_for_report(frame_elems[0])
        result["last_frame"] = frame_attributes_for_report(frame_elems[-1])
        leading_by_n: dict[int, dict[str, Any]] = {}
        for frame in frame_elems:
            attrs = frame_attributes_for_report(frame)
            n = int_or_none(attrs.get("n"))
            if n is None or n < 0 or n > V2_STABLE_RDT_ANCHOR_LOOKAHEAD_FRAMES:
                continue
            leading_by_n.setdefault(n, attrs)
        result["leading_frame_samples"] = [
            leading_by_n[n]
            for n in sorted(leading_by_n)
        ]

    count_sum = 0
    segments: list[dict[str, Any]] = []
    speed_values: set[str] = set()
    for elem in frames_elems:
        count = int_or_none(elem.attrib.get("count"))
        if count is not None:
            count_sum += count
        child_frames = [child for child in elem if local_name(child.tag) == "frame"]
        first_frame = child_frames[0] if child_frames else None
        last_frame = child_frames[-1] if child_frames else None
        first_tc = first_frame.attrib.get("tc") if first_frame is not None else None
        last_tc = last_frame.attrib.get("tc") if last_frame is not None else None
        first_rdt = first_frame.attrib.get("rdt") if first_frame is not None else None
        last_rdt = last_frame.attrib.get("rdt") if last_frame is not None else None
        first_tc_frame = parse_timecode(first_tc)
        last_tc_frame = parse_timecode(last_tc)
        first_rdt_tc_frame = datetime_timecode_frame(first_rdt, first_tc)
        last_rdt_tc_frame = datetime_timecode_frame(last_rdt, last_tc)
        segment = {
            "count": count,
            "pts": elem.attrib.get("pts"),
            "end_pts": elem.attrib.get("end_pts"),
            "abst": int_or_none(elem.attrib.get("abst")),
            "end_abst": int_or_none(elem.attrib.get("end_abst")),
            "first_n": int_or_none(first_frame.attrib.get("n")) if first_frame is not None else None,
            "last_n": int_or_none(last_frame.attrib.get("n")) if last_frame is not None else None,
            "first_tc": first_tc,
            "last_tc": last_tc,
            "first_tc_frame": first_tc_frame,
            "last_tc_frame": last_tc_frame,
            "first_rdt": first_rdt,
            "last_rdt": last_rdt,
            "first_rdt_tc_frame": first_rdt_tc_frame,
            "last_rdt_tc_frame": last_rdt_tc_frame,
            "video_rate": elem.attrib.get("video_rate"),
            "audio_rate": elem.attrib.get("audio_rate"),
            "channels": elem.attrib.get("channels"),
            "speed": elem.attrib.get("speed"),
        }
        if segment["speed"] is not None:
            speed_values.add(str(segment["speed"]))
        segments.append(segment)

    if count_sum:
        result["frames_count"] = count_sum
    result["frames_segments"] = segments
    result["speed_values"] = sorted(speed_values)

    if segments:
        first = segments[0]
        last = segments[-1]
        result["first_abst"] = first.get("abst")
        result["last_abst"] = last.get("end_abst")
        result["first_tc"] = first.get("first_tc")
        result["last_tc"] = last.get("last_tc")
        result["first_rdt"] = first.get("first_rdt")
        result["last_rdt"] = last.get("last_rdt")
        result["video_rate"] = first.get("video_rate")
        result["audio_rate"] = first.get("audio_rate")
        result["channels"] = first.get("channels")

    if frame_elems and not result["first_tc"]:
        first_frame = frame_elems[0]
        last_frame = frame_elems[-1]
        result["first_tc"] = first_frame.attrib.get("tc")
        result["last_tc"] = last_frame.attrib.get("tc")
        result["first_rdt"] = first_frame.attrib.get("rdt")
        result["last_rdt"] = last_frame.attrib.get("rdt")

    problem_frames: list[dict[str, Any]] = []
    audio_problem_frames: list[dict[str, Any]] = []
    video_problem_frames: list[dict[str, Any]] = []
    for frame in frame_elems:
        if not frame_is_problem(frame):
            continue
        attrs = frame.attrib
        frame_info = {
            "n": int_or_none(attrs.get("n")),
            "pts": attrs.get("pts"),
            "abst": int_or_none(attrs.get("abst")),
            "tc": attrs.get("tc"),
            "rdt": attrs.get("rdt"),
            "flags": sorted(name for name in attrs if name in PROBLEM_ATTRS),
            "children": sorted({local_name(child.tag) for child in frame}),
        }
        problem_frames.append(frame_info)
        if frame_has_audio_problem(frame):
            audio_problem_frames.append(frame_info)
        if frame_has_video_problem(frame):
            video_problem_frames.append(frame_info)

    result["problem_frame_count"] = len(problem_frames)
    result["problem_frames_sample"] = problem_frame_sample(problem_frames)
    result["problem_frame_ranges"] = problem_frame_ranges(problem_frames)
    result["audio_problem_frame_count"] = len(audio_problem_frames)
    result["audio_problem_frames_sample"] = problem_frame_sample(audio_problem_frames)
    result["audio_problem_frame_ranges"] = problem_frame_ranges(audio_problem_frames)
    result["video_problem_frame_count"] = len(video_problem_frames)
    result["video_problem_frames_sample"] = problem_frame_sample(video_problem_frames)
    result["video_problem_frame_ranges"] = problem_frame_ranges(video_problem_frames)

    total_frames = result["frames_count"]
    if total_frames:
        by_n = {item["n"]: item for item in problem_frames if item.get("n") is not None}
        tail_count = 0
        n = int(total_frames) - 1
        while n in by_n:
            item = by_n[n]
            flags = set(item.get("flags") or [])
            if not (flags & {"abst_r", "tc_r", "rdt_r", "seqn_r", "full_conceal_aud", "full_conceal_vid"}):
                break
            tail_count += 1
            n -= 1
        result["tail_repeated_problem_count"] = tail_count

    return result


# abst first: it is the physical tape position and is consistent across every
# capture of the same recording, so within a cluster it is the most reliable
# coverage coordinate (and is present even when recording datetime is corrupt).
# It is only ever evaluated within an already-formed (rdt-anchored) cluster, so
# its global looping/reset behaviour cannot cause cross-recording confusion.
COVERAGE_BASIS_ORDER = ["abst", "recording_datetime_timecode", "timecode"]


def abst_step_from_segment(segment: dict[str, Any]) -> int:
    """Best estimate of abst units per frame for one DVRescue segment."""
    first = int_or_none(segment.get("abst"))
    last = int_or_none(segment.get("end_abst"))
    count = int_or_none(segment.get("count"))
    if first is not None and last is not None and count and count > 1 and last >= first:
        span = last - first
        if span % (count - 1) == 0:
            step = span // (count - 1)
            if 0 < step <= 100:
                return step
    return DV_ABST_STEP_PAL


def coverage_ranges_from_meta(meta: dict[str, Any], basis: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for segment in meta.get("frames_segments") or []:
        count = int_or_none(segment.get("count"))
        if basis == "abst":
            first_abst = int_or_none(segment.get("abst"))
            last_abst = int_or_none(segment.get("end_abst"))
            if first_abst is None or last_abst is None or first_abst < 0 or last_abst < first_abst:
                item = None
            else:
                # Convert physical abst units to frame-grained indices anchored
                # to the tape's abst origin (shared across captures of the same
                # recording), so ranges from different captures are comparable.
                step = abst_step_from_segment(segment)
                item = clean_range(first_abst // step, last_abst // step, count)
        elif basis == "recording_datetime_timecode":
            start = int_or_none(segment.get("first_rdt_tc_frame"))
            end = int_or_none(segment.get("last_rdt_tc_frame"))
            item = clean_range(start, end, count)
            if item is not None and not range_is_plausible_for_count(item, count):
                # If DVRescue gives both embedded boundaries and their span is
                # impossible for the segment length, the boundary metadata is
                # not safe as coverage evidence. Do not invent preservation
                # coverage from a contradictory recording-date/timecode pair.
                item = None
        elif basis == "timecode":
            item = clean_range(
                int_or_none(segment.get("first_tc_frame")),
                int_or_none(segment.get("last_tc_frame")),
                count,
            )
        else:
            item = None
        if item is not None:
            if range_is_plausible_for_count(item, count):
                ranges.append(item)
    return normalize_ranges(ranges)


def recording_datetime_count_ranges_from_meta(meta: dict[str, Any]) -> list[tuple[int, int]]:
    """Canonical v2 range: first embedded recording timestamp plus frame count.

    Damaged DV tails often keep a valid first_rdt/first_tc and a correct raw
    frame count, while last_rdt/last_tc/abst stop early. v2 may use this only
    inside an already recording-time-clustered group; it is not a global
    duplicate/delete-safety coverage basis.
    """
    ranges: list[tuple[int, int]] = []
    for segment in meta.get("frames_segments") or []:
        count = int_or_none(segment.get("count"))
        if count is None or count <= 0:
            continue
        start = int_or_none(segment.get("first_rdt_tc_frame"))
        if start is None:
            start = datetime_timecode_frame(segment.get("first_rdt"), segment.get("first_tc"))
        if start is None:
            continue
        ranges.append((start, start + count - 1))
    if ranges:
        return normalize_ranges(ranges)

    count = int_or_none(meta.get("frames_count"))
    start = datetime_timecode_frame(meta.get("first_rdt"), meta.get("first_tc"))
    if count is None or count <= 0 or start is None:
        return []
    return [(start, start + count - 1)]


def v2_validation_ranges_from_meta(meta: dict[str, Any], basis: str) -> list[tuple[int, int]]:
    ranges = coverage_ranges_from_meta(meta, basis)
    if basis == "recording_datetime_timecode":
        return recording_datetime_count_ranges_from_meta(meta) or ranges
    if basis == "timecode":
        return timecode_count_ranges_from_meta(meta) or ranges
    return ranges


def meta_has_coverage(meta: dict[str, Any], basis: str) -> bool:
    return bool(coverage_ranges_from_meta(meta, basis))


def source_meta(source: SourceInfo) -> dict[str, Any]:
    return source.raw_dvrescue or source.dvrescue


def source_ranges(source: SourceInfo, basis: str) -> list[tuple[int, int]]:
    return coverage_ranges_from_meta(source_meta(source), basis)


def loose_identity_timecode_ranges(source: SourceInfo) -> list[tuple[int, int]]:
    """Timecode bounds for identity only, never for deletion coverage.

    Corrupted DV files can have first/last timecode spans that disagree with
    frame count by a handful of frames. That is not precise enough to prove
    source coverage, but when combined with overlapping embedded recording
    datetime it is useful evidence that two captures depict the same tape
    material.
    """
    ranges: list[tuple[int, int]] = []
    for segment in source_meta(source).get("frames_segments") or []:
        start = int_or_none(segment.get("first_tc_frame"))
        end = int_or_none(segment.get("last_tc_frame"))
        if start is None or end is None or end < start:
            continue
        ranges.append((start, end))
    if not ranges:
        start = parse_timecode(source_meta(source).get("first_tc"))
        end = parse_timecode(source_meta(source).get("last_tc"))
        if start is not None and end is not None and end >= start:
            ranges.append((start, end))
    return normalize_ranges(ranges)


def bad_frame_ratio_for_meta(meta: dict[str, Any]) -> float | None:
    frames = int_or_none(meta.get("frames_count"))
    problems = int_or_none(meta.get("problem_frame_count"))
    if frames is None or problems is None or frames <= 0:
        return None
    return problems / frames


def source_duration_seconds(source: SourceInfo) -> float | None:
    candidates: list[float] = []
    for value in (
        source.ffprobe.get("stream_duration"),
        source.ffprobe.get("format_duration"),
    ):
        duration = float_or_none(value)
        if duration is not None and duration > 0:
            candidates.append(duration)
    frames = source.frame_count
    if frames > 0:
        candidates.append(frames / DV_FPS)
    return max(candidates) if candidates else None


def bad_frame_policy_for_source(source: SourceInfo) -> dict[str, Any]:
    meta = source.dvrescue
    frames = int_or_none(meta.get("frames_count"))
    problems = int_or_none(meta.get("problem_frame_count"))
    effective_frames = frames if frames is not None else (source.frame_count or None)
    ratio = bad_frame_ratio_for_meta(meta)
    duration = source_duration_seconds(source)
    bad_frame_excluded = ratio is not None and ratio > BAD_FRAME_EXCLUSION_RATIO
    duration_excluded = duration is not None and duration < MIN_KEEP_DURATION_SECONDS
    excluded = bad_frame_excluded or duration_excluded
    reasons: list[str] = []
    if bad_frame_excluded and ratio is not None:
        reasons.append(f"problem frame ratio {ratio:.6f} is greater than {BAD_FRAME_EXCLUSION_RATIO:.2f}")
    if duration_excluded and duration is not None:
        reasons.append(f"duration {duration:.3f}s is shorter than {MIN_KEEP_DURATION_SECONDS:.3f}s")
    return {
        "excluded_from_keep": excluded,
        "threshold": BAD_FRAME_EXCLUSION_RATIO,
        "min_keep_duration_seconds": MIN_KEEP_DURATION_SECONDS,
        "bad_frame_ratio": ratio,
        "duration_seconds": duration,
        "frames": effective_frames,
        "problem_frames": problems,
        "bad_frame_excluded": bad_frame_excluded,
        "duration_excluded": duration_excluded,
        "reason": "; ".join(reasons) if reasons else None,
    }


def source_excluded_from_keep(source: SourceInfo) -> bool:
    return bool(bad_frame_policy_for_source(source)["excluded_from_keep"])


def eligible_keep_sources(sources: list[SourceInfo]) -> list[SourceInfo]:
    return [source for source in sources if not source_excluded_from_keep(source)]


def source_has_trustworthy_cross_name_coverage(source: SourceInfo) -> bool:
    meta = source_meta(source)
    frames = int(meta.get("frames_count") or 0)
    problems = int(meta.get("problem_frame_count") or 0)
    if frames <= 0:
        return False
    if problems >= frames:
        return False
    return any(source_ranges(source, basis) for basis in COVERAGE_BASIS_ORDER)


def choose_coverage_basis(sources: list[SourceInfo]) -> str | None:
    for basis in COVERAGE_BASIS_ORDER:
        if all(source_ranges(source, basis) for source in sources):
            return basis
    return None


def cluster_union_ranges(sources: list[SourceInfo], basis: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for source in sources:
        ranges.extend(source_ranges(source, basis))
    return normalize_ranges(ranges)


def coverage_deficit(
    required: list[tuple[int, int]],
    covering: list[tuple[int, int]],
    tolerance: int = SEAM_TOLERANCE_FRAMES,
) -> list[tuple[int, int]]:
    """Ranges in `required` not covered by `covering`, ignoring tiny seam slivers.

    Frame-grained abst coverage rounds at seams (a frame may index off-by-one
    between two captures), so missing runs of <= tolerance frames are treated as
    covered. This prevents false "loss" reports from rounding while still
    catching any real gap.
    """
    _, missing = ranges_cover(covering, required)
    if tolerance > 0:
        missing = [(s, e) for s, e in missing if (e - s + 1) > tolerance]
    return missing


def ranges_cover_with_tolerance(
    covering: list[tuple[int, int]],
    required: list[tuple[int, int]],
    tolerance: int = SEAM_TOLERANCE_FRAMES,
) -> bool:
    return not coverage_deficit(required, covering, tolerance)


def source_covers_union(source: SourceInfo, basis: str | None, union_ranges: list[tuple[int, int]]) -> bool:
    if basis is None:
        return len(union_ranges) == 0
    return ranges_cover_with_tolerance(source_ranges(source, basis), union_ranges)


def select_coverage_preserving_units(
    units: list[dict[str, Any]],
    union_ranges: list[tuple[int, int]],
    tolerance: int = SEAM_TOLERANCE_FRAMES,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedy minimal cover that never drops an uncovered unit.

    Each unit is {"coverage": ranges, "quality": int, ...}. Returns
    (selected, discarded). A unit is discarded only when the union of selected
    units already covers it (within tolerance); anything not provably covered is
    kept, so coverage is preserved by construction.
    """
    remaining = normalize_ranges(union_ranges)
    selected: list[dict[str, Any]] = []
    pool = list(units)
    # Phase 1: greedily cover the union, preferring biggest contribution then quality.
    while remaining and pool:
        pool.sort(
            key=lambda u: (intersection_total(u.get("coverage") or [], remaining), u.get("quality", 0)),
            reverse=True,
        )
        best = pool.pop(0)
        gain = intersection_total(best.get("coverage") or [], remaining)
        if gain <= 0:
            break
        selected.append(best)
        remaining = subtract_ranges(remaining, best.get("coverage") or [])
    # Phase 2: safety net - keep any remaining unit not fully covered by the selection.
    selected_cov: list[tuple[int, int]] = []
    for u in selected:
        selected_cov.extend(u.get("coverage") or [])
    selected_cov = normalize_ranges(selected_cov)
    discarded: list[dict[str, Any]] = []
    for u in pool:
        if coverage_deficit(u.get("coverage") or [], selected_cov, tolerance):
            selected.append(u)
            selected_cov = normalize_ranges(selected_cov + (u.get("coverage") or []))
        else:
            discarded.append(u)
    return selected, discarded


def compute_coverage_audit(
    active_sources: list[SourceInfo],
    keep_ranges: list[tuple[int, int]],
    basis: str | None,
    tolerance: int = SEAM_TOLERANCE_FRAMES,
) -> dict[str, Any]:
    """Independent completeness proof: does KEEP cover every active source frame?"""
    if basis is None:
        return {
            "basis": None,
            "source_union_frames": 0,
            "keep_union_frames": range_total(normalize_ranges(keep_ranges)),
            "deficit_frames": 0,
            "deficit_seconds": 0.0,
            "deficit_ranges": [],
            "note": "no common coverage basis; completeness not provable on a coordinate",
        }
    source_union = cluster_union_ranges(active_sources, basis)
    keep_norm = normalize_ranges(keep_ranges)
    deficit = coverage_deficit(source_union, keep_norm, tolerance)
    deficit_frames = range_total(deficit)
    return {
        "basis": basis,
        "source_union_frames": range_total(source_union),
        "keep_union_frames": range_total(keep_norm),
        "deficit_frames": deficit_frames,
        "deficit_seconds": deficit_frames / DV_FPS,
        "deficit_ranges": [{"start": s, "end": e, "frames": e - s + 1} for s, e in deficit[:30]],
        "tolerance_frames": tolerance,
    }


def source_physically_supersedes(best: SourceInfo, other: SourceInfo) -> bool:
    """True if `best` is a physical superset of `other` on the tape.

    Requires:
      * Both sources have valid first_abst/last_abst (absolute-track frame
        position on the tape), and best's window fully contains other's.
      * Both sources report identical first_rdt and last_rdt (the embedded
        recording datetime window). Matching rdt windows are what prove the
        two captures are of the same physical recording session, not two
        unrelated recordings that happen to live at similar abst on the same
        cassette (DV timecode can repeat on a single tape).

    Returns False the moment any piece is missing or disagrees, so this is
    safe to call against partially-corrupted DVRescue output.
    """
    best_meta = best.raw_dvrescue or best.dvrescue
    other_meta = other.raw_dvrescue or other.dvrescue
    best_abst0 = int_or_none(best_meta.get("first_abst"))
    best_abst1 = int_or_none(best_meta.get("last_abst"))
    other_abst0 = int_or_none(other_meta.get("first_abst"))
    other_abst1 = int_or_none(other_meta.get("last_abst"))
    if None in (best_abst0, best_abst1, other_abst0, other_abst1):
        return False
    if best_abst0 in (-1,) or best_abst1 in (-1,) or other_abst0 in (-1,) or other_abst1 in (-1,):
        return False
    abst_tolerance = DV_ABST_STEP_PAL
    if not (best_abst0 <= other_abst0 and best_abst1 + abst_tolerance >= other_abst1):
        return False
    best_rdt0 = parse_dv_datetime(best_meta.get("first_rdt"))
    best_rdt1 = parse_dv_datetime(best_meta.get("last_rdt"))
    other_rdt0 = parse_dv_datetime(other_meta.get("first_rdt"))
    other_rdt1 = parse_dv_datetime(other_meta.get("last_rdt"))
    if None in (best_rdt0, best_rdt1, other_rdt0, other_rdt1):
        return False
    assert best_rdt0 is not None and best_rdt1 is not None
    assert other_rdt0 is not None and other_rdt1 is not None
    if best_rdt1 < best_rdt0 or other_rdt1 < other_rdt0:
        return False
    rdt_tolerance = dt.timedelta(seconds=2)
    return best_rdt0 <= other_rdt0 + rdt_tolerance and best_rdt1 + rdt_tolerance >= other_rdt1


def best_source_physically_covers_cluster(
    best: SourceInfo, sources: list[SourceInfo]
) -> bool:
    """True if `best` physically supersedes every other source in the cluster.

    Used when embedded-range coverage rejects `best` because some other source
    happens to have a few isolated "trustworthy" frames in positions where
    `best`'s embedded metadata is corrupted. If `best`'s absolute-track window
    is a strict superset and their recording-datetime windows match exactly,
    the missing trustworthy ranges are inside `best`'s physical content with
    different metadata corruption, not actually missing.
    """
    for other in sources:
        if other is best:
            continue
        if not source_physically_supersedes(best, other):
            return False
    return True


def compact_ranges_for_json(ranges: list[tuple[int, int]], limit: int = 30) -> dict[str, Any]:
    normalized = normalize_ranges(ranges)
    data: dict[str, Any] = {
        "count": len(normalized),
        "total_frames": range_total(normalized),
        "ranges": [{"start": start, "end": end, "frames": end - start + 1} for start, end in normalized[:limit]],
    }
    if len(normalized) > limit:
        data["truncated_range_count"] = len(normalized) - limit
    return data


def expected_boundary_metadata(sources: list[SourceInfo], basis: str | None) -> dict[str, Any]:
    if basis is None:
        return {}
    boundary_rows: list[dict[str, Any]] = []
    for source in sources:
        meta = source_meta(source)
        for segment in meta.get("frames_segments") or []:
            ranges = coverage_ranges_from_meta({"frames_segments": [segment]}, basis)
            if not ranges:
                continue
            start, end = ranges[0]
            boundary_rows.append({"source": source, "segment": segment, "start": start, "end": end})
    if not boundary_rows:
        return {}
    first = min(boundary_rows, key=lambda item: item["start"])
    last = max(boundary_rows, key=lambda item: item["end"])
    first_segment = first["segment"]
    last_segment = last["segment"]
    overrides: dict[str, Any] = {}
    if basis == "recording_datetime_timecode":
        overrides["first_rdt"] = first_segment.get("first_rdt")
        overrides["last_rdt"] = last_segment.get("last_rdt")
    if basis in {"recording_datetime_timecode", "timecode"}:
        overrides["first_tc"] = first_segment.get("first_tc")
        overrides["last_tc"] = last_segment.get("last_tc")
    return {key: value for key, value in overrides.items() if metadata_value_is_valid(key, value)}


def boundary_metadata_for_ordered_sources(sources: list[SourceInfo]) -> dict[str, Any]:
    if not sources:
        return {}
    first_meta = source_meta(sources[0])
    last_meta = source_meta(sources[-1])
    overrides = {
        "first_tc": first_meta.get("first_tc"),
        "first_rdt": first_meta.get("first_rdt"),
        "first_abst": first_meta.get("first_abst"),
        "last_tc": last_meta.get("last_tc"),
        "last_rdt": last_meta.get("last_rdt"),
        "last_abst": last_meta.get("last_abst"),
        "video_rate": first_meta.get("video_rate"),
        "audio_rate": first_meta.get("audio_rate"),
        "channels": first_meta.get("channels"),
    }
    return {key: value for key, value in overrides.items() if metadata_value_is_valid(key, value)}


def validate_coverage(
    *,
    label: str,
    output_meta: dict[str, Any],
    sources: list[SourceInfo],
    basis: str | None,
) -> dict[str, Any]:
    if basis is None:
        return {
            "label": label,
            "ok": False,
            "basis": None,
            "issues": ["no common embedded DV coverage basis was available for every source"],
            "required": {},
            "actual": {},
            "missing": {},
        }
    required = cluster_union_ranges(sources, basis)
    actual = coverage_ranges_from_meta(output_meta, basis)
    ok, missing = ranges_cover(actual, required)
    issues = [] if ok else [f"{label} does not cover all source {basis} ranges"]
    return {
        "label": label,
        "ok": ok,
        "basis": basis,
        "issues": issues,
        "required": compact_ranges_for_json(required),
        "actual": compact_ranges_for_json(actual),
        "missing": compact_ranges_for_json(missing),
    }


def source_usable_content_frames(source: SourceInfo) -> int:
    """Primary duplicate score: frames without detected audio/video damage.

    DVRescue's broad problem_frame_count also includes metadata and continuity
    flags. Those matter, but they should not outrank actual tape content
    coverage when choosing one duplicate to keep.
    """
    return max(0, source.frame_count - max(source.audio_issue_count, source.video_issue_count))


def source_duplicate_decision_key(source: SourceInfo) -> tuple[Any, ...]:
    meta = source_meta(source)
    frames = source.frame_count
    problem_ratio = source.issue_count / frames if frames > 0 else 1.0
    coverage_basis_count = sum(1 for basis in COVERAGE_BASIS_ORDER if source_ranges(source, basis))
    metadata_score = sum(
        1
        for key in ["first_rdt", "last_rdt", "first_tc", "last_tc", "video_rate", "audio_rate", "channels"]
        if metadata_value_is_valid(key, meta.get(key))
    )
    return (
        source_usable_content_frames(source),
        frames,
        source.good_frame_count,
        -problem_ratio,
        -source.issue_count,
        -source.audio_issue_count,
        -source.video_issue_count,
        coverage_basis_count,
        metadata_score,
        source.bytes,
        source.path.name,
    )


def choose_best_duplicate_source(sources: list[SourceInfo]) -> SourceInfo:
    return max(sources, key=source_duplicate_decision_key)


def embedded_duplicate_components(sources: list[SourceInfo]) -> list[list[SourceInfo]]:
    """Group sources that are duplicate candidates, not adjacent fragments.

    Components are based on embedded identity overlap only. Micro-gap adjacency
    is intentionally excluded: adjacent non-overlapping fragments may represent
    unique tape content and can still be stitched or kept separately.
    """
    uf = UnionFind(len(sources))
    for i, left in enumerate(sources):
        for j in range(i + 1, len(sources)):
            if sources_have_embedded_identity_overlap(left, sources[j]):
                uf.union(i, j)
    components: dict[int, list[SourceInfo]] = {}
    for idx, source in enumerate(sources):
        components.setdefault(uf.find(idx), []).append(source)
    return [
        sorted(items, key=source_recording_sort_key)
        for items in components.values()
    ]


def resolve_rejected_merge_duplicate_selection(sources: list[SourceInfo]) -> dict[str, Any]:
    """Pick exactly one winner from each duplicate component.

    If DVRescue could not produce an accepted merge, the fallback must not keep
    two overlapping copies of the same item. It chooses one by media quality and
    records every loser as an intentional discard decision. Non-overlapping
    components remain separate because they are different tape portions.
    """
    selected: list[SourceInfo] = []
    discarded: dict[str, dict[str, Any]] = {}
    components_json: list[dict[str, Any]] = []
    for component in embedded_duplicate_components(sources):
        winner = choose_best_duplicate_source(component)
        selected.append(winner)
        rows = []
        for source in component:
            score = source_duplicate_decision_key(source)
            row = {
                "source": source.path.name,
                "selected": source is winner,
                "frames": source.frame_count,
                "usable_content_frames": source_usable_content_frames(source),
                "good_frames_after_all_dvrescue_problem_flags": source.good_frame_count,
                "problem_frames": source.issue_count,
                "audio_problem_frames": source.audio_issue_count,
                "video_problem_frames": source.video_issue_count,
                "score": list(score[:-1]),
            }
            rows.append(row)
            if source is not winner:
                discarded[source.path.name] = {
                    "winner": winner.path.name,
                    "reason": (
                        "duplicate candidate intentionally not kept; "
                        "the selected source has the best quality score for this embedded-overlap item"
                    ),
                    "loser_score": list(score[:-1]),
                    "winner_score": list(source_duplicate_decision_key(winner)[:-1]),
                }
        components_json.append(
            {
                "winner": winner.path.name,
                "component_sources": rows,
                "reason": (
                    "one KEEP winner chosen for this embedded-overlap duplicate component"
                    if len(component) > 1
                    else "single source component"
                ),
            }
        )
    return {
        "selected_sources": sorted(selected, key=source_recording_sort_key),
        "discarded_duplicate_sources": discarded,
        "components": components_json,
    }


def ordered_merge_sources(sources: list[SourceInfo]) -> list[SourceInfo]:
    return sorted(
        sources,
        key=lambda item: (
            item.raw_frames or 0,
            -item.issue_count,
            -item.audio_issue_count,
            item.path.name,
        ),
        reverse=True,
    )


def metadata_value_is_valid(key: str, value: Any) -> bool:
    if value is None:
        return False
    if key in {"first_abst", "last_abst"}:
        return int_or_none(value) not in {None, -1}
    return str(value) != ""


def find_expected_metadata_value(key: str, primary: SourceInfo, sources: list[SourceInfo]) -> Any:
    """Pick the value a merge output must preserve for a core DV metadata key."""
    primary_meta = primary.raw_dvrescue or primary.dvrescue
    primary_value = primary_meta.get(key)
    if metadata_value_is_valid(key, primary_value):
        return primary_value

    for source in sources:
        meta = source.raw_dvrescue or source.dvrescue
        value = meta.get(key)
        if metadata_value_is_valid(key, value):
            return value
    return primary_value


def validate_core_metadata(
    *,
    label: str,
    output_meta: dict[str, Any],
    primary: SourceInfo,
    sources: list[SourceInfo],
    expected_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reject merges that lose or corrupt core DV timestamps/metadata."""
    keys = [
        "first_tc",
        "last_tc",
        "first_rdt",
        "last_rdt",
        "first_abst",
        "last_abst",
        "video_rate",
        "audio_rate",
        "channels",
    ]
    issues: list[str] = []
    comparisons: list[dict[str, Any]] = []
    for key in keys:
        expected = None
        if expected_overrides and metadata_value_is_valid(key, expected_overrides.get(key)):
            expected = expected_overrides.get(key)
        if expected is None:
            expected = find_expected_metadata_value(key, primary, sources)
        actual = output_meta.get(key)
        comparisons.append({"key": key, "expected": expected, "actual": actual})
        if not metadata_value_is_valid(key, expected):
            continue
        if not metadata_value_is_valid(key, actual):
            issues.append(f"{label} metadata {key} is missing/invalid; expected {expected!r}")
            continue
        if str(actual) != str(expected):
            issues.append(f"{label} metadata {key} changed from {expected!r} to {actual!r}")

    primary_meta = primary.raw_dvrescue or primary.dvrescue
    output_speeds = set(output_meta.get("speed_values") or [])
    primary_speeds = set(primary_meta.get("speed_values") or [])
    new_speeds = sorted(output_speeds - primary_speeds)
    if new_speeds:
        issues.append(f"{label} metadata introduced speed flags: {', '.join(new_speeds)}")

    return {"label": label, "ok": not issues, "issues": issues, "comparisons": comparisons}


def summarize_decision(decision: dict[str, Any]) -> str:
    kind = decision.get("keep_kind") or "unknown"
    keep_file = Path(decision["keep_file"]).name if decision.get("keep_file") else "none"
    if decision.get("merge_accepted"):
        selected = decision.get("selected") or {}
        trimmed = selected.get("trimmed_frames_vs_longest_input")
        problems = selected.get("merged_problem_frames")
        audio_problems = selected.get("merged_audio_problem_frames")
        return (
            f"merge accepted; keep={keep_file}; merged_problems={problems}; "
            f"merged_audio_problems={audio_problems}; trimmed_frames={trimmed}"
        )
    if decision.get("merge_attempted"):
        stitch = decision.get("micro_gap_stitch") or {}
        if stitch.get("accepted"):
            plan = stitch.get("plan") or {}
            return (
                f"merge rejected; micro-gap stitch accepted; keep={keep_file}; "
                f"synthetic_hold_frames={plan.get('synthetic_hold_frames')}"
            )
        reasons = decision.get("merge_rejected_reasons") or []
        reason_text = "; ".join(str(item) for item in reasons[:2])
        if len(reasons) > 2:
            reason_text += f"; +{len(reasons) - 2} more"
        return f"merge rejected; keep={keep_file}; reason={reason_text or kind}"
    selected = decision.get("selected") or {}
    source = selected.get("source", "unknown")
    return f"no merge needed; source={source}; keep={keep_file}"


def short_progress_name(name: str, limit: int = 42) -> str:
    if len(name) <= limit:
        return name
    return name[: limit - 3] + "..."


def analyze_ffprobe(path: Path) -> dict[str, Any]:
    proc = run_cmd(
        [
            FFPROBE,
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            "stream=index,codec_type,codec_name,nb_read_frames,nb_frames,duration,r_frame_rate,sample_rate,channels,channel_layout,bits_per_sample",
            "-show_entries",
            "format=format_name,duration,size",
            "-of",
            "json",
            path,
        ],
        timeout=300,
    )
    result: dict[str, Any] = {"exit_code": proc.returncode, "stderr": proc.stderr.strip()}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        result["json_error"] = str(exc)
        return result
    result["raw"] = data
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), streams[0] if streams else {})
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    result.update(
        {
            "format_name": fmt.get("format_name"),
            "format_duration": float_or_none(fmt.get("duration")),
            "format_size": int_or_none(fmt.get("size")),
            "codec_name": video_stream.get("codec_name"),
            "stream_duration": float_or_none(video_stream.get("duration")),
            "nb_frames": int_or_none(video_stream.get("nb_frames")),
            "nb_read_frames": int_or_none(video_stream.get("nb_read_frames")),
            "r_frame_rate": video_stream.get("r_frame_rate"),
            "audio_stream_count": len(audio_streams),
            "audio_streams": [
                {
                    "index": int_or_none(stream.get("index")),
                    "codec_name": stream.get("codec_name"),
                    "duration": float_or_none(stream.get("duration")),
                    "sample_rate": int_or_none(stream.get("sample_rate")),
                    "channels": int_or_none(stream.get("channels")),
                    "channel_layout": stream.get("channel_layout"),
                    "bits_per_sample": int_or_none(stream.get("bits_per_sample")),
                    "nb_frames": int_or_none(stream.get("nb_frames")),
                    "nb_read_frames": int_or_none(stream.get("nb_read_frames")),
                }
                for stream in audio_streams
            ],
        }
    )
    return result


@dataclass
class SourceInfo:
    path: Path
    stem: str
    version: int | None
    cassette: str | None
    stamp: dt.datetime | None
    bytes: int
    ffprobe: dict[str, Any] = field(default_factory=dict)
    dvrescue: dict[str, Any] = field(default_factory=dict)
    raw_path: Path | None = None
    raw_bytes: int | None = None
    raw_frames: int | None = None
    raw_ffprobe: dict[str, Any] | None = None
    raw_dvrescue: dict[str, Any] | None = None

    @property
    def issue_count(self) -> int:
        raw = self.raw_dvrescue if self.raw_dvrescue else self.dvrescue
        return int(raw.get("problem_frame_count") or 0)

    @property
    def audio_issue_count(self) -> int:
        raw = self.raw_dvrescue if self.raw_dvrescue else self.dvrescue
        return int(raw.get("audio_problem_frame_count") or 0)

    @property
    def video_issue_count(self) -> int:
        raw = self.raw_dvrescue if self.raw_dvrescue else self.dvrescue
        return int(raw.get("video_problem_frame_count") or 0)

    @property
    def frame_count(self) -> int:
        if self.raw_frames is not None:
            return self.raw_frames
        if self.dvrescue.get("frames_count"):
            return int(self.dvrescue["frames_count"])
        if self.ffprobe.get("nb_read_frames"):
            return int(self.ffprobe["nb_read_frames"])
        if self.ffprobe.get("nb_frames"):
            return int(self.ffprobe["nb_frames"])
        return 0

    @property
    def tail_repeated_problem_count(self) -> int:
        raw = self.raw_dvrescue if self.raw_dvrescue else self.dvrescue
        return int(raw.get("tail_repeated_problem_count") or 0)

    @property
    def good_frame_count(self) -> int:
        return max(0, self.frame_count - self.issue_count)


def report_json_default(value: Any) -> Any:
    if isinstance(value, SourceInfo):
        return {"file": value.path.name, "path": str(value.path)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def report_json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, default=report_json_default)


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def discover_sources() -> list[SourceInfo]:
    sources: list[SourceInfo] = []
    for path in sorted(ROOT.glob("*.avi")):
        sources.append(
            SourceInfo(
                path=path,
                stem=path.stem,
                version=None,
                cassette=operating_cassette_id(),
                stamp=None,
                bytes=path.stat().st_size,
            )
        )
    return sources


def source_recording_start_datetime(source: SourceInfo) -> dt.datetime | None:
    anchor = source_stable_timeline_anchor(source)
    if anchor.get("ok"):
        parsed = datetime_from_datetime_timecode_frame(int_or_none(anchor.get("timeline_start")))
        if parsed is not None:
            return parsed
    return parse_dv_datetime(source_meta(source).get("first_rdt"))


def source_recording_sort_key(source: SourceInfo) -> tuple[dt.datetime, int, str, int]:
    anchor = source_stable_timeline_anchor(source)
    timeline_start = int_or_none(anchor.get("timeline_start")) if anchor.get("ok") else None
    return (
        source_recording_start_datetime(source) or dt.datetime.min,
        (timeline_start % DV_FPS) if timeline_start is not None else (parse_timecode(source_meta(source).get("first_tc")) or -1),
        source.path.name,
        0,
    )


def cluster_sources(
    sources: list[SourceInfo],
    close_seconds: int,
    micro_gap_max_frames: int,
) -> list[list[SourceInfo]]:
    uf = UnionFind(len(sources))
    for i, left in enumerate(sources):
        for j in range(i + 1, len(sources)):
            right = sources[j]

            if sources_have_embedded_identity_overlap(left, right):
                uf.union(i, j)
                continue

            # Adjacent fragments belong together only when embedded DV evidence
            # says they are physically contiguous. Filenames are intentionally
            # ignored here; they are labels, not preservation evidence.
            if sources_have_embedded_stitch_adjacency(left, right, micro_gap_max_frames):
                uf.union(i, j)
                continue

            # `close_seconds` is kept as a deprecated CLI/report setting for
            # compatibility, but filename timestamp clustering is no longer
            # used. A source must carry embedded DV evidence to be clustered.
            _ = close_seconds

    clusters: dict[int, list[SourceInfo]] = {}
    for idx, source in enumerate(sources):
        clusters.setdefault(uf.find(idx), []).append(source)
    return [
        sorted(items, key=lambda item: (*source_recording_sort_key(item), item.path.name))
        for items in clusters.values()
    ]


def source_true_timeline_range(source: SourceInfo) -> tuple[int, int] | None:
    """Global v2 placement: embedded recording datetime plus frame-in-second.

    The key design rule is deliberately narrow: this is the only global
    timeline coordinate. Timecode and abst can repeat across recordings, so they
    are used for local overlap/seam proof only after this placement step.
    """
    anchor = source_stable_timeline_anchor(source)
    frame_count = int_or_none(anchor.get("frame_count")) or source.frame_count
    if frame_count is None or frame_count <= 0:
        return None
    start = int_or_none(anchor.get("timeline_start"))
    if start is None:
        return None
    if not anchor.get("ok"):
        return None
    return start, start + frame_count - 1


def source_true_timeline_status(source: SourceInfo) -> dict[str, Any]:
    meta = source_meta(source)
    status: dict[str, Any] = {
        "source": source.path.name,
        "first_rdt": meta.get("first_rdt"),
        "last_rdt": meta.get("last_rdt"),
        "first_tc": meta.get("first_tc"),
        "last_tc": meta.get("last_tc"),
        "frames": int_or_none(meta.get("frames_count")) or source.frame_count,
        "timeline_range": None,
        "placeable": False,
        "stable_timeline_anchor": source_stable_timeline_anchor(source),
    }
    rng = source_true_timeline_range(source)
    if rng is not None:
        status.update(
            {
                "placeable": True,
                "timeline_range": {"start": rng[0], "end": rng[1], "frames": rng[1] - rng[0] + 1},
            }
        )
        return status
    if status["frames"] is None or status["frames"] <= 0:
        status["reason"] = "missing or invalid frame count"
    elif datetime_timecode_frame(meta.get("first_rdt"), meta.get("first_tc")) is None:
        status["reason"] = "missing embedded first_rdt or first_tc frame position"
    else:
        status["reason"] = (status.get("stable_timeline_anchor") or {}).get(
            "reason",
            "recording datetime span is inconsistent with frame count",
        )
        status["recording_datetime_span_sanity"] = source_rdt_span_sanity(source)
    return status


def cluster_timeline_summary(
    cluster: list[SourceInfo],
    timeline_gap_max_frames: int | None,
) -> dict[str, Any]:
    placed: list[tuple[int, int, SourceInfo]] = []
    unplaceable: list[dict[str, Any]] = []
    for source in cluster:
        rng = source_true_timeline_range(source)
        if rng is None:
            unplaceable.append(source_true_timeline_status(source))
        else:
            placed.append((rng[0], rng[1], source))
    placed.sort(key=lambda item: (item[0], item[1], item[2].path.name))
    positive_gaps: list[dict[str, Any]] = []
    current_end: int | None = None
    previous: SourceInfo | None = None
    for start, end, source in placed:
        if current_end is not None and start > current_end + 1:
            positive_gaps.append(
                {
                    "gap_frames": start - current_end - 1,
                    "gap_seconds": (start - current_end - 1) / DV_FPS,
                    "left_source": previous.path.name if previous else None,
                    "right_source": source.path.name,
                }
            )
        if current_end is None or end > current_end:
            current_end = end
            previous = source
    fallback_evidence: list[dict[str, Any]] = []
    if timeline_gap_max_frames is not None:
        for i, left in enumerate(cluster):
            for right in cluster[i + 1:]:
                item = v2_unplaceable_duplicate_identity_evidence(
                    left,
                    right,
                    timeline_gap_max_frames,
                )
                if item.get("ok"):
                    fallback_evidence.append(item)
    return {
        "mode": "true_recording_datetime_timeline_v2",
        "gap_max_frames": timeline_gap_max_frames,
        "gap_max_seconds": timeline_gap_max_frames / DV_FPS if timeline_gap_max_frames is not None else None,
        "placeable_sources": len(placed),
        "unplaceable_sources": unplaceable,
        "timeline_start": placed[0][0] if placed else None,
        "timeline_end": max((end for _, end, _ in placed), default=None),
        "positive_gaps": positive_gaps[:30],
        "positive_gap_count": len(positive_gaps),
        "largest_positive_gap_frames": max((item["gap_frames"] for item in positive_gaps), default=0),
        "unplaceable_duplicate_identity_fallback_pairs": len(fallback_evidence),
        "unplaceable_duplicate_identity_fallback_evidence": fallback_evidence[:30],
    }


def cluster_sources_timeline_v2(
    sources: list[SourceInfo],
    timeline_gap_max_frames: int,
) -> list[list[SourceInfo]]:
    """Build v2 clusters on the true recording-time interval sweep.

    Sources with missing/corrupt global timeline placement are no longer
    blindly singletoned. They may join a cluster only through strict duplicate
    identity fallback evidence; filenames never participate.
    """
    uf = UnionFind(len(sources))
    placed: list[tuple[int, int, int, SourceInfo]] = []
    for idx, source in enumerate(sources):
        rng = source_true_timeline_range(source)
        if rng is not None:
            placed.append((rng[0], rng[1], idx, source))
    placed.sort(key=lambda item: (item[0], item[1], *source_recording_sort_key(item[3]), item[3].path.name))

    current_root: int | None = None
    current_end: int | None = None
    for start, end, idx, _source in placed:
        if current_root is not None and current_end is not None and start > current_end + timeline_gap_max_frames + 1:
            current_root = None
            current_end = None
        if current_root is None:
            current_root = idx
        else:
            uf.union(current_root, idx)
        current_end = max(current_end if current_end is not None else end, end)

    for i, left in enumerate(sources):
        left_unplaceable = source_true_timeline_range(left) is None
        for j in range(i + 1, len(sources)):
            right = sources[j]
            if not left_unplaceable and source_true_timeline_range(right) is not None:
                continue
            evidence = v2_unplaceable_duplicate_identity_evidence(
                left,
                right,
                timeline_gap_max_frames,
            )
            if evidence.get("ok"):
                uf.union(i, j)

    clusters_by_root: dict[int, list[SourceInfo]] = {}
    for idx, source in enumerate(sources):
        clusters_by_root.setdefault(uf.find(idx), []).append(source)

    def cluster_sort_key(items: list[SourceInfo]) -> tuple[Any, ...]:
        timeline_ranges = [source_true_timeline_range(source) for source in items]
        timeline_starts = [rng[0] for rng in timeline_ranges if rng is not None]
        first = sorted(items, key=lambda item: (*source_recording_sort_key(item), item.path.name))[0]
        if timeline_starts:
            return (0, min(timeline_starts), *source_recording_sort_key(first), first.path.name)
        return (1, *source_recording_sort_key(first), first.path.name)

    clusters = [
        sorted(items, key=lambda item: (*source_recording_sort_key(item), item.path.name))
        for items in clusters_by_root.values()
    ]
    return sorted(clusters, key=cluster_sort_key)


def choose_best_source(sources: list[SourceInfo]) -> SourceInfo:
    # Primary metric is good-frame count (frames minus DVRescue-reported
    # problem frames). A longer capture with many concealed/broken frames is
    # not preferable to a slightly shorter capture with clean frames.
    return sorted(
        sources,
        key=lambda item: (
            item.good_frame_count,
            item.frame_count,
            -item.issue_count,
            -item.audio_issue_count,
            item.bytes,
        ),
        reverse=True,
    )[0]


def choose_best_source_for_cluster(sources: list[SourceInfo]) -> SourceInfo:
    physical_supersets = [
        source for source in sources
        if best_source_physically_covers_cluster(source, sources)
    ]
    if physical_supersets:
        return choose_best_source(physical_supersets)
    return choose_best_source(sources)


def output_key_for_cluster(cluster: list[SourceInfo], idx: int) -> str:
    first_dt = source_recording_start_datetime(cluster[0])
    last_dt = source_recording_start_datetime(cluster[-1])
    cassette = operating_cassette_id()
    if first_dt and last_dt:
        return safe_name(f"{idx:02d}_{cassette}.{first_dt:%Y-%m-%d_%H-%M-%S}_to_{last_dt:%H-%M-%S}")
    return safe_name(f"{idx:02d}_{cassette}")


def timecode_for_filename(tc: Any) -> str | None:
    if tc is None:
        return None
    text = str(tc).strip()
    if not re.match(r"^\d{2}:\d{2}:\d{2}:\d{2}$", text):
        return None
    return text.replace(":", "-")


def date_for_filename(rdt: Any) -> str | None:
    parsed = parse_dv_datetime(rdt)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%d_%H-%M-%S")


def professional_output_stem(
    *,
    cassette: str | None,
    dv_meta: dict[str, Any],
    fallback_key: str,
    collision_suffix: str | None = None,
) -> str:
    """Public KEEP filename stem: recording identity, not rescue workflow."""
    _ = cassette
    parts: list[str] = []
    date_part = date_for_filename(dv_meta.get("first_rdt"))
    if date_part:
        parts.append(date_part)
    else:
        parts.append(safe_name(fallback_key))
    parts.append(operating_cassette_id())
    tc = timecode_for_filename(dv_meta.get("first_tc"))
    if tc:
        parts.append(f"tc-{tc}")
    if collision_suffix:
        parts.append(safe_name(collision_suffix))
    return safe_name("__".join(parts))


def professional_keep_output_path(
    keep_dir: Path,
    *,
    cassette: str | None,
    dv_meta: dict[str, Any],
    fallback_key: str,
    suffix: str,
    collision_suffix: str | None = None,
) -> Path:
    stem = professional_output_stem(
        cassette=cassette,
        dv_meta=dv_meta,
        fallback_key=fallback_key,
        collision_suffix=collision_suffix,
    )
    return keep_dir / f"{stem}{suffix}"


KEEP_OUTPUT_PATHS_RESERVED: set[str] = set()


def reset_keep_output_path_reservations() -> None:
    KEEP_OUTPUT_PATHS_RESERVED.clear()


def keep_output_reservation_key(path: Path) -> str:
    return str(path.resolve()).casefold()


def reserve_keep_output_path(path: Path) -> Path:
    """Reserve one final KEEP path for this run, adding a neutral suffix on collision."""
    path = require_inside_target(path)
    key = keep_output_reservation_key(path)
    if key not in KEEP_OUTPUT_PATHS_RESERVED:
        KEEP_OUTPUT_PATHS_RESERVED.add(key)
        return path

    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    ordinal = 2
    while True:
        candidate = parent / f"{stem}__collision-{ordinal:02d}{suffix}"
        candidate_key = keep_output_reservation_key(candidate)
        if candidate_key not in KEEP_OUTPUT_PATHS_RESERVED:
            KEEP_OUTPUT_PATHS_RESERVED.add(candidate_key)
            return candidate
        ordinal += 1


def extract_raw(source: SourceInfo, raw_dir: Path, force: bool) -> None:
    raw_path = raw_dir / f"{safe_name(source.path.stem)}.raw.dv"
    if force or not raw_path.exists():
        proc = run_cmd(
            [
                FFMPEG,
                "-v",
                "error",
                "-y",
                "-nostdin",
                "-i",
                source.path,
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "rawvideo",
                raw_path,
            ],
            timeout=1200,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg raw extraction failed for {source.path.name}: {proc.stderr.strip()}")
    source.raw_path = raw_path
    source.raw_bytes = raw_path.stat().st_size
    source.raw_frames = source.raw_bytes // RAW_DV_FRAME_BYTES_PAL
    source.raw_ffprobe = analyze_ffprobe(raw_path)
    source.raw_dvrescue = analyze_dvrescue(raw_path)


def merge_raw_dv(sources: list[SourceInfo], out_path: Path, log_path: Path, force: bool) -> dict[str, Any]:
    ordered = ordered_merge_sources(sources)
    args: list[str | Path] = [DVRESCUE]
    args.extend(item.raw_path for item in ordered if item.raw_path is not None)
    args.extend(
        [
            "--merge",
            out_path,
            "--merge-log",
            log_path,
            "--merge-log-missing",
            "--merge-output-concealed",
        ]
    )
    if force or not out_path.exists():
        proc = run_cmd(args, timeout=1800)
    else:
        proc = subprocess.CompletedProcess(args=[str(a) for a in args], returncode=0, stdout="", stderr="")
    return {
        "command": [str(a) for a in args],
        "input_order": [item.path.name for item in ordered],
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout.splitlines()[-20:],
        "stderr_tail": proc.stderr.splitlines()[-20:],
        "output": str(out_path),
        "merge_log": str(log_path),
    }


def wrap_dv_to_avi(
    dv_path: Path,
    avi_path: Path,
    force: bool,
    metadata_tags: dict[str, str] | None = None,
    validation_dir: Path | None = None,
) -> dict[str, Any]:
    if metadata_tags is not None:
        return write_metadata_avi_from_raw_dv(
            dv_path,
            avi_path,
            metadata_tags,
            force=force,
            validation_dir=validation_dir or avi_path.parent / "_metadata_validation",
        )
    if force or not avi_path.exists():
        proc = run_cmd([FFMPEG, "-v", "error", "-y", "-nostdin", "-i", dv_path, "-c", "copy", avi_path], timeout=1200)
    else:
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    return {"exit_code": proc.returncode, "stderr": proc.stderr.strip(), "output": str(avi_path)}


def copy_file(src: Path, dst: Path, apply: bool) -> None:
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def link_source_file_to_keep(src: Path, dst: Path, apply: bool) -> str:
    """Preserve an untouched source in KEEP without consuming duplicate space."""
    if not apply:
        return "planned_ntfs_hardlink"
    dst.parent.mkdir(parents=True, exist_ok=True)
    require_inside_target(dst)
    if dst.exists():
        delete_file_inside_target(dst)
    try:
        os.link(src, dst)
        return "ntfs_hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "byte_for_byte_copy"


def read_text_if_exists(path: Path, max_chars: int = 20_000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + "\n... truncated ..."
    return text


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def iter_raw_dv_frame_hashes(raw_dv_path: Path) -> list[str]:
    """SHA-256 of each fixed-size DV frame in a raw .dv payload."""
    hashes: list[str] = []
    with raw_dv_path.open("rb") as handle:
        while True:
            frame = handle.read(RAW_DV_FRAME_BYTES_PAL)
            if len(frame) < RAW_DV_FRAME_BYTES_PAL:
                break
            hashes.append(hashlib.sha256(frame).hexdigest())
    return hashes


def deep_verify_decision(decision: dict[str, Any], source_paths_by_name: dict[str, Path], verify_root: Path) -> dict[str, Any]:
    """Prove every KEEP output frame is a byte-copy of a source frame.

    Synthetic hold frames are byte-copies of a real previous frame, so they are
    also present in the source frame set. Therefore a correct, pristine output
    has ZERO frames absent from its cluster's source frames. Any "foreign" frame
    means fabricated or re-encoded content -> verification fails.
    """
    require_inside_target(verify_root)
    verify_root.mkdir(parents=True, exist_ok=True)
    source_hashes: set[str] = set()
    for name in decision.get("source_files", []):
        src_path = source_paths_by_name.get(name)
        if src_path is None:
            continue
        raw = verify_root / f"{safe_name(Path(name).stem)}.src.dv"
        extraction = extract_raw_dv_payload(src_path, raw)
        if extraction.get("exit_code") == 0 and raw.exists():
            source_hashes.update(iter_raw_dv_frame_hashes(raw))
        delete_file_inside_target(raw)

    outputs: list[dict[str, Any]] = []
    total_foreign = 0
    for keep_file in decision.get("keep_files", []):
        keep_path = Path(keep_file)
        raw = verify_root / f"{safe_name(keep_path.stem)}.keep.dv"
        extraction = extract_raw_dv_payload(keep_path, raw)
        foreign = 0
        frames = 0
        if extraction.get("exit_code") == 0 and raw.exists():
            for frame_hash in iter_raw_dv_frame_hashes(raw):
                frames += 1
                if frame_hash not in source_hashes:
                    foreign += 1
        delete_file_inside_target(raw)
        outputs.append({"keep_file": keep_path.name, "frames": frames, "frames_not_in_sources": foreign})
        total_foreign += foreign
    return {
        "ok": total_foreign == 0,
        "frames_not_in_sources_total": total_foreign,
        "outputs": outputs,
        "note": "every real output frame, and every synthetic hold frame, must be a byte-copy of a source frame",
    }


def ffprobe_format_tags(path: Path) -> dict[str, Any]:
    proc = run_cmd([FFPROBE, "-v", "error", "-show_entries", "format_tags", "-of", "json", path], timeout=300)
    result: dict[str, Any] = {"exit_code": proc.returncode, "stderr": proc.stderr.strip(), "tags": {}}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        result["json_error"] = str(exc)
        return result
    result["tags"] = (data.get("format") or {}).get("tags") or {}
    return result


def stream_hashes(path: Path) -> dict[str, Any]:
    proc = run_cmd(
        [FFMPEG, "-v", "error", "-nostdin", "-i", path, "-map", "0", "-c", "copy", "-f", "streamhash", "-hash", "SHA256", "-"],
        timeout=1800,
    )
    hashes: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 3:
            hashes[f"{parts[0]}:{parts[1]}"] = parts[2]
    return {
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip(),
        "hashes": hashes,
        "stdout_lines": proc.stdout.splitlines(),
    }


def iso_datetime_for_metadata(value: Any) -> str | None:
    parsed = parse_dv_datetime(value)
    if parsed is None:
        return str(value).strip() if value is not None and str(value).strip() else None
    return parsed.isoformat(timespec="seconds")


def dv_datetime_string_from_timeline_frame(value: Any) -> str | None:
    parsed = datetime_from_datetime_timecode_frame(int_or_none(value))
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def output_dv_metadata_with_stable_anchor(
    dv_meta: dict[str, Any],
    stable_anchor: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return public KEEP metadata, correcting dirty first-frame datetimes.

    The DV essence is not changed. This only affects public filenames, AVI tags,
    comment payloads, and filesystem timestamps.
    """
    public_meta = dict(dv_meta or {})
    extra_payload: dict[str, Any] = {}
    if not stable_anchor or not stable_anchor.get("ok"):
        return public_meta, extra_payload

    stable_start = dv_datetime_string_from_timeline_frame(stable_anchor.get("timeline_start"))
    if not stable_start:
        return public_meta, extra_payload

    original_start = public_meta.get("first_rdt")
    original_iso = iso_datetime_for_metadata(original_start)
    stable_iso = iso_datetime_for_metadata(stable_start)
    if original_iso != stable_iso:
        public_meta["first_rdt"] = stable_start
        extra_payload.update(
            {
                "public_recording_datetime_source": "v2_stable_timeline_anchor",
                "public_recording_start_corrected_from_embedded_first_frame": True,
                "embedded_first_rdt_original": original_iso,
                "v2_stable_recording_start": stable_iso,
                "v2_stable_timeline_anchor": {
                    "method": stable_anchor.get("method"),
                    "source": stable_anchor.get("source"),
                    "leading_frames_before_anchor": stable_anchor.get("leading_frames_before_anchor"),
                    "anchor_frame": stable_anchor.get("anchor_frame"),
                    "timeline_start": stable_anchor.get("timeline_start"),
                    "timeline_start_datetime": stable_iso,
                },
            }
        )
    return public_meta, extra_payload


def source_public_dv_metadata(source: SourceInfo) -> tuple[dict[str, Any], dict[str, Any]]:
    return output_dv_metadata_with_stable_anchor(
        source.dvrescue,
        source_stable_timeline_anchor(source),
    )


def metadata_comment_payload(
    *,
    output_key: str,
    cassette: str | None,
    source_files: list[str],
    keep_kind: str,
    dv_meta: dict[str, Any],
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = cassette
    cassette_id = operating_cassette_id()
    payload = {
        "schema": METADATA_SCHEMA,
        "camera_model": CAMERA_MODEL,
        "output_key": output_key,
        "cassette": cassette_id,
        "source_files": source_files,
        "keep_kind": keep_kind,
        "dv_recording_start": iso_datetime_for_metadata(dv_meta.get("first_rdt")),
        "dv_recording_end": iso_datetime_for_metadata(dv_meta.get("last_rdt")),
        "dv_timecode_start": dv_meta.get("first_tc"),
        "dv_timecode_end": dv_meta.get("last_tc"),
        "dv_absolute_track_start": dv_meta.get("first_abst"),
        "dv_absolute_track_end": dv_meta.get("last_abst"),
        "dv_frames": dv_meta.get("frames_count"),
        "dv_video_rate_fps": dv_meta.get("video_rate"),
        "dv_audio_rate_hz": dv_meta.get("audio_rate"),
        "dv_audio_channels": dv_meta.get("channels"),
        "dv_problem_frames": dv_meta.get("problem_frame_count"),
        "dv_audio_problem_frames": dv_meta.get("audio_problem_frame_count"),
        "dv_video_problem_frames": dv_meta.get("video_problem_frame_count"),
        "dv_speed_values": dv_meta.get("speed_values") or [],
    }
    if extra_payload:
        payload.update(extra_payload)
    return payload


def build_avi_metadata_tags(
    *,
    output_key: str,
    cassette: str | None,
    source_files: list[str],
    keep_kind: str,
    dv_meta: dict[str, Any],
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    _ = cassette
    cassette_id = operating_cassette_id()
    start = iso_datetime_for_metadata(dv_meta.get("first_rdt"))
    title_bits = [cassette_id]
    if start:
        title_bits.append(start.replace("T", " "))
    title_bits.append(CAMERA_MODEL)
    payload = metadata_comment_payload(
        output_key=output_key,
        cassette=cassette_id,
        source_files=source_files,
        keep_kind=keep_kind,
        dv_meta=dv_meta,
        extra_payload=extra_payload,
    )
    tags = {
        "title": " | ".join(title_bits),
        "comment": json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        "encoded_by": f"dvrescue_combiner_v2.py; DVRescue; FFmpeg; camera={CAMERA_MODEL}",
    }
    if start:
        tags["recording_timestamp"] = start
        tags["date"] = start
    return tags


def ffmpeg_metadata_args(tags: dict[str, str]) -> list[str]:
    args: list[str] = []
    recording_timestamp = tags.get("recording_timestamp")
    if recording_timestamp:
        args.extend(["-timestamp", recording_timestamp])
    args.extend(["-map_metadata", "-1"])
    for key in ["title", "date", "comment", "encoded_by"]:
        value = tags.get(key)
        if value:
            args.extend(["-metadata", f"{key}={value}"])
    return args


def validate_written_tags(path: Path, expected_tags: dict[str, str]) -> dict[str, Any]:
    probe = ffprobe_format_tags(path)
    actual = probe.get("tags") or {}
    issues = []
    for key in ["title", "date", "comment", "encoded_by"]:
        expected = expected_tags.get(key)
        if expected and actual.get(key) != expected:
            issues.append(f"metadata tag {key!r} was not preserved")
    return {
        "ok": probe.get("exit_code") == 0 and not issues,
        "issues": issues,
        "expected": {key: expected_tags[key] for key in expected_tags if key in {"title", "date", "comment", "encoded_by"}},
        "actual": actual,
        "probe": probe,
    }


def windows_filetime_from_datetime(value: dt.datetime) -> int:
    return int(value.timestamp() * 10_000_000) + 116_444_736_000_000_000


def set_windows_file_times(path: Path, value: dt.datetime) -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": True, "skipped": True, "reason": "not Windows"}

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    set_file_time.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    filetime = windows_filetime_from_datetime(value)
    low = filetime & 0xFFFFFFFF
    high = filetime >> 32
    filetime_struct = ctypes.wintypes.FILETIME(low, high)
    handle = create_file(
        str(path),
        0x0100,  # FILE_WRITE_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,  # read/write/delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        return {"ok": False, "error": ctypes.get_last_error(), "stage": "CreateFileW"}
    try:
        ok = bool(
            set_file_time(
                handle,
                ctypes.byref(filetime_struct),
                ctypes.byref(filetime_struct),
                ctypes.byref(filetime_struct),
            )
        )
        if not ok:
            return {"ok": False, "error": ctypes.get_last_error(), "stage": "SetFileTime"}
        return {"ok": True, "skipped": False}
    finally:
        close_handle(handle)


def set_filesystem_recording_time(path: Path, dv_meta: dict[str, Any]) -> dict[str, Any]:
    value = parse_dv_datetime(dv_meta.get("first_rdt"))
    result: dict[str, Any] = {
        "recording_datetime": value.isoformat(timespec="seconds") if value else None,
        "set_creation_time": False,
        "set_modified_time": False,
        "set_access_time": False,
        "ok": False,
    }
    if value is None:
        result["error"] = "no valid embedded DV first_rdt was available"
        return result
    timestamp = value.timestamp()
    try:
        os.utime(path, (timestamp, timestamp))
        result["set_modified_time"] = True
        result["set_access_time"] = True
    except OSError as exc:
        result["error"] = f"os.utime failed: {exc}"
        return result

    windows_result = set_windows_file_times(path, value)
    result["windows_filetime"] = windows_result
    result["set_creation_time"] = bool(windows_result.get("ok"))
    stat = path.stat()
    result["actual_modified_time"] = dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    result["actual_creation_time"] = dt.datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds")
    result["ok"] = (
        result["set_modified_time"]
        and result["set_access_time"]
        and (os.name != "nt" or result["set_creation_time"])
        and abs(stat.st_mtime - timestamp) <= 2
        and (os.name != "nt" or abs(stat.st_ctime - timestamp) <= 2)
    )
    return result


def extract_raw_dv_payload(path: Path, out_path: Path) -> dict[str, Any]:
    out_path = require_inside_target(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = run_cmd(
        [FFMPEG, "-v", "error", "-y", "-nostdin", "-i", path, "-map", "0:v:0", "-c", "copy", "-f", "rawvideo", out_path],
        timeout=1800,
    )
    return {
        "command": [FFMPEG, "-v", "error", "-y", "-nostdin", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "rawvideo", str(out_path)],
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip(),
        "output": str(out_path),
        "sha256": sha256_file(out_path) if proc.returncode == 0 and out_path.exists() else None,
        "bytes": out_path.stat().st_size if proc.returncode == 0 and out_path.exists() else None,
    }


def validate_raw_dv_payload(reference_raw: Path, media_path: Path, validation_dir: Path) -> dict[str, Any]:
    validation_dir = require_inside_target(validation_dir)
    validation_dir.mkdir(parents=True, exist_ok=True)
    extracted = validation_dir / f"{safe_name(media_path.stem)}.raw.dv"
    if extracted.exists():
        delete_file_inside_target(extracted)
    extraction = extract_raw_dv_payload(media_path, extracted)
    reference_hash = sha256_file(reference_raw) if reference_raw.exists() else None
    ok = extraction["exit_code"] == 0 and reference_hash is not None and extraction["sha256"] == reference_hash
    return {
        "ok": ok,
        "reference_raw": str(reference_raw),
        "media": str(media_path),
        "reference_raw_sha256": reference_hash,
        "extracted_raw_sha256": extraction.get("sha256"),
        "extraction": extraction,
    }


def validate_stream_copy_preserved(src: Path, dst: Path) -> dict[str, Any]:
    before = stream_hashes(src)
    after = stream_hashes(dst)
    issues = []
    if before["exit_code"] != 0:
        issues.append("could not compute source stream hashes")
    if after["exit_code"] != 0:
        issues.append("could not compute output stream hashes")
    if before["hashes"] != after["hashes"]:
        issues.append("stream hashes changed during metadata remux")
    return {
        "ok": not issues,
        "issues": issues,
        "source": before,
        "output": after,
    }


def write_metadata_avi_copy(src: Path, dst: Path, tags: dict[str, str], force: bool) -> dict[str, Any]:
    dst = require_inside_target(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.metadata.tmp{dst.suffix}")
    delete_file_inside_target(tmp)
    result: dict[str, Any] = {
        "kind": "metadata_avi_stream_copy",
        "source": str(src),
        "output": str(dst),
        "metadata_tags": tags,
    }
    if not force and dst.exists():
        tag_validation = validate_written_tags(dst, tags)
        stream_validation = validate_stream_copy_preserved(src, dst)
        result.update(
            {
                "command": [],
                "exit_code": 0 if tag_validation["ok"] and stream_validation["ok"] else 1,
                "stderr": "",
                "tag_validation": tag_validation,
                "stream_hash_validation": stream_validation,
                "reused_existing_output": tag_validation["ok"] and stream_validation["ok"],
            }
        )
        if result["exit_code"] == 0:
            return result

    args: list[str | Path] = [FFMPEG, "-v", "error", "-y", "-nostdin", "-i", src, "-map", "0", "-c", "copy"]
    args.extend(ffmpeg_metadata_args(tags))
    args.append(tmp)
    proc = run_cmd(args, timeout=1800)
    result["command"] = command_for_report(args)
    result["exit_code"] = proc.returncode
    result["stderr"] = proc.stderr.strip()
    if proc.returncode != 0 or not tmp.exists():
        delete_file_inside_target(tmp)
        return result

    result["tag_validation"] = validate_written_tags(tmp, tags)
    result["stream_hash_validation"] = validate_stream_copy_preserved(src, tmp)
    if not result["tag_validation"]["ok"] or not result["stream_hash_validation"]["ok"]:
        result["exit_code"] = 1
        delete_file_inside_target(tmp)
        return result
    if dst.exists():
        delete_file_inside_target(dst)
    shutil.move(str(tmp), str(dst))
    return result


def write_metadata_avi_from_raw_dv(
    raw_dv_path: Path,
    avi_path: Path,
    tags: dict[str, str],
    force: bool,
    validation_dir: Path,
) -> dict[str, Any]:
    avi_path = require_inside_target(avi_path)
    avi_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = avi_path.with_name(f"{avi_path.stem}.metadata.tmp{avi_path.suffix}")
    delete_file_inside_target(tmp)
    result: dict[str, Any] = {
        "kind": "metadata_avi_from_raw_dv",
        "source": str(raw_dv_path),
        "output": str(avi_path),
        "metadata_tags": tags,
    }
    if not force and avi_path.exists():
        tag_validation = validate_written_tags(avi_path, tags)
        payload_validation = validate_raw_dv_payload(raw_dv_path, avi_path, validation_dir)
        result.update(
            {
                "command": [],
                "exit_code": 0 if tag_validation["ok"] and payload_validation["ok"] else 1,
                "stderr": "",
                "tag_validation": tag_validation,
                "raw_dv_payload_validation": payload_validation,
                "reused_existing_output": tag_validation["ok"] and payload_validation["ok"],
            }
        )
        if result["exit_code"] == 0:
            return result

    args: list[str | Path] = [FFMPEG, "-v", "error", "-y", "-nostdin", "-i", raw_dv_path, "-c", "copy"]
    args.extend(ffmpeg_metadata_args(tags))
    args.append(tmp)
    proc = run_cmd(args, timeout=1800)
    result["command"] = command_for_report(args)
    result["exit_code"] = proc.returncode
    result["stderr"] = proc.stderr.strip()
    if proc.returncode != 0 or not tmp.exists():
        delete_file_inside_target(tmp)
        return result

    result["tag_validation"] = validate_written_tags(tmp, tags)
    result["raw_dv_payload_validation"] = validate_raw_dv_payload(raw_dv_path, tmp, validation_dir)
    if not result["tag_validation"]["ok"] or not result["raw_dv_payload_validation"]["ok"]:
        result["exit_code"] = 1
        delete_file_inside_target(tmp)
        return result
    if avi_path.exists():
        delete_file_inside_target(avi_path)
    shutil.move(str(tmp), str(avi_path))
    return result


def command_for_report(args: list[str | Path]) -> list[str]:
    return [str(arg) for arg in args]


def run_conversion_command(args: list[str | Path], timeout: int = 1200) -> dict[str, Any]:
    proc = run_cmd(args, timeout=timeout)
    return {
        "command": command_for_report(args),
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout.splitlines()[-10:],
        "stderr_tail": proc.stderr.splitlines()[-10:],
    }


def choose_conversion_self_test_sample(sources: list[SourceInfo]) -> SourceInfo:
    candidates = [
        source
        for source in sources
        if not source_excluded_from_keep(source)
        and source.frame_count > 0
        and source.dvrescue.get("xml_error") is None
    ]
    if not candidates:
        raise RuntimeError("No non-excluded analyzable source file is available for conversion self-tests.")
    return sorted(
        candidates,
        key=lambda item: (
            item.issue_count != 0,
            bool(item.dvrescue.get("speed_values")),
            item.bytes,
            item.path.name,
        ),
    )[0]


def run_conversion_self_tests(
    *,
    sources: list[SourceInfo],
    test_dir: Path,
    keep_work: bool,
) -> dict[str, Any]:
    """Hash-test the conversion paths this script relies on.

    AVI containers may change header/index bytes when rewrapped, so the tests
    compare the DV essence payload after round-tripping.
    """
    sample = choose_conversion_self_test_sample(sources)
    test_dir = require_inside_target(test_dir)
    remove_tree_inside_target(test_dir)
    test_dir.mkdir(parents=True, exist_ok=True)

    source_raw_a = test_dir / "ffmpeg_extract_a.raw.dv"
    source_raw_b = test_dir / "ffmpeg_extract_b.raw.dv"
    wrapped_avi = test_dir / "ffmpeg_roundtrip.avi"
    wrapped_raw = test_dir / "ffmpeg_roundtrip.raw.dv"
    metadata_avi = test_dir / "ffmpeg_metadata_remux.avi"
    dvrescue_out = test_dir / "dvrescue_single_merge.dv"
    dvrescue_log = test_dir / "dvrescue_single_merge.log"

    tests: list[dict[str, Any]] = []
    try:
        extract_a_args: list[str | Path] = [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-nostdin",
            "-i",
            sample.path,
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "rawvideo",
            source_raw_a,
        ]
        extract_b_args = [*extract_a_args[:-1], source_raw_b]
        wrap_args: list[str | Path] = [FFMPEG, "-v", "error", "-y", "-nostdin", "-i", source_raw_a, "-c", "copy", wrapped_avi]
        unwrap_args: list[str | Path] = [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-nostdin",
            "-i",
            wrapped_avi,
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "rawvideo",
            wrapped_raw,
        ]
        dvrescue_args: list[str | Path] = [
            DVRESCUE,
            source_raw_a,
            "--merge",
            dvrescue_out,
            "--merge-log",
            dvrescue_log,
            "--merge-log-missing",
            "--merge-output-concealed",
        ]

        extract_a = run_conversion_command(extract_a_args)
        extract_b = run_conversion_command(extract_b_args)
        extract_ok = (
            extract_a["exit_code"] == 0
            and extract_b["exit_code"] == 0
            and source_raw_a.exists()
            and source_raw_b.exists()
            and sha256_file(source_raw_a) == sha256_file(source_raw_b)
        )
        tests.append(
            {
                "name": "ffmpeg_avi_to_raw_dv_is_deterministic",
                "ok": extract_ok,
                "sample": sample.path.name,
                "commands": [extract_a, extract_b],
                "hashes": {
                    "first_raw_sha256": sha256_file(source_raw_a) if source_raw_a.exists() else None,
                    "second_raw_sha256": sha256_file(source_raw_b) if source_raw_b.exists() else None,
                },
                "sizes": {
                    "first_raw_bytes": source_raw_a.stat().st_size if source_raw_a.exists() else None,
                    "second_raw_bytes": source_raw_b.stat().st_size if source_raw_b.exists() else None,
                },
            }
        )

        wrap = run_conversion_command(wrap_args)
        unwrap = run_conversion_command(unwrap_args)
        roundtrip_ok = (
            wrap["exit_code"] == 0
            and unwrap["exit_code"] == 0
            and source_raw_a.exists()
            and wrapped_raw.exists()
            and sha256_file(source_raw_a) == sha256_file(wrapped_raw)
        )
        tests.append(
            {
                "name": "ffmpeg_raw_dv_to_avi_to_raw_dv_preserves_dv_payload",
                "ok": roundtrip_ok,
                "sample": sample.path.name,
                "commands": [wrap, unwrap],
                "hashes": {
                    "source_raw_sha256": sha256_file(source_raw_a) if source_raw_a.exists() else None,
                    "roundtrip_raw_sha256": sha256_file(wrapped_raw) if wrapped_raw.exists() else None,
                    "container_avi_sha256": sha256_file(wrapped_avi) if wrapped_avi.exists() else None,
                },
                "sizes": {
                    "source_raw_bytes": source_raw_a.stat().st_size if source_raw_a.exists() else None,
                    "roundtrip_raw_bytes": wrapped_raw.stat().st_size if wrapped_raw.exists() else None,
                    "container_avi_bytes": wrapped_avi.stat().st_size if wrapped_avi.exists() else None,
                },
            }
        )

        metadata_tags = build_avi_metadata_tags(
            output_key=safe_name(sample.stem),
            cassette=sample.cassette,
            source_files=[sample.path.name],
            keep_kind="conversion_self_test_metadata_remux",
            dv_meta=sample.dvrescue,
        )
        metadata_remux = write_metadata_avi_copy(sample.path, metadata_avi, metadata_tags, force=True)
        metadata_remux_ok = (
            metadata_remux["exit_code"] == 0
            and metadata_remux.get("tag_validation", {}).get("ok")
            and metadata_remux.get("stream_hash_validation", {}).get("ok")
        )
        tests.append(
            {
                "name": "ffmpeg_metadata_remux_preserves_all_stream_packets",
                "ok": metadata_remux_ok,
                "sample": sample.path.name,
                "metadata_remux": metadata_remux,
                "container_avi_sha256": sha256_file(metadata_avi) if metadata_avi.exists() else None,
            }
        )

        dvrescue = run_conversion_command(dvrescue_args, timeout=1800)
        dvrescue_ok = (
            dvrescue["exit_code"] == 0
            and source_raw_a.exists()
            and dvrescue_out.exists()
            and sha256_file(source_raw_a) == sha256_file(dvrescue_out)
        )
        tests.append(
            {
                "name": "dvrescue_single_input_merge_preserves_clean_raw_dv_payload",
                "ok": dvrescue_ok,
                "sample": sample.path.name,
                "commands": [dvrescue],
                "hashes": {
                    "source_raw_sha256": sha256_file(source_raw_a) if source_raw_a.exists() else None,
                    "dvrescue_output_sha256": sha256_file(dvrescue_out) if dvrescue_out.exists() else None,
                },
                "sizes": {
                    "source_raw_bytes": source_raw_a.stat().st_size if source_raw_a.exists() else None,
                    "dvrescue_output_bytes": dvrescue_out.stat().st_size if dvrescue_out.exists() else None,
                },
                "merge_log_tail": read_text_if_exists(dvrescue_log).splitlines()[-20:] if dvrescue_log.exists() else [],
            }
        )

        failed = [test for test in tests if not test["ok"]]
        return {
            "ok": not failed,
            "sample": source_to_json(sample),
            "note": "Hashes compare DV essence payloads, not whole AVI container bytes.",
            "tests": tests,
            "failed_tests": [test["name"] for test in failed],
        }
    finally:
        if not keep_work:
            remove_tree_inside_target(test_dir)
            try:
                require_inside_target(test_dir.parent).rmdir()
            except OSError:
                pass


def source_to_json(source: SourceInfo) -> dict[str, Any]:
    coverage = {
        basis: compact_ranges_for_json(source_ranges(source, basis))
        for basis in COVERAGE_BASIS_ORDER
        if source_ranges(source, basis)
    }
    loose_identity_tc = loose_identity_timecode_ranges(source)
    return {
        "file": source.path.name,
        "path": str(source.path),
        "stem": source.stem,
        "version": source.version,
        "bytes": source.bytes,
        "cassette": source.cassette,
        "filename_timestamp": source.stamp.isoformat() if source.stamp else None,
        "bad_frame_policy": bad_frame_policy_for_source(source),
        "v2_stable_timeline_anchor": source_stable_timeline_anchor(source),
        "embedded_coverage": coverage,
        "embedded_identity_only_timecode": (
            compact_ranges_for_json(loose_identity_tc) if loose_identity_tc else {}
        ),
        "ffprobe": source.ffprobe,
        "dvrescue": source.dvrescue,
        "raw_path": str(source.raw_path) if source.raw_path else None,
        "raw_bytes": source.raw_bytes,
        "raw_frames": source.raw_frames,
        "raw_ffprobe": source.raw_ffprobe,
        "raw_dvrescue": source.raw_dvrescue,
    }


def pair_identity_evidence(
    left: SourceInfo,
    right: SourceInfo,
    micro_gap_max_frames: int,
) -> dict[str, Any]:
    left_window = source_recording_datetime_window(left)
    right_window = source_recording_datetime_window(right)
    strict_rdt_tc_overlap = ranges_overlap(
        source_ranges(left, "recording_datetime_timecode"),
        source_ranges(right, "recording_datetime_timecode"),
    )
    strict_tc_overlap = ranges_overlap(source_ranges(left, "timecode"), source_ranges(right, "timecode"))
    loose_tc_overlap = ranges_overlap(loose_identity_timecode_ranges(left), loose_identity_timecode_ranges(right))
    recording_datetime_overlap = (
        datetime_windows_overlap(left_window, right_window)
        if left_window is not None and right_window is not None
        else False
    )
    stitch_forward = source_stitch_adjacency(left, right, micro_gap_max_frames)
    stitch_reverse = source_stitch_adjacency(right, left, micro_gap_max_frames)
    return {
        "left": left.path.name,
        "right": right.path.name,
        "identity_match": sources_have_embedded_identity_overlap(left, right)
        or stitch_forward.get("ok")
        or stitch_reverse.get("ok"),
        "recording_datetime_overlap": recording_datetime_overlap,
        "strict_recording_datetime_timecode_overlap": strict_rdt_tc_overlap,
        "strict_timecode_overlap": strict_tc_overlap,
        "loose_identity_timecode_overlap": loose_tc_overlap,
        "physical_identity_overlap": sources_have_physical_identity_overlap(left, right),
        "embedded_stitch_adjacency": {
            "left_to_right": stitch_forward,
            "right_to_left": stitch_reverse,
        },
        "note": "Filenames are not used as identity evidence.",
    }


def cluster_identity_evidence(
    cluster: list[SourceInfo],
    micro_gap_max_frames: int,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for i, left in enumerate(cluster):
        for right in cluster[i + 1:]:
            item = pair_identity_evidence(left, right, micro_gap_max_frames)
            if item["identity_match"]:
                evidence.append(item)
    return evidence


def write_source_copy_keep_output(
    source: SourceInfo,
    *,
    key: str,
    keep_dir: Path,
    keep_kind: str,
    apply: bool,
    force: bool,
    collision_suffix: str | None = None,
    metadata_source_files: list[str] | None = None,
    output_dv_meta: dict[str, Any] | None = None,
    metadata_extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one source AVI as a metadata-tagged KEEP file and return its record.

    The source file itself is never modified. The helper performs a stream
    copy with embedded DV-derived metadata tags, validates stream hashes
    against the original, and sets filesystem timestamps to the embedded
    recording datetime. Raises if any of those steps fail so the caller can
    decide how to handle the failure.
    """
    public_dv_meta, stable_extra_payload = source_public_dv_metadata(source)
    if output_dv_meta is not None:
        public_dv_meta = output_dv_meta
        stable_extra_payload = {}
    extra_payload = dict(stable_extra_payload)
    if metadata_extra_payload:
        extra_payload.update(metadata_extra_payload)
    source_files_for_metadata = metadata_source_files or [source.path.name]
    output_stem = professional_output_stem(
        cassette=source.cassette,
        dv_meta=public_dv_meta,
        fallback_key=key,
        collision_suffix=collision_suffix,
    )
    dst = reserve_keep_output_path(keep_dir / f"{output_stem}{source.path.suffix}")
    output_stem = dst.stem
    metadata_tags = build_avi_metadata_tags(
        output_key=output_stem,
        cassette=source.cassette,
        source_files=source_files_for_metadata,
        keep_kind=keep_kind,
        dv_meta=public_dv_meta,
        extra_payload=extra_payload or None,
    )
    if apply:
        metadata_result = write_metadata_avi_copy(source.path, dst, metadata_tags, force=force)
        if metadata_result.get("exit_code") != 0:
            raise RuntimeError(
                f"metadata remux failed for {source.path.name}: "
                f"{metadata_result.get('stderr') or metadata_result.get('tag_validation') or metadata_result.get('stream_hash_validation')}"
            )
        filesystem_timestamp = set_filesystem_recording_time(dst, public_dv_meta)
        if not filesystem_timestamp["ok"]:
            raise RuntimeError(f"filesystem timestamp update failed for {dst.name}: {filesystem_timestamp}")
        bytes_written = dst.stat().st_size
        preservation_mode = "metadata_remux_stream_copy"
    else:
        metadata_result = {
            "kind": "planned_metadata_avi_stream_copy",
            "metadata_tags": metadata_tags,
            "note": "dry run; no KEEP file was written",
        }
        filesystem_timestamp = {
            "kind": "planned_filesystem_timestamp_update",
            "recording_datetime": iso_datetime_for_metadata(public_dv_meta.get("first_rdt")),
        }
        bytes_written = source.bytes
        preservation_mode = "planned_metadata_remux_stream_copy"
    return {
        "kind": "metadata_enriched_source_preservation",
        "source": source.path.name,
        "output": str(dst),
        "public_name": dst.name,
        "public_name_basis": "embedded DV recording datetime, cassette id, and DV timecode start",
        "bytes": bytes_written,
        "preservation_mode": preservation_mode,
        "metadata_write": metadata_result,
        "metadata_source_files": source_files_for_metadata,
        "public_dv_metadata": {
            "first_rdt": public_dv_meta.get("first_rdt"),
            "last_rdt": public_dv_meta.get("last_rdt"),
            "first_tc": public_dv_meta.get("first_tc"),
            "last_tc": public_dv_meta.get("last_tc"),
            "first_abst": public_dv_meta.get("first_abst"),
            "last_abst": public_dv_meta.get("last_abst"),
        },
        "metadata_extra_payload": extra_payload,
        "filesystem_timestamp": filesystem_timestamp,
        "reason": "metadata-enriched stream copy; stream hashes prove media packets are unchanged",
    }


def build_source_copy_outputs(
    sources: list[SourceInfo],
    *,
    key: str,
    keep_dir: Path,
    keep_kind: str,
    apply: bool,
    force: bool,
    metadata_source_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Write each source as its own KEEP copy, returning the output records.

    Adds a clip-NN collision suffix when two sources would otherwise produce
    the same professional output stem.
    """
    base_stems = [
        professional_output_stem(
            cassette=source.cassette,
            dv_meta=source_public_dv_metadata(source)[0],
            fallback_key=key,
        )
        for source in sources
    ]
    duplicate_stems = {stem for stem in base_stems if base_stems.count(stem) > 1}
    outputs: list[dict[str, Any]] = []
    for ordinal, source in enumerate(sources, start=1):
        collision_suffix = f"clip-{ordinal:02d}" if base_stems[ordinal - 1] in duplicate_stems else None
        outputs.append(
            write_source_copy_keep_output(
                source,
                key=key,
                keep_dir=keep_dir,
                keep_kind=keep_kind,
                apply=apply,
                force=force,
                collision_suffix=collision_suffix,
                metadata_source_files=metadata_source_files,
            )
        )
    return outputs


def add_copy_outputs(
    *,
    decision: dict[str, Any],
    key: str,
    keep_dir: Path,
    sources: list[SourceInfo],
    apply: bool,
    force: bool,
    keep_kind: str,
) -> None:
    outputs = build_source_copy_outputs(
        sources,
        key=key,
        keep_dir=keep_dir,
        keep_kind=keep_kind,
        apply=apply,
        force=force,
        metadata_source_files=[source.path.name for source in active_cluster],
    )
    decision["keep_files"] = [item["output"] for item in outputs]
    decision["keep_file"] = outputs[0]["output"] if outputs else None
    decision["keep_kind"] = keep_kind
    decision["selected"] = outputs[0] if len(outputs) == 1 else {"kind": keep_kind, "outputs": outputs}


def apply_single_source_master_fallback(
    *,
    decision: dict[str, Any],
    key: str,
    keep_dir: Path,
    cluster: list[SourceInfo],
    active_cluster: list[SourceInfo],
    basis: str | None,
    apply: bool,
    force: bool,
    trigger: str,
) -> None:
    """Enforce the v2 one-cluster/one-KEEP-file contract after master construction fails."""
    fallback = choose_best_source_for_cluster(active_cluster)
    physical_superset = best_source_physically_covers_cluster(fallback, active_cluster)
    keep_kind = (
        "single_source_master_fallback_physical_superset"
        if physical_superset
        else "single_source_master_fallback_best_available"
    )
    output = write_source_copy_keep_output(
        fallback,
        key=key,
        keep_dir=keep_dir,
        keep_kind=keep_kind,
        apply=apply,
        force=force,
    )
    keep_ranges = source_ranges(fallback, basis) if basis else []
    decision["keep_files"] = [output["output"]]
    decision["keep_file"] = output["output"]
    decision["keep_kind"] = keep_kind
    decision["selected"] = output
    decision["single_master_fallback"] = {
        "trigger": trigger,
        "source": fallback.path.name,
        "physical_superset": physical_superset,
        "reason": (
            "single-master contract forbids multiple KEEP clips; selected the best physical "
            "superset source after derived master construction failed"
            if physical_superset
            else "single-master contract forbids multiple KEEP clips; selected the best available source "
            "after derived master construction failed, but not every source is proven covered"
        ),
        "source_score": {
            "frames": fallback.frame_count,
            "good_frames": fallback.good_frame_count,
            "problem_frames": fallback.issue_count,
            "audio_problem_frames": fallback.audio_issue_count,
            "video_problem_frames": fallback.video_issue_count,
        },
    }
    decision["source_disposition"] = build_source_disposition(
        sources=cluster,
        basis=basis,
        keep_ranges=keep_ranges,
        keep_sources=[fallback],
    )
    decision["coverage_audit"] = compute_coverage_audit(active_cluster, keep_ranges, basis)


def _plan_micro_gap_stitch_for_basis(
    sources: list[SourceInfo],
    basis: str,
    max_gap_frames: int,
) -> dict[str, Any]:
    if max_gap_frames < 0:
        return {"ok": False, "reason": "micro-gap stitching is disabled"}
    if len(sources) < 2:
        return {"ok": False, "reason": "fewer than two selected sources"}

    rows: list[dict[str, Any]] = []
    for source in sources:
        coverage = plausible_single_range_for_stitch(source, basis, max_gap_frames)
        if coverage is None:
            return {
                "ok": False,
                "reason": f"{source.path.name} does not have one plausible {basis} coverage range",
            }
        if source.raw_path is None or not source.raw_path.exists():
            return {"ok": False, "reason": f"{source.path.name} has no extracted raw DV payload"}
        rows.append(
            {
                "source": source,
                "start": coverage[0],
                "end": coverage[1],
                "frames": source.raw_frames or source.frame_count,
            }
        )

    rows.sort(key=lambda item: (item["start"], item["end"], item["source"].path.name))
    gaps: list[dict[str, Any]] = []
    for left, right in zip(rows, rows[1:]):
        adjacency = source_stitch_adjacency(left["source"], right["source"], max_gap_frames)
        if basis == "timecode" and not adjacency.get("ok"):
            return {
                "ok": False,
                "reason": (
                    f"{left['source'].path.name} and {right['source'].path.name} "
                    f"are not safe stitch neighbors: {adjacency.get('reason')}"
                ),
                "adjacency": adjacency,
            }
        if int(right["start"]) <= int(left["end"]):
            return {
                "ok": False,
                "reason": (
                    "selected sources overlap; micro-gap stitch only handles adjacent fragments "
                    "after DVRescue merge rejection"
                ),
            }
        tc_gap_frames = int(right["start"]) - int(left["end"]) - 1
        if tc_gap_frames > max_gap_frames:
            return {
                "ok": False,
                "reason": (
                    f"gap between {left['source'].path.name} and {right['source'].path.name} "
                    f"is {tc_gap_frames} frames, above limit {max_gap_frames}"
                ),
            }
        # Trust the absolute track over timecode at the seam: capture splits can
        # show a 1-2 frame timecode/abst disagreement. The number of synthetic
        # hold frames we insert must reflect the PHYSICAL gap (abst), not the
        # timecode artifact, so we do not fabricate frames that physically exist.
        effective = adjacency.get("effective_gap_frames")
        gap_frames = int(effective) if effective is not None else tc_gap_frames
        datetime_sanity: dict[str, Any] | None = None
        if basis == "timecode":
            left_start_dt = parse_dv_datetime(source_meta(left["source"]).get("first_rdt"))
            right_start_dt = parse_dv_datetime(source_meta(right["source"]).get("first_rdt"))
            if left_start_dt is not None and right_start_dt is not None:
                expected_right_start = left_start_dt + dt.timedelta(
                    seconds=(int(left["frames"] or 0) + gap_frames) / DV_FPS
                )
                datetime_delta = abs((right_start_dt - expected_right_start).total_seconds())
                datetime_sanity = {
                    "expected_next_recording_datetime": expected_right_start.isoformat(timespec="seconds"),
                    "actual_next_recording_datetime": right_start_dt.isoformat(timespec="seconds"),
                    "delta_seconds": datetime_delta,
                    "ok": datetime_delta <= 2.0,
                }
        gap_record = {
            "previous_source": left["source"].path.name,
            "next_source": right["source"].path.name,
            "gap_frames": gap_frames,
            "timecode_gap_frames": tc_gap_frames,
            "gap_seconds": gap_frames / DV_FPS,
            "coverage_basis": basis,
            "gap_basis": "absolute_track" if effective is not None else "timecode",
            "missing_coverage_range": (
                {"start": int(left["end"]) + 1, "end": int(right["start"]) - 1}
                if gap_frames > 0
                else None
            ),
            "hold_source_frame": {
                "tc": source_meta(left["source"]).get("last_tc"),
                "rdt": source_meta(left["source"]).get("last_rdt"),
                "abst": source_meta(left["source"]).get("last_abst"),
            },
            "next_source_first_frame": {
                "tc": source_meta(right["source"]).get("first_tc"),
                "rdt": source_meta(right["source"]).get("first_rdt"),
                "abst": source_meta(right["source"]).get("first_abst"),
            },
        }
        if datetime_sanity is not None:
            gap_record["recording_datetime_sanity"] = datetime_sanity
        if adjacency:
            gap_record["adjacency_evidence"] = adjacency
        gaps.append(gap_record)

    synthetic_frames = sum(int(item["gap_frames"]) for item in gaps)
    source_frames = sum(int(item["frames"] or 0) for item in rows)
    return {
        "ok": True,
        "basis": basis,
        "max_gap_frames": max_gap_frames,
        "max_gap_seconds": max_gap_frames / DV_FPS,
        "ordered_sources": [
            {
                "source": item["source"].path.name,
                "coverage_start": int(item["start"]),
                "coverage_end": int(item["end"]),
                "frames": int(item["frames"] or 0),
            }
            for item in rows
        ],
        "gaps": gaps,
        "source_frames": source_frames,
        "synthetic_hold_frames": synthetic_frames,
        "expected_output_frames": source_frames + synthetic_frames,
    }


def plan_micro_gap_stitch(
    sources: list[SourceInfo],
    basis: str | None,
    max_gap_frames: int,
) -> dict[str, Any]:
    # Micro-gaps are about tape-frame adjacency. Use DV timecode for ordering,
    # but require absolute-track continuity and no camera rec_start boundary.
    candidate_bases = ["timecode"]
    failures: list[dict[str, Any]] = []
    for candidate in candidate_bases:
        plan = _plan_micro_gap_stitch_for_basis(sources, candidate, max_gap_frames)
        if plan["ok"]:
            plan["coverage_basis_for_delete_safety"] = basis
            return plan
        failures.append({"basis": candidate, "reason": plan.get("reason")})
    return {
        "ok": False,
        "reason": "no embedded coverage basis produced a safe micro-gap stitch plan",
        "basis_failures": failures,
    }


def append_file_bytes(src: Path, dst_handle: Any) -> None:
    with src.open("rb") as handle:
        shutil.copyfileobj(handle, dst_handle, length=1024 * 1024)


def read_last_raw_dv_frame(path: Path) -> bytes:
    with path.open("rb") as handle:
        handle.seek(-RAW_DV_FRAME_BYTES_PAL, os.SEEK_END)
        frame = handle.read(RAW_DV_FRAME_BYTES_PAL)
    if len(frame) != RAW_DV_FRAME_BYTES_PAL:
        raise RuntimeError(f"Could not read one full DV frame from {path}")
    return frame


def write_micro_gap_stitched_raw(
    plan: dict[str, Any],
    sources: list[SourceInfo],
    out_path: Path,
    force: bool,
) -> dict[str, Any]:
    out_path = require_inside_target(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    source_by_name = {source.path.name: source for source in sources}
    result: dict[str, Any] = {
        "kind": "raw_dv_micro_gap_stitch",
        "output": str(out_path),
        "plan": plan,
        "reused_existing_output": False,
    }
    expected_bytes = int(plan["expected_output_frames"]) * RAW_DV_FRAME_BYTES_PAL
    if not force and out_path.exists() and out_path.stat().st_size == expected_bytes:
        result.update(
            {
                "exit_code": 0,
                "bytes": out_path.stat().st_size,
                "expected_bytes": expected_bytes,
                "reused_existing_output": True,
            }
        )
        return result

    tmp = out_path.with_name(f"{out_path.stem}.tmp{out_path.suffix}")
    delete_file_inside_target(tmp)
    try:
        with tmp.open("wb") as output:
            for index, item in enumerate(plan["ordered_sources"]):
                source = source_by_name[item["source"]]
                if source.raw_path is None:
                    raise RuntimeError(f"{source.path.name} has no raw DV payload")
                append_file_bytes(source.raw_path, output)
                if index >= len(plan["gaps"]):
                    continue
                gap = plan["gaps"][index]
                gap_frames = int(gap["gap_frames"])
                if gap_frames <= 0:
                    continue
                hold_frame = read_last_raw_dv_frame(source.raw_path)
                for _ in range(gap_frames):
                    output.write(hold_frame)
        if tmp.stat().st_size != expected_bytes:
            raise RuntimeError(
                f"stitched raw DV size mismatch: wrote {tmp.stat().st_size}, expected {expected_bytes}"
            )
        if out_path.exists():
            delete_file_inside_target(out_path)
        shutil.move(str(tmp), str(out_path))
        result.update({"exit_code": 0, "bytes": out_path.stat().st_size, "expected_bytes": expected_bytes})
        return result
    except Exception as exc:
        delete_file_inside_target(tmp)
        result.update({"exit_code": 1, "error": str(exc), "expected_bytes": expected_bytes})
        return result


def create_micro_gap_stitch_output(
    *,
    key: str,
    keep_dir: Path,
    merge_dir: Path,
    sources: list[SourceInfo],
    basis: str | None,
    max_gap_frames: int,
    force: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "attempted": False,
        "accepted": False,
        "max_gap_frames": max_gap_frames,
        "max_gap_seconds": max_gap_frames / DV_FPS if max_gap_frames >= 0 else None,
    }
    plan = plan_micro_gap_stitch(sources, basis, max_gap_frames)
    result["plan"] = plan
    if not plan["ok"]:
        result["reason"] = plan.get("reason")
        return result

    result["attempted"] = True
    stitch_basis = plan.get("basis")
    ordered_sources = [
        next(source for source in sources if source.path.name == item["source"])
        for item in plan["ordered_sources"]
    ]
    stitched_dv = merge_dir / f"{key}.micro-gap-stitch.dv"
    raw_write = write_micro_gap_stitched_raw(plan, ordered_sources, stitched_dv, force=force)
    result["raw_write"] = raw_write
    if raw_write.get("exit_code") != 0:
        result["reason"] = "raw DV micro-gap stitch failed"
        return result

    stitched_dvrescue = analyze_dvrescue(stitched_dv)
    stitched_frames = int(stitched_dvrescue.get("frames_count") or 0)
    expected_frames = int(plan["expected_output_frames"])
    frame_validation = {
        "ok": stitched_frames == expected_frames,
        "expected_output_frames": expected_frames,
        "stitched_raw_frames": stitched_frames,
    }
    result["stitched_raw_dvrescue"] = stitched_dvrescue
    result["frame_validation"] = frame_validation
    if not frame_validation["ok"]:
        result["reason"] = "stitched raw DV frame count did not match the micro-gap plan"
        return result

    first_source = ordered_sources[0]
    stitch_metadata = {
        "derived_output": True,
        "derived_output_reason": "micro_gap_stitch_with_synthetic_hold_frames",
        "micro_gap_stitch": {
            "stitch_basis": stitch_basis,
            "coverage_basis_for_delete_safety": plan.get("coverage_basis_for_delete_safety"),
            "max_gap_frames": max_gap_frames,
            "max_gap_seconds": max_gap_frames / DV_FPS,
            "synthetic_hold_frames": int(plan["synthetic_hold_frames"]),
            "synthetic_hold_seconds": int(plan["synthetic_hold_frames"]) / DV_FPS,
            "gaps": plan["gaps"],
        },
    }
    output_stem = professional_output_stem(
        cassette=first_source.cassette,
        dv_meta=stitched_dvrescue,
        fallback_key=key,
    )
    candidate_avi = merge_dir / f"{key}.micro-gap-stitch.avi"
    final_avi = reserve_keep_output_path(keep_dir / f"{output_stem}.avi")
    output_stem = final_avi.stem
    metadata_tags = build_avi_metadata_tags(
        output_key=output_stem,
        cassette=first_source.cassette,
        source_files=[source.path.name for source in ordered_sources],
        keep_kind="avi_wrapped_from_raw_dv_micro_gap_stitch",
        dv_meta=stitched_dvrescue,
        extra_payload=stitch_metadata,
    )
    wrap_result = wrap_dv_to_avi(
        stitched_dv,
        candidate_avi,
        force=force,
        metadata_tags=metadata_tags,
        validation_dir=merge_dir / "metadata_validation_micro_gap_stitch",
    )
    wrapped_dvrescue = analyze_dvrescue(candidate_avi) if candidate_avi.exists() else {}
    metadata_validation = validate_core_metadata(
        label="micro-gap stitched AVI",
        output_meta=wrapped_dvrescue,
        primary=first_source,
        sources=ordered_sources,
        expected_overrides=boundary_metadata_for_ordered_sources(ordered_sources),
    )
    coverage_validation = validate_coverage(
        label="micro-gap stitched AVI",
        output_meta=wrapped_dvrescue,
        sources=ordered_sources,
        basis=stitch_basis,
    )
    wrapped_frames = int(wrapped_dvrescue.get("frames_count") or 0)
    wrapped_frame_validation = {
        "ok": wrapped_frames == expected_frames,
        "expected_output_frames": expected_frames,
        "wrapped_frames": wrapped_frames,
    }
    result.update(
        {
            "wrap_result": wrap_result,
            "wrapped_dvrescue": wrapped_dvrescue,
            "metadata_validation": metadata_validation,
            "coverage_validation": coverage_validation,
            "wrapped_frame_validation": wrapped_frame_validation,
        }
    )
    accepted = (
        wrap_result.get("exit_code") == 0
        and metadata_validation["ok"]
        and coverage_validation["ok"]
        and wrapped_frame_validation["ok"]
    )
    if not accepted:
        result["reason"] = "wrapped micro-gap stitch failed validation"
        return result

    if final_avi.exists():
        delete_file_inside_target(final_avi)
    shutil.move(str(candidate_avi), str(final_avi))
    filesystem_timestamp = set_filesystem_recording_time(final_avi, wrapped_dvrescue)
    result["filesystem_timestamp"] = filesystem_timestamp
    if not filesystem_timestamp["ok"]:
        result["reason"] = "filesystem timestamp update failed for micro-gap stitch"
        delete_file_inside_target(final_avi)
        return result

    result.update(
        {
            "accepted": True,
            "output": str(final_avi),
            "public_name": final_avi.name,
            "public_name_basis": "embedded DV recording datetime, cassette id, and DV timecode start",
            "bytes": final_avi.stat().st_size,
            "reason": "adjacent source fragments stitched with explicitly documented synthetic hold frames",
        }
    )
    return result


def build_source_disposition(
    *,
    sources: list[SourceInfo],
    basis: str | None,
    keep_ranges: list[tuple[int, int]],
    keep_sources: list[SourceInfo] | None = None,
    discarded_duplicate_sources: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    keep_sources = keep_sources or []
    discarded_duplicate_sources = discarded_duplicate_sources or {}
    dispositions: list[dict[str, Any]] = []
    for source in sources:
        bad_policy = bad_frame_policy_for_source(source)
        if bad_policy["excluded_from_keep"]:
            dispositions.append(
                {
                    "source": source.path.name,
                    "safe_to_delete_after_keep_verification": True,
                    "basis": basis,
                    "source_coverage": compact_ranges_for_json(source_ranges(source, basis)) if basis else {},
                    "missing_from_keep": {},
                    "excluded_from_keep_by_policy": True,
                    "bad_frame_policy": bad_policy,
                    "reason": f"source intentionally excluded from KEEP by policy: {bad_policy.get('reason')}",
                }
            )
            continue
        # NOTE: being labelled a "discarded duplicate" no longer makes a source
        # safe by itself. After the loss we found (a transitive-overlap chain
        # collapsed to one winner), safety MUST be proven by real coverage. The
        # duplicate context is still attached below for the report, but the
        # safe_to_delete decision falls through to the coverage check.
        duplicate_discard = discarded_duplicate_sources.get(source.path.name)
        # Trivial-cover shortcut: if this source is itself one of the files used
        # to build the KEEP outputs, the KEEP was stream-copied from it (which
        # the metadata-remux step already proved bit-identical via stream
        # hashes), so the source's DV essence is unambiguously preserved in
        # KEEP regardless of what the embedded coverage basis says.
        if any(ks.path == source.path for ks in keep_sources):
            dispositions.append(
                {
                    "source": source.path.name,
                    "safe_to_delete_after_keep_verification": True,
                    "basis": basis,
                    "source_coverage": compact_ranges_for_json(source_ranges(source, basis)) if basis else {},
                    "missing_from_keep": {},
                    "excluded_from_keep_by_policy": False,
                    "bad_frame_policy": bad_policy,
                    "is_keep_source": True,
                    "reason": (
                        "this source is one of the files used to build the KEEP output(s); "
                        "stream hashes prove the DV essence is bit-identical"
                    ),
                }
            )
            continue
        # Physical-superset shortcut: if any source that ended up in KEEP has
        # an absolute-track window that fully contains this source's window
        # AND identical first/last recording datetime, the source's data is
        # safely represented in KEEP regardless of whether DVRescue's
        # structured ranges happen to overlap.
        physical_keep = next(
            (ks for ks in keep_sources if ks is not source and source_physically_supersedes(ks, source)),
            None,
        )
        if physical_keep is not None:
            dispositions.append(
                {
                    "source": source.path.name,
                    "safe_to_delete_after_keep_verification": True,
                    "basis": basis,
                    "source_coverage": compact_ranges_for_json(source_ranges(source, basis)) if basis else {},
                    "missing_from_keep": {},
                    "excluded_from_keep_by_policy": False,
                    "bad_frame_policy": bad_policy,
                    "physically_covered_by_keep_source": physical_keep.path.name,
                    "reason": (
                        f"KEEP source {physical_keep.path.name} physically supersedes this file "
                        "(its absolute-track window fully contains this file's and recording datetime matches exactly)"
                    ),
                }
            )
            continue
        if basis is None:
            dispositions.append(
                {
                    "source": source.path.name,
                    "safe_to_delete_after_keep_verification": False,
                    "excluded_from_keep_by_policy": False,
                    "reason": "no common embedded coverage basis proves this file is represented in KEEP",
                }
            )
            continue
        required = source_ranges(source, basis)
        missing = coverage_deficit(required, keep_ranges, SEAM_TOLERANCE_FRAMES)
        ok = not missing
        entry = {
            "source": source.path.name,
            "safe_to_delete_after_keep_verification": ok,
            "basis": basis,
            "source_coverage": compact_ranges_for_json(required),
            "missing_from_keep": compact_ranges_for_json(missing),
            "excluded_from_keep_by_policy": False,
            "bad_frame_policy": bad_policy,
            "reason": (
                "source coverage proven represented in KEEP"
                if ok
                else "KEEP does not cover every embedded source range"
            ),
        }
        if duplicate_discard is not None:
            entry["discarded_duplicate_context"] = duplicate_discard
        dispositions.append(entry)
    return dispositions


def annotate_master_status(decision: dict[str, Any], *, single_master_goal: bool) -> None:
    keep_files = decision.get("keep_files") or []
    keep_kind = decision.get("keep_kind")
    audit = decision.get("coverage_audit") or {}
    deficit = int(audit.get("deficit_frames") or 0)
    coverage_complete = None if not audit else deficit == 0
    unsafe_sources = [
        item
        for item in decision.get("source_disposition") or []
        if item.get("safe_to_delete_after_keep_verification") is False
    ]

    status = "unknown"
    single_master_proven: bool | None = None
    reason = "cluster has not been evaluated"
    if keep_kind == "all_sources_excluded_by_policy":
        status = "no_keep_policy_excluded"
        single_master_proven = None
        reason = "all sources in this cluster were excluded by KEEP policy"
    elif keep_kind == "dry_run_merge_not_created":
        status = "not_evaluated_dry_run"
        single_master_proven = None
        reason = "a merge is planned, but --apply was not requested"
    elif deficit > 0:
        status = "coverage_deficit"
        single_master_proven = False
        reason = f"KEEP output(s) are missing {deficit} frame(s) of required source coverage"
    elif len(keep_files) == 1 and unsafe_sources:
        status = "single_master_unproven_source_disposition_blockers"
        single_master_proven = False
        reason = (
            "one output file was produced, but at least one source is not proven "
            "represented by that KEEP file"
        )
    elif len(keep_files) == 1:
        status = "single_master_verified"
        single_master_proven = True
        reason = "one output file passed the available coverage and media validation gates"
    elif len(keep_files) > 1:
        status = "single_master_not_proven_multiple_verified_parts"
        single_master_proven = False
        reason = (
            "a single master was not proven safe; multiple coverage-preserving parts were kept "
            "so no source footage is dropped"
        )
    elif keep_kind:
        status = "no_keep_output"
        single_master_proven = False
        reason = "no KEEP output file was produced"

    decision["master_status"] = {
        "single_master_goal": single_master_goal,
        "status": status,
        "single_master_proven": single_master_proven,
        "coverage_complete": coverage_complete,
        "coverage_deficit_frames": deficit if audit else None,
        "output_file_count": len(keep_files),
        "reason": reason,
    }


def source_problem_counts_for_frame_span(source: SourceInfo, start_frame: int, end_frame: int) -> dict[str, int]:
    meta = source.raw_dvrescue or source.dvrescue
    interval = [(start_frame, end_frame)]
    problem = intersection_total(report_ranges_to_tuples(meta.get("problem_frame_ranges")), interval)
    audio = intersection_total(report_ranges_to_tuples(meta.get("audio_problem_frame_ranges")), interval)
    video = intersection_total(report_ranges_to_tuples(meta.get("video_problem_frame_ranges")), interval)
    return {
        "problem_frames": problem,
        "audio_problem_frames": audio,
        "video_problem_frames": video,
        "media_problem_frames": max(audio, video),
    }


def v2_axis_range_from_segment(segment: dict[str, Any], basis: str, count: int) -> tuple[int, int] | None:
    if basis == "timecode":
        rng = clean_range(
            int_or_none(segment.get("first_tc_frame")),
            int_or_none(segment.get("last_tc_frame")),
            count,
        )
    elif basis == "recording_datetime_timecode":
        start = int_or_none(segment.get("first_rdt_tc_frame"))
        if start is None:
            rng = None
        else:
            end = int_or_none(segment.get("last_rdt_tc_frame"))
            strict = clean_range(start, end, count)
            if strict is not None and range_is_plausible_for_count(strict, count):
                rng = strict
            else:
                # Some captures have a sane recording start and frame count
                # but an inconsistent last_rdt/last_tc pair. v2's protected
                # placement is the first embedded recording datetime plus the
                # timecode frame part, then the observed frame count.
                rng = (start, start + count - 1)
    else:
        rng = None
    if rng is None or not range_is_plausible_for_count(rng, count):
        return None
    return rng


def v2_row_for_report(row: dict[str, Any]) -> dict[str, Any]:
    source = row["source"]
    return {
        "source": source.path.name,
        "segment_index": row["segment_index"],
        "axis_start": row["start"],
        "axis_end": row["end"],
        "timecode_start": row.get("tc_start"),
        "timecode_end": row.get("tc_end"),
        "recording_datetime_timecode_start": row.get("rdt_tc_start"),
        "recording_datetime_timecode_end": row.get("rdt_tc_end"),
        "source_frame_start": row["source_frame_start"],
        "source_frame_end": row["source_frame_end"],
        "frames": row["frames"],
        "placement_mode": row.get("placement_mode"),
        "quality": {
            "source_problem_frames": source.issue_count,
            "source_audio_problem_frames": source.audio_issue_count,
            "source_video_problem_frames": source.video_issue_count,
            "source_usable_content_frames": source_usable_content_frames(source),
        },
    }


def build_v2_master_candidate_rows(
    sources: list[SourceInfo],
    basis: str,
    max_gap_frames: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    tolerance = max(2, max_gap_frames)
    for source in sources:
        if source.raw_path is None or not source.raw_path.exists():
            failures.append({"source": source.path.name, "reason": "missing extracted raw DV payload"})
            continue
        raw_frames = source.raw_frames or 0
        if raw_frames <= 0:
            failures.append({"source": source.path.name, "reason": "raw DV payload has no full PAL DV frames"})
            continue
        meta = source.raw_dvrescue or source.dvrescue
        source_rows_before = len(rows)
        cumulative_offset = 0
        for segment_index, segment in enumerate(meta.get("frames_segments") or []):
            count = int_or_none(segment.get("count"))
            if count is None or count <= 0:
                continue
            axis_range = v2_axis_range_from_segment(segment, basis, count)
            if axis_range is None:
                cumulative_offset += count
                continue
            first_n = int_or_none(segment.get("first_n"))
            last_n = int_or_none(segment.get("last_n"))
            if (
                first_n is not None
                and last_n is not None
                and last_n >= first_n
                and abs((last_n - first_n + 1) - count) <= tolerance
            ):
                source_frame_start = first_n
                source_frame_end = last_n
            else:
                source_frame_start = cumulative_offset
                source_frame_end = cumulative_offset + count - 1
            cumulative_offset += count
            if source_frame_start < 0 or source_frame_start >= raw_frames:
                continue
            if source_frame_end >= raw_frames:
                trimmed_count = raw_frames - source_frame_start
                if trimmed_count <= 0:
                    continue
                axis_range = (axis_range[0], axis_range[0] + trimmed_count - 1)
                source_frame_end = raw_frames - 1
                count = trimmed_count
            tc_range = v2_axis_range_from_segment(segment, "timecode", count)
            rdt_tc_range = v2_axis_range_from_segment(segment, "recording_datetime_timecode", count)
            rows.append(
                {
                    "source": source,
                    "segment_index": segment_index,
                    "start": axis_range[0],
                    "end": axis_range[1],
                    "frames": axis_range[1] - axis_range[0] + 1,
                    "source_frame_start": source_frame_start,
                    "source_frame_end": source_frame_end,
                    "tc_start": tc_range[0] if tc_range else None,
                    "tc_end": tc_range[1] if tc_range else None,
                    "rdt_tc_start": rdt_tc_range[0] if rdt_tc_range else None,
                    "rdt_tc_end": rdt_tc_range[1] if rdt_tc_range else None,
                }
            )
        if len(rows) == source_rows_before:
            failures.append(
                {
                    "source": source.path.name,
                    "reason": f"no usable {basis} DVRescue frame segment for rough master planning",
                }
            )
    if failures:
        return {
            "ok": False,
            "basis": basis,
            "reason": "one or more sources could not be placed on the v2 master stitch axis",
            "failures": failures,
            "candidate_rows": [v2_row_for_report(row) for row in rows],
        }
    rows.sort(key=lambda item: (item["start"], item["end"], item["source"].path.name, item["segment_index"]))
    return {"ok": True, "basis": basis, "rows": rows, "candidate_rows": [v2_row_for_report(row) for row in rows]}


def first_timecode_anchor_from_meta(meta: dict[str, Any]) -> tuple[int | None, Any]:
    first_tc = meta.get("first_tc")
    parsed = parse_timecode(first_tc)
    if parsed is not None:
        return parsed, first_tc
    for segment in meta.get("frames_segments") or []:
        segment_tc = segment.get("first_tc")
        parsed = parse_timecode(segment_tc)
        if parsed is not None:
            return parsed, segment_tc
        parsed = int_or_none(segment.get("first_tc_frame"))
        if parsed is not None:
            return parsed, segment_tc
    return None, None


def timecode_count_ranges_from_meta(meta: dict[str, Any]) -> list[tuple[int, int]]:
    count = int_or_none(meta.get("frames_count"))
    start, _ = first_timecode_anchor_from_meta(meta)
    if count is None or count <= 0 or start is None:
        return []
    return [(start, start + count - 1)]


def build_v2_master_timecode_count_rows(
    sources: list[SourceInfo],
    max_gap_frames: int,
) -> dict[str, Any]:
    """Place each source by first DV timecode plus raw frame count.

    For stitching, a damaged last_tc/last_rdt tail must not move the seam. Once
    v2 has clustered sources by recording date/time, the local stitch coordinate
    is DV timecode; the safest range is first_tc plus the extracted raw frame
    count.
    """
    _ = max_gap_frames
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for source in sources:
        if source.raw_path is None or not source.raw_path.exists():
            failures.append({"source": source.path.name, "reason": "missing extracted raw DV payload"})
            continue
        raw_frames = source.raw_frames or 0
        if raw_frames <= 0:
            failures.append({"source": source.path.name, "reason": "raw DV payload has no full PAL DV frames"})
            continue
        meta = source.raw_dvrescue or source.dvrescue
        tc_start, tc_value = first_timecode_anchor_from_meta(meta)
        if tc_start is None:
            failures.append(
                {
                    "source": source.path.name,
                    "reason": "no usable first DV timecode anchor for rough master planning",
                }
            )
            continue
        rdt_tc_start = datetime_timecode_frame(meta.get("first_rdt"), tc_value)
        rows.append(
            {
                "source": source,
                "segment_index": 0,
                "start": tc_start,
                "end": tc_start + raw_frames - 1,
                "frames": raw_frames,
                "source_frame_start": 0,
                "source_frame_end": raw_frames - 1,
                "tc_start": tc_start,
                "tc_end": tc_start + raw_frames - 1,
                "rdt_tc_start": rdt_tc_start,
                "rdt_tc_end": rdt_tc_start + raw_frames - 1 if rdt_tc_start is not None else None,
                "placement_mode": "first_tc_plus_raw_frame_count",
            }
        )
    rows.sort(key=lambda item: (item["start"], item["end"], item["source"].path.name, item["segment_index"]))
    payload = {
        "ok": not failures,
        "basis": "timecode",
        "placement_mode": "first_tc_plus_raw_frame_count",
        "candidate_rows": [v2_row_for_report(row) for row in rows],
    }
    if failures:
        payload.update(
            {
                "reason": "one or more sources could not be placed on the v2 master timecode stitch axis",
                "failures": failures,
            }
        )
    else:
        payload["rows"] = rows
    return payload


def v2_timecode_loop_collisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collisions: list[dict[str, Any]] = []
    for i, left in enumerate(rows):
        left_tc = clean_range(int_or_none(left.get("tc_start")), int_or_none(left.get("tc_end")))
        left_rdt = clean_range(int_or_none(left.get("rdt_tc_start")), int_or_none(left.get("rdt_tc_end")))
        if left_tc is None:
            continue
        for right in rows[i + 1:]:
            right_tc = clean_range(int_or_none(right.get("tc_start")), int_or_none(right.get("tc_end")))
            right_rdt = clean_range(int_or_none(right.get("rdt_tc_start")), int_or_none(right.get("rdt_tc_end")))
            if right_tc is None or not ranges_overlap([left_tc], [right_tc]):
                continue
            recording_datetime_overlap = sources_have_recording_datetime_overlap(left["source"], right["source"])
            if recording_datetime_overlap:
                # Same recording-time window + overlapping timecode means
                # duplicate/alternate capture evidence, not a timecode reset.
                continue
            rdt_overlap = left_rdt is not None and right_rdt is not None and ranges_overlap([left_rdt], [right_rdt])
            if rdt_overlap:
                continue
            overlap_start = max(left_tc[0], right_tc[0])
            overlap_end = min(left_tc[1], right_tc[1])
            collisions.append(
                {
                    "left_source": left["source"].path.name,
                    "left_segment_index": left["segment_index"],
                    "right_source": right["source"].path.name,
                    "right_segment_index": right["segment_index"],
                    "timecode_overlap": {
                        "start": overlap_start,
                        "end": overlap_end,
                        "frames": overlap_end - overlap_start + 1,
                    },
                    "left_recording_datetime_timecode": (
                        {"start": left_rdt[0], "end": left_rdt[1]} if left_rdt else None
                    ),
                    "right_recording_datetime_timecode": (
                        {"start": right_rdt[0], "end": right_rdt[1]} if right_rdt else None
                    ),
                    "recording_datetime_overlap": recording_datetime_overlap,
                    "reason": (
                        "plain DV timecode overlaps while recording datetime windows are distinct; "
                        "this is a timecode loop/reset collision"
                    ),
                }
            )
    return collisions


def v2_candidate_quality_key(row: dict[str, Any], interval_start: int, interval_end: int) -> tuple[Any, ...]:
    source = row["source"]
    source_frame_start = row["source_frame_start"] + (interval_start - row["start"])
    source_frame_end = source_frame_start + (interval_end - interval_start)
    counts = source_problem_counts_for_frame_span(source, source_frame_start, source_frame_end)
    return (
        counts["media_problem_frames"],
        counts["audio_problem_frames"],
        counts["video_problem_frames"],
        counts["problem_frames"],
        -source_usable_content_frames(source),
        -source.good_frame_count,
        -(source.raw_frames or source.frame_count),
        source.issue_count,
        source.audio_issue_count,
        source.video_issue_count,
        source.path.name,
        row["segment_index"],
    )


def v2_master_chunk_from_row(row: dict[str, Any], interval_start: int, interval_end: int) -> dict[str, Any]:
    source = row["source"]
    frames = interval_end - interval_start + 1
    source_frame_start = row["source_frame_start"] + (interval_start - row["start"])
    source_frame_end = source_frame_start + frames - 1
    problem_counts = source_problem_counts_for_frame_span(source, source_frame_start, source_frame_end)
    return {
        "type": "chunk",
        "source": source.path.name,
        "source_segment_index": row["segment_index"],
        "axis_start": interval_start,
        "axis_end": interval_end,
        "source_frame_start": source_frame_start,
        "source_frame_end": source_frame_end,
        "frames": frames,
        "problem_counts": problem_counts,
    }


def coalesce_v2_master_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    coalesced: list[dict[str, Any]] = []
    for chunk in chunks:
        if (
            coalesced
            and coalesced[-1]["source"] == chunk["source"]
            and coalesced[-1]["source_frame_end"] + 1 == chunk["source_frame_start"]
            and coalesced[-1]["axis_end"] + 1 == chunk["axis_start"]
        ):
            previous = coalesced[-1]
            previous["axis_end"] = chunk["axis_end"]
            previous["source_frame_end"] = chunk["source_frame_end"]
            previous["frames"] += chunk["frames"]
            for key, value in chunk["problem_counts"].items():
                previous["problem_counts"][key] = int(previous["problem_counts"].get(key) or 0) + int(value or 0)
            continue
        coalesced.append(dict(chunk))
    return coalesced


def plan_v2_master_first_raw(
    sources: list[SourceInfo],
    max_gap_frames: int,
) -> dict[str, Any]:
    if not sources:
        return {"ok": False, "reason": "no active sources are available for v2 master planning"}
    strict_timecode_rows = build_v2_master_candidate_rows(sources, "timecode", max_gap_frames)
    timecode_rows = build_v2_master_timecode_count_rows(sources, max_gap_frames)
    if not timecode_rows["ok"]:
        return {
            "ok": False,
            "kind": "v2_master_first_raw_plan",
            "reason": "one or more sources could not be placed on the DV timecode stitch axis",
            "timecode_row_build": timecode_rows,
            "strict_timecode_row_build": strict_timecode_rows,
            "recording_datetime_timecode_row_build": None,
        }

    rows = timecode_rows["rows"]
    basis = "timecode"
    loop_collisions = v2_timecode_loop_collisions(rows)
    basis_note = (
        "v2 clusters by embedded recording date/time, then stitches only on DV timecode. "
        "Each source is placed by first_tc plus extracted raw DV frame count so damaged "
        "last_tc/last_rdt tails cannot move the seam."
    )
    if loop_collisions:
        return {
            "ok": False,
            "kind": "v2_master_first_raw_plan",
            "reason": (
                "DV timecode has loop/reset collisions inside this recording-time cluster; "
                "refusing to stitch on recording datetime because v2 stitching is timecode-only"
            ),
            "basis": basis,
            "basis_note": basis_note,
            "timecode_loop_collisions": loop_collisions[:30],
            "timecode_loop_collision_count": len(loop_collisions),
            "timecode_row_build": timecode_rows,
            "strict_timecode_row_build": strict_timecode_rows,
            "recording_datetime_timecode_row_build": None,
        }

    union_ranges = normalize_ranges([(row["start"], row["end"]) for row in rows])
    if not union_ranges:
        return {
            "ok": False,
            "kind": "v2_master_first_raw_plan",
            "reason": "no source ranges are available on the v2 master stitch axis",
            "basis": basis,
        }

    planned_chunks: list[dict[str, Any]] = []
    uncovered_intervals: list[dict[str, Any]] = []
    for union_start, union_end in union_ranges:
        points = {union_start, union_end + 1}
        for row in rows:
            if row["end"] < union_start or row["start"] > union_end:
                continue
            points.add(max(row["start"], union_start))
            points.add(min(row["end"], union_end) + 1)
        ordered_points = sorted(points)
        for left, right in zip(ordered_points, ordered_points[1:]):
            interval_start = left
            interval_end = right - 1
            if interval_start > interval_end:
                continue
            candidates = [
                row
                for row in rows
                if row["start"] <= interval_start and row["end"] >= interval_end
            ]
            if not candidates:
                uncovered_intervals.append(
                    {"start": interval_start, "end": interval_end, "frames": interval_end - interval_start + 1}
                )
                continue
            selected = min(candidates, key=lambda row: v2_candidate_quality_key(row, interval_start, interval_end))
            chunk = v2_master_chunk_from_row(selected, interval_start, interval_end)
            chunk["overlap_candidate_count"] = len(candidates)
            if len(candidates) > 1:
                chunk["overlap_candidates"] = [
                    {
                        "source": row["source"].path.name,
                        "source_segment_index": row["segment_index"],
                        "quality_key": list(v2_candidate_quality_key(row, interval_start, interval_end)),
                    }
                    for row in sorted(candidates, key=lambda row: v2_candidate_quality_key(row, interval_start, interval_end))[:10]
                ]
            planned_chunks.append(chunk)
    if uncovered_intervals:
        return {
            "ok": False,
            "kind": "v2_master_first_raw_plan",
            "reason": "the v2 master stitch axis has intervals with no covering source segment",
            "basis": basis,
            "uncovered_intervals": uncovered_intervals[:30],
            "uncovered_interval_count": len(uncovered_intervals),
        }

    chunks = coalesce_v2_master_chunks(planned_chunks)
    gaps: list[dict[str, Any]] = []
    write_items: list[dict[str, Any]] = []
    synthetic_hold_frames = 0
    for index, chunk in enumerate(chunks):
        write_items.append(chunk)
        if index + 1 >= len(chunks):
            continue
        next_chunk = chunks[index + 1]
        gap_frames = int(next_chunk["axis_start"]) - int(chunk["axis_end"]) - 1
        if gap_frames <= 0:
            continue
        gap = {
            "type": "synthetic_hold_gap",
            "previous_source": chunk["source"],
            "next_source": next_chunk["source"],
            "axis_start": int(chunk["axis_end"]) + 1,
            "axis_end": int(next_chunk["axis_start"]) - 1,
            "gap_frames": gap_frames,
            "gap_seconds": gap_frames / DV_FPS,
            "hold_source": chunk["source"],
            "hold_source_frame": chunk["source_frame_end"],
            "basis": basis,
        }
        if gap_frames > max_gap_frames:
            return {
                "ok": False,
                "kind": "v2_master_first_raw_plan",
                "reason": f"gap of {gap_frames} frame(s) exceeds v2 rough master limit {max_gap_frames}",
                "basis": basis,
                "gap": gap,
                "chunks": chunks,
            }
        gaps.append(gap)
        write_items.append(gap)
        synthetic_hold_frames += gap_frames

    source_frames = sum(int(chunk["frames"]) for chunk in chunks)
    return {
        "ok": True,
        "kind": "v2_master_first_raw_plan",
        "basis": basis,
        "basis_note": basis_note,
        "placement_mode": timecode_rows.get("placement_mode"),
        "allow_first_anchor_count_validation": True,
        "max_gap_frames": max_gap_frames,
        "max_gap_seconds": max_gap_frames / DV_FPS,
        "timecode_loop_collisions": loop_collisions[:30],
        "timecode_loop_collision_count": len(loop_collisions),
        "timecode_row_build": (
            {
                "basis": "timecode",
                "candidate_rows": timecode_rows["candidate_rows"],
            }
            if timecode_rows.get("ok")
            else timecode_rows
        ),
        "strict_timecode_row_build": (
            {
                "basis": "timecode",
                "candidate_rows": strict_timecode_rows["candidate_rows"],
            }
            if strict_timecode_rows.get("ok")
            else strict_timecode_rows
        ),
        "recording_datetime_timecode_row_build": None,
        "candidate_rows": [v2_row_for_report(row) for row in rows],
        "required_ranges": [
            {"start": start, "end": end, "frames": end - start + 1}
            for start, end in union_ranges
        ],
        "required_frames": range_total(union_ranges),
        "chunks": chunks,
        "gaps": gaps,
        "write_items": write_items,
        "source_frames": source_frames,
        "synthetic_hold_frames": synthetic_hold_frames,
        "expected_output_frames": source_frames + synthetic_hold_frames,
        "overlap_intervals_selected": sum(1 for chunk in chunks if int(chunk.get("overlap_candidate_count") or 0) > 1),
        "ordered_sources": sorted({chunk["source"] for chunk in chunks}),
    }


def copy_raw_dv_frame_range(src: Path, dst_handle: Any, start_frame: int, frame_count: int) -> None:
    if frame_count <= 0:
        return
    with src.open("rb") as handle:
        handle.seek(start_frame * RAW_DV_FRAME_BYTES_PAL)
        remaining = frame_count * RAW_DV_FRAME_BYTES_PAL
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError(f"Unexpected end of raw DV payload while reading {src}")
            dst_handle.write(chunk)
            remaining -= len(chunk)


def read_raw_dv_frame_at(path: Path, frame_index: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(frame_index * RAW_DV_FRAME_BYTES_PAL)
        frame = handle.read(RAW_DV_FRAME_BYTES_PAL)
    if len(frame) != RAW_DV_FRAME_BYTES_PAL:
        raise RuntimeError(f"Could not read frame {frame_index} from {path}")
    return frame


def write_v2_master_raw(
    plan: dict[str, Any],
    sources: list[SourceInfo],
    out_path: Path,
    force: bool,
) -> dict[str, Any]:
    out_path = require_inside_target(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    source_by_name = {source.path.name: source for source in sources}
    expected_bytes = int(plan["expected_output_frames"]) * RAW_DV_FRAME_BYTES_PAL
    result: dict[str, Any] = {
        "kind": "raw_dv_v2_master_first_stitch",
        "output": str(out_path),
        "expected_bytes": expected_bytes,
        "reused_existing_output": False,
    }
    if not force and out_path.exists() and out_path.stat().st_size == expected_bytes:
        result.update({"exit_code": 0, "bytes": out_path.stat().st_size, "reused_existing_output": True})
        return result

    tmp = out_path.with_name(f"{out_path.stem}.tmp{out_path.suffix}")
    delete_file_inside_target(tmp)
    try:
        with tmp.open("wb") as output:
            for item in plan["write_items"]:
                if item["type"] == "chunk":
                    source = source_by_name[item["source"]]
                    if source.raw_path is None:
                        raise RuntimeError(f"{source.path.name} has no raw DV payload")
                    copy_raw_dv_frame_range(
                        source.raw_path,
                        output,
                        int(item["source_frame_start"]),
                        int(item["frames"]),
                    )
                elif item["type"] == "synthetic_hold_gap":
                    source = source_by_name[item["hold_source"]]
                    if source.raw_path is None:
                        raise RuntimeError(f"{source.path.name} has no raw DV payload")
                    hold_frame = read_raw_dv_frame_at(source.raw_path, int(item["hold_source_frame"]))
                    for _ in range(int(item["gap_frames"])):
                        output.write(hold_frame)
        if tmp.stat().st_size != expected_bytes:
            raise RuntimeError(f"v2 master raw size mismatch: wrote {tmp.stat().st_size}, expected {expected_bytes}")
        if out_path.exists():
            delete_file_inside_target(out_path)
        shutil.move(str(tmp), str(out_path))
        result.update({"exit_code": 0, "bytes": out_path.stat().st_size})
        return result
    except Exception as exc:
        delete_file_inside_target(tmp)
        result.update({"exit_code": 1, "error": str(exc)})
        return result


def source_info_for_raw_master(
    raw_path: Path,
    *,
    key: str,
    cassette: str | None,
    dv_meta: dict[str, Any],
) -> SourceInfo:
    raw_bytes = raw_path.stat().st_size if raw_path.exists() else 0
    raw_frames = raw_bytes // RAW_DV_FRAME_BYTES_PAL
    first_rdt = parse_dv_datetime(dv_meta.get("first_rdt"))
    return SourceInfo(
        path=raw_path,
        stem=safe_name(key),
        version=None,
        cassette=cassette,
        stamp=first_rdt,
        bytes=raw_bytes,
        ffprobe={},
        dvrescue=dv_meta,
        raw_path=raw_path,
        raw_bytes=raw_bytes,
        raw_frames=raw_frames,
        raw_ffprobe={},
        raw_dvrescue=dv_meta,
    )


def validate_meta_covers_ranges(
    *,
    label: str,
    output_meta: dict[str, Any],
    basis: str,
    required_ranges: list[tuple[int, int]],
    allow_first_anchor_count: bool = False,
) -> dict[str, Any]:
    actual = (
        v2_validation_ranges_from_meta(output_meta, basis)
        if allow_first_anchor_count
        else coverage_ranges_from_meta(output_meta, basis)
    )
    missing = coverage_deficit(required_ranges, actual, SEAM_TOLERANCE_FRAMES)
    return {
        "label": label,
        "ok": not missing,
        "basis": basis,
        "validation_mode": (
            "first_anchor_plus_frame_count"
            if allow_first_anchor_count and basis in {"recording_datetime_timecode", "timecode"}
            else "strict_embedded_boundaries"
        ),
        "issues": [] if not missing else [f"{label} does not cover all planned v2 master {basis} ranges"],
        "required": compact_ranges_for_json(required_ranges),
        "actual": compact_ranges_for_json(actual),
        "missing": compact_ranges_for_json(missing),
        "tolerance_frames": SEAM_TOLERANCE_FRAMES,
    }


def validate_v2_master_boundary(label: str, output_meta: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    chunks = plan.get("chunks") or []
    if not chunks:
        return {"label": label, "ok": False, "issues": ["v2 master plan has no chunks"], "comparisons": []}
    basis = plan["basis"]
    expected_first = int(chunks[0]["axis_start"])
    expected_last = int(chunks[-1]["axis_end"])
    if basis == "timecode":
        actual_first = parse_timecode(output_meta.get("first_tc"))
        actual_last = parse_timecode(output_meta.get("last_tc"))
        frame_count = int_or_none(output_meta.get("frames_count"))
        actual_last_by_count = actual_first + frame_count - 1 if actual_first is not None and frame_count else None
    elif basis == "recording_datetime_timecode":
        actual_first = datetime_timecode_frame(output_meta.get("first_rdt"), output_meta.get("first_tc"))
        actual_last = datetime_timecode_frame(output_meta.get("last_rdt"), output_meta.get("last_tc"))
        frame_count = int_or_none(output_meta.get("frames_count"))
        actual_last_by_count = actual_first + frame_count - 1 if actual_first is not None and frame_count else None
    else:
        actual_first = None
        actual_last = None
        actual_last_by_count = None
    comparisons = [
        {"key": "first_axis_frame", "expected": expected_first, "actual": actual_first},
        {"key": "last_axis_frame", "expected": expected_last, "actual": actual_last},
    ]
    if basis in {"recording_datetime_timecode", "timecode"}:
        comparisons.append(
            {
                "key": "last_axis_frame_by_first_anchor_plus_frame_count",
                "expected": expected_last,
                "actual": actual_last_by_count,
            }
        )
    issues: list[str] = []
    if actual_first != expected_first:
        issues.append(f"{label} first {basis} frame is {actual_first}, expected {expected_first}")
    last_matches = actual_last == expected_last or (
        basis in {"recording_datetime_timecode", "timecode"} and actual_last_by_count == expected_last
    )
    if not last_matches:
        issues.append(f"{label} last {basis} frame is {actual_last}, expected {expected_last}")
    return {"label": label, "ok": not issues, "basis": basis, "issues": issues, "comparisons": comparisons}


def merge_raw_dv_exact_order(
    raw_inputs: list[Path],
    input_labels: list[str],
    out_path: Path,
    log_path: Path,
    force: bool,
) -> dict[str, Any]:
    args: list[str | Path] = [DVRESCUE]
    args.extend(raw_inputs)
    args.extend(
        [
            "--merge",
            out_path,
            "--merge-log",
            log_path,
            "--merge-log-missing",
            "--merge-output-concealed",
        ]
    )
    if force or not out_path.exists():
        proc = run_cmd(args, timeout=1800)
    else:
        proc = subprocess.CompletedProcess(args=[str(a) for a in args], returncode=0, stdout="", stderr="")
    return {
        "command": [str(a) for a in args],
        "input_order": input_labels,
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout.splitlines()[-20:],
        "stderr_tail": proc.stderr.splitlines()[-20:],
        "output": str(out_path),
        "merge_log": str(log_path),
    }


def try_v2_improve_master_with_dvrescue(
    *,
    key: str,
    merge_dir: Path,
    rough_master_raw: Path,
    rough_master_meta: dict[str, Any],
    active_sources: list[SourceInfo],
    plan: dict[str, Any],
    force: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "attempted": False,
        "accepted": False,
        "kind": "v2_master_first_dvrescue_improvement",
        "reason": None,
    }
    alternate_sources = [source for source in ordered_merge_sources(active_sources) if source.raw_path is not None]
    if not alternate_sources:
        result["reason"] = "no extracted source alternates are available for DVRescue improvement"
        return result
    raw_inputs = [rough_master_raw] + [source.raw_path for source in alternate_sources if source.raw_path is not None]
    input_labels = [f"{rough_master_raw.name} (rough v2 master baseline)"] + [
        source.path.name for source in alternate_sources if source.raw_path is not None
    ]
    improved_dv = merge_dir / f"{key}.v2-master-dvrescue-improved.dv"
    improved_log = merge_dir / f"{key}.v2-master-dvrescue-improved.merge-log.txt"
    merge_result = merge_raw_dv_exact_order(raw_inputs, input_labels, improved_dv, improved_log, force=force)
    improved_meta = analyze_dvrescue(improved_dv) if improved_dv.exists() else {}
    improved_frames = int(improved_meta.get("frames_count") or 0)
    rough_frames = int(rough_master_meta.get("frames_count") or 0)
    rough_issues = int(rough_master_meta.get("problem_frame_count") or 0)
    rough_audio_issues = int(rough_master_meta.get("audio_problem_frame_count") or 0)
    rough_video_issues = int(rough_master_meta.get("video_problem_frame_count") or 0)
    improved_issues = int(improved_meta.get("problem_frame_count") or 0)
    improved_audio_issues = int(improved_meta.get("audio_problem_frame_count") or 0)
    improved_video_issues = int(improved_meta.get("video_problem_frame_count") or 0)
    required_ranges = report_ranges_to_tuples(plan.get("required_ranges"))
    cassette = next((source.cassette for source in active_sources if source.cassette), None)
    rough_master_source = source_info_for_raw_master(
        rough_master_raw,
        key=f"{key}.v2-rough-master",
        cassette=cassette,
        dv_meta=rough_master_meta,
    )
    metadata_validation = validate_core_metadata(
        label="v2 DVRescue-improved raw master",
        output_meta=improved_meta,
        primary=rough_master_source,
        sources=[rough_master_source],
    )
    coverage_validation = validate_meta_covers_ranges(
        label="v2 DVRescue-improved raw master",
        output_meta=improved_meta,
        basis=plan["basis"],
        required_ranges=required_ranges,
        allow_first_anchor_count=bool(plan.get("allow_first_anchor_count_validation")),
    )
    boundary_validation = validate_v2_master_boundary("v2 DVRescue-improved raw master", improved_meta, plan)
    media_validation = {
        "ok": True,
        "issues": [],
        "rough_master_problem_frames": rough_issues,
        "improved_problem_frames": improved_issues,
        "rough_master_audio_problem_frames": rough_audio_issues,
        "improved_audio_problem_frames": improved_audio_issues,
        "rough_master_video_problem_frames": rough_video_issues,
        "improved_video_problem_frames": improved_video_issues,
    }
    if improved_issues > rough_issues:
        media_validation["issues"].append(
            f"improved master has more problem frames ({improved_issues}) than rough master ({rough_issues})"
        )
    if improved_audio_issues > rough_audio_issues:
        media_validation["issues"].append(
            "improved master has more audio problem frames "
            f"({improved_audio_issues}) than rough master ({rough_audio_issues})"
        )
    if improved_video_issues > rough_video_issues:
        media_validation["issues"].append(
            "improved master has more video problem frames "
            f"({improved_video_issues}) than rough master ({rough_video_issues})"
        )
    media_validation["ok"] = not media_validation["issues"]

    rejected_reasons: list[str] = []
    if merge_result["exit_code"] != 0:
        rejected_reasons.append("DVRescue improvement returned non-zero exit code")
    if improved_frames <= 0:
        rejected_reasons.append("DVRescue improvement has no analyzable DV frames")
    if improved_frames != rough_frames:
        rejected_reasons.append(
            f"DVRescue improvement frame count {improved_frames} differs from rough master {rough_frames}"
        )
    if not metadata_validation["ok"]:
        rejected_reasons.extend(metadata_validation["issues"])
    if not coverage_validation["ok"]:
        rejected_reasons.extend(coverage_validation["issues"])
    if not boundary_validation["ok"]:
        rejected_reasons.extend(boundary_validation["issues"])
    rejected_reasons.extend(media_validation["issues"])
    result.update(
        {
            "attempted": True,
            "merge_result": merge_result,
            "merge_log_text": read_text_if_exists(improved_log),
            "improved_dvrescue": improved_meta,
            "metadata_validation": metadata_validation,
            "coverage_validation": coverage_validation,
            "boundary_validation": boundary_validation,
            "media_problem_validation": media_validation,
            "rejected_reasons": rejected_reasons,
        }
    )
    if rejected_reasons:
        result["reason"] = "DVRescue did not produce a safe improvement over the rough v2 master"
        return result

    result.update(
        {
            "accepted": True,
            "output": str(improved_dv),
            "reason": "DVRescue output matched the rough master timeline and did not increase detected problems",
        }
    )
    return result


def compact_v2_master_plan_for_metadata(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": plan.get("kind"),
        "basis": plan.get("basis"),
        "basis_note": plan.get("basis_note"),
        "placement_mode": plan.get("placement_mode"),
        "allow_first_anchor_count_validation": plan.get("allow_first_anchor_count_validation"),
        "required_frames": plan.get("required_frames"),
        "source_frames": plan.get("source_frames"),
        "synthetic_hold_frames": plan.get("synthetic_hold_frames"),
        "expected_output_frames": plan.get("expected_output_frames"),
        "chunk_count": len(plan.get("chunks") or []),
        "gap_count": len(plan.get("gaps") or []),
        "timecode_loop_collision_count": plan.get("timecode_loop_collision_count"),
        "overlap_intervals_selected": plan.get("overlap_intervals_selected"),
    }


def create_v2_master_first_output(
    *,
    key: str,
    keep_dir: Path,
    merge_dir: Path,
    active_sources: list[SourceInfo],
    max_gap_frames: int,
    force: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "attempted": False,
        "accepted": False,
        "kind": "v2_master_first",
        "reason": None,
    }
    plan = plan_v2_master_first_raw(active_sources, max_gap_frames)
    result["plan"] = plan
    if not plan["ok"]:
        result["reason"] = plan.get("reason")
        return result

    result["attempted"] = True
    rough_master_raw = merge_dir / f"{key}.v2-rough-master.dv"
    raw_write = write_v2_master_raw(plan, active_sources, rough_master_raw, force=force)
    result["raw_write"] = raw_write
    if raw_write.get("exit_code") != 0:
        result["reason"] = "rough v2 raw master write failed"
        return result

    rough_meta = analyze_dvrescue(rough_master_raw)
    rough_frames = int(rough_meta.get("frames_count") or 0)
    expected_frames = int(plan["expected_output_frames"])
    required_ranges = report_ranges_to_tuples(plan.get("required_ranges"))
    rough_frame_validation = {
        "ok": rough_frames == expected_frames,
        "expected_output_frames": expected_frames,
        "rough_master_frames": rough_frames,
    }
    rough_coverage_validation = validate_meta_covers_ranges(
        label="rough v2 raw master",
        output_meta=rough_meta,
        basis=plan["basis"],
        required_ranges=required_ranges,
        allow_first_anchor_count=bool(plan.get("allow_first_anchor_count_validation")),
    )
    rough_boundary_validation = validate_v2_master_boundary("rough v2 raw master", rough_meta, plan)
    result["rough_master_dvrescue"] = rough_meta
    result["rough_master_validation"] = {
        "frame_validation": rough_frame_validation,
        "coverage_validation": rough_coverage_validation,
        "boundary_validation": rough_boundary_validation,
    }
    if not rough_frame_validation["ok"] or not rough_coverage_validation["ok"] or not rough_boundary_validation["ok"]:
        result["reason"] = "rough v2 raw master failed validation"
        return result

    improvement = try_v2_improve_master_with_dvrescue(
        key=key,
        merge_dir=merge_dir,
        rough_master_raw=rough_master_raw,
        rough_master_meta=rough_meta,
        active_sources=active_sources,
        plan=plan,
        force=force,
    )
    result["dvrescue_improvement"] = improvement
    if improvement.get("accepted"):
        selected_raw = Path(improvement["output"])
        selected_meta = improvement.get("improved_dvrescue") or analyze_dvrescue(selected_raw)
        selected_kind = "avi_wrapped_from_raw_dv_v2_master_dvrescue_improved"
        selected_reason = improvement["reason"]
    else:
        selected_raw = rough_master_raw
        selected_meta = rough_meta
        selected_kind = "avi_wrapped_from_raw_dv_v2_rough_master"
        selected_reason = "DVRescue improvement was rejected or unavailable; rough v2 master selected"

    cassette = next((source.cassette for source in active_sources if source.cassette), None)
    output_stem = professional_output_stem(
        cassette=cassette,
        dv_meta=selected_meta,
        fallback_key=key,
    )
    candidate_avi = merge_dir / f"{key}.v2-master.candidate.avi"
    final_avi = reserve_keep_output_path(keep_dir / f"{output_stem}.avi")
    output_stem = final_avi.stem
    extra_payload = {
        "derived_output": True,
        "derived_output_reason": "v2_master_first_rough_timecode_stitch_then_optional_dvrescue_improvement",
        "v2_master_first": compact_v2_master_plan_for_metadata(plan),
        "dvrescue_improvement": {
            "attempted": improvement.get("attempted"),
            "accepted": improvement.get("accepted"),
            "reason": improvement.get("reason"),
        },
    }
    metadata_tags = build_avi_metadata_tags(
        output_key=output_stem,
        cassette=cassette,
        source_files=[source.path.name for source in active_sources],
        keep_kind=selected_kind,
        dv_meta=selected_meta,
        extra_payload=extra_payload,
    )
    wrap_result = wrap_dv_to_avi(
        selected_raw,
        candidate_avi,
        force=force,
        metadata_tags=metadata_tags,
        validation_dir=merge_dir / "metadata_validation_v2_master_first",
    )
    wrapped_meta = analyze_dvrescue(candidate_avi) if candidate_avi.exists() else {}
    wrapped_frames = int(wrapped_meta.get("frames_count") or 0)
    selected_frames = int(selected_meta.get("frames_count") or 0)
    wrapped_frame_validation = {
        "ok": wrapped_frames == selected_frames == expected_frames,
        "expected_output_frames": expected_frames,
        "selected_raw_frames": selected_frames,
        "wrapped_frames": wrapped_frames,
    }
    wrapped_coverage_validation = validate_meta_covers_ranges(
        label="wrapped v2 master AVI",
        output_meta=wrapped_meta,
        basis=plan["basis"],
        required_ranges=required_ranges,
        allow_first_anchor_count=bool(plan.get("allow_first_anchor_count_validation")),
    )
    wrapped_boundary_validation = validate_v2_master_boundary("wrapped v2 master AVI", wrapped_meta, plan)
    wrapped_issues = int(wrapped_meta.get("problem_frame_count") or 0)
    selected_issues = int(selected_meta.get("problem_frame_count") or 0)
    wrapped_audio_issues = int(wrapped_meta.get("audio_problem_frame_count") or 0)
    selected_audio_issues = int(selected_meta.get("audio_problem_frame_count") or 0)
    wrapped_video_issues = int(wrapped_meta.get("video_problem_frame_count") or 0)
    selected_video_issues = int(selected_meta.get("video_problem_frame_count") or 0)
    wrapped_media_validation = {
        "ok": True,
        "issues": [],
        "selected_raw_problem_frames": selected_issues,
        "wrapped_problem_frames": wrapped_issues,
        "selected_raw_audio_problem_frames": selected_audio_issues,
        "wrapped_audio_problem_frames": wrapped_audio_issues,
        "selected_raw_video_problem_frames": selected_video_issues,
        "wrapped_video_problem_frames": wrapped_video_issues,
    }
    if wrapped_issues > selected_issues:
        wrapped_media_validation["issues"].append(
            f"wrapped v2 master has more problem frames ({wrapped_issues}) than selected raw ({selected_issues})"
        )
    if wrapped_audio_issues > selected_audio_issues:
        wrapped_media_validation["issues"].append(
            "wrapped v2 master has more audio problem frames "
            f"({wrapped_audio_issues}) than selected raw ({selected_audio_issues})"
        )
    if wrapped_video_issues > selected_video_issues:
        wrapped_media_validation["issues"].append(
            "wrapped v2 master has more video problem frames "
            f"({wrapped_video_issues}) than selected raw ({selected_video_issues})"
        )
    wrapped_media_validation["ok"] = not wrapped_media_validation["issues"]

    wrap_rejected_reasons: list[str] = []
    if wrap_result.get("exit_code") != 0:
        wrap_rejected_reasons.append("v2 master AVI wrap returned non-zero exit code")
    if not wrapped_frame_validation["ok"]:
        wrap_rejected_reasons.append("wrapped v2 master AVI frame count did not match selected raw master")
    if not wrapped_coverage_validation["ok"]:
        wrap_rejected_reasons.extend(wrapped_coverage_validation["issues"])
    if not wrapped_boundary_validation["ok"]:
        wrap_rejected_reasons.extend(wrapped_boundary_validation["issues"])
    wrap_rejected_reasons.extend(wrapped_media_validation["issues"])
    result.update(
        {
            "selected_raw": str(selected_raw),
            "selected_raw_dvrescue": selected_meta,
            "wrap_result": wrap_result,
            "wrapped_dvrescue": wrapped_meta,
            "wrapped_frame_validation": wrapped_frame_validation,
            "wrapped_coverage_validation": wrapped_coverage_validation,
            "wrapped_boundary_validation": wrapped_boundary_validation,
            "wrapped_media_problem_validation": wrapped_media_validation,
            "wrap_rejected_reasons": wrap_rejected_reasons,
        }
    )

    if not wrap_rejected_reasons:
        if final_avi.exists():
            delete_file_inside_target(final_avi)
        shutil.move(str(candidate_avi), str(final_avi))
        selected_keep = final_avi
        selected_keep_kind = selected_kind
        selected_keep_meta = wrapped_meta
        keep_ranges = report_ranges_to_tuples(plan.get("required_ranges"))
    else:
        raw_keep_kind = "raw_dv_v2_master_dvrescue_improved" if improvement.get("accepted") else "raw_dv_v2_rough_master"
        final_raw = reserve_keep_output_path(keep_dir / f"{output_stem}.dv")
        if final_raw.exists():
            delete_file_inside_target(final_raw)
        shutil.move(str(selected_raw), str(final_raw))
        selected_keep = final_raw
        selected_keep_kind = raw_keep_kind
        selected_keep_meta = selected_meta
        keep_ranges = report_ranges_to_tuples(plan.get("required_ranges"))
        result["avi_wrap_fallback"] = {
            "selected_raw_keep_file": str(final_raw),
            "reason": "AVI wrapping failed validation; the validated raw DV master was kept instead",
        }

    filesystem_timestamp = set_filesystem_recording_time(selected_keep, selected_keep_meta)
    result["filesystem_timestamp"] = filesystem_timestamp
    if not filesystem_timestamp["ok"]:
        result["reason"] = "filesystem timestamp update failed for v2 master"
        delete_file_inside_target(selected_keep)
        return result

    result.update(
        {
            "accepted": True,
            "output": str(selected_keep),
            "public_name": selected_keep.name,
            "public_name_basis": "embedded DV recording datetime, cassette id, and DV timecode start",
            "bytes": selected_keep.stat().st_size,
            "keep_kind": selected_keep_kind,
            "coverage_basis": plan["basis"],
            "coverage_ranges": keep_ranges,
            "reason": selected_reason,
        }
    )
    return result


def try_v2_selected_unit_master_merge(
    *,
    key: str,
    keep_dir: Path,
    merge_dir: Path,
    sources_to_merge: list[SourceInfo],
    validation_sources: list[SourceInfo],
    basis: str | None,
    boundary_metadata: dict[str, Any],
    force: bool,
) -> dict[str, Any]:
    """Transactional v2 retry using only coverage-preserving selected units.

    The first DVRescue merge still tries the full active cluster. If that fails,
    v2 can remove redundant/bad alternate captures from the input set and retry
    once with only the minimal units needed to cover the cluster. The candidate
    is accepted only if it validates against the full active source union.
    """
    result: dict[str, Any] = {
        "attempted": False,
        "accepted": False,
        "kind": "v2_selected_unit_master_merge",
        "sources_to_merge": [source.path.name for source in sources_to_merge],
        "validation_source_files": [source.path.name for source in validation_sources],
        "rejected_reasons": [],
    }
    if len(sources_to_merge) < 2:
        result["reason"] = "selected coverage set contains fewer than two merge inputs"
        return result
    if basis is None:
        result["reason"] = "no common coverage basis can validate a v2 single master"
        return result

    merge_primary = ordered_merge_sources(sources_to_merge)[0]
    merge_key = f"{key}.v2-selected-master"
    merged_dv = merge_dir / f"{merge_key}.merged.dv"
    merged_log = merge_dir / f"{merge_key}.merge-log.txt"
    merge_result = merge_raw_dv(sources_to_merge, merged_dv, merged_log, force=force)
    merged_dvrescue = analyze_dvrescue(merged_dv) if merged_dv.exists() else {}
    merged_frames = int(merged_dvrescue.get("frames_count") or 0)
    merged_issues = int(merged_dvrescue.get("problem_frame_count") or 0)
    merged_audio_issues = int(merged_dvrescue.get("audio_problem_frame_count") or 0)
    merged_video_issues = int(merged_dvrescue.get("video_problem_frame_count") or 0)
    best_input_issues = min(source.issue_count for source in validation_sources)
    best_input_audio_issues = min(source.audio_issue_count for source in validation_sources)
    best_input_video_issues = min(source.video_issue_count for source in validation_sources)

    result.update(
        {
            "attempted": True,
            "merge_result": merge_result,
            "merged_dvrescue": merged_dvrescue,
            "merge_log_text": read_text_if_exists(merged_log),
            "merged_problem_frames": merged_issues,
            "merged_audio_problem_frames": merged_audio_issues,
            "merged_video_problem_frames": merged_video_issues,
            "best_input_problem_frames": best_input_issues,
            "best_input_audio_problem_frames": best_input_audio_issues,
            "best_input_video_problem_frames": best_input_video_issues,
        }
    )

    metadata_validation = validate_core_metadata(
        label="v2 selected-unit merged raw DV",
        output_meta=merged_dvrescue,
        primary=merge_primary,
        sources=validation_sources,
        expected_overrides=boundary_metadata,
    )
    coverage_validation = validate_coverage(
        label="v2 selected-unit merged raw DV",
        output_meta=merged_dvrescue,
        sources=validation_sources,
        basis=basis,
    )
    media_problem_validation = {
        "ok": True,
        "issues": [],
        "best_input_problem_frames": best_input_issues,
        "best_input_audio_problem_frames": best_input_audio_issues,
        "best_input_video_problem_frames": best_input_video_issues,
        "merged_problem_frames": merged_issues,
        "merged_audio_problem_frames": merged_audio_issues,
        "merged_video_problem_frames": merged_video_issues,
    }
    if merged_issues > best_input_issues:
        media_problem_validation["issues"].append(
            f"v2 selected-unit merge has more problem frames ({merged_issues}) than best input ({best_input_issues})"
        )
    if merged_audio_issues > best_input_audio_issues:
        media_problem_validation["issues"].append(
            "v2 selected-unit merge has more audio problem frames "
            f"({merged_audio_issues}) than best input audio ({best_input_audio_issues})"
        )
    if merged_video_issues > best_input_video_issues:
        media_problem_validation["issues"].append(
            "v2 selected-unit merge has more video problem frames "
            f"({merged_video_issues}) than best input video ({best_input_video_issues})"
        )
    media_problem_validation["ok"] = not media_problem_validation["issues"]

    rejected_reasons: list[str] = []
    if merge_result["exit_code"] != 0:
        rejected_reasons.append("v2 selected-unit merge returned non-zero exit code")
    if merged_frames <= 0:
        rejected_reasons.append("v2 selected-unit merge has no analyzable DV frames")
    if not coverage_validation["ok"]:
        rejected_reasons.extend(coverage_validation["issues"])
    if not metadata_validation["ok"]:
        rejected_reasons.extend(metadata_validation["issues"])
    rejected_reasons.extend(media_problem_validation["issues"])
    result.update(
        {
            "metadata_validation": metadata_validation,
            "coverage_validation": coverage_validation,
            "media_problem_validation": media_problem_validation,
            "rejected_reasons": rejected_reasons,
        }
    )
    if rejected_reasons:
        result["reason"] = "v2 selected-unit merge failed transactional validation"
        return result

    output_stem = professional_output_stem(
        cassette=merge_primary.cassette,
        dv_meta=merged_dvrescue,
        fallback_key=merge_key,
    )
    candidate_avi = merge_dir / f"{merge_key}.candidate.avi"
    final_avi = reserve_keep_output_path(keep_dir / f"{output_stem}.avi")
    output_stem = final_avi.stem
    metadata_tags = build_avi_metadata_tags(
        output_key=output_stem,
        cassette=merge_primary.cassette,
        source_files=[source.path.name for source in validation_sources],
        keep_kind="avi_wrapped_from_raw_dv_v2_selected_unit_merge",
        dv_meta=merged_dvrescue,
    )
    wrap_result = wrap_dv_to_avi(
        merged_dv,
        candidate_avi,
        force=force,
        metadata_tags=metadata_tags,
        validation_dir=merge_dir / "metadata_validation_v2_selected_unit_merge",
    )
    wrapped_dvrescue = analyze_dvrescue(candidate_avi) if candidate_avi.exists() else {}
    wrapped_metadata_validation = validate_core_metadata(
        label="v2 selected-unit wrapped AVI",
        output_meta=wrapped_dvrescue,
        primary=merge_primary,
        sources=validation_sources,
        expected_overrides=boundary_metadata,
    )
    wrapped_coverage_validation = validate_coverage(
        label="v2 selected-unit wrapped AVI",
        output_meta=wrapped_dvrescue,
        sources=validation_sources,
        basis=basis,
    )
    wrapped_frames = int(wrapped_dvrescue.get("frames_count") or 0)
    wrapped_issues = int(wrapped_dvrescue.get("problem_frame_count") or 0)
    wrapped_audio_issues = int(wrapped_dvrescue.get("audio_problem_frame_count") or 0)
    wrapped_video_issues = int(wrapped_dvrescue.get("video_problem_frame_count") or 0)
    wrapped_media_validation = {
        "ok": True,
        "issues": [],
        "merged_problem_frames": merged_issues,
        "wrapped_problem_frames": wrapped_issues,
        "merged_audio_problem_frames": merged_audio_issues,
        "wrapped_audio_problem_frames": wrapped_audio_issues,
        "merged_video_problem_frames": merged_video_issues,
        "wrapped_video_problem_frames": wrapped_video_issues,
    }
    if wrapped_issues > merged_issues:
        wrapped_media_validation["issues"].append(
            f"v2 selected-unit wrapped AVI has more problem frames ({wrapped_issues}) than merged raw DV ({merged_issues})"
        )
    if wrapped_audio_issues > merged_audio_issues:
        wrapped_media_validation["issues"].append(
            "v2 selected-unit wrapped AVI has more audio problem frames "
            f"({wrapped_audio_issues}) than merged raw DV ({merged_audio_issues})"
        )
    if wrapped_video_issues > merged_video_issues:
        wrapped_media_validation["issues"].append(
            "v2 selected-unit wrapped AVI has more video problem frames "
            f"({wrapped_video_issues}) than merged raw DV ({merged_video_issues})"
        )
    wrapped_media_validation["ok"] = not wrapped_media_validation["issues"]

    wrap_rejected_reasons: list[str] = []
    if wrap_result["exit_code"] != 0:
        wrap_rejected_reasons.append("v2 selected-unit AVI wrap returned non-zero exit code")
    if wrapped_frames < merged_frames:
        wrap_rejected_reasons.append(
            f"v2 selected-unit wrapped AVI has fewer frames ({wrapped_frames}) than merged raw DV ({merged_frames})"
        )
    if not wrapped_metadata_validation["ok"]:
        wrap_rejected_reasons.extend(wrapped_metadata_validation["issues"])
    if not wrapped_coverage_validation["ok"]:
        wrap_rejected_reasons.extend(wrapped_coverage_validation["issues"])
    wrap_rejected_reasons.extend(wrapped_media_validation["issues"])
    result.update(
        {
            "wrap_result": wrap_result,
            "wrapped_dvrescue": wrapped_dvrescue,
            "wrapped_metadata_validation": wrapped_metadata_validation,
            "wrapped_coverage_validation": wrapped_coverage_validation,
            "wrapped_media_problem_validation": wrapped_media_validation,
            "wrap_rejected_reasons": wrap_rejected_reasons,
        }
    )
    if wrap_rejected_reasons:
        result["reason"] = "v2 selected-unit AVI wrap failed transactional validation"
        return result

    shutil.move(str(candidate_avi), str(final_avi))
    filesystem_timestamp = set_filesystem_recording_time(final_avi, wrapped_dvrescue)
    if not filesystem_timestamp["ok"]:
        result["reason"] = "filesystem timestamp update failed for v2 selected-unit master"
        result["filesystem_timestamp"] = filesystem_timestamp
        return result

    result.update(
        {
            "accepted": True,
            "output": str(final_avi),
            "public_name": final_avi.name,
            "public_name_basis": "embedded DV recording datetime, cassette id, and DV timecode start",
            "bytes": final_avi.stat().st_size,
            "filesystem_timestamp": filesystem_timestamp,
            "coverage_ranges": coverage_ranges_from_meta(wrapped_dvrescue, basis),
            "reason": "v2 selected-unit merge produced one verified AVI master",
        }
    )
    return result


def process_cluster(
    idx: int,
    cluster: list[SourceInfo],
    keep_dir: Path,
    work_root: Path,
    apply: bool,
    force: bool,
    micro_gap_max_frames: int,
    single_master_goal: bool = False,
    clustering_mode: str = "embedded-evidence",
    timeline_gap_max_frames: int | None = None,
) -> dict[str, Any]:
    key = output_key_for_cluster(cluster, idx)
    work_dir = work_root / key
    raw_dir = work_dir / "raw"
    merge_dir = work_dir / "merge"
    for path in [raw_dir, merge_dir, keep_dir]:
        if apply:
            path.mkdir(parents=True, exist_ok=True)

    decision: dict[str, Any] = {
        "cluster_key": key,
        "source_files": [item.path.name for item in cluster],
        "clustering": {
            "mode": clustering_mode,
            "timeline": (
                cluster_timeline_summary(cluster, timeline_gap_max_frames)
                if timeline_gap_max_frames is not None
                else None
            ),
            "local_alignment_note": (
                "v2 uses recording datetime as the only global timeline anchor; "
                "timecode and absolute-track are local overlap/seam evidence only."
                if timeline_gap_max_frames is not None
                else None
            ),
        },
        "keep_file": None,
        "keep_files": [],
        "keep_kind": None,
        "preservation_source": None,
        "merge_attempted": False,
        "merge_accepted": False,
        "merge_rejected_reasons": [],
        "merge_result": None,
        "micro_gap_stitch": None,
        "coverage": None,
        "cluster_identity_evidence": cluster_identity_evidence(cluster, micro_gap_max_frames),
        "source_disposition": [],
        "policy_excluded_sources": [],
    }

    active_cluster = eligible_keep_sources(cluster)
    excluded_cluster = [source for source in cluster if source_excluded_from_keep(source)]
    decision["policy_excluded_sources"] = [
        {"source": source.path.name, **bad_frame_policy_for_source(source)}
        for source in excluded_cluster
    ]

    if not active_cluster:
        decision["coverage"] = {
            "basis": None,
            "source_union": {},
            "active_source_files": [],
            "policy_excluded_sources": [source.path.name for source in excluded_cluster],
            "note": "No KEEP output was created because every source in this cluster was excluded by KEEP policy.",
        }
        decision["preservation_source"] = {
            "source": None,
            "note": "All sources were intentionally excluded from KEEP by policy.",
        }
        decision["keep_kind"] = "all_sources_excluded_by_policy"
        decision["selected"] = {
            "kind": "all_sources_excluded_by_policy",
            "outputs": [],
        }
        decision["source_disposition"] = build_source_disposition(
            sources=cluster,
            basis=None,
            keep_ranges=[],
        )
        annotate_master_status(decision, single_master_goal=single_master_goal)
        return decision

    basis = choose_coverage_basis(active_cluster)
    union_ranges = cluster_union_ranges(active_cluster, basis) if basis else []
    best = choose_best_source(active_cluster) if basis else choose_best_source_for_cluster(active_cluster)
    best_covers_union_by_ranges = source_covers_union(best, basis, union_ranges) if basis else False
    best_physically_covers_cluster = best_source_physically_covers_cluster(
        best, active_cluster
    )
    best_covers_everything = (
        best_covers_union_by_ranges or best_physically_covers_cluster
    )
    boundary_metadata = expected_boundary_metadata(active_cluster, basis)
    coverage_summary = {
        "basis": basis,
        "source_union": compact_ranges_for_json(union_ranges),
        "active_source_files": [source.path.name for source in active_cluster],
        "policy_excluded_sources": [source.path.name for source in excluded_cluster],
        "best_source": best.path.name,
        "best_source_covers_union": best_covers_everything,
        "best_source_covers_union_by_embedded_ranges": best_covers_union_by_ranges,
        "best_source_physically_supersedes_others": best_physically_covers_cluster,
        "boundary_metadata": boundary_metadata,
    }
    decision["coverage"] = coverage_summary
    decision["preservation_source"] = {
        "source": best.path.name,
        "note": "The source AVI remains in the target folder unless guarded source deletion is explicitly requested.",
    }

    needs_merge = (single_master_goal and len(active_cluster) > 1) or (len(active_cluster) > 1 and (
        any(item.issue_count > 0 or bool(item.dvrescue.get("speed_values")) for item in active_cluster)
        or not best_covers_everything
    ))
    if not needs_merge:
        add_copy_outputs(
            decision=decision,
            key=key,
            keep_dir=keep_dir,
            sources=[best],
            apply=apply,
            force=force,
            keep_kind="best_input_no_merge_needed",
        )
        keep_ranges_nomerge = source_ranges(best, basis) if basis else []
        decision["source_disposition"] = build_source_disposition(
            sources=cluster,
            basis=basis,
            keep_ranges=keep_ranges_nomerge,
            keep_sources=[best],
        )
        decision["coverage_audit"] = compute_coverage_audit(active_cluster, keep_ranges_nomerge, basis)
        annotate_master_status(decision, single_master_goal=single_master_goal)
        return decision

    if not apply:
        decision["merge_attempted"] = True
        decision["keep_kind"] = "dry_run_merge_not_created"
        decision["source_disposition"] = build_source_disposition(
            sources=cluster,
            basis=basis,
            keep_ranges=[],
        )
        annotate_master_status(decision, single_master_goal=single_master_goal)
        return decision

    for source in active_cluster:
        extract_raw(source, raw_dir, force=force)

    basis = choose_coverage_basis(active_cluster)
    union_ranges = cluster_union_ranges(active_cluster, basis) if basis else []
    best_after_raw = choose_best_source(active_cluster) if basis else choose_best_source_for_cluster(active_cluster)
    best_after_raw_covers_by_ranges = source_covers_union(best_after_raw, basis, union_ranges) if basis else False
    best_after_raw_physically_covers = best_source_physically_covers_cluster(
        best_after_raw, active_cluster
    )
    best_after_raw_covers_everything = (
        best_after_raw_covers_by_ranges or best_after_raw_physically_covers
    )
    boundary_metadata = expected_boundary_metadata(active_cluster, basis)
    decision["coverage"] = {
        "basis": basis,
        "source_union": compact_ranges_for_json(union_ranges),
        "active_source_files": [source.path.name for source in active_cluster],
        "policy_excluded_sources": [source.path.name for source in excluded_cluster],
        "best_source": best_after_raw.path.name,
        "best_source_covers_union": best_after_raw_covers_everything,
        "best_source_covers_union_by_embedded_ranges": best_after_raw_covers_by_ranges,
        "best_source_physically_supersedes_others": best_after_raw_physically_covers,
        "boundary_metadata": boundary_metadata,
    }

    if single_master_goal and timeline_gap_max_frames is not None and len(active_cluster) > 1:
        v2_master = create_v2_master_first_output(
            key=key,
            keep_dir=keep_dir,
            merge_dir=merge_dir,
            active_sources=active_cluster,
            max_gap_frames=timeline_gap_max_frames,
            force=force,
        )
        decision["v2_master_first"] = v2_master
        if v2_master.get("attempted"):
            decision["merge_attempted"] = bool((v2_master.get("dvrescue_improvement") or {}).get("attempted"))
            decision["merge_accepted"] = bool((v2_master.get("dvrescue_improvement") or {}).get("accepted"))
            decision["merge_result"] = (v2_master.get("dvrescue_improvement") or {}).get("merge_result")
            decision["merge_rejected_reasons"] = (
                (v2_master.get("dvrescue_improvement") or {}).get("rejected_reasons") or []
            )
        if v2_master.get("accepted"):
            keep_ranges = v2_master.get("coverage_ranges") or []
            v2_basis = v2_master.get("coverage_basis")
            decision["coverage"]["v2_master_first"] = {
                "accepted": True,
                "basis": v2_basis,
                "source_union": compact_ranges_for_json(report_ranges_to_tuples((v2_master.get("plan") or {}).get("required_ranges"))),
                "note": (
                    "v2 built a rough raw DV master first, selecting the cleaner source over overlaps, "
                    "then accepted DVRescue only if it did not shrink coverage or increase detected problems."
                ),
            }
            decision["keep_file"] = v2_master["output"]
            decision["keep_files"] = [v2_master["output"]]
            decision["keep_kind"] = v2_master["keep_kind"]
            decision["selected"] = {
                "kind": v2_master["keep_kind"],
                "output": v2_master["output"],
                "public_name": v2_master["public_name"],
                "public_name_basis": v2_master["public_name_basis"],
                "bytes": v2_master["bytes"],
                "filesystem_timestamp": v2_master["filesystem_timestamp"],
                "coverage_basis": v2_basis,
                "reason": v2_master["reason"],
                "v2_master_first": {
                    "dvrescue_improvement_accepted": bool(
                        (v2_master.get("dvrescue_improvement") or {}).get("accepted")
                    ),
                    "synthetic_hold_frames": int((v2_master.get("plan") or {}).get("synthetic_hold_frames") or 0),
                    "expected_output_frames": int((v2_master.get("plan") or {}).get("expected_output_frames") or 0),
                    "timecode_loop_collision_count": int(
                        (v2_master.get("plan") or {}).get("timecode_loop_collision_count") or 0
                    ),
                },
            }
            decision["source_disposition"] = build_source_disposition(
                sources=cluster,
                basis=v2_basis,
                keep_ranges=keep_ranges,
            )
            decision["coverage_audit"] = compute_coverage_audit(active_cluster, keep_ranges, v2_basis)
            annotate_master_status(decision, single_master_goal=single_master_goal)
            return decision

    merge_primary = ordered_merge_sources(active_cluster)[0]
    max_input_frames = max(item.raw_frames or 0 for item in active_cluster)
    best_input_issues = min(item.issue_count for item in active_cluster)
    best_input_audio_issues = min(item.audio_issue_count for item in active_cluster)
    best_input_video_issues = min(item.video_issue_count for item in active_cluster)
    max_tail_repeated = max(item.tail_repeated_problem_count for item in active_cluster)

    merged_dv = merge_dir / f"{key}.merged.dv"
    merged_log = merge_dir / f"{key}.merge-log.txt"
    merge_result = merge_raw_dv(active_cluster, merged_dv, merged_log, force=force)
    merged_dvrescue = analyze_dvrescue(merged_dv) if merged_dv.exists() else {}
    merged_ffprobe = analyze_ffprobe(merged_dv) if merged_dv.exists() else {}
    merged_frames = int(merged_dvrescue.get("frames_count") or 0)
    merged_issues = int(merged_dvrescue.get("problem_frame_count") or 0)
    merged_audio_issues = int(merged_dvrescue.get("audio_problem_frame_count") or 0)
    merged_video_issues = int(merged_dvrescue.get("video_problem_frame_count") or 0)
    trim = max_input_frames - merged_frames
    media_problem_validation_issues: list[str] = []
    if merged_issues > best_input_issues:
        media_problem_validation_issues.append(
            f"merged output has more problem frames ({merged_issues}) than best input ({best_input_issues})"
        )
    if merged_audio_issues > best_input_audio_issues:
        media_problem_validation_issues.append(
            "merged output has more audio problem frames "
            f"({merged_audio_issues}) than best input audio ({best_input_audio_issues})"
        )
    if merged_video_issues > best_input_video_issues:
        media_problem_validation_issues.append(
            "merged output has more video problem frames "
            f"({merged_video_issues}) than best input video ({best_input_video_issues})"
        )

    merge_result["merged_dvrescue"] = merged_dvrescue
    merge_result["merged_ffprobe"] = merged_ffprobe
    merge_result["merge_log_text"] = read_text_if_exists(merged_log)
    merge_result["metadata_validation"] = validate_core_metadata(
        label="merged raw DV",
        output_meta=merged_dvrescue,
        primary=merge_primary,
        sources=active_cluster,
        expected_overrides=boundary_metadata,
    )
    merge_result["coverage_validation"] = validate_coverage(
        label="merged raw DV",
        output_meta=merged_dvrescue,
        sources=active_cluster,
        basis=basis,
    )
    merge_result["max_input_frames"] = max_input_frames
    merge_result["best_input_issues"] = best_input_issues
    merge_result["best_input_audio_problem_frames"] = best_input_audio_issues
    merge_result["best_input_video_problem_frames"] = best_input_video_issues
    merge_result["merged_problem_frames"] = merged_issues
    merge_result["merged_audio_problem_frames"] = merged_audio_issues
    merge_result["merged_video_problem_frames"] = merged_video_issues
    merge_result["media_problem_validation"] = {
        "ok": not media_problem_validation_issues,
        "issues": media_problem_validation_issues,
        "best_input_problem_frames": best_input_issues,
        "best_input_audio_problem_frames": best_input_audio_issues,
        "best_input_video_problem_frames": best_input_video_issues,
        "merged_problem_frames": merged_issues,
        "merged_audio_problem_frames": merged_audio_issues,
        "merged_video_problem_frames": merged_video_issues,
    }
    merge_result["max_tail_repeated_problem_count"] = max_tail_repeated
    merge_result["trimmed_frames_vs_longest_input"] = trim
    decision["merge_attempted"] = True
    decision["merge_result"] = merge_result

    if merge_result["exit_code"] != 0:
        decision["merge_rejected_reasons"].append("dvrescue merge returned non-zero exit code")
    if merged_frames <= 0:
        decision["merge_rejected_reasons"].append("merged output has no analyzable DV frames")
    if not merge_result["coverage_validation"]["ok"]:
        decision["merge_rejected_reasons"].extend(merge_result["coverage_validation"]["issues"])
    if trim > 0 and trim > max_tail_repeated and not merge_result["coverage_validation"]["ok"]:
        decision["merge_rejected_reasons"].append(
            f"merged output trims {trim} frames, beyond repeated/problem tail allowance {max_tail_repeated}"
        )
    decision["merge_rejected_reasons"].extend(media_problem_validation_issues)
    if not merge_result["metadata_validation"]["ok"]:
        decision["merge_rejected_reasons"].extend(merge_result["metadata_validation"]["issues"])

    if not decision["merge_rejected_reasons"]:
        candidate_avi = merge_dir / f"{key}.candidate.avi"
        merged_output_stem = professional_output_stem(
            cassette=merge_primary.cassette,
            dv_meta=merged_dvrescue,
            fallback_key=key,
        )
        final_candidate_avi = reserve_keep_output_path(keep_dir / f"{merged_output_stem}.avi")
        merged_output_stem = final_candidate_avi.stem
        merged_metadata_tags = build_avi_metadata_tags(
            output_key=merged_output_stem,
            cassette=merge_primary.cassette,
            source_files=[source.path.name for source in active_cluster],
            keep_kind="avi_wrapped_from_raw_dv_merge",
            dv_meta=merged_dvrescue,
        )
        wrap_result = wrap_dv_to_avi(
            merged_dv,
            candidate_avi,
            force=force,
            metadata_tags=merged_metadata_tags,
            validation_dir=merge_dir / "metadata_validation",
        )
        wrapped_dvrescue = analyze_dvrescue(candidate_avi) if candidate_avi.exists() else {}
        wrapped_metadata_validation = validate_core_metadata(
            label="wrapped AVI",
            output_meta=wrapped_dvrescue,
            primary=merge_primary,
            sources=active_cluster,
            expected_overrides=boundary_metadata,
        )
        wrapped_coverage_validation = validate_coverage(
            label="wrapped AVI",
            output_meta=wrapped_dvrescue,
            sources=active_cluster,
            basis=basis,
        )
        wrapped_frames = int(wrapped_dvrescue.get("frames_count") or 0)
        wrapped_issues = int(wrapped_dvrescue.get("problem_frame_count") or 0)
        wrapped_audio_issues = int(wrapped_dvrescue.get("audio_problem_frame_count") or 0)
        wrapped_video_issues = int(wrapped_dvrescue.get("video_problem_frame_count") or 0)
        wrapped_media_validation = {
            "ok": (
                wrapped_issues <= merged_issues
                and wrapped_audio_issues <= merged_audio_issues
                and wrapped_video_issues <= merged_video_issues
            ),
            "issues": [],
            "merged_problem_frames": merged_issues,
            "wrapped_problem_frames": wrapped_issues,
            "merged_audio_problem_frames": merged_audio_issues,
            "wrapped_audio_problem_frames": wrapped_audio_issues,
            "merged_video_problem_frames": merged_video_issues,
            "wrapped_video_problem_frames": wrapped_video_issues,
        }
        if wrapped_issues > merged_issues:
            wrapped_media_validation["issues"].append(
                f"wrapped AVI has more problem frames ({wrapped_issues}) than merged raw DV ({merged_issues})"
            )
        if wrapped_audio_issues > merged_audio_issues:
            wrapped_media_validation["issues"].append(
                "wrapped AVI has more audio problem frames "
                f"({wrapped_audio_issues}) than merged raw DV ({merged_audio_issues})"
            )
        if wrapped_video_issues > merged_video_issues:
            wrapped_media_validation["issues"].append(
                "wrapped AVI has more video problem frames "
                f"({wrapped_video_issues}) than merged raw DV ({merged_video_issues})"
            )
        if (
            wrap_result["exit_code"] == 0
            and wrapped_frames >= merged_frames
            and wrapped_metadata_validation["ok"]
            and wrapped_coverage_validation["ok"]
            and wrapped_media_validation["ok"]
        ):
            keep_file = final_candidate_avi
            shutil.move(str(candidate_avi), str(keep_file))
            keep_format = "avi_wrapped_from_raw_dv_merge"
            keep_ranges = coverage_ranges_from_meta(wrapped_dvrescue, basis) if basis else []
        else:
            keep_file = reserve_keep_output_path(keep_dir / f"{merged_output_stem}.dv")
            shutil.move(str(merged_dv), str(keep_file))
            keep_format = "raw_dv_merge"
            keep_ranges = coverage_ranges_from_meta(merged_dvrescue, basis) if basis else []
        selected_dv_meta = wrapped_dvrescue if keep_format == "avi_wrapped_from_raw_dv_merge" else merged_dvrescue
        filesystem_timestamp = set_filesystem_recording_time(keep_file, selected_dv_meta)
        if not filesystem_timestamp["ok"]:
            raise RuntimeError(f"filesystem timestamp update failed for {keep_file.name}: {filesystem_timestamp}")
        decision["merge_accepted"] = True
        decision["keep_file"] = str(keep_file)
        decision["keep_files"] = [str(keep_file)]
        decision["keep_kind"] = keep_format
        decision["selected"] = {
            "kind": keep_format,
            "output": str(keep_file),
            "public_name": keep_file.name,
            "public_name_basis": "embedded DV recording datetime, cassette id, and DV timecode start",
            "filesystem_timestamp": filesystem_timestamp,
            "wrap_result": wrap_result,
            "wrapped_dvrescue": wrapped_dvrescue,
            "wrapped_metadata_validation": wrapped_metadata_validation,
            "wrapped_coverage_validation": wrapped_coverage_validation,
            "wrapped_media_problem_validation": wrapped_media_validation,
            "trimmed_frames_vs_longest_input": trim,
            "merged_problem_frames": merged_issues,
            "merged_audio_problem_frames": merged_audio_issues,
            "merged_video_problem_frames": merged_video_issues,
        }
        decision["source_disposition"] = build_source_disposition(
            sources=cluster,
            basis=basis,
            keep_ranges=keep_ranges,
        )
        decision["coverage_audit"] = compute_coverage_audit(active_cluster, keep_ranges, basis)
    else:
        # DVRescue could not produce an accepted merge. Assemble a
        # coverage-preserving KEEP set on the cluster-local abst axis:
        #   1) partition the active sources into adjacency runs (the
        #      capture-split fragments of one pass) and treat each run as one
        #      covering unit;
        #   2) keep the minimal set of units whose union covers the cluster,
        #      never discarding a unit that is not provably covered by the kept
        #      set -- so no distinct content is ever dropped (the loss we found);
        #   3) write each kept multi-fragment run as one stitched file and each
        #      kept singleton as a byte-identical source copy.
        runs = partition_sources_into_stitch_runs(active_cluster, micro_gap_max_frames)
        units: list[dict[str, Any]] = []
        for run_index, run in enumerate(runs):
            cov: list[tuple[int, int]] = []
            if basis:
                for s in run:
                    cov.extend(source_ranges(s, basis))
                cov = normalize_ranges(cov)
            units.append(
                {
                    "index": run_index,
                    "run": run,
                    "coverage": cov,
                    "quality": sum(s.good_frame_count for s in run),
                }
            )

        if basis is None:
            # No coordinate to prove redundancy: keep everything (never lose).
            selected_units, discarded_units = units, []
        else:
            cov_units = [u for u in units if u["coverage"]]
            nocov_units = [u for u in units if not u["coverage"]]
            selected_units, discarded_units = select_coverage_preserving_units(cov_units, union_ranges)
            # Units with no coverage on this basis cannot be proven redundant; keep them.
            selected_units = selected_units + nocov_units

        selected_units.sort(key=lambda u: (u["coverage"][0][0] if u["coverage"] else 0, u["index"]))

        decision["duplicate_resolution"] = {
            "method": "coverage_preserving_minimal_cover",
            "basis": basis,
            "selected_units": [
                {
                    "sources": [s.path.name for s in u["run"]],
                    "coverage_frames": range_total(u["coverage"]),
                }
                for u in selected_units
            ],
            "discarded_units": [
                {
                    "sources": [s.path.name for s in u["run"]],
                    "reason": "every frame is provably covered by the kept set",
                }
                for u in discarded_units
            ],
        }

        selected_sources_for_v2_merge = [
            source
            for unit in selected_units
            for source in unit["run"]
        ]
        selected_names = {source.path.name for source in selected_sources_for_v2_merge}
        active_names = {source.path.name for source in active_cluster}
        if (
            single_master_goal
            and apply
            and basis is not None
            and len(selected_sources_for_v2_merge) >= 2
            and selected_names != active_names
        ):
            v2_master_merge = try_v2_selected_unit_master_merge(
                key=key,
                keep_dir=keep_dir,
                merge_dir=merge_dir,
                sources_to_merge=selected_sources_for_v2_merge,
                validation_sources=active_cluster,
                basis=basis,
                boundary_metadata=boundary_metadata,
                force=force,
            )
            decision["v2_selected_unit_master_merge"] = v2_master_merge
            if v2_master_merge.get("accepted"):
                keep_ranges = v2_master_merge.get("coverage_ranges") or []
                decision["primary_all_source_merge_rejected_reasons"] = list(decision["merge_rejected_reasons"])
                decision["merge_rejected_reasons"] = []
                decision["merge_accepted"] = True
                decision["keep_file"] = v2_master_merge["output"]
                decision["keep_files"] = [v2_master_merge["output"]]
                decision["keep_kind"] = "avi_wrapped_from_raw_dv_v2_selected_unit_merge"
                decision["selected"] = {
                    "kind": "avi_wrapped_from_raw_dv_v2_selected_unit_merge",
                    "output": v2_master_merge["output"],
                    "public_name": v2_master_merge["public_name"],
                    "public_name_basis": v2_master_merge["public_name_basis"],
                    "bytes": v2_master_merge["bytes"],
                    "filesystem_timestamp": v2_master_merge["filesystem_timestamp"],
                    "reason": v2_master_merge["reason"],
                }
                decision["source_disposition"] = build_source_disposition(
                    sources=cluster,
                    basis=basis,
                    keep_ranges=keep_ranges,
                )
                decision["coverage_audit"] = compute_coverage_audit(active_cluster, keep_ranges, basis)
                annotate_master_status(decision, single_master_goal=single_master_goal)
                return decision

        if single_master_goal and len(selected_units) > 1:
            apply_single_source_master_fallback(
                decision=decision,
                key=key,
                keep_dir=keep_dir,
                cluster=cluster,
                active_cluster=active_cluster,
                basis=basis,
                apply=apply,
                force=force,
                trigger=(
                    "primary v2/merge paths failed and the coverage-preserving fallback "
                    f"would have emitted {len(selected_units)} KEEP units"
                ),
            )
            decision["multi_output_fallback_suppressed"] = {
                "reason": "v2 single-master contract permits only one KEEP file per cluster",
                "selected_unit_count": len(selected_units),
                "selected_units": [
                    {
                        "sources": [s.path.name for s in u["run"]],
                        "coverage_frames": range_total(u["coverage"]),
                    }
                    for u in selected_units
                ],
            }
            annotate_master_status(decision, single_master_goal=single_master_goal)
            return decision

        run_records: list[dict[str, Any]] = []
        accepted_outputs: list[dict[str, Any]] = []
        copy_sources: list[SourceInfo] = []
        runs_attempted = 0
        runs_accepted = 0
        synthetic_total = 0
        for ui, unit in enumerate(selected_units, start=1):
            run = unit["run"]
            if len(run) < 2:
                run_records.append(
                    {
                        "kind": "singleton_copy_run",
                        "sources": [s.path.name for s in run],
                        "reason": "single capture of this tape region; preserved as a byte-identical copy",
                    }
                )
                copy_sources.extend(run)
                continue
            runs_attempted += 1
            run_key = f"{key}.run{ui:02d}"
            stitch_result = create_micro_gap_stitch_output(
                key=run_key,
                keep_dir=keep_dir,
                merge_dir=merge_dir,
                sources=run,
                basis=basis,
                max_gap_frames=micro_gap_max_frames,
                force=force,
            )
            stitch_result["kind"] = "stitched_run"
            stitch_result["sources"] = [s.path.name for s in run]
            run_records.append(stitch_result)
            if stitch_result.get("accepted"):
                runs_accepted += 1
                synthetic_total += int((stitch_result.get("plan") or {}).get("synthetic_hold_frames") or 0)
                accepted_outputs.append(
                    {
                        "kind": "avi_wrapped_from_raw_dv_micro_gap_stitch",
                        "output": stitch_result["output"],
                        "public_name": stitch_result["public_name"],
                        "public_name_basis": stitch_result["public_name_basis"],
                        "bytes": stitch_result["bytes"],
                        "sources": stitch_result["sources"],
                        "filesystem_timestamp": stitch_result["filesystem_timestamp"],
                        "merged_problem_frames": int(
                            (stitch_result.get("wrapped_dvrescue") or {}).get("problem_frame_count") or 0
                        ),
                        "merged_audio_problem_frames": int(
                            (stitch_result.get("wrapped_dvrescue") or {}).get("audio_problem_frame_count") or 0
                        ),
                        "merged_video_problem_frames": int(
                            (stitch_result.get("wrapped_dvrescue") or {}).get("video_problem_frame_count") or 0
                        ),
                        "synthetic_hold_frames": int(
                            (stitch_result.get("plan") or {}).get("synthetic_hold_frames") or 0
                        ),
                        "reason": stitch_result["reason"],
                    }
                )
            else:
                # Stitch failed validation: preserve each fragment as its own
                # byte-identical KEEP copy so nothing is lost.
                copy_sources.extend(run)

        planned_output_count = len(accepted_outputs) + len(copy_sources)
        if single_master_goal and planned_output_count != 1:
            removed_generated_outputs: list[str] = []
            for item in accepted_outputs:
                output_path = Path(item.get("output") or "")
                if output_path.exists():
                    delete_file_inside_target(output_path)
                    removed_generated_outputs.append(str(output_path))
            apply_single_source_master_fallback(
                decision=decision,
                key=key,
                keep_dir=keep_dir,
                cluster=cluster,
                active_cluster=active_cluster,
                basis=basis,
                apply=apply,
                force=force,
                trigger=(
                    "micro-gap/source-copy fallback did not produce exactly one output "
                    f"(planned {planned_output_count})"
                ),
            )
            decision["multi_output_fallback_suppressed"] = {
                "reason": "v2 single-master contract permits only one KEEP file per cluster",
                "planned_output_count": planned_output_count,
                "removed_generated_outputs": removed_generated_outputs,
                "run_records": run_records,
                "copy_source_files": [source.path.name for source in copy_sources],
            }
            annotate_master_status(decision, single_master_goal=single_master_goal)
            return decision

        copy_outputs: list[dict[str, Any]] = []
        if copy_sources:
            copy_metadata_source_files = None
            if len(copy_sources) == 1 and best_source_physically_covers_cluster(copy_sources[0], active_cluster):
                copy_metadata_source_files = [source.path.name for source in active_cluster]
            copy_outputs = build_source_copy_outputs(
                copy_sources,
                key=key,
                keep_dir=keep_dir,
                keep_kind="best_input_merge_rejected_coverage_preserved_by_source_copies",
                apply=apply,
                force=force,
                metadata_source_files=copy_metadata_source_files,
            )

        if accepted_outputs and copy_outputs:
            keep_kind_final = "mixed_micro_gap_stitch_and_source_copies"
        elif accepted_outputs:
            keep_kind_final = "avi_wrapped_from_raw_dv_micro_gap_stitch"
        else:
            keep_kind_final = "best_input_merge_rejected_coverage_preserved_by_source_copies"

        all_outputs = accepted_outputs + copy_outputs
        decision["keep_files"] = [item["output"] for item in all_outputs]
        decision["keep_file"] = all_outputs[0]["output"] if all_outputs else None
        decision["keep_kind"] = keep_kind_final
        decision["selected"] = (
            all_outputs[0]
            if len(all_outputs) == 1
            else {"kind": keep_kind_final, "outputs": all_outputs}
        )
        decision["micro_gap_stitch"] = {
            "attempted": runs_attempted > 0,
            "accepted": runs_accepted > 0,
            "max_gap_frames": micro_gap_max_frames,
            "max_gap_seconds": micro_gap_max_frames / DV_FPS if micro_gap_max_frames >= 0 else None,
            "runs_total": len(selected_units),
            "runs_attempted": runs_attempted,
            "runs_accepted": runs_accepted,
            "runs_failed_validation": runs_attempted - runs_accepted,
            "singleton_runs": sum(1 for u in selected_units if len(u["run"]) < 2),
            "runs": run_records,
            "plan": {"synthetic_hold_frames": synthetic_total},
        }
        keep_ranges = []
        for u in selected_units:
            keep_ranges.extend(u["coverage"])
        keep_ranges = normalize_ranges(keep_ranges)
        decision["source_disposition"] = build_source_disposition(
            sources=cluster,
            basis=basis,
            keep_ranges=keep_ranges,
            keep_sources=copy_sources,
        )
        decision["coverage_audit"] = compute_coverage_audit(active_cluster, keep_ranges, basis)

    annotate_master_status(decision, single_master_goal=single_master_goal)
    return decision


def main() -> int:
    global ROOT, ABST_SEAM_TOLERANCE_FRAMES, CAMERA_MODEL
    parser = argparse.ArgumentParser(description="Create best DVRescue outputs for source AVI files in one cassette folder.")
    parser.add_argument(
        "target",
        help="Target cassette folder, e.g. raw_recordings/example_tape.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually write outputs. Without this, only a dry report is produced.")
    parser.add_argument("--force", action="store_true", help="Regenerate existing raw/merge/wrapped outputs.")
    parser.add_argument("--keep-work", action="store_true", help="Keep temporary raw DV/merge work files for debugging.")
    parser.add_argument(
        "--conversion-self-test-only",
        action="store_true",
        help="Analyze one target-folder source and run only hash-based conversion self-tests.",
    )
    parser.add_argument(
        "--skip-conversion-self-tests",
        action="store_true",
        help="Skip the automatic hash-based conversion self-tests that normally run before --apply.",
    )
    parser.add_argument(
        "--delete-covered-sources",
        action="store_true",
        help="After a verified apply run, delete source AVIs that are fully represented by KEEP.",
    )
    parser.add_argument(
        "--confirm-delete-covered-sources",
        choices=["DELETE_COVERED_SOURCES"],
        help="Required literal confirmation for --delete-covered-sources.",
    )
    parser.add_argument(
        "--cluster-close-seconds",
        type=int,
        default=0,
        help=(
            "Deprecated compatibility option; filename timestamps are not used "
            "for clustering or deletion safety."
        ),
    )
    parser.add_argument(
        "--v2",
        action="store_true",
        help=(
            "Use version 2 true-timeline clustering: embedded recording datetime is the global anchor, "
            "while timecode and absolute-track are used only as local overlap/seam evidence."
        ),
    )
    parser.add_argument(
        "--v2-timeline-gap-seconds",
        type=float,
        default=V2_TIMELINE_CLUSTER_GAP_SECONDS,
        help=(
            "Maximum gap, in seconds, for joining sources into one v2 true-timeline cluster. "
            "Default: 0.4 seconds (10 PAL DV frames)."
        ),
    )
    parser.add_argument(
        "--micro-gap-stitch-max-seconds",
        type=float,
        default=MICRO_GAP_STITCH_MAX_SECONDS,
        help=(
            "After DVRescue merge rejection, stitch adjacent fragments by repeating the previous DV frame "
            "when the embedded timecode gap is below this many seconds. Default: 0.1. Use 0 to allow only "
            "perfectly contiguous joins."
        ),
    )
    parser.add_argument(
        "--abst-seam-tolerance-frames",
        type=int,
        default=ABST_SEAM_TOLERANCE_FRAMES,
        help=(
            "Max absolute-track disagreement (in frames) accepted at a stitch seam. Real capture splits "
            "show a 1-2 frame timecode/abst slop. Default: 2. Use 0 to require perfect abst continuity."
        ),
    )
    parser.add_argument(
        "--deep-verify",
        action="store_true",
        help=(
            "After writing derived KEEP outputs (merge/stitch), verify every real output DV frame is a "
            "byte-for-byte copy of a source frame (only documented synthetic hold frames may differ). "
            "Slower; recommended for a final archival pass."
        ),
    )
    parser.add_argument(
        "--camera-model",
        default=CAMERA_MODEL,
        help="Camera model written into KEEP AVI title/comment metadata. Replace the placeholder for your own tapes.",
    )
    args = parser.parse_args()
    CAMERA_MODEL = args.camera_model.strip() or "Camera Model"

    target = Path(args.target)
    if not target.is_absolute():
        target = SCRIPT_ROOT / target
    ROOT = target.resolve()

    ensure_target_safe()
    if args.delete_covered_sources and not args.apply:
        raise SystemExit("--delete-covered-sources requires --apply.")
    if args.delete_covered_sources and args.confirm_delete_covered_sources != "DELETE_COVERED_SOURCES":
        raise SystemExit("--delete-covered-sources requires --confirm-delete-covered-sources DELETE_COVERED_SOURCES.")
    if args.delete_covered_sources and args.conversion_self_test_only:
        raise SystemExit("--delete-covered-sources cannot be combined with --conversion-self-test-only.")
    if args.micro_gap_stitch_max_seconds < 0:
        raise SystemExit("--micro-gap-stitch-max-seconds must be >= 0.")
    if args.v2_timeline_gap_seconds < 0:
        raise SystemExit("--v2-timeline-gap-seconds must be >= 0.")
    if args.abst_seam_tolerance_frames < 0:
        raise SystemExit("--abst-seam-tolerance-frames must be >= 0.")
    ABST_SEAM_TOLERANCE_FRAMES = args.abst_seam_tolerance_frames
    micro_gap_max_frames = micro_gap_frame_limit(args.micro_gap_stitch_max_seconds)
    v2_timeline_gap_frames = micro_gap_frame_limit(args.v2_timeline_gap_seconds)
    out_root = ROOT / "KEEP"
    work_root = ROOT / "_dvrescue_best_work_tmp"
    if args.apply:
        if args.force:
            remove_tree_inside_target(out_root)
            remove_tree_inside_target(work_root)
        out_root.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

    sources = discover_sources()
    if not sources:
        raise SystemExit(f"No source AVI files were found in {ROOT}.")

    print(f"Analyzing {len(sources)} source AVI files in {ROOT}...", flush=True)
    analysis_iter = sources
    if tqdm is not None:
        analysis_iter = tqdm(sources, desc="Analyzing sources", unit="file", dynamic_ncols=True)
    for source in analysis_iter:
        if tqdm is not None:
            analysis_iter.set_description(f"Analyzing {short_progress_name(source.path.name)}", refresh=True)
        source.ffprobe = analyze_ffprobe(source.path)
        # Pre-check: if ffprobe shows the file is shorter than the policy
        # exclusion threshold, skip DVRescue entirely. DVRescue can hang on
        # pathological micro-captures (1-2 frame broken fragments at tape
        # section boundaries), and policy will discard them anyway. Saves
        # minutes per such file.
        nb_read_frames = int_or_none(source.ffprobe.get("nb_read_frames"))
        max_skip_frames = int(MIN_KEEP_DURATION_SECONDS * DV_FPS)  # 12 frames at 25fps
        if nb_read_frames is not None and 0 < nb_read_frames <= max_skip_frames:
            source.dvrescue = {
                "exit_code": 0,
                "stderr": "skipped: ffprobe reports duration below policy threshold",
                "xml_error": None,
                "frames_count": nb_read_frames,
                "frames_segments": [],
                "first_abst": None, "last_abst": None,
                "first_tc": None, "last_tc": None,
                "first_rdt": None, "last_rdt": None,
                "video_rate": None, "audio_rate": None, "channels": None,
                "speed_values": [],
                "first_frame": {}, "last_frame": {},
                "problem_frame_count": 0,
                "problem_frames_sample": [],
                "problem_frame_ranges": [],
                "audio_problem_frame_count": 0,
                "audio_problem_frames_sample": [],
                "audio_problem_frame_ranges": [],
                "video_problem_frame_count": 0,
                "video_problem_frames_sample": [],
                "video_problem_frame_ranges": [],
                "tail_repeated_problem_count": 0,
                "leading_frame_samples": [],
                "dvrescue_skipped_below_min_duration": True,
            }
        else:
            source.dvrescue = analyze_dvrescue(source.path)
        dv = source.dvrescue
        frames = dv.get("frames_count")
        problems = dv.get("problem_frame_count")
        if tqdm is not None:
            analysis_iter.set_postfix(frames=frames if frames is not None else "?", problems=problems, refresh=False)
        if dv.get("exit_code") != 0 or dv.get("xml_error"):
            message = f"Source analysis needs checking: {source.path.name} exit={dv.get('exit_code')}"
            if dv.get("xml_error"):
                message += f" xml_error={dv.get('xml_error')}"
            if tqdm is not None:
                tqdm.write(message)
            else:
                print(message, flush=True)

    conversion_self_tests = None
    should_run_conversion_self_tests = args.conversion_self_test_only or (
        args.apply and not args.skip_conversion_self_tests
    )
    if should_run_conversion_self_tests:
        print("Running hash-based conversion self-tests on one target-folder source...", flush=True)
        conversion_self_tests = run_conversion_self_tests(
            sources=sources,
            test_dir=work_root / "_conversion_self_tests",
            keep_work=args.keep_work,
        )
        print(
            json.dumps(
                {
                    "conversion_self_tests_ok": conversion_self_tests["ok"],
                    "sample": conversion_self_tests["sample"]["file"],
                    "failed_tests": conversion_self_tests["failed_tests"],
                },
                indent=2,
            ),
            flush=True,
        )
        if args.conversion_self_test_only:
            print(json.dumps(conversion_self_tests, indent=2), flush=True)
            return 0 if conversion_self_tests["ok"] else 3
        if not conversion_self_tests["ok"]:
            print("Refusing to continue: conversion self-tests failed.", flush=True)
            return 3

    if args.v2:
        clusters = cluster_sources_timeline_v2(
            sources,
            timeline_gap_max_frames=v2_timeline_gap_frames,
        )
        clustering_mode = "true_recording_datetime_timeline_v2"
    else:
        clusters = cluster_sources(
            sources,
            close_seconds=args.cluster_close_seconds,
            micro_gap_max_frames=micro_gap_max_frames,
        )
        clustering_mode = "embedded-evidence"
    print(f"Built {len(clusters)} cluster(s).", flush=True)
    reset_keep_output_path_reservations()

    decisions = []
    merge_accepted = 0
    merge_rejected = 0
    merge_planned = 0
    no_merge = 0
    cleanup_warnings: list[str] = []
    try:
        cluster_iter = enumerate(clusters, start=1)
        if tqdm is not None:
            cluster_iter = tqdm(cluster_iter, total=len(clusters), desc="Processing clusters", unit="cluster", dynamic_ncols=True)
        for idx, cluster in cluster_iter:
            if tqdm is not None:
                cluster_iter.set_description(f"Cluster {idx}/{len(clusters)}", refresh=True)
            decision = process_cluster(
                idx,
                cluster,
                out_root,
                work_root,
                apply=args.apply,
                force=args.force,
                micro_gap_max_frames=micro_gap_max_frames,
                single_master_goal=bool(args.v2),
                clustering_mode=clustering_mode,
                timeline_gap_max_frames=v2_timeline_gap_frames if args.v2 else None,
            )
            decisions.append(decision)
            if decision.get("merge_accepted"):
                merge_accepted += 1
            elif decision.get("keep_kind") == "dry_run_merge_not_created":
                merge_planned += 1
            elif decision.get("merge_attempted"):
                merge_rejected += 1
            else:
                no_merge += 1
            if tqdm is not None:
                cluster_iter.set_postfix(
                    accepted=merge_accepted,
                    planned=merge_planned,
                    rejected=merge_rejected,
                    no_merge=no_merge,
                    refresh=False,
                )
            if args.apply and not args.keep_work:
                remove_tree_inside_target(work_root / decision["cluster_key"])
    finally:
        if args.apply and not args.keep_work:
            try:
                remove_tree_inside_target(work_root)
            except OSError as exc:
                warning = f"could not remove temporary work root {work_root}: {exc}"
                cleanup_warnings.append(warning)
                print(f"Warning: {warning}", flush=True)

    # Optional byte-for-byte pristine proof of derived outputs.
    deep_verify_summary: dict[str, Any] | None = None
    if args.apply and args.deep_verify:
        verify_root = ROOT / "_dvrescue_deep_verify_tmp"
        remove_tree_inside_target(verify_root)
        verify_root.mkdir(parents=True, exist_ok=True)
        source_paths_by_name = {source.path.name: source.path for source in sources}
        total_foreign = 0
        clusters_checked = 0
        try:
            for decision in decisions:
                if not decision.get("keep_files"):
                    continue
                res = deep_verify_decision(decision, source_paths_by_name, verify_root)
                decision["deep_verify"] = res
                total_foreign += int(res.get("frames_not_in_sources_total") or 0)
                clusters_checked += 1
        finally:
            remove_tree_inside_target(verify_root)
        deep_verify_summary = {
            "ok": total_foreign == 0,
            "clusters_checked": clusters_checked,
            "frames_not_in_sources_total": total_foreign,
        }
        print(
            f"Deep verify: clusters={clusters_checked} foreign_frames={total_foreign} ok={deep_verify_summary['ok']}",
            flush=True,
        )

    # Folder-wide completeness proof: KEEP must cover every active source frame.
    total_coverage_deficit_frames = sum(
        int((decision.get("coverage_audit") or {}).get("deficit_frames") or 0) for decision in decisions
    )
    clusters_with_deficit = [
        decision["cluster_key"]
        for decision in decisions
        if int((decision.get("coverage_audit") or {}).get("deficit_frames") or 0) > 0
    ]
    keep_output_records: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        for keep_file in decision.get("keep_files") or []:
            keep_path = Path(keep_file)
            key = keep_output_reservation_key(keep_path)
            record = keep_output_records.setdefault(
                key,
                {
                    "keep_file": keep_path.name,
                    "path": str(keep_path),
                    "clusters": [],
                    "count": 0,
                },
            )
            record["clusters"].append(decision.get("cluster_key"))
            record["count"] += 1
    duplicate_keep_outputs = [
        record
        for record in keep_output_records.values()
        if int(record.get("count") or 0) > 1
    ]

    all_dispositions = [
        item
        for decision in decisions
        for item in decision.get("source_disposition") or []
    ]
    delete_blockers = [
        item
        for item in all_dispositions
        if not item.get("safe_to_delete_after_keep_verification")
    ]
    safe_source_names = {
        item.get("source")
        for item in all_dispositions
        if item.get("safe_to_delete_after_keep_verification")
    }
    delete_eligible_sources = [source for source in sources if source.path.name in safe_source_names]
    # Deletion is additionally gated on a folder-wide completeness proof (no KEEP
    # coverage deficit) and, when requested, on the deep byte-verify passing.
    deficit_blocks_delete = total_coverage_deficit_frames > 0
    deep_verify_blocks_delete = bool(deep_verify_summary) and not deep_verify_summary.get("ok", True)
    duplicate_outputs_block_delete = bool(duplicate_keep_outputs)
    delete_plan: dict[str, Any] = {
        "requested": bool(args.delete_covered_sources),
        "mode": "not requested" if not args.delete_covered_sources else "delete covered source AVIs after verification",
        "eligible_source_files": [source.path.name for source in delete_eligible_sources],
        "blocked": bool(delete_blockers)
        or deficit_blocks_delete
        or deep_verify_blocks_delete
        or duplicate_outputs_block_delete,
        "blockers": delete_blockers[:50],
        "coverage_deficit_frames": total_coverage_deficit_frames,
        "coverage_deficit_blocks_delete": deficit_blocks_delete,
        "deep_verify_blocks_delete": deep_verify_blocks_delete,
        "duplicate_keep_outputs_block_delete": duplicate_outputs_block_delete,
        "duplicate_keep_outputs": duplicate_keep_outputs,
        "deleted_source_files": [],
    }

    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "folder": str(ROOT),
        "mode": "apply" if args.apply else "dry-run",
        "safety": "Source AVI files are deleted only with --delete-covered-sources and explicit confirmation.",
        "dvrescue": str(DVRESCUE),
        "settings": {
            "clustering_mode": clustering_mode,
            "v2": bool(args.v2),
            "v2_timeline_gap_seconds": args.v2_timeline_gap_seconds,
            "v2_timeline_gap_frames": v2_timeline_gap_frames,
            "v2_single_master_goal": bool(args.v2),
            "v2_master_first_note": (
                "In v2, the script builds one rough raw DV master for each multi-source active cluster first. "
                "DVRescue is then allowed to improve that already-built master only if the result preserves "
                "the master timeline/frame count and does not increase detected media problems."
                if args.v2
                else None
            ),
            "cluster_close_seconds": args.cluster_close_seconds,
            "identity_matching": (
                "v2 clusters are built by sweeping true recording-time intervals from embedded first_rdt plus "
                "the first_tc frame part. If a tiny dirty lead-in frame carries stale recording datetime, v2 may "
                "anchor on the first later leading frame whose rdt/tc back-projects over the lead-in and validates "
                "against the final rdt/tc plus absolute-track continuity. Timecode and absolute-track are not global "
                "identity keys; they are only local overlap/seam evidence inside a v2 timeline cluster. Sources "
                "without a sane global timeline placement may join a cluster only when strict embedded first-anchor "
                "alignment plus overlapping same-interval DV evidence proves they are duplicate captures."
                if args.v2
                else (
                    "Clusters are built from embedded DV evidence only: recording datetime + DV timecode overlap, "
                    "recording datetime + physical absolute-track overlap, strict recording_datetime_timecode overlap, "
                    "or embedded physical adjacency for micro-gap stitch candidates. Filenames are labels only."
                )
            ),
            "filename_timestamp_clustering": "disabled; the CLI option is retained only for report/backward compatibility",
            "micro_gap_stitch_max_seconds": args.micro_gap_stitch_max_seconds,
            "micro_gap_stitch_max_frames": micro_gap_max_frames,
            "micro_gap_stitch_note": (
                "When DVRescue cannot make an acceptable merge, adjacent selected fragments may be stitched "
                "by repeating the previous raw DV frame for gaps at or below this frame limit. These are "
                "documented as synthetic hold frames in the report and AVI comment metadata."
            ),
            "raw_dv_frame_bytes_pal": RAW_DV_FRAME_BYTES_PAL,
            "coverage_basis_order": COVERAGE_BASIS_ORDER,
            "coverage_basis_note": (
                "Within-cluster coverage/delete-safety prefers absolute-track (abst): it is the physical "
                "tape position and is consistent across every capture of the same recording. abst loops/resets "
                "ACROSS recordings, so it is never used as a global identity key; clustering stays anchored on "
                "recording datetime. recording_datetime_timecode and timecode are fallbacks."
            ),
            "abst_seam_tolerance_frames": ABST_SEAM_TOLERANCE_FRAMES,
            "coverage_seam_tolerance_frames": SEAM_TOLERANCE_FRAMES,
            "deep_verify": bool(args.deep_verify),
            "bad_frame_exclusion_ratio": BAD_FRAME_EXCLUSION_RATIO,
            "min_keep_duration_seconds": MIN_KEEP_DURATION_SECONDS,
            "camera_model": CAMERA_MODEL,
            "metadata_schema": METADATA_SCHEMA,
            "keep_filename_convention": "YYYY-MM-DD_HH-MM-SS__cassette-id__tc-HH-MM-SS-FF.avi",
            "keep_filename_note": (
                "Public KEEP filenames are based on embedded DV recording datetime, cassette id, and DV timecode start. "
                "They intentionally omit rescue workflow terms such as keep, merged, repaired, or version."
            ),
            "keep_file_metadata": (
                "AVI KEEP outputs are stream-copied with DV-derived title/date/comment/encoded_by tags. "
                "FFmpeg's AVI muxer writes date as RIFF ICRD DateCreated; -timestamp is also set where supported. "
                "The script verifies stream hashes or raw DV payload hashes before accepting metadata remuxes."
            ),
            "keep_filesystem_timestamps": (
                "KEEP file CreationTime, LastWriteTime, and LastAccessTime are set to embedded DV first_rdt "
                "after media validation succeeds."
            ),
        },
        "summary": {
            "source_files": len(sources),
            "clusters": len(clusters),
            "v2_unplaceable_timeline_sources": (
                sum(1 for source in sources if source_true_timeline_range(source) is None)
                if args.v2
                else None
            ),
            "v2_unplaceable_duplicate_fallback_clusters": (
                sum(
                    1
                    for item in decisions
                    if int(
                        (((item.get("clustering") or {}).get("timeline") or {}).get(
                            "unplaceable_duplicate_identity_fallback_pairs"
                        ))
                        or 0
                    )
                    > 0
                )
                if args.v2
                else None
            ),
            "v2_unplaceable_duplicate_fallback_pairs": (
                sum(
                    int(
                        (((item.get("clustering") or {}).get("timeline") or {}).get(
                            "unplaceable_duplicate_identity_fallback_pairs"
                        ))
                        or 0
                    )
                    for item in decisions
                )
                if args.v2
                else None
            ),
            "merge_attempted": sum(1 for item in decisions if item["merge_attempted"]),
            "merge_accepted": sum(1 for item in decisions if item["merge_accepted"]),
            "merge_planned_not_run": sum(1 for item in decisions if item.get("keep_kind") == "dry_run_merge_not_created"),
            "merge_rejected": sum(
                1
                for item in decisions
                if item["merge_attempted"]
                and not item["merge_accepted"]
                and item.get("keep_kind") != "dry_run_merge_not_created"
            ),
            "merge_rejected_but_micro_gap_stitched": sum(
                1
                for item in decisions
                if item["merge_attempted"]
                and not item["merge_accepted"]
                and (item.get("micro_gap_stitch") or {}).get("accepted")
            ),
            "micro_gap_stitch_attempted": sum(
                1
                for item in decisions
                if (item.get("micro_gap_stitch") or {}).get("attempted")
            ),
            "micro_gap_stitch_accepted": sum(
                1
                for item in decisions
                if (item.get("micro_gap_stitch") or {}).get("accepted")
            ),
            "v2_selected_unit_master_merge_attempted": sum(
                1
                for item in decisions
                if (item.get("v2_selected_unit_master_merge") or {}).get("attempted")
            ),
            "v2_selected_unit_master_merge_accepted": sum(
                1
                for item in decisions
                if (item.get("v2_selected_unit_master_merge") or {}).get("accepted")
            ),
            "v2_master_first_attempted": sum(
                1
                for item in decisions
                if (item.get("v2_master_first") or {}).get("attempted")
            ),
            "v2_master_first_accepted": sum(
                1
                for item in decisions
                if (item.get("v2_master_first") or {}).get("accepted")
            ),
            "v2_master_first_dvrescue_improvement_attempted": sum(
                1
                for item in decisions
                if ((item.get("v2_master_first") or {}).get("dvrescue_improvement") or {}).get("attempted")
            ),
            "v2_master_first_dvrescue_improvement_accepted": sum(
                1
                for item in decisions
                if ((item.get("v2_master_first") or {}).get("dvrescue_improvement") or {}).get("accepted")
            ),
            "v2_master_first_timecode_loop_mitigations": sum(
                1
                for item in decisions
                if int(((item.get("v2_master_first") or {}).get("plan") or {}).get("timecode_loop_collision_count") or 0) > 0
            ),
            "synthetic_hold_frames": sum(
                int(((item.get("micro_gap_stitch") or {}).get("plan") or {}).get("synthetic_hold_frames") or 0)
                for item in decisions
                if (item.get("micro_gap_stitch") or {}).get("accepted")
            )
            + sum(
                int(((item.get("v2_master_first") or {}).get("plan") or {}).get("synthetic_hold_frames") or 0)
                for item in decisions
                if (item.get("v2_master_first") or {}).get("accepted")
            ),
            "keep_files": sum(len(item.get("keep_files") or []) for item in decisions),
            "unique_keep_files": len(keep_output_records),
            "duplicate_keep_outputs": len(duplicate_keep_outputs),
            "physical_keep_files": (
                len([path for path in out_root.iterdir() if path.is_file() and path.suffix.lower() in {".avi", ".dv"}])
                if args.apply and out_root.exists()
                else None
            ),
            "single_master_goal_clusters": sum(
                1
                for item in decisions
                if (item.get("master_status") or {}).get("single_master_goal")
            ),
            "single_master_proven_clusters": sum(
                1
                for item in decisions
                if (item.get("master_status") or {}).get("single_master_proven") is True
            ),
            "single_master_not_proven_clusters": sum(
                1
                for item in decisions
                if (item.get("master_status") or {}).get("single_master_proven") is False
            ),
            "clusters_with_multiple_verified_parts": sum(
                1
                for item in decisions
                if (item.get("master_status") or {}).get("status")
                == "single_master_not_proven_multiple_verified_parts"
            ),
            "unsafe_to_delete_sources": sum(
                1
                for decision in decisions
                for item in decision.get("source_disposition") or []
                if not item.get("safe_to_delete_after_keep_verification")
            ),
            "policy_excluded_source_files": sum(
                len(item.get("policy_excluded_sources") or [])
                for item in decisions
            ),
            "bad_frame_excluded_source_files": sum(
                1
                for source in sources
                if bad_frame_policy_for_source(source).get("bad_frame_excluded")
            ),
            "short_duration_excluded_source_files": sum(
                1
                for source in sources
                if bad_frame_policy_for_source(source).get("duration_excluded")
            ),
            "clusters_with_no_keep_due_to_policy": sum(
                1
                for item in decisions
                if item.get("keep_kind") == "all_sources_excluded_by_policy"
            ),
            "total_coverage_deficit_frames": total_coverage_deficit_frames,
            "total_coverage_deficit_seconds": total_coverage_deficit_frames / DV_FPS,
            "clusters_with_coverage_deficit": clusters_with_deficit,
            "completeness_ok": total_coverage_deficit_frames == 0,
            "deep_verify_ok": (deep_verify_summary or {}).get("ok") if deep_verify_summary else None,
            "deep_verify_foreign_frames": (deep_verify_summary or {}).get("frames_not_in_sources_total") if deep_verify_summary else None,
        },
        "conversion_self_tests": conversion_self_tests,
        "deep_verify": deep_verify_summary,
        "cleanup_warnings": cleanup_warnings,
        "sources": [source_to_json(source) for source in sources],
        "decisions": decisions,
        "delete_plan": delete_plan,
    }

    report_path = out_root / ("dvrescue_keep_report.json" if args.apply else "dvrescue_keep_report_dry_run.json")
    if args.apply:
        report_path.write_text(report_json_dumps(report), encoding="utf-8")
        if args.delete_covered_sources:
            if delete_blockers:
                print(
                    f"Refusing to delete sources: {len(delete_blockers)} source disposition(s) are not proven safe.",
                    flush=True,
                )
                return 2
            if deficit_blocks_delete:
                print(
                    f"Refusing to delete sources: KEEP is missing {total_coverage_deficit_frames} frame(s) "
                    f"of source coverage across {len(clusters_with_deficit)} cluster(s).",
                    flush=True,
                )
                return 2
            if deep_verify_blocks_delete:
                print(
                    "Refusing to delete sources: --deep-verify found output frames that are not byte-copies "
                    "of source frames.",
                    flush=True,
                )
                return 2
            deleted: list[str] = []
            for source in delete_eligible_sources:
                delete_file_inside_target(source.path)
                deleted.append(source.path.name)
            report["delete_plan"]["deleted_source_files"] = deleted
            report["delete_plan"]["blocked"] = False
            report_path.write_text(report_json_dumps(report), encoding="utf-8")
    else:
        out_root.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_json_dumps(report), encoding="utf-8")
        print(json.dumps(report["summary"], indent=2), flush=True)
    print(f"Report: {report_path}", flush=True)
    print(json.dumps(report["summary"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
