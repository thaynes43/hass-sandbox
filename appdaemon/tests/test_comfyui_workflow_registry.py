"""Tests for the ComfyUI workflow registry (load + strict validation)."""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.comfyui.workflow_registry import (  # noqa: E402
    UnknownWorkflowError,
    WorkflowRegistryError,
    clear_cache,
    load_workflow_registry,
)

_LEGACY = "qwen-image-edit-2509-lightning4-legacy"
_TUNED = "qwen-image-edit-2509-lightning4-tuned"
_TUNED_3FRAME = "qwen-image-edit-2509-lightning4-tuned-3frame"
_SHIPPED_WORKFLOWS = (_LEGACY, _TUNED, _TUNED_3FRAME)


# ---------- helpers ----------


def _minimal_graph() -> Dict[str, Any]:
    """A tiny but structurally honest two-slot API-format graph."""
    return {
        "save": {"inputs": {"filename_prefix": "ComfyUI", "images": ["decode", 0]}, "class_type": "SaveImage"},
        "load0": {"inputs": {"image": "input.png"}, "class_type": "LoadImage"},
        "load1": {"inputs": {"image": "input.png"}, "class_type": "LoadImage"},
        "pos": {
            "inputs": {"prompt": "", "image1": ["load0", 0], "image2": ["load1", 0]},
            "class_type": "TextEncodeQwenImageEditPlus",
        },
        "neg": {
            "inputs": {"prompt": "", "image1": ["load0", 0], "image2": ["load1", 0]},
            "class_type": "TextEncodeQwenImageEditPlus",
        },
        "sampler": {"inputs": {"seed": 1, "steps": 4, "cfg": 1}, "class_type": "KSampler"},
        "unet": {"inputs": {"unet_name": "model.safetensors"}, "class_type": "UNETLoader"},
        "decode": {"inputs": {"samples": ["sampler", 0]}, "class_type": "VAEDecode"},
    }


def _minimal_workflow() -> Dict[str, Any]:
    return {
        "description": "test workflow",
        "expect": "a test image, quickly",
        "model": "test-model",
        "model_released": datetime.date(2025, 9, 22),
        "added": datetime.date(2026, 9, 21),
        "graph": "graph.json",
        "timeout_s": 60,
        "bindings": {
            "prompt": {"node": "pos", "input": "prompt"},
            "negative_prompt": {"node": "neg", "input": "prompt"},
            "seed": {"node": "sampler", "input": "seed"},
            "output": {"node": "save"},
            "images": [
                {"node": "load0", "input": "image"},
                {"node": "load1", "input": "image", "unlink": ["pos.image2", "neg.image2"]},
            ],
        },
        "overrides": {"sampler": {"cfg": 2.5}},
    }


def _write_registry(
    tmp_path: Path,
    *,
    workflow: Dict[str, Any] | None = None,
    graph: Dict[str, Any] | None = None,
    default_workflow: str | None = "w1",
) -> Path:
    (tmp_path / "graph.json").write_text(
        json.dumps(graph if graph is not None else _minimal_graph()), encoding="utf-8"
    )
    doc: Dict[str, Any] = {
        "workflows": {"w1": workflow if workflow is not None else _minimal_workflow()},
    }
    if default_workflow is not None:
        doc["default_workflow"] = default_workflow
    path = tmp_path / "workflow_registry.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _load_expecting_error(path: Path) -> str:
    clear_cache()
    with pytest.raises(WorkflowRegistryError) as exc_info:
        load_workflow_registry(path)
    return str(exc_info.value)


@pytest.fixture(autouse=True)
def _isolate_registry_cache():
    clear_cache()
    yield
    clear_cache()


# ---------- the shipped registry ----------


def test_shipped_registry_loads_with_expected_workflows() -> None:
    registry = load_workflow_registry()
    assert registry.default_workflow == _LEGACY
    assert registry.names == _SHIPPED_WORKFLOWS
    assert registry.default.name == _LEGACY


