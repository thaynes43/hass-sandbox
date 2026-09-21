"""Tests for the ComfyUI image generation provider (workflow-driven)."""

from __future__ import annotations

import io
import json
import struct
import sys
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.comfyui.comfyui_image_generation_provider import (  # noqa: E402
    ComfyUIImageGenerationConfig,
    ComfyUIImageGenerationProvider,
    ComfyUIWorkflowRejectedError,
    _get_image_dimensions,
    _upload_lock,
    build_upload_name,
)
from providers.ai_providers.comfyui.workflow_registry import (  # noqa: E402
    load_workflow_registry,
)
from providers.ai_providers.image_generation_provider import ExternalImageGenError  # noqa: E402

# ---------- fakes ----------


def _mock_response(payload: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _json_response(payload: Any) -> MagicMock:
    return _mock_response(json.dumps(payload).encode("utf-8"))


def _upload_response(name: str = "uploaded.jpg") -> MagicMock:
    return _json_response({"name": name, "subfolder": "", "type": "input"})


def _history_response(prompt_id: str, *, save_node: str = "60") -> MagicMock:
    return _json_response(
        {
            prompt_id: {
                "outputs": {
                    save_node: {
                        "images": [{"filename": "generated.png", "subfolder": "", "type": "output"}]
                    }
                },
                "status": {"messages": []},
            }
        }
    )


def _view_response() -> MagicMock:
    return _mock_response(b"\x89PNG\r\n\x1a\n")


def _http_error(code: int, body: Any) -> urllib.error.HTTPError:
    raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return urllib.error.HTTPError(
        "http://comfy/prompt", code, "Bad Request", {}, io.BytesIO(raw)
    )


_VALUE_NOT_IN_LIST_BODY = {
    "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"},
    "node_errors": {
        "115:37": {
            "class_type": "UNETLoader",
            "errors": [
                {
                    "type": "value_not_in_list",
                    "message": "Value not in list",
                    "extra_info": {
                        "input_name": "unet_name",
                        "received_value": "qwen_image_edit_2509_fp8_e4m3fn.safetensors",
                    },
                }
            ],
        }
    },
}


class _Urlopen:
    """Records every request and replays a scripted list of responses."""

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.requests: List[Any] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001 - urlopen signature
        self.requests.append(req)
        if not self._responses:
            raise AssertionError(f"unexpected extra request to {req.full_url}")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def urls(self) -> List[str]:
        return [r.full_url for r in self.requests]


def _make_provider(**kwargs: Any) -> ComfyUIImageGenerationProvider:
    defaults: Dict[str, Any] = {"base_url": "https://comfyui.haynesops.com"}
    defaults.update(kwargs)
    return ComfyUIImageGenerationProvider(ComfyUIImageGenerationConfig(**defaults))


def _write_jpeg(path: Path, w: int = 1920, h: int = 1080) -> Path:
    path.write_bytes(_make_minimal_jpeg(w, h))
    return path


def _inputs(tmp_path: Path, count: int, suffix: str = ".jpg") -> List[str]:
    out = []
    for i in range(count):
        p = tmp_path / f"frame_{i}{suffix}"
        _write_jpeg(p)
        out.append(str(p))
    return out


# ---------- happy path ----------


def test_default_workflow_generates_and_reports_meta(tmp_path: Path) -> None:
    out_path = tmp_path / "generated.png"
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _json_response({"prompt_id": "pid-123"}),
         _history_response("pid-123"), _view_response()]
    )
    provider = _make_provider(upload_namespace="garage")

    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="Turn this into a clean illustration.",
            output_image_path=str(out_path),
        )

    assert out_path.read_bytes().startswith(b"\x89PNG")
    assert result["provider"] == "comfyui"
    assert result["prompt_id"] == "pid-123"
    assert result["model"] == "qwen-image-edit-2509"
    assert result["workflow_name"] == "qwen-image-edit-2509-lightning4-legacy"
    assert result["workflow_name_requested"] == "qwen-image-edit-2509-lightning4-legacy"
    assert "workflow_fallback_reason" not in result
    assert result["uploaded_input_name"] == "garage-slot0.jpg"
    assert result["uploaded_input_names"] == ["garage-slot0.jpg"]
    assert result["timeout_s"] == 900.0
    assert "qwen_image_edit_2509_fp8_e4m3fn.safetensors" in result["required_models"]


