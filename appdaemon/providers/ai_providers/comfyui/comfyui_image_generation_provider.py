"""ComfyUI image generation provider using workflow JSON + prompt/history APIs."""

from __future__ import annotations

import copy
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..image_generation_provider import (
    ExternalImageGenError,
    ImageGenerationProvider,
    ImageProviderCapabilities,
    ImageProviderName,
)
from ..provider_settings import validate_image_model


@dataclass(frozen=True)
class ComfyUIImageGenerationConfig:
    base_url: str
    workflow_path: str
    model: str = "qwen-image-edit-2509"
    timeout_s: float = 300.0
    prompt_node_id: str = "115:111"
    negative_prompt_node_id: Optional[str] = "115:110"
    load_image_node_id: str = "78"
    save_image_node_id: str = "60"
    sampler_node_id: Optional[str] = "115:3"
    sampler_seed_input: str = "seed"
    filename_prefix: str = "detection-summary"
    upload_overwrite: bool = True
    poll_interval_s: float = 1.0
    provider_options: Dict[str, Any] = field(default_factory=dict)


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _build_multipart(
    *,
    image_path: Path,
    overwrite: bool,
    upload_type: str = "input",
) -> tuple[bytes, str]:
    boundary = f"----codex-comfyui-{uuid.uuid4().hex}"
    lines: list[bytes] = []

    def _field(name: str, value: str) -> None:
        lines.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                f"{value}\r\n".encode("utf-8"),
            ]
        )

    _field("type", upload_type)
    _field("overwrite", "true" if overwrite else "false")

    mime = _guess_mime(image_path)
    lines.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8"),
            image_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(lines), boundary


