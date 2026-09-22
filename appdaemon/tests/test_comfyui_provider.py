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
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.comfyui.comfyui_image_generation_provider import (  # noqa: E402
    ComfyUIImageGenerationConfig,
    ComfyUIImageGenerationProvider,
    ComfyUIWorkflowRejectedError,
    _REQUEST_TIMEOUT_S,
    _get_image_dimensions,
    _upload_lock,
    build_upload_name,
)

_PROVIDER_MODULE = "providers.ai_providers.comfyui.comfyui_image_generation_provider"
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


# The registry default renders on the Qwen-Image-2.1 graph, whose SaveImage
# node is "9". The 2509 graphs all save on "60", so a test that pins one of
# those must say so.
_QWEN21_3FRAME = "qwen-image-2.1-2609-25step-edit-3frame"  # the registry default
_QWEN21_3FRAME_GPU1 = "qwen-image-2.1-2609-25step-edit-3frame-gpu1"  # the bundle default
_QWEN21 = "qwen-image-2.1-2609-25step-edit"
_LEGACY = "qwen-image-edit-2509-lightning4-legacy"
_TUNED = "qwen-image-edit-2509-lightning4-tuned"
_TUNED_3FRAME = "qwen-image-edit-2509-lightning4-tuned-3frame"
_QWEN21_SAVE_NODE = "9"
_QWEN2509_SAVE_NODE = "60"


def _history_response(prompt_id: str, *, save_node: str = _QWEN21_SAVE_NODE) -> MagicMock:
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


_DEVICE_NOT_IN_LIST_BODY = {
    "error": {
        "type": "prompt_outputs_failed_validation",
        "message": "Prompt outputs failed validation",
    },
    "node_errors": {
        "20": {
            "class_type": "SelectModelDevice",
            "errors": [
                {
                    "type": "value_not_in_list",
                    "message": "Value not in list",
                    "extra_info": {"input_name": "device", "received_value": "gpu:1"},
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
        self.timeouts: List[Any] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001 - urlopen signature
        self.requests.append(req)
        self.timeouts.append(timeout)
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

    def timeout_for(self, url_fragment: str) -> Any:
        """The socket timeout of the single request whose URL contains a fragment."""
        matches = [
            timeout
            for req, timeout in zip(self.requests, self.timeouts)
            if url_fragment in req.full_url
        ]
        assert len(matches) == 1, f"expected exactly one {url_fragment!r} request, got {matches!r}"
        return matches[0]


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
    assert result["model"] == "qwen-image-2.1"
    assert result["workflow_name"] == _QWEN21_3FRAME
    assert result["workflow_name_requested"] == _QWEN21_3FRAME
    assert result["workflow_source"] == "registry_default"
    assert "workflow_fallback_reason" not in result
    assert result["uploaded_input_name"] == "garage-slot0.jpg"
    assert result["uploaded_input_names"] == ["garage-slot0.jpg"]
    assert result["timeout_s"] == 900.0
    assert result["workflow_model_released"] == "2026-09-20"
    assert "qwen_image_2.1_int8_convrot.safetensors" in result["required_models"]


def test_explicit_workflow_is_used(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response(), _upload_response(), _upload_response(),
         _json_response({"prompt_id": "pid-t"}),
         _history_response("pid-t", save_node=_QWEN2509_SAVE_NODE), _view_response()]
    )
    provider = _make_provider(
        workflow_name=_TUNED_3FRAME, upload_namespace="garage"
    )
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 3),
            prompt="p",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert result["workflow_name"] == _TUNED_3FRAME
    assert result["timeout_s"] == 1200.0
    assert len(result["uploaded_input_names"]) == 3


def test_tuned_workflow_uploads_only_the_first_frame(tmp_path: Path) -> None:
    """Single-frame by design: a 3-image render costs ~4x on the real server."""
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid", save_node=_QWEN2509_SAVE_NODE), _view_response()]
    )
    provider = _make_provider(workflow_name=_TUNED, upload_namespace="garage")
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
        [_upload_response(), _json_response({"prompt_id": "pid"}),
         _history_response("pid", save_node=_QWEN2509_SAVE_NODE), _view_response()]
    )
    provider = _make_provider(workflow_name=_TUNED_3FRAME)
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert result["timeout_s"] == 1200.0


# ---------- per-request socket timeouts ----------


def test_per_request_socket_timeouts_are_capped(tmp_path: Path) -> None:
    """The render budget is a deadline, not a socket timeout.

    Upload, ``POST /prompt`` and the output download each move a few MB at
    most. Handing them the 900-1200 s render budget would let one half-open
    connection hold the namespace's upload lock — and its worker thread — for
    the whole budget.
    """
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    provider = _make_provider()  # registry default: a 900 s render budget
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "out.png"),
        )

    assert result["timeout_s"] == 900.0, "the render budget itself is unchanged"
    assert _REQUEST_TIMEOUT_S < result["timeout_s"]
    assert fake.timeout_for("/upload/image") == _REQUEST_TIMEOUT_S
    assert fake.timeout_for("/prompt") == _REQUEST_TIMEOUT_S
    assert fake.timeout_for("/view?") == _REQUEST_TIMEOUT_S
    # Unchanged: /history polls were already capped, at 30 s.
    assert fake.timeout_for("/history/") == 30.0


