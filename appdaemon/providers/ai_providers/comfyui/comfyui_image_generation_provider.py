"""ComfyUI image generation provider using workflow JSON + prompt/history APIs.

The graph sent over the wire is chosen by *workflow* — a named entry in
``workflow_registry.yaml`` that owns the graph JSON, the node/input bindings
and the literal node overrides (see ``workflow_registry.py``). The provider
stays transport-only: it never touches the prompt text and never decides
anything for itself. The caller passes a workflow name, which comes from
AppDaemon config; everything else follows from it.
"""

from __future__ import annotations

import copy
import json
import logging
import mimetypes
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

from ..image_generation_provider import (
    ExternalImageGenError,
    ImageGenerationProvider,
    ImageProviderCapabilities,
    ImageProviderName,
)
from .workflow_registry import (
    Workflow,
    WorkflowRegistryError,
    load_workflow_registry,
)

# Where a workflow name came from, recorded in result meta so a rendered image
# can be traced back to the config that chose it.
SOURCE_APP_CONFIG = "app_config"
SOURCE_BUNDLE = "bundle"
SOURCE_REGISTRY_DEFAULT = "registry_default"

DEFAULT_UPLOAD_NAMESPACE = "comfyui"
DEFAULT_FILENAME_PREFIX = "detection-summary"

_UNSAFE_UPLOAD_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class ComfyUIWorkflowRejectedError(ExternalImageGenError):
    """The workflow itself was rejected — the request will never succeed as sent.

    Raised for an unknown or invalid workflow, a ComfyUI ``POST /prompt``
    validation failure (HTTP 400 — including a model file that is not on the
    server), and an ``execution_error`` reported in history.

    Deliberately NOT raised for timeouts or connection errors: those say
    nothing about the workflow, and falling back to another one would just
    burn a second render.
    """


@dataclass(frozen=True)
class ComfyUIImageGenerationConfig:
    """Everything the provider needs that the workflow itself does not own."""

    base_url: str
    # Workflow to send, by name. None -> the registry's default_workflow.
    workflow_name: Optional[str] = None
    # Where that name came from, for meta/logs only.
    workflow_source: str = SOURCE_REGISTRY_DEFAULT
    # Workflow to retry with once when the requested one is rejected.
    fallback_workflow_name: Optional[str] = None
    # Explicit timeout override. None -> the producing workflow's timeout_s.
    timeout_s: Optional[float] = None
    # Upload filename namespace — one per caller (e.g. a camera zone) so two
    # callers cannot overwrite each other's input frames on the server.
    upload_namespace: Optional[str] = None
    poll_interval_s: float = 1.0
    # Warn when the first input image's total pixels fall below this.
    min_input_pixels: Optional[int] = None
    provider_options: Dict[str, Any] = field(default_factory=dict)


# ComfyUI's LoadImage resolves filenames at *execution* time, and uploads use
# overwrite=true, so an upload for run B can replace the file run A queued but
# has not rendered yet. One lock per upload namespace, held from the first
# upload until the prompt reaches a terminal state, makes that impossible.
# Module-level on purpose: callers build a fresh provider instance per run, so
# an instance-level lock would never be contended.
#
# DetectionSummary already refuses to start a second run for a zone while one
# is in flight, so with one namespace per zone this is insurance rather than
# the primary guard — but it is what makes the namespace contract true for any
# caller, and it costs nothing when uncontended.
_UPLOAD_LOCKS: Dict[str, threading.Lock] = {}
_UPLOAD_LOCKS_GUARD = threading.Lock()


def _upload_lock(namespace: str) -> threading.Lock:
    with _UPLOAD_LOCKS_GUARD:
        lock = _UPLOAD_LOCKS.get(namespace)
        if lock is None:
            lock = threading.Lock()
            _UPLOAD_LOCKS[namespace] = lock
        return lock


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _sanitize_upload_component(value: str) -> str:
    """Reduce a name component to the characters ComfyUI's input dir tolerates."""
    cleaned = _UNSAFE_UPLOAD_CHARS.sub("_", str(value or "").strip())
    return cleaned.strip("._-") or "input"


def build_upload_name(namespace: str, slot: int, source: Path) -> str:
    """``<namespace>-slot<N><suffix>`` — stable per namespace+slot, sanitised."""
    suffix = _UNSAFE_UPLOAD_CHARS.sub("_", source.suffix or "")
    return f"{_sanitize_upload_component(namespace)}-slot{int(slot)}{suffix}"


