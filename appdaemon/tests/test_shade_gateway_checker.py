"""Unit tests for ShadeGatewayChecker.

Mocks AppDaemon methods, HAProvisioner, and asyncio.sleep — no real HA
access and no real waiting required.
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

from health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker import (
    ShadeGatewayChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "checker_id": "shade_gateway",
    "checker_name": "Shade Gateway",
    "check_interval_s": 300,
    "entity_patterns": [
        {"include": r"sensor\.test_shade_.*_battery$"},
    ],
    "disconnect_low_threshold": 5,
    "healthy_floor": 40,
    "recovery_settle_s": 900,
    "repair_button": "button.test_gateway_power_cycle",
    "repair_settle_s": 180,
    "repair_recovery_wait_s": 900,
    "auto_repair_enabled_default": True,
    "auto_repair_delay_min_default": 120,
}

MOCK_ENTITIES = {
    "sensor.test_shade_a_battery": {
        "state": "100",
        "attributes": {
            "device_class": "battery",
            "unit_of_measurement": "%",
            "friendly_name": "Test Shade A Battery",
        },
    },
    "sensor.test_shade_b_battery": {
        "state": "90",
        "attributes": {
            "device_class": "battery",
            "unit_of_measurement": "%",
            "friendly_name": "Test Shade B Battery",
        },
    },
    # Non-matching entity (wrong device_class) — must never be discovered
    "sensor.temperature_reading": {
        "state": "72",
        "attributes": {
            "device_class": "temperature",
            "unit_of_measurement": "°F",
            "friendly_name": "Room Temperature",
        },
    },
}


def _mock_get_state(entity_id=None, **kwargs):
    if entity_id is None:
        return MOCK_ENTITIES
    entity = MOCK_ENTITIES.get(entity_id)
    return entity["state"] if entity else None


def _make_app(extra_args: dict | None = None) -> ShadeGatewayChecker:
    ad = MagicMock()
    config = MagicMock()
    app = ShadeGatewayChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.get_state = AsyncMock(side_effect=_mock_get_state)
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


def _make_mock_provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    prov.ensure_script = AsyncMock(return_value=False)
    return prov


def _startup(app: ShadeGatewayChecker, mock_prov: MagicMock | None = None) -> None:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(
        "health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker.HAProvisioner",
        return_value=mock_prov,
    ):
        _run(app._async_startup())


def _init_only(app: ShadeGatewayChecker) -> None:
    """Call initialize without running async startup."""
    app.initialize()


def _init_and_discover(app: ShadeGatewayChecker) -> None:
    app.initialize()
    _run(app._discover_entities())


def _init_discover_and_seed(app: ShadeGatewayChecker) -> None:
    app.initialize()
    _run(app._discover_entities())
    _run(app._seed_baselines())


# ---------------------------------------------------------------------------
# Tests — Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_startup_provisions_helpers(self):
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        _startup(app, mock_prov)
        assert mock_prov.ensure_helper.call_count == 2

    @staticmethod
    def _turn_on_calls(app):
        return [
            c for c in app.call_service.call_args_list
            if c.args[:1] == ("input_boolean/turn_on",)
            and c.kwargs.get("entity_id")
            == "input_boolean.shade_gateway_health_auto_repair"
        ]

    def test_provisioning_enables_auto_repair_default_when_created(self):
        # A freshly-created input_boolean defaults to off; since the config
        # default is enabled, provisioning must turn it on so the read-back
        # in _refresh_auto_repair_config doesn't silently disable auto-repair.
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_prov.ensure_helper = AsyncMock(return_value=True)  # created
        _startup(app, mock_prov)
        assert len(self._turn_on_calls(app)) == 1

    def test_provisioning_does_not_enable_when_helper_already_exists(self):
        # ensure_helper returns False (already exists) — never clobber a
        # user's later on/off choice by re-enabling on every startup.
        app = _make_app()
        mock_prov = _make_mock_provisioner()  # returns False
        _startup(app, mock_prov)
        assert self._turn_on_calls(app) == []

    def test_provisioning_does_not_enable_when_default_disabled(self):
        app = _make_app({"auto_repair_enabled_default": False})
        mock_prov = _make_mock_provisioner()
        mock_prov.ensure_helper = AsyncMock(return_value=True)  # created
        _startup(app, mock_prov)
        assert self._turn_on_calls(app) == []

    def test_startup_registers_with_supports_repair_and_alerting(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is True
        assert payload["checker_id"] == "shade_gateway"
        assert payload["check_names"] == ["Gateway Link"]
        assert payload["alerting"] == {"alertname": "ShadeGatewayDisconnected"}

    def test_startup_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names
        assert "health_check_repair_shade_gateway" in event_names

    def test_startup_listens_state_for_each_discovered_entity(self):
        app = _make_app()
        _startup(app)
        listened_entities = {c[0][1] for c in app.listen_state.call_args_list}
        assert listened_entities == {
            "sensor.test_shade_a_battery",
            "sensor.test_shade_b_battery",
        }

    def test_no_entities_logs_warning(self):
        app = _make_app({
            "entity_patterns": [{"include": "sensor\\.nonexistent_.*"}],
        })
        _startup(app)
        warning_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "WARNING" and "No entities" in str(c[0])
        ]
        assert len(warning_calls) == 1


# ---------------------------------------------------------------------------
# Tests — Discovery and cold-start seeding
# ---------------------------------------------------------------------------

class TestDiscoveryAndSeeding:
    def test_discovers_shade_entities_only(self):
        app = _make_app()
        _init_and_discover(app)
        assert set(app._entities) == {
            "sensor.test_shade_a_battery",
            "sensor.test_shade_b_battery",
        }

    def test_seeds_baselines_from_healthy_current_state(self):
        app = _make_app()
        _init_discover_and_seed(app)
        assert app._last_good_value["sensor.test_shade_a_battery"] == 100.0
        assert app._last_good_value["sensor.test_shade_b_battery"] == 90.0
        assert app._current_value["sensor.test_shade_a_battery"] == 100.0

    def test_cold_start_low_reading_does_not_seed_last_good_value(self):
        """A shade reading low at AppDaemon startup must not fabricate a baseline."""
        entities = dict(MOCK_ENTITIES)
        entities["sensor.test_shade_a_battery"] = {
            "state": "0",
            "attributes": {
                "device_class": "battery",
                "unit_of_measurement": "%",
                "friendly_name": "Test Shade A Battery",
            },
        }
        app = _make_app()
        app.get_state = AsyncMock(
            side_effect=lambda entity_id=None, **kw: (
                entities if entity_id is None else entities.get(entity_id, {}).get("state")
            )
        )
        _init_discover_and_seed(app)

        assert "sensor.test_shade_a_battery" not in app._last_good_value
        # current_value is still tracked (needed for _affected_shades_healthy)
        assert app._current_value["sensor.test_shade_a_battery"] == 0.0
        # No episode fabricated from a cold-start low reading
        assert app._disconnect_since is None


# ---------------------------------------------------------------------------
# Tests — Disconnect detection (_process_reading)
# ---------------------------------------------------------------------------

class TestDetection:
    def test_impossible_drop_starts_episode(self):
        app = _make_app()
        _init_only(app)
        app._entities = {"sensor.test_shade_a_battery": "Test Shade A"}
        app._last_good_value["sensor.test_shade_a_battery"] = 100.0

        app._process_reading("sensor.test_shade_a_battery", "0")

        assert app._disconnect_since is not None
        assert app._last_zero_time is not None
        assert "sensor.test_shade_a_battery" in app._episode_affected

    def test_gradual_decline_does_not_start_episode(self):
        """8% -> 0% with a low prior baseline is a real dying battery, not a disconnect."""
        app = _make_app()
        _init_only(app)
        app._entities = {"sensor.test_shade_a_battery": "Test Shade A"}
        app._last_good_value["sensor.test_shade_a_battery"] = 8.0

        app._process_reading("sensor.test_shade_a_battery", "0")

        assert app._disconnect_since is None
        assert app._episode_affected == {}

    def test_no_baseline_does_not_start_episode(self):
        """Cold start: no last_good_value recorded yet — never flagged."""
        app = _make_app()
        _init_only(app)
        app._entities = {"sensor.test_shade_a_battery": "Test Shade A"}

        app._process_reading("sensor.test_shade_a_battery", "0")

        assert app._disconnect_since is None

    def test_mid_episode_recovery_reading_does_not_clear_episode(self):
        """A mid-episode 100% reading must not clear the episode by itself —
        only _recompute_recovery (after the settle window) can do that."""
        app = _make_app()
        _init_only(app)
        app._entities = {"sensor.test_shade_a_battery": "Test Shade A"}
        app._last_good_value["sensor.test_shade_a_battery"] = 100.0

        app._process_reading("sensor.test_shade_a_battery", "0")  # starts episode
        started_at = app._disconnect_since
        assert started_at is not None

        app._process_reading("sensor.test_shade_a_battery", "100")  # bounces back
        assert app._disconnect_since == started_at  # episode still active
        assert app._current_value["sensor.test_shade_a_battery"] == 100.0

        app._process_reading("sensor.test_shade_a_battery", "0")  # drops again
        assert app._disconnect_since == started_at  # start time unchanged
        assert app._last_zero_time is not None  # re-armed by the second drop

    def test_non_numeric_reading_does_not_start_episode(self):
        app = _make_app()
        _init_only(app)
        app._entities = {"sensor.test_shade_a_battery": "Test Shade A"}
        app._last_good_value["sensor.test_shade_a_battery"] = 100.0

        app._process_reading("sensor.test_shade_a_battery", "unavailable")

        assert app._disconnect_since is None

    def test_unavailable_reading_marks_current_value_none_without_starting_episode(self):
        """A non-numeric reading (unavailable/unknown/None) records the
        entity as explicitly unknown rather than leaving a stale cached
        value — but must never start or feed a disconnect episode (an HA
        restart briefly making shades unavailable must not fabricate one)."""
        app = _make_app()
        _init_only(app)
        entity_id = "sensor.test_shade_a_battery"
        app._entities = {entity_id: "Test Shade A"}
        app._last_good_value[entity_id] = 100.0
        app._current_value[entity_id] = 100.0

        app._process_reading(entity_id, "unavailable")

        assert app._current_value[entity_id] is None
        assert app._disconnect_since is None
        assert app._last_zero_time is None
        assert entity_id not in app._episode_affected


# ---------------------------------------------------------------------------
# Tests — Recovery (_recompute_recovery)
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_recovery_blocked_before_settle_time(self):
        app = _make_app({"recovery_settle_s": 900})
        _init_only(app)
        app._entities = {"sensor.a": "A"}
        app._current_value = {"sensor.a": 100.0}
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(seconds=1000)
        app._last_zero_time = datetime.datetime.now() - datetime.timedelta(seconds=100)

        app._recompute_recovery()

        assert app._disconnect_since is not None

    def test_recovery_clears_after_settle_time_when_all_healthy(self):
        app = _make_app({"recovery_settle_s": 900})
        _init_only(app)
        app._entities = {"sensor.a": "A"}
        app._current_value = {"sensor.a": 100.0}
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(seconds=2000)
        app._last_zero_time = datetime.datetime.now() - datetime.timedelta(seconds=1000)
        app._episode_affected = {"sensor.a": datetime.datetime.now()}
        app._repair_attempted_this_episode = True

        app._recompute_recovery()

        assert app._disconnect_since is None
        assert app._last_zero_time is None
        assert app._episode_affected == {}
        assert app._repair_attempted_this_episode is False

    def test_recovery_blocked_if_affected_shade_still_unhealthy(self):
        """An AFFECTED shade (one that actually flapped this episode) still
        reading low must block recovery even past the settle window."""
        app = _make_app({"recovery_settle_s": 900})
        _init_only(app)
        app._entities = {"sensor.a": "A", "sensor.b": "B"}
        app._current_value = {"sensor.a": 100.0, "sensor.b": 3.0}
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(seconds=2000)
        app._last_zero_time = datetime.datetime.now() - datetime.timedelta(seconds=1000)
        app._episode_affected = {"sensor.b": datetime.datetime.now()}

        app._recompute_recovery()

        assert app._disconnect_since is not None

    def test_unrelated_unavailable_shade_does_not_block_recovery(self):
        """A shade that never contributed to this episode (not in
        _episode_affected) being unavailable must NOT stall recovery of the
        shades that actually flapped — this is the hardening fix for the
        known limitation where an unrelated hiccup could stall recovery
        forever."""
        app = _make_app({"recovery_settle_s": 900})
        _init_only(app)
        app._entities = {"sensor.a": "A", "sensor.b": "B"}
        # sensor.a is the only affected shade, and it's healthy again.
        app._current_value = {"sensor.a": 100.0, "sensor.b": None}
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(seconds=2000)
        app._last_zero_time = datetime.datetime.now() - datetime.timedelta(seconds=1000)
        app._episode_affected = {"sensor.a": datetime.datetime.now()}

        app._recompute_recovery()

        assert app._disconnect_since is None

    def test_affected_shade_unavailable_blocks_recovery(self):
        """An affected shade reading None (unavailable/unknown) is NOT
        confirmed healthy — recovery must stay blocked even past the
        settle window, since we can't confirm it actually came back."""
        app = _make_app({"recovery_settle_s": 900})
        _init_only(app)
        app._entities = {"sensor.a": "A"}
        app._current_value = {"sensor.a": None}
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(seconds=2000)
        app._last_zero_time = datetime.datetime.now() - datetime.timedelta(seconds=1000)
        app._episode_affected = {"sensor.a": datetime.datetime.now()}

        app._recompute_recovery()

        assert app._disconnect_since is not None

    def test_no_episode_is_a_no_op(self):
        app = _make_app()
        _init_only(app)
        app._recompute_recovery()
        assert app._disconnect_since is None