def test_every_shipped_workflow_validates_against_its_graph() -> None:
    """Load-time validation already ran; assert the bindings really resolve."""
    registry = load_workflow_registry()
    for name in registry.names:
        workflow = registry.get(name)
        graph = workflow.load_graph()
        assert workflow.prompt.input in graph[workflow.prompt.node]["inputs"]
        assert workflow.negative_prompt is not None
        assert workflow.negative_prompt.input in graph[workflow.negative_prompt.node]["inputs"]
        assert workflow.seed is not None
        assert workflow.seed.input in graph[workflow.seed.node]["inputs"]
        assert workflow.output.input in graph[workflow.output.node]["inputs"]
        for slot in workflow.images:
            assert slot.input in graph[slot.node]["inputs"]
            for target in slot.unlink:
                node_id, _, input_name = target.partition(".")
                assert graph[node_id]["inputs"][input_name] == [slot.node, 0]
        for node_id, overrides in workflow.overrides.items():
            for input_name in overrides:
                assert input_name in graph[node_id]["inputs"]


def test_every_shipped_workflow_carries_its_operator_metadata() -> None:
    """The metadata is what tells two entries apart in a diff months later."""
    registry = load_workflow_registry()
    for name in registry.names:
        workflow = registry.get(name)
        assert workflow.model_released == datetime.date(2025, 9, 22)
        assert workflow.added == datetime.date(2026, 9, 21)
        assert workflow.description.strip()
        assert workflow.expect.strip()
        # The name carries the model's release yymm, per the documented
        # convention <model>-<model release yymm>-<sampling>-<variant>.
        assert workflow.model_released.strftime("%y%m") in name


def test_shipped_expect_text_describes_output_and_speed() -> None:
    registry = load_workflow_registry()
    assert "over-saturated" in registry.get(_LEGACY).expect
    assert "40-50 s" in registry.get(_LEGACY).expect
    assert "clean colour" in registry.get(_TUNED).expect
    assert "40-50 s" in registry.get(_TUNED).expect
    assert "three camera frames" in registry.get(_TUNED_3FRAME).expect
    assert "slower" in registry.get(_TUNED_3FRAME).expect


def test_shipped_workflow_shapes() -> None:
    registry = load_workflow_registry()
    legacy = registry.get(_LEGACY)
    assert legacy.max_images == 1
    assert legacy.timeout_s == 900
    assert legacy.graph == "workflows/02_qwen_Image_edit_subgraphed_API.json"
    assert legacy.overrides["115:3"]["cfg"] == 3.5
    assert legacy.overrides["115:93"]["megapixels"] == 1.5

    tuned = registry.get(_TUNED)
    assert tuned.max_images == 1, "a 3-image render costs several times more"
    assert tuned.timeout_s == 900
    assert tuned.overrides["115:3"]["cfg"] == 1
    assert tuned.overrides["115:3"]["steps"] == 4
    assert tuned.overrides["115:93"]["megapixels"] == 1.0

    three = registry.get(_TUNED_3FRAME)
    assert three.max_images == 3
    assert three.timeout_s == 1200
    assert three.overrides["115:3"]["cfg"] == 1
    assert three.overrides["115:93"]["megapixels"] == 1.0
    assert three.images[1].unlink == ("115:111.image2", "115:110.image2")
    assert three.images[2].unlink == ("115:111.image3", "115:110.image3")


def test_legacy_workflow_keeps_the_untouched_graph_file() -> None:
    """The rollback target must not share a cleaned graph with any other entry."""
    registry = load_workflow_registry()
    legacy = registry.get(_LEGACY)
    graph = legacy.load_graph()
    # The orphan EmptySD3LatentImage / PreviewImage nodes are still there.
    assert "115:112" in graph
    assert "115:116" in graph
    for name in registry.names:
        if name == _LEGACY:
            continue
        assert registry.get(name).graph != legacy.graph


def test_shipped_required_models() -> None:
    registry = load_workflow_registry()
    lightning = "Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors"
    for name in _SHIPPED_WORKFLOWS:
        assert lightning in registry.get(name).required_models
        assert "qwen_image_edit_2509_fp8_e4m3fn.safetensors" in registry.get(name).required_models


def test_shipped_model_labels() -> None:
    assert load_workflow_registry().model_labels() == ("qwen-image-edit-2509",)


def test_get_unknown_workflow_raises() -> None:
    registry = load_workflow_registry()
    with pytest.raises(UnknownWorkflowError) as exc_info:
        registry.get("nope")
    assert "nope" in str(exc_info.value)
    assert _LEGACY in str(exc_info.value)


def test_get_empty_workflow_name_raises() -> None:
    with pytest.raises(UnknownWorkflowError):
        load_workflow_registry().get("")


