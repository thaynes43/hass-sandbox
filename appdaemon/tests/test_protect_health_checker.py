"""Unit tests for ProtectHealthChecker.

Mocks AppDaemon methods, HAProvisioner, and HaAdminClient — no real HA or
HTTP access required.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.protect_health_checker.protect_health_checker import (
    AVAILABILITY_CHECK,
    DISCOVERY_CHECK,
    CAMERA_EVENTS_CHECK,
    ENTRY_SENSORS_CHECK,
    EventSensor,
    GROUP_CAMERA,
    GROUP_ENTRY,
    ProtectHealthChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
    UTC,
    _parse_iso_utc,
    active_seconds_between,
)

MOD_PATH = (
    "health_checks.checker_apps.protect_health_checker.protect_health_checker"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha-test:8123",
    "ha_token_env": "TOKEN",
}


def _make_app(extra_args: dict | None = None) -> ProtectHealthChecker:
    ad = MagicMock()
    config = MagicMock()
    app = ProtectHealthChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()

    return app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _init_only(app: ProtectHealthChecker) -> None:
    """Call initialize without running async startup."""
    app.initialize()


def _tz_for_local_hour(target_hour: int) -> datetime.timezone:
    """Fixed-offset zone whose local wall-clock hour is target_hour right now.

    Makes active-window checks deterministic regardless of when CI runs.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    offset = (target_hour - now.hour) % 24
    if offset > 12:
        offset -= 24
    return datetime.timezone(datetime.timedelta(hours=offset))


def _make_mock_provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    prov.ensure_script = AsyncMock(return_value=False)
    return prov


def _make_mock_admin() -> MagicMock:
    admin = MagicMock()
    admin.render_template = AsyncMock(return_value="[]")
    admin.list_config_entries = AsyncMock(return_value=[])
    admin.reload_config_entry = AsyncMock(return_value=None)
    return admin


def _startup(
    app: ProtectHealthChecker,
    mock_prov: MagicMock | None = None,
    mock_admin: MagicMock | None = None,
) -> MagicMock:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    if mock_admin is None:
        mock_admin = _make_mock_admin()
    # _refresh_auto_repair_config awaits get_state
    app.get_state = AsyncMock(return_value=None)
    app.initialize()
    with patch(MOD_PATH + ".HAProvisioner", return_value=mock_prov), patch(
        MOD_PATH + ".HaAdminClient", return_value=mock_admin
    ):
        _run(app._async_startup())
    return mock_admin


# Reusable config-entry fixtures (entry ids are obvious placeholders)
IGNORED_ENTRY = {
    "entry_id": "test-entry-ignored",
    "domain": "unifiprotect",
    "title": "Ignored UDM discovery",
    "state": "not_loaded",
    "source": "ignore",
}
LOADED_ENTRY = {
    "entry_id": "test-entry-loaded",
    "domain": "unifiprotect",
    "title": "UniFi Protect (UDM SE)",
    "state": "loaded",
    "source": "user",
}


# ---------------------------------------------------------------------------
# Tests — active_seconds_between (pure function)
# ---------------------------------------------------------------------------

W_START = datetime.time(8, 0)
W_END = datetime.time(23, 0)


class TestActiveSecondsBetween:
    def test_same_day_partial_overlap(self):
        # 07:00 -> 09:00 against 08:00-23:00 = only 08:00-09:00 counts
        start = datetime.datetime(2026, 6, 10, 7, 0)
        end = datetime.datetime(2026, 6, 10, 9, 0)
        assert active_seconds_between(start, end, W_START, W_END) == 3600.0

    def test_fully_inside_window(self):
        start = datetime.datetime(2026, 6, 10, 10, 0)
        end = datetime.datetime(2026, 6, 10, 11, 30)
        assert active_seconds_between(start, end, W_START, W_END) == 5400.0

    def test_overnight_span(self):
        # 22:00 day1 -> 09:00 day2 with 08:00-23:00 window:
        # 22:00-23:00 (3600s) + 08:00-09:00 (3600s) = 7200s
        start = datetime.datetime(2026, 6, 10, 22, 0)
        end = datetime.datetime(2026, 6, 11, 9, 0)
        assert active_seconds_between(start, end, W_START, W_END) == 7200.0

    def test_fully_outside_window_late_night(self):
        start = datetime.datetime(2026, 6, 10, 23, 15)
        end = datetime.datetime(2026, 6, 10, 23, 45)
        assert active_seconds_between(start, end, W_START, W_END) == 0.0

    def test_fully_outside_window_early_morning(self):
        start = datetime.datetime(2026, 6, 10, 5, 0)
        end = datetime.datetime(2026, 6, 10, 7, 30)
        assert active_seconds_between(start, end, W_START, W_END) == 0.0

    def test_end_equal_to_start_is_zero(self):
        t = datetime.datetime(2026, 6, 10, 12, 0)
        assert active_seconds_between(t, t, W_START, W_END) == 0.0

    def test_end_before_start_is_zero(self):
        start = datetime.datetime(2026, 6, 10, 12, 0)
        end = datetime.datetime(2026, 6, 10, 11, 0)
        assert active_seconds_between(start, end, W_START, W_END) == 0.0

    def test_inverted_window_is_zero(self):
        start = datetime.datetime(2026, 6, 10, 10, 0)
        end = datetime.datetime(2026, 6, 10, 12, 0)
        assert (
            active_seconds_between(
                start, end, datetime.time(23, 0), datetime.time(8, 0)
            )
            == 0.0
        )

    def test_multi_day(self):
        # day1 10:00 -> day3 12:00 with 08:00-23:00 window:
        # day1: 10:00-23:00 = 13h, day2: 08:00-23:00 = 15h, day3: 08:00-12:00 = 4h
        start = datetime.datetime(2026, 6, 10, 10, 0)
        end = datetime.datetime(2026, 6, 12, 12, 0)
        assert active_seconds_between(start, end, W_START, W_END) == 32 * 3600.0

    def test_aware_datetimes(self):
        tz = datetime.timezone(datetime.timedelta(hours=-4))
        start = datetime.datetime(2026, 6, 10, 7, 0, tzinfo=tz)
        end = datetime.datetime(2026, 6, 10, 9, 0, tzinfo=tz)
        assert active_seconds_between(start, end, W_START, W_END) == 3600.0


# ---------------------------------------------------------------------------
# Tests — _parse_iso_utc (pure function)
# ---------------------------------------------------------------------------

class TestParseIsoUtc:
    def test_aware_iso_string(self):
        dt = _parse_iso_utc("2026-06-10T08:00:00-04:00")
        assert dt == datetime.datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        assert dt.utcoffset() == datetime.timedelta(0)

    def test_aware_utc_iso_string(self):
        dt = _parse_iso_utc("2026-06-10T12:34:56+00:00")
        assert dt == datetime.datetime(2026, 6, 10, 12, 34, 56, tzinfo=UTC)

    def test_naive_string_assumed_utc(self):
        dt = _parse_iso_utc("2026-06-10T12:00:00")
        assert dt == datetime.datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

    def test_datetime_passthrough_aware(self):
        tz = datetime.timezone(datetime.timedelta(hours=-4))
        value = datetime.datetime(2026, 6, 10, 8, 0, tzinfo=tz)
        dt = _parse_iso_utc(value)
        assert dt == datetime.datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

    def test_datetime_passthrough_naive_assumed_utc(self):
        value = datetime.datetime(2026, 6, 10, 12, 0)
        dt = _parse_iso_utc(value)
        assert dt == datetime.datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

    def test_garbage_returns_none(self):
        assert _parse_iso_utc("not-a-timestamp") is None

    def test_empty_string_returns_none(self):
        assert _parse_iso_utc("") is None

    def test_none_returns_none(self):
        assert _parse_iso_utc(None) is None


