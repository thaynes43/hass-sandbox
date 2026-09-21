"""DetectionSummary <-> ComfyUI workflow-profile wiring.

Covers the app-layer half of runtime workflow selection: the selector is built
only for ComfyUI, the helpers are provisioned on startup, and the chosen
profile reaches both the provider config and the published bundle.
"""

from __future__ import annotations

import asyncio
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _MockHass:
    def __init__(self, ad, config):
        pass


mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo))
sys.path.insert(0, str(_repo / "apps"))

_capture_mod = import_module("detection_summary_app.capture")
CaptureState = _capture_mod.CaptureState
CapturedFrame = _capture_mod.CapturedFrame
_manager_mod = import_module("detection_summary_app.manager")
DetectionSummary = _manager_mod.DetectionSummary
_Run = _manager_mod._Run
_selection_mod = import_module("detection_summary_app.selection")
SelectionMeta = _selection_mod.SelectionMeta

from providers.ai_providers.comfyui.workflow_profile_selector import (  # noqa: E402
    ACTIVE_ENTITY_ID,
    SOURCE_HA_ACTIVE,
    SOURCE_HA_TRIAL,
    TRIAL_ENTITY_ID,
    reset_global_provisioning_state,
)

_TRIAL_BOOL = "input_boolean.garage_detection_summary_trial_workflow"


def _run_coro(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _comfyui_env(tmp_path: Path):
    """Isolate the once-per-process helper state and the media root per test."""
    reset_global_provisioning_state()
    os.environ["COMFYUI_URL"] = "https://comfyui.haynesops.com"
    global _MEDIA_ROOT
    _MEDIA_ROOT = str(tmp_path / "media")
    try:
        yield
    finally:
        os.environ.pop("COMFYUI_URL", None)
        reset_global_provisioning_state()


# Set per test by the fixture above so nothing lands in the repo tree.
_MEDIA_ROOT = ""


def _args(**overrides: Any) -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "bundle_key": "garage",
        "ha_url": "http://homeassistant.local:8123",
        "ha_token_env": "TOKEN",
        "hass_entities": {
            "camera_entity_id": "camera.garage",
            "trigger_entity_id": "binary_sensor.garage_person",
        },
        "snapshot_ha_dir": "/media/detection-summary/garage",
        "media_fs_root": _MEDIA_ROOT,
        "data_instructions": "test",
        "image_instructions": "make it nice",
        "ai_data_enabled": False,
        "run_narrative_enabled": False,
        "external_image_gen_enabled": True,
        "analyze_max_snapshots": 1,
        "ai_provider_conf": {"image": "comfyui-qwen-edit"},
    }
    args.update(overrides)
    return args


def _make_app(args: Dict[str, Any], states: Optional[Dict[str, Any]] = None) -> DetectionSummary:
    app = DetectionSummary(MagicMock(), MagicMock())
    app.args = args
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.call_service = MagicMock()
    app.create_task = MagicMock(side_effect=_run_coro)
    app.get_state = MagicMock(side_effect=lambda eid, attribute=None: (states or {}).get(eid))
    return app


def _provisioner_double() -> MagicMock:
    prov = AsyncMock()
    prov.ensure_helper.return_value = False
    prov.ensure_script.return_value = False
    prov._helper_slug = MagicMock(
        side_effect=lambda helper_type, name: "_".join(
            part
            for part in "".join(c if c.isalnum() else "_" for c in name.lower()).split("_")
            if part
        )
    )
    return prov


def _initialize(app: DetectionSummary, prov: Optional[MagicMock] = None) -> MagicMock:
    prov = prov if prov is not None else _provisioner_double()
    with patch("detection_summary_app.manager.HAProvisioner", return_value=prov):
        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            app.initialize()
            app._async_startup_wrapper({})
    return prov


# ---------- selector construction ----------


