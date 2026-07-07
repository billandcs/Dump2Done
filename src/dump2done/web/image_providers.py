from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


DEFAULT_COMFYUI_WORKFLOW_PATH = Path("configs/comfyui_image_to_image_workflow.example.json")
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-1.5"
NEGATIVE_IMAGE_PROMPT = "low quality, blurry, distorted, deformed, extra limbs, text, watermark"


@dataclass(frozen=True)
class ImageEditRequest:
    input_path: Path
    renders_dir: Path
    prompt: str
    resolution: str
    provider: str
    automatic1111_endpoint: str
    comfyui_endpoint: str
    comfyui_workflow_path: Path
    openai_image_model: str
    online_fallback_policy: str


@dataclass(frozen=True)
class ProviderFailure:
    provider: str
    title: str
    reason: str
    action: str

    def render(self) -> str:
        return f"{self.provider}: {self.title} - {self.reason} 建議：{self.action}"


class ImageProviderError(RuntimeError):
    def __init__(self, failures: list[ProviderFailure]) -> None:
        self.failures = failures
        message = build_generative_image_error(failures)
        super().__init__(message)


def edit_image_with_provider(request: ImageEditRequest) -> dict:
    registry = ImageProviderRegistry(request)
    return registry.edit()


def prompt_needs_generative_image_model(prompt: str) -> bool:
    text = prompt.lower()
    if "貓" in text and "狗" in text:
        return True
    generative_tokens = [
        "貓變狗",
        "貓變成狗",
        "把貓變",
        "變成狗",
        "變成貓",
        "替換",
        "換成",
        "改成",
        "生成",
        "重新生成",
        "修掉",
        "移除",
        "inpaint",
        "outpaint",
        "replace",
        "turn into",
        "make it a",
        "cat to dog",
        "dog",
    ]
    return any(token in text for token in generative_tokens)


class ImageProviderRegistry:
    def __init__(self, request: ImageEditRequest) -> None:
        self.request = request

    def edit(self) -> dict:
        request = self.request
        needs_generative = prompt_needs_generative_image_model(request.prompt)
        failures: list[ProviderFailure] = []

        if request.provider == "pillow":
            if needs_generative:
                raise ImageProviderError(
                    [
                        ProviderFailure(
                            "Pillow",
                            "不能處理生成式圖片",
                            "Pillow 只能做旋轉、亮度、黑白、銳化等確定性濾鏡，不能把貓變成狗。",
                            "改選 Auto、本地 ComfyUI、Automatic1111，或明確選 OpenAI Images API。",
                        )
                    ]
                )
            result = edit_image_locally(
                request.input_path,
                request.renders_dir,
                request.prompt,
                request.resolution,
            )
            result["provider"] = "pillow"
            result["provider_route"] = "local_filter"
            return result

        if request.provider == "auto" and not needs_generative:
            result = edit_image_locally(
                request.input_path,
                request.renders_dir,
                request.prompt,
                request.resolution,
            )
            result["provider"] = "pillow"
            result["provider_route"] = "local_filter"
            return result

        if request.provider in {"auto", "local_a1111"}:
            try:
                return edit_image_with_automatic1111(
                    request.input_path,
                    request.renders_dir,
                    request.prompt,
                    request.automatic1111_endpoint,
                )
            except Exception as exc:
                failures.append(
                    ProviderFailure(
                        "Automatic1111",
                        "本地 Stable Diffusion API 不可用",
                        str(exc),
                        "啟動 Stable Diffusion WebUI 並加上 --api，確認 endpoint 是 http://127.0.0.1:7860。",
                    )
                )
                if request.provider == "local_a1111":
                    raise ImageProviderError(failures) from exc

        if request.provider in {"auto", "local_comfyui"}:
            try:
                return edit_image_with_comfyui(
                    request.input_path,
                    request.renders_dir,
                    request.prompt,
                    request.comfyui_endpoint,
                    request.comfyui_workflow_path,
                )
            except Exception as exc:
                failures.append(
                    ProviderFailure(
                        "ComfyUI",
                        "本地 ComfyUI workflow 不可用",
                        str(exc),
                        "啟動 ComfyUI，安裝 checkpoint，並確認 workflow JSON 可由 API 執行。",
                    )
                )
                if request.provider == "local_comfyui":
                    raise ImageProviderError(failures) from exc

        if request.provider == "openai":
            if request.online_fallback_policy == "disabled":
                failures.append(
                    ProviderFailure(
                        "OpenAI Images",
                        "線上 fallback 已關閉",
                        "目前設定不允許把圖片送到雲端。",
                        "到設定開啟線上 fallback，或改用本地 ComfyUI / Automatic1111。",
                    )
                )
                raise ImageProviderError(failures)
            try:
                return edit_image_with_openai(
                    request.input_path,
                    request.renders_dir,
                    request.prompt,
                    request.openai_image_model,
                )
            except Exception as exc:
                failures.append(
                    ProviderFailure(
                        "OpenAI Images",
                        "OpenAI Images API 不可用",
                        str(exc),
                        "設定 OPENAI_API_KEY，確認帳號有 API 額度，或改用本地 provider。",
                    )
                )
                raise ImageProviderError(failures) from exc

        if request.provider == "auto":
            failures.append(
                ProviderFailure(
                    "Cloud fallback",
                    "Auto 不會暗中送雲端",
                    "本地 provider 都不可用時，Dump2Done 不會自動把圖片送到 OpenAI。",
                    "若你要使用雲端，請在圖片路線明確選 OpenAI Images API。",
                )
            )

        raise ImageProviderError(failures)