def test_per_request_timeout_never_exceeds_a_short_budget(tmp_path: Path) -> None:
    """The cap is a ceiling, not a floor: a deliberately short budget still wins."""
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    provider = _make_provider(timeout_s=5.0)
    with patch("urllib.request.urlopen", new=fake):
        provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert fake.timeouts == [5.0, 5.0, 5.0, 5.0]


def test_history_deadline_still_uses_the_full_render_budget() -> None:
    """Capping the per-request timeouts must not shorten the render itself."""
    provider = _make_provider(poll_interval_s=0.0)
    polls: List[Any] = []
    clock = {"now": 1000.0}

    def _pending(req, timeout=None):  # noqa: ANN001 - urlopen signature
        polls.append(timeout)
        clock["now"] += 100.0  # 100 s of wall clock per poll
        return _json_response({"pid": {"status": {"messages": []}}})

    fake_time = SimpleNamespace(time=lambda: clock["now"], sleep=lambda _s: None)

    with patch("urllib.request.urlopen", new=_pending), patch(
        f"{_PROVIDER_MODULE}.time", new=fake_time
    ):
        with pytest.raises(ExternalImageGenError) as exc_info:
            provider._wait_for_history("pid", timeout_s=900.0)

    assert "Timed out" in str(exc_info.value)
    # 900 s of budget at 100 s per poll — the 60 s request cap does not bound it.
    assert len(polls) == 9
    assert polls == [30.0] * 9


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


def test_unknown_workflow_name_fails_at_construction() -> None:
    """A typo'd workflow name must stop the app, not render on the default.

    ``registry.build_image_provider`` checks this first so it can name where
    the typo was configured; this is the same guarantee for a caller that
    builds the config directly.
    """
    with pytest.raises(ValueError) as exc_info:
        _make_provider(workflow_name="ghost-workflow", fallback_workflow_name=_LEGACY)
    message = str(exc_info.value)
    assert "workflow_name" in message
    assert "ghost-workflow" in message
    assert "not a registered workflow" in message
    # The message names what the operator may have meant.
    assert _LEGACY in message


def test_empty_workflow_name_is_the_registry_default() -> None:
    """Empty means unset, not a bad name — it resolves to the registry default."""
    provider = _make_provider(workflow_name="   ")
    assert provider.workflow_name == load_workflow_registry().default_workflow


# ---------- capabilities ----------


@pytest.mark.parametrize(
    ("workflow_name", "expected_slots"),
    [
        (_QWEN21_3FRAME, 3),
        (_QWEN21_3FRAME_GPU1, 3),
        (_QWEN21, 1),
        (_LEGACY, 1),
        (_TUNED, 1),
        (_TUNED_3FRAME, 3),
    ],
)
def test_capabilities_report_the_workflow_slot_count(
    workflow_name: str, expected_slots: int
) -> None:
    """`max_input_images` is what the caller trims to, per shipped workflow."""
    provider = _make_provider(workflow_name=workflow_name)
    assert provider.capabilities.max_input_images == expected_slots
    assert provider.capabilities.max_input_images == (
        load_workflow_registry().get(workflow_name).max_images
    )


def test_capabilities_are_per_instance_not_per_class() -> None:
    """Two apps on different workflows must not share one slot count."""
    three_frame = _make_provider(workflow_name=_QWEN21_3FRAME)
    single = _make_provider(workflow_name=_QWEN21)
    assert three_frame.capabilities.max_input_images == 3
    assert single.capabilities.max_input_images == 1
    # The class default stays workflow-independent, so reading it is not a lie.
    assert ComfyUIImageGenerationProvider.capabilities.max_input_images is None


