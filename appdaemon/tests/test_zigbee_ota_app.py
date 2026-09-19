"""Unit tests for ZigbeeOtaOrchestrator (the AppDaemon adapter).

Mocks AppDaemon methods and the MQTT plugin — no real broker or HA access.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))

from zigbee_ota.zigbee_ota_app import ZigbeeOtaOrchestrator, ha_safe  # noqa: E402


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


DEFAULT_ARGS: Dict[str, Any] = {
    "include_globs": ["update.*hue*"],
    "scan_interval_s": 120,
    "mqtt_namespace": "mqtt",
    "base_topic": "zigbee2mqtt",
}


def _entity(name: str, state: str = "on", in_progress: bool = False) -> dict[str, Any]:
    return {
        "state": state,
        "attributes": {
            "friendly_name": name,
            "installed_version": "100",
            "latest_version": "200",
            "in_progress": in_progress,
        },
    }


def _make_app(
    extra_args: dict | None = None,
    update_snapshot: dict | None = None,
    pause_state: str | None = None,
    z2m_entities: Any = None,
    template_error: Exception | None = None,
) -> ZigbeeOtaOrchestrator:
    """Build the app with AppDaemon mocked out.

    ``z2m_entities`` is what Home Assistant answers when asked which update
    entities are Zigbee2MQTT devices; by default every entity in the snapshot.
    """
    app = ZigbeeOtaOrchestrator(MagicMock(), MagicMock())
    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    snapshot = update_snapshot if update_snapshot is not None else {}
    vouched = list(snapshot) if z2m_entities is None else list(z2m_entities)

    async def fake_get_state(entity: str | None = None, **kwargs: Any) -> Any:
        if entity is None:
            # Full state dump: include non-update noise to prove filtering.
            return {
                **snapshot,
                "light.some_light": {"state": "on", "attributes": {}},
                "sensor.bad_payload": None,
            }
        return pause_state

    async def fake_render_template(template: str, **kwargs: Any) -> Any:
        if template_error is not None:
            raise template_error
        # AppDaemon literal_evals the rendered text before handing it back.
        return list(vouched)

    app.render_template = MagicMock(side_effect=fake_render_template)
    app.get_state = MagicMock(side_effect=fake_get_state)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.create_task = MagicMock()
    app.log = MagicMock()
    app.initialize()
    return app


def _published_requests(app: ZigbeeOtaOrchestrator) -> list[dict[str, Any]]:
    calls = []
    for call in app.call_service.call_args_list:
        if call.args and call.args[0] == "mqtt/publish":
            calls.append(json.loads(call.kwargs["payload"]))
    return calls


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_initialize_schedules_startup() -> None:
    app = _make_app()
    app.run_in.assert_called_once()


def test_async_startup_wires_mqtt_and_timer_and_ticks() -> None:
    app = _make_app(update_snapshot={"update.hue_a": _entity("hue_a")})
    _run(app._async_startup())
    app.listen_event.assert_called_once()
    assert app.listen_event.call_args.args[1] == "MQTT_MESSAGE"
    assert app.listen_event.call_args.kwargs["namespace"] == "mqtt"
    app.run_every.assert_called_once()
    # First tick already requested the first update.
    requests = _published_requests(app)
    assert len(requests) == 1 and requests[0]["id"] == "hue_a"


# ---------------------------------------------------------------------------
# Tick behaviour
# ---------------------------------------------------------------------------


def test_tick_publishes_to_bridge_request_topic_and_status_sensor() -> None:
    app = _make_app(update_snapshot={"update.hue_a": _entity("hue_a")})
    _run(app._tick({}))
    topic = app.call_service.call_args.kwargs["topic"]
    assert topic == "zigbee2mqtt/bridge/request/device/ota_update/update"
    assert app.call_service.call_args.kwargs["namespace"] == "mqtt"
    app.set_state.assert_called_once()
    sensor_call = app.set_state.call_args
    assert sensor_call.args[0] == "sensor.zigbee_ota_orchestrator"
    assert sensor_call.kwargs["state"] == "1"
    assert sensor_call.kwargs["attributes"]["in_flight"]["device"] == "hue_a"


def test_tick_second_call_does_not_start_second_update() -> None:
    app = _make_app(
        update_snapshot={
            "update.hue_a": _entity("hue_a"),
            "update.hue_b": _entity("hue_b"),
        }
    )
    _run(app._tick({}))
    _run(app._tick({}))
    assert len(_published_requests(app)) == 1


def test_paused_via_input_boolean_blocks_new_updates() -> None:
    app = _make_app(
        update_snapshot={"update.hue_a": _entity("hue_a")}, pause_state="on"
    )
    _run(app._tick({}))
    assert _published_requests(app) == []
    assert app.set_state.call_args.kwargs["attributes"]["paused"] == "true"


def test_tick_survives_get_state_failure() -> None:
    app = _make_app()

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("HA unavailable")

    app.get_state = MagicMock(side_effect=boom)
    _run(app._tick({}))  # must not raise
    app.log.assert_called()


# ---------------------------------------------------------------------------
# MQTT routing
# ---------------------------------------------------------------------------


def test_bridge_devices_source_does_not_log_an_empty_fleet_warning() -> None:
    """The retained document can carry the fleet on its own; warning then
    would cry wolf every tick while updates are actually running."""
    app = _make_app(
        update_snapshot={"update.hue_a": _entity("hue_a")}, z2m_entities=[]
    )
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/bridge/devices",
                "payload": json.dumps([{"friendly_name": "hue_a", "type": "Router"}]),
            },
            {},
        )
    )
    _run(app._tick({}))
    assert len(_published_requests(app)) == 1
    warnings = [
        call
        for call in app.log.call_args_list
        if call.kwargs.get("level") == "WARNING"
    ]
    assert warnings == []
    attrs = app.set_state.call_args.kwargs["attributes"]
    assert attrs["identity_source"] == "zigbee2mqtt bridge"
    assert attrs["z2m_devices_known"] == "1"


def test_bridge_devices_updates_known_set_and_filters_queue() -> None:
    app = _make_app(
        update_snapshot={
            "update.hue_a": _entity("hue_a"),
            "update.hue_ghost": _entity("hue_ghost"),
        },
        z2m_entities=[],  # HA vouches for nothing; bridge/devices is the source
    )
    devices = [
        {"friendly_name": "hue_a", "type": "Router"},
        {"friendly_name": "Coordinator", "type": "Coordinator"},
    ]
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"topic": "zigbee2mqtt/bridge/devices", "payload": json.dumps(devices)},
            {},
        )
    )
    _run(app._tick({}))
    requests = _published_requests(app)
    assert len(requests) == 1 and requests[0]["id"] == "hue_a"


def test_ota_response_routes_to_coordinator_and_triggers_next() -> None:
    app = _make_app(
        update_snapshot={
            "update.hue_a": _entity("hue_a"),
            "update.hue_b": _entity("hue_b"),
        }
    )
    _run(app._tick({}))
    first = _published_requests(app)[0]
    response = {
        "status": "ok",
        "transaction": first["transaction"],
        "data": {"id": first["id"]},
    }
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/bridge/response/device/ota_update/update",
                "payload": json.dumps(response),
            },
            {},
        )
    )
    requests = _published_requests(app)
    assert len(requests) == 2
    assert requests[1]["id"] == "hue_b"


def test_availability_json_and_plain_payloads() -> None:
    """Both retained payload shapes are understood. MQTT is the fast feed;
    Home Assistant's entity state is the one that is always there."""
    app = _make_app(
        update_snapshot={
            "update.hue_a": _entity("hue_a"),
            "update.hue_b": _entity("hue_b"),
        }
    )
    _run(app._tick({}))  # hue_a in flight, hue_b queued
    for offline_payload in (json.dumps({"state": "offline"}), "offline"):
        _run(
            app._on_mqtt_message(
                "MQTT_MESSAGE",
                {
                    "topic": "zigbee2mqtt/hue_b/availability",
                    "payload": offline_payload,
                },
                {},
            )
        )
        assert app._coordinator.status()["offline"] == ["hue_b"]
        _run(
            app._on_mqtt_message(
                "MQTT_MESSAGE",
                {"topic": "zigbee2mqtt/hue_b/availability", "payload": "online"},
                {},
            )
        )
        assert app._coordinator.status()["offline"] == []