def test_explicit_workflow_is_used(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response(), _upload_response(), _upload_response(),
         _json_response({"prompt_id": "pid-t"}), _history_response("pid-t"), _view_response()]
    )
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned-3frame", upload_namespace="garage"
    )
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 3),
            prompt="p",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert result["workflow_name"] == "qwen-image-edit-2509-lightning4-tuned-3frame"
    assert result["timeout_s"] == 1200.0
    assert len(result["uploaded_input_names"]) == 3


def test_tuned_workflow_uploads_only_the_first_frame(tmp_path: Path) -> None:
    """Single-frame by design: a 3-image render costs ~4x on the real server."""
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    provider = _make_provider(workflow_name="qwen-image-edit-2509-lightning4-tuned", upload_namespace="garage")
    paths = _inputs(tmp_path, 3)
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=paths, prompt="p", output_image_path=str(tmp_path / "out.png")
        )
    assert result["uploaded_input_names"] == ["garage-slot0.jpg"]
    assert result["ignored_input_paths"] == paths[1:]
    assert result["timeout_s"] == 900.0


def test_explicit_timeout_overrides_the_workflow(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid"}), _history_response("pid"), _view_response()]
    )
    provider = _make_provider(timeout_s=42.0)
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert result["timeout_s"] == 42.0


def test_multiframe_workflow_timeout(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid"}), _history_response("pid"), _view_response()]
    )
    provider = _make_provider(workflow_name="qwen-image-edit-2509-lightning4-tuned-3frame")
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert result["timeout_s"] == 1200.0


# ---------- input validation ----------


def test_requires_input_paths(tmp_path: Path) -> None:
    provider = _make_provider()
    with pytest.raises(ExternalImageGenError) as exc_info:
        provider.edit_image(input_image_paths=[], prompt="x", output_image_path=str(tmp_path / "o.png"))
    assert "input_image_paths" in str(exc_info.value)


def test_requires_existing_inputs(tmp_path: Path) -> None:
    provider = _make_provider()
    with pytest.raises(ExternalImageGenError) as exc_info:
        provider.edit_image(
            input_image_paths=[str(tmp_path / "missing.jpg")],
            prompt="x",
            output_image_path=str(tmp_path / "o.png"),
        )
    assert "do not exist" in str(exc_info.value)


def test_requires_prompt(tmp_path: Path) -> None:
    provider = _make_provider()
    with pytest.raises(ExternalImageGenError) as exc_info:
        provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="   ",
            output_image_path=str(tmp_path / "o.png"),
        )
    assert "prompt is required" in str(exc_info.value)


def test_unknown_fallback_workflow_fails_at_construction() -> None:
    with pytest.raises(ValueError) as exc_info:
        _make_provider(fallback_workflow_name="nope")
    assert "not a registered workflow" in str(exc_info.value)


# ---------- workflow construction ----------


def _build(workflow_name: str, uploaded: List[str], out: str = "generated.png") -> Dict[str, Any]:
    provider = _make_provider()
    workflow = load_workflow_registry().get(workflow_name)
    return provider._build_graph(
        workflow=workflow, prompt="PROMPT", uploaded_names=uploaded, output_path=Path(out)
    )


def _template(workflow_name: str) -> Dict[str, Any]:
    return load_workflow_registry().get(workflow_name).load_graph()


def test_legacy_workflow_only_differs_from_its_graph_by_bindings_and_overrides() -> None:
    """The rollback guarantee: deploying this release changes nothing on disk.

    Asserted structurally — every node/input that differs from the shipped
    1-image template must be one the workflow is declared to write.
    """
    built = _build("qwen-image-edit-2509-lightning4-legacy", ["garage-slot0.jpg"])
    template = _template("qwen-image-edit-2509-lightning4-legacy")

    assert set(built) == set(template), "no nodes may be added or removed"

    differences = set()
    for node_id, node in template.items():
        built_inputs = built[node_id]["inputs"]
        for key in set(node["inputs"]) | set(built_inputs):
            if node["inputs"].get(key) != built_inputs.get(key):
                differences.add((node_id, key))
        assert built[node_id]["class_type"] == node["class_type"]

    assert differences == {
        ("78", "image"),          # slot 0
        ("115:111", "prompt"),    # positive prompt
        ("115:3", "seed"),        # per-run seed
        ("60", "filename_prefix"),  # output name
        ("115:3", "cfg"),         # override
        ("115:93", "megapixels"),  # override
    }
    assert built["78"]["inputs"]["image"] == "garage-slot0.jpg"
    assert built["115:111"]["inputs"]["prompt"] == "PROMPT"
    assert built["115:110"]["inputs"]["prompt"] == ""
    assert built["60"]["inputs"]["filename_prefix"] == "generated"
    assert built["115:3"]["inputs"]["cfg"] == 3.5
    assert built["115:3"]["inputs"]["steps"] == 4  # workflow default, not overridden
    assert built["115:93"]["inputs"]["megapixels"] == 1.5


