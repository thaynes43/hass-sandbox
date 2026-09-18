"""Staging verification tests for PhotoFrameViewerApp.

Regression suite for the "broken images on the wall display" defect
(2026-09-18).  The app used to mark a generation ready 3 seconds after
firing ``shell_command/photo_frame_stage_gen``, with no check that the
command had actually produced anything.  When the NFS source stalled
mid-copy, HA killed the shell at its 60s timeout, the atomic ``mv`` never
ran, and the app happily published ``/local/photo-frame/live/<gen>/...``
URLs that 404'd until the next successful generation (10+ minutes later).

The invariant these tests guard: **a generation is never marked pending —
and therefore never published — until HA is verifiably serving it.**
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp  # noqa: E402


SOURCE_FILENAMES = ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"]

_SENSOR_ENTITY_ID = "sensor.wall_display_photo_frame_status"
_PICKER_ENTITY_ID = "input_select.wall_display_photo_frame_image"

# Obviously-fake value (security policy S5) — no real host is ever contacted:
# local_file_exists is patched in every test that reaches it.
_FAKE_HA_URL = "http://ha.test:8123"

_PROBE = "photo_frame_viewer.photo_frame_viewer_app.local_file_exists"


# ----------------------------------------------------------------------
# Fixtures / harness
# ----------------------------------------------------------------------


def _make_app(
    *,
    current_url: str = "",
    picker_value: str = "IMG_001.jpg",
    picker_options: list[str] | None = None,
    source_filenames: list[str] | None = None,
    extra_args: dict | None = None,
) -> PhotoFrameViewerApp:
    """A PhotoFrameViewerApp with mocked AppDaemon methods and a real temp dir."""
    source_dir = tempfile.mkdtemp(prefix="pfv_verify_")
    for fname in source_filenames if source_filenames is not None else SOURCE_FILENAMES:
        Path(os.path.join(source_dir, fname)).write_bytes(b"")

    app = PhotoFrameViewerApp(MagicMock(), MagicMock())

    base_args: dict = {
        "ha_url": _FAKE_HA_URL,
        "source_dir": source_dir,
        "ha_local_url_base": "/local/photo-frame/live",
        "stage_shell_command": "photo_frame_stage_gen",
        "cleanup_shell_command": "photo_frame_cleanup_gen",
        "source_poll_interval_s": 30,
        "stage_settle_delay_s": 3,
        "stage_verify_interval_s": 5,
        "stage_verify_timeout_s": 240,
        "picker_entity_id": _PICKER_ENTITY_ID,
        "fallback_image_path": os.path.join(source_dir, "no-image.jpg"),
        "options_max": 50,
        "auto_cycle": True,
        "state_dir": source_dir,
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args
    app.name = "photo_frame_viewer_wall_display"

    options = list(picker_options or ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"])

    def fake_get_state(entity_id, attribute=None):
        if entity_id == _SENSOR_ENTITY_ID:
            if attribute == "all":
                return {"state": "playing", "attributes": {"image_url": current_url}}
            return "playing"
        if entity_id == _PICKER_ENTITY_ID:
            if attribute == "all":
                return {"state": picker_value, "attributes": {"options": options}}
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
    # Left as a bare MagicMock on purpose: the drivers below pull the
    # coroutine back out of call_args and await it, so nothing leaks.
    app.create_task = MagicMock()

    return app


def _replace_source_files(source_dir: str, new_filenames: list[str]) -> None:
    for fname in os.listdir(source_dir):
        if os.path.splitext(fname)[1].lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            os.remove(os.path.join(source_dir, fname))
    for fname in new_filenames:
        Path(os.path.join(source_dir, fname)).write_bytes(b"")


def _service_calls(app: PhotoFrameViewerApp, service: str) -> list:
    return [
        c for c in app.call_service.call_args_list
        if c.args and str(c.args[0]) == service
    ]


def _stage_calls(app: PhotoFrameViewerApp) -> list:
    return _service_calls(app, "shell_command/photo_frame_stage_gen")


def _cleanup_calls(app: PhotoFrameViewerApp) -> list:
    return _service_calls(app, "shell_command/photo_frame_cleanup_gen")


def _last_run_in(app: PhotoFrameViewerApp, fn) -> object:
    matches = [c for c in app.run_in.call_args_list if c.args and c.args[0] == fn]
    assert matches, f"expected a run_in() scheduled for {fn.__name__}"
    return matches[-1]


def _logs(app: PhotoFrameViewerApp, level: str, needle: str) -> list[str]:
    out = []
    for c in app.log.call_args_list:
        if c.kwargs.get("level") != level:
            continue
        message = str(c.args[0]) if c.args else ""
        if needle in message:
            out.append(message)
    return out


def _verify_round(
    app: PhotoFrameViewerApp,
    *,
    exists: bool,
    elapsed_s: float | None = None,
) -> AsyncMock:
    """Drive one full verification round: timer -> HTTP probe -> result.

    Mirrors what AppDaemon does at runtime — fire the scheduled ``run_in``
    callback, run the coroutine it hands to ``create_task``, then deliver the
    result through the ``run_in(..., 0)`` bounce back onto the app thread.
    """
    if elapsed_s is not None:
        app._staging_started_at = time.monotonic() - elapsed_s

    timer_call = _last_run_in(app, app._on_stage_verify)
    before = len(app.create_task.call_args_list)

    probe = AsyncMock(return_value=exists)
    with patch(_PROBE, new=probe):
        app._on_stage_verify(dict(timer_call.kwargs))
        created = app.create_task.call_args_list[before:]
        assert len(created) == 1, (
            f"expected exactly one probe coroutine, got {len(created)}"
        )
        asyncio.run(created[0].args[0])

    result_call = _last_run_in(app, app._on_stage_verify_result)
    app._on_stage_verify_result(dict(result_call.kwargs))
    return probe


def _init_with_current_gen(gen_id: str = "3", **kwargs) -> PhotoFrameViewerApp:
    """Initialize an app that already has a current generation on the sensor.

    ``initialize()`` always forces a re-stage, so the returned app has one
    generation in flight and awaiting verification.
    """
    app = _make_app(
        current_url=f"/local/photo-frame/live/{gen_id}/IMG_001.jpg", **kwargs
    )
    app.initialize()
    return app


# ----------------------------------------------------------------------
# (a) verified on the first check
# ----------------------------------------------------------------------


class TestVerifiedFirstCheck:
    def test_nothing_is_pending_before_verification(self):
        app = _init_with_current_gen()
        assert app._staging_gen_id == "4"
        assert app._staging_in_progress is True
        assert app._pending_gen_id is None, (
            "a generation must not be pending before HA is known to serve it"
        )

    def test_first_check_success_marks_pending_and_clears_latch(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id

        _verify_round(app, exists=True)

        assert app._pending_gen_id == gen
        assert app._pending_labels == ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"]
        assert app._staging_in_progress is False
        assert app._staging_gen_id is None

    def test_probe_targets_the_url_the_card_will_load(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id

        probe = _verify_round(app, exists=True)

        assert probe.await_count == 1
        ha_url, url_path = probe.await_args.args
        assert ha_url == _FAKE_HA_URL
        assert url_path == f"/local/photo-frame/live/{gen}/IMG_001.jpg"

    def test_first_check_does_not_log_a_duration_at_info(self):
        """Only a slow stage (more than one check) is interesting at INFO."""
        app = _init_with_current_gen()
        _verify_round(app, exists=True)
        assert _logs(app, "INFO", "verified staged") == []

    def test_first_check_is_scheduled_at_the_settle_delay(self):
        app = _init_with_current_gen()
        assert _last_run_in(app, app._on_stage_verify).args[1] == app.stage_settle_delay_s

    def test_stage_service_call_is_fire_and_forget(self):
        """The 60s-blocking synchronous call starved the slideshow tick."""
        app = _init_with_current_gen()
        stage = _stage_calls(app)
        assert len(stage) == 1
        assert stage[0].kwargs.get("callback") == app._on_stage_service_result
        assert stage[0].kwargs["gen_id"] == "4"


# ----------------------------------------------------------------------
# (b) appears after several checks
# ----------------------------------------------------------------------


class TestSlowStaging:
    def test_pending_is_set_only_once_the_file_appears(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id

        for attempt in range(3):
            _verify_round(app, exists=False)
            assert app._pending_gen_id is None, (
                f"published after failed check {attempt + 1}"
            )
            assert app._staging_in_progress is True, "latch released too early"
            assert app._staging_gen_id == gen

        _verify_round(app, exists=True)

        assert app._pending_gen_id == gen
        assert app._staging_in_progress is False

    def test_rechecks_use_the_verify_interval(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=False)
        assert _last_run_in(app, app._on_stage_verify).args[1] == app.stage_verify_interval_s

    def test_slow_stage_logs_duration_and_check_count_at_info(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=False)
        _verify_round(app, exists=False)
        app.log.reset_mock()
        _verify_round(app, exists=True, elapsed_s=42.0)

        messages = _logs(app, "INFO", "verified staged")
        assert len(messages) == 1, messages
        assert "42.0s" in messages[0]
        assert "3 checks" in messages[0]

    def test_no_url_is_published_while_waiting(self):
        app = _init_with_current_gen()
        url_before = app._last_published_local_url
        app.set_state.reset_mock()

        _verify_round(app, exists=False)
        _verify_round(app, exists=False)

        assert app._last_published_local_url == url_before
        for c in app.set_state.call_args_list:
            published = c.kwargs.get("attributes", {}).get("image_url", "")
            assert published in ("", url_before)


# ----------------------------------------------------------------------
# (c) never appears -> deadline
# ----------------------------------------------------------------------


class TestVerificationDeadline:
    def _stage_a_failing_batch(self, app: PhotoFrameViewerApp) -> str:
        """Settle the startup gen, then stage a fresh batch that never lands."""
        _verify_round(app, exists=True)
        app._on_tick({})
        app.call_service.reset_mock()
        app.log.reset_mock()

        _replace_source_files(app.source_dir, ["FLORIDA_1.jpg", "FLORIDA_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": "Florida"}, {}
        )
        failed_gen = app._staging_gen_id
        assert failed_gen is not None
        assert app._staged_filter_name == "Florida"
        return failed_gen

    def test_deadline_publishes_nothing_and_cleans_up(self):
        app = _init_with_current_gen()
        failed_gen = self._stage_a_failing_batch(app)
        url_before = app._last_published_local_url

        _verify_round(app, exists=False)
        assert app._staging_in_progress is True

        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._pending_gen_id is None
        assert app._pending_fingerprint is None
        assert app._staging_in_progress is False
        assert app._staging_gen_id is None
        assert app._last_published_local_url == url_before

        cleanup = _cleanup_calls(app)
        assert any(c.kwargs.get("gen_id") == failed_gen for c in cleanup), (
            f"the abandoned gen must be cleaned up; cleanup calls: {cleanup}"
        )

    def test_deadline_logs_one_actionable_warning(self):
        app = _init_with_current_gen()
        failed_gen = self._stage_a_failing_batch(app)
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        warnings = _logs(app, "WARNING", "FAILED verification")
        assert len(warnings) == 1, warnings
        message = warnings[0]
        assert f"gen={failed_gen}" in message
        assert "/local/photo-frame/live/" in message
        assert "/config/www/photo-frame/live/.stage.log" in message
        # S3/S6: the probe URL is logged, never a credential.
        assert "Bearer" not in message
        assert "token" not in message.lower()

    def test_deadline_preserves_the_album_title_for_the_retry(self):
        app = _init_with_current_gen()
        self._stage_a_failing_batch(app)
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._staged_filter_name == "Florida", (
            "the automatic re-stage must keep the album title"
        )

    def test_next_poll_restages_with_a_new_gen_id(self):
        app = _init_with_current_gen()
        failed_gen = self._stage_a_failing_batch(app)
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        app.call_service.reset_mock()
        app._poll_for_changes(reason="poll")

        stage = _stage_calls(app)
        assert len(stage) == 1, "the next poll must re-stage after a failed stage"
        new_gen = stage[0].kwargs["gen_id"]
        assert new_gen != failed_gen
        assert app._staging_gen_id == new_gen

        # ...and the retry, once verified, publishes normally with the title.
        _verify_round(app, exists=True)
        assert app._pending_gen_id == new_gen
        app._on_tick({})
        assert app._displaying_filter_name == "Florida"
        assert f"/local/photo-frame/live/{new_gen}/" in (app._last_published_local_url or "")


# ----------------------------------------------------------------------
# (d) stale / superseded callbacks
# ----------------------------------------------------------------------


class TestStaleCallbacks:
    def test_stale_verify_timer_is_a_noop(self):
        app = _init_with_current_gen()
        in_flight = app._staging_gen_id
        app.create_task.reset_mock()

        app._on_stage_verify({"gen_id": "999"})

        assert app.create_task.call_count == 0, "superseded gen must not probe"
        assert app._staging_gen_id == in_flight
        assert app._staging_in_progress is True
        assert app._pending_gen_id is None

    def test_stale_verify_result_is_a_noop(self):
        app = _init_with_current_gen()
        in_flight = app._staging_gen_id

        app._on_stage_verify_result({"gen_id": "999", "exists": True})

        assert app._pending_gen_id is None, (
            "a superseded generation must never be promoted"
        )
        assert app._staging_gen_id == in_flight
        assert app._staging_in_progress is True
        assert app._staging_checks == 0

    def test_missing_gen_id_is_a_noop(self):
        app = _init_with_current_gen()
        app.create_task.reset_mock()

        app._on_stage_verify({})
        app._on_stage_verify_result({"exists": True})

        assert app.create_task.call_count == 0
        assert app._pending_gen_id is None
        assert app._staging_in_progress is True

    def test_zombie_instance_releases_the_latch(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id
        app.create_task.reset_mock()

        # An AppDaemon reload constructs a newer instance for the same prefix.
        newer = _make_app()
        newer.initialize()
        assert app._is_active_owner() is False

        app._on_stage_verify({"gen_id": gen})

        assert app.create_task.call_count == 0
        assert app._staging_in_progress is False
        assert app._pending_gen_id is None

    def test_zombie_instance_drops_a_verify_result(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id
        newer = _make_app()
        newer.initialize()

        app._on_stage_verify_result({"gen_id": gen, "exists": True})

        assert app._pending_gen_id is None
        assert app._staging_in_progress is False

    def test_terminate_releases_the_latch(self):
        app = _init_with_current_gen()
        assert app._staging_in_progress is True

        app.terminate()

        assert app._staging_in_progress is False
        assert app._verify_handle is None


# ----------------------------------------------------------------------
# (e) no ha_url -> legacy behaviour
# ----------------------------------------------------------------------


class TestVerificationDisabled:
    def test_warns_once_at_startup(self):
        app = _make_app(extra_args={"ha_url": ""})
        app.initialize()

        assert app._stage_verification_enabled is False
        assert len(_logs(app, "WARNING", "staging verification is DISABLED")) == 1

    def test_settle_delay_marks_pending_without_probing(self):
        app = _init_with_current_gen(extra_args={"ha_url": ""})
        gen = app._staging_gen_id
        app.create_task.reset_mock()

        timer_call = _last_run_in(app, app._on_stage_verify)
        assert timer_call.args[1] == app.stage_settle_delay_s
        app._on_stage_verify(dict(timer_call.kwargs))

        assert app.create_task.call_count == 0, "must not probe without an ha_url"
        assert app._pending_gen_id == gen
        assert app._staging_in_progress is False

    def test_no_warning_per_generation(self):
        app = _init_with_current_gen(extra_args={"ha_url": ""})
        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))
        app._on_tick({})
        app.log.reset_mock()

        _replace_source_files(app.source_dir, ["NEXT_1.jpg"])
        app._poll_for_changes(reason="poll")
        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))

        assert _logs(app, "WARNING", "staging verification is DISABLED") == []

    def test_unresolvable_ha_url_env_disables_verification_without_crashing(self):
        """A *_env pointer at an unset variable must not break initialize()."""
        app = _make_app(extra_args={"ha_url": "", "ha_url_env": "PFV_TEST_MISSING_URL"})
        os.environ.pop("PFV_TEST_MISSING_URL", None)

        app.initialize()

        assert app._ha_url == ""
        assert app._stage_verification_enabled is False
        assert len(_stage_calls(app)) == 1