def test_get_or_default_falls_back() -> None:
    registry = load_workflow_registry()
    assert registry.get_or_default("nope").name == registry.default_workflow
    assert registry.get_or_default(_TUNED).name == _TUNED
    assert registry.has(_TUNED)
    assert not registry.has("nope")
    assert not registry.has(None)


# ---------- a valid synthetic registry ----------


def test_valid_registry_round_trip(tmp_path: Path) -> None:
    registry = load_workflow_registry(_write_registry(tmp_path))
    workflow = registry.get("w1")
    assert registry.default_workflow == "w1"
    assert workflow.max_images == 2
    assert workflow.required_models == ("model.safetensors",)
    assert workflow.output.input == "filename_prefix"
    assert workflow.overrides["sampler"]["cfg"] == 2.5
    assert workflow.model_released == datetime.date(2025, 9, 22)
    assert workflow.added == datetime.date(2026, 9, 21)


def test_registry_is_cached_per_path(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    assert load_workflow_registry(path) is load_workflow_registry(path)


def test_optional_bindings_may_be_omitted(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    del workflow["bindings"]["negative_prompt"]
    del workflow["bindings"]["seed"]
    del workflow["overrides"]
    loaded = load_workflow_registry(_write_registry(tmp_path, workflow=workflow)).get("w1")
    assert loaded.negative_prompt is None
    assert loaded.seed is None
    assert dict(loaded.overrides) == {}


# ---------- metadata validation ----------


@pytest.mark.parametrize("field", ["description", "expect", "model", "graph"])
def test_required_text_fields(tmp_path: Path, field: str) -> None:
    workflow = _minimal_workflow()
    del workflow[field]
    assert f"{field} is required" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


@pytest.mark.parametrize("field", ["model_released", "added"])
def test_required_date_fields(tmp_path: Path, field: str) -> None:
    workflow = _minimal_workflow()
    del workflow[field]
    message = _load_expecting_error(_write_registry(tmp_path, workflow=workflow))
    assert f"{field} is required" in message
    assert "YYYY-MM-DD" in message


@pytest.mark.parametrize("field", ["model_released", "added"])
@pytest.mark.parametrize("value", ["yesterday", "2025-13-99", 20250922, "", None])
def test_invalid_date_fields(tmp_path: Path, field: str, value: Any) -> None:
    workflow = _minimal_workflow()
    workflow[field] = value
    message = _load_expecting_error(_write_registry(tmp_path, workflow=workflow))
    assert field in message
    assert "YYYY-MM-DD" in message


@pytest.mark.parametrize("field", ["model_released", "added"])
def test_dates_may_be_quoted_iso_strings(tmp_path: Path, field: str) -> None:
    """PyYAML gives a date for an unquoted value and a str for a quoted one."""
    workflow = _minimal_workflow()
    workflow[field] = "2024-01-02"
    loaded = load_workflow_registry(_write_registry(tmp_path, workflow=workflow)).get("w1")
    assert getattr(loaded, field) == datetime.date(2024, 1, 2)


# ---------- structural validation failures ----------


def test_missing_registry_file_raises(tmp_path: Path) -> None:
    clear_cache()
    with pytest.raises(WorkflowRegistryError) as exc_info:
        load_workflow_registry(tmp_path / "nope.yaml")
    assert "not found" in str(exc_info.value)


def test_unparseable_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "workflow_registry.yaml"
    path.write_text("workflows: [unclosed\n", encoding="utf-8")
    assert "failed to parse" in _load_expecting_error(path)


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "workflow_registry.yaml"
    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    assert "top level must be a mapping" in _load_expecting_error(path)


def test_missing_workflows_section_raises(tmp_path: Path) -> None:
    path = tmp_path / "workflow_registry.yaml"
    path.write_text(yaml.safe_dump({"default_workflow": "w1"}), encoding="utf-8")
    assert "'workflows' must be a non-empty mapping" in _load_expecting_error(path)


def test_missing_default_workflow_raises(tmp_path: Path) -> None:
    assert "'default_workflow' is required" in _load_expecting_error(
        _write_registry(tmp_path, default_workflow=None)
    )


def test_default_workflow_must_be_registered(tmp_path: Path) -> None:
    message = _load_expecting_error(_write_registry(tmp_path, default_workflow="ghost"))
    assert "default_workflow 'ghost' is not a registered workflow" in message


def test_missing_graph_file_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["graph"] = "does-not-exist.json"
    assert "graph file does not exist" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_unparseable_graph_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    (tmp_path / "graph.json").write_text("{not json", encoding="utf-8")
    assert "failed to parse graph" in _load_expecting_error(path)


def test_empty_graph_raises(tmp_path: Path) -> None:
    assert "non-empty API-format object" in _load_expecting_error(
        _write_registry(tmp_path, graph={})
    )


@pytest.mark.parametrize("timeout", [None, 0, -5, "soon"])
def test_timeout_must_be_a_positive_number(tmp_path: Path, timeout: Any) -> None:
    workflow = _minimal_workflow()
    workflow["timeout_s"] = timeout
    assert "timeout_s must be a positive number" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_bindings_must_be_a_mapping(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"] = "prompt"
    assert "'bindings' must be a mapping" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_unknown_prompt_node_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["prompt"]["node"] = "ghost"
    message = _load_expecting_error(_write_registry(tmp_path, workflow=workflow))
    assert "bindings.prompt" in message
    assert "node 'ghost' does not exist" in message


def test_unknown_prompt_input_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["prompt"]["input"] = "txt"
    message = _load_expecting_error(_write_registry(tmp_path, workflow=workflow))
    assert "has no input 'txt'" in message


def test_unknown_seed_binding_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["seed"] = {"node": "sampler", "input": "noise"}
    assert "bindings.seed" in _load_expecting_error(_write_registry(tmp_path, workflow=workflow))


def test_output_node_must_have_filename_prefix(tmp_path: Path) -> None:
    graph = _minimal_graph()
    del graph["save"]["inputs"]["filename_prefix"]
    message = _load_expecting_error(_write_registry(tmp_path, graph=graph))
    assert "bindings.output" in message
    assert "filename_prefix" in message


def test_output_binding_must_be_a_mapping(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["output"] = "save"
    assert "bindings.output must be a mapping" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_images_must_be_non_empty(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["images"] = []
    assert "slot 0 is required" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_unknown_image_slot_node_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["images"][1]["node"] = "ghost"
    message = _load_expecting_error(_write_registry(tmp_path, workflow=workflow))
    assert "bindings.images[1]" in message


def test_duplicate_image_slot_node_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["images"][1]["node"] = "load0"
    assert "bound to more than one slot" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_malformed_unlink_target_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["images"][1]["unlink"] = ["pos"]
    assert "must be '<node>.<input>'" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_unlink_target_input_must_exist(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["images"][1]["unlink"] = ["pos.image9"]
    assert "has no input 'image9'" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_unlink_target_must_link_to_its_slot_node(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    # image1 links to load0, which is slot 0 — not this slot's node.
    workflow["bindings"]["images"][1]["unlink"] = ["pos.image1"]
    message = _load_expecting_error(_write_registry(tmp_path, workflow=workflow))
    assert "does not link to slot node 'load1'" in message


def test_unlink_must_be_a_list(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["bindings"]["images"][1]["unlink"] = "pos.image2"
    assert "unlink must be a list" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_unknown_override_node_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["overrides"] = {"ghost": {"cfg": 1}}
    message = _load_expecting_error(_write_registry(tmp_path, workflow=workflow))
    assert "overrides['ghost']" in message


def test_unknown_override_input_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["overrides"] = {"sampler": {"guidance": 1}}
    assert "has no input 'guidance'" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_empty_override_body_raises(tmp_path: Path) -> None:
    workflow = _minimal_workflow()
    workflow["overrides"] = {"sampler": {}}
    assert "must be a non-empty mapping" in _load_expecting_error(
        _write_registry(tmp_path, workflow=workflow)
    )


def test_workflow_body_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "workflow_registry.yaml"
    (tmp_path / "graph.json").write_text(json.dumps(_minimal_graph()), encoding="utf-8")
    path.write_text(
        yaml.safe_dump({"default_workflow": "w1", "workflows": {"w1": "nope"}}), encoding="utf-8"
    )
    assert "must be a mapping" in _load_expecting_error(path)


def test_registry_errors_are_value_errors() -> None:
    assert issubclass(WorkflowRegistryError, ValueError)
    assert issubclass(UnknownWorkflowError, WorkflowRegistryError)
