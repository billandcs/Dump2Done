# Qualcomm Windows on ARM Platform Plan

Dump2Done local development is now Qualcomm-first for this machine. NVIDIA remains a future server deployment target, but local MVP decisions should not assume CUDA, NVENC, TensorRT, or vLLM.

## Detected Direction

The current environment report shows a Qualcomm ARMv8 CPU model and no NVIDIA CUDA device. Python currently reports `AMD64`, which likely means the active Python is x64 running on Windows on ARM emulation. That is acceptable for early smoke tests, but native ARM64 Python should be preferred for sustained performance and package compatibility.

## Practical Local Profile

Use:

```bash
python check_env.py --output output/env_report.json
python main.py analyze --config configs/qualcomm_windows_arm64.yaml --input output/smoke_input.mp4 --job-id smoke_audio
python main.py transcribe --config configs/qualcomm_windows_arm64.yaml --job-id smoke_audio
python dashboard.py --port 8765
```

The Qualcomm profile intentionally chooses:

- `faster_whisper` on CPU with `compute_type: int8`.
- Smaller ASR model defaults: `small` first, `medium` only after profiling.
- `libx264`/`libx265` CPU rendering first.
- 720p vertical MVP output before 1080p.
- Sparse vision detection with interpolation, with `center_crop` as the first stable fallback.
- ONNX Runtime QNN/DirectML as future acceleration paths, not mandatory Phase 2 dependencies.

## Qualcomm Acceleration Strategy

### Now: Phase 2 CPU-First

The immediate goal is correctness and resumability:

- FFmpeg metadata and audio extraction.
- faster-whisper CPU int8 transcription.
- short chunk LLM selection.
- center crop / sparse detection.
- CPU render.

This keeps the pipeline usable before NPU/GPU runtime details are stable.

### Next: ONNX Runtime Abstraction

For Qualcomm acceleration, add an `OnnxRuntimeBackend` later with provider selection:

```text
providers:
  - QNNExecutionProvider
  - DmlExecutionProvider
  - CPUExecutionProvider
```

Use it first for models that are naturally ONNX-friendly:

- face/person detection
- object detection
- embeddings/ranking
- lightweight classifiers

Do not start with Whisper or LLM acceleration on QNN. Those paths are more sensitive to model shape, quantization, operators, and memory behavior.

### Later: QNN / Windows ML

ONNX Runtime's QNN Execution Provider is intended for hardware-accelerated execution on Qualcomm chipsets, using the Qualcomm AI Engine Direct / QNN SDK path, and it supports Windows devices with Snapdragon SoCs. The docs also note that Windows ARM64 local inferencing uses `onnxruntime-qnn`, and QNN HTP/NPU expects quantized models with fixed shapes.

Windows ML is relevant because Microsoft positions it as the Windows-supported ONNX Runtime layer that can run local models and use execution providers for NPUs, GPUs, and CPUs. This is the cleanest future direction for a Windows app or packaged local service.

DirectML remains a fallback for GPU acceleration on Windows. ONNX Runtime's DirectML EP supports broad DirectX 12 hardware and lists Qualcomm Adreno 600+ as compatible hardware.

## What Not To Do Locally

- Do not optimize local MVP around CUDA or TensorRT.
- Do not assume FFmpeg NVENC is usable just because the FFmpeg build lists NVENC encoders.
- Do not make every model ONNX/QNN immediately.
- Do not run high-FPS face detection on long videos.
- Do not use 1080x1920 output as the default benchmark until 720x1280 is stable.

## Implementation Consequences

Required abstractions:

- `ASRBackend`: `faster_whisper_cpu` now, future ONNX/Windows ML only if practical.
- `VisionBackend`: `center_crop`, `opencv`, future `onnxruntime_qnn`.
- `LLMBackend`: `ollama` now, future OpenAI-compatible or remote worker.
- `RenderBackend`: FFmpeg CPU now, future Media Foundation / hardware encode investigation.
- `PlatformProfile`: `qualcomm_windows_arm64`, `nvidia_server`, `local_cpu`.

## References

- ONNX Runtime QNN Execution Provider: https://onnxruntime.ai/docs/execution-providers/QNN-ExecutionProvider.html
- Windows ML overview: https://learn.microsoft.com/en-us/windows/ai/new-windows-ml/overview
- ONNX Runtime DirectML Execution Provider: https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html

