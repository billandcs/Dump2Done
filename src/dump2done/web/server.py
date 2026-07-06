from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import html
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib import error as urllib_error
from urllib.parse import parse_qs, quote, urlparse
from urllib import request as urllib_request


SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dump2done.pipeline.video_edit import VideoEditCancelled, run_local_video_edit


STAGE_LABELS = [
    ("analyze", "影片分析"),
    ("transcribe", "音訊抽取"),
    ("asr", "逐字稿"),
    ("select_clips", "精華片段"),
    ("crop", "智慧裁切"),
    ("subtitle", "字幕"),
    ("render", "輸出"),
]

PIPELINE_EVENT_QUEUE: queue.Queue[dict] = queue.Queue()
VIDEO_WORKERS: set[str] = set()
VIDEO_WORKERS_LOCK = threading.Lock()
LOG_WRITE_LOCK = threading.Lock()
DEFAULT_OUTPUT_ROOT = Path("output/jobs")
CONSOLE_LOG_PATH = Path("output/logs/dashboard_console.ndjson")
DASHBOARD_SETTINGS_PATH = Path("output/dashboard_settings.json")
DASHBOARD_DEFAULT_SETTINGS = {
    "defaultOutputDirectory": "output",
    "autoPreviewOutput": False,
    "autoOpenOutputFolder": False,
    "galleryDensity": "comfortable",
    "localLlmProvider": "ollama_demo",
    "localLlmEndpoint": "http://127.0.0.1:11434",
    "visionProvider": "local_pillow_mvp",
    "imageEditProvider": "auto",
    "automatic1111Endpoint": "http://127.0.0.1:7860",
    "comfyuiEndpoint": "http://127.0.0.1:8188",
    "openaiImageModel": "gpt-image-1.5",
    "asrProvider": "faster_whisper_cpu_demo",
    "onlineFallbackPolicy": "warn_only",
}

TOOLTIPS = {
    "Qualcomm Q1": "這代表目前這台機器是 Qualcomm 平台，適合先用 CPU int8 跑本地 MVP，未來再評估 QNN/DirectML 加速。Q1 不是效能分數滿分，而是「可開發、待加速優化」。",
    "Hardware": "Dump2Done 對目前機器的整體分級。Q 代表 Qualcomm Windows on ARM 路線。",
    "Qualcomm": "是否偵測到 Qualcomm / Snapdragon 類 CPU 平台。",
    "Python": "目前 Python 是否可能跑在 x64 模擬層。native ARM64 通常會更適合這台機器。",
    "Q Readiness": "Qualcomm-ready 程度。Q1 表示先走 CPU 穩定 pipeline，之後準備 QNN/DirectML。",
    "AMD Readiness": "AMD-ready 程度。AM1 表示可做 AMD 平台開發，但 AMF/DirectML/ROCm 仍需實機驗證。",
    "Intel Readiness": "Intel-ready 程度。I1 表示可做 Intel 平台開發，但 QSV/OpenVINO/DirectML 仍需實機驗證。",
    "AMD": "偵測是否有 AMD CPU 或 Radeon/AMD GPU，未來可評估 AMF、DirectML、ROCm 路線。",
    "Intel": "偵測是否有 Intel CPU/GPU/NPU 線索，未來可評估 Quick Sync、OpenVINO、DirectML 路線。",
    "QNN EP": "ONNX Runtime 的 Qualcomm NPU 執行後端，可讓合適的 ONNX 模型跑在 Hexagon NPU。不是所有模型都適合。",
    "DirectML EP": "ONNX Runtime 的 Windows GPU 執行後端，可作為 Qualcomm Adreno GPU 的未來 fallback。",
    "Encoder": "目前影片輸出編碼路線。這台機器本地先用 CPU libx264，不假設 NVENC。",
    "ASR": "Automatic Speech Recognition，自動語音辨識。這一步會把音訊轉成 transcript.json 和 words.json。",
    "影片分析": "讀取影片基本資料，例如長度、解析度、FPS、影音 codec。這一步使用 ffprobe。",
    "音訊抽取": "把影片音軌抽成 16kHz mono WAV，方便後續語音辨識。",
    "逐字稿": "使用 faster-whisper 把語音轉成段落與 word-level timestamps。",
    "精華片段": "把逐字稿切成語意 chunks，建立 LLM input，並產生候選短影音片段。",
    "Semantic Chunks": "將 transcript 依時間與語意整理成較小段落，讓 LLM 不需要一次讀完整影片。",
    "Clip Candidates": "目前由 deterministic baseline 產生，下一階段會接 Ollama 做內容判斷。",
    "Validated Clips": "通過最短/最長時間限制的候選片段，後續會進入裁切與字幕流程。",
    "智慧裁切": "未來會分析主體位置，把橫式影片重構成 9:16 或 1:1。",
    "字幕": "未來會用 word timestamps 產生可燒錄的 ASS 動態字幕。",
    "輸出": "最後用 FFmpeg 輸出 MP4；這台 Qualcomm 本機先走 CPU encoder。",
}


class DashboardHandler(BaseHTTPRequestHandler):
    output_root = Path("output/jobs")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            query = parse_qs(parsed.query)
            self._send_html(render_index(self.output_root, query.get("job", [None])[0]))
            return
        if parsed.path == "/api/stream-logs":
            query = parse_qs(parsed.query)
            self._send_sse(stream_pipeline_events(self.output_root, query.get("job", [""])[0]))
            return
        if parsed.path == "/api/jobs":
            self._send_json(
                {
                    "jobs": jobs_for_frontend(self.output_root),
                    "gallery": gallery_for_frontend(self.output_root),
                }
            )
            return
        if parsed.path == "/api/settings":
            self._send_json({"status": "ok", "settings": load_dashboard_settings(self.output_root)})
            return
        if parsed.path == "/artifact":
            query = parse_qs(parsed.query)
            job_id = query.get("job", [""])[0]
            artifact = query.get("path", [""])[0]
            self._send_html(render_artifact(self.output_root, job_id, artifact))
            return
        if parsed.path == "/media":
            query = parse_qs(parsed.query)
            job_id = query.get("job", [""])[0]
            media_path = query.get("path", [""])[0]
            self._send_file(self.output_root, job_id, media_path)
            return
        if parsed.path == "/export":
            query = parse_qs(parsed.query)
            export_path = query.get("path", [""])[0]
            self._send_export_file(self.output_root, export_path)
            return
        if parsed.path == "/env":
            query = parse_qs(parsed.query)
            report = get_current_env_report(self.output_root.parent / "env_report.json")
            if wants_json(self.headers.get("Accept", ""), query):
                self._send_json(report)
            else:
                self._send_html(render_env_dashboard(report))
            return
        if parsed.path == "/health":
            self._send_json({"status": "ok"})
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            try:
                payload = self._read_json_body()
                response = create_preview_job(self.output_root, payload)
                self._send_json(response, status=201)
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=400)
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=500)
            return
        if parsed.path == "/api/media-jobs":
            try:
                payload = self._read_json_body()
                response = create_media_job(self.output_root, payload)
                self._send_json(response, status=201)
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=400)
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=500)
            return
        if parsed.path == "/api/open-folder":
            try:
                payload = self._read_json_body()
                response = open_folder_request(self.output_root, payload)
                self._send_json(response)
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=400)
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=500)
            return
        if parsed.path == "/api/settings":
            try:
                payload = self._read_json_body()
                response = save_dashboard_settings(self.output_root, payload)
                self._send_json(response)
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=400)
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=500)
            return
        if parsed.path == "/api/delete-artifact":
            try:
                payload = self._read_json_body()
                response = delete_artifact_request(self.output_root, payload)
                self._send_json(response)
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=400)
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=500)
            return
        if parsed.path == "/api/cancel-job":
            try:
                payload = self._read_json_body()
                response = cancel_job_request(self.output_root, payload)
                self._send_json(response)
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=400)
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=500)
            return
        if parsed.path == "/api/console-log":
            try:
                payload = self._read_json_body()
                event = {
                    "type": "browser_log",
                    "job_id": str(payload.get("job_id") or ""),
                    "message": str(payload.get("message") or ""),
                    "source": "browser",
                    "created_at": now_utc(),
                }
                persist_console_event(event)
                self._send_json({"status": "ok", "path": str(CONSOLE_LOG_PATH)})
            except Exception as exc:
                self._send_json({"status": "error", "message": str(exc)}, status=500)
            return
        self.send_error(404, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} - {format % args}")

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object.")
        return data

    def _send_sse(self, generator) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        async def write_stream() -> None:
            async for chunk in generator:
                self.wfile.write(chunk)
                self.wfile.flush()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(write_stream())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            print("[dashboard] SSE client disconnected")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(generator.aclose())
            loop.close()

    def _send_file(self, output_root: Path, job_id: str, media_path: str) -> None:
        target = resolve_job_path(output_root, job_id, media_path)
        if not target or not target.exists() or not target.is_file():
            self.send_error(404, "Media not found")
            return

        self._send_static_file(target)

    def _send_export_file(self, output_root: Path, export_path: str) -> None:
        target = resolve_export_path(output_root, export_path)
        if not target or not target.exists() or not target.is_file():
            self.send_error(404, "Export not found")
            return

        self._send_static_file(target)

    def _send_static_file(self, target: Path) -> None:
        mime_type, _ = mimetypes.guess_type(target.name)
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def render_index(output_root: Path, selected_job_id: str | None) -> str:
    return render_job_control_dashboard(output_root, selected_job_id)


async def stream_pipeline_events(output_root: Path, job_id: str = ""):
    active_job = job_id or latest_job_id(output_root) or "sse_demo"
    is_demo_stream = active_job == "sse_demo" or not (output_root / sanitize_job_id(active_job)).exists()
    yield sse_payload(
        {
            "type": "log",
            "job_id": active_job,
            "message": f"\u001b[36m[sse]\u001b[0m Connected to Dump2Done stream for {active_job}",
            "created_at": now_utc(),
        },
        event="log",
    )
    if is_demo_stream:
        async for event in simulate_pipeline_stream(active_job):
            yield sse_payload(event, event=event.get("type", "message"))
    else:
        manifest = read_json_or_none(output_root / sanitize_job_id(active_job) / "job_manifest.json") or {}
        if manifest.get("status") == "queued":
            yield sse_payload(
                {
                    "type": "log",
                    "job_id": active_job,
                    "message": "[runner] Job is queued. Actual video pipeline has not started yet.",
                    "created_at": now_utc(),
                },
                event="log",
            )

    while True:
        queued = drain_pipeline_event_queue()
        if queued:
            for event in queued:
                yield sse_payload(event, event=event.get("type", "message"))
        else:
            yield sse_payload(
                {
                    "type": "heartbeat",
                    "job_id": active_job,
                    "message": "[sse] waiting for runner.py stdout/log queue events",
                    "created_at": now_utc(),
                },
                event="heartbeat",
            )
        await asyncio.sleep(10)


async def simulate_pipeline_stream(job_id: str):
    phases = [
        (
            "asr",
            "ASR - Faster-Whisper",
            [
                "extracting 16kHz mono WAV from source video",
                "loading faster-whisper small int8 on Qualcomm CPU profile",
                "building word-level timestamps and transcript segments",
            ],
        ),
        (
            "llm",
            "LLM - Ollama",
            [
                "packing semantic chunks for highlight selection",
                "requesting structured JSON candidates from local Ollama",
                "validating duration windows and clip boundaries",
            ],
        ),
        (
            "vision",
            "Vision",
            [
                "sampling low-fps frames for subject-aware crop planning",
                "smoothing center track for vertical 9:16 output",
                "writing crop track artifact for FFmpeg render stage",
            ],
        ),
        (
            "render",
            "FFmpeg",
            [
                "building libx264 render graph for Qualcomm local profile",
                "burning subtitles and audio normalization filters",
                "writing final MP4 render artifact",
            ],
        ),
    ]
    for step, label, messages in phases:
        yield status_event(job_id, step, "running", 5)
        await asyncio.sleep(0.25)
        for index, message in enumerate(messages, start=1):
            progress = min(95, round((index / len(messages)) * 86) + 5)
            yield log_event(job_id, f"\u001b[32m[{step}]\u001b[0m {message}")
            yield status_event(job_id, step, "running", progress)
            await asyncio.sleep(0.42)
            yield log_event(job_id, f"[{label}] progress={progress}% queue=local-sse-demo")
            await asyncio.sleep(0.18)
        yield status_event(job_id, step, "completed", 100)
        yield log_event(job_id, f"\u001b[32m[{step}]\u001b[0m completed")
        await asyncio.sleep(0.45)
    yield log_event(job_id, "\u001b[33m[demo]\u001b[0m simulated stream finished; keeping SSE connection alive")


async def stream_subprocess_stdout(command: list[str], job_id: str):
    """Bridge a real runner subprocess stdout into SSE-compatible events."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    async for raw_line in process.stdout:
        yield log_event(job_id, raw_line.decode("utf-8", errors="replace").rstrip())
    return_code = await process.wait()
    yield log_event(job_id, f"[runner] process exited with code {return_code}")


class QueueStdoutBridge:
    """File-like bridge for redirect_stdout around PipelineRunner calls."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                publish_pipeline_log(self.job_id, line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            publish_pipeline_log(self.job_id, self._buffer.rstrip())
        self._buffer = ""


def publish_pipeline_log(job_id: str, message: str) -> None:
    PIPELINE_EVENT_QUEUE.put(log_event(job_id, message))


def publish_pipeline_status(job_id: str, step: str, status: str, progress: int) -> None:
    PIPELINE_EVENT_QUEUE.put(status_event(job_id, step, status, progress))


VIDEO_EDIT_STAGE_TO_FRONTEND = {
    "analyze": "asr",
    "edit_plan": "llm",
    "video_edit": "vision",
    "render": "render",
}


def start_video_edit_worker(output_root: Path, job_id: str) -> None:
    safe_job_id = sanitize_job_id(job_id)
    with VIDEO_WORKERS_LOCK:
        if safe_job_id in VIDEO_WORKERS:
            publish_pipeline_log(safe_job_id, "[video-edit] runner is already active for this job")
            return
        VIDEO_WORKERS.add(safe_job_id)

    thread = threading.Thread(
        target=run_video_edit_worker,
        args=(output_root, safe_job_id),
        daemon=True,
        name=f"video-edit-{safe_job_id}",
    )
    thread.start()


def run_video_edit_worker(output_root: Path, job_id: str) -> None:
    job_dir = output_root / sanitize_job_id(job_id)
    try:
        manifest = read_json_or_none(job_dir / "job_manifest.json") or {}
        input_relative = manifest.get("input", {}).get("source_path")
        if not input_relative:
            raise RuntimeError("Job manifest is missing input.source_path.")
        input_path = job_dir / str(input_relative)
        request = read_json_or_none(job_dir / "reports/edit_request.json") or {}
        config = read_json_or_none(job_dir / "reports/effective_config.json") or {}
        prompt = str(request.get("prompt") or "")
        resolution = str(config.get("resolution") or request.get("resolution") or "original")

        clear_job_cancel_request(job_dir)
        set_manifest_status(job_dir, "running")
        publish_pipeline_log(job_id, "[video-edit] starting local runner")

        def on_stage(stage_name: str, stage_status: str, progress: int) -> None:
            if is_job_cancel_requested(job_dir):
                raise VideoEditCancelled("User cancelled this video edit job.")
            mark_manifest_stage(job_dir, stage_name, stage_status)
            frontend_step = VIDEO_EDIT_STAGE_TO_FRONTEND.get(stage_name, stage_name)
            publish_pipeline_status(job_id, frontend_step, stage_status, progress)

        result = run_local_video_edit(
            input_path=input_path,
            job_dir=job_dir,
            prompt=prompt,
            resolution=resolution,
            log=lambda message: publish_pipeline_log(job_id, message),
            stage=on_stage,
            should_cancel=lambda: is_job_cancel_requested(job_dir),
        )
        manifest = read_json_or_none(job_dir / "job_manifest.json") or {}
        manifest["status"] = "completed"
        manifest["updated_at"] = now_utc()
        manifest.setdefault("outputs", {})["edited_video"] = result.get("output_path")
        manifest.setdefault("reports", {})["video_edit"] = "reports/video_edit.json"
        write_json_file(job_dir / "job_manifest.json", manifest)
        publish_pipeline_log(job_id, "[video-edit] gallery refresh is ready")
    except VideoEditCancelled as exc:
        mark_video_job_cancelled(job_dir, job_id, str(exc))
    except Exception as exc:
        mark_video_job_failed(job_dir, job_id, exc)
    finally:
        with VIDEO_WORKERS_LOCK:
            VIDEO_WORKERS.discard(job_id)


def set_manifest_status(job_dir: Path, status: str) -> None:
    manifest = read_json_or_none(job_dir / "job_manifest.json") or {}
    manifest["status"] = status
    manifest["updated_at"] = now_utc()
    write_json_file(job_dir / "job_manifest.json", manifest)


def mark_manifest_stage(job_dir: Path, stage: str, status: str) -> None:
    manifest = read_json_or_none(job_dir / "job_manifest.json") or {}
    manifest.setdefault("stages", {})[stage] = status
    if status == "running":
        manifest["status"] = "running"
    elif status == "failed":
        manifest["status"] = "failed"
    manifest["updated_at"] = now_utc()
    write_json_file(job_dir / "job_manifest.json", manifest)


def mark_video_job_failed(job_dir: Path, job_id: str, exc: Exception) -> None:
    manifest = read_json_or_none(job_dir / "job_manifest.json") or {}
    manifest["status"] = "failed"
    manifest.setdefault("stages", {})["video_edit"] = "failed"
    manifest["updated_at"] = now_utc()
    manifest.setdefault("errors", []).append({"message": str(exc), "created_at": now_utc()})
    write_json_file(job_dir / "job_manifest.json", manifest)
    write_json_file(
        job_dir / "reports/video_edit_error.json",
        {
            "schema_version": "1.0",
            "stage": "video_edit",
            "status": "failed",
            "created_at": now_utc(),
            "errors": [{"message": str(exc)}],
        },
    )
    publish_pipeline_status(job_id, "vision", "failed", 0)
    publish_pipeline_log(job_id, f"[video-edit] failed: {exc}")


def mark_video_job_cancelled(job_dir: Path, job_id: str, message: str = "User cancelled this job.") -> None:
    manifest = read_json_or_none(job_dir / "job_manifest.json") or {}
    manifest["status"] = "cancelled"
    stages = manifest.setdefault("stages", {})
    for stage_name, stage_status in list(stages.items()):
        if stage_status == "running":
            stages[stage_name] = "cancelled"
    manifest["updated_at"] = now_utc()
    manifest.setdefault("events", []).append({"type": "cancelled", "message": message, "created_at": now_utc()})
    write_json_file(job_dir / "job_manifest.json", manifest)
    write_json_file(
        job_dir / "reports/cancelled.json",
        {
            "schema_version": "1.0",
            "status": "cancelled",
            "message": message,
            "created_at": now_utc(),
        },
    )
    publish_pipeline_status(job_id, "vision", "cancelled", 0)
    publish_pipeline_log(job_id, f"[video-edit] cancelled: {message}")
    clear_job_cancel_request(job_dir)


def is_job_cancel_requested(job_dir: Path) -> bool:
    return (job_dir / "reports/cancel_requested.json").exists()


def clear_job_cancel_request(job_dir: Path) -> None:
    cancel_path = job_dir / "reports/cancel_requested.json"
    if cancel_path.exists():
        cancel_path.unlink()


def drain_pipeline_event_queue(limit: int = 100) -> list[dict]:
    events = []
    for _ in range(limit):
        try:
            events.append(PIPELINE_EVENT_QUEUE.get_nowait())
        except queue.Empty:
            break
    return events


def log_event(job_id: str, message: str) -> dict:
    event = {
        "type": "log",
        "job_id": job_id,
        "message": message,
        "created_at": now_utc(),
    }
    persist_console_event(event)
    return event


def status_event(job_id: str, step: str, status: str, progress: int) -> dict:
    event = {
        "type": "status",
        "job_id": job_id,
        "step": step,
        "status": status,
        "progress": max(0, min(100, int(progress))),
        "created_at": now_utc(),
    }
    persist_console_event(event)
    return event


def sse_payload(payload: dict, event: str = "message") -> bytes:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


def latest_job_id(output_root: Path) -> str:
    jobs = list_jobs(output_root)
    if not jobs:
        return ""
    return str(jobs[0]["manifest"].get("job_id", jobs[0]["dir"].name))


def persist_console_event(event: dict) -> None:
    event_type = event.get("type")
    if event_type == "heartbeat":
        return
    payload = {
        **event,
        "message_plain": strip_ansi(str(event.get("message", ""))),
    }
    with LOG_WRITE_LOCK:
        append_ndjson(CONSOLE_LOG_PATH, payload)
        job_id = str(event.get("job_id") or "")
        if job_id:
            safe_job = sanitize_job_id(job_id)
            append_ndjson(DEFAULT_OUTPUT_ROOT / safe_job / "logs" / "console.ndjson", payload)


def append_ndjson(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def render_job_control_dashboard(output_root: Path, selected_job_id: str | None) -> str:
    initial_jobs = jobs_for_frontend(output_root)
    initial_gallery = gallery_for_frontend(output_root)
    selected_id = selected_job_id or (initial_jobs[0]["id"] if initial_jobs else "")
    template = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dump2Done Job Control Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            carbon: "#05070a",
            panel: "#0b1016",
            line: "rgba(148, 163, 184, 0.18)"
          },
          boxShadow: {
            glow: "0 0 40px rgba(56, 189, 248, 0.14)"
          }
        }
      }
    };
  </script>
  <style>
    @keyframes d2d-audio-wave {
      0%, 100% { transform: scaleY(0.32); opacity: 0.55; }
      45% { transform: scaleY(1); opacity: 1; }
    }
    .audio-wave-bar {
      animation: d2d-audio-wave 1s ease-in-out infinite;
      transform-origin: center bottom;
    }
    .audio-wave-bar:nth-child(2) { animation-delay: 0.12s; }
    .audio-wave-bar:nth-child(3) { animation-delay: 0.24s; }
    .audio-wave-bar:nth-child(4) { animation-delay: 0.36s; }
    .audio-wave-bar:nth-child(5) { animation-delay: 0.48s; }
    @keyframes d2d-flow {
      0% { transform: translateX(-40%); }
      100% { transform: translateX(120%); }
    }
    @keyframes d2d-live-card {
      0%, 100% { box-shadow: 0 0 0 rgba(56, 189, 248, 0); }
      50% { box-shadow: 0 0 34px rgba(56, 189, 248, 0.18); }
    }
    .pipeline-card-running {
      animation: d2d-live-card 1.8s ease-in-out infinite;
    }
    .pipeline-flow::after {
      content: "";
      position: absolute;
      inset: 0;
      width: 42%;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,0.55), transparent);
      animation: d2d-flow 1.15s linear infinite;
    }
  </style>
