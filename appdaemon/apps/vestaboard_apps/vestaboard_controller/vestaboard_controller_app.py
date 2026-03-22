"""Vestaboard Controller App — drives the board and manages the frame queue.

Automation apps register via HA events (register_automation command).
No direct get_app() references between controller and automations.

This module is the orchestration layer.  Business logic lives in:
- automation_registry.py — proxy management and config store
- command_router.py — command dispatch and handlers
- board_io.py — sleep window and write tracking
- status_publisher.py — sensor attribute construction
"""

from __future__ import annotations

import json
import time
from datetime import datetime  # noqa: F401 — tests patch this at this module path
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3]))  # adds appdaemon/

import hassapi as hass

from providers.secrets import resolve_arg_secret
from providers.vestaboard.vestaboard_client import VestaboardClient
from providers.vestaboard.character_encoding import (
    apply_border,
    detect_border_color,
    text_to_grid,
)

from vestaboard_apps._shared.frame_queue import FrameQueue
from vestaboard_apps._shared.template_resolver import has_template, resolve_template

# Re-export for backward compatibility (tests import from this module)
from vestaboard_apps.vestaboard_controller.automation_registry import (
    RemoteAutomationProxy,
    AutomationRegistry,
)
from vestaboard_apps.vestaboard_controller.board_io import BoardIO
from vestaboard_apps.vestaboard_controller.command_router import CommandRouter
from vestaboard_apps.vestaboard_controller.status_publisher import StatusPublisher


