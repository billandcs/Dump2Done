from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from dump2done.core.artifacts import stage_artifact, write_json


LogCallback = Callable[[str], None]
StageCallback = Callable[[str, str, int], None]


def run_local_video_edit(
    input_path: Path,
    job_dir: Path,
    prompt: str,
    resolution: str,
    log: LogCallback | None = None,
    stage: StageCallback | None = None,
) -> dict[str, Any]:
    """Run the local deterministic video edit MVP and write an MP4 render artifact."""
    log = log or (lambda message: None)
    stage = stage or (lambda name, status, progress: None)
    input_path = input_path.resolve()
    job_dir = job_dir.resolve()
    cache_dir = job_dir / "cache" / "video_edit"
    raw_frames_dir = cache_dir / "frames_raw"
    edited_frames_dir = cache_dir / "frames_edited"
    renders_dir = job_dir / "renders"
    reports_dir = job_dir / "reports"
    cache_dir.mkdir(parents=True, exist_ok=True)
    renders_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    _reset_dir(raw_frames_dir)
    _reset_dir(edited_frames_dir)

    stage("analyze", "running", 10)
    log(f"[video-edit] probing source: {input_path.name}")
    probe = ffprobe_json(input_path)
    video_info = summarize_probe(probe)
    write_json(reports_dir / "video_info.json", stage_artifact("analyze", "completed", **video_info))
    stage("analyze", "completed", 100)

    stage("edit_plan", "running", 20)
    operations = plan_operations(prompt)
    plan = stage_artifact(
        "edit_plan",
        "completed",
        prompt=prompt,
        operations=operations,
        note="Local MVP uses deterministic Pillow frame transforms; AI segmentation is a later runner upgrade.",
    )
    write_json(reports_dir / "video_edit_plan.json", plan)
    log("[video-edit] plan: " + ", ".join(operations))
    stage("edit_plan", "completed", 100)

    fps = video_info.get("fps") or 30.0
    fps_text = format_fps(float(fps))
    scale_filter = scale_filter_for_resolution(resolution)
    vf_parts = []
    if scale_filter:
        vf_parts.append(scale_filter)
    vf = ",".join(vf_parts)

    stage("video_edit", "running", 5)
    log(f"[video-edit] extracting frames at {fps_text} fps")
    extract_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
    ]
    if vf:
        extract_command.extend(["-vf", vf])
    extract_command.extend([str(raw_frames_dir / "frame_%06d.png")])
    run_command(extract_command)

    frames = sorted(raw_frames_dir.glob("frame_*.png"))
    if not frames:
        raise RuntimeError("FFmpeg did not extract any frames from the uploaded video.")

    total = len(frames)
    for index, frame_path in enumerate(frames, start=1):
        output_frame = edited_frames_dir / frame_path.name
        with Image.open(frame_path) as source:
            edited = apply_frame_operations(source.convert("RGB"), operations)
            edited.save(output_frame, format="PNG", optimize=False)
        if index == 1 or index == total or index % max(1, total // 20) == 0:
            progress = max(5, min(98, round(index / total * 100)))
            stage("video_edit", "running", progress)
            log(f"[video-edit] processed frame {index}/{total}")
    stage("video_edit", "completed", 100)

    stage("render", "running", 30)
    output_path = unique_render_path(renders_dir / f"edited_{input_path.stem}.mp4")
    log(f"[video-edit] encoding render: {output_path.name}")
    render_command = [
        "ffmpeg",
        "-y",
        "-framerate",
        fps_text,
        "-i",
        str(edited_frames_dir / "frame_%06d.png"),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    run_command(render_command)
    stage("render", "completed", 100)

    relative_output = str(output_path.relative_to(job_dir)).replace("\\", "/")
    result = stage_artifact(
        "video_edit",
        "completed",
        prompt=prompt,
        operations=operations,
        input_path=str(input_path),
        frame_count=total,
        fps=fps,
        resolution=resolution,
        outputs={"edited_video": relative_output},
        output_path=relative_output,
        extract_command=extract_command,
        render_command=render_command,
    )
    write_json(reports_dir / "video_edit.json", result)
    log(f"[video-edit] completed: {relative_output}")
    return result


def plan_operations(prompt: str) -> list[str]:
    prompt_lower = prompt.lower()
    operations: list[str] = []
    clothing_tokens = ["衣服", "外套", "上衣", "衣著", "clothes", "clothing", "shirt", "jacket"]
    white_tokens = ["白色", "變白", "換成白", "white"]
    if any(token in prompt_lower for token in clothing_tokens) and any(token in prompt_lower for token in white_tokens):
        operations.append("whiten_clothing_region")
    elif any(token in prompt_lower for token in white_tokens):
        operations.append("bright_white_tone")
    if any(token in prompt_lower for token in ["黑白", "灰階", "grayscale", "black and white", "b&w"]):
        operations.append("grayscale")
    if any(token in prompt_lower for token in ["變亮", "提亮", "bright", "brighter"]):
        operations.append("brighten")
    if any(token in prompt_lower for token in ["變暗", "dark", "darker"]):
        operations.append("darken")
    if any(token in prompt_lower for token in ["對比", "contrast"]):
        operations.append("contrast")
    if any(token in prompt_lower for token in ["銳化", "sharp", "sharpen"]):
        operations.append("sharpen")
    if any(token in prompt_lower for token in ["模糊", "blur"]):
        operations.append("blur")
    if not operations:
        operations.append("copy_original_frames")
    return operations


def apply_frame_operations(image: Image.Image, operations: list[str]) -> Image.Image:
    edited = image
    for operation in operations:
        if operation == "whiten_clothing_region":
            edited = whiten_clothing_region(edited)
        elif operation == "bright_white_tone":
            edited = ImageEnhance.Color(edited).enhance(0.35)
            edited = ImageEnhance.Brightness(edited).enhance(1.18)
        elif operation == "grayscale":
            edited = ImageOps.grayscale(edited).convert("RGB")
        elif operation == "brighten":
            edited = ImageEnhance.Brightness(edited).enhance(1.18)
        elif operation == "darken":
            edited = ImageEnhance.Brightness(edited).enhance(0.82)
        elif operation == "contrast":
            edited = ImageEnhance.Contrast(edited).enhance(1.18)
        elif operation == "sharpen":
            edited = edited.filter(ImageFilter.SHARPEN)
        elif operation == "blur":
            edited = edited.filter(ImageFilter.GaussianBlur(radius=1.4))
    return edited


def whiten_clothing_region(image: Image.Image) -> Image.Image:
    width, height = image.size
    luma = ImageOps.grayscale(image)
    dark_mask = luma.point(lambda value: 255 if 18 <= value <= 205 else 0)
    region_mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(region_mask)
    draw.rounded_rectangle(
        (
            round(width * 0.14),
            round(height * 0.16),
            round(width * 0.86),
            round(height * 0.95),
        ),
        radius=max(12, round(min(width, height) * 0.04)),
        fill=255,
    )
    mask = ImageChops.multiply(dark_mask, region_mask).filter(ImageFilter.GaussianBlur(radius=5))
    desaturated = ImageEnhance.Color(image).enhance(0.05)
    brighter = ImageEnhance.Brightness(desaturated).enhance(1.45)
    white_layer = Image.blend(brighter, Image.new("RGB", image.size, (238, 238, 232)), 0.58)
    return Image.composite(white_layer, image, mask)


def ffprobe_json(input_path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    data = json.loads(completed.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("ffprobe returned unexpected data.")
    return data


def summarize_probe(probe: dict[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    fmt = probe.get("format") or {}
    fps = parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    return {
        "duration": safe_float(video.get("duration")) or safe_float(fmt.get("duration")),
        "width": safe_int(video.get("width")),
        "height": safe_int(video.get("height")),
        "fps": fps,
        "codec": video.get("codec_name"),
        "format_name": fmt.get("format_name"),
        "bit_rate": safe_int(fmt.get("bit_rate")),
        "file_size": safe_int(fmt.get("size")),
        "raw_probe": probe,
    }


def parse_fps(value: object) -> float | None:
    if not value:
        return None
    text = str(value)
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        denominator_float = safe_float(denominator)
        if denominator_float:
            return safe_float(numerator) / denominator_float
    return safe_float(text)


def format_fps(value: float) -> str:
    if value <= 0:
        return "30"
    if abs(value - round(value)) < 0.01:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def scale_filter_for_resolution(resolution: str) -> str:
    lowered = str(resolution or "").lower()
    max_side = 0
    if "720" in lowered:
        max_side = 1280
    elif "1080" in lowered:
        max_side = 1920
    if not max_side:
        return ""
    return (
        "scale="
        f"'if(gt(iw,ih),min(iw,{max_side}),-2)':"
        f"'if(gt(iw,ih),-2,min(ih,{max_side}))'"
    )


def run_command(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise RuntimeError(tail or f"Command failed with exit code {completed.returncode}: {command[0]}")


def unique_render_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: object) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