def _build_multipart(
    *,
    image_path: Path,
    upload_name: str,
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
                f'Content-Disposition: form-data; name="image"; filename="{upload_name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8"),
            image_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(lines), boundary


def _get_image_dimensions(path: Path) -> Optional[Tuple[int, int]]:
    """Read width/height from JPEG or PNG headers without Pillow."""
    try:
        with open(path, "rb") as f:
            header = f.read(32)
            # PNG: bytes 16-23 of IHDR are width (4B) + height (4B)
            if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
                w, h = struct.unpack(">II", header[16:24])
                return (w, h)
            # JPEG: scan for SOF0 (0xFFC0) or SOF2 (0xFFC2) marker
            if header[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    if marker[1] in (0xC0, 0xC2):
                        seg = f.read(7)
                        if len(seg) < 7:
                            return None
                        h, w = struct.unpack(">HH", seg[3:7])
                        return (w, h)
                    if marker[1] == 0xD9:  # EOI
                        return None
                    seg_len_b = f.read(2)
                    if len(seg_len_b) < 2:
                        return None
                    seg_len = struct.unpack(">H", seg_len_b)[0]
                    f.seek(seg_len - 2, 1)
    except Exception:
        pass
    return None


def _describe_node_errors(payload: Any) -> str:
    """Summarise a ComfyUI ``/prompt`` 400 body, naming the offending inputs.

    A missing model file arrives as ``value_not_in_list``; the input name and
    the value we sent are the two facts an operator needs.
    """
    if not isinstance(payload, dict):
        return ""
    node_errors = payload.get("node_errors")
    if not isinstance(node_errors, dict) or not node_errors:
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or "").strip()
        return ""

    parts: List[str] = []
    for node_id, node_error in node_errors.items():
        if not isinstance(node_error, dict):
            continue
        class_type = str(node_error.get("class_type") or "").strip()
        for err in node_error.get("errors") or []:
            if not isinstance(err, dict):
                continue
            extra = err.get("extra_info") if isinstance(err.get("extra_info"), dict) else {}
            input_name = str(extra.get("input_name") or err.get("input_name") or "").strip()
            received = extra.get("received_value", err.get("received_value"))
            err_type = str(err.get("type") or "").strip()
            detail = f"node {node_id}"
            if class_type:
                detail += f" ({class_type})"
            if err_type:
                detail += f" {err_type}"
            if input_name:
                detail += f" input_name={input_name!r}"
            if received is not None:
                detail += f" received_value={received!r}"
            message = str(err.get("message") or "").strip()
            if message:
                detail += f": {message}"
            parts.append(detail)
    return "; ".join(parts)


class ComfyUIImageGenerationProvider(ImageGenerationProvider):
    name = ImageProviderName.COMFYUI
    capabilities = ImageProviderCapabilities(
        supports_text_to_image=False,
        supports_image_to_image=True,
        supports_inpaint=False,
        notes="Uses ComfyUI workflow API for image-to-image generation.",
    )

    def __init__(self, config: ComfyUIImageGenerationConfig):
        self._config = config
        # Loading here fails fast on a malformed registry rather than at the
        # first render, 10 minutes into an evening.
        self._registry = load_workflow_registry()
        fallback = str(config.fallback_workflow_name or "").strip()
        if fallback and not self._registry.has(fallback):
            raise ValueError(
                f"ComfyUI fallback_workflow_name {fallback!r} is not a registered workflow; "
                f"registered workflows: {list(self._registry.names)}"
            )

    # --- what this provider will send ----------------------------------

    @property
    def workflow_name(self) -> str:
        """The workflow this provider will request, resolved against the registry."""
        return str(self._config.workflow_name or "").strip() or self._registry.default_workflow

    @property
    def workflow_source(self) -> str:
        """Where :attr:`workflow_name` came from — config, bundle, or the registry."""
        return self._config.workflow_source

    # --- public API ----------------------------------------------------

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

        requested = self.workflow_name
        fallback = str(self._config.fallback_workflow_name or "").strip()
        started = time.time()

        try:
            return self._attempt(
                name=requested,
                requested_name=requested,
                in_paths=in_paths,
                prompt=str(prompt),
                out_path=out_path,
                started=started,
            )
        except ComfyUIWorkflowRejectedError as exc:
            if not fallback or fallback == requested:
                raise
            logger.warning(
                "ComfyUI workflow %r was rejected (%s); retrying once with the fallback "
                "workflow %r",
                requested,
                exc,
                fallback,
            )
            return self._attempt(
                name=fallback,
                requested_name=requested,
                in_paths=in_paths,
                prompt=str(prompt),
                out_path=out_path,
                started=started,
                fallback_reason=str(exc),
            )

    # --- one attempt against one workflow ------------------------------

    def _attempt(
        self,
        *,
        name: str,
        requested_name: str,
        in_paths: List[Path],
        prompt: str,
        out_path: Path,
        started: float,
        fallback_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            workflow = self._registry.get(name)
        except WorkflowRegistryError as exc:
            raise ComfyUIWorkflowRejectedError(str(exc)) from exc

        timeout_s = float(
            self._config.timeout_s if self._config.timeout_s is not None else workflow.timeout_s
        )
        namespace = str(self._config.upload_namespace or "").strip() or DEFAULT_UPLOAD_NAMESPACE

        used_paths = in_paths[: workflow.max_images]
        ignored_paths = in_paths[workflow.max_images :]

        input_dimensions = self._check_primary_input(used_paths[0])

        with _upload_lock(namespace):
            uploaded_names: List[str] = []
            for slot_idx, path in enumerate(used_paths):
                uploaded_names.append(
                    self._upload_image(
                        path,
                        upload_name=build_upload_name(namespace, slot_idx, path),
                        timeout_s=timeout_s,
                    )
                )

            graph = self._build_graph(
                workflow=workflow,
                prompt=prompt,
                uploaded_names=uploaded_names,
                output_path=out_path,
            )

            prompt_id = self._queue_prompt(graph, timeout_s=timeout_s, workflow=workflow)
            history_entry = self._wait_for_history(prompt_id, timeout_s=timeout_s)

        image_info = self._extract_output_image_info(history_entry, workflow=workflow)
        img_bytes = self._download_output_image(image_info, timeout_s=timeout_s)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img_bytes)

        meta: Dict[str, Any] = {
            "backend": "external",
            "provider": "comfyui",
            "endpoint": self._config.base_url.rstrip("/"),
            "model": workflow.model,
            "created_at_epoch": time.time(),
            "elapsed_s": round(time.time() - started, 3),
            "input_paths": [str(p) for p in in_paths],
            "output_path": str(out_path),
            "prompt_id": prompt_id,
            "uploaded_input_name": uploaded_names[0],
            "uploaded_input_names": list(uploaded_names),
            "workflow_name": workflow.name,
            "workflow_name_requested": requested_name,
            "workflow_source": self._config.workflow_source,
            "workflow_graph": workflow.graph,
            "workflow_model_released": workflow.model_released.isoformat(),
            "required_models": list(workflow.required_models),
            "timeout_s": timeout_s,
            "request": {"prompt_len": len(prompt), "prompt": prompt},
            "response": {"image_info": image_info},
        }
        if fallback_reason:
            meta["workflow_fallback_reason"] = fallback_reason
        if input_dimensions:
            meta["input_dimensions"] = {"width": input_dimensions[0], "height": input_dimensions[1]}
        if ignored_paths:
            meta["ignored_input_paths"] = [str(p) for p in ignored_paths]
        return meta

    def _check_primary_input(self, primary_input: Path) -> Optional[Tuple[int, int]]:
        input_dimensions = _get_image_dimensions(primary_input)
        if input_dimensions and self._config.min_input_pixels:
            w, h = input_dimensions
            total = w * h
            if total < self._config.min_input_pixels:
                logger.warning(
                    "Input image %s is %dx%d (%d pixels), below min_input_pixels=%d. "
                    "Low-res inputs may produce poor stylization results.",
                    primary_input.name, w, h, total, self._config.min_input_pixels,
                )
        return input_dimensions

    # --- workflow construction -----------------------------------------

    def _build_graph(
        self,
        *,
        workflow: Workflow,
        prompt: str,
        uploaded_names: Sequence[str],
        output_path: Path,
    ) -> Dict[str, Any]:
        try:
            template = workflow.load_graph()
        except WorkflowRegistryError as exc:
            raise ComfyUIWorkflowRejectedError(str(exc)) from exc

        graph = copy.deepcopy(template)
        supplied = len(uploaded_names)

        try:
            for slot_idx, slot in enumerate(workflow.images):
                if slot_idx < supplied:
                    graph[slot.node]["inputs"][slot.input] = uploaded_names[slot_idx]
                    continue
                # Fewer images than slots: drop the LoadImage node and every
                # optional input that consumed it, or ComfyUI rejects the graph.
                for target in slot.unlink:
                    node_id, _, input_name = str(target).partition(".")
                    node = graph.get(node_id)
                    if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                        node["inputs"].pop(input_name, None)
                graph.pop(slot.node, None)

            graph[workflow.prompt.node]["inputs"][workflow.prompt.input] = prompt
            if workflow.negative_prompt is not None:
                graph[workflow.negative_prompt.node]["inputs"][workflow.negative_prompt.input] = ""
            if workflow.seed is not None:
                graph[workflow.seed.node]["inputs"][workflow.seed.input] = int(time.time() * 1000)
            graph[workflow.output.node]["inputs"][workflow.output.input] = (
                output_path.stem or DEFAULT_FILENAME_PREFIX
            )
            for node_id, node_overrides in workflow.overrides.items():
                graph[node_id]["inputs"].update(dict(node_overrides))
        except KeyError as exc:
            # Load-time validation should make this unreachable; if the JSON
            # changed under us it is still a workflow fault, not a transport one.
            raise ComfyUIWorkflowRejectedError(
                f"workflow {workflow.name!r}: graph is missing expected node/input: {exc!r}"
            ) from exc
        return graph

    # --- HTTP -----------------------------------------------------------

    def _upload_image(self, image_path: Path, *, upload_name: str, timeout_s: float) -> str:
        body, boundary = _build_multipart(
            image_path=image_path,
            upload_name=upload_name,
            overwrite=True,
        )
        req = urllib.request.Request(
            url=f"{self._config.base_url.rstrip('/')}/upload/image",
            method="POST",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        payload = self._read_json(req, timeout_s=timeout_s)
        name = str(payload.get("name") or upload_name).strip()
        if not name:
            raise ExternalImageGenError(f"ComfyUI upload response missing image name: {payload!r}")
        return name

    def _queue_prompt(
        self, graph: Dict[str, Any], *, timeout_s: float, workflow: Workflow
    ) -> str:
        req = urllib.request.Request(
            url=f"{self._config.base_url.rstrip('/')}/prompt",
            method="POST",
            data=_safe_json({"prompt": graph}),
            headers={"Content-Type": "application/json"},
        )
        payload = self._read_json(req, timeout_s=timeout_s, reject_workflow=workflow)
        prompt_id = str(payload.get("prompt_id") or "").strip()
        if not prompt_id:
            raise ExternalImageGenError(f"ComfyUI prompt response missing prompt_id: {payload!r}")
        return prompt_id

    def _wait_for_history(self, prompt_id: str, *, timeout_s: float) -> Dict[str, Any]:
        deadline = time.time() + float(timeout_s)
        last_payload: Any = None
        while time.time() < deadline:
            req = urllib.request.Request(
                url=f"{self._config.base_url.rstrip('/')}/history/{urllib.parse.quote(prompt_id, safe='')}",
                method="GET",
            )
            try:
                payload = self._read_json(req, timeout_s=min(30.0, float(timeout_s)))
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
                        raise ComfyUIWorkflowRejectedError(f"ComfyUI execution error: {msg!r}")
                if entry.get("outputs"):
                    return entry
            time.sleep(float(self._config.poll_interval_s))

        raise ExternalImageGenError(
            f"Timed out waiting for ComfyUI prompt_id={prompt_id} after {timeout_s:.0f}s; "
            f"last_payload={str(last_payload)[:400]!r}"
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

    def _extract_output_image_info(
        self, history_entry: Dict[str, Any], *, workflow: Workflow
    ) -> Dict[str, Any]:
        outputs = history_entry.get("outputs") or {}
        save_output = outputs.get(workflow.output.node)
        if not isinstance(save_output, dict):
            raise ExternalImageGenError(
                f"ComfyUI history missing save-image node {workflow.output.node!r} "
                f"for workflow {workflow.name!r}: {outputs!r}"
            )
        images = save_output.get("images") or []
        if not images or not isinstance(images[0], dict):
            raise ExternalImageGenError(f"ComfyUI save-image node missing images: {save_output!r}")
        return images[0]

    def _download_output_image(self, image_info: Dict[str, Any], *, timeout_s: float) -> bytes:
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
            with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
                return resp.read()
        except Exception as e:
            raise ExternalImageGenError(f"failed to download ComfyUI output image: {e!r}") from e

    def _read_json(
        self,
        req: urllib.request.Request,
        *,
        timeout_s: float,
        reject_workflow: Optional[Workflow] = None,
    ) -> Dict[str, Any]:
        """GET/POST JSON.

        ``reject_workflow`` marks a call where an HTTP 400 is ComfyUI refusing
        the *graph* (prompt validation), which is a workflow fault worth
        falling back on — not a transport failure.
        """
        try:
            with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if reject_workflow is not None and e.code == 400:
                summary = ""
                try:
                    summary = _describe_node_errors(json.loads(detail))
                except Exception:
                    summary = ""
                raise ComfyUIWorkflowRejectedError(
                    f"ComfyUI rejected workflow {reject_workflow.name!r} "
                    f"({reject_workflow.graph}): {summary or detail[:400]}"
                ) from e
            raise ExternalImageGenError(f"comfyui http error: {e.code} {e.reason}; {detail}") from e
        except Exception as e:
            raise ExternalImageGenError(f"comfyui request failed: {e!r}") from e