def comfyui_readiness_message(endpoint: str, workflow_path: Path | str | None = None) -> str:
    try:
        info = http_json("GET", f"{endpoint.rstrip('/')}/object_info", timeout=5)
    except Exception as exc:
        return f"ComfyUI unavailable: {exc}"
    checkpoints = comfyui_checkpoints(info)
    if not checkpoints:
        return "ComfyUI is running, but no checkpoint model is installed or visible to CheckpointLoaderSimple."
    path = Path(workflow_path or DEFAULT_COMFYUI_WORKFLOW_PATH)
    if not path.exists():
        return f"ComfyUI is running, but workflow JSON does not exist: {path}"
    try:
        workflow = read_comfyui_workflow(path)
        validate_comfyui_workflow(workflow)
    except Exception as exc:
        return f"ComfyUI workflow JSON is not ready: {exc}"
    return f"ComfyUI is ready with {len(checkpoints)} checkpoint(s) and workflow {path}."


def edit_image_with_automatic1111(input_path: Path, renders_dir: Path, prompt: str, endpoint: str) -> dict:
    created_at = now_utc()
    http_json("GET", f"{endpoint.rstrip('/')}/sdapi/v1/options", timeout=3)
    image_b64 = base64.b64encode(input_path.read_bytes()).decode("ascii")
    width, height = image_size_for_generation(input_path)
    payload = {
        "init_images": [image_b64],
        "prompt": normalize_image_prompt(prompt),
        "negative_prompt": NEGATIVE_IMAGE_PROMPT,
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
    return image_result(
        output_path,
        created_at,
        prompt,
        "automatic1111",
        ["stable_diffusion_img2img"],
        "已透過本地 Automatic1111 / Stable Diffusion 完成生成式圖片編輯。",
    )


def edit_image_with_comfyui(
    input_path: Path,
    renders_dir: Path,
    prompt: str,
    endpoint: str,
    workflow_path: Path | str,
) -> dict:
    created_at = now_utc()
    endpoint = endpoint.rstrip("/")
    object_info = http_json("GET", f"{endpoint}/object_info", timeout=5)
    checkpoints = comfyui_checkpoints(object_info)
    if not checkpoints:
        raise RuntimeError("ComfyUI 有回應，但沒有可用 checkpoint。請安裝模型，並確認 CheckpointLoaderSimple 看得到。")

    workflow = read_comfyui_workflow(Path(workflow_path))
    validate_comfyui_workflow(workflow)
    uploaded = comfyui_upload_image(endpoint, input_path)
    uploaded_name = str(uploaded.get("name") or input_path.name)
    workflow = prepare_comfyui_workflow(
        workflow,
        input_image=uploaded_name,
        prompt=normalize_image_prompt(prompt),
        checkpoint=checkpoints[0],
        job_id=input_path.stem,
    )
    prompt_id = comfyui_queue_prompt(endpoint, workflow)
    history = comfyui_wait_for_history(endpoint, prompt_id, timeout=420)
    image_ref = first_comfyui_output_image(history, prompt_id)
    if not image_ref:
        raise RuntimeError("ComfyUI workflow completed, but no image output was found in history.")

    output_path = renders_dir / f"edited_{input_path.stem}.png"
    comfyui_download_image(endpoint, image_ref, output_path)
    return image_result(
        output_path,
        created_at,
        prompt,
        "comfyui",
        ["comfyui_img2img_workflow"],
        "已透過本地 ComfyUI image-to-image workflow 完成生成式圖片編輯。",
        extra={
            "comfyui_prompt_id": prompt_id,
            "comfyui_workflow_path": str(workflow_path),
            "comfyui_checkpoint": checkpoints[0],
        },
    )


def edit_image_with_openai(input_path: Path, renders_dir: Path, prompt: str, model: str) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    created_at = now_utc()
    boundary = f"----Dump2Done{uuid.uuid4().hex}"
    output_path = renders_dir / f"edited_{input_path.stem}.png"
    fields = {
        "model": model or DEFAULT_OPENAI_IMAGE_MODEL,
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
    return image_result(
        output_path,
        created_at,
        prompt,
        "openai_images",
        ["openai_image_edit"],
        "已透過 OpenAI Images API 完成生成式圖片編輯。",
        extra={"model": fields["model"]},
    )


def edit_image_locally(input_path: Path, renders_dir: Path, prompt: str, resolution: str) -> dict:
    created_at = now_utc()
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except Exception as exc:
        output_path = renders_dir / f"edited_{input_path.stem}{input_path.suffix}"
        output_path.write_bytes(input_path.read_bytes())
        return image_result(
            output_path,
            created_at,
            prompt,
            "pillow",
            ["copy_original"],
            f"Pillow is unavailable; copied original image. ({exc})",
        )

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
            for token in ["往左", "向左", "左旋", "左轉", "逆時針", "rotate left", "turn left", "counterclockwise", "anti-clockwise", "ccw"]
        )
        wants_right_rotation = any(
            token in prompt_lower
            for token in ["往右", "向右", "右旋", "右轉", "順時針", "rotate right", "turn right", "clockwise", "cw"]
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

    message = "已完成本地圖片編輯。" if operations != ["copy_original_preview"] else "已保存圖片與 prompt；未匹配到本地 deterministic 編輯指令。"
    return image_result(output_path, created_at, prompt, "pillow", operations, message)


def read_comfyui_workflow(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"workflow JSON 不存在：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "prompt" in payload and isinstance(payload["prompt"], dict):
        payload = payload["prompt"]
    if not isinstance(payload, dict):
        raise ValueError("workflow JSON 必須是 ComfyUI API prompt 物件。")
    return payload


def validate_comfyui_workflow(workflow: dict) -> None:
    classes = {str(node.get("class_type") or "") for node in workflow.values() if isinstance(node, dict)}
    required = {"LoadImage", "SaveImage"}
    missing = sorted(required - classes)
    if missing:
        raise ValueError(f"workflow 缺少必要節點：{', '.join(missing)}")


def prepare_comfyui_workflow(
    workflow: dict,
    *,
    input_image: str,
    prompt: str,
    checkpoint: str,
    job_id: str,
) -> dict:
    prepared = replace_placeholders(
        copy.deepcopy(workflow),
        {
            "{{input_image}}": input_image,
            "{{prompt}}": prompt,
            "{{negative_prompt}}": NEGATIVE_IMAGE_PROMPT,
            "{{checkpoint}}": checkpoint,
            "{{seed}}": str(int(time.time() * 1000) % 2_147_483_647),
            "{{job_id}}": job_id,
        },
    )
    for node in prepared.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.setdefault("inputs", {})
        if class_type == "LoadImage":
            inputs["image"] = input_image
        elif class_type == "CheckpointLoaderSimple" and inputs.get("ckpt_name") in {"", "{{checkpoint}}"}:
            inputs["ckpt_name"] = checkpoint
        elif class_type == "KSampler":
            if not inputs.get("seed"):
                inputs["seed"] = int(time.time() * 1000) % 2_147_483_647
        elif class_type == "SaveImage":
            prefix = str(inputs.get("filename_prefix") or "Dump2Done")
            if "{{job_id}}" in prefix:
                inputs["filename_prefix"] = prefix.replace("{{job_id}}", job_id)
            elif prefix == "Dump2Done":
                inputs["filename_prefix"] = f"Dump2Done_{job_id}"
    return prepared


def replace_placeholders(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        if value == replacements.get("{{seed}}"):
            return int(value)
        return value
    if isinstance(value, list):
        return [replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_placeholders(item, replacements) for key, item in value.items()}
    return value


def comfyui_checkpoints(object_info: dict) -> list[str]:
    loader = object_info.get("CheckpointLoaderSimple", {})
    choices = loader.get("input", {}).get("required", {}).get("ckpt_name", [])
    if choices and isinstance(choices[0], list):
        return [str(item) for item in choices[0] if item]
    return []


def comfyui_upload_image(endpoint: str, input_path: Path) -> dict:
    boundary = f"----Dump2DoneComfy{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(input_path.name)[0] or "image/png"
    body = build_multipart_body(
        boundary,
        {"overwrite": "true", "type": "input"},
        "image",
        input_path,
        mime_type,
    )
    request = urllib_request.Request(
        f"{endpoint.rstrip('/')}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return open_json_request(request, timeout=120)


def comfyui_queue_prompt(endpoint: str, workflow: dict) -> str:
    response = http_json(
        "POST",
        f"{endpoint.rstrip('/')}/prompt",
        {"prompt": workflow, "client_id": f"dump2done-{uuid.uuid4().hex}"},
        timeout=30,
    )
    node_errors = response.get("node_errors")
    if node_errors:
        raise RuntimeError(f"ComfyUI workflow validation failed: {json.dumps(node_errors, ensure_ascii=False)[:700]}")
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {response}")
    return str(prompt_id)


def comfyui_wait_for_history(endpoint: str, prompt_id: str, timeout: int = 420) -> dict:
    deadline = time.time() + timeout
    last_payload: dict = {}
    while time.time() < deadline:
        payload = http_json("GET", f"{endpoint.rstrip('/')}/history/{urllib_parse.quote(prompt_id)}", timeout=10)
        if payload:
            last_payload = payload
            entry = payload.get(prompt_id) if isinstance(payload, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI workflow failed: {json.dumps(status, ensure_ascii=False)[:700]}")
            if isinstance(entry, dict) and entry.get("outputs"):
                return payload
        time.sleep(1.5)
    raise TimeoutError(f"ComfyUI workflow timed out after {timeout}s. Last response: {last_payload}")


def first_comfyui_output_image(history: dict, prompt_id: str) -> dict | None:
    entry = history.get(prompt_id) if isinstance(history, dict) else None
    outputs = entry.get("outputs", {}) if isinstance(entry, dict) else {}
    for node_output in outputs.values():
        images = node_output.get("images") if isinstance(node_output, dict) else None
        if not images:
            continue
        for image in images:
            if isinstance(image, dict) and image.get("filename"):
                return image
    return None


def comfyui_download_image(endpoint: str, image_ref: dict, output_path: Path) -> None:
    query = urllib_parse.urlencode(
        {
            "filename": image_ref.get("filename", ""),
            "subfolder": image_ref.get("subfolder", ""),
            "type": image_ref.get("type", "output"),
        }
    )
    request = urllib_request.Request(f"{endpoint.rstrip('/')}/view?{query}", method="GET")
    try:
        with urllib_request.urlopen(request, timeout=120) as response:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(response.read())
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"ComfyUI /view HTTP {exc.code}: {detail[:320]}") from exc


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


def image_result(
    output_path: Path,
    created_at: str,
    prompt: str,
    provider: str,
    operations: list[str],
    message: str,
    *,
    extra: dict | None = None,
) -> dict:
    payload = {
        "schema_version": "1.0",
        "stage": "image_edit",
        "status": "completed",
        "created_at": created_at,
        "prompt": prompt,
        "provider": provider,
        "operations": operations,
        "message": message,
        "relative_output": str(output_path.parent.name + "/" + output_path.name).replace("\\", "/"),
    }
    if extra:
        payload.update(extra)
    return payload


def build_generative_image_error(failures: list[ProviderFailure]) -> str:
    detail = "；".join(failure.render() for failure in failures) if failures else "No provider was attempted."
    return (
        "這個提示需要生成式圖片模型，Pillow 無法完成貓變狗或物件替換。"
        f" Provider 診斷：{detail}"
    )


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
    return open_json_request(request, timeout=timeout)


def open_json_request(request: urllib_request.Request, timeout: int) -> dict:
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(str(exc.reason if hasattr(exc, "reason") else exc)) from exc
    return json.loads(body or "{}")


def build_multipart_body(
    boundary: str,
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
    mime_type: str,
) -> bytes:
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
        ]
    )
    chunks.extend(
        [
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
