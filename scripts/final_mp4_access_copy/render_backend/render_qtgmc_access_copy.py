"""
QTGMC access-copy render backend.

Renders ONE deinterlaced frame for a given input file and writes a PNG, renders
a short MP4 clip when --clip-seconds is supplied, or renders from the chosen
frame to the end when --full-video is supplied. The access-copy converter
invokes this as a subprocess so VapourSynth core state is fresh each call.

Pipeline (faithful to the PAL-DV research behind this project):
  1. Load DV with L-SMASH (lossless decode of the original essence).
  2. Force BFF + BT.601 / BT.470BG colour metadata (PAL DV is always BFF).
  3. Promote chroma to YUV444P16 *as interlaced* before deinterlacing
     (fixes PAL DV's interlaced-4:2:0 chroma-upsampling error).
  4. Run the selected deinterlacer. QTGMC modes receive their configured kwargs;
     deepdeinterlace modes use vs_deepdeinterlace's neural field reconstruction;
     regular baseline modes render to the same 50p field cadence. The special
     _original mode skips deinterlacing and extracts the source frame.
  5. Map source frame F -> output frame F*2 (the 50p field-frame), grab either
     one frame or a short run, convert to RGB24, write PNG/MP4.

Usage:
    python render_qtgmc_access_copy.py --input <path> --mode <mode_id> --frame <n> --out <path.png>
    python render_qtgmc_access_copy.py --input <path> --mode <mode_id> --frame <n> --clip-seconds 3 --out <path.mp4>
    python render_qtgmc_access_copy.py --input <path> --mode <mode_id> --frame <n> --full-video --out <path.mp4>
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# VapourSynth matrix/transfer/primaries string aliases for resize.
_MATRIX = {"470bg": 5, "170m": 6, "601": 6, "709": 1}
_TRANSFER = {"601": 6, "709": 1}
_PRIMARIES = {"470bg": 5, "170m": 6, "709": 1}


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def resize_bob(src, core, tff: bool, kernel: str):
    fields = core.std.SeparateFields(src, tff=tff)
    resizer = {
        "point": core.resize.Point,
        "bilinear": core.resize.Bilinear,
        "bicubic": core.resize.Bicubic,
        "lanczos": core.resize.Lanczos,
    }[kernel]
    return resizer(fields, width=src.width, height=src.height)


def eedi3_bob(src, core, tff: bool):
    fields = core.std.SeparateFields(src, tff=tff)
    first = core.std.SelectEvery(fields, 2, 0)
    second = core.std.SelectEvery(fields, 2, 1)
    if tff:
        first = core.eedi3m.EEDI3(first, field=1, dh=1)
        second = core.eedi3m.EEDI3(second, field=0, dh=1)
    else:
        first = core.eedi3m.EEDI3(first, field=0, dh=1)
        second = core.eedi3m.EEDI3(second, field=1, dh=1)
    return core.std.Interleave([first, second])


def enum_arg(enum_cls, value):
    if isinstance(value, str):
        return getattr(enum_cls, value)
    return enum_cls(value)


def build_jet_qtgmc(opts: dict, fps_divisor: int):
    from vsdeinterlace import QTempGaussMC

    qtgmc = QTempGaussMC()
    enum_fields = {
        "prefilter": {"postprocess": QTempGaussMC.SearchPostProcess},
        "source_match": {"mode": QTempGaussMC.SourceMatchMode},
        "lossless": {"mode": QTempGaussMC.LosslessMode},
        "denoise": {
            "mode": QTempGaussMC.NoiseProcessMode,
            "deint": QTempGaussMC.NoiseDeintMode,
        },
        "sharpen": {"mode": QTempGaussMC.SharpenMode},
        "sharpen_limit": {"mode": QTempGaussMC.SharpenLimitMode},
        "back_blend": {"mode": QTempGaussMC.BackBlendMode},
    }
    stages = [
        "prefilter",
        "basic",
        "analyze",
        "source_match",
        "lossless",
        "denoise",
        "sharpen",
        "sharpen_limit",
        "back_blend",
        "final",
        "motion_blur",
    ]
    for stage in stages:
        kwargs = dict(opts.get(stage, {}))
        if stage == "motion_blur":
            kwargs.setdefault("fps_divisor", fps_divisor)
        for key, enum_cls in enum_fields.get(stage, {}).items():
            if key in kwargs:
                kwargs[key] = enum_arg(enum_cls, kwargs[key])
        if kwargs:
            getattr(qtgmc, stage)(**kwargs)
    return qtgmc


def torch_device(requested: str) -> tuple[str, bool]:
    requested = os.environ.get("DEINTERLACE_TORCH_DEVICE", requested)
    if requested != "auto":
        return requested, requested == "cuda"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", True
    except Exception:
        pass
    return "cpu", False


@contextlib.contextmanager
def torch_load_map_location(device: str):
    import torch

    original_load = torch.load

    def load_with_default_map_location(*args, **kwargs):
        kwargs.setdefault("map_location", torch.device(device))
        return original_load(*args, **kwargs)

    torch.load = load_with_default_map_location
    try:
        yield
    finally:
        torch.load = original_load


def require_deepdeinterlace_dependencies(mode_id: str, deinterlacer: list[int]) -> None:
    if 3 not in deinterlacer:
        return
    if importlib.util.find_spec("mmcv") is None:
        sys.exit(
            f"{mode_id} needs mmcv for DfConvEkSA, but mmcv is not installed. "
            "Install a compatible mmcv build before enabling this mode."
        )


def deepdeinterlace(src, mode: dict, tff: bool, matrix_s: str, range_s: str):
    import vs_deepdeinterlace

    opts = dict(mode.get("deepdeinterlace", {}))
    requested_device = opts.get("device", "auto")
    device, has_cuda = torch_device(requested_device)
    deinterlacer = opts.get("deinterlacer", 1)
    if isinstance(deinterlacer, int):
        deinterlacer = [deinterlacer, 1]
    require_deepdeinterlace_dependencies(mode.get("id", "<unnamed>"), deinterlacer)
    tta = opts.get("tta", False)
    if isinstance(tta, bool):
        tta = [tta, False]
    mocomp = opts.get("mocomp", False)
    if isinstance(mocomp, bool):
        mocomp = [mocomp, False]
    fp16 = bool(opts.get("fp16", True)) and has_cuda
    with torch_load_map_location(device):
        return vs_deepdeinterlace.YUV(
            src,
            tff=tff,
            deinterlacer=deinterlacer,
            tta=tta,
            mocomp=mocomp,
            matrix_in_s=matrix_s,
            range_in_s=range_s,
            device=device,
            fp16=fp16,
        )


def deinterlace(src, mode: dict, core, vs, tff: bool, fps_divisor: int, matrix_s: str, range_s: str):
    if "jet_qtgmc" in mode:
        return build_jet_qtgmc(mode.get("jet_qtgmc", {}), fps_divisor).deinterlace(src, tff=tff)

    if "qtgmc" in mode:
        import havsfunc

        qtgmc_kwargs = dict(mode.get("qtgmc", {}))
        qtgmc_kwargs.setdefault("TFF", tff)
        qtgmc_kwargs.setdefault("FPSDivisor", fps_divisor)
        return havsfunc.QTGMC(src, **qtgmc_kwargs)

    if "deepdeinterlace" in mode:
        return deepdeinterlace(src, mode, tff, matrix_s, range_s)

    method = mode.get("method")
    if method == "bwdif":
        return core.bwdif.Bwdif(src, field=3 if tff else 2)
    if method == "nnedi3_bob":
        fields = core.std.SeparateFields(src, tff=tff)
        return core.nnedi3.nnedi3(fields, field=1 if tff else 0, dh=1)
    if method == "eedi3_bob":
        return eedi3_bob(src, core, tff)
    if method == "bob_bicubic":
        return resize_bob(src, core, tff, "bicubic")
    if method == "bob_lanczos":
        return resize_bob(src, core, tff, "lanczos")
    if method == "bob_bilinear":
        return resize_bob(src, core, tff, "bilinear")
    if method == "bob_sinc":
        return core.resize.Bob(src, filter="sinc", tff=tff)

    sys.exit(f"unknown deinterlacer method for {mode.get('id', '<unnamed>')!r}: {method!r}")


def ffmpeg_exe() -> str:
    override = os.environ.get("FFMPEG")
    if override:
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    sys.exit("ffmpeg not found. Add it to PATH or set the FFMPEG environment variable.")


def frame_to_rgb_array(frame, np):
    # VS RGB24 plane order is R, G, B.
    r = np.asarray(frame[0])
    g = np.asarray(frame[1])
    b = np.asarray(frame[2])
    return np.stack([r, g, b], axis=-1)


def clip_fps(clip) -> float:
    if getattr(clip, "fps_num", 0) and getattr(clip, "fps_den", 0):
        return clip.fps_num / clip.fps_den
    return 50.0


def write_png(rgb, out_index: int, out_path: Path, np, image_cls) -> None:
    frame = rgb.get_frame(out_index)
    image_cls.fromarray(frame_to_rgb_array(frame, np), "RGB").save(out_path, "PNG")


def write_mp4(
    raw_clip,
    out_index: int,
    seconds: float | None,
    out_path: Path,
    *,
    input_pix_fmt: str = "gbrp",
    encode_preset: str = "veryfast",
    crf: float = 12,
    pix_fmt: str = "yuv420p",
    profile: str | None = None,
    level: str | None = None,
    tune: str | None = None,
    encoder: str = "libx264",
    nvenc_preset: str = "p7",
    nvenc_cq: float | None = None,
    nvenc_tune: str = "hq",
) -> int:
    fps = clip_fps(raw_clip)
    available = max(0, raw_clip.num_frames - out_index)
    wanted = available if seconds is None else max(1, int(round(fps * seconds)))
    count = max(1, min(wanted, available))
    width, height = raw_clip.width, raw_clip.height
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        input_pix_fmt,
        "-s:v",
        f"{width}x{height}",
        "-r",
        f"{fps:.6f}",
        "-i",
        "-",
        "-an",
        "-c:v",
        encoder,
    ]
    if encoder == "libx264":
        x264_threads = os.environ.get("QTGMC_X264_THREADS")
        if x264_threads:
            cmd.extend(["-threads:v", x264_threads])
        cmd.extend([
            "-preset",
            encode_preset,
            "-crf",
            f"{crf:g}",
        ])
        if tune:
            cmd.extend(["-tune", tune])
    elif encoder == "h264_nvenc":
        cq = crf if nvenc_cq is None else nvenc_cq
        cmd.extend([
            "-preset",
            nvenc_preset,
            "-tune",
            nvenc_tune,
            "-rc",
            "vbr",
            "-cq",
            f"{cq:g}",
            "-b:v",
            "0",
            "-spatial_aq",
            "1",
            "-aq-strength",
            "8",
        ])
    else:
        sys.exit(f"unsupported encoder: {encoder!r}")
    cmd.extend(["-pix_fmt", pix_fmt])
    if profile:
        cmd.extend(["-profile:v", profile])
    if level:
        cmd.extend(["-level", level])
    cmd.extend(["-movflags", "+faststart", str(out_path)])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    assert proc.stderr is not None
    try:
        segment = raw_clip[out_index : out_index + count]
        prefetch = int(os.environ.get("QTGMC_EXTRACT_PREFETCH", "4"))
        segment.output(proc.stdin, y4m=False, prefetch=prefetch)
        proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        ret = proc.wait()
    except Exception:
        proc.kill()
        raise
    if ret != 0:
        sys.exit(f"ffmpeg failed: {stderr[-800:]}")
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", dest="input_path", required=True)
    ap.add_argument("--mode", required=True)
    ap.add_argument("--frame", type=int, required=True)
    ap.add_argument("--clip-seconds", type=float, default=0.0)
    ap.add_argument("--full-video", action="store_true")
    ap.add_argument("--encode-preset", default="veryfast")
    ap.add_argument("--crf", type=float, default=12.0)
    ap.add_argument("--pix-fmt", default="yuv420p")
    ap.add_argument("--x264-profile")
    ap.add_argument("--x264-level")
    ap.add_argument("--x264-tune")
    ap.add_argument("--encoder", default="libx264", choices=["libx264", "h264_nvenc"])
    ap.add_argument("--nvenc-preset", default="p7")
    ap.add_argument("--nvenc-cq", type=float)
    ap.add_argument("--nvenc-tune", default="hq")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.full_video and args.clip_seconds > 0:
        sys.exit("--full-video cannot be combined with --clip-seconds")

    cfg = load_config()
    render = cfg.get("render", {})

    mode = next((d for d in cfg["deinterlacers"] if d["id"] == args.mode), None)
    if args.mode == "_original":
        mode = {"id": "_original", "label": "Original interlaced frame", "method": "original"}
    if not mode:
        sys.exit(f"unknown mode id {args.mode!r}")

    video_path = Path(args.input_path).expanduser().resolve()
    if not video_path.exists():
        sys.exit(f"missing video file: {video_path}")
    if not video_path.is_file():
        sys.exit(f"input is not a file: {video_path}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lsmash_index = out_path.with_name(f"{out_path.name}.{os.getpid()}.lwi")

    # Import VapourSynth only after arg validation.
    import vapoursynth as vs
    core = vs.core
    requested_threads = os.environ.get("QTGMC_VS_THREADS")
    if requested_threads:
        core.num_threads = max(1, int(requested_threads))

    matrix = _MATRIX.get(render.get("matrix", "470bg"), 5)
    transfer = _TRANSFER.get(render.get("transfer", "601"), 6)
    primaries = _PRIMARIES.get(render.get("primaries", "470bg"), 5)
    rng = vs.RANGE_LIMITED if render.get("range", "limited") == "limited" else vs.RANGE_FULL
    tff = bool(render.get("tff", False))
    fps_divisor = int(render.get("fps_divisor", 1))
    source_fps_num = int(render.get("source_fps_num", 25))
    source_fps_den = int(render.get("source_fps_den", 1))

    src = core.lsmas.LWLibavSource(str(video_path), cachefile=str(lsmash_index))
    src = core.std.SetFrameProps(
        src,
        _FieldBased=(vs.FIELD_TOP if tff else vs.FIELD_BOTTOM),
        _Matrix=matrix,
        _Transfer=transfer,
        _Primaries=primaries,
        _Range=int(rng),
    )
    # Some AVI captures expose a bogus 600 fps timebase even though the DV
    # essence is normal 25 fps PAL. Normalize timing before bobbing to 50p.
    src = core.std.AssumeFPS(src, fpsnum=source_fps_num, fpsden=source_fps_den)

    # Promote chroma to 4:4:4 16-bit, treating fields as interlaced (CUE fix).
    src = core.resize.Bicubic(src, format=vs.YUV444P16, range_s="limited")

    if mode.get("method") == "original":
        deint = src
        out_index = max(0, min(args.frame, deint.num_frames - 1))
    else:
        deint = deinterlace(src, mode, core, vs, tff, fps_divisor, render.get("matrix", "470bg"), render.get("range", "limited"))
        # Map source frame -> output frame index for the chosen rate.
        multiplier = max(1, 2 // fps_divisor)
        out_index = args.frame * multiplier
        out_index = max(0, min(out_index, deint.num_frames - 1))

    if args.full_video:
        # MP4 output is yuv420p, so avoid the previous YUV->RGB->YUV round trip.
        video_yuv = core.resize.Bicubic(deint, format=vs.YUV420P8, range_in=int(rng), range=int(rng))
        fps = clip_fps(video_yuv)
        count = write_mp4(
            video_yuv,
            out_index,
            None,
            out_path,
            input_pix_fmt="yuv420p",
            encode_preset=args.encode_preset,
            crf=args.crf,
            pix_fmt=args.pix_fmt,
            profile=args.x264_profile,
            level=args.x264_level,
            tune=args.x264_tune,
            encoder=args.encoder,
            nvenc_preset=args.nvenc_preset,
            nvenc_cq=args.nvenc_cq,
            nvenc_tune=args.nvenc_tune,
        )
        print(f"wrote {out_path} ({count} frames at {fps:.3f} fps, start out {out_index}, full video)")
    elif args.clip_seconds > 0:
        # Keep a full requested-duration clip by sliding back near the end.
        video_yuv = core.resize.Bicubic(deint, format=vs.YUV420P8, range_in=int(rng), range=int(rng))
        fps = clip_fps(video_yuv)
        wanted = max(1, int(round(fps * args.clip_seconds)))
        out_index = min(out_index, max(0, video_yuv.num_frames - wanted))
        count = write_mp4(
            video_yuv,
            out_index,
            args.clip_seconds,
            out_path,
            input_pix_fmt="yuv420p",
            encode_preset=args.encode_preset,
            crf=args.crf,
            pix_fmt=args.pix_fmt,
            profile=args.x264_profile,
            level=args.x264_level,
            tune=args.x264_tune,
            encoder=args.encoder,
            nvenc_preset=args.nvenc_preset,
            nvenc_cq=args.nvenc_cq,
            nvenc_tune=args.nvenc_tune,
        )
        print(f"wrote {out_path} ({count} frames at {fps:.3f} fps, start out {out_index})")
    else:
        import numpy as np
        from PIL import Image

        rgb = core.resize.Bicubic(
            deint, format=vs.RGB24, matrix_in=matrix, range_in=int(rng), range=int(vs.RANGE_FULL)
        )
        write_png(rgb, out_index, out_path, np, Image)
        print(f"wrote {out_path} (src frame {args.frame} -> out {out_index})")
    lsmash_index.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
