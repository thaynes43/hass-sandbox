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

from zigbee_ota.zigbee_ota_app import ZigbeeOtaOrchestrator  # noqa: E402


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
) -> ZigbeeOtaOrchestrator:
    app = ZigbeeOtaOrchestrator(MagicMock(), MagicMock())
    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    snapshot = update_snapshot if update_snapshot is not None else {}

    async def fake_get_state(entity: str | None = None, **kwargs: Any) -> Any:
        if entity is None:
            # Full state dump: include non-update noise to prove filtering.
            return {
                **snapshot,
                "light.some_light": {"state": "on", "attributes": {}},
                "sensor.bad_payload": None,
            }
        return pause_state

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
    assert sensor_call.kwargs["state"] == 1
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
    assert app.set_state.call_args.kwargs["attributes"]["paused"] is True


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


def test_bridge_devices_updates_known_set_and_filters_queue() -> None:
    app = _make_app(
        update_snapshot={
            "update.hue_a": _entity("hue_a"),
            "update.hue_ghost": _entity("hue_ghost"),
        }
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
    app = _make_app(update_snapshot={"update.hue_a": _entity("hue_a")})
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/hue_a/availability",
                "payload": json.dumps({"state": "offline"}),
            },
            {},
        )
    )
    _run(app._tick({}))
    assert _published_requests(app) == []  # offline device is skipped
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"topic": "zigbee2mqtt/hue_a/availability", "payload": "online"},
            {},
        )
    )
    _run(app._tick({}))
    assert len(_published_requests(app)) == 1


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
    assert attrs["in_flight"]["progress_pct"] == 42


def test_none_topic_and_foreign_topics_ignored() -> None:
    app = _make_app()
    _run(app._on_mqtt_message("MQTT_MESSAGE", {"topic": None, "payload": None}, {}))
    _run(
        app._on_mqtt_message(
            "MQTT_MESSAGE", {"topic": "other/topic", "payload": "x"}, {}
        )
    )
    app.call_service.assert_not_called()
