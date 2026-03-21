"""Vestaboard Controller App — drives the board and manages the frame queue.

Automation apps register via HA events (register_automation command).
No direct get_app() references between controller and automations.
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
from providers.vestaboard.character_encoding import (
    apply_border,
    detect_border_color,
    text_to_grid,
)

from vestaboard_apps._shared.frame_queue import BoardFrame, FrameQueue
from vestaboard_apps._shared.template_resolver import has_template, resolve_template


class RemoteAutomationProxy:
    """Stores metadata from event-based automation registration.

    Created when the controller receives a ``register_automation`` event from
    an automation app.  Provides the same interface that the controller uses
    when communicating back to automation apps (config, preview, etc.) without
    requiring a direct Python object reference.
    """

    def __init__(self, data: dict) -> None:
        self.name = data["automation_id"]
        self.automation_type = data.get("automation_type", "")
        self.display_name = data.get("display_name", "Automation")
        self.display_description = data.get("display_description", "")
        self.default_ttl_s = data.get("default_ttl_s")
        self.default_max_age_s = data.get("default_max_age_s")
        self.default_should_expire = data.get("default_should_expire", False)
        self.DEFAULT_UI_CONFIG = data.get("DEFAULT_UI_CONFIG", {})
        self._config_schema = data.get("config_schema", {})

        # preview_frame arrives as a JSON string to avoid HA zero-stripping
        raw_preview = data.get("preview_frame", "")
        if isinstance(raw_preview, str) and raw_preview:
            try:
                self._preview_frame = json.loads(raw_preview)
            except Exception:
                self._preview_frame = [[0] * 22 for _ in range(6)]
        elif isinstance(raw_preview, list):
            self._preview_frame = raw_preview
        else:
            self._preview_frame = [[0] * 22 for _ in range(6)]

        self._effective_config: dict[str, Any] = dict(self.DEFAULT_UI_CONFIG)
        self._next_fire_time: Optional[float] = None

    def get_config_schema(self) -> dict:
        return self._config_schema

    def get_preview_frame(self) -> list[list[int]]:
        return self._preview_frame

    def get_effective_config(self) -> dict[str, Any]:
        return dict(self._effective_config)

    def get_resolved_ttl_s(self) -> Optional[int]:
        ttl_min = self._effective_config.get("ttl_minutes")
        if ttl_min is not None:
            return int(float(ttl_min) * 60)
        return self.default_ttl_s

    def get_resolved_should_expire(self) -> bool:
        return bool(self._effective_config.get("should_expire", self.default_should_expire))

    def update_config(self, config: dict) -> None:
        self._effective_config.update(config)


class VestaboardControllerApp(hass.Hass):
    """AppDaemon app that drives the Vestaboard.

    Manages:
    - VestaboardClient for writing frames to the physical board.
    - FrameQueue for LIFO TTL/expiration/fallback semantics.
    - Event-based automation registration via ``register_automation`` command.
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

        # Registered automation proxies: automation_id -> RemoteAutomationProxy
        self._registered_automations: dict[str, Any] = {}

        # Last known board state for status publishing
        self._last_write_ok: Optional[bool] = None

        # AI art preview (generate-only, not pushed to board)
        self._ai_art_preview: Optional[dict[str, Any]] = None

        # Cached board state read from the physical Vestaboard
        self._external_board_frame: Optional[list[list[int]]] = None

        # Last time a template-based frame was refreshed (Unix timestamp)
        self._last_template_refresh: Optional[float] = None

        # Sleep window
        sleep_cfg = cfg.get("sleep_window") or {}
        self._sleep_enabled: bool = bool(sleep_cfg.get("enabled", True))
        self._sleep_start: str = str(sleep_cfg.get("start", "01:00:00"))
        self._sleep_end: str = str(sleep_cfg.get("end", "07:00:00"))
        self._was_sleeping: bool = False

        # Load config store synchronously so defaults are available when
        # automations register via events shortly after startup
        self._init_config_store()

        self.log(
            f"VestaboardControllerApp initializing — ip={self._vb_ip!r} "
            f"tick_interval_s={self._tick_interval_s} "
            f"sleep_enabled={self._sleep_enabled} "
            f"sleep_window={self._sleep_start}-{self._sleep_end}",
            level="INFO",
        )

        # Defer async startup so AppDaemon event loop is running
        self.run_in(self._async_startup_wrapper, 0)

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
    # Event-based automation registration
    # ------------------------------------------------------------------

    def _handle_register_automation_event(self, payload: dict) -> None:
        """Handle a ``register_automation`` command from an automation app.

        Creates (or replaces) a ``RemoteAutomationProxy`` for the automation,
        seeds config defaults, and fires the persisted config back.
        """
        auto_id = str(payload.get("automation_id", "")).strip()
        if not auto_id:
            self.log("register_automation: missing automation_id in payload", level="WARNING")
            return

        # Preserve next_fire_time from old proxy on re-registration
        old_proxy = self._registered_automations.get(auto_id)
        old_fire_time = getattr(old_proxy, "_next_fire_time", None) if old_proxy else None

        proxy = RemoteAutomationProxy(payload)
        if old_fire_time is not None:
            proxy._next_fire_time = old_fire_time
        self._registered_automations[auto_id] = proxy

        reregistered = old_proxy is not None
        self.log(
            f"Automation {'re-' if reregistered else ''}registered: {auto_id!r} "
            f"(type={proxy.automation_type!r})"
            f"{f' | preserved next_fire_time={old_fire_time}' if old_fire_time else ''}",
            level="INFO",
        )

        # Seed config store defaults
        if self._config_store:
            defaults = proxy.DEFAULT_UI_CONFIG
            if defaults and self._config_store.seed(auto_id, defaults):
                self._config_store.save()
                self.log(f"Seeded config defaults for {auto_id!r}", level="INFO")

        # Fire persisted config back to the automation
        if self._config_store:
            stored = self._config_store.get(auto_id)
            if stored:
                proxy.update_config(stored)
                self.fire_event(
                    "vb_auto_config",
                    automation_id=auto_id,
                    config=stored,
                )
                self.log(f"Config pushed back to automation {auto_id!r}", level="INFO")

        self._publish_status()

    def _handle_deregister_automation_event(self, payload: dict) -> None:
        """Handle a ``deregister_automation`` command from an automation app."""
        auto_id = str(payload.get("automation_id", "")).strip()
        removed = self._registered_automations.pop(auto_id, None)
        if removed is not None:
            self._queue.remove_source(auto_id)
            self.log(f"Automation deregistered: {auto_id!r}", level="INFO")
        else:
            self.log(
                f"deregister_automation: {auto_id!r} was not registered",
                level="DEBUG",
            )
        self._publish_status()

    def _handle_push_automation_frame_event(self, payload: dict) -> None:
        """Handle a ``push_automation_frame`` command from an automation app."""
        auto_id = str(payload.get("automation_id", "")).strip()
        source_label = str(payload.get("source_label", auto_id))

        raw_chars = payload.get("characters")
        if raw_chars is None:
            self.log(
                f"push_automation_frame: missing 'characters' from {auto_id!r}",
                level="WARNING",
            )
            return

        # characters arrives as a JSON string to prevent HA zero-stripping
        if isinstance(raw_chars, str):
            try:
                grid = json.loads(raw_chars)
            except Exception as exc:
                self.log(
                    f"push_automation_frame: failed to parse characters JSON: {exc!r}",
                    level="WARNING",
                )
                return
        else:
            grid = raw_chars

        ttl_s = payload.get("ttl_s")
        max_age_s = payload.get("max_age_s")
        override_ttl = bool(payload.get("override_ttl", False))
        should_expire = bool(payload.get("should_expire", False))

        if ttl_s is not None:
            ttl_s = int(ttl_s)
        if max_age_s is not None:
            max_age_s = int(max_age_s)

        # Update next_fire_time on the proxy if provided
        next_fire_time = payload.get("next_fire_time")
        proxy = self._registered_automations.get(auto_id)
        if proxy is not None and next_fire_time is not None:
            proxy._next_fire_time = float(next_fire_time)

        template = payload.get("template") or None
        refresh_interval_minutes = payload.get("refresh_interval_minutes")
        if refresh_interval_minutes is not None:
            refresh_interval_minutes = int(refresh_interval_minutes)

        self.push_automation_frame(
            automation_id=auto_id,
            source_label=source_label,
            grid=grid,
            ttl_s=ttl_s,
            max_age_s=max_age_s,
            override_ttl=override_ttl,
            should_expire=should_expire,
            template=template,
            refresh_interval_minutes=refresh_interval_minutes,
        )

    def _handle_push_ai_art_preview_result(self, payload: dict) -> None:
        """Handle the result of an AI art preview generate request."""
        raw_chars = payload.get("characters")
        subject = str(payload.get("subject", "abstract art"))

        if raw_chars is None:
            self.log("push_ai_art_preview_result: missing 'characters'", level="WARNING")
            return

        # Store as JSON string (already serialized by the automation)
        if not isinstance(raw_chars, str):
            raw_chars = json.dumps(raw_chars)

        self._ai_art_preview = {
            "characters": raw_chars,
            "subject": subject,
            "generated_at": time.time(),
        }
        self._publish_status()
        self.log(f"AI art preview ready for subject={subject!r}", level="INFO")

    def _handle_update_next_fire_time(self, payload: dict) -> None:
        """Update a proxy's next_fire_time so the card shows upcoming automations."""
        auto_id = str(payload.get("automation_id", ""))
        next_fire = payload.get("next_fire_time")
        proxy = self._registered_automations.get(auto_id)
        if proxy is not None and next_fire is not None:
            import time as _t
            delta = float(next_fire) - _t.time()
            self.log(
                f"Next fire time updated: {auto_id!r} → {delta / 60:.1f} min from now",
                level="INFO",
            )
            proxy._next_fire_time = float(next_fire)
            self._publish_status()

    # ------------------------------------------------------------------
    # push_automation_frame — public API (called internally)
    # ------------------------------------------------------------------

    def push_automation_frame(
        self,
        automation_id: str,
        source_label: str,
        grid: list[list[int]],
        ttl_s: Optional[int],
        max_age_s: Optional[int],
        override_ttl: bool = False,
        should_expire: bool = False,
        template: Optional[str] = None,
        refresh_interval_minutes: Optional[int] = None,
    ) -> None:
        """Push a frame generated by an automation to the queue."""
        if self._is_blank_frame(grid):
            self.log(
                f"Automation {automation_id!r} produced blank frame — skipping push",
                level="INFO",
            )
            return

        # Resolve template placeholders if present
        if template and has_template(template):
            border_color = detect_border_color(grid) if grid else None
            resolved_text, resolutions = resolve_template(
                template, lambda eid: self.get_state(eid)
            )
            grid = text_to_grid(resolved_text, justify="center", align="center")
            if border_color is not None:
                apply_border(grid, border_color)
            self.log(
                f"Automation {automation_id!r} template resolved: "
                f"resolutions={resolutions} resolved_text={resolved_text!r} "
                f"border={'yes' if border_color else 'no'}",
                level="INFO",
            )

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
            template=template,
            refresh_interval_minutes=refresh_interval_minutes,
        )

        self.log(
            f"Automation {automation_id!r} pushing frame | "
            f"ttl_s={ttl_s} max_age_s={max_age_s} "
            f"override_ttl={override_ttl} should_expire={should_expire} "
            f"has_template={bool(template and has_template(template))}",
            level="INFO",
        )

        action = self._queue.push(frame, now)
        if action.display_frame:
            self._schedule_board_write(action.display_frame.characters)
            self._last_template_refresh = now if template else None
        self._publish_status()

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: dict) -> None:
        """Route incoming commands to the appropriate handler."""
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
        # Build compact payload summary (skip large fields like characters/preview_frame)
        summary_keys = {k: v for k, v in payload.items()
                        if k not in ("characters", "preview_frame", "DEFAULT_UI_CONFIG", "config_schema")}
        self.log(
            f"Command received: {command!r} from={sender!r} payload={summary_keys}",
            level="INFO",
        )

        if command == "push_frame":
            self._handle_push_frame(payload)
        elif command == "register_automation":
            self._handle_register_automation_event(payload)
        elif command == "deregister_automation":
            self._handle_deregister_automation_event(payload)
        elif command == "push_automation_frame":
            self._handle_push_automation_frame_event(payload)
        elif command == "push_ai_art_preview_result":
            self._handle_push_ai_art_preview_result(payload)
        elif command == "update_next_fire_time":
            self._handle_update_next_fire_time(payload)
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
            self.log("AI art preview cleared", level="INFO")
        elif command == "generate_ai_message":
            self.create_task(self._handle_generate_by_type(
                payload, "message_generated_by_ai", "generate_ai_message"
            ))
        elif command == "preview_automation":
            self.create_task(self._handle_preview_automation(payload))
        else:
            self.log(f"Unknown command: {command!r}", level="WARNING")

    def _handle_push_frame(self, payload: dict) -> None:
        """Push a pre-built frame to the queue."""
        characters = payload.get("characters") or payload.get("frame")
        template = payload.get("template") or None
        refresh_interval_minutes = payload.get("refresh_interval_minutes")
        if refresh_interval_minutes is not None:
            refresh_interval_minutes = int(refresh_interval_minutes)

        # A template-only push (no pre-encoded grid) is valid — encode from template
        if not characters and template:
            characters = [[0] * 22 for _ in range(6)]  # placeholder; will be overwritten below

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

        # Resolve template placeholders if present
        if template and has_template(template):
            border_color = detect_border_color(characters) if characters else None
            resolved_text, resolutions = resolve_template(
                template, lambda eid: self.get_state(eid)
            )
            characters = text_to_grid(resolved_text, justify="center", align="center")
            if border_color is not None:
                apply_border(characters, border_color)
            self.log(
                f"push_frame: template resolved for source={source!r}: "
                f"resolutions={resolutions} resolved_text={resolved_text!r} "
                f"border={'yes' if border_color else 'no'}",
                level="INFO",
            )

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
            template=template,
            refresh_interval_minutes=refresh_interval_minutes,
        )

        self.log(
            f"push_frame: source={source!r} override_ttl={override_ttl} "
            f"ttl_s={ttl_s} max_age_s={max_age_s} should_expire={should_expire} "
            f"has_template={bool(template and has_template(template))}",
            level="INFO",
        )

        action = self._queue.push(frame, now)
        if action.display_frame:
            self.create_task(self._write_to_board(action.display_frame.characters))
            self._last_template_refresh = now if template else None
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
            self.log(
                f"Config persisted for {automation_id!r}",
                level="INFO",
            )

        # Update proxy's effective config
        if isinstance(automation, RemoteAutomationProxy):
            automation.update_config(new_config)

        # Fire config event to automation app
        self.fire_event(
            "vb_auto_config",
            automation_id=automation_id,
            config=new_config,
        )
        self.log(
            f"Config event fired to automation {automation_id!r}",
            level="INFO",
        )
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
            self.log(f"Persisted enabled=True for {automation_id!r}", level="INFO")
        # Fire enabled event to automation app
        self.fire_event(
            "vb_auto_enabled",
            automation_id=automation_id,
            enabled=True,
        )
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
            self.log(f"Persisted enabled=False for {automation_id!r}", level="INFO")
        # Fire enabled event to automation app
        self.fire_event(
            "vb_auto_enabled",
            automation_id=automation_id,
            enabled=False,
        )

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
    # Generate commands — find automation by type, fire generate event
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
        """Fire a generate event to an automation found by type."""
        auto_id, automation = self._find_automation(automation_type)
        if automation is None:
            self.log(
                f"{command_name}: {automation_type!r} automation not registered",
                level="WARNING",
            )
            return
        override_ttl = bool(payload.get("override_ttl", True))
        self.fire_event(
            "vb_auto_generate",
            automation_id=auto_id,
            generate_kwargs={"override_ttl": override_ttl},
            preview_only=False,
        )
        self.log(
            f"{command_name}: generate event fired to {auto_id!r}",
            level="INFO",
        )

    async def _handle_generate_ai_art(self, payload: dict) -> None:
        """Fire a generate event to the AI art automation."""
        auto_id, automation = self._find_automation("art_generated_by_ai", "ai_art_generator")
        if automation is None:
            self.log(
                "generate_ai_art: ai art automation not registered",
                level="WARNING",
            )
            return
        subject = str(payload.get("subject", "abstract art"))
        override_ttl = bool(payload.get("override_ttl", True))
        self.fire_event(
            "vb_auto_generate",
            automation_id=auto_id,
            generate_kwargs={"subject": subject, "override_ttl": override_ttl},
            preview_only=False,
        )
        self.log(
            f"generate_ai_art: generate event fired to {auto_id!r} subject={subject!r}",
            level="INFO",
        )

    async def _handle_generate_ai_art_preview(self, payload: dict) -> None:
        """Fire a preview generate event to the AI art automation."""
        auto_id, automation = self._find_automation("art_generated_by_ai", "ai_art_generator")
        if automation is None:
            self.log(
                "generate_ai_art_preview: ai art automation not registered",
                level="WARNING",
            )
            return
        subject = str(payload.get("subject", "abstract art"))
        self.fire_event(
            "vb_auto_generate",
            automation_id=auto_id,
            generate_kwargs={"subject": subject},
            preview_only=True,
        )
        self.log(
            f"generate_ai_art_preview: generate event fired to {auto_id!r} subject={subject!r}",
            level="INFO",
        )

    async def _handle_preview_automation(self, payload: dict) -> None:
        """Fire a generate event to a specific automation by ID for preview/testing."""
        automation_id = str(payload.get("automation_id", ""))
        auto = self._registered_automations.get(automation_id)
        if auto is None:
            self.log(
                f"preview_automation: automation {automation_id!r} not registered",
                level="WARNING",
            )
            return
        self.fire_event(
            "vb_auto_generate",
            automation_id=automation_id,
            generate_kwargs={
                "override_ttl": True,
                "_preview_short_ttl": True,
            },
            preview_only=False,
        )
        self.log(
            f"preview_automation: generate event fired to {automation_id!r}",
            level="INFO",
        )

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
            # Reset template refresh tracking when a new frame is promoted
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

        # Template refresh: if the displayed frame has a template and a refresh
        # interval, re-resolve and re-write to the board if enough time has elapsed.
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
                    await self._write_to_board(new_grid)
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
    # Board writes
    # ------------------------------------------------------------------

    def _schedule_board_write(self, characters: list[list[int]]) -> None:
        """Thread-safe board write scheduling.

        Uses run_in(0) to ensure _write_to_board runs on the controller's
        own AppDaemon thread, not the calling automation's thread.
        """
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
                from providers.vestaboard.character_encoding import decode_grid
                self.log(
                    f"Board write successful:\n{decode_grid(characters)}",
                    level="INFO",
                )
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
                        "remaining_ttl_s": f.remaining_ttl_s,
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