def test_tuned_workflow_is_single_slot_on_a_cleaned_graph() -> None:
    built = _build("qwen-image-edit-2509-lightning4-tuned", ["a-slot0.jpg"])
    template = _template("qwen-image-edit-2509-lightning4-tuned")
    # The cleaned sibling graph drops the two orphan nodes the original keeps.
    assert "115:112" not in template
    assert "115:116" not in template
    assert built["78"]["inputs"]["image"] == "a-slot0.jpg"
    assert built["115:3"]["inputs"]["cfg"] == 1
    assert built["115:3"]["inputs"]["steps"] == 4
    assert built["115:93"]["inputs"]["megapixels"] == 1.0


def test_multiframe_workflow_binds_three_images() -> None:
    built = _build("qwen-image-edit-2509-lightning4-tuned-3frame", ["a-slot0.jpg", "a-slot1.jpg", "a-slot2.jpg"])
    assert built["78"]["inputs"]["image"] == "a-slot0.jpg"
    assert built["120"]["inputs"]["image"] == "a-slot1.jpg"
    assert built["121"]["inputs"]["image"] == "a-slot2.jpg"
    assert built["115:111"]["inputs"]["image2"] == ["120", 0]
    assert built["115:111"]["inputs"]["image3"] == ["121", 0]
    assert built["115:3"]["inputs"]["cfg"] == 1
    assert built["115:93"]["inputs"]["megapixels"] == 1.0


def test_multiframe_workflow_prunes_unused_slots_with_two_images() -> None:
    built = _build("qwen-image-edit-2509-lightning4-tuned-3frame", ["a-slot0.jpg", "a-slot1.jpg"])
    assert "120" in built
    assert "121" not in built
    assert built["115:111"]["inputs"]["image2"] == ["120", 0]
    assert "image3" not in built["115:111"]["inputs"]
    assert "image3" not in built["115:110"]["inputs"]


def test_multiframe_workflow_prunes_both_extra_slots_with_one_image() -> None:
    built = _build("qwen-image-edit-2509-lightning4-tuned-3frame", ["a-slot0.jpg"])
    assert "120" not in built
    assert "121" not in built
    for node in ("115:111", "115:110"):
        assert "image2" not in built[node]["inputs"]
        assert "image3" not in built[node]["inputs"]
        assert built[node]["inputs"]["image1"] == ["115:93", 0]


def test_filename_prefix_falls_back_when_output_has_no_stem() -> None:
    built = _build("qwen-image-edit-2509-lightning4-legacy", ["a-slot0.jpg"], out="")
    assert built["60"]["inputs"]["filename_prefix"] == "detection-summary"


def test_extra_inputs_beyond_the_slots_are_reported_as_ignored(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    provider = _make_provider(upload_namespace="garage")  # 1-slot default workflow
    paths = _inputs(tmp_path, 4)
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=paths, prompt="p", output_image_path=str(tmp_path / "o.png")
        )
    assert result["uploaded_input_names"] == ["garage-slot0.jpg"]
    assert result["ignored_input_paths"] == paths[1:]
    assert result["input_paths"] == paths
    # Only one upload happened: upload + prompt + history + view.
    assert fake.call_count == 4


def test_four_inputs_fill_three_slots_on_the_multiframe_workflow(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _upload_response("garage-slot1.jpg"),
         _upload_response("garage-slot2.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned-3frame", upload_namespace="garage"
    )
    paths = _inputs(tmp_path, 4)
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=paths, prompt="p", output_image_path=str(tmp_path / "o.png")
        )
    assert result["uploaded_input_names"] == [
        "garage-slot0.jpg", "garage-slot1.jpg", "garage-slot2.jpg"
    ]
    assert result["ignored_input_paths"] == paths[3:]


# ---------- upload naming ----------


