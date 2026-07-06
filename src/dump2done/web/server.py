from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class DashboardHandler(BaseHTTPRequestHandler):
    output_root = Path("output/jobs")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(render_index(self.output_root))
            return
        if parsed.path == "/artifact":
            query = parse_qs(parsed.query)
            job_id = query.get("job", [""])[0]
            artifact = query.get("path", [""])[0]
            self._send_html(render_artifact(self.output_root, job_id, artifact))
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


def render_index(output_root: Path) -> str:
    jobs = list_jobs(output_root)
    cards = "\n".join(render_job_card(job) for job in jobs)
    if not cards:
        cards = '<section class="empty">No jobs yet. Run <code>python main.py analyze ...</code>.</section>'

    return page(
        "Dump2Done Local Verification",
        f"""
        <header>
          <div>
            <p class="eyebrow">Local verification dashboard</p>
            <h1>Dump2Done Jobs</h1>
          </div>
          <a class="button" href="/health">Health JSON</a>
        </header>
        <main class="grid">{cards}</main>
        """,
    )


def render_artifact(output_root: Path, job_id: str, artifact: str) -> str:
    safe_job = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")
    job_dir = (output_root / safe_job).resolve()
    target = (job_dir / artifact).resolve()

    if not str(target).startswith(str(job_dir)) or not target.exists():
        content = "Artifact not found."
    else:
        content = target.read_text(encoding="utf-8", errors="replace")

    return page(
        f"{job_id} / {artifact}",
        f"""
        <header>
          <div>
            <p class="eyebrow">Artifact</p>
            <h1>{html.escape(job_id)} / {html.escape(artifact)}</h1>
          </div>
          <a class="button" href="/">Back</a>
        </header>
        <main><pre>{html.escape(content)}</pre></main>
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
        jobs.append({"dir": job_dir, "manifest": manifest, "artifacts": artifacts})
    return jobs


def render_job_card(job: dict) -> str:
    manifest = job["manifest"]
    job_id = manifest.get("job_id", job["dir"].name)
    stages = manifest.get("stages", {})
    artifacts = job["artifacts"]
    stage_items = "\n".join(
        f'<li><span>{html.escape(str(name))}</span><strong>{html.escape(str(status))}</strong></li>'
        for name, status in stages.items()
    )
    if not stage_items:
        stage_items = "<li><span>No stages</span><strong>pending</strong></li>"

    artifact_items = "\n".join(
        f'<li><a href="/artifact?job={html.escape(job_id)}&path={html.escape(path)}">{html.escape(path)}</a></li>'
        for path in artifacts
    )
    if not artifact_items:
        artifact_items = "<li>No JSON artifacts yet</li>"

    original = manifest.get("input", {}).get("original_filename", "unknown")
    return f"""
    <article class="card">
      <div class="card-head">
        <div>
          <p class="eyebrow">Job</p>
          <h2>{html.escape(job_id)}</h2>
        </div>
        <span class="status">{html.escape(str(manifest.get("status", "unknown")))}</span>
      </div>
      <p class="muted">Input: {html.escape(original)}</p>
      <h3>Stages</h3>
      <ul class="stages">{stage_items}</ul>
      <h3>Artifacts</h3>
      <ul class="artifacts">{artifact_items}</ul>
    </article>
    """


def read_json_or_none(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --ink: #1c1d20;
      --muted: #686c73;
      --line: #d9dbdf;
      --accent: #176b87;
      --accent-2: #8b4e2f;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); }}
    header {{
      min-height: 112px;
      padding: 28px clamp(18px, 4vw, 48px);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }}
    h1, h2, h3, p {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: clamp(28px, 4vw, 44px); line-height: 1.05; }}
    h2 {{ font-size: 22px; line-height: 1.15; }}
    h3 {{ margin-top: 18px; margin-bottom: 8px; font-size: 13px; text-transform: uppercase; color: var(--muted); }}
    .eyebrow {{ margin-bottom: 7px; font-size: 12px; font-weight: 700; text-transform: uppercase; color: var(--accent-2); }}
    .button {{
      display: inline-flex;
      align-items: center;
      min-height: 38px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--ink);
      text-decoration: none;
      background: #fff;
      white-space: nowrap;
    }}
    .grid {{
      padding: 24px clamp(18px, 4vw, 48px);
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      min-width: 0;
    }}
    .card-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }}
    .status {{ padding: 5px 8px; border-radius: 999px; background: #e7f1f4; color: var(--accent); font-size: 12px; font-weight: 700; }}
    .muted {{ margin-top: 10px; color: var(--muted); overflow-wrap: anywhere; }}
    ul {{ list-style: none; margin: 0; padding: 0; }}
    .stages li {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 7px 0;
      border-top: 1px solid #eceef0;
    }}
    .stages strong {{ color: var(--accent); }}
    .artifacts li {{ padding: 5px 0; overflow-wrap: anywhere; }}
    a {{ color: var(--accent); }}
    pre {{
      margin: 24px clamp(18px, 4vw, 48px);
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #101418;
      color: #f4f7fb;
      overflow: auto;
      line-height: 1.45;
      font-size: 13px;
    }}
    .empty {{
      padding: 28px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fff;
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