def test_capabilities_keep_the_flags_callers_already_read() -> None:
    """The manager reads `supports_image_to_image` off the same object."""
    provider = _make_provider()
    assert provider.capabilities.supports_image_to_image is True
    assert provider.capabilities.supports_text_to_image is False
    assert provider.capabilities.supports_inpaint is False
    assert provider.capabilities.notes


def test_default_workflow_capabilities_match_the_registry_default() -> None:
    provider = _make_provider()  # no workflow named -> registry default
    default = load_workflow_registry().get(load_workflow_registry().default_workflow)
    assert provider.capabilities.max_input_images == default.max_images == 3


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
    built = _build(_LEGACY, ["garage-slot0.jpg"])
    template = _template(_LEGACY)

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
        # The one difference that is NOT what production sent on disk: the
        # 2509 graphs used to get fp8_e4m3fn weights from the server-wide
        # --fp8_e4m3fn-unet flag, which is going away because it breaks the
        # int8 checkpoints Qwen-Image-2.1 uses. Setting it per workflow keeps
        # the rendered output identical once the flag is gone — so the
        # rollback guarantee holds against the RUNNING system, which is what
        # it is for, even though it no longer holds byte-for-byte against the
        # file.
        ("115:37", "weight_dtype"),
    }
    assert built["115:37"]["inputs"]["weight_dtype"] == "fp8_e4m3fn"
    assert built["78"]["inputs"]["image"] == "garage-slot0.jpg"
    assert built["115:111"]["inputs"]["prompt"] == "PROMPT"
    assert built["115:110"]["inputs"]["prompt"] == ""
    assert built["60"]["inputs"]["filename_prefix"] == "generated"
    assert built["115:3"]["inputs"]["cfg"] == 3.5
    assert built["115:3"]["inputs"]["steps"] == 4  # workflow default, not overridden
    assert built["115:93"]["inputs"]["megapixels"] == 1.5


def test_tuned_workflow_is_single_slot_on_a_cleaned_graph() -> None:
    built = _build(_TUNED, ["a-slot0.jpg"])
    template = _template(_TUNED)
    # The cleaned sibling graph drops the two orphan nodes the original keeps.
    assert "115:112" not in template
    assert "115:116" not in template
    assert built["78"]["inputs"]["image"] == "a-slot0.jpg"
    assert built["115:3"]["inputs"]["cfg"] == 1
    assert built["115:3"]["inputs"]["steps"] == 4
    assert built["115:93"]["inputs"]["megapixels"] == 1.0


def test_multiframe_workflow_binds_three_images() -> None:
    built = _build(_TUNED_3FRAME, ["a-slot0.jpg", "a-slot1.jpg", "a-slot2.jpg"])
    assert built["78"]["inputs"]["image"] == "a-slot0.jpg"
    assert built["120"]["inputs"]["image"] == "a-slot1.jpg"
    assert built["121"]["inputs"]["image"] == "a-slot2.jpg"
    assert built["115:111"]["inputs"]["image2"] == ["120", 0]
    assert built["115:111"]["inputs"]["image3"] == ["121", 0]
    assert built["115:3"]["inputs"]["cfg"] == 1
    assert built["115:93"]["inputs"]["megapixels"] == 1.0


def test_multiframe_workflow_prunes_unused_slots_with_two_images() -> None:
    built = _build(_TUNED_3FRAME, ["a-slot0.jpg", "a-slot1.jpg"])
    assert "120" in built
    assert "121" not in built
    assert built["115:111"]["inputs"]["image2"] == ["120", 0]
    assert "image3" not in built["115:111"]["inputs"]
    assert "image3" not in built["115:110"]["inputs"]


def test_multiframe_workflow_prunes_both_extra_slots_with_one_image() -> None:
    built = _build(_TUNED_3FRAME, ["a-slot0.jpg"])
    assert "120" not in built
    assert "121" not in built
    for node in ("115:111", "115:110"):
        assert "image2" not in built[node]["inputs"]
        assert "image3" not in built[node]["inputs"]
        assert built[node]["inputs"]["image1"] == ["115:93", 0]