</head>
<body class="min-h-screen bg-carbon text-slate-100 antialiased">
  <div class="fixed inset-0 -z-10 bg-[radial-gradient(circle_at_18%_12%,rgba(56,189,248,0.16),transparent_30%),radial-gradient(circle_at_82%_0%,rgba(132,204,22,0.13),transparent_28%),linear-gradient(180deg,#05070a,#080b10_38%,#05070a)]"></div>

  <header class="sticky top-0 z-30 border-b border-white/10 bg-carbon/82 backdrop-blur-xl">
    <div class="mx-auto flex max-w-[1500px] items-center justify-between gap-4 px-5 py-4 lg:px-8">
      <a href="/" class="flex items-center gap-3">
        <span class="relative grid h-11 w-11 place-items-center rounded-xl border border-sky-300/30 bg-sky-400/10 shadow-glow">
          <span class="absolute h-6 w-6 rotate-45 rounded-sm border-r-4 border-t-4 border-lime-300"></span>
          <span class="absolute ml-1 h-5 w-5 rounded-sm border-b-4 border-l-4 border-sky-300"></span>
        </span>
        <span>
          <span class="block text-xl font-black tracking-normal">Dump2Done</span>
          <span class="mt-1 inline-flex items-center gap-2 rounded-full border border-lime-300/25 bg-lime-300/10 px-2.5 py-1 text-[11px] font-bold text-lime-200">
            <span class="h-1.5 w-1.5 rounded-full bg-lime-300 shadow-[0_0_12px_rgba(190,242,100,0.8)]"></span>
            <span data-i18n="localControlPlane">本機服務已啟動</span>
          </span>
        </span>
      </a>
      <nav class="flex items-center gap-2">
        <a href="/env" class="inline-flex items-center gap-2 rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm font-bold text-slate-200 hover:border-lime-300/40 hover:text-lime-200">
          <i data-lucide="activity" class="h-4 w-4"></i><span data-i18n="environment">環境診斷</span>
        </a>
        <label class="inline-flex items-center gap-2 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm font-bold text-slate-200">
          <i data-lucide="languages" class="h-4 w-4 text-lime-200"></i>
          <select id="languageSelect" class="bg-transparent text-sm font-bold outline-none">
            <option value="zh-Hant">繁中</option>
            <option value="en">English</option>
            <option value="ja">日本語</option>
          </select>
        </label>
        <button id="settingsButton" class="inline-grid h-10 w-10 place-items-center rounded-lg border border-white/10 bg-white/5 text-slate-200 hover:border-lime-300/40 hover:text-lime-200" type="button" title="Settings" aria-label="Settings">
          <i data-lucide="settings" class="h-4 w-4"></i>
        </button>
      </nav>
    </div>
  </header>

  <main class="mx-auto grid max-w-[1500px] min-w-0 gap-5 px-5 py-6 lg:grid-cols-[minmax(0,390px)_minmax(0,1fr)] lg:px-8">
    <section class="min-w-0 space-y-5">
      <article class="min-w-0 overflow-hidden rounded-xl border border-white/10 bg-panel/92 p-5 shadow-glow">
        <div class="mb-5 flex items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-orange-300">AI Media Editor</p>
            <h1 class="mt-2 text-2xl font-black" data-i18n="uploadMedia">上傳圖片或影片</h1>
          </div>
          <span id="mediaTypePill" class="rounded-lg border border-lime-300/30 bg-lime-300/10 px-3 py-1 text-xs font-black text-lime-200">Auto Detect</span>
        </div>
        <form id="mediaForm" class="grid min-w-0 gap-4">
          <div id="dropZone" class="block min-h-44 overflow-hidden rounded-xl border border-dashed border-white/15 bg-black/25 p-4 text-center hover:border-sky-300/50">
            <input id="mediaFile" name="mediaFile" type="file" accept="image/*,video/*" class="hidden" multiple>
            <span class="mb-3 block text-left text-xs font-bold text-slate-500" data-i18n="multiMediaHint">可多次追加素材；未來會支援多段影像一起拼接處理。</span>
            <span id="mediaAssetGrid" class="grid w-full min-w-0 grid-cols-2 gap-3"></span>
          </div>
          <label class="grid gap-2">
            <span class="text-sm font-bold text-slate-300" data-i18n="editPrompt">編輯描述</span>
            <textarea id="mediaPrompt" name="prompt" rows="7" class="resize-none rounded-lg border border-white/10 bg-black/30 px-3 py-3 text-sm leading-6 text-slate-100 outline-none focus:border-sky-300/70" placeholder="先描述你想完成的結果。選擇圖片會進入圖片編輯；選擇影片會啟動影片 runner。" data-i18n-placeholder="genericPromptPlaceholder"></textarea>
          </label>
          <div id="imageOptions" class="hidden grid gap-3 rounded-xl border border-lime-300/20 bg-lime-300/[0.04] p-3">
            <div class="flex items-start gap-3">
              <i data-lucide="image-plus" class="mt-0.5 h-5 w-5 text-lime-200"></i>
              <div>
                <p class="text-sm font-black text-lime-100" data-i18n="imageMode">圖片模式</p>
                <p class="mt-1 text-xs leading-5 text-slate-400" data-i18n="imageModeHelp">不需要 Profile 或解析度選單。上傳圖片、輸入指令，完成後會匯出 PNG 並在下方顯示完整路徑。</p>
              </div>
            </div>
            <label class="grid gap-2">
              <span class="flex items-center justify-between gap-3">
                <span class="text-sm font-bold text-slate-300" data-i18n="outputFolder">輸出資料夾</span>
                <button id="imageOutputSettingsButton" class="inline-grid h-8 w-8 place-items-center rounded-lg border border-white/10 bg-black/25 text-slate-300 hover:border-lime-300/45 hover:text-lime-200" type="button" title="設定輸出資料夾" aria-label="設定輸出資料夾">
                  <i data-lucide="settings" class="h-4 w-4"></i>
                </button>
              </span>
              <input id="imageOutputDirectory" name="imageOutputDirectory" value="output" class="h-11 min-w-0 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-lime-300/70">
            </label>
            <label class="grid gap-2">
              <span class="flex items-center gap-2 text-sm font-bold text-slate-300">
                <span data-i18n="imageEditProvider">生成式圖片路線</span>
                <button class="field-help grid h-5 w-5 place-items-center rounded-full border border-lime-300/35 bg-lime-300/10 text-[11px] font-black text-lime-100 hover:bg-lime-300/20" type="button" data-help-key="imageEditProviderHelp">?</button>
              </span>
              <select id="imageEditProvider" name="imageEditProvider" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-lime-300/70">
                <option value="auto" data-i18n="imageProviderAuto">Auto：濾鏡用本地，生成式自動找可用服務</option>
                <option value="local_a1111" data-i18n="imageProviderA1111">本地 Automatic1111 / Stable Diffusion</option>
                <option value="openai" data-i18n="imageProviderOpenAI">雲端 OpenAI Images API</option>
                <option value="pillow" data-i18n="imageProviderPillow">本地 Pillow 濾鏡</option>
              </select>
              <p class="text-xs leading-5 text-slate-500" data-i18n="imageEditProviderHint">「貓變狗」屬於生成式編輯，需要本地 diffusion server 或 OpenAI API key；Pillow 只做旋轉、亮度、黑白等非生成式處理。</p>
            </label>
            <div class="flex flex-wrap gap-2">
              <button class="prompt-chip rounded-lg border border-orange-300/25 bg-orange-300/10 px-3 py-2 text-xs font-black text-orange-100 hover:bg-orange-300/20" type="button" data-prompt="把貓變成狗，保留照片構圖與背景">貓變狗</button>
              <button class="prompt-chip rounded-lg border border-lime-300/25 bg-lime-300/10 px-3 py-2 text-xs font-black text-lime-100 hover:bg-lime-300/20" type="button" data-prompt="往左旋轉90度">左轉90度</button>
              <button class="prompt-chip rounded-lg border border-lime-300/25 bg-lime-300/10 px-3 py-2 text-xs font-black text-lime-100 hover:bg-lime-300/20" type="button" data-prompt="往右旋轉90度">右轉90度</button>
              <button class="prompt-chip rounded-lg border border-lime-300/25 bg-lime-300/10 px-3 py-2 text-xs font-black text-lime-100 hover:bg-lime-300/20" type="button" data-prompt="變亮一點並銳化">變亮 + 銳化</button>
              <button class="prompt-chip rounded-lg border border-lime-300/25 bg-lime-300/10 px-3 py-2 text-xs font-black text-lime-100 hover:bg-lime-300/20" type="button" data-prompt="轉成黑白">黑白</button>
            </div>
          </div>
          <div id="adaptiveOptions" class="grid gap-2 rounded-xl border border-white/10 bg-white/[0.03] p-3">
            <div class="flex items-start gap-3">
              <i data-lucide="scan-search" class="mt-0.5 h-5 w-5 text-sky-300"></i>
              <div>
                <p id="adaptiveOptionsTitle" class="text-sm font-black text-slate-200" data-i18n="adaptiveOptionsTitle">先選擇媒體，系統會切換工作流</p>
                <p id="adaptiveOptionsHelp" class="mt-1 text-xs leading-5 text-slate-500" data-i18n="adaptiveOptionsHelp">圖片會直接做本地圖片編輯並輸出；影片會啟動本地 video runner；音訊支援會在後續接 ASR/轉錄流程。</p>
              </div>
            </div>
          </div>
          <div id="videoOptions" class="hidden grid gap-3 rounded-xl border border-sky-300/15 bg-sky-300/[0.04] p-3">
            <input type="hidden" name="profile" value="configs/qualcomm_windows_arm64.yaml">
            <div class="rounded-lg border border-white/10 bg-black/25 p-3">
              <span class="flex items-center gap-2 text-sm font-bold text-slate-300">
                <span data-i18n="videoRuntimeProfile">影片本地設定</span>
                <button class="field-help grid h-5 w-5 place-items-center rounded-full border border-sky-300/35 bg-sky-300/10 text-[11px] font-black text-sky-100 hover:bg-sky-300/20" type="button" data-help-key="videoRuntimeProfileHelp">?</button>
              </span>
              <p class="mt-2 text-xs leading-5 text-slate-500" data-i18n="videoRuntimeProfileSummary">目前自動使用 Qualcomm 本地安全設定；等 profile 真的影響 runner 後才會開放選單。</p>
            </div>
            <div class="grid gap-3">
              <input type="hidden" name="modelVersion" value="local-v1">
              <label class="grid gap-2">
                <span class="flex items-center gap-2 text-sm font-bold text-slate-300">
                  <span data-i18n="videoResolution">輸出解析度</span>
                  <button class="field-help grid h-5 w-5 place-items-center rounded-full border border-sky-300/35 bg-sky-300/10 text-[11px] font-black text-sky-100 hover:bg-sky-300/20" type="button" data-help-key="videoResolutionHelp">?</button>
                </span>
              <select name="resolution" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-sky-300/70">
                <option value="original">Original</option>
                <option value="720p">720P</option>
                <option value="1080p">1080P</option>
              </select>
              </label>
            </div>
            <p id="videoOptionHelp" class="rounded-lg border border-white/10 bg-black/25 p-3 text-xs leading-5 text-slate-400" data-i18n="videoOptionHelpDefault">這些是目前會送到後端的影片設定；沒有實際功能的模型版本選單已先隱藏。</p>
          </div>
          <div id="mediaResult" class="hidden min-w-0 overflow-hidden rounded-xl border border-sky-300/20 bg-sky-300/[0.05] p-3 text-xs text-slate-300">
            <div class="grid min-w-0 gap-3">
              <div class="min-w-0">
                <p class="font-black text-sky-100" data-i18n="outputComplete">輸出完成</p>
                <p id="mediaResultPath" class="mt-1 break-all font-mono text-slate-300"></p>
              </div>
              <div class="grid grid-cols-2 gap-2">
                <a id="mediaResultLink" class="rounded-lg border border-sky-300/30 px-3 py-2 text-center font-black text-sky-100 hover:bg-sky-300/10" href="#" target="_blank" rel="noopener" data-i18n="previewOutput">預覽成品</a>
                <button id="openMediaResultFolder" class="rounded-lg border border-lime-300/30 px-3 py-2 font-black text-lime-100 hover:bg-lime-300/10" type="button" data-i18n="openFolder">開啟資料夾</button>
              </div>
            </div>
          </div>
          <button id="mediaSubmit" class="mt-2 inline-flex h-12 items-center justify-center gap-2 rounded-lg bg-lime-300 px-4 text-sm font-black text-slate-950 hover:bg-lime-200 disabled:cursor-not-allowed disabled:bg-slate-600 disabled:text-slate-300" type="submit">
            <i data-lucide="sparkles" class="h-5 w-5"></i><span data-i18n="createEdit">建立 / 編輯</span>
          </button>
          <p id="formHint" class="min-h-5 text-xs font-semibold text-slate-400"></p>
        </form>
      </article>

      <article class="min-w-0 overflow-hidden rounded-xl border border-white/10 bg-panel/92 p-5">
        <div class="mb-4 flex items-center justify-between">
          <div>
            <h2 class="text-lg font-black" data-i18n="jobQueue">任務佇列</h2>
            <p class="mt-1 text-xs leading-5 text-slate-500" data-i18n="jobQueueHelp">藍色是目前追蹤；Running 會自動排在前面。</p>
          </div>
          <button id="refreshJobs" class="rounded-lg border border-white/10 px-3 py-2 text-xs font-black text-slate-300 hover:border-sky-300/50">Refresh</button>
        </div>
        <div id="jobList" class="grid max-h-[360px] gap-2 overflow-y-auto pr-1"></div>
      </article>
    </section>

    <section class="grid min-w-0 gap-5">
      <article class="min-w-0 overflow-hidden rounded-xl border border-white/10 bg-panel/92 p-5">
        <div class="mb-5 flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="text-xs font-black uppercase tracking-[0.22em] text-sky-300" data-i18n="liveTracker">Live Pipeline Tracker</p>
            <h2 id="activeJobTitle" class="mt-2 max-w-[760px] truncate text-xl font-black md:text-2xl" data-i18n="selectJob">選擇任務</h2>
            <p id="activeJobMeta" class="mt-2 max-w-[760px] truncate text-xs font-semibold text-slate-500"></p>
          </div>
          <div class="flex flex-wrap justify-end gap-2">
            <div class="grid grid-cols-3 gap-2 text-center text-xs font-black">
              <div class="rounded-lg border border-white/10 bg-white/5 px-3 py-2"><span id="statRunning" class="block truncate text-sm text-sky-300">--</span><span data-i18n="jobState">狀態</span></div>
              <div class="rounded-lg border border-white/10 bg-white/5 px-3 py-2"><span id="statDone" class="block text-sm text-lime-300">--</span><span data-i18n="progressShort">進度</span></div>
              <div class="rounded-lg border border-white/10 bg-white/5 px-3 py-2"><span id="statQueue" class="block truncate text-sm text-orange-300">--</span><span data-i18n="currentStageShort">目前</span></div>
            </div>
            <button id="cancelActiveJob" class="hidden rounded-lg border border-red-300/35 bg-red-300/10 px-3 py-2 text-xs font-black text-red-100 hover:bg-red-300/20" type="button">
              <i data-lucide="circle-stop" class="mr-1 inline h-4 w-4"></i><span data-i18n="cancelJob">取消作業</span>
            </button>
          </div>
        </div>
        <div id="pipelineSummary" class="mb-4 hidden rounded-xl border border-sky-300/20 bg-sky-300/[0.05] p-4"></div>
        <div id="pipelineSteps" class="grid min-w-0 gap-3 md:grid-cols-2 xl:grid-cols-4"></div>
      </article>

      <article class="min-w-0 overflow-hidden rounded-xl border border-white/10 bg-panel/92 p-5">
        <div class="mb-4 flex items-center justify-between">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-lime-300" data-i18n="artifacts">Artifacts</p>
            <h2 class="mt-2 text-xl font-black" data-i18n="galleryTitle">產出物歷史畫廊</h2>
          </div>
          <span class="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs font-bold text-slate-300" data-i18n="mediaOnly">圖片 / 影片 / 聲音</span>
        </div>
        <div id="gallery" class="grid gap-4 md:grid-cols-2 xl:grid-cols-3"></div>
      </article>

      <article class="overflow-hidden rounded-xl border border-white/10 bg-[#050608]">
        <div class="flex items-center justify-between border-b border-white/10 bg-white/[0.03] px-4 py-3">
          <div class="flex items-center gap-2 text-sm font-black text-slate-300">
            <i data-lucide="square-terminal" class="h-4 w-4 text-sky-300"></i><span data-i18n="liveConsole">Live Console Log</span>
          </div>
          <button id="clearLog" class="rounded-md border border-white/10 px-2 py-1 text-xs font-bold text-slate-400 hover:text-slate-100" data-i18n="clear">Clear</button>
        </div>
        <div id="consoleLog" class="h-64 overflow-auto p-4 font-mono text-[12px] leading-6 text-lime-100" role="log" aria-live="polite"></div>
      </article>
    </section>
  </main>

  <div id="mediaModal" class="fixed inset-0 z-50 hidden bg-black/80 p-5 backdrop-blur-sm">
    <div class="mx-auto flex h-full max-w-5xl flex-col justify-center">
      <div class="rounded-xl border border-white/10 bg-[#080b10] shadow-2xl">
        <div class="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <h2 id="mediaTitle" class="truncate pr-4 text-sm font-black text-slate-100">Media Preview</h2>
          <button onclick="closeMediaModal()" class="rounded-lg border border-white/10 px-3 py-2 text-xs font-black text-slate-300 hover:border-red-300/50 hover:text-red-200">
            <i data-lucide="x" class="mr-1 inline h-3 w-3"></i><span data-i18n="close">關閉</span>
          </button>
        </div>
        <div class="p-4">
          <video id="mediaVideo" class="hidden max-h-[72vh] w-full rounded-lg bg-black" controls playsinline></video>
          <audio id="mediaAudio" class="hidden w-full" controls></audio>
          <img id="mediaImage" class="hidden max-h-[72vh] w-full rounded-lg object-contain bg-black" alt="Image preview">
        </div>
      </div>
    </div>
  </div>

  <div id="settingsModal" class="fixed inset-0 z-50 hidden bg-black/80 p-5 backdrop-blur-sm">
    <div class="mx-auto flex min-h-full max-w-3xl items-center justify-center">
      <div class="max-h-[92vh] w-full overflow-y-auto rounded-xl border border-white/10 bg-[#080b10] shadow-2xl">
        <div class="flex items-center justify-between border-b border-white/10 px-5 py-4">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-lime-300" data-i18n="settingsEyebrow">Settings</p>
            <h2 class="mt-1 text-xl font-black" data-i18n="settingsTitle">偏好設定</h2>
          </div>
          <button id="closeSettings" class="rounded-lg border border-white/10 px-3 py-2 text-xs font-black text-slate-300 hover:border-red-300/50 hover:text-red-200" type="button">
            <i data-lucide="x" class="mr-1 inline h-3 w-3"></i><span data-i18n="close">關閉</span>
          </button>
        </div>
        <form id="settingsForm" class="grid gap-4 p-5">
          <label class="grid gap-2">
            <span class="text-sm font-bold text-slate-300" data-i18n="defaultOutputFolder">預設輸出資料夾</span>
            <input id="defaultOutputDirectory" class="h-11 min-w-0 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-lime-300/70">
            <span class="text-xs leading-5 text-slate-500" data-i18n="defaultOutputHelp">所有輸出預設會落在這個資料夾。為了安全，目前限定在專案 output 目錄底下。</span>
          </label>
          <label class="flex items-start gap-3 rounded-lg border border-white/10 bg-white/[0.03] p-3">
            <input id="autoPreviewOutput" type="checkbox" class="mt-1 h-4 w-4 accent-lime-300">
            <span>
              <span class="block text-sm font-black text-slate-200" data-i18n="autoPreviewOutput">輸出後自動預覽成品</span>
              <span class="mt-1 block text-xs leading-5 text-slate-500" data-i18n="autoPreviewHelp">圖片完成後自動開新分頁預覽輸出檔。</span>
            </span>
          </label>
          <label class="flex items-start gap-3 rounded-lg border border-white/10 bg-white/[0.03] p-3">
            <input id="autoOpenOutputFolder" type="checkbox" class="mt-1 h-4 w-4 accent-lime-300">
            <span>
              <span class="block text-sm font-black text-slate-200" data-i18n="autoOpenOutputFolder">輸出後自動開啟資料夾</span>
              <span class="mt-1 block text-xs leading-5 text-slate-500" data-i18n="autoOpenHelp">適合批次確認檔案，但如果常常輸出會比較打擾。</span>
            </span>
          </label>
          <label class="grid gap-2">
            <span class="text-sm font-bold text-slate-300" data-i18n="galleryDensity">畫廊密度</span>
            <select id="galleryDensity" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-sky-300/70">
              <option value="comfortable" data-i18n="densityComfortable">舒適</option>
              <option value="compact" data-i18n="densityCompact">緊湊</option>
            </select>
          </label>
          <section class="grid gap-4 rounded-xl border border-sky-300/20 bg-sky-300/[0.04] p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div class="flex flex-wrap items-center gap-2">
                  <h3 class="text-sm font-black text-sky-100" data-i18n="localComputeTitle">本地運算資源中心</h3>
                  <span class="rounded-md border border-orange-300/35 bg-orange-300/10 px-2 py-1 text-[11px] font-black uppercase text-orange-200" data-i18n="demoBadge">Demo</span>
                </div>
                <p class="mt-2 text-xs leading-5 text-slate-400" data-i18n="localComputeHelp">這一區先是演示設定，用來規劃未來串接本地 LLM、影像模型、語音模型與線上 fallback。</p>
              </div>
              <i data-lucide="cpu" class="h-5 w-5 text-lime-300"></i>
            </div>
            <div class="rounded-lg border border-orange-300/25 bg-orange-300/[0.06] p-3">
              <p class="text-xs font-black text-orange-200" data-i18n="onlineFeatureNoticeTitle">目前仍偏線上的能力</p>
              <p class="mt-2 text-xs leading-5 text-slate-400" data-i18n="onlineFeatureNoticeBody">高品質生成式影片改衣服、精準人物/衣服 segmentation + tracking、複雜 Agent 規劃目前先標為線上能力；未來會逐步改成本地 Ollama / ONNX / DirectML / QNN 能跑的模組。</p>
            </div>
            <div id="computeHelpPanel" class="rounded-lg border border-white/10 bg-black/30 p-3">
              <p class="text-xs font-black text-lime-200" data-i18n="computeHelpPanelTitle">設定說明</p>
              <p id="computeHelpText" class="mt-2 text-xs leading-5 text-slate-400" data-i18n="computeHelpDefault">點擊欄位標題旁的問號，可以查看各項本地/線上運算設定的差別與未來用途。</p>
            </div>
            <div class="grid gap-3 md:grid-cols-2">
              <label class="grid gap-2">
                <span class="flex items-center gap-2 text-xs font-black uppercase tracking-wide text-slate-400">
                  <span data-i18n="localLlmProvider">Local LLM Provider</span>
                  <button class="compute-help grid h-5 w-5 place-items-center rounded-full border border-sky-300/35 bg-sky-300/10 text-[11px] font-black text-sky-100 hover:bg-sky-300/20" type="button" data-help-key="localLlmProviderHelp">?</button>
                </span>
                <select id="localLlmProvider" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-sky-300/70">
                  <option value="ollama_demo">Ollama local (demo)</option>
                  <option value="openai_online_placeholder">OpenAI online fallback (placeholder)</option>
                  <option value="none">None</option>
                </select>
              </label>
              <label class="grid gap-2">
                <span class="flex items-center gap-2 text-xs font-black uppercase tracking-wide text-slate-400">
                  <span data-i18n="localLlmEndpoint">Local LLM Endpoint</span>
                  <button class="compute-help grid h-5 w-5 place-items-center rounded-full border border-sky-300/35 bg-sky-300/10 text-[11px] font-black text-sky-100 hover:bg-sky-300/20" type="button" data-help-key="localLlmEndpointHelp">?</button>
                </span>
                <input id="localLlmEndpoint" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-sky-300/70" placeholder="http://127.0.0.1:11434">
              </label>
              <label class="grid gap-2">
                <span class="flex items-center gap-2 text-xs font-black uppercase tracking-wide text-slate-400">
                  <span data-i18n="visionProvider">Vision / Video Provider</span>
                  <button class="compute-help grid h-5 w-5 place-items-center rounded-full border border-sky-300/35 bg-sky-300/10 text-[11px] font-black text-sky-100 hover:bg-sky-300/20" type="button" data-help-key="visionProviderHelp">?</button>
                </span>
                <select id="visionProvider" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-sky-300/70">
                  <option value="local_pillow_mvp">Local Pillow / FFmpeg MVP</option>
                  <option value="onnx_directml_future">ONNX DirectML local future</option>
                  <option value="qnn_future">Qualcomm QNN local future</option>
                  <option value="online_video_edit_placeholder">Online video edit placeholder</option>
                </select>
              </label>
              <label class="grid gap-2">
                <span class="flex items-center gap-2 text-xs font-black uppercase tracking-wide text-slate-400">
                  <span data-i18n="automatic1111Endpoint">Automatic1111 Endpoint</span>
                  <button class="compute-help grid h-5 w-5 place-items-center rounded-full border border-lime-300/35 bg-lime-300/10 text-[11px] font-black text-lime-100 hover:bg-lime-300/20" type="button" data-help-key="automatic1111EndpointHelp">?</button>
                </span>
                <input id="automatic1111Endpoint" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-lime-300/70" placeholder="http://127.0.0.1:7860">
              </label>
              <label class="grid gap-2">
                <span class="flex items-center gap-2 text-xs font-black uppercase tracking-wide text-slate-400">
                  <span data-i18n="openaiImageModel">OpenAI Image Model</span>
                  <button class="compute-help grid h-5 w-5 place-items-center rounded-full border border-orange-300/35 bg-orange-300/10 text-[11px] font-black text-orange-100 hover:bg-orange-300/20" type="button" data-help-key="openaiImageModelHelp">?</button>
                </span>
                <input id="openaiImageModel" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-orange-300/70" placeholder="gpt-image-1.5">
              </label>
              <label class="grid gap-2">
                <span class="flex items-center gap-2 text-xs font-black uppercase tracking-wide text-slate-400">
                  <span data-i18n="asrProvider">Speech / ASR Provider</span>
                  <button class="compute-help grid h-5 w-5 place-items-center rounded-full border border-sky-300/35 bg-sky-300/10 text-[11px] font-black text-sky-100 hover:bg-sky-300/20" type="button" data-help-key="asrProviderHelp">?</button>
                </span>
                <select id="asrProvider" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-sky-300/70">
                  <option value="faster_whisper_cpu_demo">Faster-Whisper CPU local</option>
                  <option value="onnx_asr_future">ONNX ASR local future</option>
                  <option value="online_asr_placeholder">Online ASR placeholder</option>
                </select>
              </label>
            </div>
            <label class="grid gap-2">
              <span class="flex items-center gap-2 text-xs font-black uppercase tracking-wide text-slate-400">
                <span data-i18n="onlineFallbackPolicy">Online Fallback Policy</span>
                <button class="compute-help grid h-5 w-5 place-items-center rounded-full border border-orange-300/35 bg-orange-300/10 text-[11px] font-black text-orange-100 hover:bg-orange-300/20" type="button" data-help-key="onlineFallbackPolicyHelp">?</button>
              </span>
              <select id="onlineFallbackPolicy" class="h-11 rounded-lg border border-white/10 bg-black/40 px-3 text-sm text-slate-100 outline-none focus:border-orange-300/70">
                <option value="warn_only" data-i18n="fallbackWarnOnly">只提示，不自動送線上</option>
                <option value="disabled" data-i18n="fallbackDisabled">完全關閉線上 fallback</option>
                <option value="ask_each_time" data-i18n="fallbackAskEachTime">每次詢問後才使用線上</option>
              </select>
              <span class="text-xs leading-5 text-slate-500" data-i18n="onlineFallbackHelp">演示設定：目前不會真的呼叫線上服務，只用來規劃未來任務路由。</span>
            </label>
          </section>
          <button id="openDefaultOutputFolder" class="rounded-lg border border-sky-300/30 px-4 py-3 text-sm font-black text-sky-100 hover:bg-sky-300/10" type="button">
            <i data-lucide="folder-open" class="mr-1 inline h-4 w-4"></i><span data-i18n="openDefaultOutputFolder">開啟預設資料夾</span>
          </button>
          <div class="grid grid-cols-2 gap-3 pt-2">
            <button id="resetSettings" class="rounded-lg border border-white/10 px-4 py-3 text-sm font-black text-slate-300 hover:border-orange-300/50 hover:text-orange-200" type="button" data-i18n="resetSettings">重設</button>
            <button class="rounded-lg bg-lime-300 px-4 py-3 text-sm font-black text-slate-950 hover:bg-lime-200" type="submit" data-i18n="saveSettings">儲存設定</button>
          </div>
          <p id="settingsHint" class="min-h-5 text-xs font-semibold text-lime-200"></p>
        </form>
      </div>
    </div>
  </div>

  <script>
    const INITIAL_JOBS = __INITIAL_JOBS__;
    const INITIAL_GALLERY = __INITIAL_GALLERY__;
    const SELECTED_JOB_ID = "__SELECTED_JOB_ID__";
    let jobs = [...INITIAL_JOBS];
    let gallery = [...INITIAL_GALLERY].filter(isMediaGalleryItem);
    let activeJobId = SELECTED_JOB_ID || chooseDefaultJobId(jobs);
    let eventSource = null;
    let selectedMediaItems = [];
    let mediaItemSerial = 0;
    let pendingDeleteId = "";
    let activeComputeHelpKey = "";
    let activeVideoOptionHelpKey = "";
    const logLines = [
      "[dashboard] Booted Dump2Done local control plane on http://127.0.0.1:8765/",
      "[runner] Qualcomm profile loaded: CPU int8, single worker, ffmpeg libx264",
      "[asr] faster-whisper model=small device=cpu compute_type=int8",
      "[llm] Ollama structured output pending for semantic clip selection",
      "[sse] EventSource will attach to /api/stream-logs after first render"
    ];

    const phaseIcon = { completed: "check", running: "loader-circle", waiting: "circle", failed: "circle-alert", cancelled: "circle-stop" };
    const phaseTone = {
      completed: "border-lime-300/35 bg-lime-300/10 text-lime-100",
      running: "border-sky-300/40 bg-sky-300/10 text-sky-100",
      waiting: "border-white/10 bg-white/[0.03] text-slate-300",
      failed: "border-red-300/45 bg-red-300/10 text-red-100",
      cancelled: "border-orange-300/45 bg-orange-300/10 text-orange-100"
    };
    const I18N = {
      "zh-Hant": {
        localControlPlane: "本機服務已啟動",
        dashboard: "控制台",
        environment: "環境診斷",
        uploadMedia: "上傳圖片或影片",
        uploadMediaPrompt: "上傳想要編輯的圖片或影片",
        autoDetectHint: "系統會自動判斷 image / video",
        multiMediaHint: "可多次追加素材；未來會支援多段影像一起拼接處理。",
        addAnotherMedia: "新增素材",
        mixedMedia: "多素材",
        clearMediaSelection: "移除",
        mediaSelectionCleared: "已移除選擇的檔案，可重新上傳。",
        editPrompt: "編輯描述",
        imagePromptPlaceholder: "描述想做的圖片編輯，例如：往左旋轉90度、變亮一點、轉成黑白、銳化。",
        videoPromptPlaceholder: "描述影片編輯需求，例如：把衣服換成白色、轉成直式、保留原音訊。",
        genericPromptPlaceholder: "先描述你想完成的結果。選擇圖片會進入圖片編輯；選擇影片會啟動影片 runner。",
        imageMode: "圖片模式",
        imageModeHelp: "不需要 Profile 或解析度選單。上傳圖片、輸入指令，完成後會匯出 PNG 並在下方顯示完整路徑。",
        imageEditProvider: "生成式圖片路線",
        imageEditProviderHint: "「貓變狗」屬於生成式編輯，需要本地 diffusion server 或 OpenAI API key；Pillow 只做旋轉、亮度、黑白等非生成式處理。",
        imageProviderAuto: "Auto：濾鏡用本地，生成式自動找可用服務",
        imageProviderA1111: "本地 Automatic1111 / Stable Diffusion",
        imageProviderOpenAI: "雲端 OpenAI Images API",
        imageProviderPillow: "本地 Pillow 濾鏡",
        imageEditProviderHelp: "圖片分兩種：旋轉、亮度、黑白這類確定性操作會用 Pillow 本地完成；貓變狗、替換物件、生成新內容需要 diffusion 或 OpenAI 這類生成式模型。Auto 會先找本地 Automatic1111，沒有才依 fallback 設定考慮 OpenAI。",
        outputFolder: "輸出資料夾",
        outputComplete: "輸出完成",
        previewOutput: "預覽成品",
        openFolder: "開啟資料夾",
        createEdit: "建立 / 編輯",
        createImage: "建立圖片輸出",
        createVideo: "建立影片剪輯任務",
        jobQueue: "任務佇列",
        jobQueueHelp: "藍色是目前追蹤；Running 會自動排在前面。",
        activeTracking: "目前追蹤",
        jobState: "狀態",
        progressShort: "進度",
        currentStageShort: "目前",
        noActiveJob: "閒置",
        idleTitle: "目前沒有執行中的任務",
        idleMeta: "建立新任務後會在這裡顯示即時進度；也可以從任務佇列點選歷史任務查看細節。",
        idleCurrent: "等待新任務",
        idleProgress: "--",
        runnerInterrupted: "runner 已中斷",
        restartRequired: "需要重新建立或重跑任務",
        statusRunning: "執行中",
        statusCompleted: "完成",
        statusQueued: "等待中",
        statusFailed: "失敗",
        statusCancelled: "已取消",
        statusCancelling: "取消中",
        statusInterrupted: "已中斷",
        statusIncomplete: "資料不足",
        currentStep: "目前階段",
        nextStep: "下一步",
        overallProgress: "總進度",
        waitingForRunner: "等待 runner 接手",
        noNextStep: "沒有下一步",
        cancelJob: "取消作業",
        cancelRequested: "已送出取消請求，runner 會在安全檢查點停止。",
        cancelFailed: "取消失敗",
        selectJob: "選擇任務",
        galleryTitle: "產出物歷史畫廊",
        mediaOnly: "圖片 / 影片 / 聲音",
        liveTracker: "即時工作流追蹤",
        running: "執行中",
        done: "完成",
        queue: "佇列",
        artifacts: "Artifacts",
        liveConsole: "即時 Console Log",
        clear: "Clear",
        close: "關閉",
        noJobs: "尚無真實任務。建立新任務後會出現在這裡。",
        noVideoPath: "沒有影片路徑",
        unknownProfile: "未知設定檔",
        noTaskFlow: "尚未選擇任務。建立新任務後會顯示即時流程；點選左側任務可查看歷史狀態。",
        emptyGalleryTitle: "目前沒有可操作的真實產出物",
        emptyGalleryHelp: "這裡只顯示真正產出的圖片、影片或聲音檔。JSON 報告與原始上傳素材會留在 job 資料夾內供除錯，不放進畫廊。",
        created: "Created",
        resolution: "Resolution",
        previewImage: "預覽圖片",
        playVideo: "播放影片",
        playAudio: "播放音訊",
        imageLabel: "image",
        videoLabel: "video",
        audioLabel: "audio",
        audioHint: "點擊播放音訊",
        unavailablePreview: "不可預覽",
        deleteArtifact: "刪除",
        confirmDelete: "確認刪除",
        cancelDelete: "取消",
        deletePrompt: "確定移除這個產出物？檔案會移到 output/trash。",
        deleteMoved: "已移到 output/trash。",
        noFile: "請先選擇圖片或影片。",
        unsupportedMediaType: "目前這個編輯器只支援圖片與影片素材。",
        mediaLimitReached: "最多可先放 12 個素材，超過的檔案已略過。",
        batchCreated: "已建立批次任務數：",
        readingFile: "讀取檔案中...",
        processing: "上傳並處理中...",
        imageReadyHint: "圖片會立即以本地 Pillow 編輯並匯出到指定資料夾。",
        videoReadyHint: "影片會啟動本地編輯 runner，完成後自動回到畫廊。",
        adaptiveOptionsTitle: "先選擇媒體，系統會切換工作流",
        adaptiveOptionsHelp: "圖片會直接做本地圖片編輯並輸出；影片會啟動本地 video runner；音訊支援會在後續接 ASR/轉錄流程。",
        imageWorkflowTitle: "圖片工作流",
        imageWorkflowHelp: "圖片會使用本地 Pillow 編輯，適合旋轉、變亮、黑白、銳化等快速輸出，不需要 Profile 或解析度選單。",
        videoWorkflowTitle: "影片工作流",
        videoWorkflowHelp: "影片會啟動本地 video runner，先做 FFmpeg 分析、逐幀處理與 MP4 輸出。較複雜的生成式改衣服未來會接 segmentation/tracking 或線上 fallback。",
        videoProfile: "影片 Profile",
        videoRuntimeProfile: "影片本地設定",
        videoRuntimeProfileSummary: "目前自動使用 Qualcomm 本地安全設定；等 profile 真的影響 runner 後才會開放選單。",
        videoResolution: "輸出解析度",
        videoOptionHelpDefault: "這裡只保留目前會影響輸出的影片設定；尚未改變 runner 行為的控制項已先隱藏。",
        videoProfileHelp: "Profile 會決定後端採用哪份執行設定。目前 Qualcomm 裝置預設使用 qualcomm_windows_arm64.yaml，代表先走 CPU / FFmpeg 的穩定本地路線，避免假設 CUDA 或 NVENC。",
        videoRuntimeProfileHelp: "這不是可切換功能，而是目前後端固定採用的安全執行路線。因為你的機器是 Qualcomm 平台，系統先使用 qualcomm_windows_arm64.yaml 來避免 CUDA/NVENC 假設；等 profile 真的會改變模型、加速器或 runner 策略後，才會開放下拉選單。",
        videoResolutionHelp: "輸出解析度會影響 runner 抽幀與輸出尺寸。Original 盡量保留原始尺寸；720P / 1080P 會在處理前縮放，速度較快、檔案較小，也比較適合目前本地 MVP。",
        autoDetect: "Auto Detect",
        imageEdit: "Image Edit",
        videoPipeline: "Video Pipeline",
        unknown: "Unknown",
        settingsEyebrow: "Settings",
        settingsTitle: "偏好設定",
        defaultOutputFolder: "預設輸出資料夾",
        defaultOutputHelp: "所有輸出預設會落在這個資料夾。為了安全，目前限定在專案 output 目錄底下。",
        autoPreviewOutput: "輸出後自動預覽成品",
        autoPreviewHelp: "圖片完成後自動開新分頁預覽輸出檔。",
        autoOpenOutputFolder: "輸出後自動開啟資料夾",
        autoOpenHelp: "適合批次確認檔案，但如果常常輸出會比較打擾。",
        galleryDensity: "畫廊密度",
        densityComfortable: "舒適",
        densityCompact: "緊湊",
        resetSettings: "重設",
        saveSettings: "儲存設定",
        openDefaultOutputFolder: "開啟預設資料夾",
        settingsSaved: "設定已儲存。",
        settingsReset: "設定已重設。",
        settingsButtonLabel: "偏好設定",
        imageOutputSettingsLabel: "設定圖片輸出資料夾",
        localComputeTitle: "本地運算資源中心",
        demoBadge: "演示",
        localComputeHelp: "這一區先是演示設定，用來規劃未來串接本地 LLM、影像模型、語音模型與線上 fallback。",
        onlineFeatureNoticeTitle: "目前仍偏線上的能力",
        onlineFeatureNoticeBody: "高品質生成式影片改衣服、精準人物/衣服 segmentation + tracking、複雜 Agent 規劃目前先標為線上能力；未來會逐步改成本地 Ollama / ONNX / DirectML / QNN 能跑的模組。",
        localLlmProvider: "Local LLM Provider",
        localLlmEndpoint: "Local LLM Endpoint",
        visionProvider: "Vision / Video Provider",
        automatic1111Endpoint: "Automatic1111 Endpoint",
        openaiImageModel: "OpenAI Image Model",
        asrProvider: "Speech / ASR Provider",
        onlineFallbackPolicy: "Online Fallback Policy",
        fallbackWarnOnly: "只提示，不自動送線上",
        fallbackDisabled: "完全關閉線上 fallback",
        fallbackAskEachTime: "每次詢問後才使用線上",
        onlineFallbackHelp: "演示設定：目前不會真的呼叫線上服務，只用來規劃未來任務路由。",
        computeHelpPanelTitle: "設定說明",
        computeHelpDefault: "點擊欄位標題旁的問號，可以查看各項本地/線上運算設定的差別與未來用途。",
        helpButtonLabel: "查看此設定說明",
        localLlmProviderHelp: "決定文字理解、任務規劃、prompt 解析要交給誰。Ollama local 代表未來走本機 LLM；OpenAI online fallback 代表遇到本機模型做不到的語意規劃時，先提示再考慮線上；None 則關閉這類 LLM 路由。目前是演示，不會真的呼叫模型。",
        localLlmEndpointHelp: "本地 LLM 服務網址，例如 Ollama 預設是 http://127.0.0.1:11434。未來後端會用它檢查模型列表、送出結構化 prompt、取得剪輯或編輯計畫。現在只保存設定，不會主動連線。",
        visionProviderHelp: "決定圖片/影片視覺處理走哪條路。Local Pillow / FFmpeg MVP 是目前已能本地輸出的基礎版；ONNX DirectML 與 Qualcomm QNN 是未來把 segmentation、tracking、影像模型搬到本機 GPU/NPU 的路線；Online video edit placeholder 代表高品質生成式影片編輯目前仍偏線上。",
        automatic1111EndpointHelp: "本地 Stable Diffusion WebUI API 位址。若你啟動 AUTOMATIC1111 並開啟 --api，Dump2Done 會用 /sdapi/v1/img2img 做貓變狗、物件替換等 image-to-image。這是目前最實際的本地生成式圖片路線。",
        openaiImageModelHelp: "OpenAI Images API 的模型名稱。需要環境變數 OPENAI_API_KEY；ChatGPT Pro 不會自動等於 API key。雲端路線適合本機 diffusion 尚未部署時使用。",
        asrProviderHelp: "決定語音辨識來源。Faster-Whisper CPU local 是目前可在本機跑的路線；ONNX ASR future 是未來優化到本地加速後端；Online ASR placeholder 代表雲端備援。差別在速度、隱私、模型品質與硬體需求。",
        onlineFallbackPolicyHelp: "控制遇到本地資源做不到時是否可使用線上服務。只提示代表系統只告知需要線上能力，不會自動送出；完全關閉代表永遠不走線上；每次詢問代表未來需要你確認後才會送出。"
      },
      en: {
        localControlPlane: "Local service ready",
        dashboard: "Dashboard",
        environment: "Environment",
        uploadMedia: "Upload Image or Video",
        uploadMediaPrompt: "Upload an image or video to edit",
        autoDetectHint: "The system will detect image / video automatically",
        multiMediaHint: "You can add assets multiple times. Future versions will stitch multiple clips into one output.",
        addAnotherMedia: "Add Media",
        mixedMedia: "Multi Media",
        clearMediaSelection: "Remove",
        mediaSelectionCleared: "Removed the selected file. You can upload another one.",
        editPrompt: "Edit prompt",
        imagePromptPlaceholder: "Describe the image edit, e.g. rotate left 90 degrees, brighten, grayscale, sharpen.",
        videoPromptPlaceholder: "Describe the video edit, e.g. change clothing to white, make it vertical, keep original audio.",
        genericPromptPlaceholder: "Describe the result you want. Images use image edit mode; videos start the video runner.",
        imageMode: "Image Mode",
        imageModeHelp: "No profile or resolution menu is needed. Upload an image, enter a prompt, and the PNG output path will appear below.",
        imageEditProvider: "Generative image route",
        imageEditProviderHint: "Cat-to-dog is generative editing. It needs a local diffusion server or an OpenAI API key. Pillow only handles rotation, brightness, grayscale, and similar deterministic edits.",
        imageProviderAuto: "Auto: local filters, then available generative service",
        imageProviderA1111: "Local Automatic1111 / Stable Diffusion",
        imageProviderOpenAI: "Cloud OpenAI Images API",
        imageProviderPillow: "Local Pillow filters",
        imageEditProviderHelp: "There are two image paths: deterministic operations such as rotate, brightness, and grayscale run locally with Pillow; cat-to-dog, object replacement, and new visual content need a generative model such as local diffusion or OpenAI. Auto tries local Automatic1111 first, then OpenAI depending on fallback settings.",
        outputFolder: "Output Folder",
        outputComplete: "Output Ready",
        previewOutput: "Preview Output",
        openFolder: "Open Folder",
        createEdit: "Create / Edit",
        createImage: "Create Image Output",
        createVideo: "Create Video Job",
        jobQueue: "Job Queue",
        jobQueueHelp: "Blue is the tracked job. Running jobs are shown first.",
        activeTracking: "Tracking",
        jobState: "State",
        progressShort: "Progress",
        currentStageShort: "Now",
        noActiveJob: "Idle",
        idleTitle: "No job is running",
        idleMeta: "Create a new job to see live progress here, or select a history item from the queue.",
        idleCurrent: "Waiting",
        idleProgress: "--",
        runnerInterrupted: "Runner interrupted",
        restartRequired: "Create or rerun this job",
        statusRunning: "Running",
        statusCompleted: "Done",
        statusQueued: "Queued",
        statusFailed: "Failed",
        statusCancelled: "Cancelled",
        statusCancelling: "Cancelling",
        statusInterrupted: "Interrupted",
        statusIncomplete: "Incomplete",
        currentStep: "Current step",
        nextStep: "Next step",
        overallProgress: "Overall progress",
        waitingForRunner: "Waiting for runner handoff",
        noNextStep: "No next step",
        cancelJob: "Cancel Job",
        cancelRequested: "Cancel requested. The runner will stop at the next safe checkpoint.",
        cancelFailed: "Cancel failed",
        selectJob: "Select Job",
        galleryTitle: "Output Gallery",
        mediaOnly: "Images / Videos / Audio",
        liveTracker: "Live Pipeline Tracker",
        running: "Running",
        done: "Done",
        queue: "Queue",
        artifacts: "Artifacts",
        liveConsole: "Live Console Log",
        clear: "Clear",
        close: "Close",
        noJobs: "No real jobs yet. New jobs will appear here.",
        noVideoPath: "No video path",
        unknownProfile: "unknown profile",
        noTaskFlow: "No job is selected. Create a new job to see live progress, or select a queued/history item.",
        emptyGalleryTitle: "No playable outputs yet",
        emptyGalleryHelp: "Only produced images, videos, or audio files appear here. JSON reports and original uploads stay in the job folder for debugging.",
        created: "Created",
        resolution: "Resolution",
        previewImage: "Preview Image",
        playVideo: "Play Video",
        playAudio: "Play Audio",
        imageLabel: "image",
        videoLabel: "video",
        audioLabel: "audio",
        audioHint: "Click to play audio",
        unavailablePreview: "Unavailable",
        deleteArtifact: "Delete",
        confirmDelete: "Confirm Delete",
        cancelDelete: "Cancel",
        deletePrompt: "Remove this output? The file will be moved to output/trash.",
        deleteMoved: "Moved to output/trash.",
        noFile: "Choose an image or video first.",
        unsupportedMediaType: "This editor currently supports image and video assets only.",
        mediaLimitReached: "Up to 12 assets can be staged for now. Extra files were skipped.",
        batchCreated: "Batch jobs created:",
        readingFile: "Reading file...",
        processing: "Uploading and processing...",
        imageReadyHint: "Images are edited locally with Pillow and exported to the selected folder.",
        videoReadyHint: "Videos start the local edit runner and return to the gallery when finished.",
        adaptiveOptionsTitle: "Choose media first; the workflow will adapt",
        adaptiveOptionsHelp: "Images are edited locally and exported. Videos start the local video runner. Audio will later route into ASR/transcription.",
        imageWorkflowTitle: "Image Workflow",
        imageWorkflowHelp: "Images use local Pillow editing for rotation, brightness, grayscale, sharpening, and fast export. No profile or resolution menu is needed.",
        videoWorkflowTitle: "Video Workflow",
        videoWorkflowHelp: "Videos start the local video runner: FFmpeg analysis, frame processing, and MP4 output. Higher-quality generative clothing edits will later use segmentation/tracking or online fallback.",
        videoProfile: "Video Profile",
        videoRuntimeProfile: "Local Video Runtime",
        videoRuntimeProfileSummary: "Currently auto-uses the Qualcomm-safe local profile. The selector stays hidden until profiles change real runner behavior.",
        videoResolution: "Output Resolution",
        videoOptionHelpDefault: "Only video settings that affect output remain visible. Controls that do not change runner behavior are hidden for now.",
        videoProfileHelp: "Profile selects the backend execution config. On this Qualcomm device, qualcomm_windows_arm64.yaml means a stable local CPU / FFmpeg route without assuming CUDA or NVENC.",
        videoRuntimeProfileHelp: "This is not a user-switchable feature yet. The backend currently locks to the Qualcomm-safe route, qualcomm_windows_arm64.yaml, to avoid CUDA/NVENC assumptions. The dropdown should return only after profiles change models, accelerators, or runner strategy.",
        videoResolutionHelp: "Output resolution affects frame extraction and render size. Original keeps source dimensions where possible; 720P / 1080P scale before processing for faster local MVP runs and smaller files.",
        autoDetect: "Auto Detect",
        imageEdit: "Image Edit",
        videoPipeline: "Video Pipeline",
        unknown: "Unknown",
        settingsEyebrow: "Settings",
        settingsTitle: "Preferences",
        defaultOutputFolder: "Default Output Folder",
        defaultOutputHelp: "All outputs use this folder by default. For safety, it is currently limited to the project output folder.",
        autoPreviewOutput: "Auto-preview output after export",
        autoPreviewHelp: "Open the finished image in a new tab after export.",
        autoOpenOutputFolder: "Auto-open folder after export",
        autoOpenHelp: "Useful for batch checking files, but can be noisy when exporting often.",
        galleryDensity: "Gallery Density",
        densityComfortable: "Comfortable",
        densityCompact: "Compact",
        resetSettings: "Reset",
        saveSettings: "Save Settings",
        openDefaultOutputFolder: "Open Default Folder",
        settingsSaved: "Settings saved.",
        settingsReset: "Settings reset.",
        settingsButtonLabel: "Preferences",
        imageOutputSettingsLabel: "Set image output folder",
        localComputeTitle: "Local Compute Resource Center",
        demoBadge: "Demo",
        localComputeHelp: "Demo-only settings for future routing to local LLMs, vision models, speech models, and online fallback.",
        onlineFeatureNoticeTitle: "Capabilities still leaning online",
        onlineFeatureNoticeBody: "High-quality generative video recoloring, precise person/clothing segmentation + tracking, and complex Agent planning are marked as online capabilities for now; the roadmap is local Ollama / ONNX / DirectML / QNN modules.",
        localLlmProvider: "Local LLM Provider",
        localLlmEndpoint: "Local LLM Endpoint",
        visionProvider: "Vision / Video Provider",
        automatic1111Endpoint: "Automatic1111 Endpoint",
        openaiImageModel: "OpenAI Image Model",
        asrProvider: "Speech / ASR Provider",
        onlineFallbackPolicy: "Online Fallback Policy",
        fallbackWarnOnly: "Warn only, never auto-send online",
        fallbackDisabled: "Disable online fallback",
        fallbackAskEachTime: "Ask every time before online use",
        onlineFallbackHelp: "Demo setting: no online service is called yet. This only shapes future task routing.",
        computeHelpPanelTitle: "Setting Help",
        computeHelpDefault: "Click the question mark next to a label to see what each local/online compute setting means.",
        helpButtonLabel: "Show help for this setting",
        localLlmProviderHelp: "Chooses who handles text reasoning, task planning, and prompt parsing. Ollama local means a future local LLM route; OpenAI online fallback means online help can be suggested when local planning is not enough; None disables this LLM route. This is demo-only for now.",
        localLlmEndpointHelp: "The local LLM service URL. Ollama commonly uses http://127.0.0.1:11434. Later the backend can use this to inspect models, send structured prompts, and receive edit plans. For now it is only saved.",
        visionProviderHelp: "Chooses the image/video processing route. Local Pillow / FFmpeg MVP is the current local output path. ONNX DirectML and Qualcomm QNN are future local GPU/NPU acceleration paths for segmentation, tracking, and vision models. Online video edit placeholder means high-quality generative video editing is still online-leaning.",
        automatic1111EndpointHelp: "Local Stable Diffusion WebUI API URL. If AUTOMATIC1111 is running with --api, Dump2Done calls /sdapi/v1/img2img for cat-to-dog, object replacement, and other image-to-image edits. This is the most practical local generative image route right now.",
        openaiImageModelHelp: "OpenAI Images API model name. Requires OPENAI_API_KEY in the environment; ChatGPT Pro does not automatically provide an API key. The cloud route is useful when local diffusion is not deployed yet.",
        asrProviderHelp: "Chooses the speech recognition route. Faster-Whisper CPU local is the current local option; ONNX ASR future is a future accelerated local backend; Online ASR placeholder is cloud fallback. The tradeoffs are speed, privacy, quality, and hardware needs.",
        onlineFallbackPolicyHelp: "Controls whether online services may be used when local resources cannot complete a task. Warn only never auto-sends; disabled blocks online fallback; ask each time means future online use requires your confirmation."
      },
      ja: {
        localControlPlane: "ローカルサービス稼働中",
        dashboard: "ダッシュボード",
        environment: "環境診断",
        uploadMedia: "画像または動画をアップロード",
        uploadMediaPrompt: "編集したい画像または動画をアップロード",
        autoDetectHint: "画像 / 動画を自動判定します",
        multiMediaHint: "素材は何度でも追加できます。将来は複数クリップを1本に結合します。",
        addAnotherMedia: "素材を追加",
        mixedMedia: "複数素材",
        clearMediaSelection: "削除",
        mediaSelectionCleared: "選択したファイルを削除しました。別のファイルをアップロードできます。",
        editPrompt: "編集プロンプト",
        imagePromptPlaceholder: "画像編集を入力します。例：左に90度回転、明るくする、白黒化、シャープ化。",
        videoPromptPlaceholder: "動画編集を入力します。例：服を白にする、縦型にする、元音声を保持する。",
        genericPromptPlaceholder: "完成したい結果を入力してください。画像は画像編集、動画は video runner に切り替わります。",
        imageMode: "画像モード",
        imageModeHelp: "Profile や解像度メニューは不要です。画像と指示を入力すると PNG を出力し、下にパスを表示します。",
        imageEditProvider: "生成画像ルート",
        imageEditProviderHint: "猫を犬に変える処理は生成式編集です。ローカル diffusion server または OpenAI API key が必要です。Pillow は回転、明るさ、白黒などの確定的編集のみ対応します。",
        imageProviderAuto: "Auto：フィルターはローカル、生成は利用可能なサービス",
        imageProviderA1111: "ローカル Automatic1111 / Stable Diffusion",
        imageProviderOpenAI: "クラウド OpenAI Images API",
        imageProviderPillow: "ローカル Pillow フィルター",
        imageEditProviderHelp: "画像処理には二種類あります。回転、明るさ、白黒などの確定的処理は Pillow でローカル実行できます。猫を犬にする、物体を置き換える、新しい内容を生成する処理には local diffusion または OpenAI のような生成モデルが必要です。Auto はまず Automatic1111 を探し、fallback 設定に応じて OpenAI を検討します。",
        outputFolder: "出力フォルダー",
        outputComplete: "出力完了",
        previewOutput: "出力をプレビュー",
        openFolder: "フォルダーを開く",
        createEdit: "作成 / 編集",
        createImage: "画像出力を作成",
        createVideo: "動画ジョブを作成",
        jobQueue: "ジョブキュー",
        jobQueueHelp: "青が現在追跡中のジョブです。Running は先頭に表示します。",
        activeTracking: "追跡中",
        jobState: "状態",
        progressShort: "進捗",
        currentStageShort: "現在",
        noActiveJob: "待機中",
        idleTitle: "実行中のジョブはありません",
        idleMeta: "新しいジョブを作成するとここにリアルタイム進捗が表示されます。履歴はジョブキューから選択できます。",
        idleCurrent: "待機中",
        idleProgress: "--",
        runnerInterrupted: "runner が中断されました",
        restartRequired: "ジョブを再作成または再実行してください",
        statusRunning: "実行中",
        statusCompleted: "完了",
        statusQueued: "待機中",
        statusFailed: "失敗",
        statusCancelled: "キャンセル済み",
        statusCancelling: "キャンセル中",
        statusInterrupted: "中断",
        statusIncomplete: "情報不足",
        currentStep: "現在の段階",
        nextStep: "次の段階",
        overallProgress: "全体進捗",
        waitingForRunner: "runner の引き継ぎ待ち",
        noNextStep: "次の段階はありません",
        cancelJob: "ジョブをキャンセル",
        cancelRequested: "キャンセルをリクエストしました。runner は安全なチェックポイントで停止します。",
        cancelFailed: "キャンセル失敗",
        selectJob: "ジョブを選択",
        galleryTitle: "出力ギャラリー",
        mediaOnly: "画像 / 動画 / 音声",
        liveTracker: "リアルタイム処理フロー",
        running: "実行中",
        done: "完了",
        queue: "キュー",
        artifacts: "成果物",
        liveConsole: "ライブ Console Log",
        clear: "クリア",
        close: "閉じる",
        noJobs: "ジョブはまだありません。新しいジョブはここに表示されます。",
        noVideoPath: "動画パスなし",
        unknownProfile: "不明なプロファイル",
        noTaskFlow: "ジョブは選択されていません。新しいジョブを作成するとリアルタイムフローが表示されます。",
        emptyGalleryTitle: "再生できる出力はまだありません",
        emptyGalleryHelp: "ここには生成された画像、動画、音声のみ表示します。JSON レポートと元ファイルはデバッグ用にジョブフォルダーへ残します。",
        created: "作成日時",
        resolution: "解像度",
        previewImage: "画像をプレビュー",
        playVideo: "動画を再生",
        playAudio: "音声を再生",
        imageLabel: "画像",
        videoLabel: "動画",
        audioLabel: "音声",
        audioHint: "クリックして音声を再生",
        unavailablePreview: "プレビュー不可",
        deleteArtifact: "削除",
        confirmDelete: "削除を確認",
        cancelDelete: "キャンセル",
        deletePrompt: "この出力を削除しますか？ファイルは output/trash に移動します。",
        deleteMoved: "output/trash に移動しました。",
        noFile: "先に画像または動画を選択してください。",
        unsupportedMediaType: "現在このエディターは画像と動画のみ対応しています。",
        mediaLimitReached: "現在は最大12個まで素材を追加できます。超過分はスキップしました。",
        batchCreated: "作成したバッチジョブ数:",
        readingFile: "ファイルを読み込み中...",
        processing: "アップロードして処理中...",
        imageReadyHint: "画像は Pillow でローカル編集され、指定フォルダーへ出力されます。",
        videoReadyHint: "動画はローカル編集 runner で処理され、完了後ギャラリーに表示されます。",
        adaptiveOptionsTitle: "先にメディアを選ぶと処理フローが切り替わります",
        adaptiveOptionsHelp: "画像はローカル編集して出力します。動画は local video runner を開始します。音声は今後 ASR/文字起こしへ接続します。",
        imageWorkflowTitle: "画像ワークフロー",
        imageWorkflowHelp: "画像は Pillow によるローカル編集で、回転、明るさ、白黒、シャープ化などをすばやく出力します。Profile や解像度メニューは不要です。",
        videoWorkflowTitle: "動画ワークフロー",
        videoWorkflowHelp: "動画は local video runner で FFmpeg 解析、フレーム処理、MP4 出力を行います。高品質な生成式衣服編集は将来 segmentation/tracking または online fallback に接続します。",
        videoProfile: "動画 Profile",
        videoRuntimeProfile: "ローカル動画設定",
        videoRuntimeProfileSummary: "現在は Qualcomm 向け安全 profile を自動使用します。実際に runner 挙動が変わるまで選択肢は隠します。",
        videoResolution: "出力解像度",
        videoOptionHelpDefault: "現在は出力に影響する動画設定だけを表示します。runner 挙動を変えない項目は非表示にしています。",
        videoProfileHelp: "Profile はバックエンドの実行設定を選びます。この Qualcomm 端末では qualcomm_windows_arm64.yaml が既定で、CUDA や NVENC を前提にしない安定した CPU / FFmpeg ローカル経路です。",
        videoRuntimeProfileHelp: "これはまだ切り替え機能ではありません。現在のバックエンドは Qualcomm 安全ルート qualcomm_windows_arm64.yaml に固定し、CUDA/NVENC 前提を避けます。Profile がモデル、加速器、runner 戦略を実際に変える段階で選択肢を戻します。",
        videoResolutionHelp: "出力解像度はフレーム抽出とレンダーサイズに影響します。Original は可能な限り元サイズを保持し、720P / 1080P は処理前に縮小してローカル MVP を速く、小さくします。",
        autoDetect: "自動判定",
        imageEdit: "画像編集",
        videoPipeline: "動画 Pipeline",
        unknown: "不明",
        settingsEyebrow: "Settings",
        settingsTitle: "設定",
        defaultOutputFolder: "既定の出力フォルダー",
        defaultOutputHelp: "すべての出力は既定でこのフォルダーを使用します。安全のため、現在はプロジェクトの output 配下に限定しています。",
        autoPreviewOutput: "出力後に自動プレビュー",
        autoPreviewHelp: "画像出力後、新しいタブで完成ファイルを開きます。",
        autoOpenOutputFolder: "出力後にフォルダーを自動で開く",
        autoOpenHelp: "一括確認には便利ですが、頻繁な出力では少し邪魔になる場合があります。",
        galleryDensity: "ギャラリー密度",
        densityComfortable: "標準",
        densityCompact: "コンパクト",
        resetSettings: "リセット",
        saveSettings: "設定を保存",
        openDefaultOutputFolder: "既定フォルダーを開く",
        settingsSaved: "設定を保存しました。",
        settingsReset: "設定をリセットしました。",
        settingsButtonLabel: "設定",
        imageOutputSettingsLabel: "画像出力フォルダーを設定",
        localComputeTitle: "ローカル計算リソース",
        demoBadge: "Demo",
        localComputeHelp: "将来のローカル LLM、Vision、音声モデル、オンライン fallback のルーティング用デモ設定です。",
        onlineFeatureNoticeTitle: "現在はオンライン寄りの機能",
        onlineFeatureNoticeBody: "高品質な生成式動画編集、人物/衣服 segmentation + tracking、複雑な Agent 計画は現在オンライン機能として扱い、将来 Ollama / ONNX / DirectML / QNN でローカル化します。",
        localLlmProvider: "Local LLM Provider",
        localLlmEndpoint: "Local LLM Endpoint",
        visionProvider: "Vision / Video Provider",
        automatic1111Endpoint: "Automatic1111 Endpoint",
        openaiImageModel: "OpenAI Image Model",
        asrProvider: "Speech / ASR Provider",
        onlineFallbackPolicy: "Online Fallback Policy",
        fallbackWarnOnly: "警告のみ、自動オンライン送信なし",
        fallbackDisabled: "オンライン fallback を無効化",
        fallbackAskEachTime: "オンライン利用前に毎回確認",
        onlineFallbackHelp: "デモ設定：現時点ではオンラインサービスを呼び出しません。将来のタスクルーティング用です。",
        computeHelpPanelTitle: "設定ヘルプ",
        computeHelpDefault: "ラベル横の ? をクリックすると、各ローカル/オンライン計算設定の意味を確認できます。",
        helpButtonLabel: "この設定の説明を見る",
        localLlmProviderHelp: "テキスト理解、タスク計画、prompt 解析をどこで処理するかを決めます。Ollama local は将来のローカル LLM ルート、OpenAI online fallback はローカルで不足する場合のオンライン候補、None は LLM ルート無効です。現在はデモです。",
        localLlmEndpointHelp: "ローカル LLM サービスの URL です。Ollama は通常 http://127.0.0.1:11434 を使います。将来はモデル一覧確認、構造化 prompt、編集計画の取得に使います。今は保存のみです。",
        visionProviderHelp: "画像/動画処理ルートを決めます。Local Pillow / FFmpeg MVP は現在のローカル出力ルート。ONNX DirectML と Qualcomm QNN は segmentation、tracking、Vision モデルをローカル GPU/NPU へ移す将来ルート。Online video edit placeholder は高品質生成式動画編集がまだオンライン寄りであることを示します。",
        automatic1111EndpointHelp: "ローカル Stable Diffusion WebUI API の URL です。AUTOMATIC1111 を --api 付きで起動すると、Dump2Done は /sdapi/v1/img2img で猫を犬にする、物体置換などの image-to-image 編集を行います。現時点で最も現実的なローカル生成式画像ルートです。",
        openaiImageModelHelp: "OpenAI Images API のモデル名です。環境変数 OPENAI_API_KEY が必要です。ChatGPT Pro は API key とは別です。ローカル diffusion が未導入の場合のクラウドルートです。",
        asrProviderHelp: "音声認識ルートを決めます。Faster-Whisper CPU local は現在のローカル選択肢、ONNX ASR future は将来の高速化ローカル backend、Online ASR placeholder はクラウド fallback です。速度、プライバシー、品質、必要ハードウェアが違います。",
        onlineFallbackPolicyHelp: "ローカル資源だけではできない場合にオンラインサービスを使えるかを制御します。警告のみは自動送信なし、無効化はオンライン禁止、毎回確認は将来オンライン利用前に確認します。"
      }
    };
    let currentLocale = localStorage.getItem("dump2done.locale") || "zh-Hant";
    const DEFAULT_SETTINGS = {
      defaultOutputDirectory: "output",
      autoPreviewOutput: false,
      autoOpenOutputFolder: false,
      galleryDensity: "comfortable",
      localLlmProvider: "ollama_demo",
      localLlmEndpoint: "http://127.0.0.1:11434",
      visionProvider: "local_pillow_mvp",
      imageEditProvider: "auto",
      automatic1111Endpoint: "http://127.0.0.1:7860",
      comfyuiEndpoint: "http://127.0.0.1:8188",
      openaiImageModel: "gpt-image-1.5",
      asrProvider: "faster_whisper_cpu_demo",
      onlineFallbackPolicy: "warn_only"
    };
    let userSettings = loadSettings();

    function t(key) {
      return (I18N[currentLocale] && I18N[currentLocale][key]) || I18N["zh-Hant"][key] || key;
    }

    function loadSettings() {
      try {
        const parsed = JSON.parse(localStorage.getItem("dump2done.settings") || "{}");
        return { ...DEFAULT_SETTINGS, ...(parsed && typeof parsed === "object" ? parsed : {}) };
      } catch {
        return { ...DEFAULT_SETTINGS };
      }
    }

    function saveSettingsLocal() {
      localStorage.setItem("dump2done.settings", JSON.stringify(userSettings));
    }

    async function loadSettingsFromServer() {
      try {
        const response = await fetch("/api/settings");
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || "Settings load failed");
        userSettings = { ...DEFAULT_SETTINGS, ...(payload.settings || {}) };
        saveSettingsLocal();
        applySettingsToUi();
        renderGallery();
        lucide.createIcons();
      } catch (error) {
        appendLogLine(`[settings] ${error.message}`);
      }
    }

    async function saveSettingsToServer() {
      const response = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: userSettings })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || "Settings save failed");
      userSettings = { ...DEFAULT_SETTINGS, ...(payload.settings || {}) };
      saveSettingsLocal();
      return payload;
    }

    function applyLanguage() {
      document.documentElement.lang = currentLocale;
      document.querySelectorAll("[data-i18n]").forEach(node => {
        node.textContent = t(node.dataset.i18n);
      });
      document.querySelectorAll("[data-i18n-placeholder]").forEach(node => {
        node.setAttribute("placeholder", t(node.dataset.i18nPlaceholder));
      });
      const selector = document.getElementById("languageSelect");
      if (selector) selector.value = currentLocale;
      const settingsButton = document.getElementById("settingsButton");
      if (settingsButton) {
        settingsButton.title = t("settingsButtonLabel");
        settingsButton.setAttribute("aria-label", t("settingsButtonLabel"));
      }
      const imageOutputSettingsButton = document.getElementById("imageOutputSettingsButton");
      if (imageOutputSettingsButton) {
        imageOutputSettingsButton.title = t("imageOutputSettingsLabel");
        imageOutputSettingsButton.setAttribute("aria-label", t("imageOutputSettingsLabel"));
      }
      document.querySelectorAll(".compute-help").forEach(button => {
        const helpText = t(button.dataset.helpKey);
        button.title = helpText;
        button.setAttribute("aria-label", `${t("helpButtonLabel")}: ${helpText}`);
      });
      document.querySelectorAll(".field-help").forEach(button => {
        const helpText = t(button.dataset.helpKey);
        button.title = helpText;
        button.setAttribute("aria-label", `${t("helpButtonLabel")}: ${helpText}`);
      });
      if (activeComputeHelpKey) {
        showComputeHelp(activeComputeHelpKey);
      }
      if (activeVideoOptionHelpKey) {
        showVideoOptionHelp(activeVideoOptionHelpKey);
      }
    }

    function showComputeHelp(helpKey) {
      activeComputeHelpKey = helpKey;
      const panel = document.getElementById("computeHelpPanel");
      const text = document.getElementById("computeHelpText");
      if (!panel || !text) return;
      text.textContent = t(helpKey);
      panel.classList.remove("border-white/10", "bg-black/30");
      panel.classList.add("border-lime-300/30", "bg-lime-300/[0.06]");
      document.querySelectorAll(".compute-help").forEach(button => {
        const active = button.dataset.helpKey === helpKey;
        button.classList.toggle("border-lime-300/60", active);
        button.classList.toggle("bg-lime-300/20", active);
        button.classList.toggle("text-lime-100", active);
      });
    }

    function showVideoOptionHelp(helpKey) {
      activeVideoOptionHelpKey = helpKey;
      const text = document.getElementById("videoOptionHelp");
      if (!text) return;
      text.textContent = t(helpKey);
      text.classList.remove("border-white/10", "bg-black/25");
      text.classList.add("border-sky-300/30", "bg-sky-300/[0.06]");
      document.querySelectorAll(".field-help").forEach(button => {
        const active = button.dataset.helpKey === helpKey;
        button.classList.toggle("border-lime-300/60", active);
        button.classList.toggle("bg-lime-300/20", active);
        button.classList.toggle("text-lime-100", active);
      });
    }

    function applySettingsToUi() {
      const defaultOutput = document.getElementById("defaultOutputDirectory");
      const imageOutput = document.querySelector('input[name="imageOutputDirectory"]');
      const autoPreview = document.getElementById("autoPreviewOutput");
      const autoOpen = document.getElementById("autoOpenOutputFolder");
      const density = document.getElementById("galleryDensity");
      const localLlmProvider = document.getElementById("localLlmProvider");
      const localLlmEndpoint = document.getElementById("localLlmEndpoint");
      const visionProvider = document.getElementById("visionProvider");
      const imageEditProvider = document.getElementById("imageEditProvider");
      const automatic1111Endpoint = document.getElementById("automatic1111Endpoint");
      const openaiImageModel = document.getElementById("openaiImageModel");
      const asrProvider = document.getElementById("asrProvider");
      const onlineFallbackPolicy = document.getElementById("onlineFallbackPolicy");
      if (defaultOutput) defaultOutput.value = userSettings.defaultOutputDirectory;
      if (imageOutput && (!imageOutput.value || imageOutput.value === DEFAULT_SETTINGS.defaultOutputDirectory)) {
        imageOutput.value = userSettings.defaultOutputDirectory;
      }
      if (autoPreview) autoPreview.checked = !!userSettings.autoPreviewOutput;
      if (autoOpen) autoOpen.checked = !!userSettings.autoOpenOutputFolder;
      if (density) density.value = userSettings.galleryDensity || DEFAULT_SETTINGS.galleryDensity;
      if (localLlmProvider) localLlmProvider.value = userSettings.localLlmProvider || DEFAULT_SETTINGS.localLlmProvider;
      if (localLlmEndpoint) localLlmEndpoint.value = userSettings.localLlmEndpoint || DEFAULT_SETTINGS.localLlmEndpoint;
      if (visionProvider) visionProvider.value = userSettings.visionProvider || DEFAULT_SETTINGS.visionProvider;
      if (imageEditProvider) imageEditProvider.value = userSettings.imageEditProvider || DEFAULT_SETTINGS.imageEditProvider;
      if (automatic1111Endpoint) automatic1111Endpoint.value = userSettings.automatic1111Endpoint || DEFAULT_SETTINGS.automatic1111Endpoint;
      if (openaiImageModel) openaiImageModel.value = userSettings.openaiImageModel || DEFAULT_SETTINGS.openaiImageModel;
      if (asrProvider) asrProvider.value = userSettings.asrProvider || DEFAULT_SETTINGS.asrProvider;
      if (onlineFallbackPolicy) onlineFallbackPolicy.value = userSettings.onlineFallbackPolicy || DEFAULT_SETTINGS.onlineFallbackPolicy;
    }

    function openSettingsModal() {
      applySettingsToUi();
      document.getElementById("settingsHint").textContent = "";
      document.getElementById("settingsModal").classList.remove("hidden");
    }

    function openImageOutputSettings() {
      const imageOutput = document.getElementById("imageOutputDirectory");
      if (imageOutput && imageOutput.value.trim()) {
        userSettings.defaultOutputDirectory = imageOutput.value.trim();
      }
      openSettingsModal();
      const defaultOutput = document.getElementById("defaultOutputDirectory");
      if (defaultOutput) {
        defaultOutput.focus();
        defaultOutput.select();
      }
    }

    function closeSettingsModal() {
      document.getElementById("settingsModal").classList.add("hidden");
    }

    function finishSettingsSave(message) {
      applySettingsToUi();
      const imageOutput = document.querySelector('input[name="imageOutputDirectory"]');
      if (imageOutput) imageOutput.value = userSettings.defaultOutputDirectory;
      renderGallery();
      lucide.createIcons();
      document.getElementById("settingsHint").textContent = message;
      appendLogLine(`[settings] ${message}`);
      closeSettingsModal();
    }

    function render() {
      renderStats();
      renderJobs();
      renderPipeline();
      renderGallery();
      renderLog();
      applyLanguage();
      applySettingsToUi();
      updateEditorMode(currentMediaType);
      lucide.createIcons();
    }

    function chooseDefaultJobId(items) {
      const actionable = (items || []).filter(job => ["running", "cancelling", "queued"].includes(job.status));
      const ordered = [...actionable].sort((a, b) => {
        const weight = { running: 0, cancelling: 1, queued: 2 };
        return (weight[a.status] ?? 9) - (weight[b.status] ?? 9) || String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""));
      });
      return ordered[0] ? ordered[0].id : "";
    }

    function renderStats() {
      const job = jobs.find(item => item.id === activeJobId);
      const state = document.getElementById("statRunning");
      const progress = document.getElementById("statDone");
      const current = document.getElementById("statQueue");
      if (!job) {
        state.textContent = t("noActiveJob");
        progress.textContent = t("idleProgress");
        current.textContent = t("idleCurrent");
        state.className = "block truncate text-sm text-slate-400";
        progress.className = "block text-sm text-slate-400";
        current.className = "block truncate text-sm text-slate-400";
        return;
      }
      const phase = activePhase(job);
      state.textContent = statusLabel(job.status);
      progress.textContent = `${overallProgress(job)}%`;
      current.textContent = job.status === "interrupted" ? t("runnerInterrupted") : phase ? phase.label : t("noNextStep");
      state.className = `block truncate text-sm ${statusTextClass(job.status)}`;
      progress.className = "block text-sm text-lime-300";
      current.className = "block truncate text-sm text-orange-300";
    }

    function renderJobs() {
      const list = document.getElementById("jobList");
      if (!jobs.length) {
        list.innerHTML = `
          <div class="rounded-lg border border-dashed border-white/15 bg-white/[0.03] p-4 text-sm text-slate-400">
            ${escapeHtml(t("noJobs"))}
          </div>
        `;
        return;
      }
      const orderedJobs = [...jobs].sort((a, b) => {
        const weight = { running: 0, cancelling: 1, queued: 2, interrupted: 3, failed: 4, completed: 5, cancelled: 6, incomplete: 7 };
        return (weight[a.status] ?? 9) - (weight[b.status] ?? 9) || String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""));
      });
      list.innerHTML = orderedJobs.map(job => {
        const currentPhase = activePhase(job);
        const activeClass = job.id === activeJobId ? "border-sky-300/55 bg-sky-300/10" : job.status === "running" ? "border-sky-300/30 bg-sky-300/[0.05]" : job.status === "interrupted" ? "border-orange-300/25 bg-orange-300/[0.04]" : "border-white/10 bg-white/[0.03] hover:border-white/20";
        return `
        <button data-job-id="${escapeAttr(job.id)}" class="job-pick w-full rounded-lg border ${activeClass} p-3 text-left">
          <div class="flex items-center justify-between gap-3">
            <strong class="min-w-0 truncate text-sm">${escapeHtml(displayJobName(job.id))}</strong>
            <span class="rounded-md px-2 py-1 text-[11px] font-black ${statusBadge(job.status)}">${escapeHtml(statusLabel(job.status))}</span>
          </div>
          <p class="mt-2 min-w-0 truncate text-xs text-slate-400" title="${escapeAttr(job.videoPath || t("noVideoPath"))}">${escapeHtml(compactPath(job.videoPath || t("noVideoPath")))}</p>
          <div class="mt-2 flex items-center justify-between gap-2">
            <p class="min-w-0 truncate text-xs font-bold text-slate-500">${escapeHtml(currentPhase ? `${currentPhase.label} · ${currentPhase.progress}%` : job.profile || t("unknownProfile"))}</p>
            ${job.status === "running" ? '<span class="h-2 w-2 shrink-0 rounded-full bg-sky-300 shadow-[0_0_14px_rgba(125,211,252,.8)]"></span>' : ""}
            ${job.status === "interrupted" ? '<span class="h-2 w-2 shrink-0 rounded-full bg-orange-300 shadow-[0_0_14px_rgba(253,186,116,.75)]"></span>' : ""}
          </div>
        </button>
      `;
      }).join("");
      document.querySelectorAll(".job-pick").forEach(button => {
        button.addEventListener("click", () => {
          activeJobId = button.dataset.jobId;
          render();
          connectSseStream();
        });
      });
    }

    function renderPipeline() {
      const job = jobs.find(item => item.id === activeJobId);
      const title = document.getElementById("activeJobTitle");
      const meta = document.getElementById("activeJobMeta");
      const summary = document.getElementById("pipelineSummary");
      const target = document.getElementById("pipelineSteps");
      const cancelButton = document.getElementById("cancelActiveJob");
      if (!job) {
        title.textContent = t("idleTitle");
        title.title = "";
        meta.textContent = t("idleMeta");
        summary.classList.remove("hidden");
        summary.innerHTML = `
          <div class="flex flex-wrap items-center justify-between gap-4">
            <div>
              <p class="text-xs font-black uppercase tracking-wide text-sky-300">${escapeHtml(t("currentStep"))}</p>
              <p class="mt-1 text-lg font-black text-slate-100">${escapeHtml(t("idleCurrent"))}</p>
              <p class="mt-1 text-xs font-semibold text-slate-500">${escapeHtml(t("idleMeta"))}</p>
            </div>
            <div class="rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-sm font-black text-slate-400">
              ${escapeHtml(t("noActiveJob"))}
            </div>
          </div>
        `;
        cancelButton.classList.add("hidden");
        target.innerHTML = `
          <div class="rounded-xl border border-dashed border-white/15 bg-white/[0.03] p-5 text-sm leading-6 text-slate-400 md:col-span-2 xl:col-span-4">
            ${escapeHtml(t("noTaskFlow"))}
          </div>
        `;
        return;
      }
      title.textContent = `${displayJobName(job.id)} · ${statusLabel(job.status)}`;
      title.title = job.id;
      meta.textContent = `${t("activeTracking")} · ${compactPath(job.videoPath || job.outputDirectory || "")}`;
      const currentPhase = activePhase(job);
      const nextPhase = nextWaitingPhase(job);
      const overall = overallProgress(job);
      const canCancel = ["running", "queued", "cancelling"].includes(job.status);
      cancelButton.classList.toggle("hidden", !canCancel);
      cancelButton.disabled = job.status === "cancelling";
      summary.classList.remove("hidden");
      summary.innerHTML = `
        <div class="grid gap-4 lg:grid-cols-[1fr_1fr_160px] lg:items-center">
          <div>
            <p class="text-xs font-black uppercase tracking-wide text-sky-300">${escapeHtml(t("currentStep"))}</p>
            <p class="mt-1 text-lg font-black text-slate-100">${escapeHtml(job.status === "interrupted" ? t("runnerInterrupted") : currentPhase ? `${currentPhase.label} · ${currentPhase.progress}%` : t("waitingForRunner"))}</p>
            <p class="mt-1 text-xs font-semibold text-slate-500">${escapeHtml(job.status === "interrupted" ? t("restartRequired") : currentPhase ? currentPhase.detail : statusLabel(job.status))}</p>
          </div>
          <div>
            <p class="text-xs font-black uppercase tracking-wide text-lime-300">${escapeHtml(t("nextStep"))}</p>
            <p class="mt-1 text-sm font-bold text-slate-300">${escapeHtml(job.status === "interrupted" ? t("restartRequired") : nextPhase ? `${nextPhase.label} · ${nextPhase.detail}` : t("noNextStep"))}</p>
          </div>
          <div>
            <p class="text-xs font-black uppercase tracking-wide text-slate-500">${escapeHtml(t("overallProgress"))}</p>
            <p class="mt-1 text-3xl font-black text-lime-200">${overall}%</p>
          </div>
        </div>
        <div class="mt-4 h-2 overflow-hidden rounded-full bg-black/40">
          <div class="relative h-full overflow-hidden rounded-full bg-lime-300 ${job.status === "running" ? "pipeline-flow" : ""}" style="width:${overall}%"></div>
        </div>
      `;
      target.innerHTML = job.phases.map((phase, index) => `
        <div class="relative rounded-xl border ${phaseTone[phase.status] || phaseTone.waiting} p-4 ${phase.status === "running" ? "pipeline-card-running" : ""}">
          <div class="mb-4 flex items-center justify-between">
            <span class="grid h-9 w-9 place-items-center rounded-lg bg-black/25">
              <i data-lucide="${phaseIcon[phase.status] || phaseIcon.waiting}" class="h-5 w-5 ${phase.status === "running" ? "animate-spin" : ""}"></i>
            </span>
            <span class="text-xs font-black text-slate-400">0${index + 1}</span>
          </div>
          <h3 class="font-black">${escapeHtml(phase.label)}</h3>
          <p class="mt-1 text-xs font-semibold text-slate-400">${escapeHtml(phase.detail)}</p>
          <div class="mt-4 h-2 overflow-hidden rounded-full bg-black/40">
            <div class="relative h-full overflow-hidden rounded-full ${phase.status === "completed" ? "bg-lime-300" : phase.status === "running" ? "bg-sky-300 pipeline-flow" : phase.status === "failed" ? "bg-red-300" : phase.status === "cancelled" ? "bg-orange-300" : "bg-slate-700"}" style="width:${phase.progress}%"></div>
          </div>
          <p class="mt-2 text-xs font-bold text-slate-400">${phase.progress}% · ${escapeHtml(phase.status)}</p>
        </div>
      `).join("");
      lucide.createIcons();
    }

    function activePhase(job) {
      return (job.phases || []).find(phase => phase.status === "running")
        || (job.phases || []).find(phase => phase.status === "cancelled")
        || (job.phases || []).find(phase => phase.status === "failed")
        || (job.phases || []).find(phase => phase.status === "waiting")
        || null;
    }

    function nextWaitingPhase(job) {
      return (job.phases || []).find(phase => phase.status === "waiting") || null;
    }

    function overallProgress(job) {
      const phases = job.phases || [];
      if (!phases.length) return 0;
      const total = phases.reduce((sum, phase) => sum + Number(phase.progress || 0), 0);
      return Math.max(0, Math.min(100, Math.round(total / phases.length)));
    }

    function displayJobName(jobId) {
      const text = String(jobId || "");
      return text.length > 42 ? `${text.slice(0, 18)}...${text.slice(-14)}` : text;
    }

    function compactPath(path) {
      const text = String(path || "");
      if (text.length <= 64) return text;
      const parts = text.split(/[\\/]+/).filter(Boolean);
      if (parts.length >= 3) return `${parts[0]}\\...\\${parts[parts.length - 2]}\\${parts[parts.length - 1]}`;
      return `${text.slice(0, 28)}...${text.slice(-24)}`;
    }

    function renderGallery() {
      const grid = document.getElementById("gallery");
      const mediaGallery = gallery.filter(isMediaGalleryItem);
      if (!mediaGallery.length) {
        grid.innerHTML = `
          <div class="rounded-xl border border-dashed border-white/15 bg-black/20 p-6 md:col-span-2 xl:col-span-3">
            <div class="flex items-start gap-3">
              <i data-lucide="archive-x" class="mt-1 h-6 w-6 text-orange-300"></i>
              <div>
                <h3 class="font-black">${escapeHtml(t("emptyGalleryTitle"))}</h3>
                <p class="mt-2 text-sm leading-6 text-slate-400">${escapeHtml(t("emptyGalleryHelp"))}</p>
              </div>
            </div>
          </div>
        `;
        lucide.createIcons();
        return;
      }
      const compact = userSettings.galleryDensity === "compact";
      grid.innerHTML = mediaGallery.map(item => `
        <article class="min-w-0 rounded-xl border border-white/10 bg-black/20 ${compact ? "p-3" : "p-4"}">
          ${galleryPreview(item)}
          <h3 class="truncate font-black">${escapeHtml(item.fileName)}</h3>
          <p class="mt-1 truncate text-xs font-bold text-slate-500" title="${escapeAttr(item.relativePath)}">${escapeHtml(item.jobId)} · ${escapeHtml(item.relativePath)}</p>
          <dl class="${compact ? "mt-2" : "mt-3"} grid grid-cols-2 gap-2 text-xs text-slate-400">
            <div><dt class="font-bold text-slate-500">${escapeHtml(t("created"))}</dt><dd>${escapeHtml(item.createdAt)}</dd></div>
            <div><dt class="font-bold text-slate-500">${escapeHtml(t("resolution"))}</dt><dd>${escapeHtml(item.resolution)}</dd></div>
          </dl>
          <div class="${compact ? "mt-3" : "mt-4"} grid grid-cols-2 gap-2">
            <button class="rounded-lg border border-lime-300/30 px-3 py-2 text-xs font-black text-lime-100 hover:bg-lime-300/10" onclick="openFolderById('${escapeAttr(item.id)}')"><i data-lucide="folder-open" class="mr-1 inline h-3 w-3"></i>${escapeHtml(t("openFolder"))}</button>
            <button class="rounded-lg border border-red-300/30 px-3 py-2 text-xs font-black text-red-100 hover:bg-red-300/10" onclick="requestDeleteArtifact('${escapeAttr(item.id)}')"><i data-lucide="trash-2" class="mr-1 inline h-3 w-3"></i>${escapeHtml(t("deleteArtifact"))}</button>
          </div>
          ${deleteConfirmation(item)}
        </article>
      `).join("");
    }

    function renderLog() {
      const terminal = document.getElementById("consoleLog");
      terminal.innerHTML = "";
      for (const line of logLines.slice(-180)) {
        terminal.appendChild(logLineElement(line));
      }
      scrollTerminal();
    }

    function appendLogLine(line, persist = true) {
      const terminal = document.getElementById("consoleLog");
      logLines.push(stripAnsi(String(line)));
      if (logLines.length > 240) logLines.splice(0, logLines.length - 240);
      terminal.appendChild(logLineElement(line));
      while (terminal.children.length > 180) terminal.removeChild(terminal.firstChild);
      scrollTerminal();
      if (persist) persistConsoleLog(line);
    }

    function logLineElement(line) {
      const row = document.createElement("div");
      row.className = "min-h-5 whitespace-pre-wrap break-words";
      row.innerHTML = ansiToHtml(String(line));
      return row;
    }

    function scrollTerminal() {
      const terminal = document.getElementById("consoleLog");
      terminal.scrollTo({ top: terminal.scrollHeight, behavior: "smooth" });
    }

    async function refreshJobs() {
      const response = await fetch("/api/jobs");
      const payload = await response.json();
      jobs = [...payload.jobs];
      gallery = [...(payload.gallery || [])].filter(isMediaGalleryItem);
      if (!jobs.find(job => job.id === activeJobId)) activeJobId = chooseDefaultJobId(jobs);
      logLines.push(`[dashboard] Refreshed ${payload.jobs.length} real job(s) from output/jobs`);
      render();
    }

    async function cancelActiveJob() {
      const job = jobs.find(item => item.id === activeJobId);
      if (!job || !["running", "queued", "cancelling"].includes(job.status)) return;
      const button = document.getElementById("cancelActiveJob");
      button.disabled = true;
      try {
        const response = await fetch("/api/cancel-job", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: job.id })
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || t("cancelFailed"));
        jobs = [...(payload.jobs || jobs)];
        gallery = [...(payload.gallery || gallery)].filter(isMediaGalleryItem);
        appendLogLine(`[dashboard] ${payload.message || t("cancelRequested")}`);
        document.getElementById("formHint").textContent = t("cancelRequested");
        render();
      } catch (error) {
        appendLogLine(`[cancel] ${error.message}`);
        document.getElementById("formHint").textContent = `${t("cancelFailed")}: ${error.message}`;
      } finally {
        button.disabled = false;
      }
    }

    document.getElementById("refreshJobs").addEventListener("click", refreshJobs);
    document.getElementById("cancelActiveJob").addEventListener("click", cancelActiveJob);
    document.getElementById("clearLog").addEventListener("click", () => {
      logLines.length = 0;
      renderLog();
    });
    document.getElementById("languageSelect").addEventListener("change", event => {
      currentLocale = event.target.value || "zh-Hant";
      localStorage.setItem("dump2done.locale", currentLocale);
      render();
      renderSelectedMedia();
      updateEditorMode(currentMediaType);
    });
    document.getElementById("settingsButton").addEventListener("click", openSettingsModal);
    document.getElementById("imageOutputSettingsButton").addEventListener("click", openImageOutputSettings);
    document.getElementById("closeSettings").addEventListener("click", closeSettingsModal);
    document.getElementById("settingsModal").addEventListener("click", event => {
      if (event.target.id === "settingsModal") closeSettingsModal();
    });
    document.getElementById("settingsForm").addEventListener("click", event => {
      const helpButton = event.target.closest(".compute-help");
      if (!helpButton) return;
      event.preventDefault();
      showComputeHelp(helpButton.dataset.helpKey);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") closeSettingsModal();
    });
    document.getElementById("settingsForm").addEventListener("submit", async event => {
      event.preventDefault();
      userSettings = {
        defaultOutputDirectory: document.getElementById("defaultOutputDirectory").value.trim() || DEFAULT_SETTINGS.defaultOutputDirectory,
        autoPreviewOutput: document.getElementById("autoPreviewOutput").checked,
        autoOpenOutputFolder: document.getElementById("autoOpenOutputFolder").checked,
        galleryDensity: document.getElementById("galleryDensity").value || DEFAULT_SETTINGS.galleryDensity,
        localLlmProvider: document.getElementById("localLlmProvider").value || DEFAULT_SETTINGS.localLlmProvider,
        localLlmEndpoint: document.getElementById("localLlmEndpoint").value.trim() || DEFAULT_SETTINGS.localLlmEndpoint,
        visionProvider: document.getElementById("visionProvider").value || DEFAULT_SETTINGS.visionProvider,
        imageEditProvider: document.getElementById("imageEditProvider").value || DEFAULT_SETTINGS.imageEditProvider,
        automatic1111Endpoint: document.getElementById("automatic1111Endpoint").value.trim() || DEFAULT_SETTINGS.automatic1111Endpoint,
        comfyuiEndpoint: DEFAULT_SETTINGS.comfyuiEndpoint,
        openaiImageModel: document.getElementById("openaiImageModel").value.trim() || DEFAULT_SETTINGS.openaiImageModel,
        asrProvider: document.getElementById("asrProvider").value || DEFAULT_SETTINGS.asrProvider,
        onlineFallbackPolicy: document.getElementById("onlineFallbackPolicy").value || DEFAULT_SETTINGS.onlineFallbackPolicy
      };
      try {
        await saveSettingsToServer();
        finishSettingsSave(t("settingsSaved"));
      } catch (error) {
        document.getElementById("settingsHint").textContent = error.message;
        appendLogLine(`[settings] ${error.message}`);
      }
    });
    document.getElementById("openDefaultOutputFolder").addEventListener("click", () => {
      const folder = document.getElementById("defaultOutputDirectory").value.trim() || userSettings.defaultOutputDirectory;
      openFolder(folder, "default output");
    });
    document.getElementById("resetSettings").addEventListener("click", async () => {
      userSettings = { ...DEFAULT_SETTINGS };
      try {
        await saveSettingsToServer();
        finishSettingsSave(t("settingsReset"));
      } catch (error) {
        document.getElementById("settingsHint").textContent = error.message;
        appendLogLine(`[settings] ${error.message}`);
      }
    });
    const mediaFileInput = document.getElementById("mediaFile");
    const dropZone = document.getElementById("dropZone");
    const mediaPrompt = document.getElementById("mediaPrompt");
    const imageOptions = document.getElementById("imageOptions");
    const adaptiveOptions = document.getElementById("adaptiveOptions");
    const videoOptions = document.getElementById("videoOptions");
    const mediaResult = document.getElementById("mediaResult");
    const mediaResultPath = document.getElementById("mediaResultPath");
    const mediaResultLink = document.getElementById("mediaResultLink");
    const openMediaResultFolder = document.getElementById("openMediaResultFolder");
    const mediaAssetGrid = document.getElementById("mediaAssetGrid");
    let currentMediaType = "unknown";
    let lastMediaOutputFolder = "";
    document.querySelectorAll(".prompt-chip").forEach(button => {
      button.addEventListener("click", () => {
        mediaPrompt.value = button.dataset.prompt || "";
        mediaPrompt.focus();
      });
    });
    openMediaResultFolder.addEventListener("click", () => {
      if (lastMediaOutputFolder) openFolder(lastMediaOutputFolder, "media output");
    });
    document.getElementById("mediaForm").addEventListener("click", event => {
      const addMediaButton = event.target.closest("[data-add-media]");
      if (addMediaButton) {
        event.preventDefault();
        mediaFileInput.click();
        return;
      }
      const removeMediaButton = event.target.closest("[data-remove-media-id]");
      if (removeMediaButton) {
        event.preventDefault();
        event.stopPropagation();
        removeSelectedMedia(removeMediaButton.dataset.removeMediaId);
        return;
      }
      const helpButton = event.target.closest(".field-help");
      if (!helpButton) return;
      event.preventDefault();
      event.stopPropagation();
      showVideoOptionHelp(helpButton.dataset.helpKey);
    });
    dropZone.addEventListener("dragover", event => {
      event.preventDefault();
      dropZone.classList.add("border-sky-300/70", "bg-sky-300/10");
    });
    dropZone.addEventListener("dragleave", () => {
      dropZone.classList.remove("border-sky-300/70", "bg-sky-300/10");
    });
    dropZone.addEventListener("drop", event => {
      event.preventDefault();
      dropZone.classList.remove("border-sky-300/70", "bg-sky-300/10");
      const files = event.dataTransfer && event.dataTransfer.files ? Array.from(event.dataTransfer.files) : [];
      addSelectedMedia(files);
    });
    mediaFileInput.addEventListener("change", () => {
      const files = mediaFileInput.files ? Array.from(mediaFileInput.files) : [];
      addSelectedMedia(files);
      mediaFileInput.value = "";
    });

    function addSelectedMedia(files) {
      const accepted = files.filter(file => ["image", "video"].includes(detectClientMediaType(file)));
      const remainingSlots = Math.max(0, 12 - selectedMediaItems.length);
      accepted.slice(0, remainingSlots).forEach(file => {
        selectedMediaItems.push({
          id: `media_${Date.now()}_${mediaItemSerial++}`,
          file,
          type: detectClientMediaType(file),
          previewUrl: URL.createObjectURL(file)
        });
      });
      hideMediaResult();
      renderSelectedMedia();
      if (files.length && !accepted.length) {
        document.getElementById("formHint").textContent = t("unsupportedMediaType");
      } else if (accepted.length > remainingSlots) {
        document.getElementById("formHint").textContent = t("mediaLimitReached");
      }
    }

    function removeSelectedMedia(id) {
      const removed = selectedMediaItems.find(item => item.id === id);
      if (removed) URL.revokeObjectURL(removed.previewUrl);
      selectedMediaItems = selectedMediaItems.filter(item => item.id !== id);
      renderSelectedMedia();
      document.getElementById("formHint").textContent = t("mediaSelectionCleared");
    }

    function renderSelectedMedia() {
      const pill = document.getElementById("mediaTypePill");
      const typeSet = new Set(selectedMediaItems.map(item => item.type));
      if (!selectedMediaItems.length) {
        pill.textContent = t("autoDetect");
        updateEditorMode("unknown");
        mediaAssetGrid.innerHTML = mediaAddSlotHtml(true);
        lucide.createIcons();
        return;
      }
      const mode = typeSet.has("video") ? "video" : "image";
      updateEditorMode(mode);
      pill.textContent = typeSet.size > 1 ? t("mixedMedia") : mode === "image" ? t("imageEdit") : t("videoPipeline");
      const cards = selectedMediaItems.map(item => mediaAssetCardHtml(item)).join("");
      mediaAssetGrid.innerHTML = `${cards}${selectedMediaItems.length < 12 ? mediaAddSlotHtml(false) : ""}`;
      lucide.createIcons();
    }

    function mediaAddSlotHtml(isEmpty) {
      const extraClass = isEmpty ? "col-span-2 min-h-40" : "min-h-36";
      return `
        <button class="${extraClass} grid place-items-center rounded-lg border border-dashed border-sky-300/25 bg-white/[0.03] p-4 text-center text-slate-400 hover:border-sky-300/70 hover:bg-sky-300/10 hover:text-sky-100" type="button" data-add-media>
          <span class="grid gap-2">
            <i data-lucide="plus" class="mx-auto h-9 w-9"></i>
            <strong class="text-sm">${escapeHtml(isEmpty ? t("uploadMediaPrompt") : t("addAnotherMedia"))}</strong>
            <span class="text-xs text-slate-500">${escapeHtml(t("autoDetectHint"))}</span>
          </span>
        </button>
      `;
    }

    function mediaAssetCardHtml(item) {
      const preview = item.type === "image"
        ? `<img class="h-32 w-full object-cover" src="${escapeAttr(item.previewUrl)}" alt="${escapeAttr(item.file.name)}">`
        : `<video class="h-32 w-full object-cover" src="${escapeAttr(item.previewUrl)}" muted playsinline controls></video>`;
      const typeLabel = item.type === "image" ? t("imageLabel") : t("videoLabel");
      return `
        <span class="grid min-w-0 gap-2 rounded-lg border border-white/10 bg-black/35 p-2 text-left">
          <span class="relative overflow-hidden rounded-md bg-black">
            ${preview}
            <span class="absolute left-2 top-2 rounded-md bg-black/70 px-2 py-1 text-[11px] font-black text-lime-200">${escapeHtml(typeLabel)}</span>
            <button class="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md border border-red-300/35 bg-black/75 px-2 py-1 text-[11px] font-black text-red-100 hover:bg-red-300/15" type="button" data-remove-media-id="${escapeAttr(item.id)}">
              <i data-lucide="x" class="h-3 w-3"></i>${escapeHtml(t("clearMediaSelection"))}
            </button>
          </span>
          <span class="grid min-w-0 gap-1">
            <strong class="truncate text-sm text-slate-100">${escapeHtml(item.file.name)}</strong>
            <span class="truncate text-xs text-slate-500">${escapeHtml(item.file.type || "unknown")} · ${escapeHtml(formatBytes(item.file.size))}</span>
          </span>
        </span>
      `;
    }

    function resetMediaSelection(showHint) {
      const pill = document.getElementById("mediaTypePill");
      mediaFileInput.value = "";
      selectedMediaItems.forEach(item => URL.revokeObjectURL(item.previewUrl));
      selectedMediaItems = [];
      hideMediaResult();
      pill.textContent = t("autoDetect");
      renderSelectedMedia();
      updateEditorMode("unknown");
      if (showHint) {
        document.getElementById("formHint").textContent = t("mediaSelectionCleared");
      }
      lucide.createIcons();
    }

    document.getElementById("mediaForm").addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.currentTarget;
      if (!selectedMediaItems.length) {
        document.getElementById("formHint").textContent = t("noFile");
        return;
      }
      const data = Object.fromEntries(new FormData(form).entries());
      const hint = document.getElementById("formHint");
      const submit = document.getElementById("mediaSubmit");
      hint.textContent = t("readingFile");
      submit.disabled = true;
      try {
        const createdPayloads = [];
        for (let index = 0; index < selectedMediaItems.length; index += 1) {
          const item = selectedMediaItems[index];
          const file = item.file;
          const detectedType = item.type;
          hint.textContent = `${t("processing")} ${index + 1}/${selectedMediaItems.length}`;
          const dataBase64 = await fileToBase64(file);
          const response = await fetch("/api/media-jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              filename: file.name,
              content_type: file.type,
              data_base64: dataBase64,
              prompt: data.prompt || "",
              profile: data.profile,
              model_version: data.modelVersion,
              resolution: detectedType === "image" ? "original" : data.resolution,
              output_directory: data.imageOutputDirectory || userSettings.defaultOutputDirectory,
              image_edit_provider: detectedType === "image" ? (data.imageEditProvider || userSettings.imageEditProvider || "auto") : "",
              automatic1111_endpoint: userSettings.automatic1111Endpoint,
              comfyui_endpoint: userSettings.comfyuiEndpoint,
              openai_image_model: userSettings.openaiImageModel,
              online_fallback_policy: userSettings.onlineFallbackPolicy
            })
          });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.message || "Media job failed");
          createdPayloads.push(payload);
          jobs = [payload.job, ...jobs];
          gallery = [...(payload.gallery || []), ...gallery].filter(isMediaGalleryItem);
          activeJobId = payload.job.id;
          appendLogLine(`[api] Created ${payload.media_type} job ${payload.job.id}`);
          appendLogLine(`[media] ${payload.message}`);
          if (payload.command) appendLogLine(`[next] ${payload.command}`);
        }
        const latestPayload = createdPayloads[createdPayloads.length - 1];
        hint.textContent = selectedMediaItems.length > 1 ? `${t("batchCreated")} ${createdPayloads.length}` : latestPayload.message;
        showMediaResult(latestPayload);
        if (latestPayload.output_url && userSettings.autoPreviewOutput) {
          window.open(latestPayload.output_url, "_blank", "noopener");
        }
        if ((latestPayload.output_folder || latestPayload.output_path) && userSettings.autoOpenOutputFolder) {
          openFolder(latestPayload.output_folder || latestPayload.output_path, "media output");
        }
        render();
        connectSseStream();
      } catch (error) {
        hint.textContent = error.message;
        appendLogLine(`[error] ${error.message}`);
      } finally {
        submit.disabled = false;
      }
    });

    function updateEditorMode(mediaType) {
      currentMediaType = mediaType;
      const submit = document.getElementById("mediaSubmit");
      const hint = document.getElementById("formHint");
      const adaptiveTitle = document.getElementById("adaptiveOptionsTitle");
      const adaptiveHelp = document.getElementById("adaptiveOptionsHelp");
      if (mediaType === "image") {
        imageOptions.classList.remove("hidden");
        adaptiveOptions.classList.remove("hidden");
        videoOptions.classList.add("hidden");
        submit.innerHTML = `<i data-lucide="sparkles" class="h-5 w-5"></i>${escapeHtml(t("createImage"))}`;
        mediaPrompt.placeholder = t("imagePromptPlaceholder");
        if (adaptiveTitle) adaptiveTitle.textContent = t("imageWorkflowTitle");
        if (adaptiveHelp) adaptiveHelp.textContent = t("imageWorkflowHelp");
        hint.textContent = t("imageReadyHint");
      } else if (mediaType === "video") {
        imageOptions.classList.add("hidden");
        adaptiveOptions.classList.remove("hidden");
        videoOptions.classList.remove("hidden");
        submit.innerHTML = `<i data-lucide="sparkles" class="h-5 w-5"></i>${escapeHtml(t("createVideo"))}`;
        mediaPrompt.placeholder = t("videoPromptPlaceholder");
        if (adaptiveTitle) adaptiveTitle.textContent = t("videoWorkflowTitle");
        if (adaptiveHelp) adaptiveHelp.textContent = t("videoWorkflowHelp");
        hint.textContent = t("videoReadyHint");
      } else {
        imageOptions.classList.add("hidden");
        adaptiveOptions.classList.remove("hidden");
        videoOptions.classList.add("hidden");
        submit.innerHTML = `<i data-lucide="sparkles" class="h-5 w-5"></i>${escapeHtml(t("createEdit"))}`;
        mediaPrompt.placeholder = t("genericPromptPlaceholder");
        if (adaptiveTitle) adaptiveTitle.textContent = t("adaptiveOptionsTitle");
        if (adaptiveHelp) adaptiveHelp.textContent = t("adaptiveOptionsHelp");
        hint.textContent = "";
      }
      lucide.createIcons();
    }

    function hideMediaResult() {
      mediaResult.classList.add("hidden");
      mediaResultPath.textContent = "";
      mediaResultLink.href = "#";
      mediaResultLink.classList.add("pointer-events-none", "opacity-50");
      lastMediaOutputFolder = "";
    }

    function showMediaResult(payload) {
      const outputPath = payload.output_path || "";
      const outputFolder = payload.output_folder || "";
      const outputUrl = payload.output_url || "";
      if (!outputPath && !outputFolder) return;
      lastMediaOutputFolder = outputFolder || outputPath;
      mediaResultPath.textContent = outputPath || outputFolder;
      mediaResultLink.href = outputUrl || "#";
      mediaResultLink.classList.toggle("pointer-events-none", !outputUrl);
      mediaResultLink.classList.toggle("opacity-50", !outputUrl);
      mediaResult.classList.remove("hidden");
    }

    function isMediaGalleryItem(item) {
      return item && ["image", "video", "audio"].includes(item.kind);
    }

    function detectClientMediaType(file) {
      if ((file.type || "").startsWith("image/")) return "image";
      if ((file.type || "").startsWith("video/")) return "video";
      const name = file.name.toLowerCase();
      if (/\.(png|jpe?g|webp|bmp)$/i.test(name)) return "image";
      if (/\.(mp4|mov|mkv|webm)$/i.test(name)) return "video";
      return "unknown";
    }

    function fileToBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("讀取檔案失敗"));
        reader.onload = () => {
          const value = String(reader.result || "");
          resolve(value.includes(",") ? value.split(",", 2)[1] : value);
        };
        reader.readAsDataURL(file);
      });
    }

    function formatBytes(bytes) {
      if (!Number.isFinite(bytes)) return "unknown size";
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    }

    async function openFolderById(artifactId) {
      const item = gallery.find(entry => entry.id === artifactId);
      if (!item) {
        appendLogLine(`[folder] Artifact not found: ${artifactId}`);
        return;
      }
      await openFolder(item.folderPath, item.relativePath);
    }

    async function openFolder(folderPath, label = "") {
      appendLogLine(`[folder] ${folderPath || "No folder path supplied"}`);
      try {
        const response = await fetch("/api/open-folder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: folderPath, label })
        });
        const payload = await response.json();
        appendLogLine(`[folder] ${payload.message || payload.status}`);
      } catch (error) {
        appendLogLine(`[folder] ${error.message}`);
      }
    }

    function requestDeleteArtifact(artifactId) {
      pendingDeleteId = pendingDeleteId === artifactId ? "" : artifactId;
      renderGallery();
      lucide.createIcons();
    }

    function cancelDeleteArtifact() {
      pendingDeleteId = "";
      renderGallery();
      lucide.createIcons();
    }

    function deleteConfirmation(item) {
      if (pendingDeleteId !== item.id) return "";
      return `
        <div class="mt-3 rounded-lg border border-red-300/30 bg-red-300/10 p-3">
          <p class="text-xs font-bold leading-5 text-red-100">${escapeHtml(t("deletePrompt"))}</p>
          <div class="mt-3 grid grid-cols-2 gap-2">
            <button class="rounded-lg border border-white/10 px-3 py-2 text-xs font-black text-slate-200 hover:bg-white/5" onclick="cancelDeleteArtifact()">${escapeHtml(t("cancelDelete"))}</button>
            <button class="rounded-lg bg-red-300 px-3 py-2 text-xs font-black text-slate-950 hover:bg-red-200" onclick="deleteArtifactById('${escapeAttr(item.id)}')">${escapeHtml(t("confirmDelete"))}</button>
          </div>
        </div>
      `;
    }

    async function deleteArtifactById(artifactId) {
      const item = gallery.find(entry => entry.id === artifactId);
      if (!item) {
        appendLogLine(`[delete] Artifact not found: ${artifactId}`);
        pendingDeleteId = "";
        renderGallery();
        return;
      }
      try {
        const response = await fetch("/api/delete-artifact", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: item.jobId, path: item.relativePath })
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || "Delete failed");
        gallery = gallery.filter(entry => entry.id !== artifactId);
        if (payload.gallery) gallery = [...payload.gallery].filter(isMediaGalleryItem);
        pendingDeleteId = "";
        appendLogLine(`[delete] ${payload.message || t("deleteMoved")} ${item.relativePath}`);
        renderGallery();
        refreshJobs().catch(error => appendLogLine(`[delete] refresh failed: ${error.message}`));
      } catch (error) {
        appendLogLine(`[delete] ${error.message}`);
      }
    }

    function galleryPreview(item) {
      const safeId = escapeAttr(item.id);
      const safeUrl = escapeAttr(item.mediaUrl || "");
      const safeName = escapeAttr(item.fileName || "");
      const badge = escapeHtml(t(`${item.kind}Label`) || item.kind);
      const duration = escapeHtml(item.duration || "");
      const previewSpacing = userSettings.galleryDensity === "compact" ? "mb-3" : "mb-4";
      if (item.kind === "image") {
        return `
          <button class="group relative ${previewSpacing} block aspect-video w-full overflow-hidden rounded-lg border border-white/10 bg-black/45 text-left" onclick="playArtifactById('${safeId}')" title="${safeName}">
            <img src="${safeUrl}" class="h-full w-full object-contain transition duration-200 group-hover:scale-[1.02]" alt="${safeName}" loading="lazy">
            <span class="absolute left-3 top-3 rounded-md bg-black/70 px-2 py-1 text-xs font-black text-white">${badge}</span>
            <span class="absolute bottom-3 right-3 rounded-md bg-sky-300/90 px-2 py-1 text-xs font-black text-slate-950">${escapeHtml(t("previewImage"))}</span>
          </button>
        `;
      }
      if (item.kind === "video") {
        return `
          <button class="group relative ${previewSpacing} block aspect-video w-full overflow-hidden rounded-lg border border-white/10 bg-black text-left" onclick="playArtifactById('${safeId}')" title="${safeName}">
            <video src="${safeUrl}" class="h-full w-full object-contain opacity-90 transition duration-200 group-hover:opacity-100" muted playsinline preload="metadata"></video>
            <span class="absolute left-3 top-3 rounded-md bg-black/70 px-2 py-1 text-xs font-black text-white">${badge}</span>
            <span class="absolute bottom-3 left-3 rounded-md bg-black/70 px-2 py-1 text-xs font-black text-white">${duration}</span>
            <span class="absolute bottom-3 right-3 rounded-full bg-lime-300 p-2 text-slate-950"><i data-lucide="play" class="h-4 w-4"></i></span>
          </button>
        `;
      }
      return `
        <button class="group relative ${previewSpacing} grid aspect-video w-full place-items-center overflow-hidden rounded-lg border border-white/10 bg-gradient-to-br ${galleryGradient(item.accent)} px-4 py-5 text-left" onclick="playArtifactById('${safeId}')" title="${safeName}">
          <span class="absolute left-3 top-3 rounded-md bg-black/70 px-2 py-1 text-xs font-black text-white">${badge}</span>
          <span class="grid gap-2 text-center">
            <span class="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-black/45 text-lime-200 ring-1 ring-lime-300/30">
              <i data-lucide="volume-2" class="h-6 w-6"></i>
            </span>
            <span class="mx-auto flex h-10 items-end gap-1.5 rounded-xl bg-black/30 px-4 py-2">
              <span class="audio-wave-bar h-6 w-2 rounded-full bg-lime-300"></span>
              <span class="audio-wave-bar h-8 w-2 rounded-full bg-sky-300"></span>
              <span class="audio-wave-bar h-5 w-2 rounded-full bg-orange-300"></span>
              <span class="audio-wave-bar h-9 w-2 rounded-full bg-lime-200"></span>
              <span class="audio-wave-bar h-6 w-2 rounded-full bg-sky-200"></span>
            </span>
            <span class="rounded-md bg-black/55 px-2 py-1 text-xs font-black text-white">${escapeHtml(t("audioHint"))}</span>
          </span>
          <span class="absolute bottom-3 left-3 rounded-md bg-black/70 px-2 py-1 text-xs font-black text-white">${duration}</span>
        </button>
      `;
    }

    function playArtifactById(artifactId) {
      const item = gallery.find(entry => entry.id === artifactId);
      if (!item) {
        appendLogLine(`[player] Artifact not found: ${artifactId}`);
        return;
      }
      playLocal(item.mediaUrl, item.fileName, item.kind);
    }

    function playLocal(mediaUrl, fileName, kind) {
      if (!mediaUrl) {
        appendLogLine(`[player] Missing media URL for ${fileName}`);
        return;
      }
      appendLogLine(`[player] Opening ${fileName}`);
      const modal = document.getElementById("mediaModal");
      const title = document.getElementById("mediaTitle");
      const video = document.getElementById("mediaVideo");
      const audio = document.getElementById("mediaAudio");
      const image = document.getElementById("mediaImage");
      title.textContent = fileName;
      video.pause();
      audio.pause();
      video.classList.add("hidden");
      audio.classList.add("hidden");
      image.classList.add("hidden");
      if (kind === "audio") {
        audio.src = mediaUrl;
        audio.classList.remove("hidden");
        audio.play().catch(() => appendLogLine("[player] Browser blocked autoplay; press play manually."));
      } else if (kind === "image") {
        image.src = mediaUrl;
        image.classList.remove("hidden");
      } else {
        video.src = mediaUrl;
        video.classList.remove("hidden");
        video.play().catch(() => appendLogLine("[player] Browser blocked autoplay; press play manually."));
      }
      modal.classList.remove("hidden");
    }

    function closeMediaModal() {
      const modal = document.getElementById("mediaModal");
      const video = document.getElementById("mediaVideo");
      const audio = document.getElementById("mediaAudio");
      const image = document.getElementById("mediaImage");
      video.pause();
      audio.pause();
      video.removeAttribute("src");
      audio.removeAttribute("src");
      image.removeAttribute("src");
      modal.classList.add("hidden");
    }

    function galleryIcon(kind) {
      if (kind === "video") return "film";
      if (kind === "audio") return "audio-lines";
      if (kind === "image") return "image";
      if (kind === "json") return "file-json";
      return "file";
    }

    function connectSseStream() {
      if (!window.EventSource) {
        appendLogLine("[sse] Browser does not support EventSource.");
        return;
      }
      if (eventSource) eventSource.close();
      const url = `/api/stream-logs?job=${encodeURIComponent(activeJobId || "")}`;
      eventSource = new EventSource(url);
      appendLogLine(`[sse] connecting ${url}`);
      eventSource.addEventListener("open", () => appendLogLine("[sse] connected"));
      eventSource.addEventListener("log", event => handleStreamEvent(event));
      eventSource.addEventListener("status", event => handleStreamEvent(event));
      eventSource.addEventListener("heartbeat", event => handleStreamEvent(event));
      eventSource.onmessage = event => handleStreamEvent(event);
      eventSource.onerror = () => {
        appendLogLine("[sse] connection interrupted; browser will retry automatically");
      };
    }

    function handleStreamEvent(event) {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        appendLogLine(event.data, false);
        return;
      }
      if (payload.type === "status") {
        updateJobPhase(payload);
        renderStats();
        renderPipeline();
        lucide.createIcons();
        if (payload.step === "render" && payload.status === "completed") {
          setTimeout(refreshJobs, 700);
        }
        return;
      }
      if (payload.type === "heartbeat") {
        appendLogLine(`${payload.message} · ${payload.created_at}`, false);
        return;
      }
      appendLogLine(payload.message || JSON.stringify(payload), false);
    }

    function persistConsoleLog(line) {
      fetch("/api/console-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: activeJobId || "",
          message: stripAnsi(String(line))
        }),
        keepalive: true
      }).catch(() => {});
    }

    function updateJobPhase(payload) {
      const jobId = payload.job_id || activeJobId || "sse_demo";
      let job = jobs.find(item => item.id === jobId);
      if (!job) {
        job = {
          id: jobId,
          status: "running",
          profile: "sse-stream",
          videoPath: "live pipeline stream",
          outputDirectory: "output\\jobs",
          updatedAt: payload.created_at,
          phases: defaultPhases()
        };
        jobs = [job, ...jobs];
        activeJobId = jobId;
        renderJobs();
      }
      const phase = job.phases.find(item => item.key === payload.step);
      if (phase) {
        phase.status = payload.status || phase.status;
        phase.progress = Number(payload.progress || 0);
      }
      job.status = job.phases.some(item => item.status === "cancelled")
        ? "cancelled"
        : job.phases.some(item => item.status === "failed")
        ? "failed"
        : job.phases.every(item => item.status === "completed") ? "completed" : "running";
      job.updatedAt = payload.created_at || new Date().toISOString();
    }

    function defaultPhases() {
      return [
        { key: "asr", label: "語音識別", detail: "ASR - Faster-Whisper", status: "waiting", progress: 0 },
        { key: "llm", label: "語意理解", detail: "LLM - Ollama", status: "waiting", progress: 0 },
        { key: "vision", label: "智慧裁剪", detail: "Vision", status: "waiting", progress: 0 },
        { key: "render", label: "影音渲染", detail: "FFmpeg", status: "waiting", progress: 0 }
      ];
    }

    function ansiToHtml(value) {
      const escaped = escapeHtml(value);
      return escaped
        .replace(/\u001b\[32m/g, '<span class="text-lime-300">')
        .replace(/\u001b\[36m/g, '<span class="text-sky-300">')
        .replace(/\u001b\[33m/g, '<span class="text-orange-300">')
        .replace(/\u001b\[31m/g, '<span class="text-red-300">')
        .replace(/\u001b\[0m/g, "</span>")
        .replace(/\u001b\[[0-9;]*m/g, "");
    }

    function stripAnsi(value) {
      return value.replace(/\u001b\[[0-9;]*m/g, "");
    }

    function statusBadge(status) {
      if (status === "completed") return "bg-lime-300/15 text-lime-200";
      if (status === "running") return "bg-sky-300/15 text-sky-200";
      if (status === "failed") return "bg-red-300/15 text-red-200";
      if (status === "interrupted") return "bg-orange-300/15 text-orange-200";
      if (status === "incomplete") return "bg-slate-700/50 text-slate-300";
      if (status === "cancelled" || status === "cancelling") return "bg-orange-300/15 text-orange-200";
      return "bg-orange-300/15 text-orange-200";
    }

    function statusTextClass(status) {
      if (status === "completed") return "text-lime-300";
      if (status === "running") return "text-sky-300";
      if (status === "failed") return "text-red-300";
      if (status === "interrupted" || status === "cancelled" || status === "cancelling" || status === "queued") return "text-orange-300";
      if (status === "incomplete") return "text-slate-400";
      return "text-slate-300";
    }

    function statusLabel(status) {
      const key = {
        running: "statusRunning",
        completed: "statusCompleted",
        queued: "statusQueued",
        failed: "statusFailed",
        cancelled: "statusCancelled",
        cancelling: "statusCancelling",
        interrupted: "statusInterrupted",
        incomplete: "statusIncomplete"
      }[status];
      return key ? t(key) : String(status || "--");
    }

    function galleryGradient(accent) {
      if (accent === "lime") return "from-lime-300/35 via-slate-800 to-black";
      if (accent === "orange") return "from-orange-300/35 via-slate-800 to-black";
      return "from-sky-300/35 via-slate-800 to-black";
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }
    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, "&#96;");
    }

    renderSelectedMedia();
    render();
    loadSettingsFromServer();
    connectSseStream();
  </script>
</body>
</html>"""
    return (
        template.replace("__INITIAL_JOBS__", safe_script_json(initial_jobs))
        .replace("__INITIAL_GALLERY__", safe_script_json(initial_gallery))
        .replace("__SELECTED_JOB_ID__", html.escape(selected_id, quote=True))
    )


def render_workspace(job: dict, env: dict) -> str:
    manifest = job["manifest"]
    job_id = manifest.get("job_id", job["dir"].name)
    video = job.get("video_info") or {}
    audio = job.get("audio_info") or {}
    transcript = job.get("transcript_info") or {}
    segments = job.get("segments_info") or {}
    candidates = job.get("clip_candidates") or {}
    validated = job.get("validated_clips") or {}
    input_path = manifest.get("input", {}).get("source_path")
    video_src = media_url(job_id, input_path) if input_path else None
    completed, total = stage_progress(manifest)
    next_command = next_command_for_job(job_id, manifest)

    preview = (
        f'<video class="preview-video" controls src="{video_src}"></video>'
        if video_src
        else '<div class="preview-empty">尚未匯入影片</div>'
    )

    raw_links = render_raw_links(job)
    return f"""
    <main class="workspace">
      <section class="panel control-panel">
        <div class="panel-title">
          <div>
            <p class="eyebrow">Current Job</p>
            <h2>{html.escape(job_id)}</h2>
          </div>
          <span class="status-pill">{completed}/{total}</span>
        </div>
        <div class="progress-track">
          <span style="width:{progress_percent(completed, total)}%"></span>
        </div>
        <ol class="timeline">{render_timeline(manifest)}</ol>
        <div class="next-box">
          <p class="eyebrow">Next</p>
          <strong>{html.escape(next_command["label"])}</strong>
          <code>{html.escape(next_command["command"])}</code>
        </div>
      </section>

      <section class="panel preview-panel">
        <div class="panel-title">
          <div>
            <p class="eyebrow">Input Preview</p>
            <h2>{html.escape(manifest.get("input", {}).get("original_filename", "未命名影片"))}</h2>
          </div>
          <span class="status-pill">{html.escape(str(manifest.get("status", "running")))}</span>
        </div>
        <div class="preview-stage">{preview}</div>
        <div class="metric-grid">
          {metric("Duration", format_seconds(video.get("duration")))}
          {metric("Resolution", format_resolution(video))}
          {metric("FPS", format_value(video.get("fps")))}
          {metric("Video", video.get("video_codec", "unknown"))}
          {metric("Source Audio", video.get("audio_codec", "unknown"))}
          {metric("ASR Audio", format_audio(audio))}
        </div>
      </section>

      <section class="panel insight-panel">
        <div class="panel-title">
          <div>
            <p class="eyebrow">Platform</p>
            <h2>{tooltip(platform_badge(env))}</h2>
          </div>
        </div>
        {render_platform_summary(env)}
        {render_transcript_summary(transcript)}
        {render_clip_summary(segments, candidates, validated)}
        <div class="artifact-summary">
          <h3>Artifacts</h3>
          {raw_links}
        </div>
      </section>
    </main>
    """


def render_empty_workspace(env: dict) -> str:
    return f"""
    <main class="workspace empty-workspace">
      <section class="panel control-panel">
        <p class="eyebrow">Ready</p>
        <h2>建立第一個 job</h2>
        <div class="next-box">
          <strong>先分析影片</strong>
          <code>python main.py analyze --config configs\\qualcomm_windows_arm64.yaml --input path\\to\\video.mp4 --job-id demo001</code>
        </div>
      </section>
      <section class="panel preview-panel">
        <div class="preview-stage"><div class="preview-empty">等待影片輸入</div></div>
      </section>
      <section class="panel insight-panel">{render_platform_summary(env)}</section>
    </main>
    """


def render_timeline(manifest: dict) -> str:
    stages = manifest.get("stages", {})
    items = []
    previous_complete = True
    for key, label in STAGE_LABELS:
        status = stages.get(key)
        if status == "completed":
            state = "done"
            text = "完成"
        elif status == "running":
            state = "running"
            text = "處理中"
        elif previous_complete:
            state = "next"
            text = "下一步"
        else:
            state = "locked"
            text = "等待"
        previous_complete = status == "completed"
        items.append(f'<li class="{state}"><span>{tooltip(label)}</span><strong>{html.escape(text)}</strong></li>')
    return "\n".join(items)


def render_platform_summary(env: dict) -> str:
    level = env.get("hardware_level", {})
    qualcomm = env.get("qualcomm_platform", {})
    amd = env.get("amd_platform", {})
    intel = env.get("intel_platform", {})
    q_ready = env.get("qualcomm_readiness", {})
    amd_ready = env.get("amd_readiness", {})
    intel_ready = env.get("intel_readiness", {})
    n_ready = env.get("nvidia_readiness", {})
    ffmpeg = env.get("ffmpeg_codecs", {})
    rows = [
        ("Hardware", level.get("level", "unknown")),
        ("Python", "x64 emulation" if qualcomm.get("likely_emulated_python") else "native"),
        ("Encoder", platform_encoder_summary(ffmpeg)),
    ]
    hardware_level = level.get("level")
    if hardware_level == "Q":
        rows[1:1] = [
            ("Qualcomm", yes_no(qualcomm.get("is_qualcomm_cpu"))),
            ("Q Readiness", q_ready.get("tier", "unknown")),
            ("QNN EP", yes_no(qualcomm.get("qnn_execution_provider_available"))),
            ("DirectML EP", yes_no(qualcomm.get("directml_execution_provider_available"))),
        ]
    elif hardware_level == "AM":
        rows[1:1] = [
            ("AMD", yes_no(amd.get("is_amd_cpu") or amd.get("is_amd_gpu"))),
            ("AMD Readiness", amd_ready.get("tier", "unknown")),
            ("DirectML EP", yes_no(amd.get("directml_execution_provider_available"))),
            ("ROCm EP", yes_no(amd.get("rocm_execution_provider_available"))),
        ]
    elif hardware_level == "I":
        rows[1:1] = [
            ("Intel", yes_no(intel.get("is_intel_cpu") or intel.get("is_intel_gpu"))),
            ("Intel Readiness", intel_ready.get("tier", "unknown")),
            ("OpenVINO", yes_no(intel.get("openvino_execution_provider_available"))),
            ("DirectML EP", yes_no(intel.get("directml_execution_provider_available"))),
        ]
    elif hardware_level in {"A", "B", "C"}:
        rows[1:1] = [
            ("NVIDIA Readiness", n_ready.get("tier", "unknown")),
            ("CUDA", yes_no(env.get("gpu_cuda", {}).get("cuda_usable"))),
            ("NVENC", yes_no(ffmpeg.get("platform_encoders", {}).get("nvidia_nvenc_usable"))),
        ]
    return '<div class="facts">' + "\n".join(render_fact(name, value) for name, value in rows) + "</div>"


def render_transcript_summary(transcript: dict) -> str:
    if not transcript:
        return """
        <div class="transcript-card">
          <h3>ASR Preview</h3>
          <p class="muted">尚未產生逐字稿。下一步會跑 faster-whisper CPU int8。</p>
        </div>
        """

    segments = transcript.get("segments") or []
    text = " ".join(str(segment.get("text", "")).strip() for segment in segments).strip()
    if not text:
        text = "已完成 ASR，但這段音訊沒有偵測到可用語音文字。"
    preview = text[:260] + ("..." if len(text) > 260 else "")
    return f"""
    <div class="transcript-card">
      <h3>{tooltip("ASR")} Preview</h3>
      <div class="mini-stats">
        <span>{html.escape(str(transcript.get("language") or "unknown"))}</span>
        <span>{html.escape(str(transcript.get("segment_count", 0)))} segments</span>
        <span>{html.escape(str(transcript.get("word_count", 0)))} words</span>
      </div>
      <p>{html.escape(preview)}</p>
    </div>
    """


def render_clip_summary(segments: dict, candidates: dict, validated: dict) -> str:
    if not segments and not candidates and not validated:
        return """
        <div class="selection-card">
          <h3>Clip Selection</h3>
          <p class="muted">尚未產生語意切片。下一步會建立 segments、LLM input 與候選片段。</p>
        </div>
        """

    chunk_count = segments.get("chunk_count", 0)
    candidate_count = candidates.get("candidate_count", 0)
    selected_count = validated.get("selected_count", 0)
    reason = (
        validated.get("empty_reason")
        or candidates.get("empty_reason")
        or segments.get("empty_reason")
        or ""
    )
    candidate_rows = []
    for candidate in (candidates.get("candidates") or [])[:3]:
        candidate_rows.append(
            f"""
            <div class="candidate-row">
              <strong>{html.escape(candidate.get("title", "Untitled clip"))}</strong>
              <span>{html.escape(format_seconds(candidate.get("start")))} - {html.escape(format_seconds(candidate.get("end")))}</span>
              <p>{html.escape(candidate.get("text_preview", ""))}</p>
            </div>
            """
        )
    candidate_list = "\n".join(candidate_rows)
    if not candidate_list:
        candidate_list = f'<p class="muted">{html.escape(reason or "目前沒有候選片段。")}</p>'

    return f"""
    <div class="selection-card">
      <h3>Clip Selection</h3>
      <div class="selection-stats">
        <span>{tooltip("Semantic Chunks")} <strong>{html.escape(str(chunk_count))}</strong></span>
        <span>{tooltip("Clip Candidates")} <strong>{html.escape(str(candidate_count))}</strong></span>
        <span>{tooltip("Validated Clips")} <strong>{html.escape(str(selected_count))}</strong></span>
      </div>
      {candidate_list}
    </div>
    """


def render_raw_links(job: dict) -> str:
    items = []
    job_id = job["manifest"].get("job_id", job["dir"].name)
    for path in job["artifacts"]:
        title = human_artifact_name(path)
        items.append(
            f'<a class="artifact-chip" href="/artifact?job={quote(job_id)}&path={quote(path)}">'
            f"<span>{html.escape(title)}</span><small>{html.escape(path)}</small></a>"
        )
    return "\n".join(items) if items else '<p class="muted">尚無 artifact。</p>'


def render_job_row(job: dict, selected: dict | None) -> str:
    manifest = job["manifest"]
    job_id = manifest.get("job_id", job["dir"].name)
    video = job.get("video_info") or {}
    audio = job.get("audio_info") or {}
    completed, total = stage_progress(manifest)
    active = selected and selected["dir"] == job["dir"]
    return f"""
    <a class="job-row {'selected' if active else ''}" href="/?job={quote(job_id)}">
      <span class="job-name">{html.escape(job_id)}</span>
      <span>{html.escape(manifest.get("input", {}).get("original_filename", "unknown"))}</span>
      <span>{completed}/{total} stages</span>
      <span>{html.escape(format_resolution(video))}</span>
      <span>{html.escape(format_audio(audio))}</span>
    </a>
    """


def render_artifact(output_root: Path, job_id: str, artifact: str) -> str:
    target = resolve_job_path(output_root, job_id, artifact)
    if not target or not target.exists():
        content = "Artifact not found."
    else:
        content = target.read_text(encoding="utf-8", errors="replace")

    return page(
        f"{job_id} / {artifact}",
        f"""
        <div class="raw-page">
          <header class="topbar raw-top">
            <div>
              <p class="eyebrow">Raw Artifact</p>
              <h1>{html.escape(job_id)} / {html.escape(artifact)}</h1>
            </div>
            <a class="pill" href="/?job={quote(job_id)}">Back</a>
          </header>
          <pre>{html.escape(content)}</pre>
        </div>
        """,
    )


def get_current_env_report(cache_path: Path) -> dict:
    """Run the current project environment probe and cache the latest report."""
    try:
        import check_env

        report = check_env.build_report(
            SimpleNamespace(ollama_url="http://127.0.0.1:11434")
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    except Exception as exc:
        cached = read_json_or_none(cache_path) or {}
        cached["dashboard_probe_error"] = {
            "message": str(exc),
            "fallback": "Using cached output/env_report.json when available.",
        }
        return cached


def wants_json(accept_header: str, query: dict[str, list[str]]) -> bool:
    requested_format = (query.get("format") or query.get("as") or [""])[0].lower()
    if requested_format == "json":
        return True
    if "application/json" in accept_header and "text/html" not in accept_header:
        return True
    return False


def render_env_dashboard(report: dict) -> str:
    basic = report.get("basic", {})
    asr = report.get("asr", {})
    llm = report.get("llm", {})
    qualcomm = report.get("qualcomm_platform", {})
    emulated = bool(qualcomm.get("likely_emulated_python"))

    software_cards = "\n".join(
        [
            dependency_card("Python", basic.get("python", {}).get("version"), True, "terminal"),
            dependency_card("pip", basic.get("pip", {}).get("version"), basic.get("pip", {}).get("available"), "package"),
            dependency_card("Git", basic.get("git", {}).get("version"), basic.get("git", {}).get("available"), "git-branch"),
            dependency_card("FFmpeg", basic.get("ffmpeg", {}).get("version"), basic.get("ffmpeg", {}).get("available"), "film"),
            dependency_card("Pillow", report.get("image_edit", {}).get("pillow_version"), report.get("image_edit", {}).get("pillow_installed"), "image"),
            dependency_card("Ollama", llm.get("ollama_version"), llm.get("ollama_api", {}).get("available"), "brain-circuit"),
            dependency_card("Faster-Whisper", asr.get("ctranslate2_version"), asr.get("faster_whisper_installed"), "audio-lines"),
        ]
    )

    warning_html = ""
    if emulated:
        warning_html = f"""
        <section class="rounded-2xl border border-orange-400/50 bg-orange-500/15 p-5 shadow-2xl shadow-orange-950/30">
          <div class="flex items-start gap-4">
            <div class="rounded-xl bg-orange-400 p-3 text-black"><i data-lucide="triangle-alert" class="h-6 w-6"></i></div>
            <div>
              <h2 class="mt-1 text-2xl font-bold text-orange-50">目前在 Qualcomm ARM64 平台上模擬運行 x64 Python</h2>
              <p class="mt-2 max-w-4xl text-sm leading-6 text-orange-100">
                這會讓 ASR、OpenCV、ONNX Runtime、模型載入與影片處理承受明顯效能瓶頸。建議改用 native ARM64 Python、
                ARM64 wheels，並把未來可 ONNX 化的模型逐步導向 QNNExecutionProvider 或 DirectMLExecutionProvider。
              </p>
              <div class="mt-4 flex flex-wrap gap-2 text-xs font-semibold">
                <span class="rounded-full bg-orange-400 px-3 py-1 text-black">安裝 ARM64 Python</span>
                <span class="rounded-full bg-white/10 px-3 py-1 text-orange-100">避免 CUDA/NVENC 本機假設</span>
                <span class="rounded-full bg-white/10 px-3 py-1 text-orange-100">優先 CPU int8 + cache/resume</span>
              </div>
            </div>
          </div>
        </section>
        """

    html_doc = """
<!doctype html>
<html lang="zh-Hant" class="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dump2Done 環境診斷</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            panel: '#0d1117',
            panel2: '#151b23',
            limeok: '#a3e635',
            hotorange: '#fb923c',
            danger: '#f87171'
          }
        }
      }
    }
  </script>
</head>
<body class="min-h-screen bg-[#05070a] text-slate-100 antialiased">
  <div class="fixed inset-0 -z-10 bg-[radial-gradient(circle_at_top_right,rgba(163,230,53,0.16),transparent_30%),radial-gradient(circle_at_top_left,rgba(56,189,248,0.12),transparent_28%)]"></div>
  <main class="mx-auto max-w-7xl px-5 py-6 lg:px-8">
    <header class="mb-6 flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
      <div>
        <h1 class="text-3xl font-black tracking-tight md:text-5xl">環境診斷</h1>
      </div>
      <div class="flex flex-wrap gap-2">
        <a href="/" class="rounded-xl border border-slate-700 bg-panel2 px-4 py-2 text-sm font-bold text-slate-200 hover:border-sky-400">Back to Dashboard</a>
      </div>
    </header>

    __WARNING__

    __EXECUTIVE_SUMMARY__

    <section class="mt-5 grid gap-5 lg:grid-cols-[1.1fr_1.4fr]">
      __PRIORITY_SECTION__
      __LOCAL_DEPLOYMENT_SECTION__
    </section>

    <section class="mt-5 rounded-2xl border border-slate-800 bg-panel/70 p-4">
      <details>
        <summary class="cursor-pointer text-sm font-bold text-slate-400 hover:text-slate-200">
          細節
        </summary>
        <div class="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">__SOFTWARE_CARDS__</div>
        <a href="/env?format=json" class="mt-4 inline-flex rounded-lg border border-slate-700 px-3 py-2 text-xs font-bold text-slate-300 hover:border-lime-300/40 hover:text-lime-200">JSON</a>
      </details>
    </section>
  </main>
  <script>
    if (window.lucide) { window.lucide.createIcons(); }
  </script>
</body>
</html>
"""

    replacements = {
        "__WARNING__": warning_html,
        "__EXECUTIVE_SUMMARY__": env_executive_summary(report),
        "__PRIORITY_SECTION__": env_priority_section(report),
        "__LOCAL_DEPLOYMENT_SECTION__": local_deployment_section(report),
        "__SOFTWARE_CARDS__": software_cards,
    }
    for key, value in replacements.items():
        html_doc = html_doc.replace(key, value)
    return html_doc


def env_executive_summary(report: dict) -> str:
    basic = report.get("basic", {})
    qualcomm = report.get("qualcomm_platform", {})
    ffmpeg = report.get("ffmpeg_codecs", {})
    image = report.get("image_edit", {})
    asr = report.get("asr", {})
    llm = report.get("llm", {})
    hardware = report.get("hardware_level", {})

    emulated = bool(qualcomm.get("likely_emulated_python"))
    local_items = local_deployment_items(report)
    complete = sum(1 for item in local_items if item["state"] == "complete")
    total = len(local_items)
    platform_text = "Qualcomm Windows on ARM64" if qualcomm.get("is_qualcomm_cpu") else hardware.get("reason", "Unknown local platform")
    python_text = f"Python {basic.get('python', {}).get('version', 'unknown')}"
    python_state = "x64 模擬層，最優先修正" if emulated else "native 或非 ARM 模擬路線"
    runner_state = "可做本地圖片/影片 MVP" if image.get("pillow_installed") and basic.get("ffmpeg", {}).get("available") else "基礎 runner 依賴未完整"
    ai_state_parts = []
    ai_state_parts.append("ASR 已就緒" if asr.get("faster_whisper_installed") else "ASR 未完成")
    ai_state_parts.append("LLM 已連線" if llm.get("ollama_api", {}).get("available") else "LLM 未連線")
    ai_state = "、".join(ai_state_parts)
    cards = [
        executive_card(
            "平台判斷",
            "Qualcomm 本地路線" if qualcomm.get("is_qualcomm_cpu") else str(hardware.get("level", "Unknown")),
            str(platform_text),
            "cpu",
            "lime",
        ),
        executive_card(
            "最大阻塞",
            "Python Runtime",
            f"{python_text}；{python_state}",
            "terminal",
            "orange" if emulated else "lime",
        ),
        executive_card(
            "目前可用",
            runner_state,
            f"本地能力完成度 {complete}/{total}；CPU 編碼：{'可用' if ffmpeg.get('cpu_encoding_available') else '待確認'}。",
            "check-circle-2",
            "lime" if complete else "orange",
        ),
        executive_card(
            "AI 能力",
            ai_state,
            "ASR/LLM 影響未來自動理解與剪輯規劃；不阻塞目前 deterministic 圖片/影片輸出。",
            "brain-circuit",
            "sky",
        ),
    ]
    cards_html = "\n".join(cards)

    return f"""
    <section class="mt-5 rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl">
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-4">{cards_html}</div>
    </section>
    """


def executive_card(title: str, value: str, description: str, icon: str, tone: str) -> str:
    tone_classes = tone_palette(tone)
    return f"""
    <article class="rounded-xl border border-slate-800 bg-slate-950/60 p-4">
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs font-bold uppercase tracking-wide text-slate-500">{html.escape(title)}</p>
          <h3 class="mt-2 text-lg font-black {tone_classes['text']}">{html.escape(value)}</h3>
        </div>
        <span class="rounded-lg {tone_classes['soft']} p-2 {tone_classes['text']}"><i data-lucide="{icon}" class="h-5 w-5"></i></span>
      </div>
      <p class="mt-3 text-xs leading-5 text-slate-400">{html.escape(description)}</p>
    </article>
    """


def env_summary_card(title: str, value: object, description: object, icon: str, tone: str) -> str:
    tone_classes = tone_palette(tone)
    return f"""
    <article class="rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl lg:col-span-1">
      <div class="mb-4 flex items-center justify-between">
        <p class="text-xs font-bold uppercase tracking-wide text-slate-500">{html.escape(title)}</p>
        <span class="rounded-xl {tone_classes['soft']} p-2 {tone_classes['text']}"><i data-lucide="{icon}" class="h-5 w-5"></i></span>
      </div>
      <div class="text-3xl font-black {tone_classes['text']}">{html.escape(str(value))}</div>
      <p class="mt-2 text-sm leading-5 text-slate-400">{html.escape(str(description))}</p>
    </article>
    """


def progress_card(
    title: str,
    value: str,
    percent: float,
    icon: str,
    tone: str,
    subtitle: str | None = None,
) -> str:
    tone_classes = tone_palette(tone)
    return f"""
    <article class="rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl">
      <div class="mb-4 flex items-center justify-between">
        <p class="text-xs font-bold uppercase tracking-wide text-slate-500">{html.escape(title)}</p>
        <span class="rounded-xl {tone_classes['soft']} p-2 {tone_classes['text']}"><i data-lucide="{icon}" class="h-5 w-5"></i></span>
      </div>
      <div class="flex items-end justify-between gap-3">
        <div class="text-3xl font-black">{html.escape(value)}</div>
        <div class="text-xs font-semibold text-slate-500">{html.escape(subtitle or '')}</div>
      </div>
      <div class="mt-4 h-3 overflow-hidden rounded-full bg-slate-800">
        <div class="h-full rounded-full {tone_classes['bar']}" style="width: {clamp_percent(percent):.1f}%"></div>
      </div>
    </article>
    """


def readiness_card(title: str, readiness: dict, blockers: list, icon: str, tone: str) -> str:
    tone_classes = tone_palette(tone)
    tier = readiness.get("tier", "unknown")
    score = readiness.get("score", "unknown")
    meaning = readiness.get("meaning", "No readiness details available.")
    blocker_items = "\n".join(
        f"""
        <li class="flex gap-2 rounded-xl border border-slate-800 bg-slate-950/60 p-3 text-sm text-slate-300">
          <i data-lucide="circle-alert" class="mt-0.5 h-4 w-4 shrink-0 text-orange-300"></i>
          <span>{html.escape(str(blocker))}</span>
        </li>
        """
        for blocker in blockers
        if blocker
    )
    if not blocker_items:
        blocker_items = """
        <li class="flex gap-2 rounded-xl border border-lime-300/20 bg-lime-300/10 p-3 text-sm text-lime-100">
          <i data-lucide="check-circle-2" class="mt-0.5 h-4 w-4 shrink-0 text-lime-300"></i>
          <span>No major blockers detected.</span>
        </li>
        """

    return f"""
    <article class="rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl">
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="text-xs font-bold uppercase tracking-wide text-slate-500">{html.escape(title)}</p>
          <h2 class="mt-2 text-3xl font-black {tone_classes['text']}">{html.escape(str(tier))}</h2>
        </div>
        <span class="rounded-xl {tone_classes['soft']} p-3 {tone_classes['text']}"><i data-lucide="{icon}" class="h-6 w-6"></i></span>
      </div>
      <div class="mt-4 grid grid-cols-2 gap-3">
        <div class="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
          <p class="text-xs text-slate-500">Score</p>
          <p class="mt-1 text-2xl font-black">{html.escape(str(score))}</p>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
          <p class="text-xs text-slate-500">Tier</p>
          <p class="mt-1 text-2xl font-black">{html.escape(str(tier))}</p>
        </div>
      </div>
      <p class="mt-4 text-sm leading-6 text-slate-400">{html.escape(str(meaning))}</p>
      <h3 class="mt-5 text-xs font-bold uppercase tracking-wide text-slate-500">Blockers</h3>
      <ul class="mt-3 grid gap-2">{blocker_items}</ul>
    </article>
    """


def env_priority_section(report: dict) -> str:
    qualcomm = report.get("qualcomm_platform", {})
    basic = report.get("basic", {})
    ffmpeg = report.get("ffmpeg_codecs", {})
    asr = report.get("asr", {})
    llm = report.get("llm", {})

    priorities = []
    if qualcomm.get("likely_emulated_python"):
        priorities.append(
            (
                "critical",
                "改成 native ARM64 Python",
                "目前很可能用 x64 Python 模擬層，這會拖慢 ASR、影像處理與 ONNX runtime。這是本機效能最優先項。",
            )
        )
    priorities.append(
        (
            "ok" if basic.get("ffmpeg", {}).get("available") and ffmpeg.get("cpu_encoding_available") else "critical",
            "保留 CPU FFmpeg 輸出路線",
            "目前 Qualcomm 本機先以 FFmpeg + libx264 驗證穩定輸出，避免把 CUDA/NVENC 當成本機前提。",
        )
    )
    if not asr.get("faster_whisper_installed"):
        priorities.append(
            (
                "warn",
                "補齊本地 ASR",
                "Faster-Whisper 會影響未來語音轉錄與剪輯理解；未完成時仍可跑基本圖片/影片輸出。",
            )
        )
    if not llm.get("ollama_api", {}).get("available"):
        priorities.append(
            (
                "future",
                "接上本地 LLM",
                "Ollama 或相容端點會負責 prompt 解析、剪輯計畫與 Agent 任務拆解。",
            )
        )

    items = "\n".join(priority_item(state, title, body) for state, title, body in priorities[:6])
    return f"""
    <article class="rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl">
      <div class="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 class="text-xl font-black">優先處理</h2>
        </div>
        <i data-lucide="list-checks" class="h-6 w-6 text-orange-300"></i>
      </div>
      <div class="grid gap-3">{items}</div>
    </article>
    """


def priority_item(state: str, title: str, body: str) -> str:
    palette = {
        "critical": ("border-orange-300/35 bg-orange-300/10", "text-orange-200", "circle-alert"),
        "warn": ("border-yellow-300/30 bg-yellow-300/10", "text-yellow-200", "triangle-alert"),
        "ok": ("border-lime-300/25 bg-lime-300/10", "text-lime-200", "check-circle-2"),
        "future": ("border-sky-300/25 bg-sky-300/10", "text-sky-200", "clock-3"),
    }.get(state, ("border-slate-700 bg-slate-950/60", "text-slate-200", "circle"))
    return f"""
    <div class="rounded-xl border {palette[0]} p-3">
      <div class="flex items-start gap-3">
        <i data-lucide="{palette[2]}" class="mt-0.5 h-4 w-4 shrink-0 {palette[1]}"></i>
        <div>
          <h3 class="text-sm font-black {palette[1]}">{html.escape(title)}</h3>
          <p class="mt-1 text-xs leading-5 text-slate-400">{html.escape(body)}</p>
        </div>
      </div>
    </div>
    """


def local_deployment_section(report: dict) -> str:
    items = local_deployment_items(report)
    rows = "\n".join(local_deployment_row(item) for item in items)
    complete = sum(1 for item in items if item["state"] == "complete")
    mvp = sum(1 for item in items if item["state"] == "mvp")
    badge_text = f"{complete} 完成" + (f" · {mvp} MVP" if mvp else "")
    return f"""
    <article class="rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl">
      <div class="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 class="text-xl font-black">本地能力</h2>
        </div>
        <span class="rounded-xl border border-lime-300/25 bg-lime-300/10 px-3 py-2 text-xs font-black text-lime-200">{badge_text}</span>
      </div>
      <div class="grid gap-3 md:grid-cols-2">{rows}</div>
    </article>
    """


def local_deployment_items(report: dict) -> list[dict[str, str]]:
    basic = report.get("basic", {})
    image = report.get("image_edit", {})
    asr = report.get("asr", {})
    llm = report.get("llm", {})
    qualcomm = report.get("qualcomm_platform", {})
    ffmpeg = report.get("ffmpeg_codecs", {})
    return [
        {
            "name": "圖片本地編輯",
            "state": "complete" if image.get("pillow_installed") else "missing",
            "detail": "Pillow 已可處理旋轉、亮度、黑白、銳化並輸出圖片。" if image.get("pillow_installed") else "Pillow 未安裝，圖片功能會降級。",
        },
        {
            "name": "影片本地 runner",
            "state": "complete" if basic.get("ffmpeg", {}).get("available") else "missing",
            "detail": "已接 FFmpeg 抽幀、Pillow 逐幀處理、MP4 合成輸出。" if basic.get("ffmpeg", {}).get("available") else "需要 FFmpeg 才能輸出影片。",
        },
        {
            "name": "CPU 影片編碼",
            "state": "complete" if ffmpeg.get("cpu_encoding_available") else "missing",
            "detail": "libx264/libx265 可作為 Qualcomm 本機穩定輸出路線。",
        },
        {
            "name": "本地 ASR",
            "state": "complete" if asr.get("faster_whisper_installed") else "missing",
            "detail": "Faster-Whisper 可做本地轉錄。" if asr.get("faster_whisper_installed") else "尚未安裝 Faster-Whisper；語音理解功能未完成。",
        },
        {
            "name": "本地 LLM",
            "state": "complete" if llm.get("ollama_api", {}).get("available") else "missing",
            "detail": "Ollama API 可用，可作為未來 prompt planning 路線。" if llm.get("ollama_api", {}).get("available") else "Ollama API 未連通；目前不影響基本輸出。",
        },
        {
            "name": "QNN / NPU 加速",
            "state": "complete" if qualcomm.get("qnn_execution_provider_available") else "setup" if qualcomm.get("is_qualcomm_cpu") else "blocked",
            "detail": "QNNExecutionProvider 已可用，可接 ONNX 化模型驗證 NPU 路線。"
            if qualcomm.get("qnn_execution_provider_available")
            else "尚未偵測到 QNNExecutionProvider；需安裝/驗證 Qualcomm QNN SDK 與支援 QNN 的 ONNX Runtime。"
            if qualcomm.get("is_qualcomm_cpu")
            else "非 Qualcomm 平台時不適用。",
        },
        {
            "name": "DirectML 視覺模型",
            "state": "complete" if qualcomm.get("directml_execution_provider_available") else "setup",
            "detail": "DmlExecutionProvider 已可用，可作為 ONNX segmentation/tracking 的 Windows GPU fallback。"
            if qualcomm.get("directml_execution_provider_available")
            else "尚未偵測到 DmlExecutionProvider；需安裝 onnxruntime-directml 並用小型 ONNX vision model 驗證。",
        },
        {
            "name": "人物/衣服追蹤",
            "state": "mvp",
            "detail": "已支援中央區域 deterministic 衣服變白 MVP；精準 segmentation + tracking 尚待接入。",
        },
    ]


def local_deployment_row(item: dict[str, str]) -> str:
    state = item.get("state", "future")
    label_map = {
        "complete": ("完成", "text-lime-200", "border-lime-300/25 bg-lime-300/10", "check"),
        "missing": ("缺少", "text-orange-200", "border-orange-300/25 bg-orange-300/10", "circle-alert"),
        "setup": ("待接入", "text-orange-200", "border-orange-300/25 bg-orange-300/10", "plug-zap"),
        "mvp": ("MVP", "text-sky-200", "border-sky-300/25 bg-sky-300/10", "beaker"),
        "future": ("規劃", "text-sky-200", "border-sky-300/25 bg-sky-300/10", "clock-3"),
        "blocked": ("不適用", "text-slate-300", "border-slate-700 bg-slate-950/60", "minus-circle"),
    }
    label, text_class, shell_class, icon = label_map.get(state, label_map["future"])
    return f"""
    <div class="rounded-xl border {shell_class} p-3">
      <div class="flex items-start justify-between gap-3">
        <div>
          <h3 class="text-sm font-black text-slate-100">{html.escape(item.get("name", ""))}</h3>
          <p class="mt-1 text-xs leading-5 text-slate-400">{html.escape(item.get("detail", ""))}</p>
        </div>
        <span class="inline-flex shrink-0 items-center gap-1 rounded-md bg-black/25 px-2 py-1 text-[11px] font-black {text_class}">
          <i data-lucide="{icon}" class="h-3 w-3"></i>{html.escape(label)}
        </span>
      </div>
    </div>
    """


def active_readiness_section(report: dict) -> str:
    level = report.get("hardware_level", {}).get("level")
    if level == "Q":
        cards = [
            readiness_card(
                "Qualcomm Readiness",
                report.get("qualcomm_readiness", {}),
                report.get("qualcomm_readiness", {}).get("blockers", []),
                "cpu",
                "lime",
            )
        ]
    elif level == "AM":
        cards = [
            readiness_card(
                "AMD Readiness",
                report.get("amd_readiness", {}),
                report.get("amd_readiness", {}).get("blockers", []),
                "circuit-board",
                "red",
            )
        ]
    elif level == "I":
        cards = [
            readiness_card(
                "Intel Readiness",
                report.get("intel_readiness", {}),
                report.get("intel_readiness", {}).get("blockers", []),
                "microchip",
                "orange",
            )
        ]
    elif level in {"A", "B", "C"}:
        cards = [
            readiness_card(
                "NVIDIA Readiness",
                report.get("nvidia_readiness", {}),
                nvidia_blockers(report),
                "gpu",
                "sky",
            )
        ]
    else:
        cards = [
            readiness_card(
                "CPU Baseline Readiness",
                {
                    "tier": "CPU",
                    "score": 0,
                    "meaning": "No platform accelerator was selected. Use CPU int8 ASR and CPU FFmpeg baseline.",
                },
                [],
                "cpu",
                "lime",
            )
        ]

    return '<section class="mt-5 grid gap-5 lg:grid-cols-1">' + "\n".join(cards) + "</section>"


def dependency_card(name: str, detail: object, available: object, icon: str) -> str:
    state = "ok" if available else "missing"
    color = "bg-lime-400" if available else "bg-red-400"
    text_color = "text-lime-300" if available else "text-red-300"
    label = "Available" if available else "Missing"
    clean_detail = str(detail or "not detected").splitlines()[0][:120]
    return f"""
    <article class="rounded-2xl border border-slate-800 bg-slate-950/50 p-4">
      <div class="mb-3 flex items-center justify-between">
        <div class="flex items-center gap-3">
          <span class="rounded-xl bg-slate-800 p-2 text-slate-300"><i data-lucide="{icon}" class="h-5 w-5"></i></span>
          <h3 class="font-black">{html.escape(name)}</h3>
        </div>
        <span class="h-3 w-3 rounded-full {color} shadow-lg"></span>
      </div>
      <p class="text-xs font-bold uppercase tracking-wide {text_color}">{state_label(state, label)}</p>
      <p class="mt-2 min-h-10 text-xs leading-5 text-slate-400">{html.escape(clean_detail)}</p>
    </article>
    """


def detail_card(title: str, icon: str, rows: list[tuple[str, object]]) -> str:
    row_html = "\n".join(
        f"""
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <p class="text-xs text-slate-500">{html.escape(str(label))}</p>
          <p class="mt-1 break-words text-sm font-bold text-slate-200">{html.escape(str(value))}</p>
        </div>
        """
        for label, value in rows
    )
    return f"""
    <article class="rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl">
      <div class="mb-4 flex items-center justify-between">
        <h2 class="text-xl font-black">{html.escape(title)}</h2>
        <i data-lucide="{icon}" class="h-6 w-6 text-lime-300"></i>
      </div>
      <div class="grid gap-3">{row_html}</div>
    </article>
    """


def nvidia_blockers(report: dict) -> list[str]:
    gpu = report.get("gpu_cuda", {})
    readiness = report.get("nvidia_readiness", {})
    blockers = []
    if not gpu.get("cuda_usable"):
        blockers.append("CUDA is not usable on this local machine.")
    if not gpu.get("nvidia_smi_available"):
        blockers.append("No NVIDIA GPU detected via nvidia-smi.")
    if not readiness.get("docker_available"):
        blockers.append("Docker is not available for future GPU worker deployment.")
    if not report.get("ffmpeg_codecs", {}).get("platform_encoders", {}).get("nvidia_nvenc_usable"):
        blockers.append("NVENC is not usable locally; use CPU or another platform encoder.")
    return blockers


def platform_encoder_summary(ffmpeg: dict) -> str:
    encoders = ffmpeg.get("platform_encoders", {})
    usable = []
    if encoders.get("nvidia_nvenc_usable"):
        usable.append("NVENC")
    if encoders.get("amd_amf_usable"):
        usable.append("AMD AMF")
    if encoders.get("intel_qsv_usable"):
        usable.append("Intel QSV")
    if usable:
        return ", ".join(usable)
    return "CPU libx264/libx265"


def active_platform_profile(report: dict) -> str:
    level = report.get("hardware_level", {}).get("level")
    if level == "Q":
        return report.get("qualcomm_platform", {}).get("recommended_local_profile", "default")
    if level == "AM":
        return report.get("amd_platform", {}).get("recommended_local_profile", "default")
    if level == "I":
        return report.get("intel_platform", {}).get("recommended_local_profile", "default")
    return "default"


def tone_palette(tone: str) -> dict[str, str]:
    palettes = {
        "lime": {"text": "text-lime-300", "soft": "bg-lime-300/10", "bar": "bg-lime-400"},
        "orange": {"text": "text-orange-300", "soft": "bg-orange-300/10", "bar": "bg-orange-400"},
        "red": {"text": "text-red-300", "soft": "bg-red-300/10", "bar": "bg-red-400"},
        "sky": {"text": "text-sky-300", "soft": "bg-sky-300/10", "bar": "bg-sky-400"},
    }
    return palettes.get(tone, palettes["lime"])


def state_label(state: str, fallback: str) -> str:
    return {"ok": "Available", "missing": "Missing"}.get(state, fallback)


def clamp_percent(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(0.0, min(100.0, number))


def safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def list_jobs(output_root: Path) -> list[dict]:
    if not output_root.exists():
        return []

    jobs = []
    for job_dir in sorted(output_root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if not job_dir.is_dir():
            continue
        manifest_path = job_dir / "job_manifest.json"
        manifest = read_json_or_none(manifest_path) or {
            "job_id": job_dir.name,
            "status": "unknown",
            "stages": {},
        }
        artifacts = []
        for path in sorted(job_dir.rglob("*.json")):
            artifacts.append(str(path.relative_to(job_dir)).replace("\\", "/"))

        jobs.append(
            {
                "dir": job_dir,
                "manifest": manifest,
                "artifacts": artifacts,
                "video_info": read_json_or_none(job_dir / "reports/video_info.json"),
                "audio_info": read_json_or_none(job_dir / "audio/audio_info.json"),
                "transcript_info": read_json_or_none(job_dir / "transcripts/transcript.json"),
                "segments_info": read_json_or_none(job_dir / "transcripts/segments.json"),
                "clip_candidates": read_json_or_none(job_dir / "llm/clip_candidates.json"),
                "validated_clips": read_json_or_none(job_dir / "clips/validated_clips.json"),
            }
        )
    return jobs


def jobs_for_frontend(output_root: Path) -> list[dict]:
    jobs = []
    for job in list_jobs(output_root):
        manifest = job["manifest"]
        job_id = manifest.get("job_id", job["dir"].name)
        config = read_json_or_none(job["dir"] / "reports/effective_config.json") or {}
        frontend_status = frontend_job_status(manifest, job_id)
        jobs.append(
            {
                "id": job_id,
                "status": frontend_status,
                "profile": Path(config.get("config_path", "") or manifest.get("config", {}).get("profile", "local")).name,
                "videoPath": manifest.get("input", {}).get("original_path")
                or manifest.get("input", {}).get("source_path", ""),
                "outputDirectory": str(output_root),
                "updatedAt": manifest.get("updated_at") or manifest.get("created_at") or "",
                "phases": frontend_phases(manifest),
            }
        )
    return jobs


def frontend_phases(manifest: dict) -> list[dict]:
    stages = manifest.get("stages", {})
    if manifest_media_type(manifest) == "image":
        return [
            {
                "key": "upload",
                "label": "媒體上傳",
                "detail": "Image Upload",
                "status": "completed" if stages.get("upload") == "completed" else "waiting",
                "progress": 100 if stages.get("upload") == "completed" else 0,
            },
            {
                "key": "prompt",
                "label": "提示詞",
                "detail": "Edit Prompt",
                "status": "completed" if stages.get("upload") == "completed" else "waiting",
                "progress": 100 if stages.get("upload") == "completed" else 0,
            },
            {
                "key": "image_edit",
                "label": "圖片編輯",
                "detail": "Local Image Edit",
                "status": stages.get("image_edit", "waiting"),
                "progress": 100 if stages.get("image_edit") == "completed" else 0,
            },
            {
                "key": "render",
                "label": "輸出",
                "detail": "PNG Export",
                "status": "completed" if stages.get("image_edit") == "completed" else "waiting",
                "progress": 100 if stages.get("image_edit") == "completed" else 0,
            },
        ]
    return [
        {
            "key": "asr",
            "label": "影片分析",
            "detail": "FFprobe Analyze",
            "status": phase_status(stages, ["analyze"]),
            "progress": phase_progress(stages, ["analyze"]),
        },
        {
            "key": "llm",
            "label": "編輯計畫",
            "detail": "Prompt Plan",
            "status": phase_status(stages, ["edit_plan"]),
            "progress": phase_progress(stages, ["edit_plan"]),
        },
        {
            "key": "vision",
            "label": "影格處理",
            "detail": "Local Frame Edit",
            "status": phase_status(stages, ["video_edit"]),
            "progress": phase_progress(stages, ["video_edit"]),
        },
        {
            "key": "render",
            "label": "影音渲染",
            "detail": "FFmpeg",
            "status": phase_status(stages, ["render"]),
            "progress": phase_progress(stages, ["render"]),
        },
    ]


def phase_status(stages: dict, keys: list[str]) -> str:
    values = [stages.get(key) for key in keys]
    if any(value == "failed" for value in values):
        return "failed"
    if any(value == "cancelled" for value in values):
        return "cancelled"
    if values and all(value == "completed" for value in values):
        return "completed"
    if any(value == "running" for value in values):
        return "running"
    if any(value == "completed" for value in values):
        return "running"
    return "waiting"


def phase_progress(stages: dict, keys: list[str]) -> int:
    if not keys:
        return 0
    completed = sum(1 for key in keys if stages.get(key) == "completed")
    if any(stages.get(key) == "cancelled" for key in keys):
        return 0
    if completed == len(keys):
        return 100
    if any(stages.get(key) == "running" for key in keys):
        return max(12, round((completed / len(keys)) * 100))
    if completed:
        return round((completed / len(keys)) * 100)
    return 0


def frontend_job_status(manifest: dict, job_id: str = "") -> str:
    manifest_status = str(manifest.get("status") or "").lower()
    media_type = manifest_media_type(manifest)
    stages = manifest.get("stages", {})
    input_data = manifest.get("input", {})
    has_input_path = bool(input_data.get("source_path") or input_data.get("original_path") or input_data.get("original_filename"))
    if not media_type and not has_input_path and not stages:
        return "incomplete"
    if manifest_status == "cancelling":
        return "cancelling"
    if manifest_status == "cancelled":
        return "cancelled"
    if manifest_status == "queued" and media_type == "video":
        return "queued" if job_has_active_worker(job_id) or not transient_job_is_stale(manifest) else "interrupted"
    if manifest_status in {"failed", "error"}:
        return "failed"
    if any(status == "cancelled" for status in stages.values()):
        return "cancelled"
    if stages.get("render") == "completed":
        return "completed"
    if manifest_status == "completed":
        return "completed"
    if any(status == "running" for status in stages.values()):
        return "running" if job_has_active_worker(job_id) or not transient_job_is_stale(manifest) else "interrupted"
    if any(status == "completed" for status in stages.values()):
        if manifest_status in {"running", "queued"} and media_type == "video" and transient_job_is_stale(manifest) and not job_has_active_worker(job_id):
            return "interrupted"
        return "running"
    if manifest_status in {"running", "queued"} and media_type == "video" and transient_job_is_stale(manifest) and not job_has_active_worker(job_id):
        return "interrupted"
    return "queued"


def manifest_media_type(manifest: dict) -> str:
    input_data = manifest.get("input", {})
    media_type = str(input_data.get("media_type") or "").lower()
    if media_type:
        return media_type
    candidate = str(input_data.get("original_filename") or input_data.get("source_path") or input_data.get("original_path") or "").lower()
    suffix = Path(candidate).suffix
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return "image"
    if suffix in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
        return "video"
    return ""


def job_has_active_worker(job_id: str) -> bool:
    safe_job_id = sanitize_job_id(job_id)
    with VIDEO_WORKERS_LOCK:
        return safe_job_id in VIDEO_WORKERS


def transient_job_is_stale(manifest: dict, stale_seconds: int = 12) -> bool:
    timestamp = parse_manifest_datetime(manifest.get("updated_at") or manifest.get("created_at"))
    if not timestamp:
        return True
    return (datetime.now(timezone.utc) - timestamp).total_seconds() > stale_seconds


def parse_manifest_datetime(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def gallery_for_frontend(output_root: Path) -> list[dict]:
    gallery = []
    if not output_root.exists():
        return gallery
    allowed_suffixes = {".mp4", ".mov", ".mkv", ".wav", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    for job_dir in sorted(output_root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if not job_dir.is_dir():
            continue
        manifest = read_json_or_none(job_dir / "job_manifest.json") or {"job_id": job_dir.name}
        job_id = manifest.get("job_id", job_dir.name)
        video_info = read_json_or_none(job_dir / "reports/video_info.json") or {}
        audio_info = read_json_or_none(job_dir / "audio/audio_info.json") or {}
        paths = [
            path
            for path in job_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in allowed_suffixes
            and is_gallery_output_path(job_dir, path)
        ]
        paths.sort(key=artifact_sort_key)
        for artifact_path in paths[:18]:
            gallery.append(artifact_card_for_frontend(output_root, job_dir, job_id, artifact_path, video_info, audio_info, len(gallery)))
    return gallery[:36]


def is_gallery_output_path(job_dir: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(job_dir).parts
    except ValueError:
        return False
    if not parts:
        return False
    output_roots = {"renders", "audio", "exports"}
    return parts[0].lower() in output_roots


def artifact_sort_key(path: Path) -> tuple[int, float]:
    relative = str(path).replace("\\", "/")
    priority = 5
    if "/renders/" in relative and path.suffix.lower() in {".mp4", ".mov", ".mkv"}:
        priority = 0
    elif "/renders/" in relative and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        priority = 0
    elif "/input/" in relative and path.suffix.lower() in {".mp4", ".mov", ".mkv"}:
        priority = 1
    elif "/input/" in relative and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        priority = 1
    elif "/audio/" in relative and path.suffix.lower() == ".wav":
        priority = 2
    return priority, -path.stat().st_mtime


def artifact_card_for_frontend(
    output_root: Path,
    job_dir: Path,
    job_id: str,
    artifact_path: Path,
    video_info: dict,
    audio_info: dict,
    index: int,
) -> dict:
    relative_path = str(artifact_path.relative_to(job_dir)).replace("\\", "/")
    suffix = artifact_path.suffix.lower()
    kind = "json"
    duration = "artifact"
    resolution = "JSON"
    media_url_value = ""
    artifact_url = ""
    if suffix in {".mp4", ".mov", ".mkv"}:
        kind = "video"
        duration = format_seconds(video_info.get("duration"))
        resolution = format_resolution(video_info)
        media_url_value = media_url(job_id, relative_path) or ""
    elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        kind = "image"
        duration = "image"
        resolution = image_resolution(artifact_path)
        media_url_value = media_url(job_id, relative_path) or ""
    elif suffix == ".wav":
        kind = "audio"
        duration = format_seconds(audio_info.get("duration"))
        resolution = format_audio(audio_info)
        media_url_value = media_url(job_id, relative_path) or ""
    elif suffix == ".json":
        artifact_url = f"/artifact?job={quote(job_id)}&path={quote(relative_path)}"

    stat = artifact_path.stat()
    accent = "lime" if index % 3 == 0 else "sky" if index % 3 == 1 else "orange"
    return {
        "id": f"{job_id}:{relative_path}",
        "jobId": job_id,
        "fileName": artifact_path.name,
        "relativePath": relative_path,
        "kind": kind,
        "duration": duration,
        "createdAt": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "resolution": resolution,
        "folderPath": str(artifact_path.parent),
        "mediaUrl": media_url_value,
        "artifactUrl": artifact_url,
        "sizeBytes": stat.st_size,
        "accent": accent,
    }


def create_preview_job(output_root: Path, payload: dict) -> dict:
    video_path = str(payload.get("videoPath") or "").strip()
    if not video_path:
        raise ValueError("Video File Path is required.")
    output_directory = str(payload.get("outputDirectory") or output_root).strip() or str(output_root)
    profile = str(payload.get("profile") or "configs/qualcomm_windows_arm64.yaml").strip()
    allowed_profiles = {
        "configs/default.yaml",
        "configs/qualcomm_windows_arm64.yaml",
        "configs/amd_windows_directml.yaml",
        "configs/intel_windows_openvino.yaml",
    }
    if profile not in allowed_profiles:
        raise ValueError("Unsupported profile.")

    source_name = Path(video_path).stem or "job"
    job_id = sanitize_job_id(f"{source_name}_{uuid.uuid4().hex[:6]}")
    job_dir = output_root / job_id
    (job_dir / "reports").mkdir(parents=True, exist_ok=True)
    created_at = now_utc()
    manifest = {
        "schema_version": "1.0",
        "job_id": job_id,
        "status": "queued",
        "created_at": created_at,
        "updated_at": created_at,
        "input": {
            "source_path": "",
            "original_path": video_path,
            "original_filename": Path(video_path).name,
        },
        "config": {
            "profile": Path(profile).stem,
            "effective_config_path": "reports/effective_config.json",
            "pipeline": "dashboard_preview",
        },
        "stages": {},
        "errors": [],
        "dashboard_preview": True,
    }
    (job_dir / "job_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (job_dir / "reports/effective_config.json").write_text(
        json.dumps({"profile": profile, "output_directory": output_directory}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    command = f'python main.py run-all --config {profile} --input "{video_path}" --job-id {job_id}'
    return {
        "status": "queued",
        "command": command,
        "job": {
            "id": job_id,
            "status": "queued",
            "profile": Path(profile).name,
            "videoPath": video_path,
            "outputDirectory": output_directory,
            "updatedAt": created_at,
            "phases": frontend_phases(manifest),
        },
    }


def create_media_job(output_root: Path, payload: dict) -> dict:
    filename = Path(str(payload.get("filename") or "upload.bin")).name
    content_type = str(payload.get("content_type") or "")
    prompt = str(payload.get("prompt") or "").strip()
    profile = str(payload.get("profile") or "configs/qualcomm_windows_arm64.yaml").strip()
    allowed_profiles = {
        "configs/default.yaml",
        "configs/qualcomm_windows_arm64.yaml",
        "configs/amd_windows_directml.yaml",
        "configs/intel_windows_openvino.yaml",
    }
    if profile not in allowed_profiles:
        raise ValueError("Unsupported profile.")
    model_version = str(payload.get("model_version") or "local-v1")
    resolution = str(payload.get("resolution") or "original")
    requested_output_directory = str(payload.get("output_directory") or "").strip()
    settings = load_dashboard_settings(output_root)
    image_edit_provider = coerce_choice(
        payload.get("image_edit_provider") or settings.get("imageEditProvider"),
        {"auto", "pillow", "local_a1111", "local_comfyui", "openai"},
        "auto",
    )
    automatic1111_endpoint = coerce_endpoint(
        payload.get("automatic1111_endpoint") or settings.get("automatic1111Endpoint"),
        DASHBOARD_DEFAULT_SETTINGS["automatic1111Endpoint"],
    )
    comfyui_endpoint = coerce_endpoint(
        payload.get("comfyui_endpoint") or settings.get("comfyuiEndpoint"),
        DASHBOARD_DEFAULT_SETTINGS["comfyuiEndpoint"],
    )
    openai_image_model = coerce_short_text(
        payload.get("openai_image_model") or settings.get("openaiImageModel"),
        DASHBOARD_DEFAULT_SETTINGS["openaiImageModel"],
    )
    online_fallback_policy = coerce_choice(
        payload.get("online_fallback_policy") or settings.get("onlineFallbackPolicy"),
        {"warn_only", "disabled", "ask_each_time"},
        "warn_only",
    )
    data_base64 = str(payload.get("data_base64") or "")
    if not data_base64:
        raise ValueError("Missing uploaded file data.")
    try:
        content = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Uploaded file data is not valid base64.") from exc
    max_bytes = 80 * 1024 * 1024
    if len(content) > max_bytes:
        raise ValueError("Uploaded file is too large for the local JSON upload path. Limit is 80MB.")

    media_type = detect_media_type(filename, content_type, content)
    if media_type not in {"image", "video"}:
        raise ValueError("Only image and video uploads are supported in this editor.")

    job_id = sanitize_job_id(f"{Path(filename).stem}_{media_type}_{uuid.uuid4().hex[:6]}")
    job_dir = output_root / job_id
    input_dir = job_dir / "input"
    reports_dir = job_dir / "reports"
    renders_dir = job_dir / "renders"
    input_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    renders_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / filename
    input_path.write_bytes(content)

    created_at = now_utc()
    manifest = {
        "schema_version": "1.0",
        "job_id": job_id,
        "status": "completed" if media_type == "image" else "queued",
        "created_at": created_at,
        "updated_at": created_at,
        "input": {
            "source_path": str(input_path.relative_to(job_dir)).replace("\\", "/"),
            "original_path": filename,
            "original_filename": filename,
            "media_type": media_type,
            "content_type": content_type,
            "size_bytes": len(content),
        },
        "config": {
            "profile": Path(profile).stem,
            "effective_config_path": "reports/effective_config.json",
            "pipeline": "video_edit" if media_type == "video" else "image_edit",
        },
        "stages": {},
        "errors": [],
    }
    write_json_file(job_dir / "job_manifest.json", manifest)
    write_json_file(
        reports_dir / "edit_request.json",
        {
            "schema_version": "1.0",
            "stage": "edit_request",
            "status": "received",
            "created_at": created_at,
            "media_type": media_type,
            "prompt": prompt,
            "model_version": model_version,
            "resolution": resolution,
            "requested_output_directory": requested_output_directory,
            "image_edit_provider": image_edit_provider,
            "automatic1111_endpoint": automatic1111_endpoint,
            "comfyui_endpoint": comfyui_endpoint,
            "openai_image_model": openai_image_model,
            "online_fallback_policy": online_fallback_policy,
            "input": manifest["input"],
        },
    )
    write_json_file(
        reports_dir / "effective_config.json",
        {
            "profile": profile,
            "model_version": model_version,
            "resolution": resolution,
            "media_type": media_type,
            "requested_output_directory": requested_output_directory,
            "image_edit_provider": image_edit_provider,
            "automatic1111_endpoint": automatic1111_endpoint,
            "comfyui_endpoint": comfyui_endpoint,
            "openai_image_model": openai_image_model,
            "online_fallback_policy": online_fallback_policy,
        },
    )

    gallery_items: list[dict] = []
    command = ""
    message = ""
    if media_type == "image":
        try:
            edit_result = edit_image_with_provider(
                input_path=input_path,
                renders_dir=renders_dir,
                prompt=prompt,
                resolution=resolution,
                provider=image_edit_provider,
                automatic1111_endpoint=automatic1111_endpoint,
                comfyui_endpoint=comfyui_endpoint,
                openai_image_model=openai_image_model,
                online_fallback_policy=online_fallback_policy,
            )
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["stages"] = {"upload": "completed", "image_edit": "failed"}
            manifest["updated_at"] = now_utc()
            manifest.setdefault("errors", []).append({"message": str(exc), "created_at": now_utc()})
            write_json_file(job_dir / "job_manifest.json", manifest)
            write_json_file(
                reports_dir / "image_edit_error.json",
                {
                    "schema_version": "1.0",
                    "stage": "image_edit",
                    "status": "failed",
                    "created_at": now_utc(),
                    "provider": image_edit_provider,
                    "errors": [{"message": str(exc)}],
                },
            )
            publish_pipeline_status(job_id, "image_edit", "failed", 0)
            publish_pipeline_log(job_id, f"[image] failed: {exc}")
            raise
        output_path = job_dir / edit_result["relative_output"]
        export_dir = resolve_media_export_directory(output_root, requested_output_directory)
        export_path = export_image_result(output_path, export_dir, job_id)
        edit_result["exported_output"] = str(export_path)
        edit_result["export_directory"] = str(export_dir)
        manifest["stages"] = {"upload": "completed", "image_edit": edit_result["status"]}
        manifest["outputs"] = {
            "edited_image": edit_result["relative_output"],
            "exported_image": str(export_path),
        }
        write_json_file(job_dir / "job_manifest.json", manifest)
        write_json_file(reports_dir / "image_edit.json", edit_result)
        gallery_items.append(
            artifact_card_for_frontend(output_root, job_dir, job_id, output_path, {}, {}, 0)
        )
        message = f'{edit_result["message"]} 已匯出到 {export_path}'
        publish_pipeline_log(job_id, f"[image] {message}")
        publish_pipeline_status(job_id, "render", "completed", 100)
    else:
        manifest["status"] = "running"
        manifest["stages"] = {"upload": "completed"}
        write_json_file(job_dir / "job_manifest.json", manifest)
        command = f"background local video_edit runner: {job_id}"
        message = "影片已上傳，已啟動本地影片編輯 runner；完成後會自動出現在產出物畫廊。"
        publish_pipeline_log(job_id, f"[video] Uploaded {filename}; starting local video edit runner")
        start_video_edit_worker(output_root, job_id)

    return {
        "status": "ok",
        "media_type": media_type,
        "message": message,
        "command": command,
        "job": {
            "id": job_id,
            "status": manifest["status"],
            "profile": Path(profile).name,
            "videoPath": str(input_path),
            "outputDirectory": str(export_dir) if media_type == "image" else str(output_root),
            "updatedAt": created_at,
            "phases": frontend_phases(manifest),
        },
        "gallery": gallery_items,
        "output_path": str(export_path) if media_type == "image" else "",
        "output_folder": str(export_dir) if media_type == "image" else "",
        "output_url": export_media_url(export_path) if media_type == "image" else "",
    }


def resolve_media_export_directory(output_root: Path, requested_directory: str) -> Path:
    output_base = output_root.parent.resolve()
    if requested_directory:
        raw_target = Path(requested_directory)
        target = raw_target.resolve() if raw_target.is_absolute() else (Path.cwd() / raw_target).resolve()
    else:
        target = output_base.resolve()
    try:
        target.relative_to(output_base)
    except ValueError as exc:
        raise ValueError(f"圖片輸出資料夾需位於本專案 output 目錄底下：{output_base}") from exc
    target.mkdir(parents=True, exist_ok=True)
    return target


def export_image_result(source_path: Path, export_dir: Path, job_id: str) -> Path:
    suffix = source_path.suffix or ".png"
    target = export_dir / f"{job_id}_edited{suffix}"
    counter = 2
    while target.exists():
        target = export_dir / f"{job_id}_edited_{counter}{suffix}"
        counter += 1
    shutil.copy2(source_path, target)
    return target


def detect_media_type(filename: str, content_type: str, content: bytes) -> str:
    lowered = filename.lower()
    if content_type.startswith("image/") or lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        return "image"
    if content_type.startswith("video/") or lowered.endswith((".mp4", ".mov", ".mkv", ".webm")):
        return "video"
    if content.startswith(b"\x89PNG") or content.startswith(b"\xff\xd8\xff") or content[:4] == b"RIFF":
        return "image"
    if b"ftyp" in content[:16]:
        return "video"
    return "unknown"


def edit_image_with_provider(
    input_path: Path,
    renders_dir: Path,
    prompt: str,
    resolution: str,
    provider: str,
    automatic1111_endpoint: str,
    comfyui_endpoint: str,
    openai_image_model: str,
    online_fallback_policy: str,
) -> dict:
    if provider == "pillow" or (provider == "auto" and not prompt_needs_generative_image_model(prompt)):
        result = edit_image_locally(input_path, renders_dir, prompt, resolution)
        result["provider"] = "pillow"
        return result

    errors: list[str] = []
    if provider in {"auto", "local_a1111"}:
        try:
            return edit_image_with_automatic1111(input_path, renders_dir, prompt, automatic1111_endpoint)
        except Exception as exc:
            errors.append(f"Automatic1111 unavailable: {exc}")
            if provider == "local_a1111":
                raise ValueError(build_generative_image_error(errors)) from exc

    if provider == "local_comfyui":
        errors.append(comfyui_readiness_message(comfyui_endpoint))
        raise ValueError(build_generative_image_error(errors))

    if provider == "auto":
        errors.append(comfyui_readiness_message(comfyui_endpoint))

    if provider in {"auto", "openai"}:
        if online_fallback_policy == "disabled" and provider == "auto":
            errors.append("Online fallback is disabled in settings.")
        else:
            try:
                return edit_image_with_openai(input_path, renders_dir, prompt, openai_image_model)
            except Exception as exc:
                errors.append(f"OpenAI Images API unavailable: {exc}")
                if provider == "openai":
                    raise ValueError(build_generative_image_error(errors)) from exc

    raise ValueError(build_generative_image_error(errors))


def prompt_needs_generative_image_model(prompt: str) -> bool:
    text = prompt.lower()
    generative_tokens = [
        "貓變狗",
        "變成狗",
        "變成貓",
        "替換",
        "換成",
        "改成",
        "生成",
        "inpaint",
        "replace",
        "turn into",
        "make it a",
        "cat to dog",
        "dog",
    ]
    return any(token in text for token in generative_tokens)


def comfyui_readiness_message(endpoint: str) -> str:
    try:
        info = http_json("GET", f"{endpoint.rstrip('/')}/object_info", timeout=5)
    except Exception as exc:
        return f"ComfyUI unavailable: {exc}"
    loader = info.get("CheckpointLoaderSimple", {})
    choices = (
        loader.get("input", {})
        .get("required", {})
        .get("ckpt_name", [])
    )
    checkpoints: list[str] = []
    if choices and isinstance(choices[0], list):
        checkpoints = [str(item) for item in choices[0] if item]
    if not checkpoints:
        return "ComfyUI is running, but no checkpoint model is installed or visible to CheckpointLoaderSimple."
    return "ComfyUI is running, but Dump2Done needs a saved image-to-image workflow JSON before it can submit jobs."


def build_generative_image_error(errors: list[str]) -> str:
    details = "；".join(errors) if errors else "No generative image provider is configured."
    return (
        "這個提示需要生成式圖片模型，Pillow 無法把貓變成狗。"
        f"{details} 請啟動 Automatic1111 WebUI 並加上 --api，或設定 OPENAI_API_KEY 後選 OpenAI Images API。"
    )


def edit_image_with_automatic1111(input_path: Path, renders_dir: Path, prompt: str, endpoint: str) -> dict:
    created_at = now_utc()
    health_url = f"{endpoint.rstrip('/')}/sdapi/v1/options"
    http_json("GET", health_url, timeout=3)
    image_b64 = base64.b64encode(input_path.read_bytes()).decode("ascii")
    width, height = image_size_for_generation(input_path)
    payload = {
        "init_images": [image_b64],
        "prompt": normalize_image_prompt(prompt),
        "negative_prompt": "low quality, blurry, distorted, deformed, extra limbs, text, watermark",
        "denoising_strength": 0.82,
        "steps": 24,
        "cfg_scale": 7,
        "width": width,
        "height": height,
        "sampler_name": "DPM++ 2M Karras",
    }
    response = http_json("POST", f"{endpoint.rstrip('/')}/sdapi/v1/img2img", payload, timeout=240)
    images = response.get("images") or []
    if not images:
        raise RuntimeError("A1111 returned no image.")
    output_path = renders_dir / f"edited_{input_path.stem}.png"
    write_base64_image(images[0], output_path)
    return {
        "schema_version": "1.0",
        "stage": "image_edit",
        "status": "completed",
        "created_at": created_at,
        "prompt": prompt,
        "provider": "automatic1111",
        "operations": ["stable_diffusion_img2img"],
        "message": "已透過本地 Automatic1111 / Stable Diffusion 完成生成式圖片編輯。",
        "relative_output": str(output_path.parent.name + "/" + output_path.name).replace("\\", "/"),
    }


def edit_image_with_openai(input_path: Path, renders_dir: Path, prompt: str, model: str) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    created_at = now_utc()
    boundary = f"----Dump2Done{uuid.uuid4().hex}"
    output_path = renders_dir / f"edited_{input_path.stem}.png"
    fields = {
        "model": model or DASHBOARD_DEFAULT_SETTINGS["openaiImageModel"],
        "prompt": prompt or "Edit the image according to the user's request.",
        "size": "auto",
    }
    body = build_multipart_body(boundary, fields, "image", input_path, "image/png")
    request = urllib_request.Request(
        "https://api.openai.com/v1/images/edits",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=240) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI Images API HTTP {exc.code}: {detail[:320]}") from exc
    image_data = (payload.get("data") or [{}])[0].get("b64_json")
    if not image_data:
        raise RuntimeError("OpenAI Images API returned no base64 image.")
    write_base64_image(image_data, output_path)
    return {
        "schema_version": "1.0",
        "stage": "image_edit",
        "status": "completed",
        "created_at": created_at,
        "prompt": prompt,
        "provider": "openai_images",
        "model": fields["model"],
        "operations": ["openai_image_edit"],
        "message": "已透過 OpenAI Images API 完成生成式圖片編輯。",
        "relative_output": str(output_path.parent.name + "/" + output_path.name).replace("\\", "/"),
    }


def normalize_image_prompt(prompt: str) -> str:
    text = prompt.strip()
    lowered = text.lower()
    if ("貓" in text and "狗" in text) or "cat to dog" in lowered:
        return (
            "Transform the cat into a realistic dog while preserving the same photo composition, "
            "camera angle, lighting, background, and overall natural snapshot style."
        )
    return text or "Edit the image naturally while preserving composition and background."


def image_size_for_generation(path: Path, max_side: int = 768) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        return 768, 768
    scale = min(1.0, max_side / max(width, height))
    width = max(64, int(width * scale))
    height = max(64, int(height * scale))
    width = max(64, round(width / 64) * 64)
    height = max(64, round(height / 64) * 64)
    return width, height


def write_base64_image(value: str, output_path: Path) -> None:
    if "," in value and value.strip().startswith("data:"):
        value = value.split(",", 1)[1]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(value))


def http_json(method: str, url: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib_request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib_error.URLError as exc:
        raise RuntimeError(str(exc.reason if hasattr(exc, "reason") else exc)) from exc
    return json.loads(body or "{}")


def build_multipart_body(boundary: str, fields: dict[str, str], file_field: str, file_path: Path, mime_type: str) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks)


def edit_image_locally(input_path: Path, renders_dir: Path, prompt: str, resolution: str) -> dict:
    created_at = now_utc()
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except Exception as exc:
        output_path = renders_dir / f"edited_{input_path.stem}{input_path.suffix}"
        output_path.write_bytes(input_path.read_bytes())
        return {
            "schema_version": "1.0",
            "stage": "image_edit",
            "status": "completed",
            "created_at": created_at,
            "prompt": prompt,
            "operations": ["copy_original"],
            "message": f"Pillow is unavailable; copied original image. ({exc})",
            "relative_output": str(output_path.parent.name + "/" + output_path.name).replace("\\", "/"),
        }

    operations: list[str] = []
    output_path = renders_dir / f"edited_{input_path.stem}.png"
    prompt_lower = prompt.lower()
    with Image.open(input_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        if any(token in prompt_lower for token in ["黑白", "灰階", "grayscale", "black and white", "b&w"]):
            image = ImageOps.grayscale(image).convert("RGB")
            operations.append("grayscale")
        if any(token in prompt_lower for token in ["變亮", "提亮", "bright", "brighter"]):
            image = ImageEnhance.Brightness(image).enhance(1.25)
            operations.append("brighten")
        if any(token in prompt_lower for token in ["變暗", "dark", "darker"]):
            image = ImageEnhance.Brightness(image).enhance(0.78)
            operations.append("darken")
        if any(token in prompt_lower for token in ["對比", "contrast"]):
            image = ImageEnhance.Contrast(image).enhance(1.25)
            operations.append("contrast")
        if any(token in prompt_lower for token in ["銳化", "sharp", "sharpen"]):
            image = image.filter(ImageFilter.SHARPEN)
            operations.append("sharpen")
        if any(token in prompt_lower for token in ["模糊", "blur"]):
            image = image.filter(ImageFilter.GaussianBlur(radius=2))
            operations.append("blur")
        wants_rotation = any(token in prompt_lower for token in ["90", "九十", "旋轉", "rotate", "轉"])
        wants_left_rotation = any(
            token in prompt_lower
            for token in ["往左", "向左", "左旋", "左轉", "逆時針", "counterclockwise", "anti-clockwise", "ccw"]
        )
        wants_right_rotation = any(
            token in prompt_lower
            for token in ["往右", "向右", "右旋", "右轉", "順時針", "clockwise", "cw"]
        )
        if wants_rotation and wants_left_rotation:
            image = image.rotate(90, expand=True)
            operations.append("rotate_left_90")
        elif wants_rotation and wants_right_rotation:
            image = image.rotate(-90, expand=True)
            operations.append("rotate_right_90")
        elif "90" in prompt_lower or "九十" in prompt_lower:
            image = image.rotate(-90, expand=True)
            operations.append("rotate_right_90")
        if any(token in prompt_lower for token in ["水平翻轉", "mirror", "flip horizontal"]):
            image = ImageOps.mirror(image)
            operations.append("flip_horizontal")
        if any(token in prompt_lower for token in ["垂直翻轉", "flip vertical"]):
            image = ImageOps.flip(image)
            operations.append("flip_vertical")
        if resolution in {"720p", "1080p"}:
            max_side = 1280 if resolution == "720p" else 1920
            image.thumbnail((max_side, max_side))
            operations.append(f"fit_{resolution}")
        if not operations:
            operations.append("copy_original_preview")
        image.save(output_path, format="PNG", optimize=True)

    return {
        "schema_version": "1.0",
        "stage": "image_edit",
        "status": "completed",
        "created_at": created_at,
        "prompt": prompt,
        "operations": operations,
        "message": "已完成本地圖片編輯。" if operations != ["copy_original_preview"] else "已保存圖片與 prompt；未匹配到本地 deterministic 編輯指令。",
        "relative_output": str(output_path.parent.name + "/" + output_path.name).replace("\\", "/"),
    }


def write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_dashboard_settings(output_root: Path) -> dict:
    stored = read_json_or_none(DASHBOARD_SETTINGS_PATH) or {}
    settings = {**DASHBOARD_DEFAULT_SETTINGS, **stored}
    try:
        output_dir = resolve_media_export_directory(output_root, str(settings["defaultOutputDirectory"]))
        settings["defaultOutputDirectory"] = str(output_dir)
    except ValueError:
        settings["defaultOutputDirectory"] = DASHBOARD_DEFAULT_SETTINGS["defaultOutputDirectory"]
    settings["autoPreviewOutput"] = bool(settings.get("autoPreviewOutput"))
    settings["autoOpenOutputFolder"] = bool(settings.get("autoOpenOutputFolder"))
    if settings.get("galleryDensity") not in {"comfortable", "compact"}:
        settings["galleryDensity"] = "comfortable"
    settings["localLlmProvider"] = coerce_choice(
        settings.get("localLlmProvider"),
        {"ollama_demo", "openai_online_placeholder", "none"},
        "ollama_demo",
    )
    settings["localLlmEndpoint"] = coerce_short_text(
        settings.get("localLlmEndpoint"),
        DASHBOARD_DEFAULT_SETTINGS["localLlmEndpoint"],
    )
    settings["visionProvider"] = coerce_choice(
        settings.get("visionProvider"),
        {"local_pillow_mvp", "onnx_directml_future", "qnn_future", "online_video_edit_placeholder"},
        "local_pillow_mvp",
    )
    settings["imageEditProvider"] = coerce_choice(
        settings.get("imageEditProvider"),
        {"auto", "pillow", "local_a1111", "local_comfyui", "openai"},
        "auto",
    )
    settings["automatic1111Endpoint"] = coerce_endpoint(
        settings.get("automatic1111Endpoint"),
        DASHBOARD_DEFAULT_SETTINGS["automatic1111Endpoint"],
    )
    settings["comfyuiEndpoint"] = coerce_endpoint(
        settings.get("comfyuiEndpoint"),
        DASHBOARD_DEFAULT_SETTINGS["comfyuiEndpoint"],
    )
    settings["openaiImageModel"] = coerce_short_text(
        settings.get("openaiImageModel"),
        DASHBOARD_DEFAULT_SETTINGS["openaiImageModel"],
    )
    settings["asrProvider"] = coerce_choice(
        settings.get("asrProvider"),
        {"faster_whisper_cpu_demo", "onnx_asr_future", "online_asr_placeholder"},
        "faster_whisper_cpu_demo",
    )
    settings["onlineFallbackPolicy"] = coerce_choice(
        settings.get("onlineFallbackPolicy"),
        {"warn_only", "disabled", "ask_each_time"},
        "warn_only",
    )
    return settings


def save_dashboard_settings(output_root: Path, payload: dict) -> dict:
    settings = {**DASHBOARD_DEFAULT_SETTINGS}
    if "settings" in payload and isinstance(payload["settings"], dict):
        payload = payload["settings"]
    requested_output = str(payload.get("defaultOutputDirectory") or settings["defaultOutputDirectory"]).strip()
    output_dir = resolve_media_export_directory(output_root, requested_output)
    settings["defaultOutputDirectory"] = str(output_dir)
    settings["autoPreviewOutput"] = bool(payload.get("autoPreviewOutput"))
    settings["autoOpenOutputFolder"] = bool(payload.get("autoOpenOutputFolder"))
    density = str(payload.get("galleryDensity") or "comfortable")
    settings["galleryDensity"] = density if density in {"comfortable", "compact"} else "comfortable"
    settings["localLlmProvider"] = coerce_choice(
        payload.get("localLlmProvider"),
        {"ollama_demo", "openai_online_placeholder", "none"},
        "ollama_demo",
    )
    settings["localLlmEndpoint"] = coerce_short_text(
        payload.get("localLlmEndpoint"),
        DASHBOARD_DEFAULT_SETTINGS["localLlmEndpoint"],
    )
    settings["visionProvider"] = coerce_choice(
        payload.get("visionProvider"),
        {"local_pillow_mvp", "onnx_directml_future", "qnn_future", "online_video_edit_placeholder"},
        "local_pillow_mvp",
    )
    settings["imageEditProvider"] = coerce_choice(
        payload.get("imageEditProvider"),
        {"auto", "pillow", "local_a1111", "local_comfyui", "openai"},
        "auto",
    )
    settings["automatic1111Endpoint"] = coerce_endpoint(
        payload.get("automatic1111Endpoint"),
        DASHBOARD_DEFAULT_SETTINGS["automatic1111Endpoint"],
    )
    settings["comfyuiEndpoint"] = coerce_endpoint(
        payload.get("comfyuiEndpoint"),
        DASHBOARD_DEFAULT_SETTINGS["comfyuiEndpoint"],
    )
    settings["openaiImageModel"] = coerce_short_text(
        payload.get("openaiImageModel"),
        DASHBOARD_DEFAULT_SETTINGS["openaiImageModel"],
    )
    settings["asrProvider"] = coerce_choice(
        payload.get("asrProvider"),
        {"faster_whisper_cpu_demo", "onnx_asr_future", "online_asr_placeholder"},
        "faster_whisper_cpu_demo",
    )
    settings["onlineFallbackPolicy"] = coerce_choice(
        payload.get("onlineFallbackPolicy"),
        {"warn_only", "disabled", "ask_each_time"},
        "warn_only",
    )
    write_json_file(DASHBOARD_SETTINGS_PATH, settings)
    return {"status": "ok", "settings": settings, "path": str(DASHBOARD_SETTINGS_PATH)}


def coerce_choice(value: object, allowed: set[str], default: str) -> str:
    text = str(value or "")
    return text if text in allowed else default


def coerce_short_text(value: object, default: str, limit: int = 180) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    return text[:limit]


def coerce_endpoint(value: object, default: str) -> str:
    text = coerce_short_text(value, default, 240).rstrip("/")
    if not re.match(r"^https?://", text):
        return default.rstrip("/")
    return text


def image_resolution(path: Path) -> str:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return f"{image.width}x{image.height}"
    except Exception:
        return "image"


def open_folder_request(output_root: Path, payload: dict) -> dict:
    folder = str(payload.get("path") or "").strip()
    if not folder:
        raise ValueError("Folder path is required.")
    label = str(payload.get("label") or "").strip()
    base = Path.cwd().resolve()
    target = (base / folder).resolve() if not Path(folder).is_absolute() else Path(folder).resolve()
    allowed_root = output_root.parent.resolve()
    if not str(target).startswith(str(allowed_root)):
        raise ValueError("Only folders under the local output directory can be opened.")
    if not target.exists():
        return {"status": "missing", "message": f"Folder does not exist yet: {target}"}
    opened_target = target if target.is_dir() else target.parent
    if os.name == "nt":
        command = ["explorer.exe", str(opened_target)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        message = f"Opened Explorer: {opened_target}"
    elif hasattr(os, "startfile"):
        os.startfile(str(opened_target))  # type: ignore[attr-defined]
        message = f"Opened {opened_target}"
    else:
        message = f"Folder path: {opened_target}"
    persist_console_event(
        {
            "type": "open_folder",
            "job_id": "",
            "message": f"{message}" + (f" ({label})" if label else ""),
            "path": str(opened_target),
            "created_at": now_utc(),
        }
    )
    return {
        "status": "ok",
        "message": message,
        "path": str(opened_target),
        "label": label,
    }


def delete_artifact_request(output_root: Path, payload: dict) -> dict:
    job_id = str(payload.get("job_id") or "").strip()
    relative_path = str(payload.get("path") or "").strip()
    target = resolve_job_path(output_root, job_id, relative_path)
    if not target or not target.exists() or not target.is_file():
        raise ValueError("Artifact not found.")
    job_dir = (output_root / sanitize_job_id(job_id)).resolve()
    if not is_gallery_output_path(job_dir, target):
        raise ValueError("Only produced media artifacts can be deleted from the gallery.")
    if target.suffix.lower() not in {".mp4", ".mov", ".mkv", ".wav", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        raise ValueError("Only image, video, and audio artifacts can be deleted.")

    trash_root = (output_root.parent / "trash" / now_utc().replace(":", "").replace("-", "")).resolve()
    trash_target = trash_root / sanitize_job_id(job_id) / relative_path.replace("/", os.sep).replace("\\", os.sep)
    trash_target.parent.mkdir(parents=True, exist_ok=True)
    counter = 2
    while trash_target.exists():
        trash_target = trash_target.with_name(f"{trash_target.stem}_{counter}{trash_target.suffix}")
        counter += 1
    shutil.move(str(target), str(trash_target))
    metadata = {
        "schema_version": "1.0",
        "stage": "dashboard_delete",
        "status": "moved_to_trash",
        "created_at": now_utc(),
        "job_id": job_id,
        "original_relative_path": relative_path,
        "trash_path": str(trash_target),
    }
    write_json_file(trash_target.with_suffix(trash_target.suffix + ".delete.json"), metadata)
    persist_console_event(
        {
            "type": "delete_artifact",
            "job_id": job_id,
            "message": f"Moved artifact to trash: {relative_path}",
            "path": str(trash_target),
            "created_at": now_utc(),
        }
    )
    return {
        "status": "ok",
        "message": "Moved artifact to trash.",
        "job_id": job_id,
        "path": relative_path,
        "trash_path": str(trash_target),
        "gallery": gallery_for_frontend(output_root),
    }


def cancel_job_request(output_root: Path, payload: dict) -> dict:
    job_id = sanitize_job_id(str(payload.get("job_id") or "").strip())
    if not job_id:
        raise ValueError("Missing job_id.")
    job_dir = (output_root / job_id).resolve()
    if not job_dir.exists() or not job_dir.is_dir():
        raise ValueError("Job not found.")
    manifest_path = job_dir / "job_manifest.json"
    manifest = read_json_or_none(manifest_path) or {"job_id": job_id, "stages": {}}
    status = str(manifest.get("status") or "").lower()
    if status in {"completed", "failed", "cancelled"}:
        return {
            "status": "ok",
            "message": f"Job is already {status}.",
            "job": next((job for job in jobs_for_frontend(output_root) if job["id"] == job_id), None),
            "jobs": jobs_for_frontend(output_root),
        }

    reports_dir = job_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    write_json_file(
        reports_dir / "cancel_requested.json",
        {
            "schema_version": "1.0",
            "status": "requested",
            "job_id": job_id,
            "created_at": now_utc(),
        },
    )
    stages = manifest.setdefault("stages", {})
    if status == "queued":
        manifest["status"] = "cancelled"
        for stage_name, stage_status in list(stages.items()):
            if stage_status in {"waiting", "queued", "running"}:
                stages[stage_name] = "cancelled"
    else:
        manifest["status"] = "cancelling"
        for stage_name, stage_status in list(stages.items()):
            if stage_status == "running":
                stages[stage_name] = "cancelled"
                break
    manifest["updated_at"] = now_utc()
    manifest.setdefault("events", []).append(
        {
            "type": "cancel_requested",
            "message": "User requested cancellation from dashboard.",
            "created_at": now_utc(),
        }
    )
    write_json_file(manifest_path, manifest)
    publish_pipeline_log(job_id, "[dashboard] cancel requested by user")
    publish_pipeline_status(job_id, "vision", "cancelled", 0)
    return {
        "status": "ok",
        "message": "Cancel requested. The runner will stop at the next safe checkpoint.",
        "job_id": job_id,
        "jobs": jobs_for_frontend(output_root),
        "gallery": gallery_for_frontend(output_root),
    }


def sanitize_job_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
    return safe.strip("_") or f"job_{uuid.uuid4().hex[:6]}"


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_script_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def select_job(jobs: list[dict], selected_job_id: str | None) -> dict | None:
    if not jobs:
        return None
    if selected_job_id:
        for job in jobs:
            if job["manifest"].get("job_id", job["dir"].name) == selected_job_id:
                return job
    return jobs[0]


def resolve_job_path(output_root: Path, job_id: str, relative_path: str) -> Path | None:
    safe_job = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")
    if not safe_job or not relative_path:
        return None
    job_dir = (output_root / safe_job).resolve()
    target = (job_dir / relative_path).resolve()
    if not str(target).startswith(str(job_dir)):
        return None
    return target


def resolve_export_path(output_root: Path, export_path: str) -> Path | None:
    if not export_path:
        return None
    raw_path = Path(export_path)
    target = raw_path.resolve() if raw_path.is_absolute() else (Path.cwd() / raw_path).resolve()
    output_base = output_root.parent.resolve()
    try:
        target.relative_to(output_base)
    except ValueError:
        return None
    if target.suffix.lower() not in {".mp4", ".mov", ".mkv", ".wav", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        return None
    return target


def read_json_or_none(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def stage_progress(manifest: dict) -> tuple[int, int]:
    stages = manifest.get("stages", {})
    completed = sum(1 for key, _ in STAGE_LABELS if stages.get(key) == "completed")
    return completed, len(STAGE_LABELS)


def progress_percent(completed: int, total: int) -> int:
    return round((completed / total) * 100) if total else 0


def next_command_for_job(job_id: str, manifest: dict) -> dict[str, str]:
    stages = manifest.get("stages", {})
    if stages.get("analyze") != "completed":
        return {
            "label": "分析影片",
            "command": "python main.py analyze --config configs\\qualcomm_windows_arm64.yaml --input path\\to\\video.mp4 --job-id "
            + job_id,
        }
    if stages.get("transcribe") != "completed":
        return {
            "label": "抽取 ASR 音訊",
            "command": "python main.py transcribe --config configs\\qualcomm_windows_arm64.yaml --job-id "
            + job_id,
        }
    if stages.get("asr") != "completed":
        return {
            "label": "跑 faster-whisper ASR",
            "command": "python main.py transcribe --config configs\\qualcomm_windows_arm64.yaml --job-id "
            + job_id,
        }
    if stages.get("select_clips") != "completed":
        return {
            "label": "建立語意切片與候選片段",
            "command": "python main.py select-clips --config configs\\qualcomm_windows_arm64.yaml --job-id "
            + job_id,
        }
    if stages.get("crop") != "completed":
        return {
            "label": "產生智慧裁切軌",
            "command": "python main.py crop --config configs\\qualcomm_windows_arm64.yaml --job-id "
            + job_id,
        }
    return {
        "label": "等待下一個 pipeline 階段",
        "command": "目前下一階段開發：crop tracks + subtitle plan",
    }


def media_url(job_id: str, path: str | None) -> str | None:
    if not path:
        return None
    return f"/media?job={quote(job_id)}&path={quote(path)}"


def export_media_url(path: Path | str | None) -> str:
    if not path:
        return ""
    return f"/export?path={quote(str(path))}"


def metric(label: str, value: object) -> str:
    return f"""
    <div class="metric">
      <span>{tooltip(label)}</span>
      <strong>{html.escape(str(value))}</strong>
    </div>
    """


def render_fact(label: str, value: object) -> str:
    return f"""
    <div class="fact">
      <span>{tooltip(label)}</span>
      <strong>{html.escape(str(value))}</strong>
    </div>
    """


def platform_badge(env: dict) -> str:
    level = env.get("hardware_level", {}).get("level")
    q_ready = env.get("qualcomm_readiness", {}).get("tier")
    amd_ready = env.get("amd_readiness", {}).get("tier")
    intel_ready = env.get("intel_readiness", {}).get("tier")
    nvidia_ready = env.get("nvidia_readiness", {}).get("tier")
    if level == "Q":
        return f"Qualcomm {q_ready or 'Q'}"
    if level == "AM":
        return f"AMD {amd_ready or 'AM'}"
    if level == "I":
        return f"Intel {intel_ready or 'I'}"
    if level in {"A", "B", "C"}:
        return f"NVIDIA {nvidia_ready or level}"
    return str(level or "Unknown")


def human_artifact_name(path: str) -> str:
    mapping = {
        "job_manifest.json": "Job manifest",
        "reports/video_info.json": "Video info",
        "audio/audio_info.json": "Audio info",
        "transcripts/transcript.json": "Transcript",
        "transcripts/words.json": "Words",
        "transcripts/segments.json": "Semantic chunks",
        "llm/llm_input.json": "LLM input",
        "llm/clip_candidates.json": "Clip candidates",
        "clips/validated_clips.json": "Validated clips",
        "reports/effective_config.json": "Config",
    }
    return mapping.get(path, Path(path).name)


def tooltip(label: str) -> str:
    text = TOOLTIPS.get(label)
    if not text:
        return html.escape(label)
    return (
        f'<span class="tip" tabindex="0" title="{html.escape(text)}" data-tip="{html.escape(text)}">'
        f"{html.escape(label)}</span>"
    )


def format_seconds(value: object) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "unknown"
    minutes = int(seconds // 60)
    rest = seconds % 60
    if minutes:
        return f"{minutes}m {rest:.1f}s"
    return f"{rest:.1f}s"


def format_resolution(video: dict) -> str:
    width = video.get("width")
    height = video.get("height")
    if width and height:
        return f"{width}x{height}"
    return "unknown"


def format_audio(audio: dict) -> str:
    sample_rate = audio.get("sample_rate")
    channels = audio.get("channels")
    codec = audio.get("codec")
    if sample_rate and channels:
        return f"{sample_rate} Hz / {channels} ch"
    return codec or "pending"


def format_value(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def yes_no(value: object) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "unknown"


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #050607;
      --panel: #0d1014;
      --panel-2: #14191f;
      --line: #29313b;
      --line-2: #394553;
      --ink: #f4f7fb;
      --muted: #8f98a6;
      --soft: #c8d0db;
      --blue: #39a8ff;
      --blue-2: #0d69be;
      --green: #42d392;
      --orange: #e7a548;
      --red: #f06b6b;
      font-family: Inter, "Microsoft JhengHei", "Noto Sans TC", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ overflow-x: hidden; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      display: grid;
      grid-template-columns: 250px minmax(0, 1fr);
      letter-spacing: 0;
      overflow-x: hidden;
    }}
    a {{ color: inherit; text-decoration: none; }}
    h1, h2, h3, p {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 30px; line-height: 1.08; }}
    h2 {{ font-size: 22px; line-height: 1.15; }}
    h3 {{ font-size: 13px; color: var(--muted); text-transform: uppercase; margin: 0 0 10px; }}
    code {{
      display: block;
      margin-top: 10px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #080a0d;
      color: #d8e6f7;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: 12px;
    }}
    .sidebar {{
      min-height: 100vh;
      padding: 22px 18px;
      border-right: 1px solid var(--line);
      background: #030405;
      position: sticky;
      top: 0;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 800;
      font-size: 24px;
      margin-bottom: 34px;
    }}
    .brand-mark {{
      width: 34px;
      height: 34px;
      display: block;
      position: relative;
      border-radius: 8px;
      background: #06111d;
      border: 1px solid rgba(57, 168, 255, 0.5);
      box-shadow: 0 0 22px rgba(57, 168, 255, 0.2);
      overflow: hidden;
    }}
    .mark-play {{
      position: absolute;
      left: 8px;
      top: 7px;
      width: 0;
      height: 0;
      border-top: 10px solid transparent;
      border-bottom: 10px solid transparent;
      border-left: 16px solid var(--blue);
      filter: drop-shadow(0 0 8px rgba(57, 168, 255, 0.8));
    }}
    .mark-stack {{
      position: absolute;
      right: 6px;
      top: 8px;
      width: 4px;
      height: 18px;
      border-radius: 4px;
      background: var(--green);
      box-shadow: -6px 4px 0 rgba(66, 211, 146, 0.55);
    }}
    .nav {{ display: grid; gap: 8px; }}
    .nav a {{
      min-height: 46px;
      display: flex;
      align-items: center;
      padding: 0 14px;
      border-radius: 8px;
      color: var(--soft);
      font-weight: 700;
    }}
    .nav a.active {{
      background: rgba(57, 168, 255, 0.18);
      color: #9bd7ff;
      border: 1px solid rgba(57, 168, 255, 0.6);
    }}
    .side-note {{
      position: absolute;
      left: 18px;
      right: 18px;
      bottom: 22px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0a0d11;
      display: grid;
      gap: 4px;
      color: var(--muted);
    }}
    .side-note strong {{ color: var(--ink); }}
    .shell {{ min-width: 0; padding: 24px; }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 22px;
    }}
    .eyebrow {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 8px;
    }}
    .top-actions {{ display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }}
    .pill, .status-pill {{
      min-height: 36px;
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 0 14px;
      border: 1px solid var(--line-2);
      background: #171c24;
      font-weight: 800;
      color: #dce6f2;
      white-space: nowrap;
    }}
    .muted-pill {{ color: var(--muted); }}
    .tip {{
      position: relative;
      cursor: help;
      border-bottom: 1px dotted rgba(155, 215, 255, 0.7);
    }}
    .tip::after {{
      content: attr(data-tip);
      position: absolute;
      left: 0;
      bottom: calc(100% + 10px);
      width: min(320px, 72vw);
      padding: 12px 13px;
      border: 1px solid var(--line-2);
      border-radius: 8px;
      background: #151b23;
      color: #e9f2fb;
      font-size: 13px;
      line-height: 1.45;
      font-weight: 600;
      text-transform: none;
      box-shadow: 0 16px 38px rgba(0, 0, 0, 0.42);
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
      transform: translateY(6px);
      transition: opacity 140ms ease, transform 140ms ease;
      z-index: 20;
    }}
    .tip:hover::after, .tip:focus::after {{
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
    }}
    .top-actions .tip::after {{
      top: calc(100% + 10px);
      bottom: auto;
      left: auto;
      right: 0;
    }}
    .insight-panel .tip::after {{
      left: auto;
      right: 0;
    }}
    .workspace {{
      display: grid;
      grid-template-columns: minmax(230px, 0.8fr) minmax(340px, 1.25fr) minmax(210px, 0.75fr);
      gap: 18px;
      align-items: stretch;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      min-width: 0;
      box-shadow: 0 12px 40px rgba(0, 0, 0, 0.22);
    }}
    .panel-title {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 14px;
      margin-bottom: 18px;
    }}
    .progress-track {{
      height: 8px;
      background: #07090c;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid var(--line);
      margin-bottom: 18px;
    }}
    .progress-track span {{
      display: block;
      height: 100%;
      background: linear-gradient(90deg, var(--blue), var(--green));
    }}
    .timeline {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 9px;
    }}
    .timeline li {{
      min-height: 42px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0a0d11;
      color: var(--soft);
    }}
    .timeline li.done {{ border-color: rgba(66, 211, 146, 0.45); color: #d8f8e9; }}
    .timeline li.running, .timeline li.next {{ border-color: rgba(57, 168, 255, 0.7); color: #d9f0ff; background: rgba(57, 168, 255, 0.12); }}
    .timeline li.locked {{ opacity: 0.48; }}
    .timeline strong {{ font-size: 12px; }}
    .next-box {{
      margin-top: 18px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }}
    .next-box strong {{ display: block; font-size: 18px; }}
    .preview-stage {{
      min-height: 310px;
      border-radius: 8px;
      background: #000;
      display: grid;
      place-items: center;
      overflow: hidden;
      border: 1px solid #111820;
    }}
    .preview-video {{
      width: 100%;
      max-height: 460px;
      background: #000;
    }}
    .preview-empty {{
      color: var(--muted);
      font-weight: 800;
    }}
    .metric-grid {{
      margin-top: 16px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .metric, .fact {{
      padding: 12px;
      border-radius: 8px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      min-width: 0;
    }}
    .metric span, .fact span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 7px;
    }}
    .metric strong, .fact strong {{
      display: block;
      overflow-wrap: anywhere;
      font-size: 14px;
    }}
    .facts {{ display: grid; gap: 10px; }}
    .artifact-summary {{ margin-top: 20px; }}
    .transcript-card, .selection-card {{
      margin-top: 18px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0a0d11;
    }}
    .transcript-card p, .selection-card p {{
      color: var(--soft);
      line-height: 1.55;
      font-size: 14px;
    }}
    .selection-stats {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 10px 0 12px;
    }}
    .selection-stats span {{
      padding: 10px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      color: var(--muted);
      font-size: 12px;
      min-width: 0;
    }}
    .selection-stats strong {{
      display: block;
      margin-top: 4px;
      color: var(--ink);
      font-size: 20px;
    }}
    .candidate-row {{
      padding: 10px 0;
      border-top: 1px solid var(--line);
    }}
    .candidate-row strong, .candidate-row span {{
      display: block;
    }}
    .candidate-row span {{
      color: #8fd3ff;
      font-size: 12px;
      margin-top: 4px;
    }}
    .candidate-row p {{
      margin-bottom: 0;
    }}
    .mini-stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }}
    .mini-stats span {{
      min-height: 26px;
      display: inline-flex;
      align-items: center;
      padding: 0 9px;
      border-radius: 999px;
      background: rgba(57, 168, 255, 0.12);
      color: #b9e3ff;
      font-size: 12px;
      font-weight: 800;
    }}
    .artifact-chip {{
      display: grid;
      gap: 4px;
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0a0d11;
      margin-bottom: 8px;
    }}
    .artifact-chip span {{ font-weight: 800; }}
    .artifact-chip small {{ color: var(--muted); overflow-wrap: anywhere; }}
    .jobs {{ margin-top: 22px; }}
    .section-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
    .job-list {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: var(--panel);
    }}
    .job-row {{
      min-height: 54px;
      display: grid;
      grid-template-columns: 1.1fr 1.4fr 0.7fr 0.7fr 0.9fr;
      gap: 12px;
      align-items: center;
      padding: 0 16px;
      border-top: 1px solid var(--line);
      color: var(--soft);
    }}
    .job-row:first-child {{ border-top: 0; }}
    .job-row.selected {{ background: rgba(57, 168, 255, 0.12); color: var(--ink); }}
    .job-name {{ font-weight: 900; color: var(--ink); }}
    .empty-line {{ padding: 18px; color: var(--muted); }}
    .muted {{ color: var(--muted); }}
    .raw-page {{ min-height: 100vh; background: var(--bg); padding: 28px; }}
    .raw-top {{ margin-bottom: 16px; }}
    pre {{
      margin: 0;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #080a0d;
      color: #e7edf5;
      overflow: auto;
      line-height: 1.5;
      font-size: 13px;
    }}
    @media (max-width: 1180px) {{
      body {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; min-height: auto; }}
      .side-note {{ position: static; margin-top: 18px; }}
      .workspace {{ grid-template-columns: 1fr; }}
      .job-row {{ grid-template-columns: 1fr; padding: 14px 16px; }}
    }}
    @media (max-width: 720px) {{
      .shell {{ padding: 18px; }}
      .topbar {{ display: grid; }}
      .metric-grid {{ grid-template-columns: 1fr; }}
      .selection-stats {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 26px; }}
    }}
  </style>
</head>
<body>{body}</body>
</html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Dump2Done local verification dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-root", type=Path, default=Path("output/jobs"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    DashboardHandler.output_root = args.output_root
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dump2Done dashboard: http://{args.host}:{args.port}/")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