# ---------------------------------------------------------------------------
# Tests — Grace timing and status reporting (_build_results / _evaluate_auto_repair)
# ---------------------------------------------------------------------------

class TestGraceAndResults:
    def test_build_results_ok_when_no_episode(self):
        app = _make_app()
        _init_and_discover(app)
        results = app._build_results()
        assert results[0]["name"] == "Gateway Link"
        assert results[0]["status"] == "ok"

    def test_build_results_warning_before_deadline(self):
        app = _make_app()
        _init_and_discover(app)
        app._cached_auto_repair_delay_min = 120
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=10)
        app._episode_affected = {"sensor.test_shade_a_battery": datetime.datetime.now()}

        results = app._build_results()

        assert results[0]["status"] == "warning"
        assert "Disconnected" in results[0]["detail"]
        assert "Test Shade A" in results[0]["detail"]

    def test_build_results_critical_after_deadline(self):
        app = _make_app()
        _init_and_discover(app)
        app._cached_auto_repair_delay_min = 5
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=10)

        results = app._build_results()

        assert results[0]["status"] == "critical"
        assert "past" in results[0]["detail"]

    def test_build_results_critical_when_repair_failed(self):
        app = _make_app()
        _init_and_discover(app)
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=10)
        app._repair_status = REPAIR_FAILED
        app._repair_detail = "Gateway power-cycle did not restore shades after 900s — manual intervention needed"

        results = app._build_results()

        assert results[0]["status"] == "critical"
        assert "manual intervention needed" in results[0]["detail"]

    def test_pending_before_deadline(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 120
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=5)

        app._evaluate_auto_repair()

        assert app._repair_status == REPAIR_PENDING
        assert app._auto_repair_deadline is not None

    def test_starts_repair_at_deadline(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=10)

        app._evaluate_auto_repair()

        assert app._repair_status == REPAIR_IN_PROGRESS
        assert app._repair_attempted_this_episode is True

    def test_auto_repair_disabled_stays_idle_but_episode_tracked(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 5
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=10)

        app._evaluate_auto_repair()

        assert app._repair_status == REPAIR_IDLE
        assert app._disconnect_since is not None  # still tracked

    def test_episode_cleared_resets_repair_status_to_idle(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app._disconnect_since = None  # already cleared by recovery

        app._evaluate_auto_repair()

        assert app._repair_status == REPAIR_IDLE

    def test_one_restart_per_episode_guard_blocks_auto_retry(self):
        """After a failed auto-repair, the evaluator must not retry within
        the same episode — this is the 'one restart per episode' rule."""
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=30)
        app._repair_status = REPAIR_FAILED
        app._repair_attempted_this_episode = True

        app._evaluate_auto_repair()

        assert app._repair_status == REPAIR_FAILED
        app.create_task.assert_not_called()

    def test_success_state_does_not_retrigger(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=30)
        app._repair_status = REPAIR_SUCCESS

        app._evaluate_auto_repair()

        assert app._repair_status == REPAIR_SUCCESS
        app.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Repair execution