def test_default_workflow_binds_three_frames() -> None:
    built = _build(_QWEN21_3FRAME, ["a-slot0.jpg", "a-slot1.jpg", "a-slot2.jpg"])
    assert built["1"]["inputs"]["image"] == "a-slot0.jpg"
    assert built["10"]["inputs"]["image"] == "a-slot1.jpg"
    assert built["11"]["inputs"]["image"] == "a-slot2.jpg"
    encoder = built["6"]["inputs"]
    assert encoder["images.image_1"] == ["1", 0]
    assert encoder["images.image_2"] == ["10", 0]
    assert encoder["images.image_3"] == ["11", 0]
    assert encoder["prompt"] == "PROMPT"
    assert encoder["negative_prompt"] == ""
    assert built["9"]["inputs"]["filename_prefix"] == "generated"
    # The template's own sampler settings survive: the entry has no overrides.
    assert built["7"]["inputs"]["steps"] == 25
    assert built["7"]["inputs"]["cfg"] == 1.0
    assert built["7"]["inputs"]["seed"] != 0


def test_default_workflow_prunes_the_third_slot_with_two_images() -> None:
    built = _build(_QWEN21_3FRAME, ["a-slot0.jpg", "a-slot1.jpg"])
    assert "10" in built
    assert "11" not in built
    encoder = built["6"]["inputs"]
    assert encoder["images.image_1"] == ["1", 0]
    assert encoder["images.image_2"] == ["10", 0]
    assert "images.image_3" not in encoder


def test_default_workflow_prunes_both_extra_slots_with_one_image() -> None:
    """A single-frame caller renders the same graph the 1-slot entry does."""
    built = _build(_QWEN21_3FRAME, ["a-slot0.jpg"])
    assert "10" not in built
    assert "11" not in built
    encoder = built["6"]["inputs"]
    assert encoder["images.image_1"] == ["1", 0]
    assert "images.image_2" not in encoder
    assert "images.image_3" not in encoder
    # Identical to what the single-frame entry builds, seed aside.
    single = _build(_QWEN21, ["a-slot0.jpg"])
    for node_id in single:
        assert built[node_id]["inputs"].keys() == single[node_id]["inputs"].keys()
    assert set(built) == set(single)


def test_gpu1_workflow_binds_three_frames_and_keeps_the_device_nodes() -> None:
    """The device-selection nodes sit outside the prunable slots entirely."""
    built = _build(_QWEN21_3FRAME_GPU1, ["a-slot0.jpg", "a-slot1.jpg", "a-slot2.jpg"])
    assert built["1"]["inputs"]["image"] == "a-slot0.jpg"
    assert built["10"]["inputs"]["image"] == "a-slot1.jpg"
    assert built["11"]["inputs"]["image"] == "a-slot2.jpg"
    encoder = built["6"]["inputs"]
    assert encoder["images.image_1"] == ["1", 0]
    assert encoder["images.image_2"] == ["10", 0]
    assert encoder["images.image_3"] == ["11", 0]
    assert encoder["prompt"] == "PROMPT"
    assert encoder["negative_prompt"] == ""
    assert built["9"]["inputs"]["filename_prefix"] == "generated"
    assert built["20"]["inputs"] == {"model": ["2", 0], "device": "gpu:1"}
    assert built["21"]["inputs"] == {"clip": ["3", 0], "device": "gpu:1"}
    assert built["22"]["inputs"] == {"vae": ["4", 0], "device": "gpu:1"}
    assert built["5"]["inputs"]["model"] == ["20", 0]
    assert built["8"]["inputs"]["vae"] == ["22", 0]
    assert built["7"]["inputs"]["steps"] == 25
    assert built["7"]["inputs"]["seed"] != 0


