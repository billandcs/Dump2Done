from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from dump2done.media.ffprobe import run_ffprobe


def extract_audio_wav(
    input_video: Path,
    output_audio: Path,
    sample_rate: int = 16000,
    channels: int = 1,
) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")

    output_audio.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_video),
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(output_audio),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffmpeg audio extraction failed")

    return {
        "command": command,
        "stderr": completed.stderr.strip(),
        "output_path": output_audio,
    }


def summarize_audio_info(audio_path: Path) -> dict[str, Any]:
    probe = run_ffprobe(audio_path)
    streams = probe.get("streams", [])
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    fmt = probe.get("format", {})

    return {
        "audio_path": audio_path,
        "duration": _float_or_none(fmt.get("duration") or audio_stream.get("duration")),
        "sample_rate": _int_or_none(audio_stream.get("sample_rate")),
        "channels": _int_or_none(audio_stream.get("channels")),
        "codec": audio_stream.get("codec_name"),
        "format_name": fmt.get("format_name"),
        "bit_rate": _int_or_none(fmt.get("bit_rate") or audio_stream.get("bit_rate")),
        "file_size": _int_or_none(fmt.get("size")),
        "raw_probe": probe,
    }


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