def test_device_state_update_obj_feeds_progress() -> None:
    app = _make_app(update_snapshot={"update.hue_a": _entity("hue_a")})
    _run(app._tick({}))
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/hue_a",
                "payload": json.dumps(
                    {"state": "ON", "update": {"state": "updating", "progress": 42, "remaining": 600}}
                ),
            },
            {},
        )
    )
    _run(app._tick({}))
    attrs = app.set_state.call_args.kwargs["attributes"]
    assert attrs["in_flight"]["progress_pct"] == "42"


def test_none_topic_and_foreign_topics_ignored() -> None:
    app = _make_app()
    _run(app._on_mqtt_message("MQTT_MESSAGE", {"topic": None, "payload": None}, {}))
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE", {"topic": "other/topic", "payload": "x"}, {}
        )
    )
    app.call_service.assert_not_called()


# ---------------------------------------------------------------------------
# Device identity (fail closed)
# ---------------------------------------------------------------------------


def test_non_z2m_entities_never_get_an_ota_request() -> None:
    """A broad glob plus a non-Z2M update entity must publish nothing for it."""
    app = _make_app(
        extra_args={"include_globs": ["update.*"]},
        update_snapshot={
            "update.hue_a": _entity("hue_a"),
            "update.tom_haynes_version": _entity("Immich - Tom Version"),
        },
        z2m_entities=["update.hue_a"],
    )
    _run(app._tick({}))
    requests = _published_requests(app)
    assert [req["id"] for req in requests] == ["hue_a"]


