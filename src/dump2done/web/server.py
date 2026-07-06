from __future__ import annotations

import argparse
import html
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


STAGE_LABELS = [
    ("analyze", "影片分析"),
    ("transcribe", "音訊抽取"),
    ("asr", "逐字稿"),
    ("select_clips", "精華片段"),
    ("crop", "智慧裁切"),
    ("subtitle", "字幕"),
    ("render", "輸出"),
]


class DashboardHandler(BaseHTTPRequestHandler):
    output_root = Path("output/jobs")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            query = parse_qs(parsed.query)
            self._send_html(render_index(self.output_root, query.get("job", [None])[0]))
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
        if parsed.path == "/env":
            self._send_html(render_env_report(self.output_root.parent / "env_report.json"))
            return
        if parsed.path == "/health":
            self._send_json({"status": "ok"})
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

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, output_root: Path, job_id: str, media_path: str) -> None:
        target = resolve_job_path(output_root, job_id, media_path)
        if not target or not target.exists() or not target.is_file():
            self.send_error(404, "Media not found")
            return

        mime_type, _ = mimetypes.guess_type(target.name)
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def render_index(output_root: Path, selected_job_id: str | None) -> str:
    jobs = list_jobs(output_root)
    env = read_json_or_none(output_root.parent / "env_report.json") or {}
    selected = select_job(jobs, selected_job_id)

    if selected:
        workspace = render_workspace(selected, env)
    else:
        workspace = render_empty_workspace(env)

    job_rows = "\n".join(render_job_row(job, selected) for job in jobs)
    if not job_rows:
        job_rows = '<div class="empty-line">目前沒有 job。</div>'

    return page(
        "Dump2Done Dashboard",
        f"""
        <aside class="sidebar">
          <a class="brand" href="/">
            <span class="brand-mark">D</span>
            <span>Dump2Done</span>
          </a>
          <nav class="nav">
            <a class="active" href="/">Pipeline</a>
            <a href="#jobs">Jobs</a>
            <a href="/env">Platform</a>
            <a href="https://github.com/billandcs/Dump2Done">GitHub</a>
          </nav>
          <div class="side-note">
            <span>Local</span>
            <strong>{html.escape(platform_badge(env))}</strong>
          </div>
        </aside>
        <div class="shell">
          <header class="topbar">
            <div>
              <p class="eyebrow">本地 AI 影片後製平台</p>
              <h1>Dump2Done 工作台</h1>
            </div>
            <div class="top-actions">
              <a class="pill" href="/env">{html.escape(platform_badge(env))}</a>
              <a class="pill muted-pill" href="/health">Health</a>
            </div>
          </header>
          {workspace}
          <section class="jobs" id="jobs">
            <div class="section-head">
              <div>
                <p class="eyebrow">Jobs</p>
                <h2>處理紀錄</h2>
              </div>
            </div>
            <div class="job-list">{job_rows}</div>
          </section>
        </div>
        """,
    )


def render_workspace(job: dict, env: dict) -> str:
    manifest = job["manifest"]
    job_id = manifest.get("job_id", job["dir"].name)
    video = job.get("video_info") or {}
    audio = job.get("audio_info") or {}
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
            <h2>{html.escape(platform_badge(env))}</h2>
          </div>
        </div>
        {render_platform_summary(env)}
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
        items.append(
            f'<li class="{state}"><span>{html.escape(label)}</span><strong>{html.escape(text)}</strong></li>'
        )
    return "\n".join(items)


def render_platform_summary(env: dict) -> str:
    level = env.get("hardware_level", {})
    qualcomm = env.get("qualcomm_platform", {})
    q_ready = env.get("qualcomm_readiness", {})
    ffmpeg = env.get("ffmpeg_codecs", {})
    rows = [
        ("Hardware", level.get("level", "unknown")),
        ("Qualcomm", yes_no(qualcomm.get("is_qualcomm_cpu"))),
        ("Python", "x64 emulation" if qualcomm.get("likely_emulated_python") else "native"),
        ("Q Readiness", q_ready.get("tier", "unknown")),
        ("QNN EP", yes_no(qualcomm.get("qnn_execution_provider_available"))),
        ("DirectML EP", yes_no(qualcomm.get("directml_execution_provider_available"))),
        ("Encoder", "CPU libx264" if not ffmpeg.get("gpu_encoding_available") else "GPU"),
    ]
    return '<div class="facts">' + "\n".join(render_fact(name, value) for name, value in rows) + "</div>"


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


def render_env_report(env_report_path: Path) -> str:
    if env_report_path.exists():
        content = env_report_path.read_text(encoding="utf-8", errors="replace")
    else:
        content = "Environment report not found. Run: python check_env.py --output output/env_report.json"

    return page(
        "Environment Report",
        f"""
        <div class="raw-page">
          <header class="topbar raw-top">
            <div>
              <p class="eyebrow">Platform</p>
              <h1>Environment Report</h1>
            </div>
            <a class="pill" href="/">Back</a>
          </header>
          <pre>{html.escape(content)}</pre>
        </div>
        """,
    )


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
            }
        )
    return jobs


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
    return {
        "label": "接上 faster-whisper ASR",
        "command": "下一輪實作：transcript.json + words.json",
    }


def media_url(job_id: str, path: str | None) -> str | None:
    if not path:
        return None
    return f"/media?job={quote(job_id)}&path={quote(path)}"


def metric(label: str, value: object) -> str:
    return f"""
    <div class="metric">
      <span>{html.escape(label)}</span>
      <strong>{html.escape(str(value))}</strong>
    </div>
    """


def render_fact(label: str, value: object) -> str:
    return f"""
    <div class="fact">
      <span>{html.escape(label)}</span>
      <strong>{html.escape(str(value))}</strong>
    </div>
    """


def platform_badge(env: dict) -> str:
    level = env.get("hardware_level", {}).get("level")
    q_ready = env.get("qualcomm_readiness", {}).get("tier")
    if level == "Q":
        return f"Qualcomm {q_ready or 'Q'}"
    return str(level or "Unknown")


def human_artifact_name(path: str) -> str:
    mapping = {
        "job_manifest.json": "Job manifest",
        "reports/video_info.json": "Video info",
        "audio/audio_info.json": "Audio info",
        "reports/effective_config.json": "Config",
    }
    return mapping.get(path, Path(path).name)


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
      display: grid;
      place-items: center;
      border-radius: 8px;
      background: linear-gradient(135deg, #1dc9ff, #1765ff);
      color: white;
      font-weight: 900;
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