# ---------------------------------------------------------------------------
# Tests — initialize
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_defaults(self):
        app = _make_app()
        _init_only(app)

        assert app._checker_id == "protect"
        assert app._checker_name == "UniFi Protect"
        assert app._integration_domain == "unifiprotect"
        assert app._stale_after_s == 10800
        assert app._reload_cooldown_s == 3600
        assert app._auto_repair_enabled_default is True
        assert app._cached_auto_repair_enabled is True
        assert app._auto_repair_delay_min_default == 1
        assert app._active_start == datetime.time(8, 0)
        assert app._active_end == datetime.time(23, 0)
        assert app._check_interval_s == 300
        assert app._repair_status == REPAIR_IDLE
        assert app._frozen is False
        assert app._event_baseline is None
        assert app._motion_entities == []
        app.run_in.assert_called_once()

    def test_config_overrides(self):
        app = _make_app(
            {
                "checker_id": "protect_test",
                "checker_name": "Test Protect",
                "integration_domain": "testprotect",
                "stale_after_s": 7200,
                "reload_cooldown_s": 900,
                "active_start": "09:30",
                "active_end": "21:15",
                "auto_repair_enabled_default": False,
                "auto_repair_delay_min_default": 5,
                "check_interval_s": 60,
                "repair_recovery_wait_s": 120,
                "motion_entities": ["binary_sensor.cam_a_motion"],
            }
        )
        _init_only(app)

        assert app._checker_id == "protect_test"
        assert app._checker_name == "Test Protect"
        assert app._integration_domain == "testprotect"
        assert app._stale_after_s == 7200
        assert app._reload_cooldown_s == 900
        assert app._active_start == datetime.time(9, 30)
        assert app._active_end == datetime.time(21, 15)
        assert app._auto_repair_enabled_default is False
        assert app._cached_auto_repair_enabled is False
        assert app._auto_repair_delay_min_default == 5
        assert app._cached_auto_repair_delay_min == 5
        assert app._check_interval_s == 60
        assert app._repair_recovery_wait_s == 120
        assert app._motion_entities == ["binary_sensor.cam_a_motion"]

    def test_bad_active_hours_fall_back_to_defaults(self):
        app = _make_app({"active_start": "nonsense", "active_end": "25"})
        _init_only(app)
        assert app._active_start == datetime.time(8, 0)
        assert app._active_end == datetime.time(23, 0)


# ---------------------------------------------------------------------------
# Tests — Lifecycle / registration
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_startup_provisions_helpers_and_creates_admin(self):
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_admin = _startup(app, mock_prov)
        # input_boolean + input_number helpers
        assert mock_prov.ensure_helper.call_count == 2
        assert app._admin is mock_admin

    def test_startup_passes_url_and_token_env_to_admin(self):
        app = _make_app()
        app.get_state = AsyncMock(return_value=None)
        app.initialize()
        with patch(
            MOD_PATH + ".HAProvisioner", return_value=_make_mock_provisioner()
        ), patch(
            MOD_PATH + ".HaAdminClient", return_value=_make_mock_admin()
        ) as admin_cls:
            _run(app._async_startup())
        admin_cls.assert_called_once_with(
            ha_url="http://ha-test:8123", ha_token_env="TOKEN"
        )

    def test_startup_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names
        assert "health_check_repair_protect" in event_names

    def test_startup_without_admin_config_leaves_admin_none(self):
        app = _make_app()
        app.args = {}  # no ha_url / ha_token_env
        app.get_state = AsyncMock(return_value=None)
        app.initialize()
        _run(app._async_startup())
        assert app._admin is None
        # Registration still happens
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1

    def test_register_payload_supports_repair_and_alerting(self):
        alerting = {"severity": "critical", "page": True}
        app = _make_app({"alerting": alerting})
        _init_only(app)
        app._register()

        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is True
        assert payload["checker_id"] == "protect"
        assert payload["check_names"] == [
            DISCOVERY_CHECK,
            AVAILABILITY_CHECK,
            CAMERA_EVENTS_CHECK,
            ENTRY_SENSORS_CHECK,
        ]
        assert payload["alerting"] == alerting
        assert payload["repair_state"]["status"] == REPAIR_IDLE

    def test_register_payload_omits_alerting_when_unconfigured(self):
        app = _make_app()
        _init_only(app)
        app._register()
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "alerting" not in payload


# ---------------------------------------------------------------------------
# Tests — Event Stream check
# ---------------------------------------------------------------------------

class TestEventStream:
    def test_fresh_event_ok(self):
        app = _make_app()
        _init_only(app)
        app._tz = _tz_for_local_hour(12)  # mid-window
        now_utc = datetime.datetime.now(UTC)
        sensors = [
            (
                "binary_sensor.driveway_motion",
                now_utc - datetime.timedelta(seconds=60),
            ),
            (
                "binary_sensor.porch_person_detected",
                now_utc - datetime.timedelta(seconds=600),
            ),
        ]
        result = app._check_event_stream(sensors)
        assert result["status"] == "ok"
        assert result["name"] == CAMERA_EVENTS_CHECK
        assert "driveway_motion" in result["detail"]
        assert app._frozen is False

    def test_stale_in_window_goes_critical_and_freezes(self):
        app = _make_app()
        _init_only(app)
        app._tz = _tz_for_local_hour(12)  # local noon, window 08:00-23:00
        now_utc = datetime.datetime.now(UTC)
        newest = now_utc - datetime.timedelta(hours=5)  # >= 4h of active time
        sensors = [
            ("binary_sensor.driveway_motion", newest),
            (
                "binary_sensor.porch_person_detected",
                now_utc - datetime.timedelta(hours=6),
            ),
        ]
        result = app._check_event_stream(sensors)
        assert result["status"] == "critical"
        assert "No events for" in result["detail"]
        assert app._frozen is True
        assert app._event_baseline == newest
        assert app._frozen_since is not None

    def test_stale_outside_window_stays_ok(self):
        app = _make_app()
        _init_only(app)
        app._tz = _tz_for_local_hour(2)  # local 02:xx — outside 08:00-23:00
        now_utc = datetime.datetime.now(UTC)
        # ~8.5h of active time yesterday — far over threshold, but it's night
        newest = now_utc - datetime.timedelta(hours=12)
        sensors = [("binary_sensor.driveway_motion", newest)]
        result = app._check_event_stream(sensors)
        assert result["status"] == "ok"
        assert app._frozen is False

    def test_overnight_gap_within_active_budget_ok(self):
        app = _make_app()
        _init_only(app)
        app._tz = _tz_for_local_hour(10)  # local 10:xx
        now_utc = datetime.datetime.now(UTC)
        # 10h wall age -> newest at local midnight; only ~2h of the
        # 08:00-23:00 window has elapsed since, under the 3h threshold.
        newest = now_utc - datetime.timedelta(hours=10)
        sensors = [("binary_sensor.driveway_motion", newest)]
        result = app._check_event_stream(sensors)
        assert result["status"] == "ok"
        assert app._frozen is False

    def test_empty_sensor_list_unknown(self):
        app = _make_app()
        _init_only(app)
        result = app._check_event_stream([])
        assert result["status"] == "unknown"
        assert result["name"] == CAMERA_EVENTS_CHECK

    def test_sensors_without_timestamps_unknown(self):
        app = _make_app()
        _init_only(app)
        result = app._check_event_stream(
            [("binary_sensor.driveway_motion", None)]
        )
        assert result["status"] == "unknown"