def test_no_identity_answer_starts_nothing_on_the_first_tick() -> None:
    app = _make_app(
        extra_args={"include_globs": ["update.*"]},
        update_snapshot={"update.hue_a": _entity("hue_a")},
        template_error=RuntimeError("HA restarting"),
    )
    _run(app._tick({}))
    assert _published_requests(app) == []
    attrs = app.set_state.call_args.kwargs["attributes"]
    assert attrs["identity_source"].startswith("stale")
    assert "device list" in attrs["last_event"]


def test_identity_lookup_failure_holds_an_established_queue() -> None:
    """A working queue must not keep starting updates once HA stops answering."""
    app = _make_app(
        update_snapshot={
            "update.hue_a": _entity("hue_a"),
            "update.hue_b": _entity("hue_b"),
        }
    )
    _run(app._tick({}))
    first = _published_requests(app)[0]
    assert first["id"] == "hue_a"

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("HA restarting")

    app.render_template = MagicMock(side_effect=boom)
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/bridge/response/device/ota_update/update",
                "payload": json.dumps(
                    {
                        "status": "ok",
                        "transaction": first["transaction"],
                        "data": {"id": "hue_a"},
                    }
                ),
            },
            {},
        )
    )
    _run(app._tick({}))
    assert len(_published_requests(app)) == 1  # hue_b held back
    attrs = app.set_state.call_args.kwargs["attributes"]
    assert attrs["identity_source"].startswith("stale")
    assert "holding" in attrs["last_event"]


def test_unparseable_identity_answer_starts_nothing() -> None:
    app = _make_app(update_snapshot={"update.hue_a": _entity("hue_a")})
    app.render_template = MagicMock(
        side_effect=lambda *a, **kw: _as_coro("not a list at all")
    )
    _run(app._tick({}))
    assert _published_requests(app) == []


def _as_coro(value: Any) -> Any:
    async def _inner() -> Any:
        return value

    return _inner()


# ---------------------------------------------------------------------------
# Status sensor payload
# ---------------------------------------------------------------------------


def test_status_state_is_a_string_so_zero_survives() -> None:
    """AppDaemon drops values equal to None/False, and 0 == False, so an int 0
    state posts no state at all and Home Assistant answers 400."""
    app = _make_app(update_snapshot={})
    _run(app._tick({}))
    assert app.set_state.call_args.kwargs["state"] == "0"


def test_status_falsy_attributes_reach_home_assistant() -> None:
    app = _make_app(update_snapshot={})
    _run(app._tick({}))
    attrs = app.set_state.call_args.kwargs["attributes"]
    assert attrs["paused"] == "false"
    assert attrs["failed_attempts_this_run"] == "0"
    assert attrs["completed_count_this_run"] == "0"
    assert attrs["busy_until"] == ""
    assert attrs["z2m_devices_known"] == "0"
    assert attrs["in_flight"] == {}


def test_ha_safe_renders_every_number_as_a_string() -> None:
    """Consistent types: an attribute that is "0" at zero and 300 otherwise
    breaks any template that compares it."""
    assert ha_safe({"a": 3, "b": "x", "c": [0, {"d": True, "e": False}]}) == {
        "a": "3",
        "b": "x",
        "c": ["0", {"d": "true", "e": "false"}],
    }
    assert ha_safe(42.0) == "42"  # whole floats read as whole numbers
    assert ha_safe(42.5) == "42.5"
    assert ha_safe(None) == ""


def test_an_empty_identity_answer_is_logged_every_tick() -> None:
    """The template no longer matching is silent otherwise: nothing is queued,
    nothing fails, and the app would just sit there."""
    app = _make_app(
        update_snapshot={"update.hue_a": _entity("hue_a")}, z2m_entities=[]
    )
    _run(app._tick({}))
    _run(app._tick({}))
    warnings = [
        call
        for call in app.log.call_args_list
        if call.kwargs.get("level") == "WARNING"
        and "no Zigbee2MQTT update entities" in call.args[0]
    ]
    assert len(warnings) == 2
    assert _published_requests(app) == []
    attrs = app.set_state.call_args.kwargs["attributes"]
    assert attrs["identity_source"] == "none"
    assert attrs["z2m_devices_known"] == "0"
