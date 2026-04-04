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
    sensor_attrs: dict | None = None,
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
                attrs = {"image_url": current_url}
                if sensor_attrs:
                    attrs.update(sensor_attrs)
                return {
                    "state": "playing",
                    "attributes": attrs,
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


class TestPollPendingFingerprintRace:
    """Regression tests for the periodic-poll vs pending-gen race condition.

    Root cause: _current_fingerprint only updates on finalization, not when a
    gen becomes pending.  A periodic poll between staging and finalization sees
    a different fingerprint, triggers a redundant re-stage that replaces the
    pending gen, and loses the filter name.
    """

    def _init_with_gen(self, gen_id: str = "3") -> PhotoFrameViewerApp:
        url = f"/local/photo-frame/live/{gen_id}/IMG_001.jpg"
        app = _make_app(
            current_url=url,
            picker_value="IMG_001.jpg",
            picker_options=["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"],
        )
        app.initialize()
        app._on_stage_settled({})
        app.call_service.reset_mock()
        app.log.reset_mock()
        return app

    def test_poll_skips_when_pending_fingerprint_matches(self):
        """A periodic poll must not re-stage when source files match the
        pending gen's fingerprint (the whole reason for the fix)."""
        app = self._init_with_gen("3")

        # Stage a new gen via batch_ready (like the fetcher finishing).
        _replace_source_files(app.source_dir, ["FLORIDA_1.jpg", "FLORIDA_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready",
            {"count": 2, "filter": "Florida"},
            {},
        )
        app._on_stage_settled({})

        assert app._pending_gen_id is not None
        assert app._pending_filter_name == "Florida"
        pending_gen = app._pending_gen_id

        app.call_service.reset_mock()

        # Simulate periodic poll with SAME source files on disk — this is the
        # race that used to replace the pending gen and lose the filter name.
        app._poll_for_changes(reason="poll")

        # No new staging should have happened.
        stage_calls = _service_calls(app, "shell_command/photo_frame_stage_gen")
        assert len(stage_calls) == 0, (
            "Periodic poll must not re-stage when pending fingerprint matches"
        )
        assert app._pending_gen_id == pending_gen, "Pending gen must not be replaced"
        assert app._pending_filter_name == "Florida", "Filter name must be preserved"

        # Now apply via tick — title should be correct.
        app._on_tick({})
        assert app._displaying_filter_name == "Florida"

    def test_filter_name_carries_forward_on_restage(self):
        """If a re-stage IS needed (e.g. files actually changed), the filter
        name from the pending gen must carry forward via _staged_filter_name."""
        app = self._init_with_gen("3")

        # Stage a new gen via batch_ready.
        _replace_source_files(app.source_dir, ["FLORIDA_1.jpg", "FLORIDA_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready",
            {"count": 2, "filter": "Florida"},
            {},
        )
        app._on_stage_settled({})

        assert app._pending_filter_name == "Florida"

        # Force a re-stage by changing files (simulates an mtime change or
        # partial download that produces a different fingerprint).
        _replace_source_files(app.source_dir, ["FLORIDA_1.jpg", "FLORIDA_2.jpg", "FLORIDA_3.jpg"])
        # Force the pending fingerprint to NOT match so re-staging is triggered.
        app._pending_fingerprint = "force_mismatch"
        app.call_service.reset_mock()
        app._poll_for_changes(reason="poll")

        # A new staging SHOULD have happened (files genuinely changed).
        stage_calls = _service_calls(app, "shell_command/photo_frame_stage_gen")
        assert len(stage_calls) == 1, "Should re-stage when files actually changed"

        # The filter name should have been carried forward.
        assert app._staged_filter_name == "Florida", (
            "Filter name must carry forward from replaced pending gen"
        )

        # Settle and apply — filter name should survive.
        app._on_stage_settled({})
        assert app._pending_filter_name == "Florida"
        app._on_tick({})
        assert app._displaying_filter_name == "Florida"


class TestFilterNameRecovery:
    """Filter name must survive app restarts so the card title stays correct."""

    def test_recovers_filter_name_from_sensor(self):
        """On restart, _displaying_filter_name is restored from the sensor attribute."""
        app = _make_app(
            current_url="/local/photo-frame/live/10/IMG_001.jpg",
            sensor_attrs={"displaying_filter_name": "Hampton Beach"},
        )
        app.initialize()
        assert app._displaying_filter_name == "Hampton Beach"

    def test_recovers_empty_filter_gracefully(self):
        """If the sensor has no filter name, recovery doesn't crash."""
        app = _make_app(
            current_url="/local/photo-frame/live/10/IMG_001.jpg",
            sensor_attrs={},
        )
        app.initialize()
        # Should be empty (no filter to recover) — not crash
        assert app._displaying_filter_name == ""

    def test_recovered_filter_survives_startup_staging(self):
        """Recovered filter name persists even when the startup re-stage
        produces a pending gen with no filter name (no batch_ready event)."""
        app = _make_app(
            current_url="/local/photo-frame/live/10/IMG_001.jpg",
            sensor_attrs={"displaying_filter_name": "Robot"},
        )
        app.initialize()
        assert app._displaying_filter_name == "Robot"

        # Startup forces a re-stage. Settle produces a pending gen with
        # empty _staged_filter_name (no batch_ready event fired).
        app._on_stage_settled({})
        assert app._pending_filter_name == ""

        # Apply the pending gen via tick — filter should NOT be cleared.
        app._on_tick({})
        assert app._displaying_filter_name == "Robot", (
            "Recovered filter name must survive gen swap with empty pending filter"
        )

    def test_filter_name_persisted_to_state_file(self):
        """Filter name is saved to the runtime state file when it changes."""
        app = _make_app(
            current_url="/local/photo-frame/live/3/IMG_001.jpg",
            sensor_attrs={"displaying_filter_name": ""},
        )
        app.initialize()

        # Simulate a batch_ready + stage + apply cycle.
        _replace_source_files(app.source_dir, ["P1.jpg", "P2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready",
            {"count": 2, "filter": "Theme Park"},
            {},
        )
        app._on_stage_settled({})
        app._on_tick({})

        assert app._displaying_filter_name == "Theme Park"

        # Verify the state file contains the filter name.
        import json
        state_path = Path(app._state_file)
        assert state_path.is_file(), "State file should exist after gen swap"
        data = json.loads(state_path.read_text())
        assert data.get("displaying_filter_name") == "Theme Park"

    def test_filter_name_loaded_from_state_file_as_fallback(self):
        """If sensor doesn't have the filter name, the state file is used."""
        td = tempfile.mkdtemp(prefix="viewer_state_")
        import json
        state_file = os.path.join(td, "state.json")
        Path(state_file).write_text(json.dumps({
            "interval_seconds": 15,
            "pause_auto_resume_s": 600,
            "displaying_filter_name": "Florida",
        }))

        app = _make_app(
            source_dir=td,
            current_url="",  # No sensor to recover gen from
            extra_args={"state_dir": td},
        )
        app.initialize()
        assert app._displaying_filter_name == "Florida"
