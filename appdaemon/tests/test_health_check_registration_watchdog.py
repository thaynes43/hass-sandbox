"""Registration watchdog in HealthCheckController (hass-sandbox#226).

On 2026-10-03 the Z-Wave checker died during AppDaemon re-initialisation
before it registered with the controller.  Its dependents (Z-Wave Batteries,
Cigars) read ``unknown`` / "dependency unavailable: zwave" for three hours
and nothing alerted.  The watchdog restarts an expected checker that has not
registered (bounded, doubling backoff) and, after a grace period, publishes
it as a critical checker so it reaches Alertmanager.

The clock is the controller's ``_monotonic`` hook, driven by the tests, and
AppDaemon's app config is built with AppDaemon's own pydantic model so a
shape change in an AppDaemon upgrade fails here rather than in production.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from appdaemon.models.config.app import AllAppConfig

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.controller.health_check_controller import (  # noqa: E402
    NOT_REGISTERED_ALERTNAME,
    REGISTRATION_CHECK_NAME,
    SENSOR_ENTITY_ID,
    HealthCheckController,
    discover_configured_checkers,
)

PROTOCOL_MODULE = (
    "health_checks.checker_apps.network_protocol_checker."
    "repairable_network_protocol_checker"
)
BATTERY_MODULE = "health_checks.checker_apps.battery_checker.battery_checker"
MQTT_DEVICE_MODULE = "health_checks.checker_apps.mqtt_device_checker.mqtt_device_checker"

# A slice of apps-prod.yaml: the incident's checkers, the MQTT broker and a
# lights checker (30-minute warm-up), plus a non-checker app.
APP_CONFIG: Dict[str, Any] = {
    "health_check_controller": {
        "module": "health_checks.controller.health_check_controller",
        "class": "HealthCheckController",
    },
    "zwave_health_checker": {
        "module": PROTOCOL_MODULE,
        "class": "RepairableNetworkProtocolChecker",
        "checker_id": "zwave",
        "checker_name": "Z-Wave",
    },
    "zwave_battery_checker": {
        "module": BATTERY_MODULE,
        "class": "BatteryChecker",
        "checker_id": "zwave_batteries",
        "checker_name": "Z-Wave Batteries",
    },
    "mqtt_broker_checker": {
        "module": "health_checks.checker_apps.mqtt_broker_checker.mqtt_broker_checker",
        "class": "MqttBrokerChecker",
        "checker_id": "mqtt_broker",
        "checker_name": "MQTT Broker",
    },
    "basement_lights_checker": {
        "module": MQTT_DEVICE_MODULE,
        "class": "MqttDeviceChecker",
        "checker_id": "basement_lights",
        "checker_name": "Basement Lights",
    },
    "garage_door_notify": {
        "module": "door_notify.door_notify",
        "class": "DoorNotify",
    },
}

BASE_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "heartbeat_interval_s": 60,
    "metrics_enabled": False,
    "alertmanager_url": "http://alertmanager.test:9093",
    # Production values (apps-prod.yaml) for the gates that decide paging.
    "alert_for_seconds": {"critical": 300, "warning": 600},
    "alert_improve_hold_s": 900,
}

REGISTRATIONS: Dict[str, Dict[str, Any]] = {
    "zwave": {
        "checker_id": "zwave",
        "checker_name": "Z-Wave",
        "check_names": ["Integration Status", "Radio Ping"],
    },
    "zwave_batteries": {
        "checker_id": "zwave_batteries",
        "checker_name": "Z-Wave Batteries",
        "check_names": ["Leak Sensor", "CC Jar"],
        "dependencies": [{"checker_id": "zwave"}],
    },
    "basement_lights": {
        "checker_id": "basement_lights",
        "checker_name": "Basement Lights",
        "check_names": ["Bathroom Fan State", "Bathroom Fan MQTT"],
        "dependencies": [{"checker_id": "mqtt_broker"}],
    },
    "mqtt_broker": {
        "checker_id": "mqtt_broker",
        "checker_name": "MQTT Broker",
        "check_names": ["Broker Connectivity"],
    },
    # Registered from another AppDaemon instance (or with a typo in its
    # dependency): declares a checker nobody here configures.
    "orphan": {
        "checker_id": "orphan",
        "checker_name": "Orphan",
        "check_names": ["Own State", "Radio Link"],
        "dependencies": [{"checker_id": "zigbee_typo",
                          "affects_checks": ["Radio Link"]}],
    },
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Clock:
    """Monotonic clock (watchdog) + wall clock (Alertmanager bridge) in step."""

    def __init__(self) -> None:
        import datetime as _dt

        self._dt = _dt
        self.t = 1000.0
        self._wall0 = _dt.datetime(2026, 10, 3, 21, 45, tzinfo=_dt.timezone.utc)

    def monotonic(self) -> float:
        return self.t

    def wall(self):
        return self._wall0 + self._dt.timedelta(seconds=self.t - 1000.0)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_app(
    extra_args: Optional[dict] = None,
    app_config: Optional[Dict[str, Any]] = APP_CONFIG,
) -> tuple:
    """Start a controller with AppDaemon's real app-config model and a clock."""
    app = HealthCheckController(MagicMock(), MagicMock())
    args = dict(BASE_ARGS)
    args.update(extra_args or {})
    app.args = args
    app.name = "health_check_controller"
    if app_config is not None:
        app.AD = SimpleNamespace(
            app_management=SimpleNamespace(
                app_config=AllAppConfig.model_validate(app_config)
            )
        )
    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.log = MagicMock()
    app.restart_app = MagicMock()
    # Coroutines handed to create_task (bridge syncs, mute persistence) are
    # queued and drained in order after each step, so the Alertmanager
    # bridge sees every published snapshot as it does live.
    app.queued = []
    app.create_task = MagicMock(side_effect=app.queued.append)

    clock = _Clock()
    app._monotonic = clock.monotonic

    app.initialize()
    if app._alert_bridge is not None:
        app._alert_bridge._now_fn = clock.wall
        app._alert_bridge._client = AsyncMock()
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    prov.ensure_script = AsyncMock(return_value=False)
    with patch(
        "health_checks.controller.health_check_controller.HAProvisioner",
        return_value=prov,
    ):
        _run(app._async_startup())
        _drain(app)
    # run_every(..., "now", ...) — the first heartbeat tick fires at start-up.
    app._heartbeat_tick({})
    _drain(app)
    return app, clock


