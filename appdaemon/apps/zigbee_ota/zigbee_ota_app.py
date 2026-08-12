"""Zigbee OTA orchestrator — sequentially installs pending Zigbee2MQTT OTA
firmware updates (one device at a time) until the fleet is clean.

The decision logic lives in :mod:`ota_coordinator`; this app is the I/O shim:

- Home Assistant ``update.*`` entities (via the HASS plugin) are the source of
  truth for which devices still need firmware.
- Retained Zigbee2MQTT topics (via the MQTT plugin) provide the device list,
  per-device availability (offline bulbs are skipped and retried when they
  regain power) and live progress.
- Updates are started by publishing to
  ``<base_topic>/bridge/request/device/ota_update/update`` and finish when the
  matching ``.../response/device/ota_update/update`` arrives.

All callbacks are coroutines so they run on AppDaemon's event loop and never
mutate coordinator state concurrently.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import hassapi as hass

from zigbee_ota.ota_coordinator import OtaCoordinator

SENSOR_ENTITY_ID = "sensor.zigbee_ota_orchestrator"
PAUSE_ENTITY_ID = "input_boolean.zigbee_ota_pause"


class ZigbeeOtaOrchestrator(hass.Hass):
    def initialize(self) -> None:
        args = self.args or {}
        self._mqtt_namespace = args.get("mqtt_namespace", "mqtt")
        self._base_topic = args.get("base_topic", "zigbee2mqtt").rstrip("/")
        self._scan_interval_s = int(args.get("scan_interval_s", 120))
        self._status_sensor = args.get("status_sensor", SENSOR_ENTITY_ID)
        self._pause_entity = args.get("pause_entity", PAUSE_ENTITY_ID)
        self._coordinator = OtaCoordinator(
            include_globs=list(args.get("include_globs", ["update.*"])),
            exclude_globs=list(args.get("exclude_globs", [])),
            retry_base_s=float(args.get("retry_base_s", 900)),
            retry_max_s=float(args.get("retry_max_s", 21600)),
            busy_backoff_s=float(args.get("busy_backoff_s", 300)),
            online_retry_grace_s=float(args.get("online_retry_grace_s", 60)),
            progress_stall_s=float(args.get("progress_stall_s", 2700)),
            update_timeout_s=float(args.get("update_timeout_s", 14400)),
        )
        self.log(
            "ZigbeeOtaOrchestrator starting: globs=%s scan_interval=%ss"
            % (self._coordinator.include_globs, self._scan_interval_s)
        )
        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: dict[str, Any]) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        self.listen_event(
            self._on_mqtt_message, "MQTT_MESSAGE", namespace=self._mqtt_namespace
        )
        self.run_every(
            self._tick, f"now+{self._scan_interval_s}", self._scan_interval_s
        )
        await self._tick({})
        self.log("ZigbeeOtaOrchestrator started")

    # ------------------------------------------------------------------
    # MQTT ingest
    # ------------------------------------------------------------------

    async def _on_mqtt_message(
        self, event_name: str, data: dict[str, Any], kwargs: dict[str, Any]
    ) -> None:
        topic = data.get("topic")
        if not topic or not topic.startswith(f"{self._base_topic}/"):
            return  # includes topic=None connect/disconnect pseudo-events
        payload = self._parse_json(data.get("payload"))
        suffix = topic[len(self._base_topic) + 1 :]

        if suffix == "bridge/devices":
            if isinstance(payload, list):
                self._coordinator.set_known_devices(
                    {
                        dev.get("friendly_name")
                        for dev in payload
                        if isinstance(dev, dict)
                        and dev.get("type") != "Coordinator"
                        and dev.get("friendly_name")
                    }
                )
            return

        if suffix == "bridge/response/device/ota_update/update":
            if isinstance(payload, dict):
                self.log("OTA response: %s" % json.dumps(payload)[:500])
                self._coordinator.on_update_response(payload)
                await self._tick({})
            return

        if suffix.startswith("bridge/"):
            return

        parts = suffix.split("/")
        if len(parts) == 2 and parts[1] == "availability":
            online = self._parse_availability(payload, data.get("payload"))
            if online is None:
                return
            if self._coordinator.set_availability(parts[0], online):
                self.log("%s back online — scheduling retry" % parts[0])
                await self._tick({})
            return

        if len(parts) == 1 and isinstance(payload, dict) and "update" in payload:
            update_obj = payload.get("update")
            if isinstance(update_obj, dict):
                self._coordinator.on_device_update_obj(parts[0], update_obj)

    # ------------------------------------------------------------------
    # Tick: refresh queue, decide, act, report
    # ------------------------------------------------------------------

    async def _tick(self, kwargs: dict[str, Any]) -> None:
        try:
            snapshot = await self.get_state("update", attribute="all")
            if isinstance(snapshot, dict):
                self._coordinator.refresh_entities(
                    {
                        entity_id: (payload or {})
                        for entity_id, payload in snapshot.items()
                    }
                )
            if await self._paused():
                self._publish_status(paused=True)
                return
            decision = self._coordinator.decide()
            if decision is not None:
                self.log(
                    "Requesting OTA update for %s (transaction %s)"
                    % (decision.friendly_name, decision.transaction)
                )
                self.call_service(
                    "mqtt/publish",
                    topic=f"{self._base_topic}/bridge/request/device/ota_update/update",
                    payload=json.dumps(
                        {"id": decision.friendly_name, "transaction": decision.transaction}
                    ),
                    namespace=self._mqtt_namespace,
                )
            self._publish_status(paused=False)
        except Exception as exc:  # noqa: BLE001 - keep the orchestrator alive
            self.log("tick failed: %s" % exc, level="ERROR")

    async def _paused(self) -> bool:
        """Optional kill switch: create input_boolean.zigbee_ota_pause in HA and
        turn it on to stop new updates (an in-flight update still finishes)."""
        try:
            state = await self.get_state(self._pause_entity)
        except Exception:  # noqa: BLE001 - helper may not exist; not paused
            return False
        return state == "on"

    def _publish_status(self, paused: bool) -> None:
        status = self._coordinator.status()
        status["paused"] = paused
        remaining = status.pop("remaining")
        self.set_state(
            self._status_sensor,
            state=remaining,
            attributes={
                "friendly_name": "Zigbee OTA Orchestrator",
                "icon": "mdi:progress-download",
                **status,
            },
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: Any) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        if not isinstance(raw, str) or not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_availability(payload: Any, raw: Any) -> Optional[bool]:
        if isinstance(payload, dict) and "state" in payload:
            return payload.get("state") == "online"
        if isinstance(raw, str) and raw in ("online", "offline"):
            return raw == "online"
        return None
