from __future__ import annotations

from pathlib import Path
from typing import Any


def transcribe_with_faster_whisper(audio_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed.") from exc

    model_size = str(config.get("model_size", "small"))
    device = normalize_device(str(config.get("device", "cpu")))
    compute_type = normalize_compute_type(str(config.get("compute_type", "int8")), device)
    cpu_threads = int(config.get("cpu_threads", 0) or 0)
    num_workers = int(config.get("num_workers", 1) or 1)

    model_kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
        "num_workers": num_workers,
    }
    if cpu_threads > 0:
        model_kwargs["cpu_threads"] = cpu_threads

    model = WhisperModel(model_size, **model_kwargs)
    segments_iter, info = model.transcribe(
        str(audio_path),
        word_timestamps=bool(config.get("word_timestamps", True)),
        vad_filter=bool(config.get("vad_filter", True)),
        beam_size=int(config.get("beam_size", 1) or 1),
    )

    segments = []
    words = []
    for index, segment in enumerate(segments_iter):
        segment_words = []
        for word in segment.words or []:
            word_payload = {
                "start": safe_float(word.start),
                "end": safe_float(word.end),
                "word": word.word.strip(),
                "confidence": safe_float(getattr(word, "probability", None)),
                "segment_id": index,
            }
            words.append(word_payload)
            segment_words.append(word_payload)

        segments.append(
            {
                "id": index,
                "start": safe_float(segment.start),
                "end": safe_float(segment.end),
                "text": segment.text.strip(),
                "avg_logprob": safe_float(getattr(segment, "avg_logprob", None)),
                "no_speech_prob": safe_float(getattr(segment, "no_speech_prob", None)),
                "words": segment_words,
            }
        )

    return {
        "backend": "faster_whisper",
        "model": model_size,
        "device": device,
        "compute_type": compute_type,
        "language": getattr(info, "language", None),
        "language_probability": safe_float(getattr(info, "language_probability", None)),
        "duration": safe_float(getattr(info, "duration", None)),
        "duration_after_vad": safe_float(getattr(info, "duration_after_vad", None)),
        "segments": segments,
        "words": words,
    }


def normalize_device(device: str) -> str:
    if device == "auto":
        return "cpu"
    if device.startswith("cuda"):
        return "cuda"
    return device


def normalize_compute_type(compute_type: str, device: str) -> str:
    if compute_type == "auto":
        return "int8" if device == "cpu" else "float16"
    return compute_type


def safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

