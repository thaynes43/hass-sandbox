"""Registration watchdog in HealthCheckController (hass-sandbox#226).

On 2026-10-03 the Z-Wave checker died during AppDaemon re-initialisation
before it registered with the controller.  Its dependents (Z-Wave Batteries,
Cigars) read ``unknown`` / "dependency unavailable: zwave" for three hours
and nothing alerted.  The watchdog restarts an expected checker that has not
registered (bounded, doubling backoff) and, after a grace period, publishes
it as a critical checker so it reaches Alertmanager.

The clock is the controller's ``_monotonic`` hook, driven by the tests.
AppDaemon's admin namespace (``app.<name>`` entities: lifecycle state plus
the app's config in ``args``) is built the way AppDaemon 4.5 builds it,
from AppDaemon's own pydantic model, so a shape change in an upgrade fails
here rather than in production.  Disabled apps get no entity, and a running
app's state is ``idle`` (both observed in a real AppDaemon 4.5.13 run).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from appdaemon.models.config.app import AllAppConfig, AppConfig

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


def _admin_states(app_config: Dict[str, Any], app_states: Dict[str, str]) -> Dict[str, Any]:
    """AppDaemon's admin namespace for ``app_config``, as AppDaemon builds it."""
    states: Dict[str, Any] = {
        "thread.thread-0": {"state": "idle", "attributes": {}},
        "sensor.active_apps": {"state": len(app_config), "attributes": {}},
    }
    for name, cfg in AllAppConfig.model_validate(app_config).root.items():
        if not isinstance(cfg, AppConfig) or cfg.disable:
            continue  # AppDaemon keeps no app.<name> entity for a disabled app
        states[f"app.{name}"] = {
            "state": app_states.get(name, "idle"),
            "attributes": {
                "totalcallbacks": 0,
                "instancecallbacks": 0,
                "args": cfg.args,
                "config_path": "/conf/apps/apps.yaml",
            },
        }
    return states


def _make_app(
    extra_args: Optional[dict] = None,
    app_config: Optional[Dict[str, Any]] = APP_CONFIG,
    app_states: Optional[Dict[str, str]] = None,
) -> tuple:
    """Start a controller against a simulated AppDaemon, with a test clock.

    ``app.ad`` holds the simulated AppDaemon: ``app_config`` (None = the
    admin-namespace read fails), ``app_states`` (app name → lifecycle state,
    default ``idle``) and ``fail_read`` (a callable; True = that read fails).
    """
    app = HealthCheckController(MagicMock(), MagicMock())
    args = dict(BASE_ARGS)
    args.update(extra_args or {})
    app.args = args
    app.name = "health_check_controller"
    app.ad = SimpleNamespace(
        app_config=None if app_config is None else dict(app_config),
        app_states=dict(app_states or {}),
        fail_read=lambda: False,
    )

    def _get_state(entity_id=None, namespace=None, **kwargs):
        if namespace == "admin":
            if app.ad.app_config is None or app.ad.fail_read():
                raise TimeoutError("admin namespace read timed out")
            return _admin_states(app.ad.app_config, app.ad.app_states)
        return None  # HA helpers (mute state): nothing persisted

    app.get_state = MagicMock(side_effect=_get_state)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.log = MagicMock()
    app.restart_app = MagicMock(return_value=None)
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
    _start(app)
    return app, clock


