"""Tests for the ComfyUI workflow profile registry (load + strict validation)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.comfyui.workflow_profiles import (  # noqa: E402
    UnknownWorkflowProfileError,
    WorkflowProfileError,
    clear_cache,
    load_workflow_profiles,
)

_SHIPPED_PROFILES = (
    "qwen2509-original",
    "qwen2509-tuned",
    "qwen2509-tuned-multiframe",
)


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


def _minimal_profile() -> Dict[str, Any]:
    return {
        "description": "test profile",
        "model": "test-model",
        "workflow": "graph.json",
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
    profile: Dict[str, Any] | None = None,
    graph: Dict[str, Any] | None = None,
    default_profile: str | None = "p1",
    extra: Dict[str, Any] | None = None,
) -> Path:
    (tmp_path / "graph.json").write_text(
        json.dumps(graph if graph is not None else _minimal_graph()), encoding="utf-8"
    )
    doc: Dict[str, Any] = {
        "profiles": {"p1": profile if profile is not None else _minimal_profile()},
    }
    if default_profile is not None:
        doc["default_profile"] = default_profile
    if extra:
        doc.update(extra)
    path = tmp_path / "workflow_profiles.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _load_expecting_error(path: Path) -> str:
    clear_cache()
    with pytest.raises(WorkflowProfileError) as exc_info:
        load_workflow_profiles(path)
    return str(exc_info.value)


@pytest.fixture(autouse=True)
def _isolate_registry_cache():
    clear_cache()
    yield
    clear_cache()


# ---------- the shipped registry ----------


def test_shipped_registry_loads_with_expected_profiles() -> None:
    registry = load_workflow_profiles()
    assert registry.default_profile == "qwen2509-original"
    assert registry.names == _SHIPPED_PROFILES
    assert registry.default.name == "qwen2509-original"


def test_every_shipped_profile_validates_against_its_workflow() -> None:
    """Load-time validation already ran; assert the bindings really resolve."""
    registry = load_workflow_profiles()
    for name in registry.names:
        profile = registry.get(name)
        graph = profile.load_template()
        assert profile.prompt.input in graph[profile.prompt.node]["inputs"]
        assert profile.negative_prompt is not None
        assert profile.negative_prompt.input in graph[profile.negative_prompt.node]["inputs"]
        assert profile.seed is not None
        assert profile.seed.input in graph[profile.seed.node]["inputs"]
        assert profile.output.input in graph[profile.output.node]["inputs"]
        for slot in profile.images:
            assert slot.input in graph[slot.node]["inputs"]
            for target in slot.unlink:
                node_id, _, input_name = target.partition(".")
                assert graph[node_id]["inputs"][input_name] == [slot.node, 0]
        for node_id, overrides in profile.overrides.items():
            for input_name in overrides:
                assert input_name in graph[node_id]["inputs"]


def test_shipped_profile_shapes() -> None:
    registry = load_workflow_profiles()
    original = registry.get("qwen2509-original")
    assert original.max_images == 1
    assert original.timeout_s == 900
    assert original.workflow == "workflows/02_qwen_Image_edit_subgraphed_API.json"
    assert original.overrides["115:3"]["cfg"] == 3.5
    assert original.overrides["115:93"]["megapixels"] == 1.5

    tuned = registry.get("qwen2509-tuned")
    assert tuned.max_images == 1, "a 3-image render costs ~4x; tuned stays single-frame"
    assert tuned.timeout_s == 900
    assert tuned.overrides["115:3"]["cfg"] == 1
    assert tuned.overrides["115:3"]["steps"] == 4
    assert tuned.overrides["115:93"]["megapixels"] == 1.0

    multiframe = registry.get("qwen2509-tuned-multiframe")
    assert multiframe.max_images == 3
    assert multiframe.timeout_s == 1200
    assert multiframe.overrides["115:3"]["cfg"] == 1
    assert multiframe.overrides["115:93"]["megapixels"] == 1.0
    assert multiframe.images[1].unlink == ("115:111.image2", "115:110.image2")
    assert multiframe.images[2].unlink == ("115:111.image3", "115:110.image3")


def test_original_profile_keeps_the_untouched_workflow_file() -> None:
    """The rollback target must not share a cleaned graph with any other profile."""
    registry = load_workflow_profiles()
    original = registry.get("qwen2509-original")
    graph = original.load_template()
    # The orphan EmptySD3LatentImage / PreviewImage nodes are still there.
    assert "115:112" in graph
    assert "115:116" in graph
    for name in registry.names:
        if name == "qwen2509-original":
            continue
        assert registry.get(name).workflow != original.workflow


def test_shipped_required_models() -> None:
    registry = load_workflow_profiles()
    lightning = "Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors"
    assert lightning in registry.get("qwen2509-original").required_models
    assert lightning in registry.get("qwen2509-tuned").required_models
    assert lightning in registry.get("qwen2509-tuned-multiframe").required_models
    assert "qwen_image_edit_2509_fp8_e4m3fn.safetensors" in registry.get(
        "qwen2509-tuned"
    ).required_models


def test_shipped_model_labels() -> None:
    assert load_workflow_profiles().model_labels() == ("qwen-image-edit-2509",)


def test_get_unknown_profile_raises() -> None:
    registry = load_workflow_profiles()
    with pytest.raises(UnknownWorkflowProfileError) as exc_info:
        registry.get("nope")
    assert "nope" in str(exc_info.value)
    assert "qwen2509-original" in str(exc_info.value)


def test_get_empty_profile_name_raises() -> None:
    with pytest.raises(UnknownWorkflowProfileError):
        load_workflow_profiles().get("")


def test_get_or_default_falls_back() -> None:
    registry = load_workflow_profiles()
    assert registry.get_or_default("nope").name == registry.default_profile
    assert registry.get_or_default("qwen2509-tuned").name == "qwen2509-tuned"
    assert registry.has("qwen2509-tuned")
    assert not registry.has("nope")
    assert not registry.has(None)


# ---------- a valid synthetic registry ----------


def test_valid_registry_round_trip(tmp_path: Path) -> None:
    registry = load_workflow_profiles(_write_registry(tmp_path))
    profile = registry.get("p1")
    assert registry.default_profile == "p1"
    assert profile.max_images == 2
    assert profile.required_models == ("model.safetensors",)
    assert profile.output.input == "filename_prefix"
    assert profile.overrides["sampler"]["cfg"] == 2.5


def test_registry_is_cached_per_path(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    assert load_workflow_profiles(path) is load_workflow_profiles(path)


def test_optional_bindings_may_be_omitted(tmp_path: Path) -> None:
    profile = _minimal_profile()
    del profile["bindings"]["negative_prompt"]
    del profile["bindings"]["seed"]
    del profile["overrides"]
    loaded = load_workflow_profiles(_write_registry(tmp_path, profile=profile)).get("p1")
    assert loaded.negative_prompt is None
    assert loaded.seed is None
    assert dict(loaded.overrides) == {}


# ---------- validation failures ----------


def test_missing_registry_file_raises(tmp_path: Path) -> None:
    clear_cache()
    with pytest.raises(WorkflowProfileError) as exc_info:
        load_workflow_profiles(tmp_path / "nope.yaml")
    assert "not found" in str(exc_info.value)


def test_unparseable_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "workflow_profiles.yaml"
    path.write_text("profiles: [unclosed\n", encoding="utf-8")
    assert "failed to parse" in _load_expecting_error(path)


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "workflow_profiles.yaml"
    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    assert "top level must be a mapping" in _load_expecting_error(path)


def test_missing_profiles_section_raises(tmp_path: Path) -> None:
    path = tmp_path / "workflow_profiles.yaml"
    path.write_text(yaml.safe_dump({"default_profile": "p1"}), encoding="utf-8")
    assert "'profiles' must be a non-empty mapping" in _load_expecting_error(path)


def test_missing_default_profile_raises(tmp_path: Path) -> None:
    assert "'default_profile' is required" in _load_expecting_error(
        _write_registry(tmp_path, default_profile=None)
    )


def test_default_profile_must_be_registered(tmp_path: Path) -> None:
    message = _load_expecting_error(_write_registry(tmp_path, default_profile="ghost"))
    assert "default_profile 'ghost' is not a registered profile" in message


def test_missing_workflow_file_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["workflow"] = "does-not-exist.json"
    assert "workflow file does not exist" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_unparseable_workflow_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    (tmp_path / "graph.json").write_text("{not json", encoding="utf-8")
    assert "failed to parse workflow" in _load_expecting_error(path)


def test_empty_workflow_raises(tmp_path: Path) -> None:
    assert "non-empty API-format object" in _load_expecting_error(
        _write_registry(tmp_path, graph={})
    )


@pytest.mark.parametrize("field", ["description", "model", "workflow"])
def test_required_scalar_fields(tmp_path: Path, field: str) -> None:
    profile = _minimal_profile()
    del profile[field]
    assert f"{field} is required" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


@pytest.mark.parametrize("timeout", [None, 0, -5, "soon"])
def test_timeout_must_be_a_positive_number(tmp_path: Path, timeout: Any) -> None:
    profile = _minimal_profile()
    profile["timeout_s"] = timeout
    assert "timeout_s must be a positive number" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_bindings_must_be_a_mapping(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"] = "prompt"
    assert "'bindings' must be a mapping" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_unknown_prompt_node_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["prompt"]["node"] = "ghost"
    message = _load_expecting_error(_write_registry(tmp_path, profile=profile))
    assert "bindings.prompt" in message
    assert "node 'ghost' does not exist" in message


def test_unknown_prompt_input_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["prompt"]["input"] = "txt"
    message = _load_expecting_error(_write_registry(tmp_path, profile=profile))
    assert "has no input 'txt'" in message


def test_unknown_seed_binding_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["seed"] = {"node": "sampler", "input": "noise"}
    assert "bindings.seed" in _load_expecting_error(_write_registry(tmp_path, profile=profile))


def test_output_node_must_have_filename_prefix(tmp_path: Path) -> None:
    graph = _minimal_graph()
    del graph["save"]["inputs"]["filename_prefix"]
    message = _load_expecting_error(_write_registry(tmp_path, graph=graph))
    assert "bindings.output" in message
    assert "filename_prefix" in message


def test_output_binding_must_be_a_mapping(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["output"] = "save"
    assert "bindings.output must be a mapping" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_images_must_be_non_empty(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["images"] = []
    assert "slot 0 is required" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_unknown_image_slot_node_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["images"][1]["node"] = "ghost"
    message = _load_expecting_error(_write_registry(tmp_path, profile=profile))
    assert "bindings.images[1]" in message


def test_duplicate_image_slot_node_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["images"][1]["node"] = "load0"
    assert "bound to more than one slot" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_malformed_unlink_target_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["images"][1]["unlink"] = ["pos"]
    assert "must be '<node>.<input>'" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_unlink_target_input_must_exist(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["images"][1]["unlink"] = ["pos.image9"]
    assert "has no input 'image9'" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_unlink_target_must_link_to_its_slot_node(tmp_path: Path) -> None:
    profile = _minimal_profile()
    # image1 links to load0, which is slot 0 — not this slot's node.
    profile["bindings"]["images"][1]["unlink"] = ["pos.image1"]
    message = _load_expecting_error(_write_registry(tmp_path, profile=profile))
    assert "does not link to slot node 'load1'" in message


def test_unlink_must_be_a_list(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["bindings"]["images"][1]["unlink"] = "pos.image2"
    assert "unlink must be a list" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_unknown_override_node_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["overrides"] = {"ghost": {"cfg": 1}}
    message = _load_expecting_error(_write_registry(tmp_path, profile=profile))
    assert "overrides['ghost']" in message


def test_unknown_override_input_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["overrides"] = {"sampler": {"guidance": 1}}
    assert "has no input 'guidance'" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_empty_override_body_raises(tmp_path: Path) -> None:
    profile = _minimal_profile()
    profile["overrides"] = {"sampler": {}}
    assert "must be a non-empty mapping" in _load_expecting_error(
        _write_registry(tmp_path, profile=profile)
    )


def test_profile_body_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "workflow_profiles.yaml"
    (tmp_path / "graph.json").write_text(json.dumps(_minimal_graph()), encoding="utf-8")
    path.write_text(
        yaml.safe_dump({"default_profile": "p1", "profiles": {"p1": "nope"}}), encoding="utf-8"
    )
    assert "must be a mapping" in _load_expecting_error(path)


def test_workflow_profile_errors_are_value_errors() -> None:
    assert issubclass(WorkflowProfileError, ValueError)
    assert issubclass(UnknownWorkflowProfileError, WorkflowProfileError)
