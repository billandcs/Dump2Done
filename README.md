# Dump2Done

AI Video Online Editor.

Dump2Done is a local-first AI video post-production platform blueprint. Phase 1 focuses on environment probing, architecture design, NVIDIA-ready planning, and the MVP implementation roadmap. Phase 2 starts the local MVP CLI and artifact-driven pipeline.

## Phase 1 Deliverables

- `check_env.py`: local environment probe for OS, Python, FFmpeg, CPU/RAM/Disk, NVIDIA GPU/CUDA/NVENC, ASR, LLM, and Vision readiness.
- `configs/default.yaml`: initial config-driven profile for the future local MVP.
- `docs/phase1_architecture.md`: complete Traditional Chinese architecture blueprint, artifact schemas, prompts, CLI design, NVIDIA migration plan, and Phase 2 checklist.

## Phase 2 Starter

- `main.py`: CLI entrypoint.
- `src/dump2done/`: package layout for config, job artifacts, media analysis, and pipeline stages.
- `pyproject.toml`: package metadata and optional MVP dependencies.
- `dashboard.py`: local verification dashboard for generated job artifacts.
- `configs/qualcomm_windows_arm64.yaml`: Qualcomm-first local profile for Windows on ARM development.
- `docs/qualcomm_platform.md`: Qualcomm platform strategy and future ONNX Runtime QNN/DirectML plan.

## Run Environment Probe

```bash
python check_env.py --output output/env_report.json
```

If `python` is not available in PATH, install Python 3.10/3.11 or fix the launcher/PATH before Phase 2 implementation.

## Run Local Verification Dashboard

```bash
python dashboard.py --port 8765
```

Then open `http://127.0.0.1:8765/`.

## Qualcomm Windows on ARM Profile

```bash
python main.py analyze --config configs/qualcomm_windows_arm64.yaml --input output/smoke_input.mp4 --job-id smoke_audio
python main.py transcribe --config configs/qualcomm_windows_arm64.yaml --job-id smoke_audio
```