# ---------------------------------------------------------------------------
# Tests — Frozen state machine
# ---------------------------------------------------------------------------

class TestFrozenBehavior:
    def test_frozen_stays_critical_despite_fresh_wall_age(self):
        """The post-reload trap: re-registration gives every sensor a fresh
        last_changed, but none are newer than the settle baseline — the
        check must stay critical, not trust the small wall age."""
        app = _make_app()
        _init_only(app)
        app._tz = _tz_for_local_hour(12)
        now_utc = datetime.datetime.now(UTC)
        app._frozen = True
        app._frozen_since = now_utc - datetime.timedelta(hours=2)
        # Baseline is reload-time + settle window (in the future)
        app._event_baseline = now_utc + datetime.timedelta(seconds=60)
        sensors = [
            (
                "binary_sensor.driveway_motion",
                now_utc - datetime.timedelta(seconds=5),  # tiny wall age
            )
        ]
        result = app._check_event_stream(sensors)
        assert result["status"] == "critical"
        assert "frozen" in result["detail"].lower()
        assert app._frozen is True

    def test_unfreezes_when_event_newer_than_baseline(self):
        app = _make_app()
        _init_only(app)
        now_utc = datetime.datetime.now(UTC)
        app._frozen = True
        app._frozen_since = now_utc - datetime.timedelta(hours=2)
        app._event_baseline = now_utc - datetime.timedelta(minutes=10)
        sensors = [
            (
                "binary_sensor.driveway_motion",
                now_utc - datetime.timedelta(seconds=5),
            )
        ]
        result = app._check_event_stream(sensors)
        assert result["status"] == "ok"
        assert "resumed" in result["detail"].lower()
        assert app._frozen is False
        assert app._event_baseline is None
        assert app._frozen_since is None

    def test_frozen_detail_mentions_auto_heal_failed(self):
        app = _make_app()
        _init_only(app)
        now_utc = datetime.datetime.now(UTC)
        app._frozen = True
        app._frozen_since = now_utc - datetime.timedelta(hours=3)
        app._event_baseline = now_utc + datetime.timedelta(seconds=60)
        app._repair_status = REPAIR_FAILED
        sensors = [
            (
                "binary_sensor.driveway_motion",
                now_utc - datetime.timedelta(seconds=5),
            )
        ]
        result = app._check_event_stream(sensors)
        assert result["status"] == "critical"
        assert "auto-heal failed" in result["detail"]

    def test_frozen_with_missing_baseline_adopts_newest(self):
        """Defensive path: frozen with no baseline adopts the newest ts."""
        app = _make_app()
        _init_only(app)
        now_utc = datetime.datetime.now(UTC)
        app._frozen = True
        app._frozen_since = now_utc - datetime.timedelta(hours=1)
        app._event_baseline = None
        newest = now_utc - datetime.timedelta(minutes=30)
        result = app._check_event_stream(
            [("binary_sensor.driveway_motion", newest)]
        )
        assert result["status"] == "critical"
        assert app._event_baseline == newest