def test_build_upload_name_shape() -> None:
    assert build_upload_name("garage", 0, Path("/x/best.jpg")) == "garage-slot0.jpg"
    assert build_upload_name("garage", 2, Path("/x/frame_003.png")) == "garage-slot2.png"


def test_build_upload_name_sanitises_namespace_and_suffix() -> None:
    assert build_upload_name("back deck/pets", 1, Path("/x/f.jpg")) == "back_deck_pets-slot1.jpg"
    assert build_upload_name("../../etc", 0, Path("/x/f.jpg")) == "etc-slot0.jpg"
    assert build_upload_name("", 0, Path("/x/f")) == "input-slot0"
    assert build_upload_name("zone", 0, Path("/x/f.j p&g")) == "zone-slot0.j_p_g"


def test_uploads_are_namespaced_per_slot(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response("a-slot0.jpg"), _upload_response("a-slot1.jpg"), _upload_response("a-slot2.jpg"),
         _json_response({"prompt_id": "pid"}), _history_response("pid"), _view_response()]
    )
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned-3frame", upload_namespace="back yard pets"
    )
    with patch("urllib.request.urlopen", new=fake):
        provider.edit_image(
            input_image_paths=_inputs(tmp_path, 3),
            prompt="p",
            output_image_path=str(tmp_path / "o.png"),
        )

    uploads = [r for r in fake.requests if r.full_url.endswith("/upload/image")]
    assert len(uploads) == 3
    bodies = [r.data.decode("utf-8", errors="replace") for r in uploads]
    for slot, body in enumerate(bodies):
        assert f'filename="back_yard_pets-slot{slot}.jpg"' in body
        assert 'name="overwrite"\r\n\r\ntrue' in body


def test_upload_name_defaults_to_the_generic_namespace(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid"}), _history_response("pid"), _view_response()]
    )
    provider = _make_provider()
    with patch("urllib.request.urlopen", new=fake):
        provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "o.png"),
        )
    body = fake.requests[0].data.decode("utf-8", errors="replace")
    assert 'filename="comfyui-slot0.jpg"' in body


# ---------- upload serialisation ----------


def test_upload_locks_are_per_namespace() -> None:
    assert _upload_lock("garage") is _upload_lock("garage")
    assert _upload_lock("garage") is not _upload_lock("bulkhead")


