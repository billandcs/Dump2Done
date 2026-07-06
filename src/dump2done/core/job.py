from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from dump2done.core.artifacts import read_json, utc_now, write_json


class JobContext:
    def __init__(self, config: dict[str, Any], job_id: str) -> None:
        self.config = config
        self.job_id = job_id
        output_root = Path(config.get("project", {}).get("output_root", "output/jobs"))
        self.job_dir = output_root / job_id

    def path(self, relative_path: str | Path) -> Path:
        return self.job_dir / relative_path

    def ensure_dirs(self) -> None:
        for name in [
            "input",
            "audio",
            "transcripts",
            "llm",
            "clips",
            "crop_tracks",
            "subtitles",
            "renders",
            "reports",
            "cache",
        ]:
            self.path(name).mkdir(parents=True, exist_ok=True)

    def manifest_path(self) -> Path:
        return self.path("job_manifest.json")

    def load_manifest(self) -> dict[str, Any]:
        return read_json(self.manifest_path())

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        write_json(self.manifest_path(), manifest)

    def create_or_update_manifest(self, input_path: Path | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        if self.manifest_path().exists():
            manifest = self.load_manifest()
        else:
            manifest = {
                "schema_version": "1.0",
                "job_id": self.job_id,
                "status": "running",
                "created_at": utc_now(),
                "input": {},
                "config": {
                    "profile": self.config.get("project", {}).get("name", "default"),
                    "effective_config_path": "reports/effective_config.json",
                },
                "stages": {},
                "errors": [],
            }

        if input_path is not None:
            copied = self.copy_input(input_path)
            manifest["input"] = {
                "source_path": str(copied.relative_to(self.job_dir)).replace("\\", "/"),
                "original_path": str(input_path),
                "original_filename": input_path.name,
                "content_hash": file_sha256(copied),
            }

        self.save_manifest(manifest)
        write_json(self.path("reports/effective_config.json"), self.config)
        return manifest

    def mark_stage(self, stage: str, status: str) -> None:
        manifest = self.load_manifest()
        manifest.setdefault("stages", {})[stage] = status
        manifest["updated_at"] = utc_now()
        self.save_manifest(manifest)

    def copy_input(self, input_path: Path) -> Path:
        if not input_path.exists():
            raise FileNotFoundError(f"Input video not found: {input_path}")
        target = self.path("input") / input_path.name
        if input_path.resolve() != target.resolve():
            shutil.copy2(input_path, target)
        return target


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()