def _start(app: HealthCheckController) -> None:
    """Run the controller's async start-up and its first heartbeat tick."""
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
    def test_admin_namespace_maps_enabled_checker_apps_only(self):
        config = {
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
        }
        admin = _admin_states(config, {"zwave_health_checker": "initialize_error"})
        mapping, problems = discover_configured_checkers(
            admin, exclude_app="health_check_controller"
        )
        assert set(mapping) == {
            "zwave", "zwave_batteries", "mqtt_broker", "basement_lights", "quiet",
        }
        assert mapping["zwave"] == {
            "app": "zwave_health_checker",
            "name": "Z-Wave",
            "alerting_enabled": True,
            "app_state": "initialize_error",
        }
        assert mapping["quiet"]["alerting_enabled"] is False
        assert any("no_id_checker" in p for p in problems)
        assert any("share checker_id 'zwave'" in p for p in problems)

    def test_ignores_non_app_entities_and_malformed_entries(self):
        admin = {
            "thread.thread-1": {"state": "idle", "attributes": {}},
            "app.weird": "not-a-mapping",
            "app.no_args": {"state": "idle", "attributes": {}},
            "app.zwave_health_checker": _admin_states(APP_CONFIG, {})[
                "app.zwave_health_checker"
            ],
        }
        mapping, problems = discover_configured_checkers(admin)
        assert set(mapping) == {"zwave"}
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
            "restarted app 'zwave_health_checker' (attempt 1/3)" in line
            and "120s after AppDaemon last started it" in line
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

        assert any(
            "(attempt 2/3)" in line and "240s after AppDaemon last started it" in line
            for line in _log_lines(app, "WARNING")
        )

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

    def test_registered_checker_whose_app_died_is_restarted_then_surfaced(self):
        """Registered once, then a single-app reload whose initialize() raised
        (the #225 failure, one app only): the stale registration must not
        freeze its tile at ok with nothing paging (review finding)."""
        app, clock = self._incident()
        _register(app, "zwave")
        _report(app, "zwave", [
            {"name": "Integration Status", "status": "critical", "detail": "down"},
            {"name": "Radio Ping", "status": "ok"},
        ])
        _report(app, "zwave", [
            {"name": "Integration Status", "status": "ok"},
            {"name": "Radio Ping", "status": "ok"},
        ])
        history = list(app._checkers["zwave"]["alert_history"])
        assert history

        _advance_to(app, clock, 600)  # healthy for ten minutes
        app.restart_app.assert_not_called()

        app.ad.app_states["zwave_health_checker"] = "initialize_error"
        _advance_to(app, clock, 660)
        assert "zwave" not in app._checkers
        assert any(
            "checker 'zwave' registered earlier but its app "
            "'zwave_health_checker' is now initialize_error" in l
            for l in _log_lines(app, "WARNING")
        )
        batteries = {c["name"]: c for c in _sensor(app)[1]["zwave_batteries"]["checks"]}
        assert batteries["Leak Sensor"]["detail"] == (
            "dependency unavailable: Z-Wave (not registered)"
        )

        _advance_to(app, clock, 780)  # 120s after the registration was voided
        app.restart_app.assert_called_once_with("zwave_health_checker")
        _advance_to(app, clock, 960)  # 300s after
        assert _sensor(app)[1]["zwave"]["status"] == "critical"

        # The restarted app comes back and keeps its alert history.
        app.ad.app_states["zwave_health_checker"] = "idle"
        _register(app, "zwave")
        assert app._checkers["zwave"]["alert_history"] == history

    def test_voided_registration_dropped_from_config_resolves_its_own_page(self):
        """A registered checker is firing its own alert, its app dies (the
        registration is voided), and the app is disabled inside the grace
        window: its alert must resolve, not be re-posted for ever (review
        finding, round 3)."""
        config = {
            **APP_CONFIG,
            "spa_health_checker": {
                "module": "health_checks.checker_apps.spa_health_checker."
                          "spa_health_checker",
                "class": "SpaHealthChecker",
                "checker_id": "spa",
                "checker_name": "Spa",
            },
        }
        app, clock = _make_app(app_config=config)
        app._metrics_enabled = True
        app._metrics = MagicMock()
        for checker_id in ("zwave", "zwave_batteries", "mqtt_broker",
                           "basement_lights"):
            _register(app, checker_id)
        _command(app, "register_checker", {
            "checker_id": "spa", "checker_name": "Spa", "check_names": ["Gateway Ping"],
        })
        _report(app, "spa", [{"name": "Gateway Ping", "status": "critical"}])
        _advance_to(app, clock, 360)
        _report(app, "spa", [{"name": "Gateway Ping", "status": "critical"}])
        assert app._alert_bridge.active_alerts["spa"]["labels"]["alertname"] == "SpaUnhealthy"

        app.ad.app_states["spa_health_checker"] = "initialize_error"
        _advance_to(app, clock, 420)
        assert "spa" not in app._checkers
        assert app._unregistered["spa"]["surfaced"] is False

        app.ad.app_config["spa_health_checker"] = {
            **config["spa_health_checker"], "disable": True,
        }
        _advance_to(app, clock, 480)
        resolved = _posted_alerts(app)[-1]
        assert resolved["labels"]["alertname"] == "SpaUnhealthy"
        assert "endsAt" in resolved
        assert app._alert_bridge.active_alerts == {}
        app._metrics.remove_checker.assert_called_with("spa")
        before = len(_posted_alerts(app))
        _run(app._alert_bridge.repost_active())
        assert len(_posted_alerts(app)) == before

    def test_registered_checker_whose_app_is_removed_is_forgotten(self):
        """The realistic ordering: the app's admin entity simply vanishes
        (disabled or removed), with no dead state seen first (review, round 5)."""
        config = {
            **APP_CONFIG,
            "spa_health_checker": {
                "module": "health_checks.checker_apps.spa_health_checker."
                          "spa_health_checker",
                "class": "SpaHealthChecker",
                "checker_id": "spa",
                "checker_name": "Spa",
            },
        }
        app, clock = _make_app(app_config=config)
        app._metrics_enabled = True
        app._metrics = MagicMock()
        for checker_id in ("zwave", "zwave_batteries", "mqtt_broker",
                           "basement_lights"):
            _register(app, checker_id)
        _command(app, "register_checker", {
            "checker_id": "spa", "checker_name": "Spa", "check_names": ["Gateway Ping"],
        })
        _report(app, "spa", [{"name": "Gateway Ping", "status": "critical"}])
        _advance_to(app, clock, 360)
        _report(app, "spa", [{"name": "Gateway Ping", "status": "critical"}])
        assert "spa" in app._alert_bridge.active_alerts

        del app.ad.app_config["spa_health_checker"]
        _advance_to(app, clock, 420)
        assert "spa" not in app._checkers
        assert "spa" not in _sensor(app)[1]
        resolved = _posted_alerts(app)[-1]
        assert resolved["labels"]["alertname"] == "SpaUnhealthy"
        assert "endsAt" in resolved
        assert app._alert_bridge.active_alerts == {}
        app._metrics.remove_checker.assert_called_with("spa")
        assert any(
            "checker 'spa' was registered but its app is no longer configured" in l
            for l in _log_lines(app, "WARNING")
        )

    def test_removed_app_still_depended_on_is_reported_missing(self):
        """Removing an app that others still depend on is a config error that
        masks the dependents: it must surface, not vanish."""
        app, clock = self._incident()
        _register(app, "zwave")
        del app.ad.app_config["zwave_health_checker"]
        _advance_to(app, clock, 60)
        assert "zwave" not in app._checkers
        _advance_to(app, clock, 360)
        app.restart_app.assert_not_called()
        reg = _sensor(app)[1]["zwave"]["checks"][0]
        assert reg["status"] == "critical"
        assert "no checker app with checker_id 'zwave' is configured" in reg["detail"]

    def test_checker_registered_from_another_appdaemon_is_never_dropped(self):
        """The controller is event-only so a laptop can register checkers
        against production: an id this instance never had is left alone."""
        app, clock = self._incident()
        _command(app, "register_checker", {
            "checker_id": "lab_dev", "checker_name": "Lab (dev)",
            "check_names": ["Ping"],
        })
        _advance_to(app, clock, 3600)
        assert "lab_dev" in app._checkers
        assert "lab_dev" in _sensor(app)[1]

    def test_empty_read_never_drops_registrations(self):
        app, clock = self._incident()
        _register(app, "zwave")
        app.ad.app_config = {}  # e.g. AppDaemon mid-reload, no entities yet
        _advance_to(app, clock, 120)
        assert set(app._checkers) >= {"zwave", "zwave_batteries", "mqtt_broker"}

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

    def test_unreadable_app_states_fall_back_to_declared_dependencies(self):
        app, clock = _make_app(app_config=None)  # every admin read fails
        _register(app, "zwave_batteries")
        _advance_to(app, clock, 360)
        assert _sensor(app)[1]["zwave"]["status"] == "critical"
        app.restart_app.assert_not_called()  # no app known to restart
        warnings = [
            line for line in _log_lines(app, "WARNING")
            if "cannot read AppDaemon's app states" in line
        ]
        assert len(warnings) == 1  # said once, not every tick

    def test_flapping_app_state_read_does_not_reset_the_clocks(self):
        """A read that fails every other tick must not restart the 120s/300s
        clocks or the attempt counter (review finding on #227)."""
        app, clock = self._incident()
        reads = {"n": 0}

        def _fail_every_other() -> bool:
            reads["n"] += 1
            return reads["n"] % 2 == 0

        app.ad.fail_read = _fail_every_other
        _advance_to(app, clock, 120)
        app.restart_app.assert_called_once_with("zwave_health_checker")
        _advance_to(app, clock, 300)
        assert _sensor(app)[1]["zwave"]["status"] == "critical"
        _advance_to(app, clock, 360)
        assert app.restart_app.call_count == 2

    def test_surfaced_checker_dropped_from_config_resolves_its_page(self):
        """Disabling the broken app is the natural response to the page: the
        alert must resolve, not be re-posted for ever (review finding)."""
        config = {
            **APP_CONFIG,
            "imagegen_health_checker": {
                "module": "health_checks.checker_apps.imagegen_health_checker."
                          "imagegen_health_checker",
                "class": "ImageGenHealthChecker",
                "checker_id": "imagegen",
                "checker_name": "Image Gen",
            },
        }
        app, clock = _make_app(app_config=config)
        app._metrics_enabled = True
        app._metrics = MagicMock()
        for checker_id in ("zwave", "zwave_batteries", "mqtt_broker",
                           "basement_lights"):
            _register(app, checker_id)
        _advance_to(app, clock, 600)
        firing = _posted_alerts(app)
        assert [a["labels"]["checker"] for a in firing] == ["imagegen"]
        assert "imagegen" in app._alert_bridge.active_alerts

        app.ad.app_config["imagegen_health_checker"] = {
            **config["imagegen_health_checker"], "disable": True,
        }
        _advance_to(app, clock, 660)
        resolved = _posted_alerts(app)[-1]
        assert resolved["labels"]["checker"] == "imagegen"
        assert resolved["labels"]["alertname"] == NOT_REGISTERED_ALERTNAME
        assert "endsAt" in resolved
        assert app._alert_bridge.active_alerts == {}
        assert "imagegen" not in _sensor(app)[1]
        app._metrics.remove_checker.assert_called_once_with("imagegen")
        # The re-post loop has nothing left to keep alive.
        before = len(_posted_alerts(app))
        _run(app._alert_bridge.repost_active())
        assert len(_posted_alerts(app)) == before

    def test_failed_restart_call_is_logged_and_counted(self):
        app, clock = self._incident()
        app.restart_app.side_effect = RuntimeError("admin namespace down")
        _advance_to(app, clock, 120)
        assert any(
            "restart of app 'zwave_health_checker' for checker 'zwave' failed "
            "(attempt 1/3)" in line
            for line in _log_lines(app, "ERROR")
        )
        assert not any("restarted app" in l for l in _log_lines(app))
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

    def test_surfaced_missing_checker_takes_notes_that_survive_registration(self):
        """The Shepherd runbook's precondition 0 has it record_note a wake on
        a not-registered alert (review round 6)."""
        app, clock = self._incident()
        _command(app, "record_note", {"checker_id": "zwave", "note": "too early"})
        assert any(
            "record_note for unknown checker: 'zwave'" in l
            for l in _log_lines(app, "WARNING")
        )  # not surfaced yet: no tile to annotate

        _advance_to(app, clock, 300)
        _command(app, "record_note", {
            "checker_id": "zwave", "note": "watchdog retrying; skipped",
            "source": "shepherd",
        })
        history = _sensor(app)[1]["zwave"]["alert_history"]
        assert history[0]["detail"] == "watchdog retrying; skipped"
        assert history[0]["is_note_event"] is True

        _register(app, "zwave")
        assert app._checkers["zwave"]["alert_history"][0]["detail"] == (
            "watchdog retrying; skipped"
        )

    def test_clear_history_reaches_a_missing_checker(self):
        """The detail card's Clear History (empty payload) and the per-checker
        form must clear a not-registered tile too (review round 7)."""
        app, clock = self._incident()
        _advance_to(app, clock, 300)
        _command(app, "record_note", {"checker_id": "zwave", "note": "one"})
        _command(app, "clear_alert_history", {"checker_id": "zwave"})
        assert _sensor(app)[1]["zwave"]["alert_history"] == []
        _command(app, "record_note", {"checker_id": "zwave", "note": "two"})
        _command(app, "clear_alert_history", {})
        assert _sensor(app)[1]["zwave"]["alert_history"] == []

    def test_kept_history_is_dropped_once_the_checker_is_gone(self):
        app, clock = self._incident()
        _advance_to(app, clock, 300)
        _command(app, "record_note", {"checker_id": "zwave", "note": "n"})
        assert "zwave" in app._reg_saved_history
        del app.ad.app_config["zwave_health_checker"]
        del app.ad.app_config["zwave_battery_checker"]  # nothing depends on it now
        app._checkers.pop("zwave_batteries")
        _advance_to(app, clock, 360)
        assert "zwave" not in app._unregistered
        assert "zwave" not in app._reg_saved_history

    def test_voided_checker_tile_keeps_its_history(self):
        app, clock = self._incident()
        _register(app, "zwave")
        _report(app, "zwave", [
            {"name": "Integration Status", "status": "critical", "detail": "down"},
            {"name": "Radio Ping", "status": "ok"},
        ])
        app.ad.app_states["zwave_health_checker"] = "initialize_error"
        _advance_to(app, clock, 60)
        _advance_to(app, clock, 360)  # surfaced 300s after the void
        history = _sensor(app)[1]["zwave"]["alert_history"]
        assert history and history[0]["check"] == "Integration Status"

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
# AppDaemon lifecycle and API guards
# ---------------------------------------------------------------------------


