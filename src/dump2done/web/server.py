from __future__ import annotations

import argparse
import html
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
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

TOOLTIPS = {
    "Qualcomm Q1": "這代表目前這台機器是 Qualcomm 平台，適合先用 CPU int8 跑本地 MVP，未來再評估 QNN/DirectML 加速。Q1 不是效能分數滿分，而是「可開發、待加速優化」。",
    "Hardware": "Dump2Done 對目前機器的整體分級。Q 代表 Qualcomm Windows on ARM 路線。",
    "Qualcomm": "是否偵測到 Qualcomm / Snapdragon 類 CPU 平台。",
    "Python": "目前 Python 是否可能跑在 x64 模擬層。native ARM64 通常會更適合這台機器。",
    "Q Readiness": "Qualcomm-ready 程度。Q1 表示先走 CPU 穩定 pipeline，之後準備 QNN/DirectML。",
    "QNN EP": "ONNX Runtime 的 Qualcomm NPU 執行後端，可讓合適的 ONNX 模型跑在 Hexagon NPU。不是所有模型都適合。",
    "DirectML EP": "ONNX Runtime 的 Windows GPU 執行後端，可作為 Qualcomm Adreno GPU 的未來 fallback。",
    "Encoder": "目前影片輸出編碼路線。這台機器本地先用 CPU libx264，不假設 NVENC。",
    "ASR": "Automatic Speech Recognition，自動語音辨識。這一步會把音訊轉成 transcript.json 和 words.json。",
    "影片分析": "讀取影片基本資料，例如長度、解析度、FPS、影音 codec。這一步使用 ffprobe。",
    "音訊抽取": "把影片音軌抽成 16kHz mono WAV，方便後續語音辨識。",
    "逐字稿": "使用 faster-whisper 把語音轉成段落與 word-level timestamps。",
    "精華片段": "未來會用 LLM 從逐字稿中挑出可做短影音的候選片段。",
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
            <span class="brand-mark" aria-label="Dump2Done logo">
              <span class="mark-play"></span>
              <span class="mark-stack"></span>
            </span>
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
            <strong>{tooltip(platform_badge(env))}</strong>
          </div>
        </aside>
        <div class="shell">
          <header class="topbar">
            <div>
              <p class="eyebrow">本地 AI 影片後製平台</p>
              <h1>Dump2Done 工作台</h1>
            </div>
            <div class="top-actions">
              <a class="pill" href="/env">{tooltip(platform_badge(env))}</a>
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
    transcript = job.get("transcript_info") or {}
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
    compute = report.get("compute", {})
    memory = compute.get("memory", {})
    disk = compute.get("disk", {})
    gpu = report.get("gpu_cuda", {})
    ffmpeg = report.get("ffmpeg_codecs", {})
    asr = report.get("asr", {})
    llm = report.get("llm", {})
    qualcomm = report.get("qualcomm_platform", {})
    q_ready = report.get("qualcomm_readiness", {})
    n_ready = report.get("nvidia_readiness", {})
    hardware = report.get("hardware_level", {})

    memory_used = clamp_percent(memory.get("percent_used"))
    disk_total = safe_float(disk.get("cwd_total_gb"))
    disk_free = safe_float(disk.get("cwd_free_gb"))
    disk_used = clamp_percent(((disk_total - disk_free) / disk_total * 100) if disk_total else 0)
    emulated = bool(qualcomm.get("likely_emulated_python"))

    software_cards = "\n".join(
        [
            dependency_card("Python", basic.get("python", {}).get("version"), True, "terminal"),
            dependency_card("pip", basic.get("pip", {}).get("version"), basic.get("pip", {}).get("available"), "package"),
            dependency_card("Git", basic.get("git", {}).get("version"), basic.get("git", {}).get("available"), "git-branch"),
            dependency_card("FFmpeg", basic.get("ffmpeg", {}).get("version"), basic.get("ffmpeg", {}).get("available"), "film"),
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
              <p class="text-sm font-semibold uppercase tracking-wide text-orange-200">Critical Performance Warning</p>
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
  <title>Dump2Done Platform Environment Report</title>
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
        <div class="mb-3 inline-flex items-center gap-2 rounded-full border border-lime-300/30 bg-lime-300/10 px-3 py-1 text-xs font-semibold text-lime-200">
          <i data-lucide="radar" class="h-4 w-4"></i>
          Live probe generated on request
        </div>
        <h1 class="text-3xl font-black tracking-tight md:text-5xl">Platform Environment Report</h1>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
          即時診斷 Dump2Done 本地硬體、AI runtime、依賴元件與 Qualcomm / NVIDIA readiness。
        </p>
      </div>
      <div class="flex flex-wrap gap-2">
        <a href="/" class="rounded-xl border border-slate-700 bg-panel2 px-4 py-2 text-sm font-bold text-slate-200 hover:border-sky-400">Back to Dashboard</a>
        <a href="/env?format=json" class="rounded-xl border border-lime-300/40 bg-lime-300/10 px-4 py-2 text-sm font-bold text-lime-200 hover:bg-lime-300/20">Raw JSON</a>
      </div>
    </header>

    __WARNING__

    <section class="mt-5 grid gap-4 lg:grid-cols-4">
      __HERO_CARD__
      __MEMORY_CARD__
      __DISK_CARD__
      __PYTHON_CARD__
    </section>

    <section class="mt-5 grid gap-5 lg:grid-cols-2">
      __NVIDIA_CARD__
      __QUALCOMM_CARD__
    </section>

    <section class="mt-5 rounded-2xl border border-slate-800 bg-panel/95 p-5 shadow-xl">
      <div class="mb-4 flex items-center justify-between gap-3">
        <div>
          <p class="text-xs font-bold uppercase tracking-wide text-slate-500">Dependency Matrix</p>
          <h2 class="mt-1 text-xl font-black">軟體依賴元件清單</h2>
        </div>
        <i data-lucide="layout-grid" class="h-6 w-6 text-lime-300"></i>
      </div>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">__SOFTWARE_CARDS__</div>
    </section>

    <section class="mt-5 grid gap-5 lg:grid-cols-3">
      __CPU_CARD__
      __AI_CARD__
      __RECOMMEND_CARD__
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
        "__HERO_CARD__": env_summary_card("Hardware Level", hardware.get("level", "unknown"), hardware.get("reason", "No reason available."), "activity", "lime"),
        "__MEMORY_CARD__": progress_card("Memory Used", f"{memory_used:.0f}%", memory_used, "memory-stick", "lime" if memory_used < 70 else "orange"),
        "__DISK_CARD__": progress_card("Workspace Disk Used", f"{disk_used:.0f}%", disk_used, "hard-drive", "lime" if disk_used < 75 else "orange", f"{disk_free:.1f} GB free"),
        "__PYTHON_CARD__": env_summary_card("Python Runtime", basic.get("python", {}).get("version", "unknown"), "x64 emulation likely" if emulated else "native or non-ARM path", "terminal", "orange" if emulated else "lime"),
        "__NVIDIA_CARD__": readiness_card("NVIDIA Readiness", n_ready, nvidia_blockers(report), "gpu", "sky"),
        "__QUALCOMM_CARD__": readiness_card("Qualcomm Readiness", q_ready, q_ready.get("blockers", []), "cpu", "lime"),
        "__SOFTWARE_CARDS__": software_cards,
        "__CPU_CARD__": detail_card("CPU / OS", "cpu", [
            ("CPU", compute.get("cpu", {}).get("model", "unknown")),
            ("Threads", compute.get("cpu", {}).get("logical_threads", "unknown")),
            ("OS", f"{basic.get('os', {}).get('system', 'unknown')} {basic.get('os', {}).get('release', '')}"),
        ]),
        "__AI_CARD__": detail_card("AI Runtime", "brain", [
            ("ASR", "faster-whisper installed" if asr.get("faster_whisper_installed") else "missing"),
            ("Ollama API", "available" if llm.get("ollama_api", {}).get("available") else "unavailable"),
            ("Local models", ", ".join(llm.get("ollama_api", {}).get("models", [])) or "none"),
        ]),
        "__RECOMMEND_CARD__": detail_card("Optimization Notes", "wrench", [
            ("Profile", qualcomm.get("recommended_local_profile", "default")),
            ("Encoder", "CPU libx264/libx265" if not ffmpeg.get("gpu_encoding_available") else "GPU encoder available"),
            ("Next", "native ARM64 Python + ONNX Runtime QNN/DirectML validation" if emulated else "ASR and clip pipeline profiling"),
        ]),
    }
    for key, value in replacements.items():
        html_doc = html_doc.replace(key, value)
    return html_doc


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
    if not report.get("ffmpeg_codecs", {}).get("gpu_encoding_available"):
        blockers.append("NVENC is not usable locally; use CPU encoder on Qualcomm.")
    return blockers


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
    if stages.get("asr") != "completed":
        return {
            "label": "跑 faster-whisper ASR",
            "command": "python main.py transcribe --config configs\\qualcomm_windows_arm64.yaml --job-id "
            + job_id,
        }
    return {
        "label": "進入語意切片",
        "command": "下一輪實作：segments.json + LLM clip candidates",
    }


def media_url(job_id: str, path: str | None) -> str | None:
    if not path:
        return None
    return f"/media?job={quote(job_id)}&path={quote(path)}"


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
    if level == "Q":
        return f"Qualcomm {q_ready or 'Q'}"
    return str(level or "Unknown")


def human_artifact_name(path: str) -> str:
    mapping = {
        "job_manifest.json": "Job manifest",
        "reports/video_info.json": "Video info",
        "audio/audio_info.json": "Audio info",
        "transcripts/transcript.json": "Transcript",
        "transcripts/words.json": "Words",
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
    .transcript-card {{
      margin-top: 18px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0a0d11;
    }}
    .transcript-card p {{
      color: var(--soft);
      line-height: 1.55;
      font-size: 14px;
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