def test_comfyui_image_provider_gets_a_workflow_selector() -> None:
    app = _make_app(_args())
    _initialize(app)
    assert app._workflow_profile_selector is not None
    assert app._trial_workflow_entity_id == _TRIAL_BOOL


def test_non_comfyui_image_provider_gets_no_selector_and_no_helpers() -> None:
    app = _make_app(
        _args(
            ai_provider_conf={"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        )
    )
    prov = _initialize(app)
    assert app._workflow_profile_selector is None
    assert app._trial_workflow_entity_id is None
    helper_names = [c.args[1] for c in prov.ensure_helper.call_args_list]
    assert helper_names == ["Garage Detection Summary"]


def test_startup_provisions_the_workflow_helpers() -> None:
    app = _make_app(_args())
    prov = _initialize(app)
    helper_names = [c.args[1] for c in prov.ensure_helper.call_args_list]
    assert helper_names == [
        "Garage Detection Summary",
        "ComfyUI Active Workflow",
        "ComfyUI Trial Workflow",
        "Garage Detection Summary Trial Workflow",
    ]


def test_startup_capability_check_still_rejects_a_non_image_provider() -> None:
    app = _make_app(_args(ai_provider_conf={"provider": "ollama", "base_url": "http://x"}))
    with pytest.raises(ValueError) as exc_info:
        app.initialize()
    assert "image generation" in str(exc_info.value).lower()


# ---------- selection reaches the provider + the bundle ----------


def _one_frame_run(app: DetectionSummary, run_id: str) -> _Run:
    local_run_dir = (
        app._ha_path_to_local_fs(app.snapshot_ha_dir) / app.bundle_runs_subdir / run_id
    )
    frames_dir = local_run_dir / app.captured_subdir
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "frame_000.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
    return _Run(
        capture=CaptureState(
            run_id=run_id,
            started_ts=1.0,
            ended_ts=2.0,
            frames=[
                CapturedFrame(
                    idx=0,
                    filename="frame_000.jpg",
                    image_ha_path=(
                        f"{app.snapshot_ha_dir}/{app.bundle_runs_subdir}/{run_id}"
                        f"/{app.captured_subdir}/frame_000.jpg"
                    ),
                    captured_ts=1.5,
                )
            ],
            capture_idx=1,
        )
    )


def _fake_score(**_: Any):
    score = _selection_mod.ScoreResult(
        male_count=1,
        female_count=0,
        animal_count=0,
        person_score=9.0,
        face_score=8.0,
        frame_score=9.0,
        pose="standing",
        summary="A person at the door.",
        structured={},
    )
    return {0: score}, SelectionMeta(
        budget=1, scored_indices=[0], probes=[0], cutoff_idx_inclusive=0, best_idx=0
    )


def _drive_image_gen(
    app: DetectionSummary, run_id: str, *, edit_meta: Dict[str, Any]
) -> tuple[Dict[str, Any], List[Any]]:
    """Run _build_bundle far enough to reach image generation; return bundle + configs."""
    run = _one_frame_run(app, run_id)
    seen_configs: List[Any] = []

    fake_provider = MagicMock()
    fake_provider.capabilities = MagicMock(supports_image_to_image=True)
    fake_provider.edit_image.return_value = dict(edit_meta)

    def _build(cfg):
        seen_configs.append(cfg)
        return fake_provider

    with patch("detection_summary_app.manager.adaptive_select_and_score", side_effect=lambda **kw: _fake_score(**kw)), \
         patch("detection_summary_app.manager.should_publish_bundle", return_value=True), \
         patch("detection_summary_app.manager.build_image_provider", side_effect=_build), \
         patch("detection_summary_app.manager.delete_run_dir", return_value=False):
        bundle = app._build_bundle(run)

    assert bundle is not None
    return bundle, seen_configs


_EDIT_META = {
    "elapsed_s": 51.2,
    "model": "qwen-image-edit-2509",
    "workflow_profile": "qwen2509-tuned",
    "workflow_profile_requested": "qwen2509-tuned",
}


def test_active_profile_reaches_the_provider_config_and_the_bundle() -> None:
    states = {ACTIVE_ENTITY_ID: "qwen2509-tuned", TRIAL_ENTITY_ID: "qwen2509-tuned-multiframe", _TRIAL_BOOL: "off"}
    app = _make_app(_args(), states)
    _initialize(app)

    bundle, configs = _drive_image_gen(app, "run-active", edit_meta=_EDIT_META)

    # The build-time config plus the per-run one.
    run_cfg = configs[-1]
    assert run_cfg.provider_options["workflow_profile"] == "qwen2509-tuned"
    assert run_cfg.provider_options["upload_namespace"] == "garage"

    generated = bundle["generated_image"]
    assert generated["workflow_profile"] == "qwen2509-tuned"
    assert generated["workflow_profile_source"] == SOURCE_HA_ACTIVE

    image_events = [
        e for e in bundle["summary"]["summarized_llm_events"] if e.get("type") == "image_edit"
    ]
    assert len(image_events) == 1
    assert image_events[0]["workflow_profile"] == "qwen2509-tuned"
    assert image_events[0]["workflow_profile_source"] == SOURCE_HA_ACTIVE


def test_trial_toggle_switches_the_zone_to_the_trial_profile() -> None:
    states = {ACTIVE_ENTITY_ID: "qwen2509-tuned", TRIAL_ENTITY_ID: "qwen2509-tuned-multiframe", _TRIAL_BOOL: "on"}
    app = _make_app(_args(), states)
    _initialize(app)

    meta = dict(_EDIT_META)
    meta["workflow_profile"] = "qwen2509-tuned-multiframe"
    meta["workflow_profile_requested"] = "qwen2509-tuned-multiframe"
    bundle, configs = _drive_image_gen(app, "run-trial", edit_meta=meta)

    assert configs[-1].provider_options["workflow_profile"] == "qwen2509-tuned-multiframe"
    assert bundle["generated_image"]["workflow_profile"] == "qwen2509-tuned-multiframe"
    assert bundle["generated_image"]["workflow_profile_source"] == SOURCE_HA_TRIAL


def test_provider_fallback_is_reported_not_overwritten() -> None:
    """When the provider fell back, the bundle must name the profile that rendered."""
    states = {ACTIVE_ENTITY_ID: "qwen2509-tuned-multiframe", _TRIAL_BOOL: "off"}
    app = _make_app(_args(), states)
    _initialize(app)

    meta = dict(_EDIT_META)
    meta["workflow_profile"] = "qwen2509-original"
    meta["workflow_profile_requested"] = "qwen2509-tuned-multiframe"
    meta["workflow_profile_fallback_reason"] = "value_not_in_list input_name='unet_name'"
    bundle, _ = _drive_image_gen(app, "run-fallback", edit_meta=meta)

    generated = bundle["generated_image"]
    assert generated["workflow_profile"] == "qwen2509-original"
    assert generated["workflow_profile_requested"] == "qwen2509-tuned-multiframe"
    assert generated["workflow_profile_source"] == SOURCE_HA_ACTIVE
    image_event = [
        e for e in bundle["summary"]["summarized_llm_events"] if e.get("type") == "image_edit"
    ][0]
    assert image_event["workflow_profile"] == "qwen2509-original"


def test_image_gen_start_log_names_the_profile_and_source() -> None:
    states = {ACTIVE_ENTITY_ID: "qwen2509-tuned", _TRIAL_BOOL: "off"}
    app = _make_app(_args(), states)
    _initialize(app)
    _drive_image_gen(app, "run-log", edit_meta=_EDIT_META)

    start_logs = [
        str(c) for c in app.log.mock_calls if "image gen start" in str(c)
    ]
    assert start_logs
    assert "workflow_profile=qwen2509-tuned" in start_logs[0]
    assert f"workflow_profile_source={SOURCE_HA_ACTIVE}" in start_logs[0]