# ---------------------------------------------------------------------------
# Tests — Sensor discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def _recent_iso(self, minutes: int = 0) -> str:
        return (
            datetime.datetime.now(UTC) - datetime.timedelta(minutes=minutes)
        ).isoformat()

    def test_template_discovery_filters_event_sensors(self):
        app = _make_app()
        _init_only(app)
        rows = [
            # Camera channels
            [
                "binary_sensor.g4_doorbell_motion",
                "motion", "off", self._recent_iso(10), "dev_cam1",
            ],
            # Smart detections render device_class as the string "None"
            [
                "binary_sensor.g4_doorbell_person_detected",
                "None", "off", self._recent_iso(5), "dev_cam1",
            ],
            # _detected wins even with a non-motion device_class
            [
                "binary_sensor.backyard_doorbell_detected",
                "occupancy", "off", self._recent_iso(7), "dev_cam2",
            ],
            # USL entry sensor: door channel marks the device as entry;
            # its motion + door channels are entry events, tamper dropped
            [
                "binary_sensor.usl_kitchen_motion",
                "motion", "off", self._recent_iso(20), "dev_usl1",
            ],
            [
                "binary_sensor.usl_kitchen_contact",
                "door", "off", self._recent_iso(3), "dev_usl1",
            ],
            [
                "binary_sensor.usl_kitchen_tamper",
                "tamper", "off", self._recent_iso(3), "dev_usl1",
            ],
            # All of these must be dropped:
            ["binary_sensor.g4_doorbell_is_dark", "None", "off", self._recent_iso(60), "dev_cam1"],
            ["binary_sensor.nvr_storage_problem", "problem", "off", self._recent_iso(2), "dev_nvr"],
            ["binary_sensor.garage_occupancy", "occupancy", "off", self._recent_iso(1), "dev_cam3"],
        ]
        admin = _make_mock_admin()
        admin.render_template = AsyncMock(return_value=json.dumps(rows))
        app._admin = admin

        sensors, result = _run(app._discover_sensors())

        by_id = {s.entity_id: s for s in sensors}
        assert set(by_id) == {
            "binary_sensor.g4_doorbell_motion",
            "binary_sensor.g4_doorbell_person_detected",
            "binary_sensor.backyard_doorbell_detected",
            "binary_sensor.usl_kitchen_motion",
            "binary_sensor.usl_kitchen_contact",
        }
        # Device-based grouping: USL channels (incl. its motion) are entry
        assert by_id["binary_sensor.usl_kitchen_motion"].group == GROUP_ENTRY
        assert by_id["binary_sensor.usl_kitchen_contact"].group == GROUP_ENTRY
        assert by_id["binary_sensor.g4_doorbell_motion"].group == GROUP_CAMERA
        assert by_id["binary_sensor.backyard_doorbell_detected"].group == GROUP_CAMERA
        assert result["status"] == "ok"
        assert result["name"] == DISCOVERY_CHECK
        assert "5 event sensors" in result["detail"]
        assert "3 camera, 2 entry" in result["detail"]
        # Caches: _sensors holds the CAMERA group; groups map holds all
        assert app._sensors == [
            "binary_sensor.g4_doorbell_motion",
            "binary_sensor.g4_doorbell_person_detected",
            "binary_sensor.backyard_doorbell_detected",
        ]
        assert app._sensor_groups["binary_sensor.usl_kitchen_contact"] == GROUP_ENTRY
        # Timestamps parsed to aware datetimes
        assert all(
            s.last_changed is not None and s.last_changed.tzinfo is not None
            for s in sensors
        )
        # Template targets the configured integration domain
        template = admin.render_template.call_args[0][0]
        assert "integration_entities('unifiprotect')" in template

    def test_template_discovery_no_sensors_critical(self):
        app = _make_app()
        _init_only(app)
        admin = _make_mock_admin()
        admin.render_template = AsyncMock(return_value="[]")
        app._admin = admin
        sensors, result = _run(app._discover_sensors())
        assert sensors == []
        assert result["status"] == "critical"
        assert "No event sensors found" in result["detail"]

    def test_discovery_failure_with_cache_warns_and_falls_back(self):
        app = _make_app()
        _init_only(app)
        admin = _make_mock_admin()
        admin.render_template = AsyncMock(
            side_effect=RuntimeError("registry down")
        )
        app._admin = admin
        app._sensor_groups = {"binary_sensor.cached_motion": GROUP_CAMERA}
        recent = self._recent_iso(1)
        app.get_state = AsyncMock(
            return_value={"state": "off", "last_changed": recent}
        )

        sensors, result = _run(app._discover_sensors())

        assert result["status"] == "warning"
        assert "cached" in result["detail"]
        assert [s.entity_id for s in sensors] == ["binary_sensor.cached_motion"]
        assert sensors[0].last_changed == _parse_iso_utc(recent)
        assert sensors[0].group == GROUP_CAMERA
        app.get_state.assert_awaited_once_with(
            "binary_sensor.cached_motion", attribute="all"
        )

    def test_discovery_failure_without_cache_critical(self):
        app = _make_app()
        _init_only(app)
        admin = _make_mock_admin()
        admin.render_template = AsyncMock(side_effect=RuntimeError("boom"))
        app._admin = admin
        app._sensor_groups = {}
        sensors, result = _run(app._discover_sensors())
        assert sensors == []
        assert result["status"] == "critical"
        assert "Discovery failed" in result["detail"]

    def test_no_admin_and_no_override_critical(self):
        app = _make_app()
        _init_only(app)
        app._admin = None
        sensors, result = _run(app._discover_sensors())
        assert sensors == []
        assert result["status"] == "critical"
        assert "No ha_url/ha_token_env" in result["detail"]

    def test_motion_entities_override_skips_admin(self):
        app = _make_app(
            {
                "motion_entities": [
                    "binary_sensor.cam_a_motion",
                    "binary_sensor.cam_b_motion",
                ]
            }
        )
        _init_only(app)
        admin = _make_mock_admin()
        app._admin = admin
        recent = self._recent_iso(1)
        app.get_state = AsyncMock(
            return_value={"state": "off", "last_changed": recent}
        )

        sensors, result = _run(app._discover_sensors())

        assert result["status"] == "ok"
        assert "2 configured event sensors" in result["detail"]
        assert [s.entity_id for s in sensors] == [
            "binary_sensor.cam_a_motion",
            "binary_sensor.cam_b_motion",
        ]
        # Override entities are all treated as the camera group
        assert all(s.group == GROUP_CAMERA for s in sensors)
        assert app._sensors == [s.entity_id for s in sensors]
        admin.render_template.assert_not_called()
        admin.list_config_entries.assert_not_called()

    def test_motion_entities_override_all_missing_critical(self):
        app = _make_app(
            {
                "motion_entities": [
                    "binary_sensor.cam_a_motion",
                    "binary_sensor.cam_b_motion",
                ]
            }
        )
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        sensors, result = _run(app._discover_sensors())
        assert result["status"] == "critical"
        assert "None of the 2 configured entities found" in result["detail"]


# ---------------------------------------------------------------------------
# Tests — Auto-repair state machine
# ---------------------------------------------------------------------------

CRITICAL_RESULTS = [
    {"name": DISCOVERY_CHECK, "status": "ok", "detail": "4 event sensors"},
    {
        "name": CAMERA_EVENTS_CHECK,
        "status": "critical",
        "detail": "No events for 4.0h of active hours",
    },
]

OK_RESULTS = [
    {"name": DISCOVERY_CHECK, "status": "ok", "detail": ""},
    {"name": CAMERA_EVENTS_CHECK, "status": "ok", "detail": ""},
]


