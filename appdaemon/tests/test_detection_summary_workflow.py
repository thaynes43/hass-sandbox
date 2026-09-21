"""DetectionSummary <-> ComfyUI workflow selection, which is config only.

A workflow is named in AppDaemon config — the bundle default, or
`ai_provider_conf.image_workflow` on one app — and a bad name stops the app at
`initialize()`. Nothing here reads Home Assistant.
"""

from __future__ import annotations

import asyncio
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List
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

from providers.ai_providers.comfyui.comfyui_image_generation_provider import (  # noqa: E402
    SOURCE_APP_CONFIG,
    SOURCE_BUNDLE,
)

_QWEN21 = "qwen-image-2.1-2609-25step-edit"
_LEGACY = "qwen-image-edit-2509-lightning4-legacy"
_TUNED = "qwen-image-edit-2509-lightning4-tuned"
_TUNED_3FRAME = "qwen-image-edit-2509-lightning4-tuned-3frame"

# Set per test by the fixture below so nothing lands in the repo tree.
_MEDIA_ROOT = ""


def _run_coro(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _comfyui_env(tmp_path: Path):
    os.environ["COMFYUI_URL"] = "https://comfyui.haynesops.com"
    global _MEDIA_ROOT
    _MEDIA_ROOT = str(tmp_path / "media")
    try:
        yield
    finally:
        os.environ.pop("COMFYUI_URL", None)


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


def _make_app(args: Dict[str, Any]) -> DetectionSummary:
    app = DetectionSummary(MagicMock(), MagicMock())
    app.args = args
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.call_service = MagicMock()
    app.get_state = MagicMock(return_value=None)
    app.create_task = MagicMock(side_effect=_run_coro)
    return app


def _provisioner_double() -> MagicMock:
    prov = AsyncMock()
    prov.ensure_helper.return_value = False
    prov.ensure_script.return_value = False
    prov._helper_slug = MagicMock(return_value="garage_detection_summary")
    return prov


def _initialize(app: DetectionSummary) -> MagicMock:
    prov = _provisioner_double()
    with patch("detection_summary_app.manager.HAProvisioner", return_value=prov):
        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            app.initialize()
            app._async_startup_wrapper({})
    return prov


# ---------- which workflow the config selects ----------


def test_bundle_default_is_used_when_the_app_says_nothing() -> None:
    app = _make_app(_args())
    _initialize(app)
    assert app._comfyui_workflow == _QWEN21
    assert app._comfyui_workflow_source == SOURCE_BUNDLE


def test_an_app_can_roll_back_to_a_2509_workflow_by_name() -> None:
    """Rolling one camera back to the pre-2.1 model is one config line."""
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _LEGACY})
    )
    _initialize(app)
    assert app._comfyui_workflow == _LEGACY
    assert app._comfyui_workflow_source == SOURCE_APP_CONFIG


def test_an_app_can_override_the_bundle_workflow_by_name() -> None:
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _TUNED})
    )
    _initialize(app)
    assert app._comfyui_workflow == _TUNED
    assert app._comfyui_workflow_source == SOURCE_APP_CONFIG


def test_the_app_override_wins_over_the_bundle() -> None:
    """Both levels set: the app's own config is the more specific one."""
    from providers.ai_providers.model_settings import loader

    loader.clear_cache()
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _TUNED_3FRAME})
    )
    _initialize(app)
    # The bundle pins the legacy workflow; the app asked for the 3-frame one.
    assert app._comfyui_workflow == _TUNED_3FRAME
    assert app._comfyui_workflow_source == SOURCE_APP_CONFIG


def test_the_registry_default_is_used_when_the_bundle_pins_nothing() -> None:
    """An inline config with no workflow falls through to the registry."""
    app = _make_app(
        _args(
            ai_provider_conf={
                "provider": "comfyui",
                "base_url": "https://comfyui.haynesops.com",
                "image_model": "qwen-image-edit-2509",
            }
        )
    )
    _initialize(app)
    assert app._comfyui_workflow == _QWEN21
    assert app._comfyui_workflow_source == "registry_default"


def test_the_resolved_workflow_is_logged_at_startup() -> None:
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _TUNED})
    )
    _initialize(app)
    lines = [str(c) for c in app.log.mock_calls if "comfyui workflow=" in str(c)]
    assert lines
    assert _TUNED in lines[0]
    assert SOURCE_APP_CONFIG in lines[0]


# ---------- fail fast on a bad name ----------


def test_an_unknown_app_workflow_stops_the_app_at_startup() -> None:
    """A config typo must never reach a render."""
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": "qwen-typo"})
    )
    with pytest.raises(ValueError) as exc_info:
        _initialize(app)
    message = str(exc_info.value)
    assert "qwen-typo" in message
    assert "not registered" in message
    # The error has to name what IS available, or the operator is guessing.
    for name in (_QWEN21, _LEGACY, _TUNED, _TUNED_3FRAME):
        assert name in message
    assert SOURCE_APP_CONFIG in message


