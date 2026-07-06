from __future__ import annotations

from pathlib import Path
from typing import Any

from dump2done.core.artifacts import stage_artifact, write_json
from dump2done.core.job import JobContext
from dump2done.media.ffprobe import run_ffprobe, summarize_video_info


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
        self._not_implemented("transcribe", "Phase 2 next: FFmpeg audio extraction + faster-whisper.")

    def select_clips(self) -> None:
        self._not_implemented("select_clips", "Phase 2 next: transcript chunking + LLM selection.")

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

