"""Integration tests for PhotoFrameViewerApp generation swap lifecycle.

Uses the same mocked-AppDaemon pattern as test_detection_summary.py.
The viewer now reads source files via os.listdir() instead of a sensor.

After the provisioner migration, state (paused, interval, image_url) is
held internally and published on a virtual sensor rather than individual
helper entities.  Tests that previously relied on the paused/interval
helpers now set internal state directly after initialize().
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp


SOURCE_FILENAMES = ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"]

# Entity IDs derived from the default prefix "wall_display"
_SENSOR_ENTITY_ID = "sensor.wall_display_photo_frame_status"
_PICKER_ENTITY_ID = "input_select.wall_display_photo_frame_image"


def _make_app(
    *,
    source_dir: str = "",
    source_filenames: list[str] | None = None,
    current_url: str = "",
    picker_value: str = "",
    picker_options: list[str] | None = None,
    extra_args: dict | None = None,
) -> PhotoFrameViewerApp:
    """Create a PhotoFrameViewerApp with mocked AppDaemon methods.

    If *source_dir* is empty a temporary directory is created with
    *source_filenames* (defaults to SOURCE_FILENAMES) written as empty
    files.  The caller is responsible for cleanup if they care, but
    since these are under /tmp they'll be garbage collected.

    To test paused behaviour, call ``app.initialize()`` first and then
    set ``app._paused = True`` before triggering the relevant actions.
    """
    if source_filenames is None:
        source_filenames = list(SOURCE_FILENAMES)

    # Create a real temp dir with files so os.listdir works
    if not source_dir:
        td = tempfile.mkdtemp(prefix="viewer_test_")
        source_dir = td
    for fname in source_filenames:
        Path(os.path.join(source_dir, fname)).write_bytes(b"")

    ad = MagicMock()
    config = MagicMock()
    app = PhotoFrameViewerApp(ad, config)

    base_args = {
        "source_dir": source_dir,
        "ha_local_url_base": "/local/photo-frame/live",
        "stage_shell_command": "photo_frame_stage_gen",
        "cleanup_shell_command": "photo_frame_cleanup_gen",
        "source_poll_interval_s": 30,
        "stage_settle_delay_s": 3,
        "picker_entity_id": _PICKER_ENTITY_ID,
        "fallback_image_path": os.path.join(source_dir, "no-image.jpg"),
        "options_max": 50,
        "refresh_options_every_s": 60,
        "auto_cycle": True,
        "state_dir": source_dir,
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args

    # AppDaemon sets app.name to the instance name; set it so prefix
    # derivation produces a stable "wall_display" prefix in tests.
    app.name = "photo_frame_viewer_wall_display"

    if picker_options is None:
        picker_options = []

    def fake_get_state(entity_id, attribute=None):
        # Virtual status sensor — used by _recover_gen_from_sensor()
        if entity_id == _SENSOR_ENTITY_ID:
            if attribute == "all":
                return {
                    "state": "playing",
                    "attributes": {"image_url": current_url},
                }
            return "playing"
        # Picker entity
        if entity_id == _PICKER_ENTITY_ID:
            if attribute == "all":
                return {
                    "state": picker_value,
                    "attributes": {"options": list(picker_options)},
                }
            return picker_value
        return None

    app.get_state = MagicMock(side_effect=fake_get_state)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.cancel_timer = MagicMock()
    app.timer_running = MagicMock(return_value=False)
    app.datetime = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()

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


def _replace_source_files(source_dir: str, new_filenames: list[str]) -> None:
    """Replace all image files in source_dir with new ones."""
    for fname in os.listdir(source_dir):
        ext = os.path.splitext(fname)[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            os.remove(os.path.join(source_dir, fname))
    for fname in new_filenames:
        Path(os.path.join(source_dir, fname)).write_bytes(b"")


class TestInitializeRecovery:
    def test_parses_gen_from_existing_url(self):
        app = _make_app(current_url="/local/photo-frame/live/5/IMG_001.jpg")
        app.initialize()
        assert app._current_gen_id == "5"
        assert app._next_gen_counter == 7  # gen=5 recovered → next=6 → force re-stage uses 6 → next=7
        stage_calls = _service_calls(app, "shell_command/photo_frame_stage_gen")
        assert len(stage_calls) >= 1, "Force re-stage on init should have triggered staging"

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

    def test_registers_batch_ready_listener(self):
        app = _make_app(current_url="")
        app.initialize()
        event_names = [c.args[1] for c in app.listen_event.call_args_list]
        assert "immich_fetcher_batch_ready" in event_names


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
        # Complete the init staging that initialize() triggers (force re-stage)
        app._on_stage_settled({})
        app.call_service.reset_mock()
        app.log.reset_mock()
        return app

    def _stage_pending(
        self, app: PhotoFrameViewerApp, new_filenames: list[str] | None = None
    ):
        """Replace source files and trigger staging + settle."""
        if new_filenames is None:
            new_filenames = ["NEW_001.jpg", "NEW_002.jpg"]
        _replace_source_files(app.source_dir, new_filenames)
        app._poll_for_changes(reason="test")
        app._on_stage_settled({})

    def test_staging_does_not_change_displayed_url(self):
        app = self._init_with_gen("3")
        url_before = app._last_published_local_url

        _replace_source_files(app.source_dir, ["NEW_001.jpg", "NEW_002.jpg"])
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

        assert app._pending_gen_id == "5"
        set_opts = _set_options_calls(app)
        assert len(set_opts) == 0, "Picker options must not change while pending"

    def test_finalize_on_tick(self):
        app = self._init_with_gen("3")
        self._stage_pending(app, ["NEW_001.jpg"])
        assert app._pending_gen_id == "5"
        app.call_service.reset_mock()

        app._on_tick({})

        assert app._current_gen_id == "5"
        assert app._pending_gen_id is None
        assert "/local/photo-frame/live/5/" in (app._last_published_local_url or "")

        cleanup_calls = _service_calls(app, "shell_command/photo_frame_cleanup_gen")
        assert len(cleanup_calls) == 1
        assert cleanup_calls[0].kwargs["gen_id"] == "3"

    def test_paused_preserves_old_gen(self):
        url = "/local/photo-frame/live/3/IMG_001.jpg"
        app = _make_app(
            current_url=url,
            picker_value="IMG_001.jpg",
            picker_options=["IMG_001.jpg"],
        )
        app.initialize()
        app._paused = True  # Pause after initialize() to avoid reset
        app._on_stage_settled({})
        app.call_service.reset_mock()

        _replace_source_files(app.source_dir, ["NEW.jpg"])
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
        )
        app.initialize()
        app._paused = True  # Pause after initialize() to avoid reset
        app._on_stage_settled({})
        url_after_init = app._last_published_local_url

        app.call_service.reset_mock()
        app.set_state.reset_mock()

        _replace_source_files(app.source_dir, ["BRAND_NEW.jpg"])
        app._poll_for_changes(reason="test")
        app._on_stage_settled({})

        assert app._pending_gen_id is not None
        assert app._current_gen_id == "3"
        assert app._last_published_local_url == url_after_init

        # set_state should not have been called with a new image_url while paused
        for c in app.set_state.call_args_list:
            attrs = c[1].get("attributes", {})
            published_url = attrs.get("image_url", "")
            assert published_url == "" or published_url == url_after_init, (
                f"Paused slideshow published unexpected URL: {published_url}"
            )

        set_opts = _set_options_calls(app)
        assert len(set_opts) == 0

    def test_manual_nav_applies_pending(self):
        """When user manually navigates while a pending gen exists,
        the pending gen is applied."""
        app = self._init_with_gen("3")
        self._stage_pending(app, ["X.jpg"])
        assert app._pending_gen_id == "5"
        app.call_service.reset_mock()

        app._on_picker_change("", "", "IMG_001.jpg", "IMG_002.jpg", {})

        assert app._current_gen_id == "5"
        assert app._pending_gen_id is None
        assert "/local/photo-frame/live/5/" in (app._last_published_local_url or "")

    def test_back_to_back_batches_replaces_pending(self):
        app = self._init_with_gen("3")

        _replace_source_files(app.source_dir, ["A.jpg"])
        app._poll_for_changes(reason="test")
        app._on_stage_settled({})
        assert app._pending_gen_id == "5"

        app.call_service.reset_mock()

        _replace_source_files(app.source_dir, ["B.jpg"])
        app._current_fingerprint = None  # force re-detection
        app._poll_for_changes(reason="test2")

        cleanup_calls = _service_calls(app, "shell_command/photo_frame_cleanup_gen")
        assert any(c.kwargs.get("gen_id") == "5" for c in cleanup_calls)

        app._on_stage_settled({})
        assert app._pending_gen_id == "6"

    def test_tick_echo_absorbed_by_programmatic_target(self):
        """Programmatic picker change from _on_tick must not be treated as manual nav.

        Regression test for a bug where the time-based debounce window
        (1 s) was too short for HA state-change round-trips, causing every
        auto-advance to be mis-classified as manual navigation and the timer
        to be rescheduled repeatedly.
        """
        app = self._init_with_gen("3")
        # Clear any pending gen so _on_tick exercises the normal advance path.
        app._pending_gen_id = None
        opts = app._picker_options()
        assert len(opts) >= 2, "Need at least 2 picker options"
        first_label = opts[0]

        # Point the picker at the first label so _on_tick advances to the second.
        app.get_state = MagicMock(side_effect=lambda eid, attribute=None: (
            first_label if eid == _PICKER_ENTITY_ID and attribute is None
            else ({"state": first_label, "attributes": {"options": opts}}
                  if eid == _PICKER_ENTITY_ID and attribute == "all"
                  else None)
        ))

        app._on_tick({})

        # _on_tick should have set a programmatic target.
        expected_next = opts[1]
        assert app._programmatic_target == expected_next

        # Simulate HA echoing the state change back (this is _on_picker_change).
        app.log.reset_mock()
        app._on_picker_change(
            _PICKER_ENTITY_ID, "state", first_label, expected_next, {},
        )

        # The echo should be absorbed — no "manual nav" log.
        manual_nav_logs = [
            c for c in app.log.call_args_list
            if "manual nav" in str(c).lower()
        ]
        assert len(manual_nav_logs) == 0, (
            f"Expected no 'manual nav' logs but got: {manual_nav_logs}"
        )
        # Target should be cleared after absorption.
        assert app._programmatic_target is None

    def test_tick_with_pending_gen_does_not_double_advance(self):
        """_on_tick must return early after applying a pending gen swap.

        Regression test: previously _on_tick would apply the pending gen
        (selecting photo_0000) and then also advance to photo_0001 in the
        same tick, overwriting _programmatic_target.  The echo for
        photo_0000 would then be misclassified as manual nav, causing a
        visible double-advance (rapid skip).
        """
        app = self._init_with_gen("3")
        assert app._pending_gen_id is not None, "Should have a pending gen"
        pending_labels = app._pending_labels[:]
        assert len(pending_labels) >= 2, "Need at least 2 pending labels"

        app.call_service.reset_mock()
        app._on_tick({})

        # The pending gen should have been applied — first label selected.
        assert app._pending_gen_id is None, "Pending gen should be consumed"
        assert app._programmatic_target == pending_labels[0]

        # Crucially, _on_tick should NOT have also advanced to the next label.
        select_option_calls = [
            c for c in app.call_service.call_args_list
            if c.args[0] == "input_select/select_option"
        ]
        # Expect exactly one select_option call (from _apply_pending_gen),
        # not two (which would mean the tick also advanced).
        options_selected = [c.kwargs.get("option") for c in select_option_calls]
        assert options_selected == [pending_labels[0]], (
            f"Expected only [{pending_labels[0]}] but got {options_selected}"
        )

    def test_real_manual_nav_not_absorbed(self):
        """A genuine user navigation must not be absorbed by the programmatic target."""
        app = self._init_with_gen("3")
        # Consume any pending gen so _apply_pending_gen doesn't interfere.
        app._pending_gen_id = None
        opts = app._picker_options()
        assert len(opts) >= 3, "Need at least 3 picker options"

        # Simulate a tick that targets opts[1].
        app._programmatic_target = opts[1]

        # User navigates to opts[2] instead — different from the target.
        app.log.reset_mock()
        app._on_picker_change(
            _PICKER_ENTITY_ID, "state", opts[0], opts[2], {},
        )

        manual_nav_logs = [
            c for c in app.log.call_args_list
            if "manual nav" in str(c).lower()
        ]
        assert len(manual_nav_logs) == 1, (
            f"Expected 1 'manual nav' log but got: {manual_nav_logs}"
        )
        # Target should be cleared even on mismatch.
        assert app._programmatic_target is None

    def test_batch_ready_event_triggers_poll(self):
        """The immich_fetcher_batch_ready event handler calls _poll_for_changes."""
        app = self._init_with_gen("3")

        _replace_source_files(app.source_dir, ["FRESH.jpg"])
        app._on_batch_ready("immich_fetcher_batch_ready", {"count": 5}, {})

        stage_calls = _service_calls(app, "shell_command/photo_frame_stage_gen")
        assert len(stage_calls) == 1

    def test_displaying_filter_name_set_on_gen_swap(self):
        """Filter name from batch_ready event is published only when the gen is applied."""
        app = self._init_with_gen("3")
        # Simulate a batch_ready with a filter name + new source files.
        _replace_source_files(app.source_dir, ["F1.jpg", "F2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready",
            {"count": 2, "filter": "Boats"},
            {},
        )
        # Staging is triggered but the gen is only pending — filter name
        # should NOT be displayed yet.
        app._on_stage_settled({})
        assert app._displaying_filter_name == "", (
            "Filter name should not update until gen is actually applied"
        )

        # Now apply the pending gen via a tick.
        app._on_tick({})
        assert app._displaying_filter_name == "Boats", (
            "Filter name should update when gen is applied"
        )