def test_same_namespace_runs_do_not_interleave(tmp_path: Path) -> None:
    """Two runs in one namespace must not overlap between upload and terminal state.

    ComfyUI's LoadImage resolves filenames at execution time and uploads
    overwrite, so an unserialised second run would replace the frame the first
    one queued but has not rendered yet.
    """
    active = 0
    overlapped = False
    guard = threading.Lock()

    def _by_url(req, timeout=None):  # noqa: ANN001 - one shared patch for both threads
        url = req.full_url
        if url.endswith("/upload/image"):
            return _upload_response()
        if url.endswith("/prompt"):
            return _json_response({"prompt_id": "pid"})
        return _view_response()

    def _slow_history(self, prompt_id, *, timeout_s):  # noqa: ANN001
        nonlocal active, overlapped
        with guard:
            active += 1
            if active > 1:
                overlapped = True
        time.sleep(0.05)
        with guard:
            active -= 1
        return {
            "outputs": {"60": {"images": [{"filename": "g.png", "subfolder": "", "type": "output"}]}},
            "status": {"messages": []},
        }

    def _run(namespace: str, work_dir: str) -> None:
        provider = _make_provider(upload_namespace=namespace)
        provider.edit_image(
            input_image_paths=_inputs(tmp_path / work_dir, 1),
            prompt="p",
            output_image_path=str(tmp_path / work_dir / "o.png"),
        )

    for name in ("a", "b", "c", "d"):
        (tmp_path / name).mkdir()

    with patch("urllib.request.urlopen", new=_by_url), \
         patch.object(ComfyUIImageGenerationProvider, "_wait_for_history", _slow_history):
        threads = [
            threading.Thread(target=_run, args=("garage", "a")),
            threading.Thread(target=_run, args=("garage", "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not overlapped, "two runs in the same namespace overlapped"

        # Different namespaces share no lock, so they must both finish.
        threads = [
            threading.Thread(target=_run, args=("garage", "c")),
            threading.Thread(target=_run, args=("bulkhead", "d")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(not t.is_alive() for t in threads)
    assert (tmp_path / "d" / "o.png").exists()


# ---------- error taxonomy + fallback ----------


def test_http_400_value_not_in_list_is_a_workflow_rejection(tmp_path: Path) -> None:
    fake = _Urlopen([_upload_response(), _http_error(400, _VALUE_NOT_IN_LIST_BODY)])
    provider = _make_provider()  # requested == fallback default -> no retry
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ComfyUIWorkflowRejectedError) as exc_info:
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    message = str(exc_info.value)
    assert "qwen-image-edit-2509-lightning4-legacy" in message
    assert "value_not_in_list" in message
    assert "input_name='unet_name'" in message
    assert "qwen_image_edit_2509_fp8_e4m3fn.safetensors" in message
    assert fake.call_count == 2, "a rejected default must not be retried"


def test_rejection_falls_back_exactly_once(tmp_path: Path) -> None:
    fake = _Urlopen(
        [
            _upload_response(),                       # attempt 1 (tuned) upload
            _http_error(400, _VALUE_NOT_IN_LIST_BODY),  # attempt 1 rejected
            _upload_response("garage-slot0.jpg"),     # attempt 2 (original) upload
            _json_response({"prompt_id": "pid-fb"}),
            _history_response("pid-fb"),
            _view_response(),
        ]
    )
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned",
        fallback_workflow_name="qwen-image-edit-2509-lightning4-legacy",
        upload_namespace="garage",
    )
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "o.png"),
        )

    assert result["workflow_name"] == "qwen-image-edit-2509-lightning4-legacy"
    assert result["workflow_name_requested"] == "qwen-image-edit-2509-lightning4-tuned"
    assert "qwen-image-edit-2509-lightning4-tuned" in result["workflow_fallback_reason"]
    assert "unet_name" in result["workflow_fallback_reason"]
    assert result["timeout_s"] == 900.0
    assert fake.call_count == 6


def test_fallback_is_not_retried_when_it_is_also_rejected(tmp_path: Path) -> None:
    fake = _Urlopen(
        [
            _upload_response(), _http_error(400, _VALUE_NOT_IN_LIST_BODY),
            _upload_response(), _http_error(400, _VALUE_NOT_IN_LIST_BODY),
        ]
    )
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned", fallback_workflow_name="qwen-image-edit-2509-lightning4-legacy"
    )
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ComfyUIWorkflowRejectedError):
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert fake.call_count == 4, "exactly one fallback attempt, then give up"


def test_unknown_requested_workflow_falls_back(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid"}), _history_response("pid"), _view_response()]
    )
    provider = _make_provider(
        workflow_name="ghost-workflow", fallback_workflow_name="qwen-image-edit-2509-lightning4-legacy"
    )
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "o.png"),
        )
    assert result["workflow_name"] == "qwen-image-edit-2509-lightning4-legacy"
    assert result["workflow_name_requested"] == "ghost-workflow"
    assert "ghost-workflow" in result["workflow_fallback_reason"]


def test_execution_error_in_history_is_a_workflow_rejection(tmp_path: Path) -> None:
    history = _json_response(
        {
            "pid-x": {
                "outputs": {},
                "status": {"messages": [["execution_error", {"exception_message": "boom"}]]},
            }
        }
    )
    fake = _Urlopen([_upload_response(), _json_response({"prompt_id": "pid-x"}), history])
    provider = _make_provider()
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ComfyUIWorkflowRejectedError) as exc_info:
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert "execution error" in str(exc_info.value)


def test_non_400_http_error_is_not_a_workflow_rejection(tmp_path: Path) -> None:
    fake = _Urlopen([_upload_response(), _http_error(500, b"kaboom")])
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned", fallback_workflow_name="qwen-image-edit-2509-lightning4-legacy"
    )
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ExternalImageGenError) as exc_info:
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert not isinstance(exc_info.value, ComfyUIWorkflowRejectedError)
    assert fake.call_count == 2, "a 500 must not trigger a fallback render"


