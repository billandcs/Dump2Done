from __future__ import annotations

from pathlib import Path
from typing import Any

from dump2done.asr.faster_whisper_backend import transcribe_with_faster_whisper
from dump2done.core.artifacts import read_json, stage_artifact, write_json
from dump2done.core.job import JobContext
from dump2done.media.audio import extract_audio_wav, summarize_audio_info
from dump2done.media.ffprobe import run_ffprobe, summarize_video_info
from dump2done.pipeline.selection import build_selection_artifacts


class PipelineRunner:
    def __init__(self, config: dict[str, Any], job_id: str, resume: bool = False) -> None:
        self.config = config
        self.job = JobContext(config=config, job_id=job_id)
        self.resume = resume

    def analyze(self, input_path: Path) -> None:
        manifest = self.job.create_or_update_manifest(input_path=input_path)
        self.job.mark_stage("analyze", "running")
        video_path = self.job.path(manifest["input"]["source_path"])
        video_info_path = self.job.path("reports/video_info.json")

        if self.resume and video_info_path.exists():
            self.job.mark_stage("analyze", "completed")
            print(f"Reused existing artifact: {video_info_path}")
            return

        try:
            probe = run_ffprobe(video_path)
            summary = summarize_video_info(probe)
            artifact = stage_artifact(
                "analyze",
                "completed",
                input_path=manifest["input"]["source_path"],
                outputs={"video_info": "reports/video_info.json"},
                **summary,
            )
            write_json(video_info_path, artifact)
            self.job.mark_stage("analyze", "completed")
            print(f"Wrote {video_info_path}")
        except Exception as exc:
            artifact = stage_artifact(
                "analyze",
                "failed",
                input_path=manifest["input"].get("source_path"),
                errors=[{"message": str(exc)}],
            )
            write_json(video_info_path, artifact)
            self.job.mark_stage("analyze", "failed")
            raise

    def transcribe(self) -> None:
        manifest = self.job.create_or_update_manifest()
        if not manifest.get("input", {}).get("source_path"):
            raise RuntimeError("Job has no input video. Run analyze first.")

        self.job.mark_stage("transcribe", "running")
        audio_info_path = self.job.path("audio/audio_info.json")
        audio_path = self.job.path("audio/audio.wav")
        transcript_path = self.job.path("transcripts/transcript.json")
        words_path = self.job.path("transcripts/words.json")

        if (
            self.resume
            and audio_info_path.exists()
            and audio_path.exists()
            and transcript_path.exists()
            and words_path.exists()
        ):
            self.job.mark_stage("transcribe", "completed")
            self.job.mark_stage("asr", "completed")
            print(f"Reused existing artifacts: {audio_info_path}, {transcript_path}, {words_path}")
            return

        media_config = self.config.get("media", {})
        sample_rate = int(media_config.get("extract_audio_sample_rate", 16000))
        channels = int(media_config.get("extract_audio_channels", 1))
        video_path = self.job.path(manifest["input"]["source_path"])

        try:
            extraction = extract_audio_wav(
                input_video=video_path,
                output_audio=audio_path,
                sample_rate=sample_rate,
                channels=channels,
            )
            summary = summarize_audio_info(audio_path)
            relative_audio = str(audio_path.relative_to(self.job.job_dir)).replace("\\", "/")
            artifact = stage_artifact(
                "extract_audio",
                "completed",
                input_path=manifest["input"]["source_path"],
                audio_path=relative_audio,
                outputs={"audio": relative_audio, "audio_info": "audio/audio_info.json"},
                ffmpeg_command=extraction["command"],
                duration=summary["duration"],
                sample_rate=summary["sample_rate"],
                channels=summary["channels"],
                codec=summary["codec"],
                format_name=summary["format_name"],
                bit_rate=summary["bit_rate"],
                file_size=summary["file_size"],
                raw_probe=summary["raw_probe"],
            )
            write_json(audio_info_path, artifact)
            print(f"Wrote {audio_info_path}")

            self.job.mark_stage("asr", "running")
            asr_config = self.config.get("asr", {})
            asr_result = transcribe_with_faster_whisper(audio_path, asr_config)
            transcript_artifact = stage_artifact(
                "transcribe",
                "completed",
                backend=asr_result["backend"],
                model=asr_result["model"],
                device=asr_result["device"],
                compute_type=asr_result["compute_type"],
                language=asr_result["language"],
                language_probability=asr_result["language_probability"],
                duration=asr_result["duration"],
                duration_after_vad=asr_result["duration_after_vad"],
                audio_path=relative_audio,
                outputs={
                    "transcript": "transcripts/transcript.json",
                    "words": "transcripts/words.json",
                },
                segments=asr_result["segments"],
                segment_count=len(asr_result["segments"]),
                word_count=len(asr_result["words"]),
            )
            words_artifact = stage_artifact(
                "transcribe_words",
                "completed",
                backend=asr_result["backend"],
                model=asr_result["model"],
                language=asr_result["language"],
                audio_path=relative_audio,
                words=asr_result["words"],
                word_count=len(asr_result["words"]),
            )
            write_json(transcript_path, transcript_artifact)
            write_json(words_path, words_artifact)
            self.job.mark_stage("asr", "completed")
            self.job.mark_stage("transcribe", "completed")
            print(f"Wrote {transcript_path}")
            print(f"Wrote {words_path}")
        except Exception as exc:
            artifact = stage_artifact(
                "transcribe",
                "failed",
                input_path=manifest["input"].get("source_path"),
                errors=[{"message": str(exc)}],
            )
            failure_path = transcript_path if audio_path.exists() else audio_info_path
            write_json(failure_path, artifact)
            self.job.mark_stage("transcribe", "failed")
            self.job.mark_stage("asr", "failed")
            raise

    def select_clips(self) -> None:
        self.job.create_or_update_manifest()
        transcript_path = self.job.path("transcripts/transcript.json")
        segments_path = self.job.path("transcripts/segments.json")
        llm_input_path = self.job.path("llm/llm_input.json")
        candidates_path = self.job.path("llm/clip_candidates.json")
        validated_path = self.job.path("clips/validated_clips.json")

        if not transcript_path.exists():
            raise RuntimeError("Missing transcript artifact. Run transcribe first.")

        if (
            self.resume
            and segments_path.exists()
            and llm_input_path.exists()
            and candidates_path.exists()
            and validated_path.exists()
        ):
            self.job.mark_stage("select_clips", "completed")
            print(
                "Reused existing artifacts: "
                f"{segments_path}, {llm_input_path}, {candidates_path}, {validated_path}"
            )
            return

        self.job.mark_stage("select_clips", "running")
        try:
            transcript = read_json(transcript_path)
            artifacts = build_selection_artifacts(transcript, self.config)
            write_json(segments_path, artifacts["segments"])
            write_json(llm_input_path, artifacts["llm_input"])
            write_json(candidates_path, artifacts["clip_candidates"])
            write_json(validated_path, artifacts["validated_clips"])
            self.job.mark_stage("select_clips", "completed")
            print(f"Wrote {segments_path}")
            print(f"Wrote {llm_input_path}")
            print(f"Wrote {candidates_path}")
            print(f"Wrote {validated_path}")
        except Exception as exc:
            artifact = stage_artifact(
                "select_clips",
                "failed",
                source="transcripts/transcript.json",
                errors=[{"message": str(exc)}],
            )
            write_json(candidates_path, artifact)
            self.job.mark_stage("select_clips", "failed")
            raise

    def crop(self) -> None:
        self._not_implemented("crop", "Phase 2 next: low-fps face detection + crop smoothing.")

    def subtitle(self) -> None:
        self._not_implemented("subtitle", "Phase 2 next: word timestamps to ASS subtitle.")

    def render(self) -> None:
        self._not_implemented("render", "Phase 2 next: FFmpeg render plan + NVENC fallback.")

    def run_all(self, input_path: Path) -> None:
        self.analyze(input_path)
        self.transcribe()
        self.select_clips()
        self.crop()
        self.subtitle()
        self.render()

    def _not_implemented(self, stage: str, message: str) -> None:
        self.job.create_or_update_manifest()
        self.job.mark_stage(stage, "pending")
        raise NotImplementedError(message)