def _drain(app: HealthCheckController) -> None:
    """Run queued create_task coroutines to completion, in order."""
    while app.queued:
        _run(app.queued.pop(0))


def _command(app: HealthCheckController, command: str, payload: dict) -> None:
    app._on_command("health_check_command", {
        "command": command, "payload": json.dumps(payload),
    }, {})
    _drain(app)


def _register(app: HealthCheckController, checker_id: str) -> None:
    _command(app, "register_checker", REGISTRATIONS[checker_id])


def _report(app: HealthCheckController, checker_id: str, results: List[dict]) -> None:
    _command(app, "report_status", {"checker_id": checker_id, "results": results})


def _advance_to(app: HealthCheckController, clock: _Clock, t_rel: float,
                step: float = 60.0) -> None:
    """Run heartbeat ticks every ``step`` seconds up to ``t_rel`` after start."""
    target = 1000.0 + t_rel
    while clock.t + step <= target + 1e-9:
        clock.t += step
        app._heartbeat_tick({})
        _drain(app)


def _sensor(app: HealthCheckController) -> tuple:
    """Return (state, checkers-attrs) of the last published sensor."""
    call = app.set_state.call_args
    assert call[0][0] == SENSOR_ENTITY_ID
    return call[1]["state"], call[1]["attributes"]["checkers"]


