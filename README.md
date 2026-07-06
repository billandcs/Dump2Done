# Dump2Done

Dump2Done is a local-first AI video post-production platform blueprint. Phase 1 focuses on environment probing, architecture design, NVIDIA-ready planning, and the MVP implementation roadmap.

## Phase 1 Deliverables

- `check_env.py`: local environment probe for OS, Python, FFmpeg, CPU/RAM/Disk, NVIDIA GPU/CUDA/NVENC, ASR, LLM, and Vision readiness.
- `configs/default.yaml`: initial config-driven profile for the future local MVP.
- `docs/phase1_architecture.md`: complete Traditional Chinese architecture blueprint, artifact schemas, prompts, CLI design, NVIDIA migration plan, and Phase 2 checklist.

## Run Environment Probe

```bash
python check_env.py --output output/env_report.json
```

If `python` is not available in PATH, install Python 3.10/3.11 or fix the launcher/PATH before Phase 2 implementation.

