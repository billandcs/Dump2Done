from __future__ import annotations

from typing import Any

from dump2done.core.artifacts import stage_artifact


def build_selection_artifacts(transcript: dict[str, Any], config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    llm_config = config.get("llm", {})
    chunk_duration = float(llm_config.get("chunk_duration_sec") or config.get("pipeline", {}).get("chunk_duration_sec") or 180)
    min_duration = float(llm_config.get("min_duration") or 20)
    max_duration = float(llm_config.get("max_duration") or 75)
    global_top_n = int(llm_config.get("global_top_n") or 3)

    source_segments = normalize_transcript_segments(transcript.get("segments") or [])
    chunks = chunk_transcript_segments(source_segments, chunk_duration)
    empty_reason = ""
    if not source_segments:
        empty_reason = "No transcript segments were available. The input may contain no detectable speech."
    elif not chunks:
        empty_reason = "Transcript text was too sparse to build semantic chunks."

    candidates = build_baseline_candidates(chunks, min_duration, max_duration, global_top_n)
    validated = [
        candidate
        for candidate in candidates
        if candidate.get("validation", {}).get("meets_min_duration")
        and candidate.get("validation", {}).get("meets_max_duration")
    ]

    segments_artifact = stage_artifact(
        "semantic_segmentation",
        "completed",
        source="transcripts/transcript.json",
        chunk_duration_sec=chunk_duration,
        source_segment_count=len(source_segments),
        chunk_count=len(chunks),
        empty_reason=empty_reason,
        outputs={
            "segments": "transcripts/segments.json",
            "llm_input": "llm/llm_input.json",
            "clip_candidates": "llm/clip_candidates.json",
            "validated_clips": "clips/validated_clips.json",
        },
        chunks=chunks,
    )

    llm_input = stage_artifact(
        "llm_input",
        "ready" if chunks else "skipped",
        backend=llm_config.get("backend", "ollama"),
        model=llm_config.get("model", "unknown"),
        task="select_short_video_highlights",
        source="transcripts/segments.json",
        empty_reason=empty_reason,
        constraints={
            "global_top_n": global_top_n,
            "min_duration": min_duration,
            "max_duration": max_duration,
            "top_n_candidates_per_chunk": int(llm_config.get("top_n_candidates_per_chunk") or 3),
        },
        instruction=(
            "Select short-form highlight clips from transcript chunks. Return start/end seconds, "
            "a concise title, rationale, and confidence. Prefer complete thoughts and avoid dead air."
        ),
        chunks=[
            {
                "chunk_id": chunk["chunk_id"],
                "start": chunk["start"],
                "end": chunk["end"],
                "duration": chunk["duration"],
                "transcript": chunk["text"],
            }
            for chunk in chunks
        ],
    )

    clip_candidates = stage_artifact(
        "select_clips",
        "completed",
        selection_mode="deterministic_transcript_baseline",
        source="llm/llm_input.json",
        empty_reason=empty_reason,
        candidate_count=len(candidates),
        candidates=candidates,
        note="This baseline prepares stable artifacts before enabling live Ollama selection.",
    )

    validated_clips = stage_artifact(
        "validate_clips",
        "completed",
        source="llm/clip_candidates.json",
        empty_reason=empty_reason or ("No candidates met duration constraints." if candidates and not validated else ""),
        selected_count=len(validated),
        clips=validated,
    )

    return {
        "segments": segments_artifact,
        "llm_input": llm_input,
        "clip_candidates": clip_candidates,
        "validated_clips": validated_clips,
    }


def normalize_transcript_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for index, segment in enumerate(segments, start=1):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = safe_float(segment.get("start"))
        end = safe_float(segment.get("end"))
        if end < start:
            end = start
        words = segment.get("words") if isinstance(segment.get("words"), list) else []
        normalized.append(
            {
                "segment_id": f"seg_{index:04d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(max(0.0, end - start), 3),
                "text": text,
                "word_count": len(words) if words else len(text.split()),
            }
        )
    return normalized


def chunk_transcript_segments(segments: list[dict[str, Any]], chunk_duration: float) -> list[dict[str, Any]]:
    if not segments:
        return []

    chunk_duration = max(30.0, chunk_duration)
    chunks = []
    current: list[dict[str, Any]] = []
    chunk_start = segments[0]["start"]

    for segment in segments:
        if current and segment["end"] - chunk_start > chunk_duration:
            chunks.append(make_chunk(len(chunks) + 1, current))
            current = []
            chunk_start = segment["start"]
        current.append(segment)

    if current:
        chunks.append(make_chunk(len(chunks) + 1, current))
    return chunks


def make_chunk(index: int, segments: list[dict[str, Any]]) -> dict[str, Any]:
    start = min(segment["start"] for segment in segments)
    end = max(segment["end"] for segment in segments)
    text = " ".join(segment["text"] for segment in segments).strip()
    word_count = sum(int(segment.get("word_count") or 0) for segment in segments)
    return {
        "chunk_id": f"chunk_{index:03d}",
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(max(0.0, end - start), 3),
        "text": text,
        "text_preview": preview_text(text, 180),
        "word_count": word_count,
        "source_segment_ids": [segment["segment_id"] for segment in segments],
    }


def build_baseline_candidates(
    chunks: list[dict[str, Any]],
    min_duration: float,
    max_duration: float,
    global_top_n: int,
) -> list[dict[str, Any]]:
    ranked = sorted(chunks, key=lambda item: (item.get("word_count", 0), item.get("duration", 0)), reverse=True)
    candidates = []
    for index, chunk in enumerate(ranked[: max(0, global_top_n)], start=1):
        duration = float(chunk.get("duration") or 0.0)
        end = float(chunk.get("end") or 0.0)
        start = float(chunk.get("start") or 0.0)
        if duration > max_duration:
            end = start + max_duration
            duration = max_duration
        meets_min = duration >= min_duration
        meets_max = duration <= max_duration
        candidates.append(
            {
                "candidate_id": f"clip_{index:03d}",
                "chunk_id": chunk["chunk_id"],
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(duration, 3),
                "title": title_from_text(str(chunk.get("text") or "")),
                "rationale": "Deterministic baseline candidate from the densest transcript chunk.",
                "score": round(min(1.0, (float(chunk.get("word_count") or 0) / 120.0)), 3),
                "text_preview": chunk.get("text_preview", ""),
                "validation": {
                    "meets_min_duration": meets_min,
                    "meets_max_duration": meets_max,
                    "needs_llm_review": True,
                },
            }
        )
    return candidates


def title_from_text(text: str) -> str:
    words = text.split()
    if not words:
        return "Untitled clip"
    title = " ".join(words[:8]).strip(" ,.;:!?")
    return title or "Untitled clip"


def preview_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