def _posted_alerts(app: HealthCheckController) -> List[dict]:
    client = app._alert_bridge._client
    alerts: List[dict] = []
    for call in client.post_alerts.await_args_list:
        alerts.extend(call[0][0])
    return alerts


def _log_lines(app: HealthCheckController, level: Optional[str] = None) -> List[str]:
    return [
        c[0][0] for c in app.log.call_args_list
        if level is None or c[1].get("level") == level
    ]


# ---------------------------------------------------------------------------
# Discovery against AppDaemon's real config model
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_real_appdaemon_model_maps_checker_apps_only(self):
        cfg = AllAppConfig.model_validate({
            **APP_CONFIG,
            "disabled_checker": {
                "module": BATTERY_MODULE, "class": "BatteryChecker",
                "checker_id": "off", "disable": True,
            },
            "no_id_checker": {"module": BATTERY_MODULE, "class": "BatteryChecker"},
            "dup_zwave": {
                "module": PROTOCOL_MODULE, "class": "X", "checker_id": "zwave",
            },
            "quiet_checker": {
                "module": BATTERY_MODULE, "class": "BatteryChecker",
                "checker_id": "quiet", "alerting": {"enabled": False},
            },
            "a_global": {"module": "globals_mod", "global": True},
        })
        mapping, problems = discover_configured_checkers(
            cfg.root, exclude_app="health_check_controller"
        )
        assert set(mapping) == {
            "zwave", "zwave_batteries", "mqtt_broker", "basement_lights", "quiet",
        }
        assert mapping["zwave"] == {
            "app": "zwave_health_checker", "name": "Z-Wave", "alerting_enabled": True,
        }
        assert mapping["quiet"]["alerting_enabled"] is False
        assert any("no_id_checker" in p for p in problems)
        assert any("share checker_id 'zwave'" in p for p in problems)

    def test_plain_dicts_are_accepted(self):
        mapping, problems = discover_configured_checkers(APP_CONFIG)
        assert set(mapping) == {
            "zwave", "zwave_batteries", "mqtt_broker", "basement_lights",
        }
        assert problems == []


# ---------------------------------------------------------------------------
# The incident: a checker that fails registration
# ---------------------------------------------------------------------------