@pytest.mark.parametrize(
    "uploaded, kept, dropped",
    [
        (["a-slot0.jpg", "a-slot1.jpg"], ("10",), ("11",)),
        (["a-slot0.jpg"], (), ("10", "11")),
    ],
)
def test_gpu1_workflow_prunes_unused_slots(uploaded, kept, dropped) -> None:
    """Pruning must still work once the device nodes are in the graph.

    They consume the loaders, not the ``LoadImage`` slots, so a pruned slot
    leaves them untouched — and nothing may be left dangling.
    """
    built = _build(_QWEN21_3FRAME_GPU1, uploaded)
    for node_id in kept:
        assert node_id in built
    for node_id in dropped:
        assert node_id not in built
    encoder = built["6"]["inputs"]
    assert encoder["images.image_1"] == ["1", 0]
    for slot_node, input_name in (("10", "images.image_2"), ("11", "images.image_3")):
        if slot_node in built:
            assert encoder[input_name] == [slot_node, 0]
        else:
            assert input_name not in encoder
    # The device placement survives every prune.
    assert built["20"]["inputs"]["device"] == "gpu:1"
    assert built["21"]["inputs"]["device"] == "gpu:1"
    assert built["22"]["inputs"]["device"] == "gpu:1"
    assert built["5"]["inputs"]["model"] == ["20", 0]
    assert built["6"]["inputs"]["clip"] == ["21", 0]
    assert built["6"]["inputs"]["vae"] == ["22", 0]
    assert built["8"]["inputs"]["vae"] == ["22", 0]
    dangling = [
        f"{node_id}.{name}"
        for node_id, node in built.items()
        for name, value in node["inputs"].items()
        if isinstance(value, list) and value and str(value[0]) not in built
    ]
    assert dangling == []


def test_gpu1_workflow_with_one_frame_matches_the_plain_entry_apart_from_placement() -> None:
    """Same render, different card: only the three device nodes differ."""
    gpu1 = _build(_QWEN21_3FRAME_GPU1, ["a-slot0.jpg"])
    plain = _build(_QWEN21_3FRAME, ["a-slot0.jpg"])
    assert set(gpu1) - set(plain) == {"20", "21", "22"}
    assert set(plain) - set(gpu1) == set()
    rewired = {"5": {"model"}, "6": {"clip", "vae"}, "8": {"vae"}}
    for node_id, node in plain.items():
        for input_name, value in node["inputs"].items():
            if input_name in rewired.get(node_id, set()) or input_name == "seed":
                continue
            assert gpu1[node_id]["inputs"][input_name] == value


def test_default_workflow_unlinks_a_dotted_input_name() -> None:
    """``6.images.image_2`` names input ``images.image_2`` on node ``6``.

    The pruning parser splits on the FIRST dot. Splitting on the last would
    delete an input ``image_2`` that does not exist, leave ``images.image_2``
    pointing at the deleted LoadImage node, and ComfyUI would reject the whole
    graph at render time — after the upload, with nothing in the logs but a
    400.
    """
    template = _template(_QWEN21_3FRAME)
    assert "images.image_2" in template["6"]["inputs"], "the dot is in the INPUT name"
    built = _build(_QWEN21_3FRAME, ["a-slot0.jpg"])
    dangling = [
        f"{node_id}.{name}"
        for node_id, node in built.items()
        for name, value in node["inputs"].items()
        if isinstance(value, list) and value and str(value[0]) not in built
    ]
    assert dangling == []


def test_filename_prefix_falls_back_when_output_has_no_stem() -> None:
    built = _build(_LEGACY, ["a-slot0.jpg"], out="")
    assert built["60"]["inputs"]["filename_prefix"] == "detection-summary"


def test_extra_inputs_beyond_the_slots_are_reported_as_ignored(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    # The single-frame 2.1 entry, named explicitly: the registry default is the
    # three-slot one now, and this is the one-slot behaviour under test.
    provider = _make_provider(workflow_name=_QWEN21, upload_namespace="garage")
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


def test_four_inputs_fill_three_slots_on_the_default_workflow(tmp_path: Path) -> None:
    """The camera apps send 2-4 frames; the default takes the first three."""
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _upload_response("garage-slot1.jpg"),
         _upload_response("garage-slot2.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    provider = _make_provider(upload_namespace="garage")  # registry default
    paths = _inputs(tmp_path, 4)
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=paths, prompt="p", output_image_path=str(tmp_path / "o.png")
        )
    assert result["workflow_name"] == _QWEN21_3FRAME
    assert result["uploaded_input_names"] == [
        "garage-slot0.jpg", "garage-slot1.jpg", "garage-slot2.jpg"
    ]
    assert result["ignored_input_paths"] == paths[3:]
    assert result["timeout_s"] == 900.0


def test_two_inputs_render_on_the_default_workflow(tmp_path: Path) -> None:
    """Two frames upload two slots and prune the third — no rejection."""
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _upload_response("garage-slot1.jpg"),
         _json_response({"prompt_id": "pid"}), _history_response("pid"), _view_response()]
    )
    provider = _make_provider(upload_namespace="garage")
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 2),
            prompt="p",
            output_image_path=str(tmp_path / "o.png"),
        )
    assert result["uploaded_input_names"] == ["garage-slot0.jpg", "garage-slot1.jpg"]
    assert "ignored_input_paths" not in result
    sent = json.loads(
        [r for r in fake.requests if r.full_url.endswith("/prompt")][0].data.decode("utf-8")
    )["prompt"]
    assert "11" not in sent
    assert "images.image_3" not in sent["6"]["inputs"]
    assert sent["6"]["inputs"]["images.image_2"] == ["10", 0]