class TestAppDaemonLifecycle:
    def _others_registered(self, app: HealthCheckController) -> None:
        for checker_id in ("zwave_batteries", "mqtt_broker", "basement_lights"):
            _register(app, checker_id)

    def test_restarts_wait_until_appdaemon_has_started_the_app(self):
        """A slow AppDaemon-wide re-initialisation: the app is still queued or
        in initialize() — never restart it mid-start (review finding)."""
        app, clock = _make_app(app_states={"zwave_health_checker": "loaded"})
        self._others_registered(app)
        _advance_to(app, clock, 120)
        app.ad.app_states["zwave_health_checker"] = "initializing"
        _advance_to(app, clock, 180)
        app.restart_app.assert_not_called()

        app.ad.app_states["zwave_health_checker"] = "idle"  # started, no register
        _advance_to(app, clock, 300)
        app.restart_app.assert_not_called()  # clock started at the 240s tick
        # Surfacing still counts from when it became expected.
        assert _sensor(app)[1]["zwave"]["status"] == "critical"
        _advance_to(app, clock, 360)
        app.restart_app.assert_called_once_with("zwave_health_checker")

    def test_app_appdaemon_never_finishes_starting_is_still_reported(self):
        app, clock = _make_app(
            app_states={"zwave_health_checker": "initializing"}  # hung
        )
        self._others_registered(app)
        _advance_to(app, clock, 1800)
        app.restart_app.assert_not_called()
        reg = _sensor(app)[1]["zwave"]["checks"][0]
        assert reg["status"] == "critical"
        assert (
            "AppDaemon has not finished starting app 'zwave_health_checker' "
            "(state: initializing)" in reg["detail"]
        )

    def test_failed_initialize_is_restarted(self):
        app, clock = _make_app()
        app.ad.app_states["zwave_health_checker"] = "initialize_error"
        self._others_registered(app)
        _advance_to(app, clock, 120)
        app.restart_app.assert_called_once_with("zwave_health_checker")

    def test_app_busy_in_a_callback_counts_as_started(self):
        app, clock = _make_app()
        app.ad.app_states["zwave_health_checker"] = (
            "RepairableNetworkProtocolChecker._on_startup for zwave_health_checker"
        )
        self._others_registered(app)
        _advance_to(app, clock, 120)
        app.restart_app.assert_called_once_with("zwave_health_checker")

    def test_slow_restart_rearms_the_backoff_from_when_it_finished(self):
        """A restart that takes longer than the backoff must still be followed
        by a full window to register (review finding, round 4)."""
        app, clock = _make_app()
        self._others_registered(app)
        _advance_to(app, clock, 120)
        app.restart_app.assert_called_once()

        # AppDaemon takes ~7 minutes to get through the restart.
        app.ad.app_states["zwave_health_checker"] = "initializing"
        _advance_to(app, clock, 600)
        app.restart_app.assert_called_once()
        app.ad.app_states["zwave_health_checker"] = "idle"
        _advance_to(app, clock, 660)  # finished: window re-armed from here
        _advance_to(app, clock, 840)
        app.restart_app.assert_called_once()  # 660 + 240 = 900 not reached
        _advance_to(app, clock, 900)
        assert app.restart_app.call_count == 2
        assert any(
            "(attempt 2/3)" in line and "240s after AppDaemon last started it" in line
            for line in _log_lines(app, "WARNING")
        )

    def test_not_registered_page_ignores_per_checker_for_overrides(self):
        """ups/shade_gateway page at 0s for what they report; an app that
        never started gets the default gate like every other (review)."""
        app, clock = _make_app({
            "alert_for_overrides": {
                "zwave": {"critical": 0},
                "mqtt_broker": {"critical": 1800},
            },
        })
        self._others_registered(app)
        _advance_to(app, clock, 300)
        assert _sensor(app)[1]["zwave"]["status"] == "critical"
        assert _posted_alerts(app) == []  # not paged at surfacing
        _advance_to(app, clock, 540)
        assert _posted_alerts(app) == []
        _advance_to(app, clock, 600)  # default critical gate: 300s
        assert [a["labels"]["checker"] for a in _posted_alerts(app)] == ["zwave"]

    def test_restart_app_returning_a_coroutine_is_not_claimed_as_a_restart(self):
        app, clock = _make_app()
        self._others_registered(app)

        async def _never_awaited():
            return None

        coro = _never_awaited()
        app.restart_app.return_value = coro
        _advance_to(app, clock, 120)
        assert coro.cr_frame is None  # closed, so no "never awaited" warning
        assert any(
            "returned an un-awaited coroutine — app NOT restarted" in line
            for line in _log_lines(app, "ERROR")
        )
        assert not any("restarted app" in l for l in _log_lines(app))

    def test_no_restart_app_is_warned_at_start_and_still_surfaces(self):
        # Build the normal harness, then take restart_app away and restart.
        app, clock = _make_app()
        app.restart_app = None
        app.log.reset_mock()
        _start(app)
        assert any(
            "AppDaemon offers no restart_app()" in line
            for line in _log_lines(app, "WARNING")
        )
        self._others_registered(app)
        _advance_to(app, clock, 300)
        assert _sensor(app)[1]["zwave"]["status"] == "critical"

    def test_app_states_are_never_read_on_the_event_loop(self):
        """On AppDaemon's event loop get_state() returns a Task, not states —
        found running the controller in a real AppDaemon 4.5.13. Start-up
        must leave the read to the heartbeat tick (a worker thread)."""
        app, clock = _make_app()
        app.get_state.reset_mock()
        prov = MagicMock()
        prov.ensure_helper = AsyncMock(return_value=False)
        prov.ensure_script = AsyncMock(return_value=False)
        with patch(
            "health_checks.controller.health_check_controller.HAProvisioner",
            return_value=prov,
        ):
            _run(app._async_startup())
            _drain(app)
        assert not any(
            c.kwargs.get("namespace") == "admin" for c in app.get_state.call_args_list
        )
        armed = [l for l in _log_lines(app, "INFO") if "Registration watchdog armed" in l]
        assert armed == [
            "Registration watchdog armed: 4 configured checker app(s) tracked, "
            "grace 300s, first restart 120s after AppDaemon has started the app, "
            "at most 3 restart(s) per checker"
        ]

    def test_first_tick_during_appdaemon_start_up_is_not_reported_as_armed(self):
        """The first tick can run before AppDaemon has created the other apps
        (no app.<name> entity yet): neither "armed: 0" nor "found no checker
        app" may latch on it (review finding)."""
        app, clock = _make_app(
            app_config={"garage_door_notify": APP_CONFIG["garage_door_notify"]}
        )
        assert not any("Registration watchdog armed" in l for l in _log_lines(app))
        assert not any("found no enabled app" in l for l in _log_lines(app))

        app.ad.app_config = dict(APP_CONFIG)  # AppDaemon has created the apps
        _advance_to(app, clock, 60)
        armed = [l for l in _log_lines(app, "INFO") if "Registration watchdog armed" in l]
        assert len(armed) == 1 and "armed: 4 configured checker app(s)" in armed[0]
        _advance_to(app, clock, 900)
        assert not any("found no enabled app" in l for l in _log_lines(app))

    def test_detail_with_restarts_off_never_reads_as_pending(self):
        """registration_restart_attempts: 0 must not leave the detail at
        "restart … pending", which the Shepherd treats as "skip" (round 8)."""
        app, clock = _make_app({"registration_restart_attempts": 0})
        self._others_registered(app)
        _advance_to(app, clock, 300)
        detail = _sensor(app)[1]["zwave"]["checks"][0]["detail"]
        assert "pending" not in detail and "retrying" not in detail
        assert "automatic restarts are off" in detail
        app.restart_app.assert_not_called()

    def test_detail_after_failed_reads_does_not_claim_a_config_error(self):
        """No successful read yet: say the app list is unreadable, not that
        no app is configured (which the Shepherd escalates as a config error)."""
        app, clock = _make_app(app_config=None)
        _register(app, "zwave_batteries")
        _advance_to(app, clock, 360)
        detail = _sensor(app)[1]["zwave"]["checks"][0]["detail"]
        assert "app list could not be read" in detail
        assert "is configured" not in detail

    def test_one_failed_read_does_not_turn_a_config_error_into_a_read_error(self):
        """The detail branches the Shepherd keys on must not flip on a single
        transient read failure (review round 9)."""
        app, clock = _make_app()
        self._others_registered(app)
        _register(app, "zwave")
        _command(app, "register_checker", {
            "checker_id": "orphan", "checker_name": "Orphan",
            "check_names": ["Ping"], "dependencies": [{"checker_id": "zigbee_typo"}],
        })
        _advance_to(app, clock, 360)
        key = "no checker app with checker_id 'zigbee_typo' is configured"
        assert key in _sensor(app)[1]["zigbee_typo"]["checks"][0]["detail"]
        app.ad.fail_read = lambda: True
        _advance_to(app, clock, 420)
        assert key in _sensor(app)[1]["zigbee_typo"]["checks"][0]["detail"]

    def test_crash_loop_cannot_reset_its_restart_budget(self):
        """Registers, dies, is restarted, registers, dies again: one episode,
        so restarts stay bounded and it pages (review round 9)."""
        app, clock = _make_app()
        self._others_registered(app)
        _register(app, "zwave")

        def _die() -> None:
            app.ad.app_states["zwave_health_checker"] = "initialize_error"

        def _comes_back(name: str) -> None:
            app.ad.app_states["zwave_health_checker"] = "idle"
            _register(app, "zwave")  # ...and dies again shortly after

        app.restart_app.side_effect = _comes_back
        for cycle in range(6):
            _die()
            _advance_to(app, clock, clock.t - 1000.0 + 600)
        assert app.restart_app.call_count == 3  # bounded across the cycles
        assert any(
            "missing again" in l and "resuming its episode" in l
            for l in _log_lines(app, "WARNING")
        )
        _die()
        _advance_to(app, clock, clock.t - 1000.0 + 600)
        assert app.restart_app.call_count == 3
        assert _sensor(app)[1]["zwave"]["status"] == "critical"
        assert any(a["labels"]["checker"] == "zwave" for a in _posted_alerts(app))
        # The detail names the loop and times this absence (since the budget
        # ran out it has stayed down), not the whole episode.
        entry = app._unregistered["zwave"]
        assert entry["missing_since"] > entry["since"]
        detail = _sensor(app)[1]["zwave"]["checks"][0]["detail"]
        minutes = round((clock.t - entry["missing_since"]) / 60)
        assert f"not registered with the controller after {minutes} min" in detail
        assert "(crash loop)" in detail
        assert "brought it back but it did not stay registered" in detail

    def test_disabled_then_reenabled_app_starts_a_fresh_budget(self):
        """Forgetting a departed checker forgets its episode too (round 10)."""
        app, clock = _make_app()
        self._others_registered(app)
        _advance_to(app, clock, 120)  # restart 1
        _register(app, "zwave")
        assert "zwave" in app._reg_recent
        zwave_app = app.ad.app_config.pop("zwave_health_checker")
        batteries = app.ad.app_config.pop("zwave_battery_checker")
        app._checkers.pop("zwave_batteries")  # nothing depends on zwave now
        _advance_to(app, clock, 180)
        assert "zwave" not in app._checkers
        assert "zwave" not in app._reg_recent
        app.ad.app_config["zwave_health_checker"] = zwave_app  # re-enabled
        app.ad.app_config["zwave_battery_checker"] = batteries
        _advance_to(app, clock, 240)
        assert app._unregistered["zwave"]["attempts"] == 0
        assert app._unregistered["zwave"]["surfaced"] is False

    def test_registration_that_holds_starts_a_fresh_budget(self):
        app, clock = _make_app()
        self._others_registered(app)
        _advance_to(app, clock, 120)  # restart 1
        _register(app, "zwave")
        _advance_to(app, clock, 120 + 1860)  # held for 31 minutes
        assert "zwave" not in app._reg_recent
        app.ad.app_states["zwave_health_checker"] = "initialize_error"
        _advance_to(app, clock, 120 + 1860 + 60)
        assert app._unregistered["zwave"]["attempts"] == 0

    def test_all_registered_line_waits_for_a_settled_app_list(self):
        app, clock = _make_app(
            app_config={"garage_door_notify": APP_CONFIG["garage_door_notify"]}
        )
        assert not any("expected checkers registered" in l for l in _log_lines(app))
        app.ad.app_config = dict(APP_CONFIG)
        for checker_id in ("zwave", "zwave_batteries", "mqtt_broker",
                           "basement_lights"):
            _register(app, checker_id)
        _advance_to(app, clock, 60)
        done = [l for l in _log_lines(app, "INFO") if "expected checkers registered" in l]
        assert done == [
            "Registration watchdog: all 4 expected checkers registered "
            "(4 configured apps)"
        ]

    def test_reading_app_states_that_hold_no_checker_app_is_warned_once(self):
        config = {"garage_door_notify": APP_CONFIG["garage_door_notify"]}
        app, clock = _make_app(app_config=config)
        _advance_to(app, clock, 600)
        warnings = [
            l for l in _log_lines(app, "WARNING")
            if "found no enabled app under 'health_checks.checker_apps.'" in l
        ]
        assert len(warnings) == 1
        assert any(
            "Registration watchdog armed: 0 configured checker app(s)" in l
            for l in _log_lines(app, "INFO")
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