class TestFailedRegistration:
    def _incident(self) -> tuple:
        """Start-up where every checker registers except zwave."""
        app, clock = _make_app()
        _register(app, "zwave_batteries")
        _register(app, "mqtt_broker")
        _register(app, "basement_lights")
        return app, clock

    def test_retries_with_backoff_then_surfaces_critical_and_pages(self):
        app, clock = self._incident()

        # Normal start-up window: nothing happens yet.
        _advance_to(app, clock, 60)
        app.restart_app.assert_not_called()
        assert "zwave" not in _sensor(app)[1]

        # 1. Self-heal: first restart at registration_restart_after_s (120s).
        _advance_to(app, clock, 120)
        app.restart_app.assert_called_once_with("zwave_health_checker")
        assert any(
            "restarting app 'zwave_health_checker' (attempt 1/3)" in line
            for line in _log_lines(app, "WARNING")
        )

        # Still in grace: not surfaced, and dependents name the missing checker.
        _advance_to(app, clock, 240)
        state, checkers = _sensor(app)
        assert "zwave" not in checkers
        assert state != "critical"
        batteries = {c["name"]: c for c in checkers["zwave_batteries"]["checks"]}
        assert batteries["Leak Sensor"]["status"] == "unknown"
        assert batteries["Leak Sensor"]["detail"] == (
            "dependency unavailable: Z-Wave (not registered)"
        )

        # 2. Surface: past registration_grace_s (300s) it is a critical checker.
        _advance_to(app, clock, 300)
        state, checkers = _sensor(app)
        assert state == "critical"
        zwave = checkers["zwave"]
        assert zwave["name"] == "Z-Wave"
        assert zwave["status"] == "critical"
        assert zwave["is_dependency"] is True
        (reg,) = zwave["checks"]
        assert reg["name"] == REGISTRATION_CHECK_NAME
        assert reg["status"] == "critical"
        assert "not registered with the controller after 5 min" in reg["detail"]
        assert "dependents masked: Z-Wave Batteries" in reg["detail"]
        assert "1 automatic restart(s) of app 'zwave_health_checker'" in reg["detail"]
        assert any(
            "checker 'zwave' (Z-Wave) is still not registered" in line
            for line in _log_lines(app, "WARNING")
        )

        # Alertmanager: pending behind the normal critical for-gate (300s)...
        assert _posted_alerts(app) == []
        assert "zwave" in app._alert_bridge.pending_alerts

        # ...second restart on the doubled backoff (120 + 240 = 360s)...
        _advance_to(app, clock, 360)
        assert app.restart_app.call_count == 2

        # ...and promoted to a critical page once sustained.
        _advance_to(app, clock, 600)
        alerts = _posted_alerts(app)
        assert len(alerts) == 1
        labels = alerts[0]["labels"]
        assert labels == {
            "alertname": NOT_REGISTERED_ALERTNAME,
            "severity": "critical",
            "source": "appdaemon-health-check",
            "checker": "zwave",
        }
        assert "Registration: not registered" in alerts[0]["annotations"]["description"]

        # Bounded: third and last restart at 360 + 480 = 840s, then no more.
        _advance_to(app, clock, 840)
        assert app.restart_app.call_count == 3
        _advance_to(app, clock, 4 * 3600)
        assert app.restart_app.call_count == 3
        reg = _sensor(app)[1]["zwave"]["checks"][0]
        assert "3 automatic restart(s)" in reg["detail"]
        assert "check the AppDaemon error log" in reg["detail"]
        # Still critical and still firing — it needs a human.
        assert _sensor(app)[0] == "critical"
        assert "zwave" in app._alert_bridge.active_alerts

    def test_restart_that_heals_never_pages(self):
        app, clock = self._incident()
        _advance_to(app, clock, 120)
        app.restart_app.assert_called_once_with("zwave_health_checker")

        clock.t += 5
        _register(app, "zwave")  # the restarted app registers
        assert "zwave" not in app._unregistered
        assert any(
            "checker 'zwave' registered after" in line and "(1 restart)" in line
            for line in _log_lines(app, "INFO")
        )

        _advance_to(app, clock, 3600)
        app.restart_app.assert_called_once()
        assert _posted_alerts(app) == []
        batteries = {c["name"]: c for c in _sensor(app)[1]["zwave_batteries"]["checks"]}
        assert "dependency unavailable" not in batteries["Leak Sensor"]["detail"]

    def test_checker_whose_startup_raises_is_healed_by_the_restart(self):
        """The 2026-10-03 failure end to end: the checker's first start-up
        dies before ``_register()``; the watchdog's restart re-runs it."""
        app, clock = _make_app()

        class _FlakyZwaveChecker:
            starts = 0

            def start(self) -> None:
                self.starts += 1
                if self.starts == 1:
                    # AppDaemon logs the task's exception; nothing registers.
                    return
                _register(app, "zwave")

        zwave_app = _FlakyZwaveChecker()
        zwave_app.start()  # AppDaemon's re-initialisation at 21:45
        app.restart_app.side_effect = (
            lambda name: zwave_app.start() if name == "zwave_health_checker" else None
        )
        for checker_id in ("zwave_batteries", "mqtt_broker", "basement_lights"):
            _register(app, checker_id)

        _advance_to(app, clock, 3600)
        assert zwave_app.starts == 2
        app.restart_app.assert_called_once_with("zwave_health_checker")
        assert "zwave" in app._checkers
        assert app._unregistered == {}
        assert _posted_alerts(app) == []

    def test_late_registration_after_surfacing_clears_the_critical(self):
        app, clock = self._incident()
        _advance_to(app, clock, 300)
        assert _sensor(app)[1]["zwave"]["status"] == "critical"

        _register(app, "zwave")
        state, checkers = _sensor(app)
        assert checkers["zwave"]["status"] == "unknown"  # the real registration
        assert checkers["zwave"]["checks"][0]["name"] == "Integration Status"
        assert any(
            "had been reported critical" in line for line in _log_lines(app, "INFO")
        )

    def test_missing_dependency_with_no_app_is_surfaced_without_restart(self):
        """A dependency nobody configured (typo, other instance) cannot be
        restarted, but it must not stay silent either."""
        app, clock = _make_app()
        for checker_id in ("zwave", "zwave_batteries", "mqtt_broker",
                           "basement_lights", "orphan"):
            _register(app, checker_id)
        # Expected from the first tick after the declaring checker registered.
        _advance_to(app, clock, 300)
        assert "zigbee_typo" not in _sensor(app)[1]
        _advance_to(app, clock, 360)

        app.restart_app.assert_not_called()
        checkers = _sensor(app)[1]
        missing = checkers["zigbee_typo"]
        assert missing["status"] == "critical"
        detail = missing["checks"][0]["detail"]
        assert "dependents masked: Orphan" in detail
        assert "cannot be restarted" in detail
        orphan = {c["name"]: c for c in checkers["orphan"]["checks"]}
        assert orphan["Radio Link"]["detail"] == (
            "dependency unavailable: zigbee_typo (not registered)"
        )
        # Only the declared check is masked.
        assert "dependency" not in orphan["Own State"]["detail"]

    def test_unreadable_app_config_falls_back_to_declared_dependencies(self):
        app, clock = _make_app(app_config=None)  # no self.AD at all
        _register(app, "zwave_batteries")
        _advance_to(app, clock, 360)
        assert _sensor(app)[1]["zwave"]["status"] == "critical"
        app.restart_app.assert_not_called()
        warnings = [
            line for line in _log_lines(app, "WARNING")
            if "cannot read AppDaemon's app config" in line
        ]
        assert len(warnings) == 1  # said once, not every tick

    def test_failed_restart_call_is_logged_and_counted(self):
        app, clock = self._incident()
        app.restart_app.side_effect = RuntimeError("admin namespace down")
        _advance_to(app, clock, 120)
        assert any(
            "restart of app 'zwave_health_checker' failed" in line
            for line in _log_lines(app, "ERROR")
        )
        assert app._unregistered["zwave"]["attempts"] == 1

    def test_surfaced_missing_checker_can_be_muted(self):
        """The tile's mute button must work on a missing checker too."""
        app, clock = self._incident()

        async def _done():
            return None

        # Sync from the heartbeat callback, awaited from _persist_mute — as
        # AppDaemon's sync_decorator behaves.
        app.call_service = MagicMock(
            side_effect=lambda service, **kw: (
                _done() if service == "input_text/set_value" else None
            )
        )
        prov = MagicMock()
        prov.ensure_helper = AsyncMock(return_value=False)
        with patch(
            "health_checks.controller.health_check_controller.HAProvisioner",
            return_value=prov,
        ):
            _command(app, "mute_checker", {"checker_id": "zwave"})
            assert "zwave" not in app._mutes  # not surfaced yet → unknown

            _advance_to(app, clock, 300)
            _command(app, "mute_checker", {"checker_id": "zwave"})

        assert "zwave" in app._mutes
        assert _sensor(app)[1]["zwave"]["muted"] is True
        _advance_to(app, clock, 1200)
        assert _posted_alerts(app) == []
        # Persisted like any other checker's mute.
        app.call_service.assert_any_call(
            "input_text/set_value",
            entity_id="input_text.health_check_mute_zwave",
            value=json.dumps({"muted": True, "until": None}),
        )

    def test_configured_alerting_opt_out_is_honoured(self):
        config = dict(APP_CONFIG)
        config["zwave_health_checker"] = {
            **APP_CONFIG["zwave_health_checker"], "alerting": {"enabled": False},
        }
        app, clock = _make_app(app_config=config)
        _advance_to(app, clock, 1200)
        assert _sensor(app)[1]["zwave"]["status"] == "critical"
        assert not any(a["labels"]["checker"] == "zwave" for a in _posted_alerts(app))

    def test_watchdog_failure_does_not_stop_the_heartbeat(self):
        app, clock = self._incident()
        app._registration_watchdog_tick = MagicMock(side_effect=ValueError("boom"))
        app._heartbeat_tick({})
        app.call_service.assert_called()  # heartbeat still written
        assert any(
            "Registration watchdog failed" in line for line in _log_lines(app, "ERROR")
        )


