#!/usr/bin/env python3
"""Create a QTGMC/H.264 MP4 access copy from one DV AVI.

The source file is read-only input. The script:
  1. Reads AVI wrapper metadata with ffprobe.
  2. Reads embedded DV frame metadata with DVRescue.
  3. Deinterlaces with the existing VapourSynth/QTGMC extractor.
  4. Muxes AAC audio and archival metadata with FFmpeg.
  5. Writes the important QuickTime Keys/header/XMP fields with ExifTool.

By default, the only lasting output is the MP4 file. Temporary render/mux files
are cleaned up after a successful run.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parents[2]
RENDER_BACKEND_DIR = Path(__file__).resolve().parent / "render_backend"
QTGMC_EXTRACT = RENDER_BACKEND_DIR / "render_qtgmc_access_copy.py"
DVRESCUE = ROOT / "DVRescue.exe"
DEFAULT_EXIFTOOL = Path(os.environ["EXIFTOOL"]) if os.environ.get("EXIFTOOL") else None
DEFAULT_CAMERA_MAKE = os.environ.get("MINIDV_CAMERA_MAKE", "Camera Make")
DEFAULT_CAMERA_MODEL = os.environ.get("MINIDV_CAMERA_MODEL", "Camera Model")
DEFAULT_ARCHIVE_CONTRIBUTOR = os.environ.get("MINIDV_ARCHIVE_CONTRIBUTOR", "")
DEFAULT_TIMEZONE = os.environ.get("MINIDV_TIMEZONE", "UTC")
RECORDING_DATE_FRAME_NUMBER = 5
DV_FPS = 25


def int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def parse_dv_datetime_value(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    raw = value.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(raw[:19], fmt)
        except ValueError:
            pass
    return None


def format_dv_datetime_value(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def camera_model_tag(make: str | None, model: str | None) -> str:
    make = (make or "").strip()
    model = (model or "").strip()
    if make and model.lower().startswith(make.lower()):
        return model[len(make):].strip() or model
    return model


def archive_camera_label(make: str | None, model: str | None) -> str:
    make = (make or DEFAULT_CAMERA_MAKE).strip()
    model = (model or DEFAULT_CAMERA_MODEL).strip()
    if make and model and not model.lower().startswith(make.lower()):
        return f"{make} {model}"
    return model or make


def format_archive_title(recording_start: dt.datetime, cassette: str, camera_label: str) -> str:
    return f"{recording_start.strftime('%d. %m. %Y')} | Kassette: {cassette} | {camera_label}"


def which_or_path(name: str, fallback: Path | None = None) -> str:
    found = shutil.which(name)
    if found:
        return found
    if fallback and fallback.exists():
        return str(fallback)
    raise SystemExit(f"Required tool not found: {name}")


def run(cmd: list[str | Path], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-3000:]
        raise SystemExit(f"Command failed ({proc.returncode}):\n{' '.join(map(str, cmd))}\n\n{tail}")
    return proc


def ffprobe(path: Path) -> dict:
    proc = run([
        "ffprobe", "-v", "quiet", "-show_format", "-show_streams", "-of", "json", path
    ], timeout=120)
    return json.loads(proc.stdout)


def parse_avi_comment(data: dict) -> dict:
    tags = data.get("format", {}).get("tags", {}) or {}
    raw = tags.get("comment") or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_comment": raw}
    return parsed if isinstance(parsed, dict) else {"raw_comment": raw}


def first_xml_document(text: str) -> str:
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<dvrescue")
    return text[start:] if start >= 0 else text


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def analyze_dv_frames(path: Path) -> dict:
    import xml.etree.ElementTree as ET

    if not DVRESCUE.exists():
        raise SystemExit(f"DVRescue.exe not found at {DVRESCUE}")
    proc = run([DVRESCUE, path, "-v", "9"], timeout=1200)
    root = ET.fromstring(first_xml_document(proc.stdout))

    frame_elems = [elem for elem in root.iter() if local_name(elem.tag) == "frame"]
    frames_elems = [elem for elem in root.iter() if local_name(elem.tag) == "frames"]

    def attrs(frame):
        return {} if frame is None else dict(frame.attrib)

    first_frame = attrs(frame_elems[0] if frame_elems else None)
    last_frame = attrs(frame_elems[-1] if frame_elems else None)
    first_frames = frames_elems[0].attrib if frames_elems else {}
    last_frames = frames_elems[-1].attrib if frames_elems else {}

    frames_segments: list[dict] = []
    running_first_n = 0
    for elem in frames_elems:
        child_frames = [child for child in elem if local_name(child.tag) == "frame"]
        first_child = attrs(child_frames[0] if child_frames else None)
        last_child = attrs(child_frames[-1] if child_frames else None)
        count = int_or_none(elem.attrib.get("count"))
        first_n = int_or_none(first_child.get("n"))
        if first_n is None:
            first_n = running_first_n
        last_n = int_or_none(last_child.get("n"))
        if last_n is None and count is not None and first_n is not None:
            last_n = first_n + count - 1
        segment = {
            "count": count,
            "first_n": first_n,
            "last_n": last_n,
            "first_tc": first_child.get("tc"),
            "last_tc": last_child.get("tc"),
            "first_rdt": first_child.get("rdt"),
            "last_rdt": last_child.get("rdt"),
            "first_abst": int_or_none(elem.attrib.get("abst") or first_child.get("abst")),
            "last_abst": int_or_none(elem.attrib.get("end_abst") or last_child.get("abst")),
            "video_rate": elem.attrib.get("video_rate"),
            "audio_rate": elem.attrib.get("audio_rate"),
            "channels": elem.attrib.get("channels"),
            "chroma_subsampling": elem.attrib.get("chroma_subsampling"),
            "aspect_ratio": elem.attrib.get("aspect_ratio"),
            "speed": elem.attrib.get("speed"),
        }
        frames_segments.append(segment)
        if count is not None:
            running_first_n = first_n + count
        elif last_n is not None:
            running_first_n = last_n + 1

    target_frame_n = max(0, RECORDING_DATE_FRAME_NUMBER - 1)
    segment_frame_count = sum(segment["count"] or 0 for segment in frames_segments)
    frames_count = segment_frame_count or int(first_frames.get("count", len(frame_elems)) or len(frame_elems))
    if frames_count > 0:
        target_frame_n = min(target_frame_n, frames_count - 1)
    exact_frames_by_n = {
        n: attrs(frame)
        for frame in frame_elems
        for n in [int_or_none(frame.attrib.get("n"))]
        if n is not None
    }
    target_segment = next(
        (
            segment
            for segment in frames_segments
            if segment.get("first_n") is not None
            and segment.get("last_n") is not None
            and segment["first_n"] <= target_frame_n <= segment["last_n"]
        ),
        None,
    )
    recording_date_frame = dict(exact_frames_by_n.get(target_frame_n, {}))
    recording_date_frame_index = target_frame_n if frames_count > 0 else -1
    recording_date_frame_source = f"dv_frame_{RECORDING_DATE_FRAME_NUMBER}_not_found"

    if recording_date_frame.get("rdt"):
        recording_date_frame_source = f"dv_frame_{RECORDING_DATE_FRAME_NUMBER}_reported_with_rdt"
    elif target_segment:
        first_dt = parse_dv_datetime_value(target_segment.get("first_rdt"))
        last_dt = parse_dv_datetime_value(target_segment.get("last_rdt"))
        inferred = None
        if first_dt is not None and target_segment.get("first_n") is not None:
            inferred = first_dt + dt.timedelta(seconds=(target_frame_n - target_segment["first_n"]) / DV_FPS)
        elif last_dt is not None and target_segment.get("last_n") is not None:
            inferred = last_dt - dt.timedelta(seconds=(target_segment["last_n"] - target_frame_n) / DV_FPS)
        if inferred is not None:
            recording_date_frame.setdefault("n", str(target_frame_n))
            recording_date_frame["rdt"] = format_dv_datetime_value(inferred)
            recording_date_frame_source = f"dv_frame_{RECORDING_DATE_FRAME_NUMBER}_inferred_from_dvrescue_segment"
    if not recording_date_frame.get("rdt"):
        rdt_frames = [(index, attrs(frame)) for index, frame in enumerate(frame_elems) if frame.attrib.get("rdt")]
        if rdt_frames:
            recording_date_frame_index, recording_date_frame = rdt_frames[min(RECORDING_DATE_FRAME_NUMBER, len(rdt_frames)) - 1]
            recording_date_frame_source = "fallback_reported_dv_rdt_frame"
        else:
            recording_date_frame_index = 0 if frame_elems else -1
            recording_date_frame = first_frame
            recording_date_frame_source = "no_dv_rdt_available"

    frame_attr_keys = sorted({key for frame in frame_elems for key in frame.attrib})
    child_tags = sorted({local_name(child.tag) for frame in frame_elems for child in frame})
    problem_flags = sorted({
        key
        for frame in frame_elems
        for key in frame.attrib
        if key.endswith("_r") or key.endswith("_nc") or key.startswith("conceal_") or key.startswith("full_conceal_")
    })

    audio_problem_count = 0
    video_problem_count = 0
    problem_numbers: list[int] = []
    for frame in frame_elems:
        keys = set(frame.attrib)
        children = {local_name(child.tag) for child in frame}
        is_audio_problem = bool(
            keys & {"full_conceal_aud", "conceal_aud_type", "conceal_aud_value"} or "aud" in children
        )
        is_video_problem = bool(
            keys & {"full_conceal_vid", "conceal_vid_type", "conceal_vid_value"} or "vid" in children
        )
        is_problem = is_audio_problem or is_video_problem or bool(
            keys & {"abst_r", "tc_r", "rdt_r", "seqn_r", "abst_nc", "tc_nc", "rdt_nc", "seqn_nc", "source_error"}
        )
        if is_problem:
            try:
                problem_numbers.append(int(frame.attrib.get("n", "-1")))
            except ValueError:
                pass
        audio_problem_count += int(is_audio_problem)
        video_problem_count += int(is_video_problem)

    return {
        "frames_count": frames_count,
        "frames_segments": frames_segments,
        "first_frame": first_frame,
        "last_frame": last_frame,
        "first_tc": first_frame.get("tc"),
        "last_tc": last_frame.get("tc"),
        "first_rdt": first_frame.get("rdt"),
        "last_rdt": last_frame.get("rdt"),
        "recording_date_frame_number_requested": RECORDING_DATE_FRAME_NUMBER,
        "recording_date_frame_index": recording_date_frame_index,
        "recording_date_frame_n": recording_date_frame.get("n"),
        "recording_date_frame_source": recording_date_frame_source,
        "recording_date_frame": recording_date_frame,
        "recording_date_tc": recording_date_frame.get("tc"),
        "recording_date_rdt": recording_date_frame.get("rdt"),
        "first_abst": first_frames.get("abst") or first_frame.get("abst"),
        "last_abst": last_frames.get("end_abst") or last_frame.get("abst"),
        "video_rate": first_frames.get("video_rate"),
        "audio_rate": first_frames.get("audio_rate"),
        "channels": first_frames.get("channels"),
        "chroma_subsampling": first_frames.get("chroma_subsampling"),
        "aspect_ratio": first_frames.get("aspect_ratio"),
        "problem_frame_count": len(problem_numbers),
        "audio_problem_frame_count": audio_problem_count,
        "video_problem_frame_count": video_problem_count,
        "problem_frame_sample": problem_numbers[:20],
        "frame_attribute_keys": frame_attr_keys,
        "frame_child_tags": child_tags,
        "problem_flags_present": problem_flags,
        "raw_first_frames_group": dict(first_frames),
        "raw_last_frames_group": dict(last_frames),
    }


def parse_filename_fallback(path: Path) -> dict:
    stem = path.stem
    newer = re.match(
        r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})(?:__.*)?$",
        stem,
    )
    older = re.match(
        r"(?P<cassette>.+)\.(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})\.(?P<part>\d+)$",
        stem,
    )
    match = newer or older
    if not match:
        return {}
    data = match.groupdict()
    timestamp = f"{data['date']}T{data['time'].replace('-', ':')}"
    result = {"recording_start": timestamp}
    tc_match = re.search(r"__tc-(\d{2}-\d{2}-\d{2}-\d{2})(?:$|__)", stem)
    if tc_match:
        result["timecode_start"] = tc_match.group(1).replace("-", ":")
    return result


def cassette_label(source: Path, args) -> str:
    explicit = getattr(args, "cassette", None)
    label = (explicit or source.parent.name).strip()
    return label or source.stem


def parse_local_datetime(value: str | None, fallback: str | None = None) -> dt.datetime:
    raw = value or fallback
    if not raw:
        raise SystemExit("No recording date/time found in DV metadata, AVI tags, or filename")
    raw = raw.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    raise SystemExit(f"Could not parse recording date/time: {raw!r}")


def exiftool_datetime(value: dt.datetime, tz_name: str) -> tuple[str, str, str]:
    zone = ZoneInfo(tz_name)
    aware = value.replace(tzinfo=zone)
    local_no_tz = aware.strftime("%Y:%m:%d %H:%M:%S")
    offset = aware.strftime("%z")
    with_tz = f"{local_no_tz}{offset[:3]}:{offset[3:]}"
    iso_with_tz = aware.isoformat(timespec="seconds")
    return local_no_tz, with_tz, iso_with_tz


def build_metadata(source: Path, ffprobe_data: dict, avi_meta: dict, dv: dict, args) -> dict:
    tags = ffprobe_data.get("format", {}).get("tags", {}) or {}
    audio_stream = next(
        (stream for stream in ffprobe_data.get("streams", []) if stream.get("codec_type") == "audio"),
        {},
    )
    fallback = parse_filename_fallback(source)

    recording_start_raw = (
        dv.get("recording_date_rdt")
        or dv.get("first_rdt")
        or avi_meta.get("dv_recording_start")
        or tags.get("date")
        or fallback.get("recording_start")
    )
    if dv.get("recording_date_rdt"):
        recording_start_source = dv.get("recording_date_frame_source")
    elif dv.get("first_rdt"):
        recording_start_source = "first_dv_frame_rdt_fallback"
    elif avi_meta.get("dv_recording_start"):
        recording_start_source = "avi_comment_dv_recording_start"
    elif tags.get("date"):
        recording_start_source = "avi_format_date_tag"
    else:
        recording_start_source = "filename_fallback"
    recording_end_raw = dv.get("last_rdt") or avi_meta.get("dv_recording_end")
    recording_start = parse_local_datetime(recording_start_raw)
    recording_end = None
    if recording_end_raw:
        try:
            recording_end = parse_local_datetime(recording_end_raw)
        except SystemExit:
            recording_end = None

    local_date, offset_date, iso_offset = exiftool_datetime(recording_start, args.timezone)
    _, recording_end_offset, recording_end_iso = (
        exiftool_datetime(recording_end, args.timezone) if recording_end else ("", "", "")
    )

    cassette = cassette_label(source, args)
    camera_make = args.camera_make.strip()
    camera_model = args.camera_model.strip()
    camera_label = archive_camera_label(camera_make, camera_model)
    source_files = avi_meta.get("source_files") or [source.name]
    tc_start = dv.get("first_tc") or avi_meta.get("dv_timecode_start") or fallback.get("timecode_start")
    tc_end = dv.get("last_tc") or avi_meta.get("dv_timecode_end")
    source_audio_rate = dv.get("audio_rate") or audio_stream.get("sample_rate")
    source_audio_channels = dv.get("channels") or audio_stream.get("channels")
    title = args.title or format_archive_title(recording_start, cassette, camera_label)
    keywords = [
        "MiniDV",
        "PAL DV",
        cassette,
        camera_label,
        "QTGMC",
        args.encoder,
        f"CRF {args.crf:g}",
        "tune grain" if args.encoder == "libx264" and args.tune == "grain" else f"tune {args.tune}" if args.encoder == "libx264" and args.tune else "",
    ]
    keywords = [item for item in keywords if item]
    source_ref = f"MiniDV tape {cassette} / {source.name}"
    description = (
        f"PAL DV 720x576 BFF source from {camera_label}. "
        f"Deinterlaced with QTGMC {args.mode} to 50p. "
        f"Encoded as H.264 MP4, {args.encoder} "
        f"{'preset ' + args.preset + ', ' if args.encoder == 'libx264' else 'preset ' + args.nvenc_preset + ', '}"
        f"quality {args.crf:g}"
        f"{', tune ' + args.tune if args.encoder == 'libx264' and args.tune else ''}, "
        f"yuv420p; audio AAC {args.audio_bitrate}. "
        f"Original DV timecode {tc_start or 'unknown'}"
        + (f" to {tc_end}." if tc_end else ".")
    )
    processing = {
        "deinterlacer": "QTGMC via VapourSynth",
        "deinterlacer_mode_id": args.mode,
        "field_order": "BFF",
        "output_rate": "50p",
        "encoder": args.encoder,
        "preset": args.preset,
        "nvenc_preset": args.nvenc_preset,
        "crf": args.crf,
        "nvenc_cq": args.nvenc_cq,
        "tune": args.tune,
        "pix_fmt": "yuv420p",
        "profile": "high",
        "level": args.x264_level,
        "audio": f"AAC {args.audio_bitrate}",
    }
    return {
        "recording_start": recording_start,
        "recording_start_source": recording_start_source,
        "recording_start_frame_number_requested": dv.get("recording_date_frame_number_requested"),
        "recording_start_frame_index": dv.get("recording_date_frame_index"),
        "recording_start_frame_n": dv.get("recording_date_frame_n"),
        "recording_start_local": local_date,
        "recording_start_offset": offset_date,
        "recording_start_iso_offset": iso_offset,
        "recording_end": recording_end,
        "recording_end_offset": recording_end_offset,
        "recording_end_iso_offset": recording_end_iso,
        "cassette": cassette,
        "camera_make": camera_make,
        "camera_model": camera_model,
        "camera_label": camera_label,
        "title": title,
        "author": args.author,
        "contributor": args.contributor,
        "description": description,
        "keywords": keywords,
        "source_ref": source_ref,
        "source_files": source_files,
        "timecode_start": tc_start,
        "timecode_end": tc_end,
        "source_audio_rate": source_audio_rate,
        "source_audio_channels": source_audio_channels,
        "processing": processing,
    }


def render_video_only(source: Path, out: Path, args) -> list[str]:
    cmd: list[str | Path] = [
        sys.executable,
        QTGMC_EXTRACT,
        "--input",
        source,
        "--mode",
        args.mode,
        "--frame",
        "0",
        "--full-video",
        "--encode-preset",
        args.preset,
        "--crf",
        str(args.crf),
        "--pix-fmt",
        "yuv420p",
        "--x264-profile",
        "high",
        "--x264-level",
        args.x264_level,
        "--out",
        out,
        "--encoder",
        args.encoder,
    ]
    if args.tune:
        cmd.extend(["--x264-tune", args.tune])
    if args.encoder == "h264_nvenc":
        cmd.extend(["--nvenc-preset", args.nvenc_preset, "--nvenc-tune", args.nvenc_tune])
        if args.nvenc_cq is not None:
            cmd.extend(["--nvenc-cq", str(args.nvenc_cq)])
    run(cmd, timeout=args.render_timeout)
    return [str(part) for part in cmd]


def mux_audio(video_only: Path, source: Path, out: Path, args) -> list[str]:
    cmd: list[str | Path] = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        video_only,
        "-i",
        source,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "copy",
        "-bsf:v",
        "h264_metadata=video_full_range_flag=0:colour_primaries=5:transfer_characteristics=6:matrix_coefficients=5",
        "-c:a",
        "aac",
        "-b:a",
        args.audio_bitrate,
        "-ar",
        str(args.audio_rate),
        "-ac",
        str(args.audio_channels),
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
    ]
    cmd.append(out)
    run(cmd, timeout=args.mux_timeout)
    return [str(part) for part in cmd]


def stamp_exiftool_metadata(output: Path, meta: dict, args) -> list[str]:
    exiftool = which_or_path("exiftool", DEFAULT_EXIFTOOL)
    camera_model_for_tag = camera_model_tag(meta["camera_make"], meta["camera_model"])
    now = dt.datetime.now().astimezone().strftime("%Y:%m:%d %H:%M:%S%z")
    now = f"{now[:-2]}:{now[-2:]}"
    document_id = f"uuid:{uuid.uuid5(uuid.NAMESPACE_URL, str(output.resolve()))}"
    instance_id = f"uuid:{uuid.uuid4()}"
    source_doc_id = f"file:{args.input.name}"
    if args.encoder == "libx264":
        encoder_params = f"libx264 preset={args.preset}; crf={args.crf:g}; tune={args.tune or 'none'}; "
        encoder_tag = f"preset {args.preset} CRF {args.crf:g} tune {args.tune or 'none'}"
    else:
        nvenc_quality = args.nvenc_cq if args.nvenc_cq is not None else args.crf
        encoder_params = f"h264_nvenc preset={args.nvenc_preset}; cq={nvenc_quality:g}; tune={args.nvenc_tune}; "
        encoder_tag = f"preset {args.nvenc_preset} CQ {nvenc_quality:g} tune {args.nvenc_tune}"

    history = [
        {
            "action": "produced",
            "softwareAgent": "DV capture / DVRescue / FFmpeg",
            "when": meta["recording_start_offset"],
            "parameters": (
                "Source PAL DV; 720x576; 25i/50 fields; BFF; DV25; "
                f"audio_rate={meta.get('source_audio_rate')}; channels={meta.get('source_audio_channels')}"
            ),
        },
        {
            "action": "deinterlaced",
            "softwareAgent": "QTGMC via VapourSynth",
            "when": now,
            "parameters": f"mode={args.mode}; FieldOrder=BFF; double-rate 50p",
        },
        {
            "action": "encoded",
            "softwareAgent": f"FFmpeg/{args.encoder}",
            "when": now,
            "parameters": (
                encoder_params
                +
                f"pix_fmt=yuv420p; profile=high; level={args.x264_level}; audio=AAC {args.audio_bitrate}"
            ),
        },
    ]

    command: list[str | Path] = [
        exiftool,
        "-m",
        "-P",
        "-overwrite_original",
        "-api",
        "QuickTimeUTC=1",
        f"-Keys:CreationDate={meta['recording_start_offset']}",
        f"-QuickTime:CreateDate={meta['recording_start_local']}",
        f"-QuickTime:ModifyDate={meta['recording_start_local']}",
        f"-Track*Date={meta['recording_start_local']}",
        f"-Media*Date={meta['recording_start_local']}",
        f"-Keys:Make={meta['camera_make']}",
        f"-Keys:Model={camera_model_for_tag}",
        f"-XMP-tiff:Make={meta['camera_make']}",
        f"-XMP-tiff:Model={camera_model_for_tag}",
        f"-UserData:Make={meta['camera_make']}",
        f"-UserData:Model={camera_model_for_tag}",
        f"-Keys:Title={meta['title']}",
        f"-Keys:Description={meta['description']}",
        f"-Keys:Keywords={'; '.join(meta['keywords'])}",
        f"-Keys:Encoder={args.encoder} {encoder_tag}",
        f"-XMP-dc:Title={meta['title']}",
        f"-XMP-dc:Description={meta['description']}",
        f"-XMP-dc:Subject={'; '.join(meta['keywords'])}",
        f"-XMP-dc:Source={meta['source_ref']}",
        f"-XMP-xmp:CreateDate={meta['recording_start_offset']}",
        f"-XMP-xmp:ModifyDate={meta['recording_start_offset']}",
        f"-XMP-exif:DateTimeOriginal={meta['recording_start_offset']}",
        f"-XMP-photoshop:DateCreated={meta['recording_start_offset']}",
        f"-XMP:CreatorTool=FFmpeg/{args.encoder} + QTGMC via VapourSynth",
        f"-XMP-xmpMM:DocumentID={document_id}",
        f"-XMP-xmpMM:InstanceID={instance_id}",
        f"-XMP-xmpMM:OriginalDocumentID={source_doc_id}",
        f"-XMP-xmpMM:DerivedFromDocumentID={source_doc_id}",
        f"-XMP-xmpMM:DerivedFromFilePath={args.input.name}",
    ]
    if meta.get("contributor"):
        command.extend([
            f"-ItemList:Artist={meta['contributor']}",
            f"-XMP-dc:Contributor={meta['contributor']}",
        ])
    for event in history:
        command.append(
            "-XMP-xmpMM:History+="
            + "{"
            + ",".join(f"{key}={value}" for key, value in event.items())
            + "}"
        )
    command.append(output)
    run(command, timeout=120)
    return [str(part) for part in command]


def stamp_file_dates(output: Path, meta: dict, args) -> list[str] | None:
    if not args.set_file_dates:
        return None
    zone = ZoneInfo(args.timezone)
    aware = meta["recording_start"].replace(tzinfo=zone)
    timestamp = aware.timestamp()
    os.utime(output, (timestamp, timestamp))
    if sys.platform == "win32":
        filetime = int(timestamp * 10_000_000) + 116444736000000000
        filetime_obj = ctypes.wintypes.FILETIME(filetime & 0xFFFFFFFF, filetime >> 32)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.SetFileTime.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.FILETIME),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.FILETIME),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateFileW(
            str(output),
            0x0100,  # FILE_WRITE_ATTRIBUTES
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), f"Could not open output to set file dates: {output}")
        try:
            ok = kernel32.SetFileTime(
                handle,
                ctypes.byref(filetime_obj),
                None,
                ctypes.byref(filetime_obj),
            )
            if not ok:
                raise OSError(ctypes.get_last_error(), f"Could not set file dates: {output}")
        finally:
            kernel32.CloseHandle(handle)
    return ["native-file-dates", str(output), meta["recording_start_offset"]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Source DV AVI file. It is never modified.")
    parser.add_argument("output", type=Path, help="Final MP4 output path.")
    parser.add_argument("--force", action="store_true", help="Overwrite output if it already exists.")
    parser.add_argument("--mode", default="slower_default", help="Deinterlacer mode id from render_backend/config.json.")
    parser.add_argument("--preset", default="fast", help="x264 preset. Use --preset slower for the previous archival default.")
    parser.add_argument("--crf", type=float, default=17.0, help="x264 CRF, or NVENC CQ fallback when --nvenc-cq is omitted.")
    parser.add_argument("--tune", default="grain", help="x264 tune; use empty string for none.")
    parser.add_argument("--encoder", default="libx264", choices=["libx264", "h264_nvenc"], help="Video encoder.")
    parser.add_argument("--nvenc-preset", default="p7", help="NVENC preset when --encoder h264_nvenc.")
    parser.add_argument("--nvenc-cq", type=float, help="NVENC CQ value; defaults to --crf when omitted.")
    parser.add_argument("--nvenc-tune", default="hq", help="NVENC tune when --encoder h264_nvenc.")
    parser.add_argument("--x264-level", default="4.0")
    parser.add_argument("--audio-bitrate", default="192k")
    parser.add_argument("--audio-rate", type=int, default=48000)
    parser.add_argument("--audio-channels", type=int, default=2)
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone for timezone-aware recording dates, e.g. Europe/Berlin. Default: UTC or MINIDV_TIMEZONE.",
    )
    parser.add_argument("--author", default="")
    parser.add_argument("--contributor", default=DEFAULT_ARCHIVE_CONTRIBUTOR)
    parser.add_argument(
        "--camera-make",
        default=DEFAULT_CAMERA_MAKE,
        help="Camera manufacturer written to MP4 metadata. Replace the placeholder for your own tapes.",
    )
    parser.add_argument(
        "--camera-model",
        default=DEFAULT_CAMERA_MODEL,
        help="Camera model written to MP4 metadata and titles. Replace the placeholder for your own tapes.",
    )
    parser.add_argument(
        "--cassette",
        help="Cassette label for metadata. Defaults to the source folder name; filenames are not used for cassette identity.",
    )
    parser.add_argument("--title")
    parser.add_argument("--no-file-dates", dest="set_file_dates", action="store_false", help="Do not set output file dates.")
    parser.set_defaults(set_file_dates=True)
    parser.add_argument("--render-timeout", type=int, default=7200)
    parser.add_argument("--mux-timeout", type=int, default=1200)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.contributor = (args.contributor or "").strip()
    args.camera_make = (args.camera_make or DEFAULT_CAMERA_MAKE).strip() or DEFAULT_CAMERA_MAKE
    args.camera_model = (args.camera_model or DEFAULT_CAMERA_MODEL).strip() or DEFAULT_CAMERA_MODEL
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if not args.input.exists() or not args.input.is_file():
        raise SystemExit(f"Input file does not exist: {args.input}")
    if args.output.exists() and not args.force:
        raise SystemExit(f"Output already exists: {args.output} (pass --force to overwrite)")
    if not QTGMC_EXTRACT.exists():
        raise SystemExit(f"QTGMC extractor not found: {QTGMC_EXTRACT}")
    which_or_path("ffmpeg")
    which_or_path("ffprobe")
    which_or_path("exiftool", DEFAULT_EXIFTOOL)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    token = uuid.uuid4().hex[:10]
    temp_video = args.output.with_name(f"{args.output.stem}.__{token}.video.mp4")
    temp_muxed = args.output.with_name(f"{args.output.stem}.__{token}.muxed.mp4")

    ffprobe_data = ffprobe(args.input)
    avi_meta = parse_avi_comment(ffprobe_data)
    dv = analyze_dv_frames(args.input)
    args.source_dv = dv
    meta = build_metadata(args.input, ffprobe_data, avi_meta, dv, args)

    commands: dict[str, list[str]] = {}
    try:
        commands["qtgmc_render"] = render_video_only(args.input, temp_video, args)
        commands["ffmpeg_mux"] = mux_audio(temp_video, args.input, temp_muxed, args)
        if args.output.exists():
            args.output.unlink()
        temp_muxed.replace(args.output)
        commands["exiftool_metadata"] = stamp_exiftool_metadata(args.output, meta, args)
        file_dates_command = stamp_file_dates(args.output, meta, args)
        if file_dates_command:
            commands["exiftool_file_dates"] = file_dates_command
    finally:
        temp_video.unlink(missing_ok=True)
        temp_muxed.unlink(missing_ok=True)

    print(json.dumps({
        "ok": True,
        "output": str(args.output),
        "recording_date": meta["recording_start_offset"],
        "recording_date_source": meta["recording_start_source"],
        "recording_date_frame_n": meta["recording_start_frame_n"],
        "timecode": {"start": meta.get("timecode_start"), "end": meta.get("timecode_end")},
        "source_read_only": str(args.input),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
