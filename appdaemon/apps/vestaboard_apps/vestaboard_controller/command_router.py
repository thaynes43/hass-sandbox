"""Command router — dispatches vestaboard_controller_command events to handlers."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3]))  # adds appdaemon/

from providers.vestaboard.character_encoding import (
    apply_border,
    detect_border_color,
    text_to_grid,
)

from vestaboard_apps._shared.frame_queue import BoardFrame, FrameQueue
from vestaboard_apps._shared.template_resolver import has_template, resolve_template

from vestaboard_apps.vestaboard_controller.automation_registry import AutomationRegistry


class CommandRouter:
    """Routes incoming commands to handler methods.

    Receives a reference to the controller app so that callbacks always
    resolve to the *current* method on the app instance.  This is critical
    for test compatibility — tests replace methods like ``fire_event``,
    ``create_task``, and ``_write_to_board`` after ``initialize()`` runs.
    """

    def __init__(
        self,
        app: Any,
        registry: AutomationRegistry,
    ) -> None:
        self._app = app
        self._registry = registry

    @property
    def _queue(self) -> FrameQueue:
        """Always resolve to the app's current queue (tests may replace it)."""
        return self._app._queue

    # Proxy accessors — always resolve to the current method on the app
    def _log(self, msg: str, level: str = "INFO") -> None:
        self._app.log(msg, level=level)

    def _fire_event(self, *args, **kwargs) -> None:
        self._app.fire_event(*args, **kwargs)

    def _get_state(self, entity_id: str) -> Any:
        return self._app.get_state(entity_id)

    def _create_task(self, coro) -> Any:
        return self._app.create_task(coro)

    def _schedule_write(self, characters, source="unknown") -> None:
        self._app._schedule_board_write(characters, source=source)

    def _write_to_board(self, characters, source="unknown"):
        return self._app._write_to_board(characters, source=source)

    def _publish_status(self) -> None:
        self._app._publish_status()

    def _set_ai_art_preview(self, value) -> None:
        self._app._ai_art_preview = value

    def _set_last_template_refresh(self, value) -> None:
        self._app._last_template_refresh = value

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle_command(self, command: str, payload: dict) -> None:
        """Route a parsed command+payload to the appropriate handler."""
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
            self._create_task(self._handle_generate_by_type(
                payload, "messages_from_library", "generate_random_message"
            ))
        elif command == "generate_random_art":
            self._create_task(self._handle_generate_by_type(
                payload, "art_from_library", "generate_random_art"
            ))
        elif command == "generate_ai_art":
            self._create_task(self._handle_generate_ai_art(payload))
        elif command == "generate_ai_art_preview":
            self._create_task(self._handle_generate_ai_art_preview(payload))
        elif command == "clear_ai_art_preview":
            self._set_ai_art_preview(None)
            self._publish_status()
            self._log("AI art preview cleared")
        elif command == "generate_ai_message":
            self._create_task(self._handle_generate_by_type(
                payload, "message_generated_by_ai", "generate_ai_message"
            ))
        elif command == "preview_automation":
            self._create_task(self._handle_preview_automation(payload))
        else:
            self._log(f"Unknown command: {command!r}", level="WARNING")

    # ------------------------------------------------------------------
    # Automation registration
    # ------------------------------------------------------------------

    def _handle_register_automation_event(self, payload: dict) -> None:
        """Handle a ``register_automation`` command from an automation app."""
        auto_id = str(payload.get("automation_id", "")).strip()
        if not auto_id:
            self._log("register_automation: missing automation_id in payload", level="WARNING")
            return

        try:
            proxy, reregistered = self._registry.register(payload)
        except ValueError:
            return

        # Fire persisted config back to the automation
        stored = self._registry.get_stored_config(auto_id)
        if stored:
            self._fire_event(
                "vb_auto_config",
                automation_id=auto_id,
                config=stored,
            )
            self._log(f"Config pushed back to automation {auto_id!r}")

        self._publish_status()

    def _handle_deregister_automation_event(self, payload: dict) -> None:
        """Handle a ``deregister_automation`` command from an automation app."""
        auto_id = str(payload.get("automation_id", "")).strip()
        removed = self._registry.deregister(auto_id)
        if removed is not None:
            self._queue.remove_source(auto_id)
            self._log(f"Automation deregistered: {auto_id!r}")
        else:
            self._log(f"deregister_automation: {auto_id!r} was not registered")
        self._publish_status()

    # ------------------------------------------------------------------
    # Automation frame push
    # ------------------------------------------------------------------

    def _handle_push_automation_frame_event(self, payload: dict) -> None:
        """Handle a ``push_automation_frame`` command from an automation app."""
        auto_id = str(payload.get("automation_id", "")).strip()
        source_label = str(payload.get("source_label", auto_id))

        raw_chars = payload.get("characters")
        if raw_chars is None:
            self._log(
                f"push_automation_frame: missing 'characters' from {auto_id!r}",
                level="WARNING",
            )
            return

        # characters arrives as a JSON string to prevent HA zero-stripping
        if isinstance(raw_chars, str):
            try:
                grid = json.loads(raw_chars)
            except Exception as exc:
                self._log(
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
        proxy = self._registry.get(auto_id)
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
            self._log(
                f"Automation {automation_id!r} produced blank frame — skipping push"
            )
            return

        # Resolve template placeholders if present
        if template and has_template(template):
            border_color = detect_border_color(grid) if grid else None
            resolved_text, resolutions = resolve_template(
                template, lambda eid: self._get_state(eid)
            )
            grid = text_to_grid(resolved_text, justify="center", align="center")
            if border_color is not None:
                apply_border(grid, border_color)
            self._log(
                f"Automation {automation_id!r} template resolved: "
                f"resolutions={resolutions} resolved_text={resolved_text!r} "
                f"border={'yes' if border_color else 'no'}"
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

        self._log(
            f"Automation {automation_id!r} pushing frame | "
            f"ttl_s={ttl_s} max_age_s={max_age_s} "
            f"override_ttl={override_ttl} should_expire={should_expire} "
            f"has_template={bool(template and has_template(template))}"
        )

        action = self._queue.push(frame, now)
        if action.display_frame:
            self._schedule_write(action.display_frame.characters, source=automation_id)
            self._set_last_template_refresh(now if template else None)
        self._publish_status()

    # ------------------------------------------------------------------
    # AI art preview
    # ------------------------------------------------------------------

    def _handle_push_ai_art_preview_result(self, payload: dict) -> None:
        """Handle the result of an AI art preview generate request."""
        raw_chars = payload.get("characters")
        subject = str(payload.get("subject", "abstract art"))

        if raw_chars is None:
            self._log("push_ai_art_preview_result: missing 'characters'", level="WARNING")
            return

        if not isinstance(raw_chars, str):
            raw_chars = json.dumps(raw_chars)

        self._set_ai_art_preview({
            "characters": raw_chars,
            "subject": subject,
            "generated_at": time.time(),
        })
        self._publish_status()
        self._log(f"AI art preview ready for subject={subject!r}")

    # ------------------------------------------------------------------
    # Next fire time
    # ------------------------------------------------------------------

    def _handle_update_next_fire_time(self, payload: dict) -> None:
        """Update a proxy's next_fire_time so the card shows upcoming automations."""
        auto_id = str(payload.get("automation_id", ""))
        next_fire = payload.get("next_fire_time")
        if next_fire is not None:
            self._registry.update_next_fire_time(auto_id, float(next_fire))
            self._publish_status()

    # ------------------------------------------------------------------
    # Push frame (user/card)
    # ------------------------------------------------------------------

    def _handle_push_frame(self, payload: dict) -> None:
        """Push a pre-built frame to the queue."""
        characters = payload.get("characters") or payload.get("frame")
        template = payload.get("template") or None
        refresh_interval_minutes = payload.get("refresh_interval_minutes")
        if refresh_interval_minutes is not None:
            refresh_interval_minutes = int(refresh_interval_minutes)

        # A template-only push (no pre-encoded grid) is valid
        if not characters and template:
            characters = [[0] * 22 for _ in range(6)]

        if not characters:
            self._log("push_frame: missing 'characters'/'frame' in payload", level="WARNING")
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
                template, lambda eid: self._get_state(eid)
            )
            characters = text_to_grid(resolved_text, justify="center", align="center")
            if border_color is not None:
                apply_border(characters, border_color)
            self._log(
                f"push_frame: template resolved for source={source!r}: "
                f"resolutions={resolutions} resolved_text={resolved_text!r} "
                f"border={'yes' if border_color else 'no'}"
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

        self._log(
            f"push_frame: source={source!r} override_ttl={override_ttl} "
            f"ttl_s={ttl_s} max_age_s={max_age_s} should_expire={should_expire} "
            f"has_template={bool(template and has_template(template))}"
        )

        action = self._queue.push(frame, now)
        if action.display_frame:
            self._create_task(self._write_to_board(action.display_frame.characters, source=source))
            self._set_last_template_refresh(now if template else None)
        self._publish_status()

    # ------------------------------------------------------------------
    # Clear board
    # ------------------------------------------------------------------

    def _handle_clear_board(self) -> None:
        """Clear all frames from the queue and blank the board."""
        self._log("clear_board command received")
        from providers.vestaboard.character_encoding import blank_grid

        self._queue.clear()
        blank = blank_grid()
        self._create_task(self._write_to_board(blank, source="clear_board"))
        self._publish_status()

    # ------------------------------------------------------------------
    # Automation config
    # ------------------------------------------------------------------

    def _handle_set_automation_config(self, automation_id: str, new_config: dict) -> None:
        """Update config for a registered automation and persist."""
        proxy = self._registry.set_config(automation_id, new_config)
        if proxy is None:
            self._log(f"set_automation_config: unknown automation {automation_id!r}", level="WARNING")
            return

        self._log(f"Updating config for automation {automation_id!r}: {new_config}")

        self._fire_event(
            "vb_auto_config",
            automation_id=automation_id,
            config=new_config,
        )
        self._log(f"Config event fired to automation {automation_id!r}")
        self._publish_status()

    # ------------------------------------------------------------------
    # Activate / deactivate
    # ------------------------------------------------------------------

    def _handle_activate_automation(self, automation_id: str) -> None:
        """Activate a registered automation."""
        proxy = self._registry.activate(automation_id)
        if proxy is None:
            self._log(f"activate_automation: {automation_id!r} not registered", level="WARNING")
            return

        self._fire_event(
            "vb_auto_enabled",
            automation_id=automation_id,
            enabled=True,
        )
        self._log(f"Automation {automation_id!r} activated")
        self._publish_status()

    def _handle_deactivate_automation(self, automation_id: str) -> None:
        """Deactivate a registered automation."""
        proxy = self._registry.deactivate(automation_id)
        if proxy is None:
            self._log(f"deactivate_automation: {automation_id!r} not registered", level="WARNING")
            return

        self._fire_event(
            "vb_auto_enabled",
            automation_id=automation_id,
            enabled=False,
        )

        # Purge frames from this automation
        action = self._queue.remove_source(automation_id)
        if action.dropped_frames:
            self._log(
                f"Deactivation purged {len(action.dropped_frames)} frame(s) "
                f"from {automation_id!r}"
            )
            tick_action = self._queue.tick(time.time())
            if tick_action.display_frame:
                self._create_task(
                    self._write_to_board(
                        tick_action.display_frame.characters,
                        source=tick_action.display_frame.source,
                    )
                )

        self._log(f"Automation {automation_id!r} deactivated")
        self._publish_status()

    # ------------------------------------------------------------------
    # Generate commands
    # ------------------------------------------------------------------

    async def _handle_generate_by_type(
        self, payload: dict, automation_type: str, command_name: str
    ) -> None:
        """Fire a generate event to an automation found by type."""
        auto_id, automation = self._registry.find(automation_type)
        if automation is None:
            self._log(
                f"{command_name}: {automation_type!r} automation not registered",
                level="WARNING",
            )
            return
        override_ttl = bool(payload.get("override_ttl", True))
        self._fire_event(
            "vb_auto_generate",
            automation_id=auto_id,
            generate_kwargs={"override_ttl": override_ttl},
            preview_only=False,
        )
        self._log(f"{command_name}: generate event fired to {auto_id!r}")

    async def _handle_generate_ai_art(self, payload: dict) -> None:
        """Fire a generate event to the AI art automation."""
        auto_id, automation = self._registry.find("art_generated_by_ai", "ai_art_generator")
        if automation is None:
            self._log("generate_ai_art: ai art automation not registered", level="WARNING")
            return
        subject = str(payload.get("subject", "abstract art"))
        override_ttl = bool(payload.get("override_ttl", True))
        self._fire_event(
            "vb_auto_generate",
            automation_id=auto_id,
            generate_kwargs={"subject": subject, "override_ttl": override_ttl},
            preview_only=False,
        )
        self._log(f"generate_ai_art: generate event fired to {auto_id!r} subject={subject!r}")

    async def _handle_generate_ai_art_preview(self, payload: dict) -> None:
        """Fire a preview generate event to the AI art automation."""
        auto_id, automation = self._registry.find("art_generated_by_ai", "ai_art_generator")
        if automation is None:
            self._log("generate_ai_art_preview: ai art automation not registered", level="WARNING")
            return
        subject = str(payload.get("subject", "abstract art"))
        self._fire_event(
            "vb_auto_generate",
            automation_id=auto_id,
            generate_kwargs={"subject": subject},
            preview_only=True,
        )
        self._log(
            f"generate_ai_art_preview: generate event fired to {auto_id!r} subject={subject!r}"
        )

    async def _handle_preview_automation(self, payload: dict) -> None:
        """Fire a generate event to a specific automation by ID for preview."""
        automation_id = str(payload.get("automation_id", ""))
        auto = self._registry.get(automation_id)
        if auto is None:
            self._log(f"preview_automation: automation {automation_id!r} not registered", level="WARNING")
            return
        self._fire_event(
            "vb_auto_generate",
            automation_id=automation_id,
            generate_kwargs={
                "override_ttl": True,
                "_preview_short_ttl": True,
            },
            preview_only=False,
        )
        self._log(f"preview_automation: generate event fired to {automation_id!r}")

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