# ---------------------------------------------------------------------------
# No false alarms during normal start-up and warm-up
# ---------------------------------------------------------------------------


class TestNormalWarmUp:
    def test_normal_startup_and_lights_warmup_stay_quiet(self):
        app, clock = _make_app()
        # Every configured checker registers within seconds of start-up
        # (live: all 26 inside 8 s on 2026-10-04).
        clock.t += 8
        for checker_id in ("zwave", "zwave_batteries", "mqtt_broker",
                           "basement_lights"):
            _register(app, checker_id)
        _report(app, "zwave", [
            {"name": "Integration Status", "status": "ok"},
            {"name": "Radio Ping", "status": "ok"},
        ])
        _report(app, "zwave_batteries", [
            {"name": "Leak Sensor", "status": "ok"},
            {"name": "CC Jar", "status": "ok"},
        ])
        _report(app, "mqtt_broker", [
            {"name": "Broker Connectivity", "status": "ok"},
        ])

        # The lights checker stays unknown through its ~30-minute warm-up,
        # so the overall state reads unknown — by design, never an alert.
        _advance_to(app, clock, 35 * 60)
        app.restart_app.assert_not_called()
        state, checkers = _sensor(app)
        assert state == "unknown"
        assert checkers["basement_lights"]["status"] == "unknown"
        assert app._unregistered == {}
        assert _posted_alerts(app) == []
        assert not any("Registration watchdog: checker" in l for l in _log_lines(app))

    def test_all_registered_quiet_and_logs_once(self):
        config = {
            k: v for k, v in APP_CONFIG.items()
            if k not in ("basement_lights_checker", "mqtt_broker_checker")
        }
        app, clock = _make_app(app_config=config)
        _register(app, "zwave")
        _register(app, "zwave_batteries")
        _advance_to(app, clock, 35 * 60)

        app.restart_app.assert_not_called()
        assert app._unregistered == {}
        assert _posted_alerts(app) == []
        assert _sensor(app)[0] != "critical"
        armed = [l for l in _log_lines(app, "INFO") if "Registration watchdog armed" in l]
        assert armed and "2 configured checker app(s)" in armed[0]
        done = [
            l for l in _log_lines(app, "INFO")
            if "Registration watchdog: all 2 expected checkers registered" in l
        ]
        assert len(done) == 1

    def test_slow_registration_inside_restart_window_is_left_alone(self):
        app, clock = _make_app()
        for checker_id in ("zwave_batteries", "mqtt_broker", "basement_lights"):
            _register(app, checker_id)
        _advance_to(app, clock, 60)
        clock.t += 50  # 110s: slower than ever seen live, still before 120s
        _register(app, "zwave")
        _advance_to(app, clock, 1800)
        app.restart_app.assert_not_called()
        assert "zwave" not in app._unregistered

    def test_disabled_watchdog_never_restarts_or_surfaces(self):
        app, clock = _make_app({"registration_watchdog_enabled": False})
        for checker_id in ("zwave_batteries", "mqtt_broker", "basement_lights"):
            _register(app, checker_id)
        _advance_to(app, clock, 3600)
        app.restart_app.assert_not_called()
        assert "zwave" not in _sensor(app)[1]
        assert not any("Registration watchdog armed" in l for l in _log_lines(app))