def test_a_caller_that_trimmed_to_the_slot_count_ignores_nothing(tmp_path: Path) -> None:
    """The normal case now: the caller read `max_input_images` and sent that many.

    The truncation above stays as a safety net — a caller that does not trim,
    or a one-shot fallback onto a narrower workflow, still cannot overrun the
    slots — but on a trimmed run it must be a no-op, so `ignored_input_paths`
    is absent rather than empty.
    """
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _upload_response("garage-slot1.jpg"),
         _upload_response("garage-slot2.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid"), _view_response()]
    )
    provider = _make_provider(upload_namespace="garage")  # registry default
    paths = _inputs(tmp_path, provider.capabilities.max_input_images)
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=paths, prompt="p", output_image_path=str(tmp_path / "o.png")
        )
    assert len(paths) == 3
    assert result["input_paths"] == paths
    assert len(result["uploaded_input_names"]) == 3
    assert "ignored_input_paths" not in result


def test_four_inputs_fill_three_slots_on_the_multiframe_workflow(tmp_path: Path) -> None:
    fake = _Urlopen(
        [_upload_response("garage-slot0.jpg"), _upload_response("garage-slot1.jpg"),
         _upload_response("garage-slot2.jpg"), _json_response({"prompt_id": "pid"}),
         _history_response("pid", save_node=_QWEN2509_SAVE_NODE), _view_response()]
    )
    provider = _make_provider(
        workflow_name=_TUNED_3FRAME, upload_namespace="garage"
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
         _json_response({"prompt_id": "pid"}),
         _history_response("pid", save_node=_QWEN2509_SAVE_NODE), _view_response()]
    )
    provider = _make_provider(
        workflow_name=_TUNED_3FRAME, upload_namespace="back yard pets"
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
            "outputs": {
                _QWEN21_SAVE_NODE: {
                    "images": [{"filename": "g.png", "subfolder": "", "type": "output"}]
                }
            },
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
    assert _QWEN21 in message
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
            _history_response("pid-fb", save_node=_QWEN2509_SAVE_NODE),
            _view_response(),
        ]
    )
    provider = _make_provider(
        workflow_name=_TUNED,
        fallback_workflow_name=_LEGACY,
        upload_namespace="garage",
    )
    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "o.png"),
        )

    assert result["workflow_name"] == _LEGACY
    assert result["workflow_name_requested"] == _TUNED
    assert _TUNED in result["workflow_fallback_reason"]
    assert "unet_name" in result["workflow_fallback_reason"]
    assert result["timeout_s"] == 900.0
    assert fake.call_count == 6


def test_a_missing_second_gpu_falls_back_to_the_gpu_agnostic_default(tmp_path: Path) -> None:
    """The card leaving the bus is exactly what the one-shot fallback is for.

    ``SelectModelDevice``'s ``device`` is a combo input, so a ``gpu:1`` that is
    no longer enumerated comes back from ``POST /prompt`` as HTTP 400
    ``value_not_in_list`` — a deterministic graph rejection. The bundle asks
    for the gpu1 entry, the registry default (which pins no card) is the
    fallback, and the retry renders on ``gpu:0`` instead of the zone going dark.
    """
    fake = _Urlopen(
        [
            _upload_response(),                          # attempt 1 (gpu1) upload
            _http_error(400, _DEVICE_NOT_IN_LIST_BODY),  # attempt 1 rejected
            _upload_response("garage-slot0.jpg"),        # attempt 2 (default) upload
            _json_response({"prompt_id": "pid-fb"}),
            _history_response("pid-fb"),                 # the 2.1 graph saves on "9"
            _view_response(),
        ]
    )
    registry = load_workflow_registry()
    provider = _make_provider(
        workflow_name=_QWEN21_3FRAME_GPU1,
        workflow_source="bundle",
        # Exactly what build_image_provider passes: the registry's own default.
        fallback_workflow_name=registry.default_workflow,
        upload_namespace="garage",
    )
    assert registry.default_workflow == _QWEN21_3FRAME
    assert provider.workflow_name != provider._config.fallback_workflow_name, (
        "a fallback equal to the requested workflow is never retried"
    )

    with patch("urllib.request.urlopen", new=fake):
        result = provider.edit_image(
            input_image_paths=_inputs(tmp_path, 1),
            prompt="p",
            output_image_path=str(tmp_path / "o.png"),
        )

    assert result["workflow_name"] == _QWEN21_3FRAME
    assert result["workflow_name_requested"] == _QWEN21_3FRAME_GPU1
    assert result["workflow_source"] == "bundle"
    reason = result["workflow_fallback_reason"]
    assert _QWEN21_3FRAME_GPU1 in reason
    assert "value_not_in_list" in reason
    assert "input_name='device'" in reason
    assert "received_value='gpu:1'" in reason
    assert result["model"] == "qwen-image-2.1", "the same model, on the other card"
    assert fake.call_count == 6


