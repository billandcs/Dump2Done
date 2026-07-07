"""Dump2Done local environment probe.

This script checks whether the current machine is suitable for the first
Dump2Done development pipeline and prints a JSON report. It intentionally uses
the Python standard library first, then detects optional packages if installed.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def run_command(command: list[str], timeout: int = 10) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if not executable:
        return {
            "available": False,
            "command": command,
            "error": f"{command[0]} not found in PATH",
        }

    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "available": completed.returncode == 0,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "command": command,
            "error": f"Command timed out after {timeout}s",
        }
    except Exception as exc:  # pragma: no cover - defensive OS boundary
        return {"available": False, "command": command, "error": str(exc)}


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def import_version(name: str) -> str | None:
    if not module_available(name):
        return None
    try:
        module = __import__(name)
        return getattr(module, "__version__", None)
    except Exception:
        return None


def bytes_to_gb(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024**3), 2)


def get_memory_info() -> dict[str, Any]:
    if module_available("psutil"):
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        return {
            "source": "psutil",
            "total_gb": bytes_to_gb(memory.total),
            "available_gb": bytes_to_gb(memory.available),
            "percent_used": memory.percent,
        }

    if platform.system().lower() == "windows":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {
                "source": "ctypes",
                "total_gb": bytes_to_gb(status.ullTotalPhys),
                "available_gb": bytes_to_gb(status.ullAvailPhys),
                "percent_used": status.dwMemoryLoad,
            }

    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            available_pages = os.sysconf("SC_AVPHYS_PAGES")
            return {
                "source": "sysconf",
                "total_gb": bytes_to_gb(pages * page_size),
                "available_gb": bytes_to_gb(available_pages * page_size),
                "percent_used": None,
            }
        except (ValueError, OSError):
            pass

    return {
        "source": "unknown",
        "total_gb": None,
        "available_gb": None,
        "percent_used": None,
    }


def get_cpu_model() -> str:
    processor = platform.processor()
    if processor:
        return processor

    system = platform.system().lower()
    if system == "windows":
        result = run_command(["wmic", "cpu", "get", "name"], timeout=5)
        if result.get("available") and result.get("stdout"):
            lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
            if len(lines) > 1:
                return lines[1]
    elif system == "linux":
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass

    return "unknown"


def get_video_controllers() -> list[str]:
    controllers: list[str] = []
    if platform.system().lower() == "windows":
        result = run_command(["wmic", "path", "win32_VideoController", "get", "name"], timeout=5)
        if result.get("available") and result.get("stdout"):
            lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
            controllers.extend(line for line in lines if line.lower() != "name")
        if not controllers:
            ps = run_command(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
                ],
                timeout=8,
            )
            if ps.get("available") and ps.get("stdout"):
                controllers.extend(line.strip() for line in ps["stdout"].splitlines() if line.strip())
    elif platform.system().lower() == "linux":
        result = run_command(["lspci"], timeout=5)
        if result.get("available") and result.get("stdout"):
            for line in result["stdout"].splitlines():
                lowered = line.lower()
                if "vga" in lowered or "3d controller" in lowered or "display controller" in lowered:
                    controllers.append(line.strip())
    return sorted(set(controllers))


def parse_first_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def check_basic_tools() -> dict[str, Any]:
    pip_result = run_command([sys.executable, "-m", "pip", "--version"], timeout=10)
    ffmpeg_result = run_command(["ffmpeg", "-version"], timeout=10)
    ffprobe_result = run_command(["ffprobe", "-version"], timeout=10)
    git_result = run_command(["git", "--version"], timeout=10)

    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "is_venv": sys.prefix != sys.base_prefix,
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
        },
        "pip": {
            "available": pip_result.get("available", False),
            "version": parse_first_line(pip_result.get("stdout") or pip_result.get("stderr")),
        },
        "ffmpeg": {
            "available": ffmpeg_result.get("available", False),
            "version": parse_first_line(ffmpeg_result.get("stdout")),
        },
        "ffprobe": {
            "available": ffprobe_result.get("available", False),
            "version": parse_first_line(ffprobe_result.get("stdout")),
        },
        "git": {
            "available": git_result.get("available", False),
            "version": parse_first_line(git_result.get("stdout") or git_result.get("stderr")),
        },
    }


def check_compute() -> dict[str, Any]:
    cwd_disk = shutil.disk_usage(Path.cwd())
    temp_disk = shutil.disk_usage(tempfile.gettempdir())
    memory = get_memory_info()
    cpu_count = os.cpu_count()

    total_ram = memory.get("total_gb") or 0
    can_handle_long_video = bool(
        total_ram >= 16
        and bytes_to_gb(cwd_disk.free) is not None
        and (bytes_to_gb(cwd_disk.free) or 0) >= 50
    )

    return {
        "cpu": {
            "model": get_cpu_model(),
            "logical_threads": cpu_count,
            "physical_cores": None,
            "note": "Install psutil to report physical core count accurately."
            if not module_available("psutil")
            else None,
        },
        "memory": memory,
        "disk": {
            "cwd": str(Path.cwd()),
            "cwd_free_gb": bytes_to_gb(cwd_disk.free),
            "cwd_total_gb": bytes_to_gb(cwd_disk.total),
            "temp_dir": tempfile.gettempdir(),
            "temp_free_gb": bytes_to_gb(temp_disk.free),
            "recommended_temp_dir": "Use a fast SSD/NVMe path with at least 100GB free for long videos.",
        },
        "long_video_suitability": {
            "suitable_for_30_min_plus": can_handle_long_video,
            "reason": "RAM >= 16GB and workspace disk free >= 50GB"
            if can_handle_long_video
            else "Use chunk-based processing; upgrade RAM/disk for reliable 30min+ jobs.",
        },
    }


def parse_nvidia_smi_csv(stdout: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        name, memory_total, driver_version, cuda_version = parts[:4]
        try:
            memory_gb = round(float(memory_total.split()[0]) / 1024, 2)
        except (ValueError, IndexError):
            memory_gb = None
        gpus.append(
            {
                "name": name,
                "vram_gb": memory_gb,
                "driver_version": driver_version,
                "cuda_version_from_driver": cuda_version,
            }
        )
    return gpus


def check_torch_cuda() -> dict[str, Any]:
    if not module_available("torch"):
        return {
            "installed": False,
            "cuda_available": False,
            "version": None,
            "cuda_runtime_version": None,
        }

    try:
        import torch  # type: ignore

        device_count = torch.cuda.device_count() if torch.cuda.is_available() else 0
        devices = []
        for index in range(device_count):
            capability = torch.cuda.get_device_capability(index)
            props = torch.cuda.get_device_properties(index)
            devices.append(
                {
                    "index": index,
                    "name": props.name,
                    "total_memory_gb": bytes_to_gb(props.total_memory),
                    "compute_capability": f"{capability[0]}.{capability[1]}",
                    "fp16_likely_supported": capability[0] >= 5,
                }
            )

        return {
            "installed": True,
            "version": getattr(torch, "__version__", None),
            "cuda_available": torch.cuda.is_available(),
            "cuda_runtime_version": getattr(torch.version, "cuda", None),
            "device_count": device_count,
            "devices": devices,
        }
    except Exception as exc:
        return {
            "installed": True,
            "cuda_available": False,
            "error": str(exc),
        }


def check_gpu_cuda() -> dict[str, Any]:
    smi = run_command(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version,cuda_version",
            "--format=csv,noheader",
        ],
        timeout=10,
    )
    nvcc = run_command(["nvcc", "--version"], timeout=10)
    torch_cuda = check_torch_cuda()
    gpus = parse_nvidia_smi_csv(smi.get("stdout", "")) if smi.get("available") else []
    max_vram = max([gpu.get("vram_gb") or 0 for gpu in gpus], default=0)

    cuda_usable = bool(smi.get("available") and (torch_cuda.get("cuda_available") or nvcc.get("available")))
    fp16_supported = bool(
        any(device.get("fp16_likely_supported") for device in torch_cuda.get("devices", []))
        or max_vram >= 6
    )

    return {
        "nvidia_smi_available": smi.get("available", False),
        "gpus": gpus,
        "gpu_count": len(gpus),
        "max_vram_gb": max_vram,
        "nvcc_available": nvcc.get("available", False),
        "nvcc_version": parse_first_line(nvcc.get("stdout") or nvcc.get("stderr")),
        "torch": torch_cuda,
        "cuda_usable": cuda_usable,
        "fp16_likely_supported": fp16_supported,
        "faster_whisper_cuda_recommendation": (
            'Use device="cuda", compute_type="float16" or "int8_float16".'
            if cuda_usable and fp16_supported
            else 'Use device="cpu", compute_type="int8", or install CUDA-enabled PyTorch/CTranslate2.'
        ),
        "fallback": (
            "CPU int8 ASR, CPU/libx264 rendering, lower detection FPS, or external/API LLM."
            if not cuda_usable
            else None
        ),
    }


def check_ffmpeg_codecs() -> dict[str, Any]:
    encoders = run_command(["ffmpeg", "-hide_banner", "-encoders"], timeout=20)
    decoders = run_command(["ffmpeg", "-hide_banner", "-decoders"], timeout=20)
    encoder_text = (encoders.get("stdout") or "") + "\n" + (encoders.get("stderr") or "")
    decoder_text = (decoders.get("stdout") or "") + "\n" + (decoders.get("stderr") or "")

    encoder_names = [
        "h264_nvenc",
        "hevc_nvenc",
        "av1_nvenc",
        "h264_amf",
        "hevc_amf",
        "av1_amf",
        "h264_qsv",
        "hevc_qsv",
        "av1_qsv",
        "libx264",
        "libx265",
    ]
    decoder_names = ["h264_cuvid", "hevc_cuvid"]
    supported_encoders = {name: name in encoder_text for name in encoder_names}
    supported_decoders = {name: name in decoder_text for name in decoder_names}
    nvenc_build_available = any(
        supported_encoders.get(name) for name in ["h264_nvenc", "hevc_nvenc", "av1_nvenc"]
    )
    amf_build_available = any(
        supported_encoders.get(name) for name in ["h264_amf", "hevc_amf", "av1_amf"]
    )
    qsv_build_available = any(
        supported_encoders.get(name) for name in ["h264_qsv", "hevc_qsv", "av1_qsv"]
    )

    return {
        "encoders_query_available": encoders.get("available", False),
        "decoders_query_available": decoders.get("available", False),
        "encoders": supported_encoders,
        "decoders": supported_decoders,
        "nvenc_build_available": nvenc_build_available,
        "amf_build_available": amf_build_available,
        "qsv_build_available": qsv_build_available,
        "gpu_encoding_available": nvenc_build_available or amf_build_available or qsv_build_available,
        "platform_encoders": {
            "nvidia_nvenc": nvenc_build_available,
            "amd_amf": amf_build_available,
            "intel_qsv": qsv_build_available,
        },
        "cpu_encoding_available": supported_encoders.get("libx264") or supported_encoders.get("libx265"),
        "recommendation": (
            "Prefer h264_nvenc for MVP exports; try hevc_nvenc for smaller files."
            if nvenc_build_available
            else "Use available platform encoder (AMF/QSV) or libx264/libx265; validate output quality per machine."
            if amf_build_available or qsv_build_available
            else "Use libx264/libx265 and expect render time to be a major bottleneck."
        ),
        "long_video_bottleneck": (
            "ASR, smart crop detection, and final render I/O will dominate; process clips/chunks."
            if nvenc_build_available or amf_build_available or qsv_build_available
            else "CPU encoding plus ASR/CV will dominate; keep MVP clips short and cache artifacts."
        ),
    }


def check_onnxruntime_runtime() -> dict[str, Any]:
    onnxruntime = {
        "installed": module_available("onnxruntime"),
        "version": import_version("onnxruntime"),
        "available_providers": [],
        "qnn_plugin_installed": module_available("onnxruntime_qnn"),
        "qnn_plugin_registered": False,
        "qnn_library_path": None,
    }
    if onnxruntime["installed"]:
        try:
            import onnxruntime as ort  # type: ignore

            providers = list(ort.get_available_providers())
            if "QNNExecutionProvider" not in providers and onnxruntime["qnn_plugin_installed"]:
                try:
                    import onnxruntime_qnn as qnn  # type: ignore

                    library_path = qnn.get_library_path()
                    onnxruntime["qnn_library_path"] = library_path
                    if Path(library_path).exists():
                        ort.register_execution_provider_library(qnn.get_ep_name(), library_path)
                        onnxruntime["qnn_plugin_registered"] = True
                        providers = list(ort.get_available_providers())
                    else:
                        onnxruntime["qnn_plugin_error"] = f"QNN provider library not found: {library_path}"
                except Exception as exc:
                    onnxruntime["qnn_plugin_error"] = str(exc)
            else:
                onnxruntime["qnn_plugin_registered"] = "QNNExecutionProvider" in providers
            onnxruntime["available_providers"] = providers
        except Exception as exc:
            onnxruntime["error"] = str(exc)
    return onnxruntime


def check_qualcomm_platform() -> dict[str, Any]:
    cpu_model = get_cpu_model()
    processor_identifier = os.environ.get("PROCESSOR_IDENTIFIER")
    processor_architecture = os.environ.get("PROCESSOR_ARCHITECTURE")
    processor_architew6432 = os.environ.get("PROCESSOR_ARCHITEW6432")
    python_machine = platform.machine()
    is_qualcomm = "qualcomm" in (cpu_model or "").lower() or "qualcomm" in (
        processor_identifier or ""
    ).lower()
    is_arm_cpu = any(token in (cpu_model or "").lower() for token in ["arm", "aarch64"])
    is_arm_python = python_machine.lower() in {"arm64", "aarch64"}
    likely_emulated_python = bool(is_arm_cpu and not is_arm_python)

    onnxruntime = check_onnxruntime_runtime()
    providers = onnxruntime.get("available_providers") or []
    qnn_env = {
        "QNN_SDK_ROOT": os.environ.get("QNN_SDK_ROOT"),
        "QAIRT_ROOT": os.environ.get("QAIRT_ROOT"),
        "QUALCOMM_AI_ENGINE_DIRECT_SDK": os.environ.get("QUALCOMM_AI_ENGINE_DIRECT_SDK"),
    }

    return {
        "is_qualcomm_cpu": is_qualcomm,
        "is_arm_cpu": is_arm_cpu,
        "cpu_model": cpu_model,
        "python_machine": python_machine,
        "processor_architecture": processor_architecture,
        "processor_architew6432": processor_architew6432,
        "processor_identifier": processor_identifier,
        "likely_emulated_python": likely_emulated_python,
        "native_arm64_python_recommended": bool(is_qualcomm and likely_emulated_python),
        "onnxruntime": onnxruntime,
        "qnn_execution_provider_available": "QNNExecutionProvider" in providers,
        "directml_execution_provider_available": "DmlExecutionProvider" in providers,
        "qnn_environment": qnn_env,
        "recommended_local_profile": "qualcomm_windows_arm64" if is_qualcomm else "default",
        "optimization_notes": [
            "Prefer native ARM64 Python and ARM64 wheels when available.",
            "Use CPU int8 faster-whisper for Phase 2; evaluate ONNX Runtime QNN/DirectML for future ONNX-compatible models.",
            "Do not depend on CUDA, NVENC, TensorRT, or vLLM locally on this machine.",
            "Keep NVIDIA profiles for future server deployment, not for this local Qualcomm development path.",
        ]
        if is_qualcomm
        else [],
    }


def check_amd_platform() -> dict[str, Any]:
    cpu_model = get_cpu_model()
    controllers = get_video_controllers()
    haystack = " ".join([cpu_model, os.environ.get("PROCESSOR_IDENTIFIER") or "", *controllers]).lower()
    is_amd_cpu = any(token in (cpu_model or "").lower() for token in ["amd", "ryzen", "threadripper", "epyc"])
    is_amd_gpu = any(
        token in haystack
        for token in ["radeon", "rx ", "vega", "amd radeon", "advanced micro devices"]
    )
    onnxruntime = check_onnxruntime_runtime()
    providers = onnxruntime.get("available_providers") or []
    rocm_candidates = {
        "torch_hip_version": None,
        "hipcc_available": run_command(["hipcc", "--version"], timeout=5).get("available", False),
        "rocminfo_available": run_command(["rocminfo"], timeout=5).get("available", False),
    }
    if module_available("torch"):
        try:
            import torch  # type: ignore

            rocm_candidates["torch_hip_version"] = getattr(torch.version, "hip", None)
        except Exception as exc:
            rocm_candidates["torch_error"] = str(exc)

    return {
        "is_amd_cpu": is_amd_cpu,
        "is_amd_gpu": is_amd_gpu,
        "cpu_model": cpu_model,
        "video_controllers": controllers,
        "onnxruntime": onnxruntime,
        "directml_execution_provider_available": "DmlExecutionProvider" in providers,
        "rocm_execution_provider_available": "ROCMExecutionProvider" in providers,
        "rocm": rocm_candidates,
        "recommended_local_profile": "amd_windows_directml" if is_amd_gpu or is_amd_cpu else "default",
        "optimization_notes": [
            "Prefer FFmpeg AMF encoders when available for render acceleration.",
            "Use DirectML/ONNX Runtime for future ONNX-compatible vision or embedding models on Windows.",
            "ROCm is useful on supported Linux AMD GPU systems; treat Windows ROCm as not generally available.",
            "Keep ASR CPU int8 as a reliable baseline unless a tested GPU backend exists.",
        ]
        if is_amd_cpu or is_amd_gpu
        else [],
    }


def check_intel_platform() -> dict[str, Any]:
    cpu_model = get_cpu_model()
    controllers = get_video_controllers()
    haystack = " ".join([cpu_model, os.environ.get("PROCESSOR_IDENTIFIER") or "", *controllers]).lower()
    is_intel_cpu = any(token in (cpu_model or "").lower() for token in ["intel", "core(tm)", "xeon", "ultra"])
    is_intel_gpu = any(token in haystack for token in ["intel", "iris", "uhd graphics", "arc", "xe graphics"])
    is_intel_npu_hint = any(token in haystack for token in ["npu", "ai boost"])
    onnxruntime = check_onnxruntime_runtime()
    providers = onnxruntime.get("available_providers") or []
    openvino = {
        "installed": module_available("openvino"),
        "version": import_version("openvino"),
        "mo_available": run_command(["mo", "--version"], timeout=5).get("available", False),
        "benchmark_app_available": run_command(["benchmark_app", "--version"], timeout=5).get("available", False),
    }

    return {
        "is_intel_cpu": is_intel_cpu,
        "is_intel_gpu": is_intel_gpu,
        "is_intel_npu_hint": is_intel_npu_hint,
        "cpu_model": cpu_model,
        "video_controllers": controllers,
        "onnxruntime": onnxruntime,
        "openvino": openvino,
        "openvino_execution_provider_available": "OpenVINOExecutionProvider" in providers,
        "directml_execution_provider_available": "DmlExecutionProvider" in providers,
        "recommended_local_profile": "intel_windows_openvino" if is_intel_cpu or is_intel_gpu else "default",
        "optimization_notes": [
            "Prefer FFmpeg QSV encoders when available for Intel Quick Sync render acceleration.",
            "Evaluate OpenVINO for ONNX-compatible vision, detection, and embedding models.",
            "Use DirectML as a Windows fallback for supported GPU inference paths.",
            "Keep CPU int8 ASR as the baseline until a tested OpenVINO/DirectML path is validated.",
        ]
        if is_intel_cpu or is_intel_gpu
        else [],
    }


def check_asr() -> dict[str, Any]:
    fw_installed = module_available("faster_whisper")
    ct2_installed = module_available("ctranslate2")
    ct2_version = import_version("ctranslate2")

    return {
        "faster_whisper_installed": fw_installed,
        "ctranslate2_installed": ct2_installed,
        "ctranslate2_version": ct2_version,
        "planned_backends": ["faster-whisper", "WhisperX", "onnxruntime-qnn-future"],
        "model_recommendations": {
            "high_vram": {"model": "large-v3 or distil-large-v3", "compute_type": "float16"},
            "mid_vram": {"model": "distil-large-v3 or medium", "compute_type": "int8_float16"},
            "low_vram_or_cpu": {"model": "small or medium", "compute_type": "int8"},
        },
    }


def check_ollama(base_url: str, timeout: int = 3) -> dict[str, Any]:
    ollama_binary = run_command(["ollama", "--version"], timeout=5)
    url = base_url.rstrip("/") + "/api/tags"
    api: dict[str, Any] = {"available": False, "url": url, "models": []}
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            api = {
                "available": True,
                "url": url,
                "models": [model.get("name") for model in payload.get("models", [])],
            }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        api["error"] = str(exc)

    return {
        "ollama_binary_available": ollama_binary.get("available", False),
        "ollama_version": parse_first_line(ollama_binary.get("stdout") or ollama_binary.get("stderr")),
        "ollama_api": api,
        "backend_abstraction_needed": True,
        "supported_future_backends": ["ollama", "vllm", "llama.cpp", "openai-compatible", "triton"],
        "structured_output_note": "Prefer instruct models that reliably emit strict JSON.",
    }


def check_vision() -> dict[str, Any]:
    cv2_installed = module_available("cv2")
    mediapipe_installed = module_available("mediapipe")
    cv2_cuda = None

    if cv2_installed:
        try:
            import cv2  # type: ignore

            cv2_cuda = {
                "opencv_version": cv2.__version__,
                "cuda_device_count": cv2.cuda.getCudaEnabledDeviceCount()
                if hasattr(cv2, "cuda")
                else 0,
            }
        except Exception as exc:
            cv2_cuda = {"error": str(exc)}

    return {
        "opencv_installed": cv2_installed,
        "mediapipe_installed": mediapipe_installed,
        "opencv_cuda": cv2_cuda,
        "strategy": {
            "long_video_face_detection": "Sample at 1-10 fps by hardware level, interpolate crop track, smooth with EMA.",
            "avoid": "Do not run full face/object detection on every frame for 30min+ videos.",
            "fallback": "Center crop when no confident subject is detected.",
        },
    }


def check_image_edit() -> dict[str, Any]:
    return {
        "pillow_installed": module_available("PIL"),
        "pillow_version": import_version("PIL"),
        "local_operations": [
            "grayscale",
            "brighten",
            "darken",
            "contrast",
            "sharpen",
            "blur",
            "rotate_90",
            "flip_horizontal",
            "flip_vertical",
            "fit_720p",
            "fit_1080p",
        ],
    }


@dataclass
class LevelDecision:
    level: str
    reason: str
    recommendations: dict[str, Any]


def classify_hardware(report: dict[str, Any]) -> LevelDecision:
    memory_gb = report["compute"]["memory"].get("total_gb") or 0
    gpu = report["gpu_cuda"]
    max_vram = gpu.get("max_vram_gb") or 0
    cuda = bool(gpu.get("cuda_usable"))
    nvenc = bool(report["ffmpeg_codecs"].get("gpu_encoding_available"))
    qualcomm = report.get("qualcomm_platform", {})
    amd = report.get("amd_platform", {})
    intel = report.get("intel_platform", {})

    if qualcomm.get("is_qualcomm_cpu"):
        return LevelDecision(
            "Q",
            "Qualcomm Windows on ARM platform: optimize for native ARM64, CPU int8, ONNX Runtime QNN/DirectML future paths.",
            {
                "whisper_model": "tiny/base/small first; medium only for short clips",
                "compute_type": "int8",
                "llm": "Ollama if stable, otherwise small quantized llama.cpp/OpenAI-compatible fallback",
                "smart_crop_detection_fps": "0.5-2 initially; prefer sparse detection plus interpolation",
                "output": "720x1280 for MVP validation, 1080x1920 only after profiling",
                "encoder": "libx264/libx265 CPU; Qualcomm hardware encode requires a separate Media Foundation path later",
                "local_ai_acceleration": "Evaluate ONNX Runtime QNNExecutionProvider and DirectML for ONNX-compatible models",
                "python": "Use native ARM64 Python when possible; current x64 Python may run under emulation.",
                "long_video": "Chunk everything; 30min+ is possible for pipeline validation but not high throughput yet.",
            },
        )

    if cuda and nvenc and max_vram >= 16 and memory_gb >= 32:
        return LevelDecision(
            "A",
            "High-end local workstation: CUDA, NVENC, >=16GB VRAM, >=32GB RAM.",
            {
                "whisper_model": "large-v3 or distil-large-v3",
                "compute_type": "float16",
                "llm": "7B/8B instruct local model",
                "smart_crop_detection_fps": "5-10",
                "output": "1080x1920 30fps",
                "encoder": "h264_nvenc or hevc_nvenc",
                "long_video": "Chunk processing still recommended.",
            },
        )

    if cuda and max_vram >= 8 and memory_gb >= 16:
        return LevelDecision(
            "B",
            "Mid-range local workstation: CUDA with 8-12GB+ VRAM and 16GB+ RAM.",
            {
                "whisper_model": "distil-large-v3 or medium",
                "compute_type": "float16 or int8_float16",
                "llm": "quantized 7B instruct model",
                "smart_crop_detection_fps": "3-5",
                "output": "720x1280 or 1080x1920",
                "encoder": "Prefer NVENC; fallback to libx264.",
                "long_video": "Segment pipeline; never load all frames/transcript at once.",
            },
        )

    if cuda and max_vram > 0:
        return LevelDecision(
            "C",
            "Entry GPU platform: CUDA exists but VRAM/RAM is limited.",
            {
                "whisper_model": "small, medium, or distil-large-v3 int8",
                "compute_type": "int8 or int8_float16",
                "llm": "small quantized local model or external fallback",
                "smart_crop_detection_fps": "1-3",
                "output": "720x1280",
                "encoder": "NVENC if available; otherwise libx264.",
                "long_video": "Validate with 1-3 minute clips before full videos.",
            },
        )

    if amd.get("is_amd_cpu") or amd.get("is_amd_gpu"):
        return LevelDecision(
            "AM",
            "AMD platform: optimize CPU baseline now, validate AMF render and DirectML/ROCm model paths before relying on acceleration.",
            {
                "whisper_model": "small/medium CPU int8 first; GPU ASR only after backend validation",
                "compute_type": "int8 baseline",
                "llm": "Ollama or llama.cpp quantized model; external fallback for long jobs",
                "smart_crop_detection_fps": "1-3 initially; prefer sparse detection plus interpolation",
                "output": "720x1280 for MVP, 1080x1920 after AMF profiling",
                "encoder": "Prefer AMF if detected and stable; fallback libx264/libx265",
                "local_ai_acceleration": "Evaluate DirectML/ONNX Runtime; ROCm only on supported AMD GPU systems",
                "long_video": "Chunk everything and cache artifacts; GPU encoder availability varies by driver/build.",
            },
        )

    if intel.get("is_intel_cpu") or intel.get("is_intel_gpu"):
        return LevelDecision(
            "I",
            "Intel platform: optimize CPU baseline now, validate Quick Sync render and OpenVINO/DirectML model paths.",
            {
                "whisper_model": "small/medium CPU int8 first; OpenVINO path after validation",
                "compute_type": "int8 baseline",
                "llm": "Ollama or llama.cpp quantized model; external fallback for long jobs",
                "smart_crop_detection_fps": "1-3 initially; OpenVINO candidate for future vision models",
                "output": "720x1280 for MVP, 1080x1920 after QSV profiling",
                "encoder": "Prefer Intel QSV if detected and stable; fallback libx264/libx265",
                "local_ai_acceleration": "Evaluate OpenVINOExecutionProvider and DirectML for ONNX-compatible models",
                "long_video": "Chunk everything and cache artifacts; Quick Sync depends on driver/FFmpeg build.",
            },
        )

    return LevelDecision(
        "D",
        "CPU-only or unstable CUDA platform.",
        {
            "whisper_model": "small or medium CPU",
            "compute_type": "int8",
            "llm": "small CPU quantized model or external API",
            "smart_crop_detection_fps": "0.5-1 with interpolation",
            "output": "720x1280 for tests",
            "encoder": "libx264",
            "long_video": "Suitable for feature validation, not production throughput.",
        },
    )


def classify_nvidia_readiness(report: dict[str, Any]) -> dict[str, Any]:
    docker = run_command(["docker", "--version"], timeout=5)
    compose = run_command(["docker", "compose", "version"], timeout=5)
    gpu = report["gpu_cuda"]
    nvenc = report["ffmpeg_codecs"].get("gpu_encoding_available")
    gpu_count = gpu.get("gpu_count") or 0
    max_vram = gpu.get("max_vram_gb") or 0

    ready_score = 0
    ready_score += 2 if gpu.get("cuda_usable") else 0
    ready_score += 2 if nvenc else 0
    ready_score += 1 if docker.get("available") else 0
    ready_score += 1 if compose.get("available") else 0
    ready_score += 2 if gpu_count >= 2 else 0
    ready_score += 1 if max_vram >= 16 else 0

    if ready_score >= 7:
        tier = "N2"
        meaning = "Strong NVIDIA workstation/server candidate."
    elif ready_score >= 4:
        tier = "N1"
        meaning = "NVIDIA-ready single-node candidate; container/runtime work remains."
    else:
        tier = "N0"
        meaning = "Not NVIDIA-platform-ready yet; keep local MVP backend-agnostic."

    return {
        "tier": tier,
        "meaning": meaning,
        "score": ready_score,
        "docker_available": docker.get("available", False),
        "docker_version": parse_first_line(docker.get("stdout") or docker.get("stderr")),
        "docker_compose_available": compose.get("available", False),
        "docker_compose_version": parse_first_line(compose.get("stdout") or compose.get("stderr")),
        "supports_worker_split": gpu_count >= 1,
        "multi_gpu_candidate": gpu_count >= 2,
        "triton_candidate": bool(max_vram >= 16 and gpu.get("cuda_usable")),
        "tensorrt_candidate": bool(max_vram >= 8 and gpu.get("cuda_usable")),
        "batch_processing_candidate": bool(gpu.get("cuda_usable") and report["compute"]["memory"].get("total_gb", 0) >= 32),
    }


def classify_qualcomm_readiness(report: dict[str, Any]) -> dict[str, Any]:
    qualcomm = report.get("qualcomm_platform", {})
    memory_gb = report["compute"]["memory"].get("total_gb") or 0
    score = 0
    score += 2 if qualcomm.get("is_qualcomm_cpu") else 0
    score += 1 if qualcomm.get("is_arm_cpu") else 0
    score += 1 if not qualcomm.get("likely_emulated_python") else 0
    score += 2 if qualcomm.get("qnn_execution_provider_available") else 0
    score += 1 if qualcomm.get("directml_execution_provider_available") else 0
    score += 1 if memory_gb >= 32 else 0

    if score >= 6:
        tier = "Q2"
        meaning = "Strong Qualcomm local AI candidate with native/runtime acceleration available."
    elif score >= 3:
        tier = "Q1"
        meaning = "Qualcomm-first development candidate; optimize CPU path now and prepare QNN/DirectML."
    elif qualcomm.get("is_qualcomm_cpu"):
        tier = "Q0"
        meaning = "Qualcomm hardware detected, but native/runtime acceleration is not ready yet."
    else:
        tier = "N/A"
        meaning = "Not a Qualcomm platform."

    blockers = []
    if qualcomm.get("likely_emulated_python"):
        blockers.append("Native ARM64 Python is recommended.")
    if qualcomm.get("is_qualcomm_cpu") and not qualcomm.get("qnn_execution_provider_available"):
        blockers.append("ONNX Runtime QNN provider is not currently available.")
    if qualcomm.get("is_qualcomm_cpu") and not qualcomm.get("directml_execution_provider_available"):
        blockers.append("DirectML provider is not currently available.")

    return {
        "tier": tier,
        "meaning": meaning,
        "score": score,
        "recommended_config": qualcomm.get("recommended_local_profile"),
        "blockers": blockers,
    }


def classify_amd_readiness(report: dict[str, Any]) -> dict[str, Any]:
    amd = report.get("amd_platform", {})
    ffmpeg = report.get("ffmpeg_codecs", {})
    memory_gb = report["compute"]["memory"].get("total_gb") or 0
    score = 0
    score += 1 if amd.get("is_amd_cpu") else 0
    score += 2 if amd.get("is_amd_gpu") else 0
    score += 2 if ffmpeg.get("platform_encoders", {}).get("amd_amf") else 0
    score += 2 if amd.get("directml_execution_provider_available") else 0
    score += 2 if amd.get("rocm_execution_provider_available") or amd.get("rocm", {}).get("torch_hip_version") else 0
    score += 1 if memory_gb >= 32 else 0

    detected = amd.get("is_amd_cpu") or amd.get("is_amd_gpu")
    if not detected:
        tier = "N/A"
        meaning = "Not an AMD platform."
    elif score >= 6:
        tier = "AM2"
        meaning = "Strong AMD local candidate; AMF/DirectML or ROCm acceleration paths are visible."
    elif score >= 3:
        tier = "AM1"
        meaning = "AMD development candidate; keep CPU baseline and validate AMF/DirectML before relying on it."
    elif detected:
        tier = "AM0"
        meaning = "AMD hardware detected, but acceleration/runtime support is not ready yet."

    blockers = []
    if detected and not ffmpeg.get("platform_encoders", {}).get("amd_amf"):
        blockers.append("FFmpeg AMF encoder was not detected; render may use CPU libx264/libx265.")
    if detected and not amd.get("directml_execution_provider_available"):
        blockers.append("ONNX Runtime DirectML provider is not available.")
    if amd.get("is_amd_gpu") and not (
        amd.get("rocm_execution_provider_available") or amd.get("rocm", {}).get("torch_hip_version")
    ):
        blockers.append("ROCm/HIP path is not available; this is expected on many Windows AMD systems.")

    return {
        "tier": tier,
        "meaning": meaning,
        "score": score,
        "recommended_config": amd.get("recommended_local_profile"),
        "blockers": blockers,
    }


def classify_intel_readiness(report: dict[str, Any]) -> dict[str, Any]:
    intel = report.get("intel_platform", {})
    ffmpeg = report.get("ffmpeg_codecs", {})
    memory_gb = report["compute"]["memory"].get("total_gb") or 0
    score = 0
    score += 1 if intel.get("is_intel_cpu") else 0
    score += 1 if intel.get("is_intel_gpu") else 0
    score += 2 if ffmpeg.get("platform_encoders", {}).get("intel_qsv") else 0
    score += 2 if intel.get("openvino_execution_provider_available") or intel.get("openvino", {}).get("installed") else 0
    score += 1 if intel.get("directml_execution_provider_available") else 0
    score += 1 if intel.get("is_intel_npu_hint") else 0
    score += 1 if memory_gb >= 32 else 0

    detected = intel.get("is_intel_cpu") or intel.get("is_intel_gpu")
    if not detected:
        tier = "N/A"
        meaning = "Not an Intel platform."
    elif score >= 6:
        tier = "I2"
        meaning = "Strong Intel local candidate; QSV/OpenVINO or DirectML paths are visible."
    elif score >= 3:
        tier = "I1"
        meaning = "Intel development candidate; validate QSV/OpenVINO before relying on acceleration."
    elif detected:
        tier = "I0"
        meaning = "Intel hardware detected, but acceleration/runtime support is not ready yet."

    blockers = []
    if detected and not ffmpeg.get("platform_encoders", {}).get("intel_qsv"):
        blockers.append("FFmpeg Intel QSV encoder was not detected; render may use CPU libx264/libx265.")
    if detected and not intel.get("openvino_execution_provider_available") and not intel.get("openvino", {}).get("installed"):
        blockers.append("OpenVINO runtime/provider is not available.")
    if detected and not intel.get("directml_execution_provider_available"):
        blockers.append("ONNX Runtime DirectML provider is not available.")

    return {
        "tier": tier,
        "meaning": meaning,
        "score": score,
        "recommended_config": intel.get("recommended_local_profile"),
        "blockers": blockers,
    }


def update_ffmpeg_hardware_flags(report: dict[str, Any]) -> None:
    ffmpeg = report["ffmpeg_codecs"]
    encoders = ffmpeg.setdefault("platform_encoders", {})
    nvidia_available = bool(report["gpu_cuda"].get("nvidia_smi_available"))
    amd_available = bool(report.get("amd_platform", {}).get("is_amd_gpu"))
    intel_available = bool(report.get("intel_platform", {}).get("is_intel_gpu") or report.get("intel_platform", {}).get("is_intel_cpu"))

    encoders["nvidia_nvenc_usable"] = bool(encoders.get("nvidia_nvenc") and nvidia_available)
    encoders["amd_amf_usable"] = bool(encoders.get("amd_amf") and amd_available)
    encoders["intel_qsv_usable"] = bool(encoders.get("intel_qsv") and intel_available)
    ffmpeg["gpu_encoding_available"] = bool(
        encoders["nvidia_nvenc_usable"] or encoders["amd_amf_usable"] or encoders["intel_qsv_usable"]
    )
    if not encoders["nvidia_nvenc_usable"] and ffmpeg.get("nvenc_build_available"):
        ffmpeg["nvidia_hardware_note"] = "NVENC encoders are present in this FFmpeg build, but no NVIDIA GPU was detected."
    if not ffmpeg["gpu_encoding_available"]:
        ffmpeg["recommendation"] = "Use libx264/libx265 locally; no usable platform GPU encoder was confirmed."
        ffmpeg["long_video_bottleneck"] = "CPU encoding plus ASR/CV will dominate locally; keep MVP clips short and cache artifacts."


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "tool": "dump2done.check_env",
        "generated_at_unix": started,
        "basic": check_basic_tools(),
        "compute": check_compute(),
    }
    report["gpu_cuda"] = check_gpu_cuda()
    report["ffmpeg_codecs"] = check_ffmpeg_codecs()
    report["asr"] = check_asr()
    report["llm"] = check_ollama(args.ollama_url)
    report["vision"] = check_vision()
    report["image_edit"] = check_image_edit()
    report["qualcomm_platform"] = check_qualcomm_platform()
    report["amd_platform"] = check_amd_platform()
    report["intel_platform"] = check_intel_platform()
    update_ffmpeg_hardware_flags(report)

    decision = classify_hardware(report)
    report["hardware_level"] = {
        "level": decision.level,
        "reason": decision.reason,
        "recommendations": decision.recommendations,
    }
    report["nvidia_readiness"] = classify_nvidia_readiness(report)
    report["qualcomm_readiness"] = classify_qualcomm_readiness(report)
    report["amd_readiness"] = classify_amd_readiness(report)
    report["intel_readiness"] = classify_intel_readiness(report)
    report["elapsed_seconds"] = round(time.time() - started, 2)
    return report


def print_human_summary(report: dict[str, Any]) -> None:
    level = report["hardware_level"]
    nvidia = report["nvidia_readiness"]
    basic = report["basic"]
    gpu = report["gpu_cuda"]
    ffmpeg = report["ffmpeg_codecs"]
    qualcomm = report["qualcomm_platform"]
    qualcomm_ready = report["qualcomm_readiness"]
    amd = report["amd_platform"]
    intel = report["intel_platform"]
    amd_ready = report["amd_readiness"]
    intel_ready = report["intel_readiness"]

    print("Dump2Done Environment Probe")
    print("===========================")
    print(f"OS: {basic['os']['system']} {basic['os']['release']} ({basic['os']['machine']})")
    print(f"Python: {basic['python']['version']} | venv: {basic['python']['is_venv']}")
    print(f"FFmpeg: {basic['ffmpeg']['available']} | FFprobe: {basic['ffprobe']['available']}")
    print(f"Git: {basic['git']['available']}")
    print(f"NVIDIA GPUs: {gpu['gpu_count']} | CUDA usable: {gpu['cuda_usable']} | max VRAM GB: {gpu['max_vram_gb']}")
    print(f"NVENC available: {ffmpeg['gpu_encoding_available']}")
    print(
        "Qualcomm: "
        f"{qualcomm['is_qualcomm_cpu']} | emulated Python likely: {qualcomm['likely_emulated_python']}"
    )
    print(f"AMD: CPU {amd['is_amd_cpu']} | GPU {amd['is_amd_gpu']}")
    print(f"Intel: CPU {intel['is_intel_cpu']} | GPU {intel['is_intel_gpu']} | NPU hint {intel['is_intel_npu_hint']}")
    print(f"Hardware Level: {level['level']} - {level['reason']}")
    print(f"NVIDIA Readiness: {nvidia['tier']} - {nvidia['meaning']}")
    print(f"Qualcomm Readiness: {qualcomm_ready['tier']} - {qualcomm_ready['meaning']}")
    print(f"AMD Readiness: {amd_ready['tier']} - {amd_ready['meaning']}")
    print(f"Intel Readiness: {intel_ready['tier']} - {intel_ready['meaning']}")
    print("")
    print("Recommendations:")
    for key, value in level["recommendations"].items():
        print(f"- {key}: {value}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check local Dump2Done development environment.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report to stdout.")
    parser.add_argument("--output", type=Path, help="Write full JSON report to a file.")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama base URL.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_human_summary(report)
        if args.output:
            print(f"\nFull report written to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
