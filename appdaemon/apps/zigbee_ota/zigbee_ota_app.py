"""Zigbee OTA orchestrator — sequentially installs pending Zigbee2MQTT OTA
firmware updates (one device at a time) until the fleet is clean.

The decision logic lives in :mod:`ota_coordinator`; this app is the I/O shim:

- Home Assistant ``update.*`` entities (via the HASS plugin) are the source of
  truth for which devices still need firmware, and a Home Assistant template
  is what tells the app which of them are Zigbee2MQTT devices.
- Retained Zigbee2MQTT topics (via the MQTT plugin) provide per-device
  availability (offline devices are skipped and retried when they regain
  power), live progress, and — when it happens to arrive — a second copy of
  the device list.
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

# Which update entities belong to Zigbee2MQTT. Z2M's MQTT discovery gives every
# device a Home Assistant device whose identifiers carry the Z2M IEEE address
# (``('mqtt', 'zigbee2mqtt_0x943469fffe05cb47')``), which nothing else does.
# Asking Home Assistant every tick is the reliable route: the retained
# ``bridge/devices`` document is delivered when AppDaemon's MQTT plugin
# subscribes — seconds before this app registers its listener — and is never
# replayed for an app restart.
Z2M_UPDATE_ENTITY_TEMPLATE = (
    "{% set found = namespace(ids=[]) %}"
    "{%- for e in integration_entities('mqtt') -%}"
    "{%- if e.startswith('update.') and "
    "'zigbee2mqtt_' in (device_attr(e, 'identifiers') | string) -%}"
    "{%- set found.ids = found.ids + [e] -%}"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{{ found.ids | tojson }}"
)


def ha_safe(value: Any) -> Any:
    """Render a value so Home Assistant actually receives it.

    AppDaemon prunes anything equal to ``None`` or ``False`` from the payload it
    POSTs to ``/api/states`` — and in Python ``0 == False``, so zeros disappear
    too. A device count of 0, ``paused: False`` and ``failed_attempts: 0`` are
    all meaningful here, so booleans and numbers are all sent as strings. Every
    number, not just the zeros: an attribute that is a string at 0 and a number
    otherwise breaks any template that compares it.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, dict):
        return {key: ha_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [ha_safe(item) for item in value]
    return value


class ZigbeeOtaOrchestrator(hass.Hass):
    def initialize(self) -> None:
        args = self.args or {}
        self._mqtt_namespace = args.get("mqtt_namespace", "mqtt")
        self._base_topic = args.get("base_topic", "zigbee2mqtt").rstrip("/")
        self._scan_interval_s = int(args.get("scan_interval_s", 120))
        self._status_sensor = args.get("status_sensor", SENSOR_ENTITY_ID)
        self._pause_entity = args.get("pause_entity", PAUSE_ENTITY_ID)
        self._last_z2m_count = -1  # only log the device count when it changes
        self._coordinator = OtaCoordinator(
            include_globs=list(args.get("include_globs", ["update.*"])),
            exclude_globs=list(args.get("exclude_globs", [])),
            retry_base_s=float(args.get("retry_base_s", 900)),
            retry_max_s=float(args.get("retry_max_s", 21600)),
            busy_backoff_s=float(args.get("busy_backoff_s", 300)),
            online_retry_grace_s=float(args.get("online_retry_grace_s", 60)),
            progress_stall_s=float(args.get("progress_stall_s", 2700)),
            update_timeout_s=float(args.get("update_timeout_s", 14400)),
            park_recheck_s=float(args.get("park_recheck_s", 86400)),
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
            z2m_entities = await self._z2m_update_entities()
            if z2m_entities is None:
                reason = "Zigbee2MQTT device list unavailable from Home Assistant"
                self._coordinator.mark_identity_unavailable(reason)
                self._last_z2m_count = -1
                self.log("%s — starting nothing this tick" % reason, level="WARNING")
            elif not self._coordinator.set_z2m_entities(z2m_entities):
                self._last_z2m_count = -1
                self.log(
                    "Home Assistant reported no Zigbee2MQTT update entities; "
                    "keeping the last known list and starting nothing",
                    level="WARNING",
                )
            elif not self._coordinator.identity_ready:
                # Never silent: this is what a template that stopped matching
                # looks like, and it manages nothing until someone notices.
                # identity_ready, not the answer itself — the retained bridge
                # document can carry the fleet on its own.
                self._last_z2m_count = -1
                self.log(
                    "Home Assistant reported no Zigbee2MQTT update entities; "
                    "managing nothing until that changes",
                    level="WARNING",
                )
            elif self._coordinator.z2m_device_count != self._last_z2m_count:
                self._last_z2m_count = self._coordinator.z2m_device_count
                self.log(
                    "Managing %d Zigbee2MQTT update entities" % self._last_z2m_count
                )
            # Domain queries can't combine with attribute="all" in AppDaemon,
            # so take the full state dump and filter to update.* ourselves.
            snapshot = await self.get_state()
            update_entities = (
                {
                    entity_id: payload
                    for entity_id, payload in snapshot.items()
                    if entity_id.startswith("update.")
                    and isinstance(payload, dict)
                }
                if isinstance(snapshot, dict)
                else {}
            )
            if not update_entities:
                # AppDaemon hands back None mid-reconnect, and a Home Assistant
                # restart serves other domains before the update platform sets
                # up. Either way an empty update domain is not the truth, and
                # applying it would drop every device's backoff and every park
                # — the same defect the empty-identity guard exists for. The
                # check is on the filtered view, because that is what
                # refresh_entities would act on.
                reason = "no Home Assistant update entities in the state snapshot"
                self._coordinator.mark_identity_unavailable(reason)
                self.log("%s — starting nothing this tick" % reason, level="WARNING")
            else:
                self._coordinator.refresh_entities(update_entities)
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

    async def _z2m_update_entities(self) -> Optional[set[str]]:
        """Ask Home Assistant which ``update.*`` entities are Z2M devices.

        Returns ``None`` when the answer can't be trusted — the caller then
        starts nothing on this tick rather than guessing.
        """
        try:
            rendered = await self.render_template(Z2M_UPDATE_ENTITY_TEMPLATE)
        except Exception as exc:  # noqa: BLE001 - HA may be restarting
            self.log("Z2M entity lookup failed: %s" % exc, level="WARNING")
            return None
        if isinstance(rendered, str):
            try:
                rendered = json.loads(rendered)
            except (ValueError, TypeError):
                self.log(
                    "Z2M entity lookup returned unparseable output: %s"
                    % rendered[:200],
                    level="WARNING",
                )
                return None
        if not isinstance(rendered, (list, tuple, set)):
            self.log(
                "Z2M entity lookup returned %s, expected a list"
                % type(rendered).__name__,
                level="WARNING",
            )
            return None
        return {
            item
            for item in rendered
            if isinstance(item, str) and item.startswith("update.")
        }

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
            # str(): AppDaemon prunes values equal to None/False from the POST
            # body, and 0 == False, so a bare 0 would post no state at all and
            # Home Assistant answers 400.
            state=str(remaining),
            attributes=ha_safe(
                {
                    "friendly_name": "Zigbee OTA Orchestrator",
                    "icon": "mdi:progress-download",
                    **status,
                }
            ),
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