class TestAutoRepairStateMachine:
    def _prepped(self, delay_min: int = 5) -> ProtectHealthChecker:
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = delay_min
        return app

    def test_first_critical_cycle_goes_pending_with_deadline(self):
        app = self._prepped(delay_min=5)
        app._evaluate_auto_repair(CRITICAL_RESULTS)
        assert app._repair_status == REPAIR_PENDING
        assert app._unhealthy_since is not None
        assert app._auto_repair_deadline is not None
        remaining = (
            app._auto_repair_deadline - datetime.datetime.now()
        ).total_seconds()
        assert 250 < remaining <= 300  # ~5 minutes out

    def test_idle_starts_repair_when_already_past_delay(self):
        app = self._prepped(delay_min=5)
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(
            minutes=10
        )
        with patch.object(app, "_start_repair") as start:
            app._evaluate_auto_repair(CRITICAL_RESULTS)
        start.assert_called_once()

    def test_pending_starts_repair_at_deadline(self):
        app = self._prepped()
        app._repair_status = REPAIR_PENDING
        app._auto_repair_deadline = datetime.datetime.now() - datetime.timedelta(
            seconds=1
        )
        with patch.object(app, "_start_repair") as start:
            app._evaluate_auto_repair(CRITICAL_RESULTS)
        start.assert_called_once()

    def test_reload_cooldown_pushes_deadline_out(self):
        app = self._prepped(delay_min=1)
        # Past the delay, but a reload just happened (cooldown 3600s)
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(
            minutes=10
        )
        app._last_reload_at = datetime.datetime.now()
        with patch.object(app, "_start_repair") as start:
            app._evaluate_auto_repair(CRITICAL_RESULTS)
        start.assert_not_called()
        assert app._repair_status == REPAIR_PENDING
        remaining = (
            app._auto_repair_deadline - datetime.datetime.now()
        ).total_seconds()
        assert 3500 < remaining <= 3600  # ~the reload cooldown

    def test_failed_does_not_retry_during_cooldown(self):
        app = self._prepped()
        app._repair_status = REPAIR_FAILED
        app._auto_repair_deadline = None
        app._last_reload_at = datetime.datetime.now()
        with patch.object(app, "_start_repair") as start:
            app._evaluate_auto_repair(CRITICAL_RESULTS)
        start.assert_not_called()
        assert app._repair_status == REPAIR_FAILED
        assert app._auto_repair_deadline is not None
        assert "retry at" in app._repair_detail

    def test_failed_retries_after_cooldown_passes(self):
        app = self._prepped()
        app._repair_status = REPAIR_FAILED
        app._auto_repair_deadline = None
        app._last_reload_at = datetime.datetime.now() - datetime.timedelta(
            seconds=7200
        )
        with patch.object(app, "_start_repair") as start:
            app._evaluate_auto_repair(CRITICAL_RESULTS)
        start.assert_called_once()

    def test_all_ok_resets_pending_to_idle(self):
        app = self._prepped()
        app._repair_status = REPAIR_PENDING
        app._repair_detail = "Auto-repair at soon"
        app._auto_repair_deadline = datetime.datetime.now() + datetime.timedelta(
            minutes=5
        )
        app._unhealthy_since = datetime.datetime.now()
        app._evaluate_auto_repair(OK_RESULTS)
        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""
        assert app._auto_repair_deadline is None
        assert app._unhealthy_since is None

    def test_all_ok_resets_failed_to_idle(self):
        app = self._prepped()
        app._repair_status = REPAIR_FAILED
        app._repair_detail = "No events within 600s of reload"
        app._unhealthy_since = datetime.datetime.now()
        app._evaluate_auto_repair(OK_RESULTS)
        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""
        assert app._unhealthy_since is None

    def test_disabled_tracks_unhealthy_but_stays_idle(self):
        app = self._prepped()
        app._cached_auto_repair_enabled = False
        app._evaluate_auto_repair(CRITICAL_RESULTS)
        assert app._repair_status == REPAIR_IDLE
        assert app._unhealthy_since is not None
        assert app._auto_repair_deadline is None

    def test_warning_only_does_not_trigger_repair(self):
        app = self._prepped()
        results = [
            {"name": DISCOVERY_CHECK, "status": "warning", "detail": "cached"},
            {"name": CAMERA_EVENTS_CHECK, "status": "ok", "detail": ""},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_IDLE
        assert app._unhealthy_since is None


# ---------------------------------------------------------------------------
# Tests — Repair execution
# ---------------------------------------------------------------------------

class TestRepairExecution:
    def test_execute_repair_reloads_loaded_entry_and_succeeds(self):
        app = _make_app()
        _init_only(app)
        app._repair_recovery_wait_s = 0.05
        app._repair_status = REPAIR_IN_PROGRESS
        app._frozen = True
        app._frozen_since = datetime.datetime.now(UTC) - datetime.timedelta(
            hours=4
        )
        app._event_baseline = datetime.datetime.now(UTC) - datetime.timedelta(
            hours=4
        )
        admin = _make_mock_admin()
        admin.list_config_entries = AsyncMock(
            return_value=[IGNORED_ENTRY, LOADED_ENTRY]
        )
        app._admin = admin
        app._any_event_after = AsyncMock(
            return_value="binary_sensor.driveway_motion"
        )

        with patch(MOD_PATH + ".REPAIR_POLL_INTERVAL_S", 0.01):
            _run(app._execute_repair())

        # The ignored not_loaded entry must be skipped
        admin.reload_config_entry.assert_awaited_once_with("test-entry-loaded")
        assert app._repair_status == REPAIR_SUCCESS
        assert "Events resumed" in app._repair_detail
        assert "driveway_motion" in app._repair_detail
        assert app._frozen is False
        assert app._event_baseline is None
        assert app._last_reload_at is not None
        app._any_event_after.assert_awaited()

    def test_execute_repair_no_loaded_entry_fails(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        admin = _make_mock_admin()
        admin.list_config_entries = AsyncMock(return_value=[IGNORED_ENTRY])
        app._admin = admin

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert "No loaded" in app._repair_detail
        admin.reload_config_entry.assert_not_awaited()

    def test_execute_repair_recovery_timeout_fails_and_stays_frozen(self):
        app = _make_app()
        _init_only(app)
        # elapsed accumulates the patched poll interval (0.01) per loop
        app._repair_recovery_wait_s = 0.03
        app._repair_status = REPAIR_IN_PROGRESS
        app._frozen = True
        app._event_baseline = datetime.datetime.now(UTC) - datetime.timedelta(
            hours=4
        )
        admin = _make_mock_admin()
        admin.list_config_entries = AsyncMock(
            return_value=[IGNORED_ENTRY, LOADED_ENTRY]
        )
        app._admin = admin
        app._any_event_after = AsyncMock(return_value=None)

        with patch(MOD_PATH + ".REPAIR_POLL_INTERVAL_S", 0.01):
            _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert "No events within" in app._repair_detail
        assert app._frozen is True
        # Baseline was pushed to reload-time + settle window (the future)
        assert app._event_baseline > datetime.datetime.now(UTC)

    def test_execute_repair_error_fails(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        admin = _make_mock_admin()
        admin.list_config_entries = AsyncMock(
            return_value=[LOADED_ENTRY]
        )
        admin.reload_config_entry = AsyncMock(
            side_effect=RuntimeError("HA returned 500")
        )
        app._admin = admin

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert "Repair error" in app._repair_detail

    def test_start_repair_without_admin_fails_immediately(self):
        app = _make_app()
        _init_only(app)
        app._admin = None
        app._start_repair()
        assert app._repair_status == REPAIR_FAILED
        assert "No HA admin access" in app._repair_detail


# ---------------------------------------------------------------------------
# Tests — Repair command handler
# ---------------------------------------------------------------------------

class TestRepairCommandHandler:
    def test_manual_start_repair_bypasses_cooldown(self):
        """Manual repair from the card must start even mid-cooldown."""
        app = _make_app()
        _init_only(app)
        app._admin = _make_mock_admin()
        app._last_reload_at = datetime.datetime.now()  # cooldown fully active

        app._on_repair_command(
            "health_check_repair_protect",
            {"action": "start_repair"},
            {},
        )

        assert app._repair_status == REPAIR_IN_PROGRESS
        app.create_task.assert_called_once()
        # A repair-status-only report is fired
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) >= 1

    def test_manual_start_repair_invokes_start_repair(self):
        app = _make_app()
        _init_only(app)
        app._last_reload_at = datetime.datetime.now()
        with patch.object(app, "_start_repair") as start:
            app._on_repair_command(
                "health_check_repair_protect",
                {"action": "start_repair"},
                {},
            )
        start.assert_called_once()

    def test_cancel_repair_while_pending(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._repair_detail = "Auto-repair at soon"
        app._auto_repair_deadline = datetime.datetime.now() + datetime.timedelta(
            minutes=5
        )
        app._unhealthy_since = datetime.datetime.now()

        app._on_repair_command(
            "health_check_repair_protect",
            {"action": "cancel_repair"},
            {},
        )

        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""
        assert app._auto_repair_deadline is None
        assert app._unhealthy_since is None


# ---------------------------------------------------------------------------
# Tests — Reload race protection (concurrent check cycle during the reload)
# ---------------------------------------------------------------------------

class TestReloadRaceProtection:
    def test_frozen_check_does_not_unfreeze_while_repair_in_progress(self):
        """A check cycle that lands while the reload await is in flight sees
        re-registration timestamps newer than the stale baseline.  It must
        NOT unfreeze (that would resolve the page and mask a failed heal) —
        _execute_repair owns recovery detection while in progress."""
        app = _make_app()
        _init_only(app)
        app._tz = _tz_for_local_hour(12)
        now_utc = datetime.datetime.now(UTC)
        app._frozen = True
        app._frozen_since = now_utc - datetime.timedelta(hours=3)
        app._repair_status = REPAIR_IN_PROGRESS
        # Stale baseline from detection time — the race the guard closes
        app._event_baseline = now_utc - datetime.timedelta(hours=3)
        sensors = [
            (
                "binary_sensor.driveway_motion",
                now_utc - datetime.timedelta(seconds=2),  # re-registration ts
            )
        ]

        result = app._check_event_stream(sensors)

        assert result["status"] == "critical"
        assert "reload in progress" in result["detail"]
        assert app._frozen is True
        # Baseline untouched — _execute_repair manages it
        assert app._event_baseline == now_utc - datetime.timedelta(hours=3)

    def test_baseline_raised_before_reload_await(self):
        """Belt-and-braces for the same race: the event baseline must be in
        the future BEFORE the reload HTTP call starts, so re-registration
        timestamps produced during the await can never read as recovery."""
        app = _make_app()
        _init_only(app)
        app._repair_recovery_wait_s = 0.05
        app._repair_status = REPAIR_IN_PROGRESS
        app._frozen = True
        start_utc = datetime.datetime.now(UTC)
        app._frozen_since = start_utc - datetime.timedelta(hours=4)
        app._event_baseline = start_utc - datetime.timedelta(hours=4)

        captured = {}

        async def capture_reload(entry_id):
            captured["baseline_at_reload"] = app._event_baseline
            return {"require_restart": False}

        admin = _make_mock_admin()
        admin.list_config_entries = AsyncMock(
            return_value=[IGNORED_ENTRY, LOADED_ENTRY]
        )
        admin.reload_config_entry = AsyncMock(side_effect=capture_reload)
        app._admin = admin
        app._any_event_after = AsyncMock(
            return_value="binary_sensor.driveway_motion"
        )

        with patch(MOD_PATH + ".REPAIR_POLL_INTERVAL_S", 0.01):
            _run(app._execute_repair())

        assert "baseline_at_reload" in captured
        # Raised to now + settle (future) before the await — not the stale
        # detection-time value.
        assert captured["baseline_at_reload"] > start_utc
        assert app._repair_status == REPAIR_SUCCESS


# ---------------------------------------------------------------------------
# Tests — _any_event_after (the real implementation, unmocked)
# ---------------------------------------------------------------------------

class TestAnyEventAfterReal:
    def _app_with_states(self, states: dict):
        app = _make_app()
        _init_only(app)
        app._sensors = list(states.keys())

        async def fake_get_state(entity_id, attribute=None):
            value = states[entity_id]
            if isinstance(value, Exception):
                raise value
            return value

        app.get_state = MagicMock(side_effect=fake_get_state)
        return app

    def test_rejects_pre_baseline_accepts_post_baseline(self):
        """The single safety-critical verifier: re-registration timestamps
        (pre-baseline) must be rejected; only a genuinely newer event counts."""
        baseline = datetime.datetime.now(UTC)
        states = {
            "binary_sensor.a": {
                "state": "off",
                "last_changed": (
                    baseline - datetime.timedelta(seconds=30)
                ).isoformat(),
            },
            "binary_sensor.b": {
                "state": "off",
                "last_changed": (
                    baseline + datetime.timedelta(seconds=30)
                ).isoformat(),
            },
        }
        app = self._app_with_states(states)

        assert _run(app._any_event_after(baseline)) == "binary_sensor.b"

    def test_returns_none_when_all_pre_baseline(self):
        baseline = datetime.datetime.now(UTC)
        states = {
            "binary_sensor.a": {
                "last_changed": (
                    baseline - datetime.timedelta(seconds=30)
                ).isoformat()
            },
            "binary_sensor.b": {
                "last_changed": (
                    baseline - datetime.timedelta(seconds=1)
                ).isoformat()
            },
        }
        app = self._app_with_states(states)

        assert _run(app._any_event_after(baseline)) is None

    def test_skips_errors_missing_entities_and_bad_timestamps(self):
        baseline = datetime.datetime.now(UTC)
        states = {
            "binary_sensor.err": RuntimeError("HA hiccup"),
            "binary_sensor.gone": None,
            "binary_sensor.bad": {"state": "off", "last_changed": "not-a-timestamp"},
            "binary_sensor.unavail": {
                # Post-baseline transition INTO unavailable is NOT an event
                "state": "unavailable",
                "last_changed": (
                    baseline + datetime.timedelta(seconds=20)
                ).isoformat(),
            },
            "binary_sensor.good": {
                "state": "on",
                "last_changed": (
                    baseline + datetime.timedelta(seconds=10)
                ).isoformat(),
            },
        }
        app = self._app_with_states(states)

        assert _run(app._any_event_after(baseline)) == "binary_sensor.good"


# ---------------------------------------------------------------------------
# Tests — Sensor Availability fast path + Entry Sensors line item
# ---------------------------------------------------------------------------

def _sensor(
    entity_id: str,
    group: str = GROUP_CAMERA,
    state: str = "off",
    age_s: float = 60,
    device_id: str = "",
) -> EventSensor:
    return EventSensor(
        entity_id,
        state,
        datetime.datetime.now(UTC) - datetime.timedelta(seconds=age_s),
        group,
        device_id,
    )


def _prime(app, sensors, witnessed: bool = True) -> None:
    """Build the device maps the checks consume.

    With witnessed=True, dark devices are also marked as previously seen
    alive (simulating a device that was up earlier in this app's lifetime).
    """
    app._update_device_maps_from_sensors(sensors)
    if witnessed:
        app._devices_seen_alive |= set(app._device_live)


class TestAvailabilityCheck:
    def test_all_available_is_ok(self):
        app = _make_app()
        _init_only(app)
        sensors = [_sensor("binary_sensor.a"), _sensor("binary_sensor.b")]
        result = app._check_availability(sensors)
        assert result["status"] == "ok"
        assert result["name"] == AVAILABILITY_CHECK

    def test_mass_unavailable_past_grace_is_critical(self):
        """The 2026-06-11 outage: UNVR 401 -> every sensor unavailable.
        After the grace period this must page WITHOUT waiting out the 3h
        staleness threshold."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=1000)
            for i in range(10)
        ]
        result = app._check_availability(sensors)
        assert result["status"] == "critical"
        assert "10/10" in result["detail"]
        assert "connection lost" in result["detail"]

    def test_mass_unavailable_within_grace_is_ok(self):
        """A config-entry reload makes everything briefly unavailable —
        the grace period must absorb it (no self-triggering mid-repair)."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=30)
            for i in range(10)
        ]
        result = app._check_availability(sensors)
        assert result["status"] == "ok"

    def test_partial_unavailable_warns_only_for_witnessed_devices(self):
        """The 03:26 case: the USL subset drops while cameras keep working.
        Warns because this app saw the devices alive earlier — chronic darks
        (never witnessed) must NOT warn (separate test below)."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor(f"binary_sensor.cam{i}") for i in range(8)
        ] + [
            _sensor(
                f"binary_sensor.usl{i}",
                group=GROUP_ENTRY,
                state="unavailable",
                age_s=7200,
            )
            for i in range(2)
        ]
        _prime(app, sensors, witnessed=True)
        result = app._check_availability(sensors)
        assert result["status"] == "warning"
        assert "2 device(s) recently went offline" in result["detail"]
        assert "binary_sensor.usl0" in result["detail"]

    def test_chronic_dark_devices_never_warn(self):
        """Devices dark since before this app started (wireless cameras
        unplugged most of the year, disabled features) stay quiet — the ok
        detail discloses them instead."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor(f"binary_sensor.cam{i}") for i in range(8)
        ] + [
            _sensor(
                "binary_sensor.kitchen_g6_motion",
                state="unavailable",
                age_s=86_400 * 3,
            )
        ]
        _prime(app, sensors, witnessed=False)  # never seen alive
        result = app._check_availability(sensors)
        assert result["status"] == "ok"
        assert "8/9 event sensors available" in result["detail"]
        assert "expected-offline" in result["detail"]

    def test_disabled_channel_on_live_device_never_warns(self):
        """A dead channel whose sibling on the SAME device is alive is a
        disabled feature (USL motion off, audio detections off) — excluded
        everywhere, even though it was 'witnessed' via the device."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor("binary_sensor.usl_motion", group=GROUP_ENTRY,
                    state="unavailable", age_s=86_400 * 5, device_id="usl1"),
            _sensor("binary_sensor.usl_contact", group=GROUP_ENTRY,
                    device_id="usl1"),
            _sensor("binary_sensor.side_yard_barking_detected",
                    state="unavailable", age_s=86_400 * 5, device_id="cam1"),
            _sensor("binary_sensor.side_yard_motion", device_id="cam1"),
        ]
        _prime(app, sensors, witnessed=True)
        result = app._check_availability(sensors)
        assert result["status"] == "ok"
        assert "2/4 event sensors available" in result["detail"]

        entry = app._check_entry_sensors(sensors)
        assert entry["status"] == "ok"
        assert "1 entry sensors online" in entry["detail"]
        assert "1 disabled channels" in entry["detail"]

    def test_offline_warning_expires_after_warn_window(self):
        """Past the warn window the downtime is accepted as expected (e.g.
        a wireless camera brought back inside) and the warning clears."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor(f"binary_sensor.cam{i}") for i in range(5)
        ] + [
            _sensor(
                "binary_sensor.g6_vacation_motion",
                state="unavailable",
                age_s=app._availability_warn_window_s + 3600,
            )
        ]
        _prime(app, sensors, witnessed=True)
        result = app._check_availability(sensors)
        assert result["status"] == "ok"

    def test_unavailable_without_timestamp_respects_grace(self):
        """Entities missing from the state machine entirely (reload unload
        window) are timed from first observation — instant-down would
        bypass the grace and false-page mid-repair."""
        app = _make_app()
        _init_only(app)
        sensors = [
            EventSensor(f"binary_sensor.cam{i}", "unavailable", None, GROUP_CAMERA)
            for i in range(3)
        ]
        # First observation: grace clock starts now -> not down yet
        result = app._check_availability(sensors)
        assert result["status"] == "ok"

        # Backdate the first-seen times past the grace -> confirmed down
        backdated = datetime.datetime.now(UTC) - datetime.timedelta(seconds=1000)
        for entity_id in app._missing_since:
            app._missing_since[entity_id] = backdated
        result = app._check_availability(sensors)
        assert result["status"] == "critical"

    def test_latch_survives_reload_resetting_dwell_clocks(self):
        """Finding from review: a reload re-registers everything, resetting
        every last_changed. A FAILED heal must not false-resolve the page —
        once confirmed, only sensors actually coming back clears it."""
        app = _make_app()
        _init_only(app)
        # Confirmed outage (dwell past grace); devices were seen alive
        sensors = [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=1000)
            for i in range(10)
        ]
        _prime(app, sensors, witnessed=True)
        assert app._check_availability(sensors)["status"] == "critical"
        assert app._availability_down is True

        # Post-reload: still unavailable but with FRESH transition times
        fresh = [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=10)
            for i in range(10)
        ]
        _prime(app, fresh, witnessed=True)
        result = app._check_availability(fresh)
        assert result["status"] == "critical"  # latched — no false resolve
        assert app._availability_down is True

        # Genuine recovery: sensors actually available again
        recovered = [_sensor(f"binary_sensor.cam{i}") for i in range(10)]
        _prime(app, recovered)
        result = app._check_availability(recovered)
        assert result["status"] == "ok"
        assert app._availability_down is False

    def test_partial_recovery_holds_latch_at_warning_and_repages_on_relapse(self):
        """Review finding: after a reload partially recovers (some sensors
        back, the rest still down with FRESH timestamps), falling through to
        the dwell path would read false-ok for a grace period and reset the
        repair state. The latch must hold at warning until ALL sensors are
        back — and a relapse during recovery must re-page instantly."""
        app = _make_app()
        _init_only(app)
        # Confirmed outage; devices were seen alive earlier
        sensors = [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=1000)
            for i in range(10)
        ]
        _prime(app, sensors, witnessed=True)
        assert app._check_availability(sensors)["status"] == "critical"

        # Partial recovery: 2 back, 8 still down with fresh post-reload ts
        partial = [_sensor(f"binary_sensor.cam{i}", age_s=10) for i in range(2)] + [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=10)
            for i in range(2, 10)
        ]
        _prime(app, partial, witnessed=True)
        result = app._check_availability(partial)
        assert result["status"] == "warning"  # NOT ok
        assert "recovering" in result["detail"]
        assert "8 device(s) still offline" in result["detail"]
        assert app._availability_down is True  # latch held

        # Relapse during recovery: instantly critical, no dwell re-wait
        relapse = [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=5)
            for i in range(10)
        ]
        _prime(app, relapse, witnessed=True)
        result = app._check_availability(relapse)
        assert result["status"] == "critical"
        assert app._availability_down is True

        # Full recovery clears the latch
        recovered = [_sensor(f"binary_sensor.cam{i}") for i in range(10)]
        _prime(app, recovered)
        assert app._check_availability(recovered)["status"] == "ok"
        assert app._availability_down is False

    def test_chronic_dark_device_does_not_hold_latch_open(self):
        """Recovery from a real outage must complete even though a
        chronically-dark device (never witnessed alive) is still dark —
        otherwise the latch would hold a warning forever."""
        app = _make_app()
        _init_only(app)
        chronic = _sensor(
            "binary_sensor.kitchen_g6_motion",
            state="unavailable",
            age_s=86_400 * 3,
        )
        alive = [_sensor(f"binary_sensor.cam{i}") for i in range(9)]
        # Outage: the 9 witnessed devices go dark (chronic was never alive)
        _prime(app, alive + [chronic], witnessed=False)
        app._devices_seen_alive |= {f"binary_sensor.cam{i}" for i in range(9)}
        outage = [
            _sensor(f"binary_sensor.cam{i}", state="unavailable", age_s=1000)
            for i in range(9)
        ] + [chronic]
        app._update_device_maps_from_sensors(outage)
        assert app._check_availability(outage)["status"] == "critical"

        # Recovery: witnessed devices return; chronic stays dark
        recovered = alive + [chronic]
        app._update_device_maps_from_sensors(recovered)
        result = app._check_availability(recovered)
        assert result["status"] == "ok"
        assert app._availability_down is False
        assert "expected-offline" in result["detail"]

    def test_no_sensors_unknown(self):
        app = _make_app()
        _init_only(app)
        assert app._check_availability([])["status"] == "unknown"

    def test_critical_pct_threshold_respected(self):
        app = _make_app({"availability_critical_pct": 50})
        _init_only(app)
        sensors = [
            _sensor(f"binary_sensor.up{i}") for i in range(4)
        ] + [
            _sensor(
                f"binary_sensor.down{i}", state="unavailable", age_s=2000
            )
            for i in range(6)
        ]
        result = app._check_availability(sensors)
        assert result["status"] == "critical"