# ---------------------------------------------------------------------------

class TestRepairExecution:
    def test_start_repair_presses_button_and_sets_in_progress(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()

        app._start_repair()

        assert app._repair_status == REPAIR_IN_PROGRESS
        assert app._repair_attempted_this_episode is True
        app.create_task.assert_called_once()

    def test_no_repair_button_fails_immediately(self):
        app = _make_app({"repair_button": ""})
        _init_only(app)

        app._start_repair()

        assert app._repair_status == REPAIR_FAILED
        assert "No repair button" in app._repair_detail

    def test_execute_repair_success_clears_episode(self):
        app = _make_app({"repair_settle_s": 1, "repair_recovery_wait_s": 1})
        _init_only(app)
        app._entities = {"sensor.a": "A"}
        app._current_value = {"sensor.a": 100.0}
        app._last_zero_time = None
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=130)
        app._episode_affected = {"sensor.a": datetime.datetime.now()}
        app._repair_status = REPAIR_IN_PROGRESS

        with patch(
            "health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            _run(app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS
        assert app._disconnect_since is None
        assert app._repair_attempted_this_episode is False
        service_calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "button/press" in service_calls
        assert app.call_service.call_args_list[0].kwargs["entity_id"] == "button.test_gateway_power_cycle"

    def test_execute_repair_timeout_sets_failed_and_escalation_detail(self):
        app = _make_app({"repair_settle_s": 1, "repair_recovery_wait_s": 1})
        _init_only(app)
        app._entities = {"sensor.a": "A"}
        app._current_value = {"sensor.a": 2.0}  # never recovers
        app._episode_affected = {"sensor.a": datetime.datetime.now()}
        app._disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=130)
        app._repair_status = REPAIR_IN_PROGRESS
        app._repair_attempted_this_episode = True

        with patch(
            "health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert "manual intervention needed" in app._repair_detail
        # One restart per episode: the flag must survive a failed attempt.
        assert app._repair_attempted_this_episode is True
        # Episode must still be considered active (not silently cleared).
        assert app._disconnect_since is not None

    def test_execute_repair_error_handling(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        app.call_service = MagicMock(side_effect=Exception("Service unavailable"))

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert "error" in app._repair_detail.lower()

    def test_manual_repair_works_even_after_previous_failure_this_episode(self):
        """Manual repair via the card must bypass the one-restart-per-episode
        guard — only the *automatic* trigger is gated."""
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()
        app._repair_status = REPAIR_FAILED
        app._repair_attempted_this_episode = True

        app._start_repair()

        assert app._repair_status == REPAIR_IN_PROGRESS


# ---------------------------------------------------------------------------
# Tests — Cancel repair and config updates
# ---------------------------------------------------------------------------

class TestCancelAndConfig:
    def test_cancel_repair_leaves_disconnect_since_untouched(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._auto_repair_deadline = datetime.datetime.now() + datetime.timedelta(minutes=10)
        disconnect_since = datetime.datetime.now() - datetime.timedelta(minutes=5)
        app._disconnect_since = disconnect_since

        app._cancel_repair()

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        assert app._disconnect_since == disconnect_since  # NOT cleared

    def test_cancel_repair_rejected_when_not_pending(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IDLE

        app._cancel_repair()

        assert app._repair_status == REPAIR_IDLE
        warning_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_update_auto_repair_enabled(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="off")

        app._update_repair_config({"auto_repair_enabled": True})

        app.call_service.assert_called_with(
            "input_boolean/turn_on",
            entity_id="input_boolean.shade_gateway_health_auto_repair",
        )

    def test_update_auto_repair_delay(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="15")

        app._update_repair_config({"auto_repair_delay_min": 180})

        app.call_service.assert_called_with(
            "input_number/set_value",
            entity_id="input_number.shade_gateway_health_auto_repair_delay",
            value=180,
        )

    def test_on_repair_command_start(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()

        app._on_repair_command(
            "health_check_repair_shade_gateway", {"action": "start_repair"}, {}
        )

        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_build_repair_state_shape(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 120

        state = app._build_repair_state()

        assert state["status"] == REPAIR_IDLE
        assert state["auto_repair_enabled"] is True
        assert state["auto_repair_delay_min"] == 120
        assert state["auto_repair_deadline"] is None
        assert "last_repair_attempt" in state
