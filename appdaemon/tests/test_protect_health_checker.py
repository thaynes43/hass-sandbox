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
    DISCOVERY_CHECK,
    EVENT_STREAM_CHECK,
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
        assert payload["check_names"] == [DISCOVERY_CHECK, EVENT_STREAM_CHECK]
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
        assert result["name"] == EVENT_STREAM_CHECK
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
        assert result["name"] == EVENT_STREAM_CHECK

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
            ["binary_sensor.g4_doorbell_motion", "motion", self._recent_iso(10)],
            # Smart detections render device_class as the string "None"
            [
                "binary_sensor.g4_doorbell_person_detected",
                "None",
                self._recent_iso(5),
            ],
            # _detected wins even with a non-motion device_class
            [
                "binary_sensor.backyard_doorbell_detected",
                "occupancy",
                self._recent_iso(7),
            ],
            # All of these must be dropped:
            ["binary_sensor.g4_doorbell_is_dark", "None", self._recent_iso(60)],
            ["binary_sensor.front_gate_door", "door", self._recent_iso(3)],
            ["binary_sensor.nvr_storage_problem", "problem", self._recent_iso(2)],
            ["binary_sensor.garage_occupancy", "occupancy", self._recent_iso(1)],
            ["binary_sensor.basement_moisture", "moisture", self._recent_iso(4)],
        ]
        admin = _make_mock_admin()
        admin.render_template = AsyncMock(return_value=json.dumps(rows))
        app._admin = admin

        sensors, result = _run(app._discover_sensors())

        ids = [e for e, _ in sensors]
        assert ids == [
            "binary_sensor.g4_doorbell_motion",
            "binary_sensor.g4_doorbell_person_detected",
            "binary_sensor.backyard_doorbell_detected",
        ]
        assert result["status"] == "ok"
        assert result["name"] == DISCOVERY_CHECK
        assert "3 event sensors" in result["detail"]
        # Cache populated with the kept entity ids
        assert app._sensors == ids
        # Timestamps parsed to aware datetimes
        assert all(ts is not None and ts.tzinfo is not None for _, ts in sensors)
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
        app._sensors = ["binary_sensor.cached_motion"]
        recent = self._recent_iso(1)
        app.get_state = AsyncMock(return_value={"last_changed": recent})

        sensors, result = _run(app._discover_sensors())

        assert result["status"] == "warning"
        assert "cached" in result["detail"]
        assert [e for e, _ in sensors] == ["binary_sensor.cached_motion"]
        assert sensors[0][1] == _parse_iso_utc(recent)
        app.get_state.assert_awaited_once_with(
            "binary_sensor.cached_motion", attribute="all"
        )

    def test_discovery_failure_without_cache_critical(self):
        app = _make_app()
        _init_only(app)
        admin = _make_mock_admin()
        admin.render_template = AsyncMock(side_effect=RuntimeError("boom"))
        app._admin = admin
        app._sensors = []
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
        app.get_state = AsyncMock(return_value={"last_changed": recent})

        sensors, result = _run(app._discover_sensors())

        assert result["status"] == "ok"
        assert "2 configured event sensors" in result["detail"]
        assert [e for e, _ in sensors] == [
            "binary_sensor.cam_a_motion",
            "binary_sensor.cam_b_motion",
        ]
        assert app._sensors == [e for e, _ in sensors]
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
        "name": EVENT_STREAM_CHECK,
        "status": "critical",
        "detail": "No events for 4.0h of active hours",
    },
]

OK_RESULTS = [
    {"name": DISCOVERY_CHECK, "status": "ok", "detail": ""},
    {"name": EVENT_STREAM_CHECK, "status": "ok", "detail": ""},
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
            {"name": EVENT_STREAM_CHECK, "status": "ok", "detail": ""},
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
                "last_changed": (
                    baseline - datetime.timedelta(seconds=30)
                ).isoformat()
            },
            "binary_sensor.b": {
                "last_changed": (
                    baseline + datetime.timedelta(seconds=30)
                ).isoformat()
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
            "binary_sensor.bad": {"last_changed": "not-a-timestamp"},
            "binary_sensor.good": {
                "last_changed": (
                    baseline + datetime.timedelta(seconds=10)
                ).isoformat()
            },
        }
        app = self._app_with_states(states)

        assert _run(app._any_event_after(baseline)) == "binary_sensor.good"