class ComfyUIImageGenerationProvider(ImageGenerationProvider):
    name = ImageProviderName.COMFYUI
    capabilities = ImageProviderCapabilities(
        supports_text_to_image=False,
        supports_image_to_image=True,
        supports_inpaint=False,
        notes="Uses ComfyUI workflow API for image-to-image generation.",
    )

    def __init__(self, config: ComfyUIImageGenerationConfig):
        ok, err = validate_image_model("comfyui", config.model)
        if not ok and err:
            raise ValueError(err)
        self._config = config
        workflow_path = Path(config.workflow_path)
        if not workflow_path.is_absolute():
            workflow_path = Path(__file__).resolve().parent / workflow_path
        if not workflow_path.exists():
            raise ValueError(f"ComfyUI workflow file does not exist: {workflow_path}")
        self._workflow_path = workflow_path

    def edit_image(
        self,
        *,
        input_image_paths: Sequence[str],
        prompt: str,
        output_image_path: str,
    ) -> Dict[str, Any]:
        in_paths = [
            Path(p)
            for p in (list(input_image_paths) if input_image_paths else [])
            if str(p).strip()
        ]
        out_path = Path(output_image_path)

        if not in_paths:
            raise ExternalImageGenError("input_image_paths is required")
        missing = [p for p in in_paths if not p.exists()]
        if missing:
            raise ExternalImageGenError(f"input image(s) do not exist: {[str(p) for p in missing]}")
        if not str(prompt or "").strip():
            raise ExternalImageGenError("prompt is required")

        primary_input = in_paths[0]
        uploaded_name = self._upload_image(primary_input)
        workflow = self._build_workflow(prompt=str(prompt), uploaded_name=uploaded_name, output_path=out_path)

        started = time.time()
        prompt_id = self._queue_prompt(workflow)
        history_entry = self._wait_for_history(prompt_id)
        image_info = self._extract_output_image_info(history_entry)
        img_bytes = self._download_output_image(image_info)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img_bytes)

        meta = {
            "backend": "external",
            "provider": "comfyui",
            "endpoint": self._config.base_url.rstrip("/"),
            "model": self._config.model,
            "created_at_epoch": time.time(),
            "elapsed_s": round(time.time() - started, 3),
            "input_paths": [str(p) for p in in_paths],
            "output_path": str(out_path),
            "prompt_id": prompt_id,
            "uploaded_input_name": uploaded_name,
            "request": {"prompt_len": len(str(prompt)), "prompt_preview": str(prompt)[:400]},
            "response": {"image_info": image_info},
        }
        if len(in_paths) > 1:
            meta["ignored_input_paths"] = [str(p) for p in in_paths[1:]]
        return meta

    def _build_workflow(self, *, prompt: str, uploaded_name: str, output_path: Path) -> Dict[str, Any]:
        try:
            template = json.loads(self._workflow_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ExternalImageGenError(f"failed to load ComfyUI workflow: {self._workflow_path}") from e

        workflow = copy.deepcopy(template)

        try:
            workflow[self._config.load_image_node_id]["inputs"]["image"] = uploaded_name
            workflow[self._config.prompt_node_id]["inputs"]["prompt"] = prompt
            if self._config.negative_prompt_node_id:
                workflow[self._config.negative_prompt_node_id]["inputs"]["prompt"] = ""
            workflow[self._config.save_image_node_id]["inputs"]["filename_prefix"] = output_path.stem or self._config.filename_prefix
            if self._config.sampler_node_id:
                workflow[self._config.sampler_node_id]["inputs"][self._config.sampler_seed_input] = int(time.time() * 1000)
        except KeyError as e:
            raise ExternalImageGenError(f"workflow is missing expected ComfyUI node/input: {e!r}") from e
        return workflow

    def _upload_image(self, image_path: Path) -> str:
        body, boundary = _build_multipart(
            image_path=image_path,
            overwrite=bool(self._config.upload_overwrite),
        )
        req = urllib.request.Request(
            url=f"{self._config.base_url.rstrip('/')}/upload/image",
            method="POST",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        payload = self._read_json(req, timeout_s=self._config.timeout_s)
        name = str(payload.get("name") or image_path.name).strip()
        if not name:
            raise ExternalImageGenError(f"ComfyUI upload response missing image name: {payload!r}")
        return name

    def _queue_prompt(self, workflow: Dict[str, Any]) -> str:
        req = urllib.request.Request(
            url=f"{self._config.base_url.rstrip('/')}/prompt",
            method="POST",
            data=_safe_json({"prompt": workflow}),
            headers={"Content-Type": "application/json"},
        )
        payload = self._read_json(req, timeout_s=self._config.timeout_s)
        prompt_id = str(payload.get("prompt_id") or "").strip()
        if not prompt_id:
            raise ExternalImageGenError(f"ComfyUI prompt response missing prompt_id: {payload!r}")
        return prompt_id

    def _wait_for_history(self, prompt_id: str) -> Dict[str, Any]:
        deadline = time.time() + float(self._config.timeout_s)
        last_payload: Any = None
        while time.time() < deadline:
            req = urllib.request.Request(
                url=f"{self._config.base_url.rstrip('/')}/history/{urllib.parse.quote(prompt_id, safe='')}",
                method="GET",
            )
            try:
                payload = self._read_json(req, timeout_s=min(30.0, float(self._config.timeout_s)))
            except ExternalImageGenError:
                time.sleep(float(self._config.poll_interval_s))
                continue

            last_payload = payload
            entry = self._extract_history_entry(prompt_id, payload)
            if entry is not None:
                status = entry.get("status") or {}
                messages = status.get("messages") or []
                for msg in messages:
                    if isinstance(msg, (list, tuple)) and msg and msg[0] == "execution_error":
                        raise ExternalImageGenError(f"ComfyUI execution error: {msg!r}")
                if entry.get("outputs"):
                    return entry
            time.sleep(float(self._config.poll_interval_s))

        raise ExternalImageGenError(
            f"Timed out waiting for ComfyUI prompt_id={prompt_id}; last_payload={str(last_payload)[:400]!r}"
        )

    @staticmethod
    def _extract_history_entry(prompt_id: str, payload: Any) -> Optional[Dict[str, Any]]:
        if isinstance(payload, dict):
            if "outputs" in payload:
                return payload
            prompt_entry = payload.get(prompt_id)
            if isinstance(prompt_entry, dict):
                return prompt_entry
            if len(payload) == 1:
                only = next(iter(payload.values()))
                if isinstance(only, dict) and "outputs" in only:
                    return only
        return None

    def _extract_output_image_info(self, history_entry: Dict[str, Any]) -> Dict[str, Any]:
        outputs = history_entry.get("outputs") or {}
        save_output = outputs.get(self._config.save_image_node_id) or outputs.get(str(self._config.save_image_node_id))
        if not isinstance(save_output, dict):
            raise ExternalImageGenError(
                f"ComfyUI history missing save-image node {self._config.save_image_node_id!r}: {outputs!r}"
            )
        images = save_output.get("images") or []
        if not images or not isinstance(images[0], dict):
            raise ExternalImageGenError(f"ComfyUI save-image node missing images: {save_output!r}")
        return images[0]

    def _download_output_image(self, image_info: Dict[str, Any]) -> bytes:
        query = urllib.parse.urlencode(
            {
                "filename": str(image_info.get("filename") or ""),
                "subfolder": str(image_info.get("subfolder") or ""),
                "type": str(image_info.get("type") or "output"),
            }
        )
        req = urllib.request.Request(
            url=f"{self._config.base_url.rstrip('/')}/view?{query}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=float(self._config.timeout_s)) as resp:
                return resp.read()
        except Exception as e:
            raise ExternalImageGenError(f"failed to download ComfyUI output image: {e!r}") from e

    def _read_json(self, req: urllib.request.Request, *, timeout_s: float) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ExternalImageGenError(f"comfyui http error: {e.code} {e.reason}; {detail}") from e
        except Exception as e:
            raise ExternalImageGenError(f"comfyui request failed: {e!r}") from e