def test_fallback_is_not_retried_when_it_is_also_rejected(tmp_path: Path) -> None:
    fake = _Urlopen(
        [
            _upload_response(), _http_error(400, _VALUE_NOT_IN_LIST_BODY),
            _upload_response(), _http_error(400, _VALUE_NOT_IN_LIST_BODY),
        ]
    )
    provider = _make_provider(
        workflow_name=_TUNED, fallback_workflow_name=_LEGACY
    )
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ComfyUIWorkflowRejectedError):
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert fake.call_count == 4, "exactly one fallback attempt, then give up"


def _execution_error_history(prompt_id: str) -> MagicMock:
    return _json_response(
        {
            prompt_id: {
                "outputs": {},
                "status": {"messages": [["execution_error", {"exception_message": "CUDA OOM"}]]},
            }
        }
    )


def test_execution_error_in_history_does_not_fall_back(tmp_path: Path) -> None:
    """The graph validated and ran, so the failure is runtime, not the graph.

    A CUDA OOM on a rolled-back zone must not quietly re-render it on the
    current default workflow — which is what a fallback here would do.
    """
    fake = _Urlopen(
        [_upload_response(), _json_response({"prompt_id": "pid-x"}),
         _execution_error_history("pid-x")]
    )
    provider = _make_provider(
        workflow_name=_TUNED, fallback_workflow_name=_QWEN21
    )
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ExternalImageGenError) as exc_info:
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert not isinstance(exc_info.value, ComfyUIWorkflowRejectedError)
    assert "execution error" in str(exc_info.value)
    assert "CUDA OOM" in str(exc_info.value)
    assert fake.call_count == 3, "no second render on the fallback workflow"


def test_interrupted_prompt_fails_promptly_instead_of_polling_to_the_deadline(tmp_path: Path) -> None:
    """A cancelled render is terminal: ComfyUI records status_str="error" with an
    `execution_interrupted` message and no outputs. Polling that entry to a
    900-1200 s deadline would hold the zone's upload lock the whole time."""
    interrupted = _json_response(
        {
            "pid-i": {
                "outputs": {},
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [["execution_start", {}], ["execution_interrupted", {"node_id": "7"}]],
                },
            }
        }
    )
    fake = _Urlopen([_upload_response(), _json_response({"prompt_id": "pid-i"}), interrupted])
    provider = _make_provider(workflow_name=_QWEN21, fallback_workflow_name=_QWEN21)
    with patch("urllib.request.urlopen", new=fake):
        with pytest.raises(ExternalImageGenError) as exc_info:
            provider.edit_image(
                input_image_paths=_inputs(tmp_path, 1),
                prompt="p",
                output_image_path=str(tmp_path / "o.png"),
            )
    assert not isinstance(exc_info.value, ComfyUIWorkflowRejectedError)
    assert "execution_interrupted" in str(exc_info.value)
    assert fake.call_count == 3, "one history poll, not a poll loop to the deadline"


def test_non_400_http_error_is_not_a_workflow_rejection(tmp_path: Path) -> None:
    fake = _Urlopen([_upload_response(), _http_error(500, b"kaboom")])
    provider = _make_provider(
        workflow_name=_TUNED, fallback_workflow_name=_LEGACY
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
        workflow_name=_TUNED,
        fallback_workflow_name=_LEGACY,
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
        workflow_name=_TUNED, fallback_workflow_name=_LEGACY
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
