"""Integration tests for PhotoFrameViewerApp generation swap lifecycle.

Uses the same mocked-AppDaemon pattern as test_detection_summary.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp


SOURCE_PATHS = [
    "/config/www/immich-album/IMG_001.jpg",
    "/config/www/immich-album/IMG_002.jpg",
    "/config/www/immich-album/IMG_003.jpg",
]


def _make_app(
    *,
    current_url: str = "",
    picker_value: str = "",
    picker_options: list[str] | None = None,
    paused: bool = False,
    extra_args: dict | None = None,
) -> PhotoFrameViewerApp:
    """Create a PhotoFrameViewerApp with mocked AppDaemon methods."""
    ad = MagicMock()
    config = MagicMock()
    app = PhotoFrameViewerApp(ad, config)

    base_args = {
        "source_sensor_entity_id": "sensor.immich_album",
        "source_dir": "/config/www/immich-album",
        "ha_local_url_base": "/local/photo-frame/live",
        "stage_shell_command": "photo_frame_stage_gen",
        "cleanup_shell_command": "photo_frame_cleanup_gen",
        "source_poll_interval_s": 30,
        "stage_settle_delay_s": 3,
        "picker_entity_id": "input_select.wall_display_photo_frame_image",
        "paused_entity_id": "input_boolean.wall_display_photo_frame_paused",
        "interval_entity_id": "input_number.wall_display_photo_frame_interval_seconds",
        "cache_bust_entity_id": "input_text.wall_display_photo_frame_cache_bust",
        "image_local_url_entity_id": "input_text.wall_display_photo_frame_image_local_url",
        "fallback_image_path": "/config/www/immich-album/no-image.jpg",
        "options_max": 50,
        "refresh_options_every_s": 60,
        "auto_cycle": True,
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args

    if picker_options is None:
        picker_options = []

    def fake_get_state(entity_id, attribute=None):
        if entity_id == "input_text.wall_display_photo_frame_image_local_url":
            if attribute == "all":
                return {"state": current_url, "attributes": {}}
            return current_url
        if entity_id == "input_boolean.wall_display_photo_frame_paused":
            return "on" if paused else "off"
        if entity_id == "input_number.wall_display_photo_frame_interval_seconds":
            return "10.0"
        if entity_id == "input_select.wall_display_photo_frame_image":
            if attribute == "all":
                return {
                    "state": picker_value,
                    "attributes": {"options": list(picker_options)},
                }
            return picker_value
        if entity_id == "sensor.immich_album":
            if attribute == "all":
                return {
                    "state": "10",
                    "attributes": {"file_list": list(SOURCE_PATHS)},
                }
            return "10"
        return None

    app.get_state = MagicMock(side_effect=fake_get_state)
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.cancel_timer = MagicMock()
    app.timer_running = MagicMock(return_value=False)
    app.datetime = MagicMock()
    app.log = MagicMock()

    return app


def _service_calls(app: PhotoFrameViewerApp, service_prefix: str) -> list:
    """Extract call_service invocations matching a prefix."""
    return [
        c for c in app.call_service.call_args_list
        if c.args and str(c.args[0]).startswith(service_prefix)
    ]


def _set_options_calls(app: PhotoFrameViewerApp) -> list:
    """Extract input_select/set_options invocations."""
    return [
        c for c in app.call_service.call_args_list
        if c.args and c.args[0] == "input_select/set_options"
    ]


class TestInitializeRecovery:
    def test_parses_gen_from_existing_url(self):
        app = _make_app(current_url="/local/photo-frame/live/5/IMG_001.jpg")
        app.initialize()
        assert app._current_gen_id == "5"
        assert app._next_gen_counter == 6

    def test_first_run_stages_gen_1(self):
        app = _make_app(current_url="")
        app.initialize()
        stage_calls = _service_calls(app, "shell_command/photo_frame_stage_gen")
        assert len(stage_calls) == 1
        assert stage_calls[0].kwargs["gen_id"] == "1"

    def test_first_run_with_old_style_url(self):
        app = _make_app(current_url="/local/immich-album/pic.jpg")
        app.initialize()
        assert app._current_gen_id is None
        stage_calls = _service_calls(app, "shell_command/photo_frame_stage_gen")
        assert len(stage_calls) == 1


class TestPendingSwap:
    def _init_with_gen(self, gen_id: str = "3") -> PhotoFrameViewerApp:
        """Initialize an app that already has a current generation."""
        url = f"/local/photo-frame/live/{gen_id}/IMG_001.jpg"
        app = _make_app(
            current_url=url,
            picker_value="IMG_001.jpg",
            picker_options=["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"],
        )
        app.initialize()
        app.call_service.reset_mock()
        app.log.reset_mock()
        return app

    def _stage_pending(self, app: PhotoFrameViewerApp, new_source: list[str] | None = None):
        """Trigger source change + settle to create a pending gen."""
        if new_source is None:
            new_source = ["/config/www/immich-album/NEW_001.jpg", "/config/www/immich-album/NEW_002.jpg"]
        original_get = app.get_state.side_effect

        def updated_get(entity_id, attribute=None):
            if entity_id == "sensor.immich_album" and attribute == "all":
                return {"state": "5", "attributes": {"file_list": new_source}}
            return original_get(entity_id, attribute)

        app.get_state.side_effect = updated_get
        app._poll_for_changes(reason="test")
        app._on_stage_settled({})

    def test_staging_does_not_change_displayed_url(self):
        app = self._init_with_gen("3")
        url_before = app._last_published_local_url

        new_source = ["/config/www/immich-album/NEW_001.jpg", "/config/www/immich-album/NEW_002.jpg"]
        original_get = app.get_state.side_effect

        def updated_get(entity_id, attribute=None):
            if entity_id == "sensor.immich_album" and attribute == "all":
                return {"state": "5", "attributes": {"file_list": new_source}}
            return original_get(entity_id, attribute)

        app.get_state.side_effect = updated_get
        app._poll_for_changes(reason="test")

        stage_calls = _service_calls(app, "shell_command/photo_frame_stage_gen")
        assert len(stage_calls) == 1
        assert app._last_published_local_url == url_before

    def test_staging_does_not_update_picker_options(self):
        """Regression: staging must NOT update picker options (would cause
        HA to reset the selected value, changing the displayed image)."""
        app = self._init_with_gen("3")
        app.call_service.reset_mock()

        self._stage_pending(app)

        assert app._pending_gen_id == "4"
        set_opts = _set_options_calls(app)
        assert len(set_opts) == 0, "Picker options must not change while pending"

    def test_finalize_on_tick(self):
        app = self._init_with_gen("3")
        self._stage_pending(app, ["/config/www/immich-album/NEW_001.jpg"])
        assert app._pending_gen_id == "4"
        app.call_service.reset_mock()

        # Simulate tick (not paused). _on_tick calls _apply_pending_gen
        # then select_next, which triggers _on_picker_change.
        app._on_tick({})

        assert app._current_gen_id == "4"
        assert app._pending_gen_id is None
        assert "/local/photo-frame/live/4/" in (app._last_published_local_url or "")

        cleanup_calls = _service_calls(app, "shell_command/photo_frame_cleanup_gen")
        assert len(cleanup_calls) == 1
        assert cleanup_calls[0].kwargs["gen_id"] == "3"

    def test_paused_preserves_old_gen(self):
        url = "/local/photo-frame/live/3/IMG_001.jpg"
        app = _make_app(
            current_url=url,
            picker_value="IMG_001.jpg",
            picker_options=["IMG_001.jpg"],
            paused=True,
        )
        app.initialize()
        app.call_service.reset_mock()

        new_source = ["/config/www/immich-album/NEW.jpg"]
        original_get = app.get_state.side_effect

        def updated_get(entity_id, attribute=None):
            if entity_id == "sensor.immich_album" and attribute == "all":
                return {"state": "5", "attributes": {"file_list": new_source}}
            return original_get(entity_id, attribute)

        app.get_state.side_effect = updated_get
        app._poll_for_changes(reason="test")
        app._on_stage_settled({})

        assert app._pending_gen_id is not None
        assert app._current_gen_id == "3"

        cleanup_calls = _service_calls(app, "shell_command/photo_frame_cleanup_gen")
        assert all(c.kwargs.get("gen_id") != "3" for c in cleanup_calls)

    def test_paused_image_url_unchanged_on_new_batch(self):
        """Core regression test: a paused slideshow must NOT change the
        displayed image URL when a new batch arrives and stages."""
        url = "/local/photo-frame/live/3/IMG_001.jpg"
        app = _make_app(
            current_url=url,
            picker_value="IMG_001.jpg",
            picker_options=["IMG_001.jpg", "IMG_002.jpg"],
            paused=True,
        )
        app.initialize()
        url_after_init = app._last_published_local_url

        app.call_service.reset_mock()

        new_source = ["/config/www/immich-album/BRAND_NEW.jpg"]
        original_get = app.get_state.side_effect

        def updated_get(entity_id, attribute=None):
            if entity_id == "sensor.immich_album" and attribute == "all":
                return {"state": "99", "attributes": {"file_list": new_source}}
            return original_get(entity_id, attribute)

        app.get_state.side_effect = updated_get
        app._poll_for_changes(reason="test")
        app._on_stage_settled({})

        # Pending gen should be queued but NOT applied
        assert app._pending_gen_id is not None
        assert app._current_gen_id == "3"
        assert app._last_published_local_url == url_after_init

        # No input_text/set_value calls (image URL unchanged)
        url_calls = [
            c for c in app.call_service.call_args_list
            if c.args and c.args[0] == "input_text/set_value"
            and c.kwargs.get("entity_id") == "input_text.wall_display_photo_frame_image_local_url"
        ]
        assert len(url_calls) == 0

        # No picker option updates either
        set_opts = _set_options_calls(app)
        assert len(set_opts) == 0

    def test_manual_nav_applies_pending(self):
        """When user manually navigates while a pending gen exists,
        the pending gen is applied."""
        app = self._init_with_gen("3")
        self._stage_pending(app, ["/config/www/immich-album/X.jpg"])
        assert app._pending_gen_id == "4"
        app.call_service.reset_mock()

        # Simulate manual picker change
        app._on_picker_change("", "", "IMG_001.jpg", "IMG_002.jpg", {})

        assert app._current_gen_id == "4"
        assert app._pending_gen_id is None
        assert "/local/photo-frame/live/4/" in (app._last_published_local_url or "")

    def test_back_to_back_batches_replaces_pending(self):
        app = self._init_with_gen("3")

        batch1 = ["/config/www/immich-album/A.jpg"]
        batch2 = ["/config/www/immich-album/B.jpg"]
        original_get = app.get_state.side_effect

        def make_getter(batch):
            def getter(entity_id, attribute=None):
                if entity_id == "sensor.immich_album" and attribute == "all":
                    return {"state": "x", "attributes": {"file_list": batch}}
                return original_get(entity_id, attribute)
            return getter

        app.get_state.side_effect = make_getter(batch1)
        app._poll_for_changes(reason="test")
        app._on_stage_settled({})
        assert app._pending_gen_id == "4"

        app.call_service.reset_mock()

        app.get_state.side_effect = make_getter(batch2)
        app._current_fingerprint = None  # force re-detection
        app._poll_for_changes(reason="test2")

        cleanup_calls = _service_calls(app, "shell_command/photo_frame_cleanup_gen")
        assert any(c.kwargs.get("gen_id") == "4" for c in cleanup_calls)

        app._on_stage_settled({})
        assert app._pending_gen_id == "5"
