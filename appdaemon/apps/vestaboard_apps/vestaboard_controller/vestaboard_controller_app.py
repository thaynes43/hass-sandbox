"""Vestaboard Controller App — drives the board and manages the frame queue.

Automation apps register dynamically via register_automation() / deregister_automation().
The controller no longer owns automation lifecycle — each automation is its own AppDaemon app.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3]))  # adds appdaemon/

import hassapi as hass

from providers.secrets import resolve_arg_secret
from providers.vestaboard.vestaboard_client import VestaboardClient

from vestaboard_apps._shared.frame_queue import BoardFrame, FrameQueue


class VestaboardControllerApp(hass.Hass):
    """AppDaemon app that drives the Vestaboard.

    Manages:
    - VestaboardClient for writing frames to the physical board.
    - FrameQueue for LIFO TTL/expiration/fallback semantics.
    - Dynamic automation registration via register_automation/deregister_automation.
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

        # Persistent automation config store (UI-editable settings)
        self._automation_config_path: str = str(
            cfg.get("automation_config_path", "")
        )
        self._config_store: Optional[Any] = None

        # Frame library path (for automations that need it)
        self._frame_library_path: str = str(
            cfg.get("frame_library_path", "")
        )

        # Build VestaboardClient
        self._client = VestaboardClient(
            ip=self._vb_ip,
            api_key=self._vb_api_key,
        )

        # Build FrameQueue
        self._queue = FrameQueue(log_fn=self.log)

        # Registered automation instances: automation_id -> automation app ref
        self._registered_automations: dict[str, Any] = {}

        # Last known board state for status publishing
        self._last_write_ok: Optional[bool] = None

        # AI art preview (generate-only, not pushed to board)
        self._ai_art_preview: Optional[dict[str, Any]] = None

        # Cached board state read from the physical Vestaboard
        self._external_board_frame: Optional[list[list[int]]] = None

        # Sleep window
        sleep_cfg = cfg.get("sleep_window") or {}
        self._sleep_enabled: bool = bool(sleep_cfg.get("enabled", True))
        self._sleep_start: str = str(sleep_cfg.get("start", "01:00:00"))
        self._sleep_end: str = str(sleep_cfg.get("end", "07:00:00"))
        self._was_sleeping: bool = False

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
        """Provision HA entities, register listeners, init config store."""
        await self._provision_entities()

        # Register command event listener
        self.listen_event(self._on_command, self.COMMAND_EVENT)
        self.log(f"Listening for {self.COMMAND_EVENT} events", level="INFO")

        # Register periodic tick
        from datetime import datetime as dt
        await self.run_every(self._tick_wrapper, dt.now(), self._tick_interval_s)
        self.log(f"Tick timer registered every {self._tick_interval_s}s", level="INFO")

        # Load persistent automation config store
        self._init_config_store()

        # Read current board state so we show something even if queue is empty
        await self._read_board_state()

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
    # Dynamic automation registration API
    # ------------------------------------------------------------------

    def register_automation(self, automation: Any) -> None:
        """Register an automation app with this controller.

        Called by automation apps from their initialize() via the mixin's
        register_with_controller().
        """
        auto_id = automation.name  # AppDaemon app key

        self._registered_automations[auto_id] = automation
        self.log(
            f"Automation registered: {auto_id!r} "
            f"(type={getattr(automation, 'automation_type', '?')})",
            level="INFO",
        )

        # Seed config store defaults
        if self._config_store:
            defaults = getattr(automation, "DEFAULT_UI_CONFIG", {})
            if defaults and self._config_store.seed(auto_id, defaults):
                self._config_store.save()
                self.log(f"Seeded config defaults for {auto_id!r}", level="INFO")

        # Push persisted config back to automation
        if self._config_store:
            stored = self._config_store.get(auto_id)
            if stored:
                automation.on_config_updated(stored)

        self._publish_status()

    def deregister_automation(self, auto_id: str) -> None:
        """Deregister an automation from this controller.

        Called by automation apps from their terminate() via the mixin's
        deregister_from_controller().
        """
        removed = self._registered_automations.pop(auto_id, None)
        if removed is not None:
            self._queue.remove_source(auto_id)
            self.log(f"Automation deregistered: {auto_id!r}", level="INFO")
        self._publish_status()

    def push_automation_frame(
        self,
        automation_id: str,
        source_label: str,
        grid: list[list[int]],
        ttl_s: Optional[int],
        max_age_s: Optional[int],
        override_ttl: bool = False,
        should_expire: bool = False,
    ) -> None:
        """Push a frame generated by an automation to the queue.

        Public API called by automation apps via the mixin's push_frame().
        """
        if self._is_blank_frame(grid):
            self.log(
                f"Automation {automation_id!r} produced blank frame — skipping push",
                level="INFO",
            )
            return
        now = time.time()
        frame = BoardFrame(
            frame_id=uuid.uuid4().hex,
            characters=grid,
            source=automation_id,
            source_label=source_label,
            ttl_s=ttl_s,
            max_age_s=max_age_s,
            override_ttl=override_ttl,
            should_expire=should_expire,
            created_at=now,
        )

        self.log(
            f"Automation {automation_id!r} pushing frame | "
            f"ttl_s={ttl_s} max_age_s={max_age_s} "
            f"override_ttl={override_ttl} should_expire={should_expire}",
            level="INFO",
        )

        action = self._queue.push(frame, now)
        if action.display_frame:
            self._schedule_board_write(action.display_frame.characters)
        self._publish_status()

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: dict) -> None:
        """Route incoming commands to the appropriate handler."""
        command = str(data.get("command", "")).strip()
        raw_payload = data.get("payload", "{}")

        self.log(f"Command received: {command!r}", level="INFO")

        try:
            if isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                payload = json.loads(raw_payload) if raw_payload else {}
        except (json.JSONDecodeError, TypeError) as exc:
            self.log(f"Failed to parse payload: {exc!r} raw={raw_payload!r}", level="WARNING")
            payload = {}

        if command == "push_frame":
            self._handle_push_frame(payload)
        elif command == "activate_automation":
            automation_id = str(payload.get("automation_id", ""))
            self._handle_activate_automation(automation_id)
        elif command == "deactivate_automation":
            automation_id = str(payload.get("automation_id", ""))
            self._handle_deactivate_automation(automation_id)
        elif command == "clear_board":
            self._handle_clear_board()
        elif command == "set_automation_config":
            automation_id = str(payload.get("automation_id", ""))
            new_config = payload.get("config")
            if not isinstance(new_config, dict):
                new_config = {
                    k: v for k, v in payload.items()
                    if k != "automation_id" and v is not None
                }
            self._handle_set_automation_config(automation_id, new_config)
        elif command == "generate_random_message":
            self.create_task(self._handle_generate_by_type(
                payload, "messages_from_library", "generate_random_message"
            ))
        elif command == "generate_random_art":
            self.create_task(self._handle_generate_by_type(
                payload, "art_from_library", "generate_random_art"
            ))
        elif command == "generate_ai_art":
            self.create_task(self._handle_generate_ai_art(payload))
        elif command == "generate_ai_art_preview":
            self.create_task(self._handle_generate_ai_art_preview(payload))
        elif command == "clear_ai_art_preview":
            self._ai_art_preview = None
            self._publish_status()
            self.log("AI art preview cleared")
        elif command == "generate_ai_message":
            self.create_task(self._handle_generate_by_type(
                payload, "message_generated_by_ai", "generate_ai_message"
            ))
        else:
            self.log(f"Unknown command: {command!r}", level="WARNING")

    def _handle_push_frame(self, payload: dict) -> None:
        """Push a pre-built frame to the queue."""
        characters = payload.get("characters") or payload.get("frame")
        if not characters:
            self.log("push_frame: missing 'characters'/'frame' in payload", level="WARNING")
            return

        source = str(payload.get("source", "user"))
        source_label = str(payload.get("source_label", "User"))

        ttl_s = payload.get("ttl_s")
        ttl_minutes = payload.get("ttl_minutes")
        if ttl_s is None and ttl_minutes is not None:
            ttl_s = int(ttl_minutes) * 60

        max_age_s = payload.get("max_age_s")

        respect_ttl = payload.get("respect_ttl", False)
        override_ttl = not respect_ttl if "respect_ttl" in payload else bool(payload.get("override_ttl", True))
        should_expire = bool(payload.get("should_expire", False))

        if ttl_s is not None:
            ttl_s = int(ttl_s)
        if max_age_s is not None:
            max_age_s = int(max_age_s)

        now = time.time()
        frame = BoardFrame(
            frame_id=uuid.uuid4().hex,
            characters=characters,
            source=source,
            source_label=source_label,
            ttl_s=ttl_s,
            max_age_s=max_age_s,
            override_ttl=override_ttl,
            should_expire=should_expire,
            created_at=now,
        )

        self.log(
            f"push_frame: source={source!r} override_ttl={override_ttl} "
            f"ttl_s={ttl_s} max_age_s={max_age_s} should_expire={should_expire}",
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
        """Update config for a registered automation and persist to config store."""
        automation = self._registered_automations.get(automation_id)
        if automation is None:
            self.log(
                f"set_automation_config: unknown automation {automation_id!r}",
                level="WARNING",
            )
            return
        self.log(
            f"Updating config for automation {automation_id!r}: {new_config}",
            level="INFO",
        )

        # Persist to config store
        if self._config_store is not None:
            self._config_store.update(automation_id, new_config)

        # Push config to automation
        automation.on_config_updated(new_config)
        self._publish_status()

    def _handle_activate_automation(self, automation_id: str) -> None:
        """Activate a registered automation."""
        automation = self._registered_automations.get(automation_id)
        if automation is None:
            self.log(
                f"activate_automation: {automation_id!r} not registered",
                level="WARNING",
            )
            return
        if self._config_store is not None:
            self._config_store.update(automation_id, {"enabled": True})
        automation.set_enabled(True)
        self.log(f"Automation {automation_id!r} activated", level="INFO")
        self._publish_status()

    def _handle_deactivate_automation(self, automation_id: str) -> None:
        """Deactivate a registered automation."""
        automation = self._registered_automations.get(automation_id)
        if automation is None:
            self.log(
                f"deactivate_automation: {automation_id!r} not registered",
                level="WARNING",
            )
            return
        if self._config_store is not None:
            self._config_store.update(automation_id, {"enabled": False})
        automation.set_enabled(False)

        # Purge frames from this automation
        action = self._queue.remove_source(automation_id)
        if action.dropped_frames:
            self.log(
                f"Deactivation purged {len(action.dropped_frames)} frame(s) "
                f"from {automation_id!r}",
                level="INFO",
            )
            tick_action = self._queue.tick(time.time())
            if tick_action.display_frame:
                self.create_task(self._write_to_board(tick_action.display_frame.characters))

        self.log(f"Automation {automation_id!r} deactivated", level="INFO")
        self._publish_status()

    # ------------------------------------------------------------------
    # Generate commands — find automation by type
    # ------------------------------------------------------------------

    def _find_automation_by_type(self, automation_type: str) -> tuple[Optional[str], Optional[Any]]:
        """Find a registered automation by its automation_type."""
        for auto_id, auto in self._registered_automations.items():
            if getattr(auto, "automation_type", "") == automation_type:
                return auto_id, auto
        return None, None

    def _find_automation(self, *candidate_ids: str) -> tuple[Optional[str], Optional[Any]]:
        """Find first matching automation by trying multiple IDs."""
        for aid in candidate_ids:
            auto = self._registered_automations.get(aid)
            if auto is not None:
                return aid, auto
        # Also try by automation_type
        for aid in candidate_ids:
            found_id, found = self._find_automation_by_type(aid)
            if found is not None:
                return found_id, found
        return None, None

    async def _handle_generate_by_type(
        self, payload: dict, automation_type: str, command_name: str
    ) -> None:
        """Generate and push a frame from an automation found by type."""
        auto_id, automation = self._find_automation(automation_type)
        if automation is None:
            self.log(
                f"{command_name}: {automation_type!r} automation not registered",
                level="WARNING",
            )
            return
        try:
            grid = await automation.generate_frame()
            override_ttl = bool(payload.get("override_ttl", True))
            ttl_s = automation.get_resolved_ttl_s() if hasattr(automation, 'get_resolved_ttl_s') else automation.default_ttl_s
            should_expire = automation.get_resolved_should_expire() if hasattr(automation, 'get_resolved_should_expire') else automation.default_should_expire
            self.push_automation_frame(
                automation_id=auto_id,
                source_label=automation.display_name,
                grid=grid,
                ttl_s=ttl_s,
                max_age_s=None,
                override_ttl=override_ttl,
                should_expire=should_expire,
            )
        except Exception as exc:
            self.log(f"{command_name} failed: {exc!r}", level="ERROR")

    async def _handle_generate_ai_art(self, payload: dict) -> None:
        """Generate and push AI pixel art."""
        auto_id, automation = self._find_automation("art_generated_by_ai", "ai_art_generator")
        if automation is None:
            self.log(
                "generate_ai_art: ai art automation not registered",
                level="WARNING",
            )
            return
        subject = str(payload.get("subject", "abstract art"))
        override_ttl = bool(payload.get("override_ttl", True))
        try:
            grid = await automation.generate_frame(subject=subject)
            ttl_s = automation.get_resolved_ttl_s() if hasattr(automation, 'get_resolved_ttl_s') else automation.default_ttl_s
            should_expire = automation.get_resolved_should_expire() if hasattr(automation, 'get_resolved_should_expire') else automation.default_should_expire
            self.push_automation_frame(
                automation_id=auto_id,
                source_label=automation.display_name,
                grid=grid,
                ttl_s=ttl_s,
                max_age_s=None,
                override_ttl=override_ttl,
                should_expire=should_expire,
            )
        except Exception as exc:
            self.log(f"generate_ai_art failed: {exc!r}", level="ERROR")

    async def _handle_generate_ai_art_preview(self, payload: dict) -> None:
        """Generate AI pixel art and store as preview — does NOT push to board."""
        auto_id, automation = self._find_automation("art_generated_by_ai", "ai_art_generator")
        if automation is None:
            self.log(
                "generate_ai_art_preview: ai art automation not registered",
                level="WARNING",
            )
            return
        subject = str(payload.get("subject", "abstract art"))
        try:
            grid = await automation.generate_frame(subject=subject)
            self._ai_art_preview = {
                "characters": json.dumps(grid),
                "subject": subject,
                "generated_at": time.time(),
            }
            self._publish_status()
            self.log(f"AI art preview ready for subject={subject!r}")
        except Exception as exc:
            self.log(f"generate_ai_art_preview failed: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick_wrapper(self, kwargs: dict) -> None:
        """AppDaemon timer callback — advance queue state."""
        self.create_task(self._tick())

    async def _tick(self) -> None:
        now = time.time()
        sleeping = self._is_sleeping()
        sleep_changed = sleeping != self._was_sleeping

        if self._was_sleeping and not sleeping:
            self.log("Sleep window ended — waking up", level="INFO")
            state = self._queue.get_state(now)
            if state.displayed is not None:
                self.log(
                    f"Reconciling board on wake: source={state.displayed.source!r}",
                    level="INFO",
                )
                await self._write_to_board(state.displayed.characters)
        elif not self._was_sleeping and sleeping:
            self.log("Sleep window started — suppressing board writes", level="INFO")

        self._was_sleeping = sleeping

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
            await self._read_board_state()
            self._publish_status()
        elif self._queue.get_state(now).displayed is None and self._external_board_frame is None:
            await self._read_board_state()
            if self._external_board_frame is not None:
                self._publish_status()

        if sleep_changed:
            self._publish_status()

    # ------------------------------------------------------------------
    # Board writes
    # ------------------------------------------------------------------

    def _schedule_board_write(self, characters: list[list[int]]) -> None:
        """Thread-safe board write scheduling.

        Uses run_in(0) to ensure _write_to_board runs on the controller's
        own AppDaemon thread, not the calling automation's thread. This is
        necessary because push_automation_frame() is called cross-app.
        """
        # Capture characters in closure for the run_in callback
        self.run_in(self._board_write_callback, 0, characters=characters)

    def _board_write_callback(self, kwargs: dict) -> None:
        """run_in callback that triggers the async board write."""
        characters = kwargs.get("characters")
        if characters:
            self.create_task(self._write_to_board(characters))

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int, int, int]:
        """Parse 'HH:MM:SS' or 'HH:MM' into (hour, minute, second)."""
        parts = str(time_str).split(":")
        h = int(parts[0]) if len(parts) > 0 else 0
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return h, m, s

    def _is_sleeping(self) -> bool:
        """Check if the current time is within the sleep window."""
        if not self._sleep_enabled:
            return False
        from datetime import datetime as dt, time as dtime
        now = dt.now().time()
        sh, sm, ss = self._parse_time(self._sleep_start)
        eh, em, es = self._parse_time(self._sleep_end)
        start = dtime(sh, sm, ss)
        end = dtime(eh, em, es)
        if start < end:
            return start <= now < end
        else:
            return now >= start or now < end

    async def _write_to_board(self, characters: list[list[int]]) -> None:
        """Write a frame to the physical Vestaboard."""
        if self._is_sleeping():
            self.log("Board write suppressed — sleep window active", level="DEBUG")
            return
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

    async def _read_board_state(self) -> None:
        """Read the current frame from the physical Vestaboard and cache it."""
        if not self._vb_ip or not self._vb_api_key:
            self.log("No Vestaboard IP/API key — skipping board read", level="DEBUG")
            return
        try:
            async with VestaboardClient(
                ip=self._vb_ip, api_key=self._vb_api_key
            ) as client:
                grid = await client.read_current()
            if isinstance(grid, dict):
                self.log(f"Board read returned dict with keys: {list(grid.keys())}", level="DEBUG")
                grid = grid.get("message") or grid.get("currentMessage") or grid.get("characters")
            if grid and isinstance(grid, list) and len(grid) == 6:
                self._external_board_frame = grid
                self.log("Read current board state from Vestaboard", level="INFO")
            else:
                self.log(f"Board read returned unexpected data: {type(grid)}", level="WARNING")
        except Exception as exc:
            self.log(f"Failed to read board state: {exc!r}", level="WARNING")

    # ------------------------------------------------------------------
    # Automation config store
    # ------------------------------------------------------------------

    def _init_config_store(self) -> None:
        """Load the persistent automation config store."""
        if not self._automation_config_path:
            self.log(
                "automation_config_path not configured — config store disabled",
                level="DEBUG",
            )
            return

        from vestaboard_apps._shared.config_store import AutomationConfigStore

        self._config_store = AutomationConfigStore(self._automation_config_path)
        self._config_store.load()

        self.log(
            f"AutomationConfigStore loaded from {self._automation_config_path!r}",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_blank_frame(grid: list[list[int]]) -> bool:
        """Return True if grid is empty or contains only zeros."""
        if not grid:
            return True
        return all(
            all(cell == 0 for cell in row) if row else True
            for row in grid
        )

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
                "characters": json.dumps(displayed.characters),
            }
        elif self._external_board_frame is not None:
            displayed_frame_info = {
                "frame_id": "external",
                "source": "external",
                "source_label": "External / Other App",
                "characters": json.dumps(self._external_board_frame),
            }

        all_automations = []
        for automation_id, auto in self._registered_automations.items():
            entry: dict[str, Any] = {
                "id": automation_id,
                "name": getattr(auto, "display_name", automation_id.replace("_", " ").title()),
                "description": getattr(auto, "display_description", "A Vestaboard automation."),
                "enabled": bool(self._config_store.get(automation_id).get("enabled", True)) if self._config_store else True,
            }

            # Include next scheduled fire time
            next_fire = getattr(auto, "_next_fire_time", None)
            if next_fire is not None:
                entry["next_fire_time"] = next_fire

            # Include preview frame
            if hasattr(auto, "get_preview_frame"):
                try:
                    entry["preview_frame"] = json.dumps(auto.get_preview_frame())
                except Exception as exc:
                    self.log(
                        f"get_preview_frame failed for {automation_id!r}: {exc!r}",
                        level="WARNING",
                    )

            # Include config schema and current config
            if hasattr(auto, "get_config_schema"):
                try:
                    entry["config_schema"] = auto.get_config_schema()
                except Exception as exc:
                    self.log(
                        f"get_config_schema failed for {automation_id!r}: {exc!r}",
                        level="WARNING",
                    )

            if hasattr(auto, "get_effective_config"):
                try:
                    entry["config"] = {
                        k: v for k, v in auto.get_effective_config().items()
                        if not k.startswith("_") and k != "type"
                    }
                except Exception:
                    entry["config"] = {}
            else:
                entry["config"] = {}

            all_automations.append(entry)

        attributes: dict[str, Any] = {
            "displayed_frame": displayed_frame_info,
            "displayed_source": displayed.source if displayed else ("external" if displayed_frame_info else None),
            "displayed_source_label": displayed.source_label if displayed else ("External / Other App" if displayed_frame_info else None),
            "displayed_ttl_remaining_s": state.displayed_ttl_remaining_s,
            "pending_count": len(state.pending),
            "fallback_count": len(state.fallback_stack),
            "all_automations": all_automations,
            "last_write_ok": self._last_write_ok,
            "sleeping": self._is_sleeping(),
            "sleep_end": self._sleep_end if self._sleep_enabled else None,
            "ai_art_preview": self._ai_art_preview,
            "queue_state": {
                "pending": [
                    {
                        "frame_id": f.frame_id,
                        "source": f.source,
                        "source_label": f.source_label,
                        "ttl_s": f.ttl_s,
                        "max_age_s": f.max_age_s,
                        "expires_at": (
                            datetime.fromtimestamp(
                                f.created_at + f.max_age_s, tz=timezone.utc
                            ).isoformat()
                            if f.max_age_s is not None
                            else None
                        ),
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
