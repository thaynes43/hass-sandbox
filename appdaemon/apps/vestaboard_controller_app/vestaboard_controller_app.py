"""Vestaboard Controller App — drives board automations and manages the frame queue."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from providers.secrets import resolve_arg_secret
from providers.vestaboard.vestaboard_client import VestaboardClient

from vestaboard_controller_app.frame_queue import BoardFrame, FrameQueue


# ---------------------------------------------------------------------------
# Automation registry
# ---------------------------------------------------------------------------

_AUTOMATION_CLASSES: dict[str, type] = {}


def _register_automations() -> None:
    """Lazy-import automation classes to avoid circular imports at module load."""
    global _AUTOMATION_CLASSES
    if _AUTOMATION_CLASSES:
        return
    from vestaboard_controller_app.automations.calendar_clock import CalendarClockAutomation
    from vestaboard_controller_app.automations.random_message import RandomMessageAutomation
    from vestaboard_controller_app.automations.random_art import RandomArtAutomation
    from vestaboard_controller_app.automations.ai_art_generator import AIArtGeneratorAutomation
    from vestaboard_controller_app.automations.calendar_summary import CalendarSummaryAutomation

    _AUTOMATION_CLASSES = {
        "calendar_clock": CalendarClockAutomation,
        "random_message": RandomMessageAutomation,
        "random_art": RandomArtAutomation,
        "ai_art_generator": AIArtGeneratorAutomation,
        "calendar_summary": CalendarSummaryAutomation,
    }


# ---------------------------------------------------------------------------
# Controller app
# ---------------------------------------------------------------------------


class VestaboardControllerApp(hass.Hass):
    """AppDaemon app that drives the Vestaboard board automations.

    Manages:
    - VestaboardClient for writing frames to the physical board.
    - FrameQueue for LIFO TTL/expiration/fallback semantics.
    - BoardAutomation instances that generate frames.
    - Command event listener for card/automation-driven requests.
    - Periodic tick to advance queue state.
    """

    SENSOR_ENTITY = "sensor.vestaboard_controller_status"
    RELAY_SCRIPT_ID = "vestaboard_controller_relay"
    COMMAND_EVENT = "vestaboard_controller_command"

    # ------------------------------------------------------------------
    # AppDaemon lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        cfg = self.args or {}

        # Vestaboard connection
        self._vb_ip: str = str(resolve_arg_secret(cfg, "vestaboard_ip", default=""))
        self._vb_api_key: str = str(resolve_arg_secret(cfg, "vestaboard_api_key", default=""))

        # HA provisioning credentials
        self._ha_url: str = str(resolve_arg_secret(cfg, "ha_url", default=""))
        self._ha_token_env: str = str(cfg.get("ha_token_env", ""))

        # Tick interval
        self._tick_interval_s: int = int(cfg.get("tick_interval_s", 15))

        # Automation configs from YAML
        self._automation_configs: dict[str, Any] = dict(
            cfg.get("automations") or {}
        )

        # Build VestaboardClient (no session yet — async context managed per write)
        self._client = VestaboardClient(
            ip=self._vb_ip,
            api_key=self._vb_api_key,
        )

        # Build FrameQueue
        self._queue = FrameQueue(log_fn=self.log)

        # Active automation instances: automation_id -> BoardAutomation
        self._automations: dict[str, Any] = {}

        # Timer handles for automation triggers: (automation_id, trigger_idx) -> handle
        self._trigger_handles: dict[tuple[str, int], Any] = {}

        # Set of currently active (enabled) automation IDs
        self._active_automations: set[str] = set()

        # Last known board state for status publishing
        self._last_write_ok: Optional[bool] = None

        self.log(
            f"VestaboardControllerApp initializing — ip={self._vb_ip!r} "
            f"tick_interval_s={self._tick_interval_s}",
            level="INFO",
        )

        # Defer async startup so AppDaemon event loop is running
        self.run_in(self._async_startup_wrapper, 0)

    def _async_startup_wrapper(self, kwargs: dict) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Provision HA entities, register listeners, init automations."""
        await self._provision_entities()

        # Register command event listener
        self.listen_event(self._on_command, self.COMMAND_EVENT)
        self.log(f"Listening for {self.COMMAND_EVENT} events", level="INFO")

        # Register periodic tick
        from datetime import datetime as dt
        await self.run_every(self._tick_wrapper, dt.now(), self._tick_interval_s)
        self.log(f"Tick timer registered every {self._tick_interval_s}s", level="INFO")

        # Initialise automations from config
        self._init_automations()

        # Publish initial status
        self._publish_status()

        self.log("VestaboardControllerApp startup complete", level="INFO")

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    async def _provision_entities(self) -> None:
        """Create relay script and status sensor via HAProvisioner."""
        if not self._ha_url or not self._ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        from providers.ha_provisioner import HAProvisioner

        prov = HAProvisioner(ha_url=self._ha_url, ha_token_env=self._ha_token_env)

        # Relay script
        try:
            created = await prov.ensure_script(
                self.RELAY_SCRIPT_ID,
                {
                    "alias": "Vestaboard Controller Relay",
                    "description": "Relays vestaboard controller commands to AppDaemon",
                    "mode": "queued",
                    "max": 10,
                    "fields": {
                        "command": {
                            "name": "Command",
                            "required": True,
                            "selector": {"text": {}},
                        },
                        "payload": {
                            "name": "Payload",
                            "required": False,
                            "selector": {"text": {}},
                        },
                    },
                    "sequence": [
                        {
                            "event": self.COMMAND_EVENT,
                            "event_data": {
                                "command": "{{ command }}",
                                "payload": "{{ payload | default('{}') }}",
                            },
                        }
                    ],
                },
            )
            msg = "created" if created else "already exists"
            self.log(
                f"Relay script.{self.RELAY_SCRIPT_ID} {msg}",
                level="INFO" if created else "DEBUG",
            )
        except Exception as exc:
            self.log(f"Failed to provision relay script: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: dict) -> None:
        """Route incoming commands to the appropriate handler."""
        command = str(data.get("command", "")).strip()
        raw_payload = data.get("payload", "{}")

        self.log(f"Command received: {command!r}", level="INFO")

        # Parse payload
        try:
            if isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                payload = json.loads(raw_payload) if raw_payload else {}
        except (json.JSONDecodeError, TypeError) as exc:
            self.log(f"Failed to parse payload: {exc!r} raw={raw_payload!r}", level="WARNING")
            payload = {}

        # Route
        if command == "push_frame":
            self._handle_push_frame(payload)
        elif command == "activate_automation":
            automation_id = str(payload.get("automation_id", ""))
            self._activate_automation(automation_id)
        elif command == "deactivate_automation":
            automation_id = str(payload.get("automation_id", ""))
            self._deactivate_automation(automation_id)
        elif command == "clear_board":
            self._handle_clear_board()
        elif command == "set_automation_config":
            automation_id = str(payload.get("automation_id", ""))
            new_config = dict(payload.get("config") or {})
            self._handle_set_automation_config(automation_id, new_config)
        elif command == "generate_random_message":
            self.create_task(self._handle_generate_random_message(payload))
        elif command == "generate_random_art":
            self.create_task(self._handle_generate_random_art(payload))
        elif command == "generate_ai_art":
            self.create_task(self._handle_generate_ai_art(payload))
        else:
            self.log(f"Unknown command: {command!r}", level="WARNING")

    def _handle_push_frame(self, payload: dict) -> None:
        """Push a pre-built frame to the queue."""
        characters = payload.get("characters")
        if not characters:
            self.log("push_frame: missing 'characters' in payload", level="WARNING")
            return

        source = str(payload.get("source", "user"))
        source_label = str(payload.get("source_label", "User"))
        ttl_s = payload.get("ttl_s")
        expiration_s = payload.get("expiration_s")
        override_ttl = bool(payload.get("override_ttl", True))

        if ttl_s is not None:
            ttl_s = int(ttl_s)
        if expiration_s is not None:
            expiration_s = int(expiration_s)

        now = time.time()
        frame = BoardFrame(
            frame_id=uuid.uuid4().hex,
            characters=characters,
            source=source,
            source_label=source_label,
            ttl_s=ttl_s,
            expiration_s=expiration_s,
            override_ttl=override_ttl,
            created_at=now,
        )

        self.log(
            f"push_frame: source={source!r} override_ttl={override_ttl} "
            f"ttl_s={ttl_s} expiration_s={expiration_s}",
            level="INFO",
        )

        action = self._queue.push(frame, now)
        if action.display_frame:
            self.create_task(self._write_to_board(action.display_frame.characters))
        self._publish_status()

    def _handle_clear_board(self) -> None:
        """Clear all frames from the queue and blank the board."""
        self.log("clear_board command received", level="INFO")
        from providers.vestaboard.character_encoding import blank_grid

        self._queue.clear()
        blank = blank_grid()
        self.create_task(self._write_to_board(blank))
        self._publish_status()

    def _handle_set_automation_config(self, automation_id: str, new_config: dict) -> None:
        """Update config for a running automation."""
        if automation_id not in self._automations:
            self.log(
                f"set_automation_config: unknown automation {automation_id!r}",
                level="WARNING",
            )
            return
        self.log(
            f"Updating config for automation {automation_id!r}: {new_config}",
            level="INFO",
        )
        self._automations[automation_id].config.update(new_config)

    async def _handle_generate_random_message(self, payload: dict) -> None:
        """Generate and push a random message from RandomMessageAutomation."""
        automation = self._automations.get("random_message")
        if automation is None:
            self.log(
                "generate_random_message: random_message automation not active",
                level="WARNING",
            )
            return
        try:
            grid = await automation.generate_frame()
            override_ttl = bool(payload.get("override_ttl", True))
            self._push_automation_frame(
                automation_id="random_message",
                source_label="RandomMessage",
                grid=grid,
                ttl_s=None,
                expiration_s=None,
                override_ttl=override_ttl,
            )
        except Exception as exc:
            self.log(f"generate_random_message failed: {exc!r}", level="ERROR")

    async def _handle_generate_random_art(self, payload: dict) -> None:
        """Generate and push a random art frame from RandomArtAutomation."""
        automation = self._automations.get("random_art")
        if automation is None:
            self.log(
                "generate_random_art: random_art automation not active",
                level="WARNING",
            )
            return
        try:
            grid = await automation.generate_frame()
            override_ttl = bool(payload.get("override_ttl", True))
            self._push_automation_frame(
                automation_id="random_art",
                source_label="RandomArt",
                grid=grid,
                ttl_s=None,
                expiration_s=None,
                override_ttl=override_ttl,
            )
        except Exception as exc:
            self.log(f"generate_random_art failed: {exc!r}", level="ERROR")

    async def _handle_generate_ai_art(self, payload: dict) -> None:
        """Generate and push AI pixel art from AIArtGeneratorAutomation."""
        automation = self._automations.get("ai_art_generator")
        if automation is None:
            self.log(
                "generate_ai_art: ai_art_generator automation not active",
                level="WARNING",
            )
            return
        subject = str(payload.get("subject", "abstract art"))
        override_ttl = bool(payload.get("override_ttl", True))
        try:
            grid = await automation.generate_frame(subject=subject)
            self._push_automation_frame(
                automation_id="ai_art_generator",
                source_label="AIArtGenerator",
                grid=grid,
                ttl_s=None,
                expiration_s=None,
                override_ttl=override_ttl,
            )
        except Exception as exc:
            self.log(f"generate_ai_art failed: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick_wrapper(self, kwargs: dict) -> None:
        """AppDaemon timer callback — advance queue state."""
        self.create_task(self._tick())

    async def _tick(self) -> None:
        now = time.time()
        action = self._queue.tick(now)
        if action.display_frame:
            self.log(
                f"Tick promoting frame: source={action.display_frame.source!r} "
                f"reason={action.reason!r}",
                level="INFO",
            )
            await self._write_to_board(action.display_frame.characters)
            self._publish_status()
        elif action.dropped_frames:
            # Frames expired — update status even without a display change
            self._publish_status()

    # ------------------------------------------------------------------
    # Board writes
    # ------------------------------------------------------------------

    async def _write_to_board(self, characters: list[list[int]]) -> None:
        """Write a frame to the physical Vestaboard."""
        if not self._vb_ip or not self._vb_api_key:
            self.log(
                "Vestaboard IP/API key not configured — skipping board write",
                level="WARNING",
            )
            return
        try:
            async with VestaboardClient(
                ip=self._vb_ip, api_key=self._vb_api_key
            ) as client:
                ok = await client.write_frame(characters)
            self._last_write_ok = ok
            if ok:
                self.log("Board write successful", level="DEBUG")
            else:
                self.log("Board write returned non-2xx response", level="WARNING")
        except Exception as exc:
            self._last_write_ok = False
            self.log(f"Board write failed: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Automation management
    # ------------------------------------------------------------------

    def _init_automations(self) -> None:
        """Instantiate and activate automations from config."""
        _register_automations()

        # Build global config (shared AI provider etc.) from app args
        global_ai_conf = dict((self.args or {}).get("ai_provider_conf") or {})

        for automation_id, auto_cfg in self._automation_configs.items():
            if not isinstance(auto_cfg, dict):
                auto_cfg = {}

            enabled = bool(auto_cfg.get("enabled", True))

            # Merge global AI conf into automation config (automation config takes priority)
            merged_cfg = {**auto_cfg}
            if global_ai_conf and "ai_provider_conf" not in merged_cfg:
                merged_cfg["ai_provider_conf"] = global_ai_conf

            cls = _AUTOMATION_CLASSES.get(automation_id)
            if cls is None:
                self.log(
                    f"Unknown automation type: {automation_id!r} — skipping",
                    level="WARNING",
                )
                continue

            try:
                instance = cls(app=self, config=merged_cfg)
                self._automations[automation_id] = instance
                self.log(
                    f"Automation {automation_id!r} ({cls.__name__}) instantiated"
                    f" enabled={enabled}",
                    level="INFO",
                )
                if enabled:
                    self._activate_automation(automation_id)
                else:
                    self.log(f"Automation {automation_id!r} disabled in config", level="INFO")
            except Exception as exc:
                self.log(
                    f"Failed to instantiate automation {automation_id!r}: {exc!r}",
                    level="ERROR",
                )

    def _activate_automation(self, automation_id: str) -> None:
        """Register all triggers for an automation."""
        automation = self._automations.get(automation_id)
        if automation is None:
            self.log(
                f"activate_automation: {automation_id!r} not found",
                level="WARNING",
            )
            return

        self._active_automations.add(automation_id)

        triggers = automation.get_triggers()
        if not triggers:
            self.log(
                f"Automation {automation_id!r} activated (on-demand only, no triggers)",
                level="INFO",
            )
            self._publish_status()
            return

        # Schedule async registration (cancel + register + initial frame)
        self.create_task(self._register_triggers_async(automation_id, triggers))

    async def _register_triggers_async(self, automation_id: str, triggers: list) -> None:
        """Register triggers asynchronously so run_every handles resolve."""
        # Cancel any existing triggers first
        await self._cancel_triggers(automation_id)

        for idx, trigger in enumerate(triggers):
            result = await self._register_trigger_async(automation_id, idx, trigger)
            if result is not None:
                self._trigger_handles[(automation_id, idx)] = result

        self.log(
            f"Automation {automation_id!r} activated with {len(triggers)} trigger(s)",
            level="INFO",
        )

        # Fire an initial frame immediately so the user gets visual feedback
        await self._fire_automation_frame(automation_id)
        self._publish_status()

    async def _fire_automation_frame(self, automation_id: str) -> None:
        """Generate and push a frame from an automation."""
        automation = self._automations.get(automation_id)
        if automation is None:
            return
        try:
            grid = await automation.generate_frame()
            if grid:
                self._push_automation_frame(
                    automation_id=automation_id,
                    source_label=automation.name,
                    grid=grid,
                    ttl_s=automation.default_ttl_s,
                    expiration_s=automation.default_expiration_s,
                )
        except Exception as exc:
            self.log(
                f"Initial frame generation for {automation_id!r} failed: {exc!r}",
                level="WARNING",
            )

    def _deactivate_automation(self, automation_id: str) -> None:
        """Cancel all triggers for an automation."""
        if automation_id not in self._automations:
            self.log(
                f"deactivate_automation: {automation_id!r} not found",
                level="WARNING",
            )
            return
        self._active_automations.discard(automation_id)
        self.create_task(self._deactivate_async(automation_id))

    async def _register_trigger_async(
        self, automation_id: str, idx: int, trigger: dict
    ) -> Optional[tuple[Any, str]]:
        """Register a single trigger and return (handle, type) tuple."""
        trigger_type = str(trigger.get("type", ""))
        callback = trigger.get("callback")

        if trigger_type == "time_interval":
            interval_s = int(trigger.get("interval_s", 60))
            from datetime import datetime as dt
            handle = await self.run_every(callback, dt.now(), interval_s)
            self.log(
                f"Registered interval trigger for {automation_id!r}[{idx}]: "
                f"every {interval_s}s",
                level="DEBUG",
            )
            return (handle, "timer")

        elif trigger_type == "state":
            entity_id = str(trigger.get("entity_id", ""))
            if not entity_id:
                self.log(
                    f"State trigger for {automation_id!r}[{idx}] missing entity_id",
                    level="WARNING",
                )
                return None
            handle = await self.listen_state(callback, entity_id)
            self.log(
                f"Registered state trigger for {automation_id!r}[{idx}]: {entity_id!r}",
                level="DEBUG",
            )
            return (handle, "state")

        else:
            self.log(
                f"Unknown trigger type {trigger_type!r} for {automation_id!r}[{idx}]",
                level="WARNING",
            )
            return None

    async def _deactivate_async(self, automation_id: str) -> None:
        """Async deactivation — cancel triggers and publish status."""
        await self._cancel_triggers(automation_id)
        self.log(f"Automation {automation_id!r} deactivated", level="INFO")
        self._publish_status()

    async def _cancel_triggers(self, automation_id: str) -> None:
        """Cancel all registered triggers for an automation."""
        keys_to_remove = [
            key for key in self._trigger_handles if key[0] == automation_id
        ]
        for key in keys_to_remove:
            handle, handle_type = self._trigger_handles.pop(key)
            try:
                if handle_type == "state":
                    await self.cancel_listen_state(handle)
                else:
                    await self.cancel_timer(handle)
            except Exception:
                pass  # handle may already be invalid

    # ------------------------------------------------------------------
    # Frame push helper (called by automations)
    # ------------------------------------------------------------------

    def _push_automation_frame(
        self,
        automation_id: str,
        source_label: str,
        grid: list[list[int]],
        ttl_s: Optional[int],
        expiration_s: Optional[int],
        override_ttl: bool = False,
    ) -> None:
        """Push a frame generated by an automation to the queue."""
        now = time.time()
        frame = BoardFrame(
            frame_id=uuid.uuid4().hex,
            characters=grid,
            source=automation_id,
            source_label=source_label,
            ttl_s=ttl_s,
            expiration_s=expiration_s,
            override_ttl=override_ttl,
            created_at=now,
        )

        self.log(
            f"Automation {automation_id!r} pushing frame | "
            f"ttl_s={ttl_s} expiration_s={expiration_s} override_ttl={override_ttl}",
            level="INFO",
        )

        action = self._queue.push(frame, now)
        if action.display_frame:
            self.create_task(self._write_to_board(action.display_frame.characters))
        self._publish_status()

    # ------------------------------------------------------------------
    # Status publishing
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        """Publish current queue state to the status sensor entity."""
        now = time.time()
        state = self._queue.get_state(now)

        displayed = state.displayed
        displayed_frame_info = None
        if displayed is not None:
            displayed_frame_info = {
                "frame_id": displayed.frame_id,
                "source": displayed.source,
                "source_label": displayed.source_label,
                "characters": displayed.characters,
            }

        all_automations = []
        for automation_id in self._automation_configs:
            auto = self._automations.get(automation_id)
            all_automations.append({
                "id": automation_id,
                "name": auto.name if auto else automation_id.replace("_", " ").title(),
                "enabled": automation_id in self._active_automations,
            })

        attributes: dict[str, Any] = {
            "displayed_frame": displayed_frame_info,
            "displayed_source": displayed.source if displayed else None,
            "displayed_source_label": displayed.source_label if displayed else None,
            "displayed_ttl_remaining_s": state.displayed_ttl_remaining_s,
            "pending_count": len(state.pending),
            "fallback_count": len(state.fallback_stack),
            "all_automations": all_automations,
            "last_write_ok": self._last_write_ok,
            "queue_state": {
                "pending": [
                    {
                        "frame_id": f.frame_id,
                        "source": f.source,
                        "source_label": f.source_label,
                        "ttl_s": f.ttl_s,
                        "expiration_s": f.expiration_s,
                    }
                    for f in state.pending
                ],
                "fallback": [
                    {
                        "frame_id": f.frame_id,
                        "source": f.source,
                        "source_label": f.source_label,
                    }
                    for f in state.fallback_stack
                ],
            },
        }

        sensor_state = "active" if displayed is not None else "idle"

        self.set_state(
            self.SENSOR_ENTITY,
            state=sensor_state,
            attributes=attributes,
        )