class VestaboardControllerApp(hass.Hass):
    """AppDaemon app that drives the Vestaboard.

    Orchestrates:
    - FrameQueue for FIFO TTL/expiration/fallback semantics
    - AutomationRegistry for event-based automation management
    - CommandRouter for card/automation-driven requests
    - BoardIO for sleep-window and write tracking
    - StatusPublisher for sensor state construction
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
        self.__config_store: Optional[Any] = None

        # Frame library path (for automations that need it)
        self._frame_library_path: str = str(
            cfg.get("frame_library_path", "")
        )

        # Sleep window config
        sleep_cfg = cfg.get("sleep_window") or {}
        sleep_enabled: bool = bool(sleep_cfg.get("enabled", True))
        sleep_start: str = str(sleep_cfg.get("start", "01:00:00"))
        sleep_end: str = str(sleep_cfg.get("end", "07:00:00"))

        # Build components
        self._board_io = BoardIO(
            sleep_enabled=sleep_enabled,
            sleep_start=sleep_start,
            sleep_end=sleep_end,
            log_fn=self.log,
        )

        self._queue = FrameQueue(log_fn=self.log)

        # Load config store synchronously so defaults are available when
        # automations register via events shortly after startup
        self._init_config_store()

        self._registry = AutomationRegistry(
            config_store=self.__config_store,
            log_fn=self.log,
        )

        self._router = CommandRouter(
            app=self,
            registry=self._registry,
        )

        self._publisher = StatusPublisher()

        # Instance state
        self._ai_art_preview: Optional[dict[str, Any]] = None
        self._external_board_frame: Optional[list[list[int]]] = None
        self._last_template_refresh: Optional[float] = None
        self._was_sleeping: bool = False

        self.log(
            f"VestaboardControllerApp initializing — ip={self._vb_ip!r} "
            f"tick_interval_s={self._tick_interval_s} "
            f"sleep_enabled={sleep_enabled} "
            f"sleep_window={sleep_start}-{sleep_end}",
            level="INFO",
        )

        # Defer async startup so AppDaemon event loop is running
        self.run_in(self._async_startup_wrapper, 0)

    # ------------------------------------------------------------------
    # Properties for test compatibility
    # ------------------------------------------------------------------

    @property
    def _registered_automations(self) -> dict:
        """Expose registry internals for backward compatibility with tests."""
        return self._registry._automations

    @_registered_automations.setter
    def _registered_automations(self, value: dict) -> None:
        self._registry._automations = value

    @property
    def _config_store(self) -> Any:
        return self.__config_store

    @_config_store.setter
    def _config_store(self, value: Any) -> None:
        self.__config_store = value
        if hasattr(self, "_registry"):
            self._registry._config_store = value

    @property
    def _last_write_ok(self) -> Optional[bool]:
        return self._board_io.last_write_ok

    @_last_write_ok.setter
    def _last_write_ok(self, value: Optional[bool]) -> None:
        self._board_io.last_write_ok = value

    @property
    def _sleep_enabled(self) -> bool:
        return self._board_io._sleep_enabled

    @_sleep_enabled.setter
    def _sleep_enabled(self, value: bool) -> None:
        self._board_io._sleep_enabled = value

    @property
    def _sleep_start(self) -> str:
        return self._board_io._sleep_start

    @_sleep_start.setter
    def _sleep_start(self, value: str) -> None:
        self._board_io._sleep_start = value

    @property
    def _sleep_end(self) -> str:
        return self._board_io._sleep_end

    @_sleep_end.setter
    def _sleep_end(self, value: str) -> None:
        self._board_io._sleep_end = value

    def _is_sleeping(self) -> bool:
        return self._board_io.is_sleeping()

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int, int, int]:
        return BoardIO.parse_time(time_str)

    def _set_ai_art_preview(self, value: Optional[dict]) -> None:
        self._ai_art_preview = value

    def _set_last_template_refresh(self, value: Optional[float]) -> None:
        self._last_template_refresh = value

    # ------------------------------------------------------------------
    # Async startup
    # ------------------------------------------------------------------

    def _async_startup_wrapper(self, kwargs: dict) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Provision HA entities, register listeners, fire ready event."""
        await self._provision_entities()

        # Register command event listener
        self.listen_event(self._on_command, self.COMMAND_EVENT)
        self.log(f"Listening for {self.COMMAND_EVENT} events", level="INFO")

        # Register periodic tick
        from datetime import datetime as dt
        await self.run_every(self._tick_wrapper, dt.now(), self._tick_interval_s)
        self.log(f"Tick timer registered every {self._tick_interval_s}s", level="INFO")

        # Read current board state so we show something even if queue is empty
        await self._read_board_state()

        # Publish initial status
        self._publish_status()

        # Announce readiness so automations can re-register if needed
        self.fire_event("vestaboard_controller_ready")
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
    # Command handling (thin delegation)
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: dict) -> None:
        """Route incoming commands to the command router."""
        command = str(data.get("command", "")).strip()
        raw_payload = data.get("payload", "{}")

        try:
            if isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                payload = json.loads(raw_payload) if raw_payload else {}
        except (json.JSONDecodeError, TypeError) as exc:
            self.log(f"Failed to parse payload: {exc!r} raw={raw_payload!r}", level="WARNING")
            payload = {}

        # Log command with sender and payload summary
        sender = payload.get("automation_id") or payload.get("source") or "unknown"
        summary_keys = {k: v for k, v in payload.items()
                        if k not in ("characters", "preview_frame", "DEFAULT_UI_CONFIG", "config_schema")}
        self.log(
            f"Command received: {command!r} from={sender!r} payload={summary_keys}",
            level="INFO",
        )

        self._router.handle_command(command, payload)

    # ------------------------------------------------------------------
    # Delegation methods (test compatibility)
    # ------------------------------------------------------------------

    def push_automation_frame(self, *args, **kwargs) -> None:
        self._router.push_automation_frame(*args, **kwargs)

    def _handle_register_automation_event(self, payload: dict) -> None:
        self._router._handle_register_automation_event(payload)

    def _handle_deregister_automation_event(self, payload: dict) -> None:
        self._router._handle_deregister_automation_event(payload)

    def _handle_push_automation_frame_event(self, payload: dict) -> None:
        self._router._handle_push_automation_frame_event(payload)

    def _handle_push_ai_art_preview_result(self, payload: dict) -> None:
        self._router._handle_push_ai_art_preview_result(payload)

    def _handle_update_next_fire_time(self, payload: dict) -> None:
        self._router._handle_update_next_fire_time(payload)

    def _handle_push_frame(self, payload: dict) -> None:
        self._router._handle_push_frame(payload)

    def _handle_clear_board(self) -> None:
        self._router._handle_clear_board()

    def _handle_set_automation_config(self, automation_id: str, new_config: dict) -> None:
        self._router._handle_set_automation_config(automation_id, new_config)

    def _handle_activate_automation(self, automation_id: str) -> None:
        self._router._handle_activate_automation(automation_id)

    def _handle_deactivate_automation(self, automation_id: str) -> None:
        self._router._handle_deactivate_automation(automation_id)

    def _find_automation_by_type(self, automation_type: str):
        return self._registry.find_by_type(automation_type)

    def _find_automation(self, *candidate_ids: str):
        return self._registry.find(*candidate_ids)

    async def _handle_generate_by_type(self, payload, automation_type, command_name):
        return await self._router._handle_generate_by_type(payload, automation_type, command_name)

    async def _handle_generate_ai_art(self, payload):
        return await self._router._handle_generate_ai_art(payload)

    async def _handle_generate_ai_art_preview(self, payload):
        return await self._router._handle_generate_ai_art_preview(payload)

    async def _handle_preview_automation(self, payload):
        return await self._router._handle_preview_automation(payload)

    @staticmethod
    def _is_blank_frame(grid: list[list[int]]) -> bool:
        return CommandRouter._is_blank_frame(grid)

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
                await self._write_to_board(state.displayed.characters, source=state.displayed.source)
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
            await self._write_to_board(action.display_frame.characters, source=action.display_frame.source)
            displayed = action.display_frame
            self._last_template_refresh = now if (displayed.template and has_template(displayed.template)) else None
            self._publish_status()
        elif action.dropped_frames:
            await self._read_board_state()
            self._publish_status()
        elif self._queue.get_state(now).displayed is None and self._external_board_frame is None:
            await self._read_board_state()
            if self._external_board_frame is not None:
                self._publish_status()

        # Template refresh
        displayed_frame = self._queue._displayed
        if (
            displayed_frame is not None
            and displayed_frame.template
            and has_template(displayed_frame.template)
            and displayed_frame.refresh_interval_minutes is not None
            and not sleeping
        ):
            refresh_interval_s = displayed_frame.refresh_interval_minutes * 60
            last_refresh = self._last_template_refresh if self._last_template_refresh is not None else 0.0
            if now - last_refresh >= refresh_interval_s:
                border_color = detect_border_color(displayed_frame.characters) if displayed_frame.characters else None
                resolved_text, resolutions = resolve_template(
                    displayed_frame.template, lambda eid: self.get_state(eid)
                )
                new_grid = text_to_grid(resolved_text, justify="center", align="center")
                if border_color is not None:
                    apply_border(new_grid, border_color)
                self.log(
                    f"Template refresh for source={displayed_frame.source!r}: "
                    f"resolutions={resolutions} resolved_text={resolved_text!r}",
                    level="INFO",
                )
                if new_grid != displayed_frame.characters:
                    displayed_frame.characters = new_grid
                    await self._write_to_board(new_grid, source=displayed_frame.source)
                    self._publish_status()
                else:
                    self.log(
                        f"Template refresh for source={displayed_frame.source!r}: "
                        f"grid unchanged — skipping board write",
                        level="DEBUG",
                    )
                self._last_template_refresh = now

        if sleep_changed:
            self._publish_status()

    # ------------------------------------------------------------------
    # Board writes (kept here for VestaboardClient patch compatibility)
    # ------------------------------------------------------------------

    def _schedule_board_write(self, characters: list[list[int]], source: str = "unknown") -> None:
        """Thread-safe board write scheduling via run_in(0)."""
        self.run_in(self._board_write_callback, 0, characters=characters, source=source)

    def _board_write_callback(self, kwargs: dict) -> None:
        characters = kwargs.get("characters")
        source = kwargs.get("source", "unknown")
        if characters:
            self.create_task(self._write_to_board(characters, source=source))

    async def _write_to_board(self, characters: list[list[int]], source: str = "unknown") -> None:
        """Write a frame to the physical Vestaboard."""
        if self._board_io.is_sleeping():
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
            self._board_io.last_write_ok = ok
            if ok:
                from providers.vestaboard.character_encoding import decode_grid
                self.log(
                    f"Board write successful (source={source!r}):\n{decode_grid(characters)}",
                    level="INFO",
                )
            else:
                self.log(f"Board write returned non-2xx response (source={source!r})", level="WARNING")
        except Exception as exc:
            self._board_io.last_write_ok = False
            self.log(f"Board write failed (source={source!r}): {exc!r}", level="ERROR")

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

        self.__config_store = AutomationConfigStore(self._automation_config_path)
        self.__config_store.load()

        self.log(
            f"AutomationConfigStore loaded from {self._automation_config_path!r}",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Status publishing
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        """Publish current queue state to the status sensor entity."""
        now = time.time()
        state = self._queue.get_state(now)

        sensor_state, attributes = StatusPublisher.build_attributes(
            queue_state=state,
            registry=self._registry,
            board_io=self._board_io,
            ai_art_preview=self._ai_art_preview,
            external_board_frame=self._external_board_frame,
            log_fn=self.log,
        )

        self.set_state(
            self.SENSOR_ENTITY,
            state=sensor_state,
            attributes=attributes,
        )
