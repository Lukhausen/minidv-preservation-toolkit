#!/usr/bin/env python3
"""Crash-resumable batch conversion for the raw MiniDV archive.

The raw source tree is read-only input. Each source is converted into a staging
MP4 inside the state/temp tree, validated for media shape and required archival
metadata, then published to the final output root. A rerun resumes by validating
existing final MP4s and skipping only files that are already complete.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import datetime as dt
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_ROOT = ROOT / "raw_recordings"
DEFAULT_OUTPUT_ROOT = ROOT / "final_mp4_access_copies"
DEFAULT_STATE_ROOT = ROOT / "TEMP" / "mp4_batch_state"
DEFAULT_STITCH_DECISIONS = ROOT / "data" / "stitching" / "stitch_decisions_v1.json"
CONVERTER = Path(__file__).resolve().with_name("dv_to_mp4_access_copy.py")
DEFAULT_EXIFTOOL = Path(os.environ["EXIFTOOL"]) if os.environ.get("EXIFTOOL") else None
DEFAULT_ARCHIVE_CONTRIBUTOR = os.environ.get("MINIDV_ARCHIVE_CONTRIBUTOR", "")
DEFAULT_CAMERA_MAKE = os.environ.get("MINIDV_CAMERA_MAKE", "Camera Make")
DEFAULT_CAMERA_MODEL = os.environ.get("MINIDV_CAMERA_MODEL", "Camera Model")
DEFAULT_TIMEZONE = os.environ.get("MINIDV_TIMEZONE", "UTC")
EXPECTED_DEFAULT_SOURCE_COUNT = 0
EXPECTED_DEFAULT_STITCH_CHAIN_COUNT = 0
SOURCE_EXTENSIONS = {".avi", ".dv"}
REQUIRED_METADATA = [
    "CreationDate",
    "ModifyDate",
    "TrackCreateDate",
    "TrackModifyDate",
    "MediaCreateDate",
    "MediaModifyDate",
    "Make",
    "Model",
    "Title",
    "Encoder",
    "Description",
    "CreateDate",
    "DateTimeOriginal",
    "DateCreated",
    "CreatorTool",
    "Source",
    "DerivedFromFilePath",
    "HistoryAction",
    "HistoryParameters",
    "HistorySoftwareAgent",
]
FORBIDDEN_DEFAULT_METADATA = [
    "Keys:Author",
    "Keys:Artist",
    "Keys:Software",
    "UserData:Author",
    "UserData:Artist",
    "XMP-dc:Creator",
]


class BatchError(RuntimeError):
    pass


def which_or_path(name: str, fallback: Path | None = None) -> str:
    found = shutil.which(name)
    if found:
        return found
    if fallback and fallback.exists():
        return str(fallback)
    raise BatchError(f"Required tool not found: {name}")


def run(cmd: list[str | Path], *, timeout: int | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-3000:]
        raise BatchError(f"Command failed ({proc.returncode}):\n{' '.join(map(str, cmd))}\n\n{tail}")
    return proc


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def event_log(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"at": now_iso(), **event}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def resolved_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def path_anchor_exists(path: Path) -> bool:
    anchor = path.anchor
    if not anchor:
        return True
    return Path(anchor).exists()


def assert_generated_delete_target(path: Path, allowed_root: Path, source_root: Path, label: str) -> None:
    resolved = path.resolve()
    allowed = allowed_root.resolve()
    source = source_root.resolve()
    if resolved in {ROOT.resolve(), allowed, source}:
        raise BatchError(f"Refusing to clean {label} at unsafe root path: {resolved}")
    if resolved_under(resolved, source) or resolved_under(source, resolved):
        raise BatchError(f"Refusing to clean {label} because it overlaps source material: {resolved}")
    if not resolved_under(resolved, allowed):
        raise BatchError(f"Refusing to clean {label} outside allowed generated root {allowed}: {resolved}")


def is_empty_dir(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is None


def state_summary_matches_output_root(args: argparse.Namespace) -> bool:
    summary_path = args.state_root / "summary.json"
    if not summary_path.exists():
        return False
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        previous_output = Path(data.get("output_root", "")).expanduser().resolve()
        previous_source = Path(data.get("source_root", "")).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return False
    return previous_output == args.output_root.resolve() and previous_source == args.source_root.resolve()


def clean_output_root(args: argparse.Namespace, cleaned: list[str]) -> None:
    if not args.output_root.exists():
        return
    output_resolved = args.output_root.resolve()
    if args.output_root.anchor and output_resolved == Path(args.output_root.anchor).resolve():
        raise BatchError(f"Refusing to clean output root because it is a drive/share root: {output_resolved}")
    if resolved_under(output_resolved, args.source_root) or resolved_under(args.source_root, output_resolved):
        raise BatchError(f"Refusing to clean output root because it overlaps source material: {output_resolved}")
    if resolved_under(output_resolved, ROOT):
        assert_generated_delete_target(args.output_root, ROOT, args.source_root, "output root")
        delete_root = ROOT
    elif is_empty_dir(args.output_root) or state_summary_matches_output_root(args):
        delete_root = args.output_root.parent
    else:
        raise BatchError(
            "Refusing clean-start of external output root without matching prior state summary. "
            f"Use an empty/new final output folder, or clean it manually after inspection: {output_resolved}"
        )
    if args.output_root.is_dir():
        safe_rmtree(args.output_root, delete_root)
    else:
        args.output_root.unlink()
    cleaned.append(str(args.output_root))


def clean_generated_outputs(args: argparse.Namespace) -> dict:
    if args.dry_run:
        raise BatchError("--clean-start cannot be combined with --dry-run")
    lock_path = args.state_root / "runner.lock"
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            if pid_is_running(int(data.get("pid", -1))) and not args.force_lock:
                raise BatchError(f"Refusing clean-start while runner lock is active: {lock_path}")
        except json.JSONDecodeError:
            if not args.force_lock:
                raise BatchError(f"Refusing clean-start with unreadable runner lock: {lock_path}")

    cleaned: list[str] = []
    clean_output_root(args, cleaned)
    if args.state_root.exists():
        temp_root = ROOT / "TEMP"
        assert_generated_delete_target(args.state_root, temp_root, args.source_root, "state root")
        if args.state_root.is_dir():
            safe_rmtree(args.state_root, temp_root)
        else:
            args.state_root.unlink()
        cleaned.append(str(args.state_root))
    args.state_root.mkdir(parents=True, exist_ok=True)
    return {"cleaned": cleaned}


def safe_rmtree(path: Path, root: Path, *, retries: int = 6) -> None:
    if not path.exists():
        return
    if not resolved_under(path, root):
        raise BatchError(f"Refusing to delete path outside work root: {path}")
    last_exc: Exception | None = None
    def make_writable(func, failed_path, exc_info):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except Exception as exc:  # noqa: BLE001
            raise exc

    for attempt in range(retries):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=make_writable)
            else:
                shutil.rmtree(path, onerror=lambda func, failed_path, exc_info: make_writable(func, failed_path, exc_info))
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            time.sleep(0.25 * (attempt + 1))
    raise BatchError(f"Could not remove work directory after retries: {path}: {last_exc}")


def ffprobe(path: Path) -> dict:
    proc = run([
        "ffprobe",
        "-v",
        "quiet",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        path,
    ], timeout=120)
    return json.loads(proc.stdout)


def parse_rate(raw: str | None) -> float | None:
    if not raw or raw == "0/0":
        return None
    try:
        return float(Fraction(raw))
    except (ValueError, ZeroDivisionError):
        return None


def duration_seconds(probe: dict) -> float | None:
    raw = probe.get("format", {}).get("duration")
    if raw and raw != "N/A":
        try:
            return float(raw)
        except ValueError:
            pass
    for stream in probe.get("streams", []):
        raw = stream.get("duration")
        if raw and raw != "N/A":
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def metadata_value_matches(value, expected: str) -> bool:
    if isinstance(value, list):
        return expected in [str(item) for item in value]
    return str(value) == expected


def required_metadata_fields(contributor: str | None) -> list[str]:
    fields = list(REQUIRED_METADATA)
    if contributor:
        fields.extend(["Artist", "Contributor"])
    return fields


def archive_date_part(value) -> str:
    if not value:
        return "Unbekanntes Datum"
    raw = str(value).strip()
    head = raw[:10].replace(":", "-")
    parts = head.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        year, month, day = parts
        return f"{day}. {month}. {year}"
    return "Unbekanntes Datum"


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


def archive_title_from_metadata(meta: dict, cassette: str) -> str:
    date_value = meta.get("CreationDate") or meta.get("CreateDate") or meta.get("DateTimeOriginal")
    return f"{archive_date_part(date_value)} | Kassette: {cassette} | {archive_camera_label(meta.get('Make'), meta.get('Model'))}"


def source_has_audio(source_probe: dict) -> bool:
    return any(stream.get("codec_type") == "audio" for stream in source_probe.get("streams", []))


def exiftool_json(path: Path, exiftool: str) -> dict:
    tags = [
        "-CreationDate",
        "-QuickTime:CreateDate",
        "-QuickTime:ModifyDate",
        "-TrackCreateDate",
        "-TrackModifyDate",
        "-MediaCreateDate",
        "-MediaModifyDate",
        "-Keys:Make",
        "-Keys:Model",
        "-Keys:Title",
        "-ItemList:Artist",
        "-Keys:Encoder",
        "-Keys:Description",
        "-XMP-xmp:CreateDate",
        "-XMP-exif:DateTimeOriginal",
        "-XMP-photoshop:DateCreated",
        "-XMP:CreatorTool",
        "-XMP-dc:Contributor",
        "-XMP-dc:Source",
        "-XMP-xmpMM:DerivedFromFilePath",
        "-XMP-xmpMM:HistoryAction",
        "-XMP-xmpMM:HistoryParameters",
        "-XMP-xmpMM:HistorySoftwareAgent",
    ]
    proc = run([exiftool, "-j", "-api", "QuickTimeUTC=1", *tags, path], timeout=120)
    data = json.loads(proc.stdout)
    return data[0] if data else {}


def exiftool_grouped_json(path: Path, exiftool: str, tags: list[str]) -> dict:
    proc = run([exiftool, "-j", "-G1", "-a", "-s", "-api", "QuickTimeUTC=1", *tags, path], timeout=120)
    data = json.loads(proc.stdout)
    return data[0] if data else {}


def reject_forbidden_default_metadata(path: Path, exiftool: str) -> list[str]:
    grouped = exiftool_grouped_json(path, exiftool, [f"-{tag}" for tag in FORBIDDEN_DEFAULT_METADATA])
    reasons = []
    for tag in FORBIDDEN_DEFAULT_METADATA:
        if grouped.get(tag):
            reasons.append(f"forbidden default metadata {tag} is present: {grouped.get(tag)!r}")
    return reasons


def ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_shell_details(path: Path) -> dict:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "$p = " + ps_single_quote(str(path.resolve())) + ";"
        "$shell = New-Object -ComObject Shell.Application;"
        "$folder = $shell.Namespace([System.IO.Path]::GetDirectoryName($p));"
        "$item = $folder.ParseName([System.IO.Path]::GetFileName($p));"
        "[pscustomobject]@{"
        "Title=$folder.GetDetailsOf($item,21);"
        "ContributingArtists=$folder.GetDetailsOf($item,13);"
        "Authors=$folder.GetDetailsOf($item,20);"
        "AlbumArtist=$folder.GetDetailsOf($item,239);"
        "MediaCreated=$folder.GetDetailsOf($item,210)"
        "} | ConvertTo-Json -Compress"
    )
    proc = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=60)
    return json.loads(proc.stdout)


def create_preflight_sample_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x576:rate=50:duration=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "35",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            path,
        ],
        timeout=120,
    )


def stamp_preflight_metadata(path: Path, exiftool: str, args: argparse.Namespace) -> str:
    local_date = "2000:01:02 14:20:23"
    offset_date = "2000:01:02 14:20:23+00:00"
    cassette = "TAPE-001"
    camera_make = args.camera_make.strip()
    camera_model = args.camera_model.strip()
    camera_model_for_tag = camera_model_tag(camera_make, camera_model)
    camera_label = archive_camera_label(camera_make, camera_model)
    title = f"02. 01. 2000 | Kassette: {cassette} | {camera_label}"
    description = "Metadata contract preflight for the MiniDV access-copy batch runner."
    keywords = f"MiniDV; PAL DV; {cassette}; {camera_label}"
    history = [
        "{action=produced,softwareAgent=metadata preflight,when=2000:01:02 14:20:23+00:00,parameters=synthetic MP4 metadata mapping probe}",
        "{action=deinterlaced,softwareAgent=QTGMC via VapourSynth,when=2000:01:02 15:00:00+00:00,parameters=mode=preflight; FieldOrder=BFF; double-rate 50p}",
        "{action=encoded,softwareAgent=FFmpeg/libx264,when=2000:01:02 15:10:00+00:00,parameters=libx264 preset=ultrafast; crf=35; pix_fmt=yuv420p}",
    ]
    clear = [
        "-Keys:Title=",
        "-ItemList:Title=",
        "-UserData:Title=",
        "-XMP-dc:Title=",
        "-Keys:Author=",
        "-ItemList:Author=",
        "-UserData:Author=",
        "-XMP-dc:Creator=",
        "-Keys:Artist=",
        "-UserData:Artist=",
        "-Keys:Software=",
        "-XMP-dc:Contributor=",
    ]
    command: list[str | Path] = [
        exiftool,
        "-m",
        "-P",
        "-overwrite_original",
        "-api",
        "QuickTimeUTC=1",
        *clear,
        f"-Keys:CreationDate={offset_date}",
        f"-QuickTime:CreateDate={local_date}",
        f"-QuickTime:ModifyDate={local_date}",
        f"-Track*Date={local_date}",
        f"-Media*Date={local_date}",
        f"-Keys:Make={camera_make}",
        f"-Keys:Model={camera_model_for_tag}",
        f"-XMP-tiff:Make={camera_make}",
        f"-XMP-tiff:Model={camera_model_for_tag}",
        f"-UserData:Make={camera_make}",
        f"-UserData:Model={camera_model_for_tag}",
        f"-Keys:Title={title}",
        f"-Keys:Description={description}",
        f"-Keys:Keywords={keywords}",
        "-Keys:Encoder=libx264 preset ultrafast CRF 35 tune none",
        f"-XMP-dc:Title={title}",
        f"-XMP-dc:Description={description}",
        f"-XMP-dc:Subject={keywords}",
        f"-XMP-dc:Source=MiniDV tape {cassette} / synthetic-metadata-preflight.dv",
        f"-XMP-xmp:CreateDate={offset_date}",
        f"-XMP-xmp:ModifyDate={offset_date}",
        f"-XMP-exif:DateTimeOriginal={offset_date}",
        f"-XMP-photoshop:DateCreated={offset_date}",
        "-XMP:CreatorTool=FFmpeg/libx264 + QTGMC via VapourSynth",
        "-XMP-xmpMM:DerivedFromFilePath=synthetic-metadata-preflight.dv",
        *[f"-XMP-xmpMM:History+={event}" for event in history],
        path,
    ]
    if args.contributor:
        command[-1:-1] = [
            f"-ItemList:Artist={args.contributor}",
            f"-XMP-dc:Contributor={args.contributor}",
        ]
    run(command, timeout=120)
    return title


def run_metadata_preflight(args: argparse.Namespace, exiftool: str) -> dict:
    preflight_dir = args.state_root / "preflight"
    if preflight_dir.exists():
        safe_rmtree(preflight_dir, args.state_root)
    preflight_dir.mkdir(parents=True, exist_ok=True)
    sample = preflight_dir / "metadata_contract_probe.mp4"
    create_preflight_sample_mp4(sample)
    expected_title = stamp_preflight_metadata(sample, exiftool, args)

    meta = exiftool_json(sample, exiftool)
    grouped = exiftool_grouped_json(
        sample,
        exiftool,
        [
            "-Keys:Title",
            "-ItemList:Artist",
            "-Keys:Artist",
            "-Keys:Author",
            "-UserData:Artist",
            "-UserData:Author",
            "-Keys:Software",
            "-XMP-dc:Creator",
            "-XMP-dc:Contributor",
            "-XMP:CreatorTool",
        ],
    )
    probe = ffprobe(sample)
    tags = probe.get("format", {}).get("tags", {}) or {}
    shell = windows_shell_details(sample) if os.name == "nt" else {}

    errors: list[str] = []
    warnings: list[str] = []
    for key in required_metadata_fields(args.contributor):
        if not meta.get(key):
            errors.append(f"preflight missing metadata field {key}")
    if meta.get("Make") != args.camera_make:
        errors.append(f"preflight Make is {meta.get('Make')!r}, expected {args.camera_make!r}")
    expected_model = camera_model_tag(args.camera_make, args.camera_model)
    if meta.get("Model") != expected_model:
        errors.append(f"preflight Model is {meta.get('Model')!r}, expected {expected_model!r}")
    if meta.get("Title") != expected_title:
        errors.append(f"preflight Title is {meta.get('Title')!r}, expected {expected_title!r}")
    if args.contributor:
        if not metadata_value_matches(meta.get("Artist"), args.contributor):
            errors.append(f"preflight ItemList Artist is {meta.get('Artist')!r}")
        if not metadata_value_matches(meta.get("Contributor"), args.contributor):
            errors.append(f"preflight XMP Contributor is {meta.get('Contributor')!r}")
    else:
        if meta.get("Artist"):
            errors.append(f"preflight ItemList Artist should be empty, got {meta.get('Artist')!r}")
        if meta.get("Contributor"):
            errors.append(f"preflight XMP Contributor should be empty, got {meta.get('Contributor')!r}")
    creator_tool = meta.get("CreatorTool") or ""
    if "FFmpeg" not in creator_tool or "QTGMC" not in creator_tool:
        errors.append(f"preflight CreatorTool does not identify FFmpeg/QTGMC: {creator_tool!r}")
    errors.extend(reject_forbidden_default_metadata(sample, exiftool))
    ffprobe_title = tags.get("title") or tags.get("com.apple.quicktime.title")
    if ffprobe_title != expected_title:
        errors.append(f"ffprobe title is {ffprobe_title!r}, expected {expected_title!r}")
    if os.name == "nt":
        if shell.get("Title") != expected_title:
            errors.append(f"Windows Titel is {shell.get('Title')!r}, expected {expected_title!r}")
        if args.contributor and shell.get("ContributingArtists") != args.contributor:
            errors.append(
                f"Windows Mitwirkende Interpreten is {shell.get('ContributingArtists')!r}, "
                f"expected {args.contributor!r}"
            )
        if not args.contributor and shell.get("ContributingArtists"):
            errors.append(f"Windows Mitwirkende Interpreten should be empty, got {shell.get('ContributingArtists')!r}")
        if shell.get("AlbumArtist"):
            errors.append(f"Windows Albuminterpret should be empty, got {shell.get('AlbumArtist')!r}")
        media_created = shell.get("MediaCreated") or ""
        if "2000" not in media_created:
            errors.append(f"Windows Medium erstellt does not show 2000: {media_created!r}")

    report = {
        "ok": not errors,
        "sample": str(sample),
        "expected_title": expected_title,
        "exiftool": meta,
        "grouped": grouped,
        "ffprobe_tags": tags,
        "windows_shell": shell,
        "errors": errors,
        "warnings": warnings,
    }
    report_path = preflight_dir / "metadata_preflight_report.json"
    write_summary(report_path, report)
    if errors:
        raise BatchError(f"metadata preflight failed; see {report_path}: {'; '.join(errors)}")
    return report


def validate_output(
    output: Path,
    source: Path,
    source_probe: dict,
    *,
    args: argparse.Namespace,
    exiftool: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not output.exists():
        return False, ["missing output"]
    if output.stat().st_size < 100_000:
        reasons.append("output too small")

    try:
        probe = ffprobe(output)
    except Exception as exc:  # noqa: BLE001
        return False, [f"ffprobe failed: {exc}"]

    video = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    if not video:
        reasons.append("missing video stream")
    else:
        if video.get("codec_name") != "h264":
            reasons.append(f"video codec is {video.get('codec_name')}, expected h264")
        if int(video.get("width") or 0) != 720 or int(video.get("height") or 0) != 576:
            reasons.append(f"video dimensions are {video.get('width')}x{video.get('height')}, expected 720x576")
        if video.get("pix_fmt") != "yuv420p":
            reasons.append(f"pixel format is {video.get('pix_fmt')}, expected yuv420p")
        fps = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
        if fps is None or abs(fps - 50.0) > 0.02:
            reasons.append(f"frame rate is {fps}, expected 50 fps")

    if source_has_audio(source_probe):
        audio = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
        if not audio:
            reasons.append("source has audio but output has no audio stream")
        elif audio.get("codec_name") != "aac":
            reasons.append(f"audio codec is {audio.get('codec_name')}, expected aac")

    src_dur = duration_seconds(source_probe)
    out_dur = duration_seconds(probe)
    if not out_dur or out_dur <= 0:
        reasons.append("output duration missing or zero")
    elif src_dur and abs(src_dur - out_dur) > max(0.35, src_dur * 0.03):
        reasons.append(f"duration mismatch: source {src_dur:.3f}s, output {out_dur:.3f}s")

    try:
        meta = exiftool_json(output, exiftool)
    except Exception as exc:  # noqa: BLE001
        return False, [*reasons, f"exiftool validation failed: {exc}"]
    try:
        reasons.extend(reject_forbidden_default_metadata(output, exiftool))
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"forbidden metadata validation failed: {exc}")

    for key in required_metadata_fields(args.contributor):
        if not meta.get(key):
            reasons.append(f"missing metadata field {key}")
    if meta.get("Make") != args.camera_make:
        reasons.append(f"Make is {meta.get('Make')!r}, expected {args.camera_make!r}")
    expected_model = camera_model_tag(args.camera_make, args.camera_model)
    if meta.get("Model") != expected_model:
        reasons.append(f"Model is {meta.get('Model')!r}, expected {expected_model!r}")
    expected_title = archive_title_from_metadata(meta, source.parent.name)
    if meta.get("Title") != expected_title:
        reasons.append(f"Title is {meta.get('Title')!r}, expected {expected_title!r}")
    if args.contributor:
        if not metadata_value_matches(meta.get("Artist"), args.contributor):
            reasons.append(f"Artist is {meta.get('Artist')!r}, expected {args.contributor!r}")
        if not metadata_value_matches(meta.get("Contributor"), args.contributor):
            reasons.append(f"Contributor is {meta.get('Contributor')!r}, expected {args.contributor!r}")
    else:
        if meta.get("Artist"):
            reasons.append(f"Artist should be empty, got {meta.get('Artist')!r}")
        if meta.get("Contributor"):
            reasons.append(f"Contributor should be empty, got {meta.get('Contributor')!r}")
    encoder = meta.get("Encoder") or ""
    if "libx264" not in encoder or f"preset {args.preset}" not in encoder or f"CRF {args.crf:g}" not in encoder:
        reasons.append(f"Encoder does not match requested x264 settings: {encoder!r}")
    if args.tune and f"tune {args.tune}" not in encoder:
        reasons.append(f"Encoder is missing tune {args.tune!r}: {encoder!r}")
    creator_tool = meta.get("CreatorTool") or ""
    if "FFmpeg" not in creator_tool or "QTGMC" not in creator_tool:
        reasons.append(f"CreatorTool does not identify FFmpeg/QTGMC: {creator_tool!r}")
    description = meta.get("Description") or ""
    if f"QTGMC {args.mode}" not in description:
        reasons.append(f"Description does not mention QTGMC {args.mode}: {description!r}")
    source_tag = meta.get("Source") or ""
    if source.name not in source_tag:
        reasons.append(f"Source metadata does not mention source filename: {source_tag!r}")
    derived = meta.get("DerivedFromFilePath") or ""
    if source.name != Path(derived).name:
        reasons.append(f"DerivedFromFilePath is {derived!r}, expected {source.name!r}")
    history = ",".join(str(meta.get(key, "")) for key in ("HistoryAction", "HistoryParameters", "HistorySoftwareAgent"))
    for token in ("produced", "deinterlaced", "encoded", args.mode, "libx264"):
        if token not in history:
            reasons.append(f"XMP history missing {token!r}")
            break

    return not reasons, reasons


def parse_exif_datetime(raw: str) -> dt.datetime:
    # ExifTool gives e.g. 2002:02:02 21:17:13+01:00.
    if len(raw) >= 25 and raw[-3] == ":":
        raw = raw[:-3] + raw[-2:]
    return dt.datetime.strptime(raw, "%Y:%m:%d %H:%M:%S%z")


def stamp_file_dates_from_metadata(output: Path, exiftool: str) -> None:
    proc = run([exiftool, "-api", "QuickTimeUTC=1", "-s3", "-Keys:CreationDate", output], timeout=60)
    raw = proc.stdout.strip()
    if not raw:
        proc = run([exiftool, "-api", "QuickTimeUTC=1", "-s3", "-QuickTime:CreateDate", output], timeout=60)
        raw = proc.stdout.strip()
    if not raw:
        raise BatchError(f"Could not read recording date from output metadata: {output}")
    timestamp = parse_exif_datetime(raw).timestamp()
    os.utime(output, (timestamp, timestamp))
    if sys.platform == "win32":
        filetime = int(timestamp * 10_000_000) + 116444736000000000
        filetime_obj = ctypes.wintypes.FILETIME(filetime & 0xFFFFFFFF, filetime >> 32)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            str(output),
            0x0100,  # FILE_WRITE_ATTRIBUTES
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,  # OPEN_EXISTING
            0x80,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), f"Could not open output to set file dates: {output}")
        try:
            ok = kernel32.SetFileTime(handle, ctypes.byref(filetime_obj), None, ctypes.byref(filetime_obj))
            if not ok:
                raise OSError(ctypes.get_last_error(), f"Could not set file dates: {output}")
        finally:
            kernel32.CloseHandle(handle)


def discover_sources(source_root: Path) -> list[Path]:
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in SOURCE_EXTENSIONS
    )


def output_for(source: Path, source_root: Path, output_root: Path) -> Path:
    rel = source.relative_to(source_root)
    return (output_root / rel).with_suffix(".mp4")


def job_id_for(source: Path, source_root: Path) -> str:
    rel = source.relative_to(source_root).as_posix().lower()
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def resolve_stitch_source(edge: dict, side: str, source_root: Path) -> Path:
    path_key = f"{side}Path"
    name_key = side
    raw = edge.get(path_key)
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (ROOT / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if resolved_under(candidate, source_root):
            return candidate

    cassette = edge.get("cassette")
    name = edge.get(name_key)
    if cassette and name:
        candidate = (source_root / cassette / name).resolve()
        if candidate.exists():
            return candidate
    raise BatchError(f"Stitch edge does not resolve under source root: {edge}")


def chain_id_for(sources: list[Path], source_root: Path) -> str:
    rels = [source.relative_to(source_root).as_posix().lower() for source in sources]
    return hashlib.sha1("\n".join(rels).encode("utf-8")).hexdigest()[:12]


def load_stitch_chains(decisions_path: Path, source_root: Path, output_root: Path, stitch_output_root: Path) -> list[dict]:
    if not decisions_path.exists():
        raise BatchError(f"stitch decisions file not found: {decisions_path}")
    data = json.loads(decisions_path.read_text(encoding="utf-8"))
    edges = data.get("stitch") or []
    if not isinstance(edges, list):
        raise BatchError(f"stitch decisions file has no stitch list: {decisions_path}")

    outgoing: dict[Path, list[tuple[Path, dict]]] = {}
    incoming: dict[Path, list[tuple[Path, dict]]] = {}
    nodes: set[Path] = set()
    for edge in edges:
        a = resolve_stitch_source(edge, "a", source_root)
        b = resolve_stitch_source(edge, "b", source_root)
        outgoing.setdefault(a, []).append((b, edge))
        incoming.setdefault(b, []).append((a, edge))
        nodes.add(a)
        nodes.add(b)

    problems = []
    for source, outs in outgoing.items():
        if len(outs) > 1:
            problems.append(f"{source} has {len(outs)} outgoing stitch edges")
    for source, ins in incoming.items():
        if len(ins) > 1:
            problems.append(f"{source} has {len(ins)} incoming stitch edges")
    if problems:
        raise BatchError("Stitch graph is not a set of simple chains: " + "; ".join(problems[:10]))

    starts = sorted((node for node in nodes if node not in incoming), key=lambda item: str(item).lower())
    visited: set[Path] = set()
    chains: list[dict] = []
    for start in starts:
        current = start
        sources = [current]
        chain_edges: list[dict] = []
        while current in outgoing:
            if current in visited:
                raise BatchError(f"Cycle detected in stitch graph near {current}")
            visited.add(current)
            next_source, edge = outgoing[current][0]
            chain_edges.append(edge)
            sources.append(next_source)
            current = next_source
        visited.add(current)
        if len(sources) < 2:
            continue

        chain_id = chain_id_for(sources, source_root)
        first = sources[0]
        last = sources[-1]
        cassette = first.relative_to(source_root).parts[0]
        out_name = f"{first.stem}__stitched-{len(sources)}files__{chain_id}.mp4"
        output = stitch_output_root / cassette / out_name
        chains.append(
            {
                "id": chain_id,
                "cassette": cassette,
                "sources": sources,
                "source_outputs": [output_for(source, source_root, output_root) for source in sources],
                "output": output,
                "first_source": first,
                "last_source": last,
                "edges": chain_edges,
            }
        )

    unvisited = sorted(nodes - visited, key=lambda item: str(item).lower())
    if unvisited:
        raise BatchError(f"Cycle detected in stitch graph; no chain start for {unvisited[0]}")
    return chains


def mp4_signature(probe: dict) -> dict:
    video = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"), None)
    return {
        "video_codec": video.get("codec_name") if video else None,
        "width": int(video.get("width") or 0) if video else None,
        "height": int(video.get("height") or 0) if video else None,
        "pix_fmt": video.get("pix_fmt") if video else None,
        "fps": round(parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")) or 0.0, 3) if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_sample_rate": int(audio.get("sample_rate") or 0) if audio and audio.get("sample_rate") else None,
        "audio_channels": int(audio.get("channels") or 0) if audio else None,
    }


def validate_mp4_concat_inputs(chain: dict, exiftool: str, args: argparse.Namespace) -> tuple[bool, list[str], list[dict]]:
    reasons: list[str] = []
    infos: list[dict] = []
    signature = None
    for source, path in zip(chain["sources"], chain["source_outputs"], strict=True):
        if not path.exists():
            reasons.append(f"missing input MP4: {path}")
            continue
        try:
            probe = ffprobe(path)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"ffprobe failed for input {path}: {exc}")
            continue
        sig = mp4_signature(probe)
        info = {"path": path, "probe": probe, "signature": sig, "duration": duration_seconds(probe)}
        infos.append(info)
        if sig["video_codec"] != "h264":
            reasons.append(f"{path} video codec is {sig['video_codec']}, expected h264")
        if sig["width"] != 720 or sig["height"] != 576:
            reasons.append(f"{path} dimensions are {sig['width']}x{sig['height']}, expected 720x576")
        if sig["pix_fmt"] != "yuv420p":
            reasons.append(f"{path} pixel format is {sig['pix_fmt']}, expected yuv420p")
        if sig["fps"] is None or abs(sig["fps"] - 50.0) > 0.02:
            reasons.append(f"{path} frame rate is {sig['fps']}, expected 50")
        if signature is None:
            signature = sig
        elif sig != signature:
            reasons.append(f"{path} media signature differs from first stitch input")
        try:
            source_probe = ffprobe(source)
            metadata_valid, metadata_reasons = validate_output(
                path,
                source,
                source_probe,
                args=args,
                exiftool=exiftool,
            )
        except Exception as exc:  # noqa: BLE001
            metadata_valid = False
            metadata_reasons = [str(exc)]
        if not metadata_valid:
            reasons.append(f"{path} failed source/access-copy validation: {'; '.join(metadata_reasons)}")
    return not reasons, reasons, infos


def validate_stitched_mp4(output: Path, inputs: list[Path], input_infos: list[dict]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not output.exists():
        return False, ["missing stitched output"]
    try:
        probe = ffprobe(output)
    except Exception as exc:  # noqa: BLE001
        return False, [f"ffprobe failed for stitched output: {exc}"]
    sig = mp4_signature(probe)
    if sig.get("video_codec") != "h264":
        reasons.append(f"stitched video codec is {sig.get('video_codec')}, expected h264")
    if sig.get("width") != 720 or sig.get("height") != 576:
        reasons.append(f"stitched dimensions are {sig.get('width')}x{sig.get('height')}, expected 720x576")
    if sig.get("pix_fmt") != "yuv420p":
        reasons.append(f"stitched pixel format is {sig.get('pix_fmt')}, expected yuv420p")
    if sig.get("fps") is None or abs(sig["fps"] - 50.0) > 0.02:
        reasons.append(f"stitched frame rate is {sig.get('fps')}, expected 50")
    if input_infos:
        first_sig = input_infos[0]["signature"]
        for key in ("audio_codec", "audio_sample_rate", "audio_channels"):
            if sig.get(key) != first_sig.get(key):
                reasons.append(f"stitched {key} is {sig.get(key)}, expected {first_sig.get(key)}")
    expected_duration = sum(float(info.get("duration") or 0.0) for info in input_infos)
    actual_duration = duration_seconds(probe)
    if not actual_duration:
        reasons.append("stitched duration missing or zero")
    elif expected_duration and abs(expected_duration - actual_duration) > max(1.0, expected_duration * 0.01):
        reasons.append(f"stitched duration mismatch: inputs {expected_duration:.3f}s, output {actual_duration:.3f}s")
    if output.stat().st_size < max(100_000, sum(path.stat().st_size for path in inputs if path.exists()) * 0.15):
        reasons.append("stitched output is unexpectedly small")
    return not reasons, reasons


def write_concat_list(path: Path, inputs: list[Path]) -> None:
    def quote_for_concat(item: Path) -> str:
        text = item.resolve().as_posix()
        return "file '" + text.replace("'", "'\\''") + "'"

    path.write_text("\n".join(quote_for_concat(item) for item in inputs) + "\n", encoding="utf-8")


def stamp_stitched_metadata(
    output: Path,
    inputs: list[Path],
    chain: dict,
    exiftool: str,
    args: argparse.Namespace,
) -> None:
    first = inputs[0]
    first_meta = exiftool_json(first, exiftool)
    camera_make = first_meta.get("Make") or args.camera_make
    camera_model = first_meta.get("Model") or camera_model_tag(args.camera_make, args.camera_model)
    title = archive_title_from_metadata(first_meta, chain["cassette"])
    source_names = [path.name for path in inputs]
    edge_ids = [edge.get("id") for edge in chain["edges"] if edge.get("id")]
    source_text = f"Stitched MP4 access copy from {len(inputs)} converted MiniDV access copies: " + "; ".join(source_names)
    history_parameters = json.dumps(
        {
            "method": f"ffmpeg concat demuxer; video stream copy; audio {args.stitch_audio_mode}",
            "stitch_decisions": str(args.stitch_decisions),
            "chain_id": chain["id"],
            "source_mp4s": [str(path) for path in inputs],
            "stitch_edge_ids": edge_ids,
        },
        ensure_ascii=False,
    )
    description = (
        f"Stitched MP4 access copy from {len(inputs)} deinterlaced H.264/AAC MP4 segments. "
        "Recording date/time is inherited from the first segment."
    )
    run(
        [
            exiftool,
            "-overwrite_original",
            "-api",
            "QuickTimeUTC=1",
            "-TagsFromFile",
            first,
            "-Keys:CreationDate",
            "-QuickTime:CreateDate",
            "-QuickTime:ModifyDate",
            "-TrackCreateDate",
            "-TrackModifyDate",
            "-MediaCreateDate",
            "-MediaModifyDate",
            "-XMP-xmp:CreateDate",
            "-XMP-xmp:ModifyDate",
            "-XMP-exif:DateTimeOriginal",
            "-XMP-photoshop:DateCreated",
            "-Keys:Make",
            "-Keys:Model",
            "-Keys:Encoder",
            "-ItemList:Title=",
            "-UserData:Title=",
            "-ItemList:Artist=",
            "-XMP-dc:Contributor=",
            f"-XMP-tiff:Make={camera_make}",
            f"-XMP-tiff:Model={camera_model}",
            f"-UserData:Make={camera_make}",
            f"-UserData:Model={camera_model}",
            f"-Keys:Title={title}",
            f"-Keys:Description={description}",
            f"-XMP-dc:Title={title}",
            f"-XMP-dc:Description={description}",
            f"-XMP-dc:Source={source_text}",
            "-XMP:CreatorTool=FFmpeg concat demuxer; scripts/batch_convert_raw_to_mp4.py",
            f"-XMP-xmpMM:DerivedFromFilePath={'; '.join(source_names)}",
            "-XMP-xmpMM:HistoryAction=stitched",
            f"-XMP-xmpMM:HistoryParameters={history_parameters}",
            "-XMP-xmpMM:HistorySoftwareAgent=FFmpeg concat demuxer; scripts/batch_convert_raw_to_mp4.py",
            f"-Comment={history_parameters}",
            output,
        ],
        timeout=120,
    )
    if args.contributor:
        run(
            [
                exiftool,
                "-overwrite_original",
                "-api",
                "QuickTimeUTC=1",
                f"-ItemList:Artist={args.contributor}",
                f"-XMP-dc:Contributor={args.contributor}",
                output,
            ],
            timeout=120,
        )


def validate_stitched_metadata(
    output: Path,
    first_input: Path,
    chain: dict,
    exiftool: str,
    args: argparse.Namespace,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    try:
        meta = exiftool_json(output, exiftool)
        first_meta = exiftool_json(first_input, exiftool)
    except Exception as exc:  # noqa: BLE001
        return False, [f"exiftool stitched metadata validation failed: {exc}"]
    try:
        reasons.extend(reject_forbidden_default_metadata(output, exiftool))
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"stitched forbidden metadata validation failed: {exc}")
    required = [
        "CreationDate",
        "CreateDate",
        "DateTimeOriginal",
        "Make",
        "Model",
        "Title",
        "Encoder",
        "CreatorTool",
        "Description",
        "Source",
        "DerivedFromFilePath",
    ]
    if args.contributor:
        required.extend(["Artist", "Contributor"])
    for key in required:
        value = meta.get(key)
        if not value:
            reasons.append(f"missing stitched metadata field {key}")
        elif isinstance(value, str) and value.startswith("0000:00:00"):
            reasons.append(f"stitched metadata field {key} is zero date")
    for key in ("CreationDate", "CreateDate", "DateTimeOriginal"):
        if first_meta.get(key) and meta.get(key) != first_meta.get(key):
            reasons.append(f"stitched {key} {meta.get(key)!r} does not match first input {first_meta.get(key)!r}")
    expected_title = archive_title_from_metadata(first_meta, chain["cassette"])
    if meta.get("Title") != expected_title:
        reasons.append(f"stitched Title is {meta.get('Title')!r}, expected {expected_title!r}")
    if args.contributor:
        if not metadata_value_matches(meta.get("Artist"), args.contributor):
            reasons.append(f"stitched Artist is {meta.get('Artist')!r}, expected {args.contributor!r}")
        if not metadata_value_matches(meta.get("Contributor"), args.contributor):
            reasons.append(f"stitched Contributor is {meta.get('Contributor')!r}, expected {args.contributor!r}")
    else:
        if meta.get("Artist"):
            reasons.append(f"stitched Artist should be empty, got {meta.get('Artist')!r}")
        if meta.get("Contributor"):
            reasons.append(f"stitched Contributor should be empty, got {meta.get('Contributor')!r}")
    creator_tool = meta.get("CreatorTool") or ""
    if "ffmpeg concat" not in creator_tool.lower():
        reasons.append(f"stitched CreatorTool does not identify ffmpeg concat: {creator_tool!r}")
    source = meta.get("Source") or ""
    description = meta.get("Description") or ""
    history = ",".join(str(meta.get(key, "")) for key in ("HistoryAction", "HistoryParameters", "HistorySoftwareAgent"))
    if "Stitched MP4 access copy" not in source:
        reasons.append("stitched Source metadata does not identify stitched access copy")
    if "Stitched MP4 access copy" not in description:
        reasons.append("stitched Description metadata does not identify stitched access copy")
    if "stitched" not in history or "ffmpeg concat" not in history:
        reasons.append("stitched XMP history does not identify ffmpeg concat stitching")
    return not reasons, reasons


def process_stitch_chain(chain: dict, state_root: Path, args: argparse.Namespace) -> dict:
    inputs = chain["source_outputs"]
    missing = [path for path in inputs if not path.exists()]
    if missing:
        return {
            "status": "stitch_blocked_missing_input",
            "chain_id": chain["id"],
            "output": str(chain["output"]),
            "missing_inputs": [str(path) for path in missing],
        }

    exiftool = which_or_path("exiftool", DEFAULT_EXIFTOOL)
    ok, reasons, infos = validate_mp4_concat_inputs(chain, exiftool, args)
    if not ok:
        return {
            "status": "stitch_failed",
            "chain_id": chain["id"],
            "output": str(chain["output"]),
            "error": "; ".join(reasons),
        }

    final = chain["output"]
    stitch_title = archive_title_from_metadata(exiftool_json(inputs[0], exiftool), chain["cassette"])
    if final.exists() and not args.force_restitch:
        valid, final_reasons = validate_stitched_mp4(final, inputs, infos)
        metadata_valid, metadata_reasons = validate_stitched_metadata(final, inputs[0], chain, exiftool, args)
        if valid and metadata_valid:
            return {"status": "stitch_skipped_valid", "chain_id": chain["id"], "output": str(final)}

    work_dir = state_root / "stitch_work" / chain["id"]
    if work_dir.exists():
        safe_rmtree(work_dir, state_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    stage = work_dir / "stage.mp4"
    concat_list = work_dir / "concat.txt"
    write_concat_list(concat_list, inputs)

    comment = json.dumps(
        {
            "derived_output_reason": "mp4_soft_cut_stitch_from_human_reviewed_edges",
            "chain_id": chain["id"],
            "source_mp4_count": len(inputs),
            "source_mp4s": [str(path) for path in inputs],
            "stitch_edge_ids": [edge.get("id") for edge in chain["edges"]],
        },
        ensure_ascii=False,
    )
    cmd: list[str | Path] = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list,
        "-map",
        "0",
        "-c:v",
        "copy",
    ]
    if args.stitch_audio_mode == "copy":
        cmd.extend(["-c:a", "copy"])
    else:
        cmd.extend(["-c:a", "aac", "-b:a", args.audio_bitrate, "-ar", str(args.audio_rate), "-ac", str(args.audio_channels)])
    cmd.extend([
        "-map_metadata",
        "0",
        "-movflags",
        "+faststart",
        "-metadata",
        f"comment={comment}",
        stage,
    ])
    start = time.monotonic()
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=args.stitch_timeout,
    )
    (work_dir / "ffmpeg.stdout.txt").write_text(proc.stdout, encoding="utf-8", errors="replace")
    (work_dir / "ffmpeg.stderr.txt").write_text(proc.stderr, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not stage.exists():
        return {
            "status": "stitch_failed",
            "chain_id": chain["id"],
            "output": str(final),
            "error": f"ffmpeg concat failed ({proc.returncode}); see {work_dir}",
        }

    valid, final_reasons = validate_stitched_mp4(stage, inputs, infos)
    if not valid:
        return {
            "status": "stitch_failed",
            "chain_id": chain["id"],
            "output": str(final),
            "error": f"staged stitched output failed validation: {'; '.join(final_reasons)}; see {work_dir}",
        }

    try:
        stamp_stitched_metadata(stage, inputs, chain, exiftool, args)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "stitch_failed",
            "chain_id": chain["id"],
            "output": str(final),
            "error": f"staged stitched metadata stamping failed: {exc}; see {work_dir}",
        }
    metadata_valid, metadata_reasons = validate_stitched_metadata(stage, inputs[0], chain, exiftool, args)
    if not metadata_valid:
        return {
            "status": "stitch_failed",
            "chain_id": chain["id"],
            "output": str(final),
            "error": f"staged stitched metadata failed validation: {'; '.join(metadata_reasons)}; see {work_dir}",
        }

    publish_info = publish_stage_to_final(stage, final)
    file_date_warning = None
    if args.set_file_dates:
        try:
            stamp_file_dates_from_metadata(final, exiftool)
        except Exception as exc:  # noqa: BLE001
            file_date_warning = str(exc)
    final_valid, final_reasons = validate_stitched_mp4(final, inputs, infos)
    final_metadata_valid, final_metadata_reasons = validate_stitched_metadata(final, inputs[0], chain, exiftool, args)
    if not final_valid or not final_metadata_valid:
        return {
            "status": "stitch_failed",
            "chain_id": chain["id"],
            "output": str(final),
            "error": (
                "published stitched output failed validation: "
                + "; ".join(final_reasons + final_metadata_reasons)
                + f"; see {work_dir}"
            ),
        }

    cleanup_warning = None
    if not args.keep_work:
        try:
            safe_rmtree(work_dir, state_root)
        except BatchError as exc:
            cleanup_warning = str(exc)
    return {
        "status": "stitched",
        "chain_id": chain["id"],
        "output": str(final),
        "input_count": len(inputs),
        "seconds": round(time.monotonic() - start, 3),
        "bytes": final.stat().st_size,
        "publish": publish_info,
        "file_date_warning": file_date_warning,
        "cleanup_warning": cleanup_warning,
    }


def stitch_plan_summary(chains: list[dict]) -> dict:
    by_count: dict[int, int] = {}
    for chain in chains:
        count = len(chain["source_outputs"])
        by_count[count] = by_count.get(count, 0) + 1
    return {
        "chain_count": len(chains),
        "file_count_distribution": {str(key): by_count[key] for key in sorted(by_count)},
        "top_by_file_count": [
            {
                "chain_id": chain["id"],
                "cassette": chain["cassette"],
                "input_count": len(chain["source_outputs"]),
                "output": str(chain["output"]),
                "first_source": str(chain["first_source"]),
                "last_source": str(chain["last_source"]),
            }
            for chain in sorted(chains, key=lambda item: (-len(item["source_outputs"]), str(item["output"]).lower()))[:10]
        ],
    }


def run_structural_preflight(
    args: argparse.Namespace,
    sources: list[Path],
    stitch_chains: list[dict],
    stitch_output_by_source: dict[Path, Path],
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if resolved_under(args.output_root, args.source_root) or resolved_under(args.source_root, args.output_root):
        errors.append(f"output root overlaps source root: {args.output_root}")
    if resolved_under(args.state_root, args.source_root) or resolved_under(args.source_root, args.state_root):
        errors.append(f"state root overlaps source root: {args.state_root}")
    if not path_anchor_exists(args.output_root):
        errors.append(f"output root drive/share does not exist: {args.output_root.anchor}")
    if not path_anchor_exists(args.state_root):
        errors.append(f"state root drive/share does not exist: {args.state_root.anchor}")
    if args.stitch_mode != "off" and not path_anchor_exists(args.stitch_output_root):
        errors.append(f"stitch output root drive/share does not exist: {args.stitch_output_root.anchor}")
    if args.output_root.resolve() == args.state_root.resolve():
        errors.append("output root and state root are the same path")
    if (args.output_root / "_stitched").exists():
        errors.append(f"legacy public _stitched folder exists: {args.output_root / '_stitched'}")

    full_source_run = (
        args.source_root == DEFAULT_SOURCE_ROOT.resolve()
        and args.limit is None
        and not args.start_after
        and args.stitch_mode not in {"only", "plan"}
    )
    if args.expected_source_count and full_source_run and len(sources) != args.expected_source_count:
        errors.append(f"discovered {len(sources)} sources, expected {args.expected_source_count}")

    full_stitch_plan = (
        args.stitch_mode != "off"
        and args.stitch_limit is None
        and args.stitch_decisions == DEFAULT_STITCH_DECISIONS.resolve()
    )
    if args.expected_stitch_chain_count and full_stitch_plan and len(stitch_chains) != args.expected_stitch_chain_count:
        errors.append(f"planned {len(stitch_chains)} stitch chains, expected {args.expected_stitch_chain_count}")

    seen_public_outputs: set[Path] = set()
    duplicate_public_outputs: list[str] = []
    for chain in stitch_chains:
        output = chain["output"].resolve()
        if output in seen_public_outputs:
            duplicate_public_outputs.append(str(output))
        seen_public_outputs.add(output)
        if not resolved_under(output, args.stitch_output_root):
            errors.append(f"stitch output is outside stitch output root: {output}")
        if "_stitched" in output.parts:
            errors.append(f"stitch output still targets legacy _stitched folder: {output}")
        for source, source_output in zip(chain["sources"], chain["source_outputs"], strict=True):
            source_output = source_output.resolve()
            if not resolved_under(source.resolve(), args.source_root):
                errors.append(f"stitch source is outside source root: {source}")
            if not resolved_under(source_output, args.stitch_segment_root):
                errors.append(f"stitch component output is not internal: {source_output}")
            if resolved_under(source_output, args.output_root):
                errors.append(f"stitch component would be public: {source_output}")
    if duplicate_public_outputs:
        errors.append(f"duplicate stitch output paths: {duplicate_public_outputs[:5]}")

    stitch_source_set = set(stitch_output_by_source)
    standalone_count = sum(1 for source in sources if source.resolve() not in stitch_source_set)
    planned_public_output_count = standalone_count + (len(stitch_chains) if args.stitch_mode != "off" else 0)
    if args.stitch_mode == "off":
        planned_public_output_count = len(sources)
    if args.limit is not None or args.start_after:
        warnings.append("public output count is partial because --limit or --start-after is active")

    report = {
        "ok": not errors,
        "source_count": len(sources),
        "cassette_count": len({source.relative_to(args.source_root).parts[0] for source in sources}) if sources else None,
        "stitch_chain_count": len(stitch_chains),
        "stitch_source_count": len(stitch_source_set),
        "standalone_public_output_count": standalone_count,
        "planned_public_output_count": planned_public_output_count,
        "output_root": str(args.output_root),
        "state_root": str(args.state_root),
        "publish_model": "render_and_stitch_in_state_root_then_publish_finished_mp4s",
        "stitch_segment_root": str(args.stitch_segment_root),
        "errors": errors,
        "warnings": warnings,
    }
    if errors:
        raise BatchError("structural preflight failed: " + "; ".join(errors[:10]))
    return report


def write_batch_summary(
    summary_path: Path,
    args: argparse.Namespace,
    counts: dict,
    stitch_counts: dict,
    failures: list[dict],
    stitch_failures: list[dict],
) -> None:
    write_summary(summary_path, {
        "updated_at": now_iso(),
        "source_root": str(args.source_root),
        "output_root": str(args.output_root),
        "state_root": str(args.state_root),
        "stitch_decisions": str(args.stitch_decisions) if args.stitch_mode != "off" else None,
        "stitch_output_root": str(args.stitch_output_root) if args.stitch_mode != "off" else None,
        "stitch_segment_root": str(args.stitch_segment_root) if args.stitch_mode != "off" else None,
        "publish_model": "render_and_stitch_in_state_root_then_publish_finished_mp4s",
        "clean_start": getattr(args, "clean_report", None),
        "preflight": getattr(args, "preflight_report", None),
        "settings": {
            "mode": args.mode,
            "preset": args.preset,
            "crf": args.crf,
            "tune": args.tune,
            "x264_level": args.x264_level,
            "audio_bitrate": args.audio_bitrate,
            "timezone": args.timezone,
            "author": args.author,
            "contributor": args.contributor,
            "camera_make": args.camera_make,
            "camera_model": args.camera_model,
            "stitch_audio_mode": getattr(args, "stitch_audio_mode", None),
        },
        "counts": counts,
        "stitch_counts": stitch_counts,
        "failures": failures[-50:],
        "stitch_failures": stitch_failures[-50:],
    })


def converter_command(source: Path, stage: Path, args: argparse.Namespace) -> list[str | Path]:
    cassette = source.relative_to(args.source_root).parts[0]
    cmd: list[str | Path] = [
        sys.executable,
        CONVERTER,
        source,
        stage,
        "--force",
        "--mode",
        args.mode,
        "--preset",
        args.preset,
        "--crf",
        f"{args.crf:g}",
        "--tune",
        args.tune,
        "--x264-level",
        args.x264_level,
        "--audio-bitrate",
        args.audio_bitrate,
        "--audio-rate",
        str(args.audio_rate),
        "--audio-channels",
        str(args.audio_channels),
        "--timezone",
        args.timezone,
        "--author",
        args.author,
        "--contributor",
        args.contributor,
        "--camera-make",
        args.camera_make,
        "--camera-model",
        args.camera_model,
        "--cassette",
        cassette,
        "--render-timeout",
        str(args.render_timeout),
        "--mux-timeout",
        str(args.mux_timeout),
    ]
    if not args.set_file_dates:
        cmd.append("--no-file-dates")
    return cmd


def write_summary(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False)
        last_exc: Exception | None = None
        for attempt in range(12):
            try:
                tmp.replace(path)
                return
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.15 * (attempt + 1))
        raise BatchError(f"Could not replace summary after retries: {path}: {last_exc}")
    finally:
        tmp.unlink(missing_ok=True)


def is_cross_device_move_error(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def publish_stage_to_final(stage: Path, final: Path) -> dict:
    """Promote a validated stage file to the final public output path.

    Same-volume output uses os.replace(). Cross-volume output copies to a
    temporary file in the destination directory, validates the byte count, then
    atomically replaces the final filename on that destination volume.
    """
    if not stage.exists():
        raise BatchError(f"Cannot publish missing staged output: {stage}")
    final.parent.mkdir(parents=True, exist_ok=True)
    stage_size = stage.stat().st_size
    try:
        os.replace(stage, final)
        return {"method": "rename", "cross_volume": False}
    except OSError as exc:
        if not is_cross_device_move_error(exc):
            raise

    publish_tmp = final.with_name(f".{final.name}.{os.getpid()}.{time.time_ns()}.publishing.tmp")
    try:
        shutil.copy2(stage, publish_tmp)
        copied_size = publish_tmp.stat().st_size
        if copied_size != stage_size:
            raise BatchError(
                f"Publish copy size mismatch for {final}: staged {stage_size} bytes, copied {copied_size} bytes"
            )
        os.replace(publish_tmp, final)
        stage.unlink()
        return {"method": "copy_then_rename", "cross_volume": True}
    finally:
        publish_tmp.unlink(missing_ok=True)


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return f'"{pid}"' in proc.stdout or f",{pid}," in proc.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class RunnerLock:
    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            stale = True
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                stale = not pid_is_running(int(data.get("pid", -1)))
            except Exception:
                stale = True
            if stale or self.force:
                self.path.unlink(missing_ok=True)
            else:
                raise BatchError(f"Another batch runner appears to be active. Lock file: {self.path}")
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, json.dumps({"pid": os.getpid(), "started_at": now_iso()}).encode("utf-8"))
        except FileExistsError as exc:
            raise BatchError(f"Could not acquire lock: {self.path}") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


def process_source(
    source: Path,
    source_root: Path,
    output_root: Path,
    state_root: Path,
    args: argparse.Namespace,
    *,
    target_output: Path | None = None,
) -> dict:
    exiftool = which_or_path("exiftool", DEFAULT_EXIFTOOL)
    final = target_output or output_for(source, source_root, output_root)
    job_id = job_id_for(source, source_root)
    work_dir = state_root / "work" / job_id
    stage = work_dir / "stage.mp4"
    source_probe = ffprobe(source)
    previous_invalid_reasons: list[str] | None = None

    if final.exists() and not args.force_rebuild:
        valid, reasons = validate_output(
            final,
            source,
            source_probe,
            args=args,
            exiftool=exiftool,
        )
        if valid:
            return {"status": "skipped_valid", "source": str(source), "output": str(final)}
        previous_invalid_reasons = reasons

    if work_dir.exists():
        try:
            safe_rmtree(work_dir, state_root)
        except BatchError:
            work_dir = state_root / "work" / f"{job_id}_{int(time.time())}"
            stage = work_dir / "stage.mp4"
    work_dir.mkdir(parents=True, exist_ok=True)

    cmd = converter_command(source, stage, args)
    start = time.monotonic()
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=args.per_file_timeout,
    )
    (work_dir / "converter.stdout.txt").write_text(proc.stdout, encoding="utf-8", errors="replace")
    (work_dir / "converter.stderr.txt").write_text(proc.stderr, encoding="utf-8", errors="replace")
    elapsed = time.monotonic() - start
    if proc.returncode != 0 or not stage.exists():
        raise BatchError(f"converter failed for {source} ({proc.returncode}); see {work_dir}")

    valid, reasons = validate_output(
        stage,
        source,
        source_probe,
        args=args,
        exiftool=exiftool,
    )
    if not valid:
        raise BatchError(f"staged output failed validation for {source}: {'; '.join(reasons)}; see {work_dir}")

    publish_info = publish_stage_to_final(stage, final)
    if args.set_file_dates:
        stamp_file_dates_from_metadata(final, exiftool)

    valid, reasons = validate_output(
        final,
        source,
        source_probe,
        args=args,
        exiftool=exiftool,
    )
    if not valid:
        raise BatchError(f"final output failed validation after promote for {source}: {'; '.join(reasons)}")

    cleanup_warning = None
    if not args.keep_work:
        try:
            safe_rmtree(work_dir, state_root)
        except BatchError as exc:
            cleanup_warning = str(exc)
    return {
        "status": "rebuilt_invalid" if previous_invalid_reasons else "converted",
        "source": str(source),
        "output": str(final),
        "seconds": round(elapsed, 3),
        "bytes": final.stat().st_size,
        "publish": publish_info,
        "previous_invalid_reasons": previous_invalid_reasons,
        "cleanup_warning": cleanup_warning,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument(
        "--output-root",
        "--final-output-root",
        dest="output_root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            "Final public MP4 output root. Heavy conversion and stitch work stays in "
            "--state-root; only validated finished files are published here."
        ),
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help="Temp/state/work directory. Default is root TEMP/mp4_batch_state.",
    )
    parser.add_argument("--mode", default="slower_default", help="Deinterlacer mode id.")
    parser.add_argument("--preset", default="veryslow", help="x264 preset for final archive access copies.")
    parser.add_argument("--crf", type=float, default=17.0)
    parser.add_argument("--tune", default="grain")
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
    parser.add_argument("--no-file-dates", dest="set_file_dates", action="store_false")
    parser.set_defaults(set_file_dates=True)
    parser.add_argument("--dry-run", action="store_true", help="List planned work without converting.")
    parser.add_argument("--preflight-only", action="store_true", help="Run structural and metadata preflight, then exit without converting.")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip the default preflight before a real run.")
    parser.add_argument("--clean-start", action="store_true", help="Remove generated output/state roots before running. Never touches source_root.")
    parser.add_argument(
        "--expected-source-count",
        type=int,
        default=EXPECTED_DEFAULT_SOURCE_COUNT,
        help="Optional safety check. 0 disables the source-count check.",
    )
    parser.add_argument(
        "--expected-stitch-chain-count",
        type=int,
        default=EXPECTED_DEFAULT_STITCH_CHAIN_COUNT,
        help="Optional safety check. 0 disables the stitch-chain-count check.",
    )
    parser.add_argument("--verify-only", action="store_true", help="Validate existing outputs without converting.")
    parser.add_argument("--force-rebuild", action="store_true", help="Rebuild even valid existing outputs.")
    parser.add_argument("--limit", type=int, help="Process only the first N discovered sources.")
    parser.add_argument("--start-after", help="Resume listing after a source path/name containing this text.")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--keep-work", action="store_true", help="Keep per-file staging logs after success.")
    parser.add_argument("--force-lock", action="store_true", help="Remove an existing runner lock before starting.")
    parser.add_argument("--render-timeout", type=int, default=7200)
    parser.add_argument("--mux-timeout", type=int, default=1200)
    parser.add_argument("--per-file-timeout", type=int, default=10800)
    parser.add_argument(
        "--stitch-mode",
        choices=("off", "plan", "after", "only"),
        default="off",
        help=(
            "MP4 stitching stage. off keeps the old one-source-one-MP4 behavior; "
            "plan prints stitch chains only; after converts/verifies sources then stitches available MP4 chains; "
            "only stitches existing MP4 outputs without converting raw sources."
        ),
    )
    parser.add_argument("--stitch-decisions", type=Path, default=DEFAULT_STITCH_DECISIONS)
    parser.add_argument(
        "--stitch-output-root",
        type=Path,
        help="Where final stitched MP4s are published. Default: <output-root>, directly inside each cassette folder.",
    )
    parser.add_argument("--stitch-limit", type=int, help="Process only the first N stitch chains.")
    parser.add_argument("--stitch-timeout", type=int, default=3600)
    parser.add_argument("--force-restitch", action="store_true", help="Rebuild stitched MP4s even if valid outputs exist.")
    parser.add_argument(
        "--stitch-audio-mode",
        choices=("reencode", "copy"),
        default="reencode",
        help=(
            "How to handle AAC audio when stitching MP4s. Default reencodes only audio "
            "to create a clean continuous timestamp timeline while stream-copying video."
        ),
    )
    parser.add_argument(
        "--require-complete-stitches",
        action="store_true",
        help="Exit non-zero if any stitch chain is blocked because an input MP4 is missing.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.contributor = (args.contributor or "").strip()
    args.camera_make = (args.camera_make or DEFAULT_CAMERA_MAKE).strip() or DEFAULT_CAMERA_MAKE
    args.camera_model = (args.camera_model or DEFAULT_CAMERA_MODEL).strip() or DEFAULT_CAMERA_MODEL
    args.source_root = args.source_root.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    args.state_root = args.state_root.expanduser()
    if not args.state_root.is_absolute():
        args.state_root = (ROOT / args.state_root).resolve()
    else:
        args.state_root = args.state_root.resolve()
    args.stitch_decisions = args.stitch_decisions.expanduser()
    if not args.stitch_decisions.is_absolute():
        args.stitch_decisions = (ROOT / args.stitch_decisions).resolve()
    else:
        args.stitch_decisions = args.stitch_decisions.resolve()
    if args.stitch_output_root is None:
        args.stitch_output_root = args.output_root
    else:
        args.stitch_output_root = args.stitch_output_root.expanduser()
        if not args.stitch_output_root.is_absolute():
            args.stitch_output_root = (ROOT / args.stitch_output_root).resolve()
        else:
            args.stitch_output_root = args.stitch_output_root.resolve()
    if not args.source_root.exists() or not args.source_root.is_dir():
        raise SystemExit(f"source root does not exist: {args.source_root}")
    if not CONVERTER.exists():
        raise SystemExit(f"converter not found: {CONVERTER}")
    which_or_path("ffmpeg")
    which_or_path("ffprobe")
    exiftool = which_or_path("exiftool", DEFAULT_EXIFTOOL)

    args.stitch_segment_root = args.state_root / "stitch_segments"
    clean_report = None
    if args.clean_start:
        clean_report = clean_generated_outputs(args)
    args.clean_report = clean_report

    state_root = args.state_root
    log_path = state_root / "events.jsonl"
    summary_path = state_root / "summary.json"
    sources = [] if args.stitch_mode in {"only", "plan"} else discover_sources(args.source_root)
    if args.start_after:
        marker = args.start_after.lower()
        while sources and marker not in str(sources[0]).lower():
            sources.pop(0)
        if sources:
            sources.pop(0)
    if args.limit is not None:
        sources = sources[: max(0, args.limit)]

    stitch_chains: list[dict] = []
    if args.stitch_mode != "off":
        stitch_chains = load_stitch_chains(
            args.stitch_decisions,
            args.source_root,
            args.stitch_segment_root,
            args.stitch_output_root,
        )
        if args.stitch_limit is not None:
            stitch_chains = stitch_chains[: max(0, args.stitch_limit)]
    stitch_output_by_source: dict[Path, Path] = {}
    for chain in stitch_chains:
        for source, source_output in zip(chain["sources"], chain["source_outputs"], strict=True):
            stitch_output_by_source[source.resolve()] = source_output

    def conversion_output_for(source: Path) -> Path:
        return stitch_output_by_source.get(source.resolve(), output_for(source, args.source_root, args.output_root))

    planned = [
        {
            "source": str(source),
            "output": str(conversion_output_for(source)),
            "public_output": str(output_for(source, args.source_root, args.output_root)),
            "is_internal_stitch_segment": source.resolve() in stitch_output_by_source,
        }
        for source in sources
    ]
    structural_preflight = run_structural_preflight(args, sources, stitch_chains, stitch_output_by_source)

    if args.preflight_only:
        metadata_preflight = run_metadata_preflight(args, exiftool)
        print(json.dumps({
            "ok": True,
            "preflight_only": True,
            "clean_start": clean_report,
            "structural_preflight": structural_preflight,
            "metadata_preflight": {
                "sample": metadata_preflight["sample"],
                "expected_title": metadata_preflight["expected_title"],
                "windows_shell": metadata_preflight["windows_shell"],
            },
        }, indent=2, ensure_ascii=False))
        return 0

    if args.dry_run or args.stitch_mode == "plan":
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "clean_start": clean_report,
            "stitch_mode": args.stitch_mode,
            "source_root": str(args.source_root),
            "output_root": str(args.output_root),
            "state_root": str(args.state_root),
            "stitch_decisions": str(args.stitch_decisions) if args.stitch_mode != "off" else None,
            "stitch_output_root": str(args.stitch_output_root) if args.stitch_mode != "off" else None,
            "stitch_segment_root": str(args.stitch_segment_root) if args.stitch_mode != "off" else None,
            "count": len(planned),
            "first": planned[:10],
            "structural_preflight": structural_preflight,
            "stitch_plan": stitch_plan_summary(stitch_chains) if args.stitch_mode != "off" else None,
        }, indent=2, ensure_ascii=False))
        return 0

    metadata_preflight = None
    if not args.skip_preflight:
        metadata_preflight = run_metadata_preflight(args, exiftool)
    args.preflight_report = {
        "structural": structural_preflight,
        "metadata_report": str(args.state_root / "preflight" / "metadata_preflight_report.json") if metadata_preflight else None,
    }

    counts = {"total": len(sources), "converted": 0, "rebuilt_invalid": 0, "skipped_valid": 0, "verified": 0, "failed": 0}
    stitch_counts = {
        "total": len(stitch_chains),
        "stitched": 0,
        "stitch_skipped_valid": 0,
        "stitch_blocked_missing_input": 0,
        "stitch_failed": 0,
    }
    failures: list[dict] = []
    stitch_failures: list[dict] = []
    stitched_chain_ids: set[str] = set()
    chains_by_input: dict[Path, list[dict]] = {}
    if stitch_chains:
        for chain in stitch_chains:
            for source_output in chain["source_outputs"]:
                chains_by_input.setdefault(source_output.resolve(), []).append(chain)

    def handle_stitch_result(chain: dict, result: dict, *, index: int | None = None, total: int | None = None) -> bool:
        status = result["status"]
        stitch_counts[status] = stitch_counts.get(status, 0) + 1
        event_log(log_path, {"event": status, **result})
        if status == "stitch_failed" or (args.require_complete_stitches and status == "stitch_blocked_missing_input"):
            stitch_failures.append(result)
            prefix = f"[stitch {index}/{total}]" if index is not None and total is not None else "[stitch]"
            print(f"{prefix} FAILED: {chain['output']}\n  {result.get('error') or 'missing input MP4'}", file=sys.stderr)
            return False
        prefix = f"[stitch {index}/{total}]" if index is not None and total is not None else "[stitch]"
        print(f"{prefix} {status}: {chain['output']}")
        return True

    def attempt_incremental_stitches(source_output: Path) -> bool:
        if args.stitch_mode != "after":
            return True
        ok_to_continue = True
        for chain in chains_by_input.get(source_output.resolve(), []):
            if chain["id"] in stitched_chain_ids:
                continue
            if not all(path.exists() for path in chain["source_outputs"]):
                continue
            event_log(
                log_path,
                {
                    "event": "start_stitch_chain_incremental",
                    "chain_id": chain["id"],
                    "output": str(chain["output"]),
                    "input_count": len(chain["source_outputs"]),
                    "trigger_output": str(source_output),
                },
            )
            result = process_stitch_chain(chain, state_root, args)
            stitched_chain_ids.add(chain["id"])
            if not handle_stitch_result(chain, result):
                ok_to_continue = False
                if args.stop_on_error:
                    break
            write_batch_summary(summary_path, args, counts, stitch_counts, failures, stitch_failures)
        return ok_to_continue

    with RunnerLock(state_root / "runner.lock", force=args.force_lock):
        for index, source in enumerate(sources, start=1):
            final = conversion_output_for(source)
            event_log(log_path, {"event": "start_file", "index": index, "total": len(sources), "source": str(source), "output": str(final)})
            try:
                if args.verify_only:
                    exiftool = which_or_path("exiftool", DEFAULT_EXIFTOOL)
                    source_probe = ffprobe(source)
                    valid, reasons = validate_output(
                        final,
                        source,
                        source_probe,
                        args=args,
                        exiftool=exiftool,
                    )
                    if not valid:
                        raise BatchError("; ".join(reasons))
                    result = {"status": "verified", "source": str(source), "output": str(final)}
                else:
                    result = process_source(source, args.source_root, args.output_root, state_root, args, target_output=final)
                status = result["status"]
                counts[status] = counts.get(status, 0) + 1
                event_log(log_path, {"event": status, **result})
                print(f"[{index}/{len(sources)}] {status}: {source}")
                if not attempt_incremental_stitches(final):
                    if args.stop_on_error:
                        break
            except Exception as exc:  # noqa: BLE001
                counts["failed"] += 1
                failure = {"source": str(source), "output": str(final), "error": str(exc)}
                failures.append(failure)
                event_log(log_path, {"event": "failed", **failure})
                print(f"[{index}/{len(sources)}] FAILED: {source}\n  {exc}", file=sys.stderr)
                if args.stop_on_error:
                    break
            write_batch_summary(summary_path, args, counts, stitch_counts, failures, stitch_failures)

        if args.stitch_mode in {"after", "only"}:
            for index, chain in enumerate(stitch_chains, start=1):
                if chain["id"] in stitched_chain_ids:
                    continue
                event_log(
                    log_path,
                    {
                        "event": "start_stitch_chain",
                        "index": index,
                        "total": len(stitch_chains),
                        "chain_id": chain["id"],
                        "output": str(chain["output"]),
                        "input_count": len(chain["source_outputs"]),
                    },
                )
                result = process_stitch_chain(chain, state_root, args)
                stitched_chain_ids.add(chain["id"])
                if not handle_stitch_result(chain, result, index=index, total=len(stitch_chains)):
                    write_batch_summary(summary_path, args, counts, stitch_counts, failures, stitch_failures)
                    if args.stop_on_error:
                        break
                else:
                    write_batch_summary(summary_path, args, counts, stitch_counts, failures, stitch_failures)

    ok = counts["failed"] == 0 and stitch_counts.get("stitch_failed", 0) == 0
    if args.require_complete_stitches and stitch_counts.get("stitch_blocked_missing_input", 0):
        ok = False
    print(json.dumps({
        "ok": ok,
        "counts": counts,
        "stitch_counts": stitch_counts,
        "failures": failures,
        "stitch_failures": stitch_failures,
    }, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
