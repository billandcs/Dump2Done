# Dump2Done

Dump2Done is a local-first AI media editing control dashboard for short-form video and image workflows.

The current project is an active MVP, not a finished commercial editor. It can run a local web dashboard, inspect the machine environment, upload images/videos, produce deterministic local image/video outputs, and route generative image requests to real providers when those providers are available.

## Product Direction

Dump2Done is designed around a local-first migration path.

The long-term goal is that part, most, or eventually all media AI work can run on the user's own machine when local hardware and models are ready. Cloud services are treated as optional bridges, not the default center of the product. The UI and backend should therefore keep provider routing flexible:

- prefer local execution when a local provider is available
- expose cloud providers as explicit fallback choices
- show why a local route is not ready yet
- let users gradually move from cloud to local without changing the whole workflow
- keep artifacts, settings, and provider decisions visible enough for debugging

Current local-first layers:

| Layer | Local-first target | Temporary fallback |
| --- | --- | --- |
| Image filters | Pillow | None needed |
| Generative images | Automatic1111 / ComfyUI / local diffusion | OpenAI Images API |
| Video analysis/render | FFmpeg CPU baseline | Future remote worker for heavy workloads |
| Video segmentation/tracking | ONNX Runtime DirectML/QNN, later local models | Online or remote model only when explicitly enabled |
| ASR | Faster-Whisper CPU, later ONNX/accelerated local ASR | Online ASR only when explicitly enabled |
| LLM planning | Ollama or OpenAI-compatible local endpoint | OpenAI/remote LLM only when explicitly enabled |

## Current Status

| Area | Status | Notes |
| --- | --- | --- |
| Local dashboard | Working | Runs at `http://127.0.0.1:8765/`. |
| Environment report | Working | `/env` runs the local probe and shows platform readiness. |
| Image upload preview | Working | Supports image preview, prompt entry, output folder selection, and artifact gallery. |
| Local image filters | Working | Pillow-based rotate, brightness, grayscale, contrast, sharpen, blur, flip, and PNG export. |
| Generative image edit | Provider-gated | Cat-to-dog/object replacement needs Automatic1111, ComfyUI workflow + model, or OpenAI Images API. |
| Video upload preview | Working | Uploaded video is previewed in the browser. |
| Local video runner | MVP | FFmpeg frame extraction, deterministic Pillow frame edits, MP4 render. No true AI segmentation yet. |
| Live job tracking | Working | SSE log/status stream, active job tracker, cancel request support. |
| Artifact gallery | Working | Shows produced images, videos, and audio only. JSON/debug files stay in job folders. |
| Provider health cards | Working | Settings panel checks Pillow, FFmpeg, QNN, Faster-Whisper, Ollama, Automatic1111, ComfyUI, and OpenAI key readiness. |
| Qualcomm platform support | MVP | Qualcomm Windows on ARM is treated as the local-first target; native ARM64 Python is preferred. |
| QNN / DirectML acceleration | Readiness wired | ARM64 dashboard can install/register `onnxruntime-qnn` and report `QNNExecutionProvider`; production models are not routed through QNN yet. |
| Full AI video editing | Not implemented | Precise clothing/person tracking, segmentation, and high-quality generative video edits are future work. |

## What Works Today

### Local Web Dashboard

```powershell
python src\dump2done\web\server.py --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
```

On Qualcomm Windows ARM64 machines, use the native ARM64 virtual environment:

```powershell
.\scripts\start_dashboard_arm64.ps1
```

The expected environment report should show `python_machine: ARM64` and
`likely_emulated_python: false`.

Optional Qualcomm QNN readiness can be installed into the ARM64 virtual environment:

```powershell
.\.venv-arm64\Scripts\python.exe -m pip install .[qnn]
.\.venv-arm64\Scripts\python.exe check_env.py --output output\env_report_arm64_qnn.json
```

When QNN registration is healthy, the report should list `QNNExecutionProvider` under
`qualcomm_platform.onnxruntime.available_providers`.

The dashboard includes:

- image/video upload
- adaptive UI for image vs video
- prompt input
- configurable output folder under `output`
- live pipeline tracker
- SSE console log
- output gallery with preview/play/delete/open-folder actions
- settings panel for local/cloud provider routing
- local-first provider migration settings
- provider health cards and local readiness summary
- QNN provider registration status when running on native Windows ARM64 Python
- Traditional Chinese default UI, with English/Japanese language switching

### Environment Dashboard

```text
http://127.0.0.1:8765/env
```

The environment page runs the local probe and summarizes:

- platform and Python runtime
- Qualcomm readiness
- memory and disk context
- local capability readiness
- missing or future acceleration paths

Raw JSON is still available:

```text
http://127.0.0.1:8765/env?format=json
```

### Deterministic Local Image Editing

These run fully local through Pillow:

- rotate left/right 90 degrees
- brighten/darken
- grayscale
- contrast
- sharpen
- blur
- horizontal/vertical flip
- export PNG

Example prompts:

```text
往左旋轉90度
變亮一點並銳化
轉成黑白
```

### Generative Image Editing

Requests like this are generative and cannot be done by Pillow:

```text
把貓變成狗，保留照片構圖與背景
```

Dump2Done now routes these requests to real providers:

1. Automatic1111 / Stable Diffusion WebUI
2. ComfyUI image-to-image workflow
3. OpenAI Images API, only when explicitly selected

The intended order is local first. If no local route is ready, the dashboard reports the real blocker instead of copying the original image and pretending it succeeded. Auto mode does not silently upload images to OpenAI; the cloud route must be selected explicitly.

#### Automatic1111 Local Route

Start AUTOMATIC1111 with API enabled:

```powershell
webui-user.bat --api
```

Default endpoint:

```text
http://127.0.0.1:7860
```

Dump2Done calls:

```text
/sdapi/v1/img2img
```

This is currently the most practical local route for image-to-image edits such as cat-to-dog.

#### ComfyUI Local Route

Default endpoint:

```text
http://127.0.0.1:8188
```

Current support:

- detects whether ComfyUI is running
- checks whether checkpoint models are visible
- loads a ComfyUI API-format workflow JSON
- uploads the source image through `/upload/image`
- queues the workflow through `/prompt`
- polls `/history/{prompt_id}`
- downloads the first generated image through `/view`
- reports missing checkpoint/workflow blockers as user-facing setup guidance

Default workflow:

```text
configs/comfyui_image_to_image_workflow.example.json
```

The default workflow fills these placeholders automatically:

```text
{{input_image}}
{{prompt}}
{{negative_prompt}}
{{checkpoint}}
{{job_id}}
```

You can point the dashboard setting `ComfyUI Workflow JSON` to an exported ComfyUI API workflow. ComfyUI still needs at least one visible checkpoint model before cat-to-dog or object replacement can run locally.

#### OpenAI Images API Route

Set an API key before starting the dashboard:

```powershell
$env:OPENAI_API_KEY="sk-..."
python src\dump2done\web\server.py --host 127.0.0.1 --port 8765
```

Default image model:

```text
gpt-image-1.5
```

Important: ChatGPT Pro does not automatically provide an API key to local Python code. The app needs `OPENAI_API_KEY` in the process environment.

### Local Video MVP

Uploaded videos currently use a local deterministic runner:

- FFmpeg analyzes the video
- FFmpeg extracts frames
- Pillow applies simple frame-level edits
- FFmpeg renders an MP4
- progress/logs stream back to the dashboard

Current video editing is intentionally limited. For example, "change clothing color" is currently handled by a deterministic center-region MVP, not true subject-aware AI segmentation.

## Install

Python 3.10+ is recommended.

```powershell
python -m pip install -e .
python -m pip install -r requirements.txt
```

Optional MVP packages:

```powershell
python -m pip install faster-whisper ctranslate2 opencv-python mediapipe requests
```

FFmpeg should be available on PATH for video features:

```powershell
ffmpeg -version
ffprobe -version
```

## Run Environment Probe

```powershell
python check_env.py --output output/env_report.json
```

## CLI Smoke Commands

The older CLI pipeline still exists for local artifact generation:

```powershell
python main.py analyze --config configs/qualcomm_windows_arm64.yaml --input output/smoke_input.mp4 --job-id smoke_audio
python main.py transcribe --config configs/qualcomm_windows_arm64.yaml --job-id smoke_audio
```

The web dashboard is now the primary verification surface.

## Platform Notes

This development machine is treated as Qualcomm Windows on ARM-first.

Local assumptions:

- prefer CPU-safe paths
- do not assume CUDA/NVENC
- do not claim QNN/DirectML acceleration unless the provider is actually detected
- keep FFmpeg CPU render as the stable baseline

Future platform routes:

- Qualcomm QNNExecutionProvider for ONNX models
- DirectMLExecutionProvider for Windows GPU fallback
- OpenVINO / Intel route
- AMD DirectML / ROCm route
- remote/cloud worker route for heavier models

The migration rule is simple: add the cloud route only when it helps users keep working, but keep the same job/artifact shape so the route can later be replaced by a local model.

## Known Limitations

- Cat-to-dog requires a real generative image provider. Pillow cannot do it.
- ComfyUI support is readiness-only until a workflow JSON route is added.
- Automatic1111 must be running with `--api`.
- OpenAI Images API requires `OPENAI_API_KEY`.
- Video generation and true video object replacement are not implemented.
- Person/clothing tracking is MVP-only, not robust segmentation/tracking.
- The dashboard is local-only and not hardened for multi-user deployment.

## Repository Map

```text
check_env.py                         Environment probe
src/dump2done/web/server.py          Local web dashboard and API routes
src/dump2done/pipeline/video_edit.py Local deterministic video runner
src/dump2done/pipeline/runner.py     Older artifact pipeline runner
src/dump2done/media/                 FFmpeg/FFprobe helpers
configs/                             Platform profiles
docs/                                Architecture and platform notes
output/                              Local generated jobs, reports, logs, exports
```

## Roadmap

- Add ComfyUI workflow JSON configuration and queue submission.
- Add local model install/readiness guide for Stable Diffusion or FLUX on Windows.
- Add true segmentation/tracking backend for clothing/person edits.
- Add ONNX Runtime DirectML/QNN experiments for Qualcomm local acceleration.
- Add local LLM planning through Ollama/OpenAI-compatible local endpoints.
- Add local ASR acceleration experiments.
- Add richer job retry/resume controls.
- Split web templates out of `server.py` once the UI stabilizes.
