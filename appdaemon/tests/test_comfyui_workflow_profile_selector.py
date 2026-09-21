"""Tests for the Home Assistant ComfyUI workflow-profile selector."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.comfyui.workflow_profile_selector import (  # noqa: E402
    ACTIVE_ENTITY_ID,
    ACTIVE_HELPER_NAME,
    SOURCE_HA_ACTIVE,
    SOURCE_HA_TRIAL,
    SOURCE_YAML_DEFAULT,
    TRIAL_ENTITY_ID,
    TRIAL_HELPER_NAME,
    WorkflowProfileSelector,
    reset_global_provisioning_state,
    trial_helper_name,
)
from providers.ai_providers.comfyui.workflow_profiles import (  # noqa: E402
    clear_cache,
    load_workflow_profiles,
)
from providers.ha_provisioner.provisioner import HAProvisioner  # noqa: E402

_DEFAULT = "qwen2509-original"
_PROFILES = list(load_workflow_profiles().names)


class _FakeApp:
    """Minimal stand-in for an AppDaemon app (get_state / call_service / log)."""

    def __init__(self, states: Optional[Dict[str, Any]] = None) -> None:
        self.states: Dict[str, Any] = dict(states or {})
        self.service_calls: List[tuple] = []
        self.logs: List[tuple] = []

    def get_state(self, entity_id: str, attribute: Optional[str] = None) -> Any:
        value = self.states.get(entity_id)
        if attribute == "all":
            return value
        if isinstance(value, dict):
            return value.get("state")
        return value

    def call_service(self, service: str, **kwargs: Any) -> None:
        self.service_calls.append((service, kwargs))

    def log(self, message: str, level: str = "INFO") -> None:
        self.logs.append((level, message))

    # test helpers
    def warnings(self) -> List[str]:
        return [m for lvl, m in self.logs if lvl == "WARNING"]

    def services(self, name: str) -> List[Dict[str, Any]]:
        return [kwargs for svc, kwargs in self.service_calls if svc == name]


def _select_state(state: str, options: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"state": state, "attributes": {"options": list(options if options is not None else _PROFILES)}}


@pytest.fixture(autouse=True)
def _reset_globals():
    reset_global_provisioning_state()
    yield
    reset_global_provisioning_state()


# ---------- entity ids ----------


def test_entity_ids_match_what_ha_derives_from_the_helper_names() -> None:
    """The ids are a public contract; derive them the way HA does and compare."""
    assert HAProvisioner._helper_slug("input_select", ACTIVE_HELPER_NAME) == "comfyui_active_workflow"
    assert HAProvisioner._helper_slug("input_select", TRIAL_HELPER_NAME) == "comfyui_trial_workflow"
    assert ACTIVE_ENTITY_ID == "input_select.comfyui_active_workflow"
    assert TRIAL_ENTITY_ID == "input_select.comfyui_trial_workflow"


@pytest.mark.parametrize(
    ("display", "expected"),
    [
        ("Garage", "input_boolean.garage_detection_summary_trial_workflow"),
        ("Back Deck Pets", "input_boolean.back_deck_pets_detection_summary_trial_workflow"),
        ("Front Door", "input_boolean.front_door_detection_summary_trial_workflow"),
    ],
)
def test_zone_trial_boolean_entity_ids(display: str, expected: str) -> None:
    name = trial_helper_name(display)
    assert name == f"{display} Detection Summary Trial Workflow"
    assert f"input_boolean.{HAProvisioner._helper_slug('input_boolean', name)}" == expected


# ---------- provisioning ----------


def _provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=True)
    prov._helper_slug = MagicMock(side_effect=HAProvisioner._helper_slug)
    return prov


def test_provision_creates_both_selects_and_the_zone_boolean() -> None:
    app = _FakeApp()
    prov = _provisioner()
    selector = WorkflowProfileSelector(app)

    trial_entity = asyncio.run(selector.provision(prov, zone_display_name="Garage"))

    assert trial_entity == "input_boolean.garage_detection_summary_trial_workflow"
    calls = {c.args[1]: c for c in prov.ensure_helper.call_args_list}
    assert set(calls) == {ACTIVE_HELPER_NAME, TRIAL_HELPER_NAME, "Garage Detection Summary Trial Workflow"}
    for helper_name in (ACTIVE_HELPER_NAME, TRIAL_HELPER_NAME):
        call = calls[helper_name]
        assert call.args[0] == "input_select"
        assert call.kwargs["options"] == _PROFILES
        assert call.kwargs["initial"] == _DEFAULT
    zone_call = calls["Garage Detection Summary Trial Workflow"]
    assert zone_call.args[0] == "input_boolean"
    assert zone_call.kwargs["initial"] is False


def test_global_selects_are_provisioned_once_across_zones() -> None:
    """All 7 zones provision concurrently; ensure_helper is check-then-create."""
    prov = _provisioner()

    async def _all_zones() -> List[str]:
        selectors = [WorkflowProfileSelector(_FakeApp()) for _ in range(7)]
        return await asyncio.gather(
            *(
                s.provision(prov, zone_display_name=f"Zone {i}")
                for i, s in enumerate(selectors)
            )
        )

    entity_ids = asyncio.run(_all_zones())

    assert len(set(entity_ids)) == 7
    select_names = [
        c.args[1] for c in prov.ensure_helper.call_args_list if c.args[0] == "input_select"
    ]
    assert select_names == [ACTIVE_HELPER_NAME, TRIAL_HELPER_NAME]
    boolean_calls = [c for c in prov.ensure_helper.call_args_list if c.args[0] == "input_boolean"]
    assert len(boolean_calls) == 7


def test_provision_survives_a_failing_provisioner() -> None:
    app = _FakeApp()
    prov = _provisioner()
    prov.ensure_helper = AsyncMock(side_effect=RuntimeError("HA is down"))
    selector = WorkflowProfileSelector(app)

    trial_entity = asyncio.run(selector.provision(prov, zone_display_name="Garage"))

    assert trial_entity == "input_boolean.garage_detection_summary_trial_workflow"
    assert [lvl for lvl, _ in app.logs].count("ERROR") == 3


# ---------- option reconciliation ----------


def test_reconcile_sets_options_when_they_differ() -> None:
    app = _FakeApp(
        {
            ACTIVE_ENTITY_ID: _select_state(_DEFAULT, ["old-profile", _DEFAULT]),
            TRIAL_ENTITY_ID: _select_state(_DEFAULT, ["old-profile", _DEFAULT]),
        }
    )
    WorkflowProfileSelector(app).reconcile_options()

    set_options = app.services("input_select/set_options")
    assert len(set_options) == 2
    assert all(c["options"] == _PROFILES for c in set_options)
    assert app.services("input_select/select_option") == []


def test_reconcile_is_a_no_op_when_options_already_match() -> None:
    app = _FakeApp(
        {
            ACTIVE_ENTITY_ID: _select_state("qwen2509-tuned"),
            TRIAL_ENTITY_ID: _select_state(_DEFAULT),
        }
    )
    WorkflowProfileSelector(app).reconcile_options()
    assert app.service_calls == []


def test_reconcile_preserves_a_still_valid_selection() -> None:
    app = _FakeApp(
        {
            ACTIVE_ENTITY_ID: _select_state("qwen2509-tuned-multiframe", ["qwen2509-tuned-multiframe"]),
            TRIAL_ENTITY_ID: _select_state("qwen2509-tuned-multiframe", ["qwen2509-tuned-multiframe"]),
        }
    )
    WorkflowProfileSelector(app).reconcile_options()
    assert len(app.services("input_select/set_options")) == 2
    assert app.services("input_select/select_option") == []


def test_reconcile_reselects_the_default_when_the_selection_is_gone() -> None:
    app = _FakeApp(
        {
            ACTIVE_ENTITY_ID: _select_state("retired-profile", ["retired-profile"]),
            TRIAL_ENTITY_ID: _select_state("unknown"),
        }
    )
    WorkflowProfileSelector(app).reconcile_options()

    selected = app.services("input_select/select_option")
    assert len(selected) == 2
    assert all(c["option"] == _DEFAULT for c in selected)


def test_reconcile_skips_entities_that_do_not_exist_yet() -> None:
    app = _FakeApp()
    WorkflowProfileSelector(app).reconcile_options()
    assert app.service_calls == []
    assert app.warnings() == []


def test_reconcile_tolerates_a_raising_app() -> None:
    app = _FakeApp({ACTIVE_ENTITY_ID: _select_state(_DEFAULT, ["x"])})
    app.call_service = MagicMock(side_effect=RuntimeError("boom"))
    WorkflowProfileSelector(app).reconcile_options()
    assert any("failed to reconcile options" in w for w in app.warnings())


# ---------- selection ----------


_TRIAL_BOOL = "input_boolean.garage_detection_summary_trial_workflow"


def test_trial_off_uses_the_active_select() -> None:
    app = _FakeApp(
        {
            _TRIAL_BOOL: "off",
            ACTIVE_ENTITY_ID: "qwen2509-tuned",
            TRIAL_ENTITY_ID: "qwen2509-tuned-multiframe",
        }
    )
    selection = WorkflowProfileSelector(app).select(_TRIAL_BOOL)
    assert selection.profile == "qwen2509-tuned"
    assert selection.source == SOURCE_HA_ACTIVE
    assert selection.rejected_state is None


def test_trial_on_uses_the_trial_select() -> None:
    app = _FakeApp(
        {
            _TRIAL_BOOL: "on",
            ACTIVE_ENTITY_ID: "qwen2509-tuned",
            TRIAL_ENTITY_ID: "qwen2509-tuned-multiframe",
        }
    )
    selection = WorkflowProfileSelector(app).select(_TRIAL_BOOL)
    assert selection.profile == "qwen2509-tuned-multiframe"
    assert selection.source == SOURCE_HA_TRIAL


def test_no_trial_entity_uses_the_active_select() -> None:
    app = _FakeApp({ACTIVE_ENTITY_ID: "qwen2509-tuned-multiframe"})
    selection = WorkflowProfileSelector(app).select(None)
    assert selection.profile == "qwen2509-tuned-multiframe"
    assert selection.source == SOURCE_HA_ACTIVE


@pytest.mark.parametrize("state", ["", "unknown", "unavailable", "retired-profile", None])
def test_unusable_state_falls_back_to_the_yaml_default(state: Any) -> None:
    app = _FakeApp({_TRIAL_BOOL: "off", ACTIVE_ENTITY_ID: state})
    selection = WorkflowProfileSelector(app).select(_TRIAL_BOOL)
    assert selection.profile == _DEFAULT
    assert selection.source == SOURCE_YAML_DEFAULT
    assert len(app.warnings()) == 1


def test_bad_state_warns_once_per_distinct_value() -> None:
    app = _FakeApp({_TRIAL_BOOL: "off", ACTIVE_ENTITY_ID: "retired-profile"})
    selector = WorkflowProfileSelector(app)

    for _ in range(5):
        assert selector.select(_TRIAL_BOOL).source == SOURCE_YAML_DEFAULT
    assert len(app.warnings()) == 1

    app.states[ACTIVE_ENTITY_ID] = "another-bad-one"
    for _ in range(3):
        selector.select(_TRIAL_BOOL)
    assert len(app.warnings()) == 2
    assert "retired-profile" in app.warnings()[0]
    assert "another-bad-one" in app.warnings()[1]

    # A good value after a bad one must not warn again.
    app.states[ACTIVE_ENTITY_ID] = "qwen2509-tuned"
    assert selector.select(_TRIAL_BOOL).profile == "qwen2509-tuned"
    assert len(app.warnings()) == 2


def test_a_raising_get_state_degrades_to_the_default() -> None:
    app = _FakeApp()
    app.get_state = MagicMock(side_effect=RuntimeError("no HA"))
    selector = WorkflowProfileSelector(app)
    selection = selector.select(_TRIAL_BOOL)
    assert selection.profile == _DEFAULT
    assert selection.source == SOURCE_YAML_DEFAULT
    # Repeated failures must not spam the log.
    for _ in range(5):
        selector.select(_TRIAL_BOOL)
    assert len(app.warnings()) <= 3


def test_selector_exposes_the_registry() -> None:
    selector = WorkflowProfileSelector(_FakeApp())
    assert selector.profile_names == _PROFILES
    assert selector.default_profile == _DEFAULT


# ---------- profile names are matched case-sensitively ----------


def _mixed_case_registry(tmp_path: Path):
    """A synthetic registry whose profile names carry capitals."""
    (tmp_path / "graph.json").write_text(
        json.dumps(
            {
                "save": {"inputs": {"filename_prefix": "ComfyUI"}, "class_type": "SaveImage"},
                "load0": {"inputs": {"image": "input.png"}, "class_type": "LoadImage"},
                "pos": {"inputs": {"prompt": ""}, "class_type": "TextEncodeQwenImageEditPlus"},
            }
        ),
        encoding="utf-8",
    )
    profile = {
        "description": "mixed case",
        "model": "test-model",
        "workflow": "graph.json",
        "timeout_s": 60,
        "bindings": {
            "prompt": {"node": "pos", "input": "prompt"},
            "output": {"node": "save"},
            "images": [{"node": "load0", "input": "image"}],
        },
    }
    path = tmp_path / "workflow_profiles.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "default_profile": "Qwen2509-Original",
                "profiles": {"Qwen2509-Original": profile, "Qwen2509-Tuned": dict(profile)},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    clear_cache()
    try:
        return load_workflow_profiles(path)
    finally:
        clear_cache()


def test_a_mixed_case_profile_name_is_matched_exactly(tmp_path: Path) -> None:
    """Lower-casing the state would make a capitalised profile unselectable."""
    registry = _mixed_case_registry(tmp_path)
    app = _FakeApp({_TRIAL_BOOL: "off", ACTIVE_ENTITY_ID: "Qwen2509-Tuned"})
    selection = WorkflowProfileSelector(app, registry=registry).select(_TRIAL_BOOL)
    assert selection.profile == "Qwen2509-Tuned"
    assert selection.source == SOURCE_HA_ACTIVE
    assert app.warnings() == []


def test_a_wrong_case_profile_name_is_not_silently_accepted(tmp_path: Path) -> None:
    registry = _mixed_case_registry(tmp_path)
    app = _FakeApp({_TRIAL_BOOL: "off", ACTIVE_ENTITY_ID: "qwen2509-tuned"})
    selection = WorkflowProfileSelector(app, registry=registry).select(_TRIAL_BOOL)
    assert selection.profile == "Qwen2509-Original"
    assert selection.source == SOURCE_YAML_DEFAULT
    assert selection.rejected_state == "qwen2509-tuned"


def test_the_trial_toggle_is_still_case_insensitive(tmp_path: Path) -> None:
    registry = _mixed_case_registry(tmp_path)
    app = _FakeApp(
        {
            _TRIAL_BOOL: "ON",
            ACTIVE_ENTITY_ID: "Qwen2509-Original",
            TRIAL_ENTITY_ID: "Qwen2509-Tuned",
        }
    )
    selection = WorkflowProfileSelector(app, registry=registry).select(_TRIAL_BOOL)
    assert selection.profile == "Qwen2509-Tuned"
    assert selection.source == SOURCE_HA_TRIAL


@pytest.mark.parametrize("state", ["Unknown", "UNAVAILABLE", "  ", "Unavailable"])
def test_unusable_states_are_recognised_whatever_their_case(state: str) -> None:
    app = _FakeApp({_TRIAL_BOOL: "off", ACTIVE_ENTITY_ID: state})
    selection = WorkflowProfileSelector(app).select(_TRIAL_BOOL)
    assert selection.profile == _DEFAULT
    assert selection.source == SOURCE_YAML_DEFAULT


def test_reconcile_matches_the_selection_case_sensitively(tmp_path: Path) -> None:
    registry = _mixed_case_registry(tmp_path)
    app = _FakeApp(
        {
            ACTIVE_ENTITY_ID: _select_state("qwen2509-tuned", ["Qwen2509-Original", "Qwen2509-Tuned"]),
            TRIAL_ENTITY_ID: _select_state("Qwen2509-Tuned", ["Qwen2509-Original", "Qwen2509-Tuned"]),
        }
    )
    WorkflowProfileSelector(app, registry=registry).reconcile_options()
    selected = app.services("input_select/select_option")
    assert len(selected) == 1, "only the wrong-case selection is reset"
    assert selected[0]["option"] == "Qwen2509-Original"
