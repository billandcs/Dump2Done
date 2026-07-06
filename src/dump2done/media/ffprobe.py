from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def run_ffprobe(input_path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found in PATH")

    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffprobe failed")

    return json.loads(completed.stdout)


def summarize_video_info(probe: dict[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    fmt = probe.get("format", {})

    duration = _float_or_none(fmt.get("duration") or video_stream.get("duration"))
    fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))

    return {
        "duration": duration,
        "width": _int_or_none(video_stream.get("width")),
        "height": _int_or_none(video_stream.get("height")),
        "fps": fps,
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name"),
        "bit_rate": _int_or_none(fmt.get("bit_rate")),
        "format_name": fmt.get("format_name"),
        "streams": [
            {
                "index": stream.get("index"),
                "type": stream.get("codec_type"),
                "codec": stream.get("codec_name"),
            }
            for stream in streams
        ],
        "raw_probe": probe,
    }


def _parse_fps(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            denominator_float = float(denominator)
            return round(float(numerator) / denominator_float, 4) if denominator_float else None
        except ValueError:
            return None
    return _float_or_none(value)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