class TestEntrySensorsCheck:
    def test_no_entry_sensors_is_ok(self):
        app = _make_app()
        _init_only(app)
        sensors = [_sensor("binary_sensor.cam_motion")]
        result = app._check_entry_sensors(sensors)
        assert result["status"] == "ok"
        assert "No entry sensors" in result["detail"]
        assert result["name"] == ENTRY_SENSORS_CHECK

    def test_available_entry_sensors_show_newest_event(self):
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor("binary_sensor.cam_motion"),
            _sensor("binary_sensor.usl_motion", group=GROUP_ENTRY, age_s=300),
            _sensor("binary_sensor.usl_contact", group=GROUP_ENTRY, age_s=7200),
        ]
        _prime(app, sensors)
        result = app._check_entry_sensors(sensors)
        assert result["status"] == "ok"
        assert "2 entry sensors online" in result["detail"]
        assert "binary_sensor.usl_motion" in result["detail"]

    def test_quiet_entry_sensors_never_page(self):
        """A door not opening for many hours is normal — must stay ok."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor(
                "binary_sensor.usl_contact",
                group=GROUP_ENTRY,
                age_s=60 * 60 * 30,  # 30h since last event, still online
            )
        ]
        _prime(app, sensors)
        result = app._check_entry_sensors(sensors)
        assert result["status"] == "ok"

    def test_fully_dark_entry_device_warns_persistently(self):
        """A whole USL gone dark warns — no witnessed gate, no expiry
        (security sensors are never expected offline). Chronic-dark
        included deliberately."""
        app = _make_app()
        _init_only(app)
        sensors = [
            _sensor(
                "binary_sensor.usl_dead_motion",
                group=GROUP_ENTRY,
                state="unavailable",
                age_s=86_400 * 9,  # dark for over a week — still warns
                device_id="usl_dead",
            ),
            _sensor(
                "binary_sensor.usl_dead_contact",
                group=GROUP_ENTRY,
                state="unavailable",
                age_s=86_400 * 9,
                device_id="usl_dead",
            ),
            _sensor(
                "binary_sensor.usl_ok_contact",
                group=GROUP_ENTRY,
                device_id="usl_ok",
            ),
        ]
        _prime(app, sensors, witnessed=False)
        result = app._check_entry_sensors(sensors)
        assert result["status"] == "warning"
        assert "1/2 entry sensors offline" in result["detail"]
        assert "binary_sensor.usl_dead_contact" in result["detail"]


class TestFrozenWithUnavailableSensors:
    def test_frozen_stays_critical_when_cameras_go_unavailable(self):
        """Freeze firing, then the integration dies on top of it: the empty
        available-camera list must keep the check critical, not flip it to
        unknown (which would resolve the page)."""
        app = _make_app()
        _init_only(app)
        app._frozen = True
        app._frozen_since = datetime.datetime.now(UTC) - datetime.timedelta(
            hours=1
        )
        app._event_baseline = datetime.datetime.now(UTC)

        result = app._check_event_stream([])

        assert result["status"] == "critical"
        assert "unavailable" in result["detail"]
        assert app._frozen is True

    def test_run_checks_reports_four_results_and_excludes_unavailable(self):
        """End-to-end _run_checks: all sensors unavailable past grace ->
        Availability critical, Camera Events unknown (no fresh-timestamp
        false health), and the report carries all four checks."""
        app = _make_app()
        _init_only(app)
        rows = [
            [
                f"binary_sensor.cam{i}_motion",
                "motion",
                "unavailable",
                (
                    datetime.datetime.now(UTC) - datetime.timedelta(seconds=1000)
                ).isoformat(),
                f"dev{i}",
            ]
            for i in range(5)
        ]
        admin = _make_mock_admin()
        admin.render_template = AsyncMock(return_value=json.dumps(rows))
        app._admin = admin
        app.get_state = AsyncMock(return_value=None)

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        by_name = {r["name"]: r for r in payload["results"]}
        assert set(by_name) == {
            DISCOVERY_CHECK,
            AVAILABILITY_CHECK,
            CAMERA_EVENTS_CHECK,
            ENTRY_SENSORS_CHECK,
        }
        assert by_name[AVAILABILITY_CHECK]["status"] == "critical"
        # The unavailable transitions refreshed last_changed, but they are
        # NOT events — Camera Events must not report fresh health.
        assert by_name[CAMERA_EVENTS_CHECK]["status"] == "unknown"
        assert by_name[DISCOVERY_CHECK]["status"] == "ok"


class TestRepairStandDownOnWarnings:
    def test_chronic_warning_does_not_pin_success_state(self):
        """A lingering warning (e.g. USL sensor offline for days) must not
        leave repair stuck in SUCCESS — that guard would block auto-heal
        for the NEXT freeze."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._repair_status = REPAIR_SUCCESS

        warning_only = [
            {"name": CAMERA_EVENTS_CHECK, "status": "ok", "detail": ""},
            {"name": ENTRY_SENSORS_CHECK, "status": "warning", "detail": "1/2 down"},
        ]
        app._evaluate_auto_repair(warning_only)
        assert app._repair_status == REPAIR_IDLE

        # A new critical can now arm a repair again
        critical = [
            {"name": CAMERA_EVENTS_CHECK, "status": "critical", "detail": "frozen"},
            {"name": ENTRY_SENSORS_CHECK, "status": "warning", "detail": "1/2 down"},
        ]
        app._evaluate_auto_repair(critical)
        assert app._repair_status == REPAIR_PENDING

    def test_illusory_success_rearms_while_still_critical(self):
        """If checks are STILL critical after a 'successful' reload, the
        success was illusory — repair must re-arm (cooldown-paced), not pin
        SUCCESS forever."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._repair_status = REPAIR_SUCCESS
        app._last_reload_at = datetime.datetime.now()  # cooldown active

        critical = [
            {"name": AVAILABILITY_CHECK, "status": "critical", "detail": "down"},
        ]
        app._evaluate_auto_repair(critical)

        # Re-armed as pending with a cooldown-pushed deadline
        assert app._repair_status == REPAIR_PENDING
        assert app._auto_repair_deadline is not None
        assert app._auto_repair_deadline > datetime.datetime.now()

        # Once the cooldown lapses, the next critical cycle starts the repair
        app._last_reload_at = datetime.datetime.now() - datetime.timedelta(
            seconds=app._reload_cooldown_s + 1
        )
        app._auto_repair_deadline = datetime.datetime.now() - datetime.timedelta(
            seconds=1
        )
        with patch.object(app, "_start_repair") as start:
            app._evaluate_auto_repair(critical)
        start.assert_called_once()

    def test_pending_cancelled_when_criticals_clear_despite_warning(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._repair_status = REPAIR_PENDING
        app._auto_repair_deadline = datetime.datetime.now() + datetime.timedelta(
            minutes=5
        )
        app._unhealthy_since = datetime.datetime.now()

        app._evaluate_auto_repair(
            [{"name": CAMERA_EVENTS_CHECK, "status": "warning", "detail": ""}]
        )

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        assert app._unhealthy_since is None