def test_an_unknown_bundle_workflow_stops_the_app_at_startup() -> None:
    app = _make_app(
        _args(
            ai_provider_conf={
                "provider": "comfyui",
                "base_url": "https://comfyui.haynesops.com",
                "image_model": "qwen-image-edit-2509",
                "provider_options": {"workflow": "ghost-workflow"},
            }
        )
    )
    with pytest.raises(ValueError) as exc_info:
        _initialize(app)
    assert "ghost-workflow" in str(exc_info.value)
    assert SOURCE_BUNDLE in str(exc_info.value)


def test_image_workflow_is_ignored_for_a_non_comfyui_provider() -> None:
    """Other providers have no workflow concept; the key must not break them."""
    app = _make_app(
        _args(
            ai_provider_conf={
                "provider": "openai",
                "api_key_env": "OPENAI_API_KEY",
                "image_workflow": "qwen-typo",
            }
        )
    )
    _initialize(app)
    assert app._comfyui_workflow is None
    assert app._comfyui_workflow_source is None


def test_startup_capability_check_still_rejects_a_non_image_provider() -> None:
    app = _make_app(_args(ai_provider_conf={"provider": "ollama", "base_url": "http://x"}))
    with pytest.raises(ValueError) as exc_info:
        app.initialize()
    assert "image generation" in str(exc_info.value).lower()


def test_no_workflow_helpers_are_provisioned() -> None:
    """The HA selection layer is gone: only the summary-text helper remains."""
    app = _make_app(_args())
    prov = _initialize(app)
    helper_names = [c.args[1] for c in prov.ensure_helper.call_args_list]
    assert helper_names == ["Garage Detection Summary"]
    assert not any("input_select" in str(c) for c in prov.ensure_helper.call_args_list)
    assert not any("input_boolean" in str(c) for c in prov.ensure_helper.call_args_list)


# ---------- the workflow reaches the render and the bundle ----------


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
    """Run _build_bundle far enough to reach image generation."""
    run = _one_frame_run(app, run_id)
    seen_configs: List[Any] = []

    fake_provider = MagicMock()
    fake_provider.capabilities = MagicMock(supports_image_to_image=True)
    fake_provider.edit_image.return_value = dict(edit_meta)
    fake_provider.workflow_name = edit_meta.get("workflow_name")
    fake_provider.workflow_source = edit_meta.get("workflow_source")

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
    "elapsed_s": 47.1,
    "model": "qwen-image-edit-2509",
    "workflow_name": _TUNED,
    "workflow_name_requested": _TUNED,
    "workflow_source": SOURCE_APP_CONFIG,
}


def test_the_render_is_namespaced_to_the_zone() -> None:
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _TUNED})
    )
    _initialize(app)

    _bundle, configs = _drive_image_gen(app, "run-ns", edit_meta=_EDIT_META)

    run_cfg = configs[-1]
    assert run_cfg.provider_options["upload_namespace"] == "garage"
    # The workflow name still comes from config, untouched by the manager.
    assert run_cfg.provider_options["workflow"] == _TUNED


def test_the_workflow_reaches_the_bundle_and_the_llm_event() -> None:
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _TUNED})
    )
    _initialize(app)

    bundle, _configs = _drive_image_gen(app, "run-bundle", edit_meta=_EDIT_META)

    generated = bundle["generated_image"]
    assert generated["workflow_name"] == _TUNED
    assert generated["workflow_source"] == SOURCE_APP_CONFIG

    events = [
        e for e in bundle["summary"]["summarized_llm_events"] if e.get("type") == "image_edit"
    ]
    assert len(events) == 1
    assert events[0]["workflow_name"] == _TUNED
    assert events[0]["workflow_source"] == SOURCE_APP_CONFIG


def test_a_provider_fallback_is_reported_not_overwritten() -> None:
    """When the provider fell back, the bundle must name what actually rendered."""
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _TUNED_3FRAME})
    )
    _initialize(app)

    meta = dict(_EDIT_META)
    meta["workflow_name"] = _LEGACY
    meta["workflow_name_requested"] = _TUNED_3FRAME
    meta["workflow_fallback_reason"] = "value_not_in_list input_name='unet_name'"
    bundle, _configs = _drive_image_gen(app, "run-fallback", edit_meta=meta)

    generated = bundle["generated_image"]
    assert generated["workflow_name"] == _LEGACY
    assert generated["workflow_name_requested"] == _TUNED_3FRAME
    event = [
        e for e in bundle["summary"]["summarized_llm_events"] if e.get("type") == "image_edit"
    ][0]
    assert event["workflow_name"] == _LEGACY
    assert event["workflow_fallback_reason"].startswith("value_not_in_list")


def test_image_gen_start_log_names_the_workflow() -> None:
    app = _make_app(
        _args(ai_provider_conf={"image": "comfyui-qwen-edit", "image_workflow": _TUNED})
    )
    _initialize(app)
    _drive_image_gen(app, "run-log", edit_meta=_EDIT_META)

    start_logs = [str(c) for c in app.log.mock_calls if "image gen start" in str(c)]
    assert start_logs
    assert f"workflow={_TUNED}" in start_logs[0]
    assert f"workflow_source={SOURCE_APP_CONFIG}" in start_logs[0]