def test_timeout_does_not_fall_back(tmp_path: Path) -> None:
    """A slow render says nothing about the workflow — never burn a second one."""
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned",
        fallback_workflow_name="qwen-image-edit-2509-lightning4-legacy",
        timeout_s=0.05,
        poll_interval_s=0.0,
    )
    responses: List[Any] = [_upload_response(), _json_response({"prompt_id": "pid-slow"})]
    fake = _Urlopen(responses)

    def _never_done(req, timeout=None):  # noqa: ANN001
        fake.requests.append(req)
        if responses:
            nxt = responses.pop(0)
            return nxt
        return _json_response({"pid-slow": {"status": {"messages": []}}})

    with patch("urllib.request.urlopen", new=_never_done):
        with pytest.raises(ExternalImageGenError) as exc_info:
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert not isinstance(exc_info.value, ComfyUIWorkflowRejectedError)
    assert "Timed out" in str(exc_info.value)
    upload_count = len([r for r in fake.requests if r.full_url.endswith("/upload/image")])
    assert upload_count == 1, "the fallback workflow must not have been attempted"


def test_connection_error_does_not_fall_back(tmp_path: Path) -> None:
    fake = _Urlopen([OSError("connection refused")])
    provider = _make_provider(
        workflow_name="qwen-image-edit-2509-lightning4-tuned", fallback_workflow_name="qwen-image-edit-2509-lightning4-legacy"
    )
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ExternalImageGenError) as exc_info:
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert not isinstance(exc_info.value, ComfyUIWorkflowRejectedError)
    assert fake.call_count == 1


def test_missing_save_node_output_is_a_transport_error(tmp_path: Path) -> None:
    history = _json_response({"pid": {"outputs": {"99": {}}, "status": {"messages": []}}})
    fake = _Urlopen([_upload_response(), _json_response({"prompt_id": "pid"}), history])
    provider = _make_provider(timeout_s=0.3, poll_interval_s=0.0)
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ExternalImageGenError):
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )


# ---------- image dimension parsing ----------


def _make_minimal_png(w: int, h: int) -> bytes:
    """Create minimal valid PNG header with given dimensions."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return sig + b"\x00\x00\x00\x0d" + b"IHDR" + ihdr_data


def _make_minimal_jpeg(w: int, h: int) -> bytes:
    """Create minimal JPEG with SOF0 marker for given dimensions."""
    sof0 = b"\xff\xc0"
    # SOF0 segment: length(2) + precision(1) + height(2) + width(2) + components(1)
    seg = struct.pack(">HBHH", 8, 8, h, w) + b"\x01"
    return b"\xff\xd8" + sof0 + seg


def test_get_image_dimensions_png(tmp_path: Path) -> None:
    p = tmp_path / "test.png"
    p.write_bytes(_make_minimal_png(1600, 1200))
    assert _get_image_dimensions(p) == (1600, 1200)


def test_get_image_dimensions_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "test.jpg"
    p.write_bytes(_make_minimal_jpeg(640, 360))
    assert _get_image_dimensions(p) == (640, 360)


def test_get_image_dimensions_unknown_format(tmp_path: Path) -> None:
    p = tmp_path / "test.bmp"
    p.write_bytes(b"BM" + b"\x00" * 50)
    assert _get_image_dimensions(p) is None


# ---------- low-res input warning ----------


def test_low_res_input_logs_warning(tmp_path: Path) -> None:
    input_path = tmp_path / "small.jpg"
    input_path.write_bytes(_make_minimal_jpeg(640, 360))
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid-1"}), _history_response("pid-1"), _view_response()]
    )
    provider = _make_provider(min_input_pixels=500000)

    with patch("urllib.request.urlopen", new=fake), \
         patch("providers.ai_providers.comfyui.comfyui_image_generation_provider.logger") as mock_logger:
        result = provider.edit_image(
            input_image_paths=[str(input_path)],
            prompt="test prompt",
            output_image_path=str(tmp_path / "out.png"),
        )
    mock_logger.warning.assert_called_once()
    assert "640" in str(mock_logger.warning.call_args)
    assert result["input_dimensions"] == {"width": 640, "height": 360}


def test_high_res_input_no_warning(tmp_path: Path) -> None:
    input_path = tmp_path / "big.png"
    input_path.write_bytes(_make_minimal_png(1600, 1200))
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid-2"}), _history_response("pid-2"), _view_response()]
    )
    provider = _make_provider(min_input_pixels=500000)

    with patch("urllib.request.urlopen", new=fake), \
         patch("providers.ai_providers.comfyui.comfyui_image_generation_provider.logger") as mock_logger:
        provider.edit_image(
            input_image_paths=[str(input_path)],
            prompt="test prompt",
            output_image_path=str(tmp_path / "out.png"),
        )
    mock_logger.warning.assert_not_called()
