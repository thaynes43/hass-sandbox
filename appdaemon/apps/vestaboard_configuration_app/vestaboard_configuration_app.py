"""Vestaboard Configuration App.

Configuration bridge between the Lovelace card and the Vestaboard controller app.
Manages the frame library (CRUD) and forwards push/automation commands to the
controller via events.

Architecture:
  Lovelace card
    → relay script (vestaboard_configuration_relay)
    → vestaboard_configuration_command event
    → this app
    → (for push/automation commands) vestaboard_controller_command event
    → vestaboard_controller_app
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[2]))  # adds appdaemon/

import hassapi as hass

from providers.secrets import resolve_arg_secret
from vestaboard_configuration_app.frame_library import FrameLibrary, LibraryFrame


class VestaboardConfigurationApp(hass.Hass):
    """Configuration bridge between the Lovelace card and the controller app."""

    SENSOR_ENTITY = "sensor.vestaboard_configuration_status"
    RELAY_SCRIPT_ID = "vestaboard_configuration_relay"
    COMMAND_EVENT = "vestaboard_configuration_command"
    CONTROLLER_STATUS_ENTITY = "sensor.vestaboard_controller_status"
    CONTROLLER_COMMAND_EVENT = "vestaboard_controller_command"

    def initialize(self) -> None:
        cfg = self.args or {}

        # HA credentials for provisioning
        self._ha_url: str = str(resolve_arg_secret(cfg, "ha_url", default=""))
        self._ha_token_env: str = str(cfg.get("ha_token_env", ""))

        # Frame library storage path
        self._frame_library_path: str = str(
            cfg.get("frame_library_path", "/media/vestaboard/frame-library.json")
        )

        # Creators list — mutable at runtime (add_creator command)
        default_creators = ["Mom", "Tom", "Jackson", "Penelope", "Anonymous"]
        self._creators: list[str] = list(cfg.get("creators") or default_creators)

        # Cached controller status attributes for merging into our published status
        self._controller_attrs: dict[str, Any] = {}

        # Build and load the frame library
        self._frame_library = FrameLibrary(
            storage_path=self._frame_library_path,
            log_fn=self.log,
        )
        self._frame_library.load()

        # Seed Hello World if library is empty
        self._seed_library()

        # Kick off async provisioning
        self.run_in(self._async_startup_wrapper, 0)

        self.log(
            "VestaboardConfigurationApp initialized — library_path=%r creators=%s",
            self._frame_library_path,
            self._creators,
        )

    # ------------------------------------------------------------------
    # Async startup — provisioning
    # ------------------------------------------------------------------

    def _async_startup_wrapper(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Provision HA entities and register event listeners."""
        await self._provision_entities()

        # Listen for relay commands from the card
        self.listen_event(self._on_command, self.COMMAND_EVENT)
        self.log("Listening for %s events", self.COMMAND_EVENT)

        # Mirror controller status into our sensor
        self.listen_state(self._on_controller_status, self.CONTROLLER_STATUS_ENTITY, attribute="all")
        self.log("Listening for controller status changes on %s", self.CONTROLLER_STATUS_ENTITY)

        # Immediate sync attempt — controller may already have state from a prior run
        try:
            ctrl_state = self.get_state(self.CONTROLLER_STATUS_ENTITY, attribute="all")
            # In async context, get_state may return a coroutine
            if hasattr(ctrl_state, "__await__"):
                ctrl_state = await ctrl_state
            ctrl_state = ctrl_state or {}
            new_attrs = dict(ctrl_state.get("attributes", {}))
            if new_attrs:
                self._controller_attrs = new_attrs
                self.log("Immediate controller sync — loaded controller attrs", level="DEBUG")
        except Exception as exc:
            self.log("Immediate controller sync failed: %r", exc, level="WARNING")

        # Publish initial status so the card has data on first load
        self._publish_status()
        self.log("VestaboardConfigurationApp async startup complete")

        # Schedule a delayed read of controller status as a safety net
        # (in case controller hasn't published its state yet at this moment)
        self.run_in(self._deferred_controller_sync, 5)

    async def _provision_entities(self) -> None:
        """Provision relay script and helpers via HAProvisioner."""
        ha_url = self._ha_url
        ha_token_env = self._ha_token_env
        if not ha_url or not ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        try:
            from providers.ha_provisioner import HAProvisioner

            prov = HAProvisioner(ha_url, ha_token_env)

            # --- Relay script ---
            try:
                created = await prov.ensure_script(
                    self.RELAY_SCRIPT_ID,
                    {
                        "alias": "Vestaboard Configuration Relay",
                        "description": "Relays Vestaboard card commands to AppDaemon",
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
                    "Relay script.%s %s",
                    self.RELAY_SCRIPT_ID,
                    msg,
                    level="INFO" if created else "DEBUG",
                )
            except Exception as exc:
                self.log(
                    "Failed to provision relay script: %r",
                    exc,
                    level="ERROR",
                )

            # --- Creator input_select helper ---
            try:
                creator_options = self._creators or ["Anonymous"]
                created = await prov.ensure_helper(
                    "input_select",
                    "Vestaboard Creator",
                    options=creator_options,
                )
                msg = "created" if created else "already exists"
                self.log(
                    "Helper input_select.vestaboard_creator %s",
                    msg,
                    level="INFO" if created else "DEBUG",
                )
            except Exception as exc:
                self.log(
                    "Failed to provision input_select 'Vestaboard Creator': %r",
                    exc,
                    level="ERROR",
                )

        except Exception as exc:
            self.log("Provisioning failed unexpectedly: %r", exc, level="ERROR")

    # ------------------------------------------------------------------
    # Command routing
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Dispatch incoming card commands to the appropriate handler."""
        command = data.get("command", "")
        raw_payload = data.get("payload", "{}")
        self.log("Received command: %r payload_len=%d", command, len(str(raw_payload)))

        try:
            payload: dict = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except (json.JSONDecodeError, TypeError) as exc:
            self.log("Failed to decode payload for command %r: %r", command, exc, level="ERROR")
            return

        try:
            self._dispatch_command(command, payload)
        except Exception as exc:
            self.log(
                "Unhandled error processing command %r: %r",
                command,
                exc,
                level="ERROR",
            )

    def _dispatch_command(self, command: str, payload: dict) -> None:
        handlers = {
            "save_frame": self._cmd_save_frame,
            "update_frame": self._cmd_update_frame,
            "delete_frame": self._cmd_delete_frame,
            "push_frame": self._cmd_push_frame,
            "push_library_frame": self._cmd_push_library_frame,
            "toggle_automation": self._cmd_toggle_automation,
            "set_automation_config": self._cmd_set_automation_config,
            "refresh_status": self._cmd_refresh_status,
            "add_creator": self._cmd_add_creator,
            "move_frame": self._cmd_move_frame,
            "quick_save_art": self._cmd_quick_save_art,
            "clear_art_preview": self._cmd_clear_art_preview,
            "generate_art": self._cmd_generate_art,
            "generate_ai_message": self._cmd_generate_ai_message,
            "save_art_to_library": self._cmd_save_art_to_library,
        }
        handler = handlers.get(command)
        if handler is None:
            self.log("Unknown command: %r", command, level="WARNING")
            return
        handler(payload)

    # ------------------------------------------------------------------
    # Command handlers — library mutations
    # ------------------------------------------------------------------

    def _cmd_save_frame(self, payload: dict) -> None:
        """Save a new static frame to the library.

        Accepts an optional ``category`` field ("message" or "art").
        Defaults to "message" for backward compatibility.
        """
        frame_data = payload.get("frame")
        name = str(payload.get("name", "")).strip()
        creator = str(payload.get("creator", "Anonymous"))
        rating = int(payload.get("rating", 0))
        category = str(payload.get("category", "message"))

        if not frame_data:
            self.log("save_frame: missing 'frame' in payload", level="WARNING")
            return
        if not name:
            self.log("save_frame: missing or empty 'name' in payload", level="WARNING")
            return

        # Deduplicate: if a frame with identical characters exists, update it
        existing = self._frame_library.find_by_characters(frame_data)
        duplicate_name = self._frame_library.find_by_name(
            name,
            exclude_frame_id=existing.frame_id if existing else None,
        )
        if duplicate_name is not None:
            self.log(
                "save_frame: duplicate 'name' %r conflicts with frame_id=%r",
                name,
                duplicate_name.frame_id,
                level="WARNING",
            )
            return

        if existing is not None:
            self._frame_library.update_frame(
                existing.frame_id, name=name, creator=creator, rating=rating,
                category=category,
            )
            self._publish_status()
            self.log(
                "save_frame: duplicate characters — updated existing "
                "frame_id=%r name=%r creator=%r category=%r",
                existing.frame_id,
                name,
                creator,
                category,
            )
            return

        frame = LibraryFrame(
            frame_id=uuid.uuid4().hex,
            characters=frame_data,
            creator=creator,
            created_at=time.time(),
            rating=rating,
            name=name,
            category=category,
        )
        self._frame_library.add_frame(frame)
        self._publish_status()
        self.log(
            "Saved frame frame_id=%r name=%r creator=%r category=%r",
            frame.frame_id,
            name,
            creator,
            category,
        )

    def _cmd_update_frame(self, payload: dict) -> None:
        """Update mutable fields on an existing library frame."""
        frame_id = payload.get("frame_id")
        if not frame_id:
            self.log("update_frame: missing 'frame_id' in payload", level="WARNING")
            return

        # Collect updatable fields from payload (immutable keys are rejected by library)
        update_fields = {
            k: v
            for k, v in payload.items()
            if k not in ("frame_id",)
        }
        update_fields.pop("frame_id", None)

        if "name" in update_fields:
            name = str(update_fields["name"]).strip()
            if not name:
                self.log("update_frame: empty 'name' in payload", level="WARNING")
                return
            duplicate_name = self._frame_library.find_by_name(
                name,
                exclude_frame_id=frame_id,
            )
            if duplicate_name is not None:
                self.log(
                    "update_frame: duplicate 'name' %r conflicts with frame_id=%r",
                    name,
                    duplicate_name.frame_id,
                    level="WARNING",
                )
                return
            update_fields["name"] = name

        # The card sends "frame" but the dataclass field is "characters"
        if "frame" in update_fields:
            update_fields["characters"] = update_fields.pop("frame")

        updated = self._frame_library.update_frame(frame_id, **update_fields)
        if updated:
            self._publish_status()
            self.log("Updated frame frame_id=%r fields=%s", frame_id, list(update_fields.keys()))
        else:
            self.log("update_frame: frame_id=%r not found", frame_id, level="WARNING")

    def _cmd_delete_frame(self, payload: dict) -> None:
        """Remove a frame from the library."""
        frame_id = payload.get("frame_id")
        if not frame_id:
            self.log("delete_frame: missing 'frame_id' in payload", level="WARNING")
            return

        removed = self._frame_library.delete_frame(frame_id)
        if removed:
            self._publish_status()
            self.log("Deleted frame frame_id=%r", frame_id)
        else:
            self.log("delete_frame: frame_id=%r not found", frame_id, level="WARNING")

    def _cmd_move_frame(self, payload: dict) -> None:
        """Move a frame between categories (e.g. message ↔ art)."""
        frame_id = payload.get("frame_id")
        category = payload.get("category")
        if not frame_id:
            self.log("move_frame: missing 'frame_id' in payload", level="WARNING")
            return
        if not category:
            self.log("move_frame: missing 'category' in payload", level="WARNING")
            return

        updated = self._frame_library.update_frame(frame_id, category=category)
        if updated:
            self._publish_status()
            self.log("Moved frame frame_id=%r to category=%r", frame_id, category)
        else:
            self.log("move_frame: frame_id=%r not found", frame_id, level="WARNING")

    def _cmd_quick_save_art(self, payload: dict) -> None:
        """Quick-save generated art with backend-generated defaults.

        Saves with:
          - category: "art"
          - creator: "AI" (or payload override)
          - name: auto-generated unique name ("AI Art <short-hex>")
          - rating: 0

        TTL and should_expire are push-time concepts, not stored on
        library frames.  The frontend should set those when pushing.
        """
        frame = payload.get("frame")
        if not frame:
            self.log("quick_save_art: missing 'frame' in payload", level="WARNING")
            return

        short_id = uuid.uuid4().hex[:8]
        name = str(payload.get("name", f"AI Art {short_id}"))
        creator = str(payload.get("creator", "AI"))

        lib_frame = LibraryFrame(
            frame_id=uuid.uuid4().hex,
            characters=frame,
            creator=creator,
            created_at=time.time(),
            rating=0,
            name=name,
            category="art",
        )
        self._frame_library.add_frame(lib_frame)
        self._publish_status()
        self.log(
            "Quick-saved art frame_id=%r name=%r creator=%r",
            lib_frame.frame_id,
            name,
            creator,
        )

    # ------------------------------------------------------------------
    # Command handlers — push to controller
    # ------------------------------------------------------------------

    def _cmd_push_frame(self, payload: dict) -> None:
        """Push a frame directly to the controller (override TTL)."""
        frame = payload.get("frame")
        if not frame:
            self.log("push_frame: missing 'frame' in payload", level="WARNING")
            return

        ctrl_payload = {
            "frame": frame,
            "ttl_minutes": payload.get("ttl_minutes"),
            "should_expire": payload.get("should_expire", False),
            "respect_ttl": False,
        }
        self._forward_to_controller("push_frame", ctrl_payload)

    def _cmd_push_library_frame(self, payload: dict) -> None:
        """Look up a frame by ID and push its characters to the controller."""
        frame_id = payload.get("frame_id")
        if not frame_id:
            self.log("push_library_frame: missing 'frame_id' in payload", level="WARNING")
            return

        frame = self._frame_library.get_frame(frame_id)
        if frame is None:
            self.log(
                "push_library_frame: frame_id=%r not found in library",
                frame_id,
                level="WARNING",
            )
            return

        ctrl_payload = {
            "frame": frame.characters,
            "ttl_minutes": payload.get("ttl_minutes"),
            "respect_ttl": payload.get("respect_ttl", False),
            "should_expire": True,
        }
        self._forward_to_controller("push_frame", ctrl_payload)
        self.log("Forwarded library frame frame_id=%r to controller", frame_id)

    # ------------------------------------------------------------------
    # Command handlers — automation management (forwarded to controller)
    # ------------------------------------------------------------------

    def _cmd_toggle_automation(self, payload: dict) -> None:
        """Enable or disable a board automation via the controller."""
        automation_id = payload.get("automation_id")
        enabled = payload.get("enabled")
        if automation_id is None:
            self.log("toggle_automation: missing 'automation_id' in payload", level="WARNING")
            return
        command = "activate_automation" if enabled else "deactivate_automation"
        self._forward_to_controller(command, {"automation_id": automation_id})
        self.log("toggle_automation automation_id=%r enabled=%r → %s", automation_id, enabled, command)

    def _cmd_set_automation_config(self, payload: dict) -> None:
        """Update configuration for a board automation.

        Accepts arbitrary config fields from the store UI and forwards
        them to the controller.  The ``automation_id`` key is required;
        the ``config`` dict holds the actual field values to update.
        Legacy ``ttl_minutes`` / ``expiration_minutes`` at the top level
        are still supported for backward compatibility.
        """
        automation_id = payload.get("automation_id")
        if automation_id is None:
            self.log("set_automation_config: missing 'automation_id' in payload", level="WARNING")
            return

        # New-style: config dict with arbitrary fields
        config = payload.get("config")
        if config and isinstance(config, dict):
            ctrl_payload = {
                "automation_id": automation_id,
                **config,
            }
        else:
            # Legacy style: top-level ttl_minutes / expiration_minutes
            ctrl_payload = {
                "automation_id": automation_id,
                "ttl_minutes": payload.get("ttl_minutes"),
                "expiration_minutes": payload.get("expiration_minutes"),
            }
        self._forward_to_controller("set_automation_config", ctrl_payload)
        self.log("set_automation_config automation_id=%r config=%r", automation_id, ctrl_payload)

    # ------------------------------------------------------------------
    # Command handlers — misc
    # ------------------------------------------------------------------

    def _cmd_refresh_status(self, payload: dict) -> None:
        """Re-publish the current configuration status sensor."""
        self._publish_status()
        self.log("Status refreshed on request")

    def _cmd_add_creator(self, payload: dict) -> None:
        """Add a new name to the creators list and update the input_select."""
        name = str(payload.get("name", "")).strip()
        if not name:
            self.log("add_creator: empty name, ignoring", level="WARNING")
            return
        if name in self._creators:
            self.log("add_creator: %r already in creators list", name)
            return

        self._creators.append(name)
        self.log("Added creator %r — updating input_select", name)

        # Refresh the input_select options via call_service
        try:
            self.call_service(
                "input_select/set_options",
                entity_id="input_select.vestaboard_creator",
                options=self._creators,
            )
        except Exception as exc:
            self.log("Failed to update input_select options: %r", exc, level="WARNING")

        self._publish_status()

    def _cmd_clear_art_preview(self, payload: dict) -> None:
        """Clear the AI art preview from status."""
        self._forward_to_controller("clear_ai_art_preview", {})
        self.log("clear_art_preview forwarded to controller")

    def _cmd_generate_art(self, payload: dict) -> None:
        """Forward an art generation request to the controller as a preview.

        The controller generates the art but does NOT push it to the board.
        Instead it stores the result in ``ai_art_preview`` status attribute,
        which mirrors into our sensor so the card can render it as a preview.
        """
        subject = payload.get("subject", "")
        self._forward_to_controller("generate_ai_art_preview", {"subject": subject})
        self.log("generate_art subject=%r forwarded to controller as generate_ai_art_preview", subject)

    def _cmd_generate_ai_message(self, payload: dict) -> None:
        """Forward an AI message generation request to the controller."""
        self._forward_to_controller("generate_ai_message", {})
        self.log("generate_ai_message forwarded to controller")

    def _cmd_save_art_to_library(self, payload: dict) -> None:
        """Save AI-generated art as a library frame."""
        frame = payload.get("frame")
        name = str(payload.get("name", "AI Art"))
        creator = str(payload.get("creator", "AI"))

        if not frame:
            self.log("save_art_to_library: missing 'frame' in payload", level="WARNING")
            return

        lib_frame = LibraryFrame(
            frame_id=uuid.uuid4().hex,
            characters=frame,
            creator=creator,
            created_at=time.time(),
            rating=0,
            name=name,
            category="art",
        )
        self._frame_library.add_frame(lib_frame)
        self._publish_status()
        self.log("Saved AI art to library frame_id=%r name=%r", lib_frame.frame_id, name)

    # ------------------------------------------------------------------
    # Controller event forwarding
    # ------------------------------------------------------------------

    def _forward_to_controller(self, command: str, payload: dict) -> None:
        """Fire a controller command event."""
        self.fire_event(
            self.CONTROLLER_COMMAND_EVENT,
            command=command,
            payload=json.dumps(payload),
        )
        self.log("Forwarded to controller: %s", command)

    # ------------------------------------------------------------------
    # Controller status mirroring
    # ------------------------------------------------------------------

    def _on_controller_status(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        kwargs: Any,
    ) -> None:
        """Cache controller status attributes and republish our sensor."""
        try:
            attrs = self.get_state(self.CONTROLLER_STATUS_ENTITY, attribute="all") or {}
            self._controller_attrs = dict(attrs.get("attributes", {}))
            self.log(
                "Controller status updated — republishing configuration status",
                level="DEBUG",
            )
        except Exception as exc:
            self.log(
                "Failed to read controller status attributes: %r",
                exc,
                level="WARNING",
            )
        self._publish_status()

    def _deferred_controller_sync(self, kwargs: Any) -> None:
        """Read controller status after a short delay to cover startup race."""
        try:
            ctrl_state = self.get_state(self.CONTROLLER_STATUS_ENTITY, attribute="all") or {}
            new_attrs = dict(ctrl_state.get("attributes", {}))
            if new_attrs and new_attrs != self._controller_attrs:
                self._controller_attrs = new_attrs
                self._publish_status()
                self.log("Deferred controller sync — status updated", level="INFO")
            else:
                self.log("Deferred controller sync — no new data", level="DEBUG")
        except Exception as exc:
            self.log("Deferred controller sync failed: %r", exc, level="WARNING")

    # ------------------------------------------------------------------
    # Status publishing
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        """Write the configuration status sensor with merged controller data."""
        ca = self._controller_attrs

        # Map controller attribute names to what the card expects
        displayed = ca.get("displayed_frame")  # dict with characters, source, etc.
        queue_state = ca.get("queue_state", {})

        # Compute TTL expiry ISO timestamp from remaining seconds
        ttl_remaining = ca.get("displayed_ttl_remaining_s")
        ttl_expires = None
        if ttl_remaining is not None and ttl_remaining > 0:
            import datetime
            ttl_expires = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=ttl_remaining)
            ).isoformat()

        attrs = {
            "current_frame": displayed.get("characters") if displayed else None,
            "current_source": ca.get("displayed_source", "none"),
            "current_ttl_expires": ttl_expires,
            "queue": queue_state.get("pending", []),
            "fallback_source": (queue_state.get("fallback", [{}])[0].get("source")
                                if queue_state.get("fallback") else None),
            "library": self._frame_library.to_json(),
            "creators": self._creators,
            "automations": ca.get("all_automations", []),
            "ai_art_preview": json.dumps(ca["ai_art_preview"]) if ca.get("ai_art_preview") else None,
            "status": "ok",
        }
        try:
            self.set_state(self.SENSOR_ENTITY, state="ok", attributes=attrs)
            self.log("Published status to %s", self.SENSOR_ENTITY, level="DEBUG")
        except Exception as exc:
            self.log("Failed to publish status: %r", exc, level="ERROR")

    # ------------------------------------------------------------------
    # Library seeding
    # ------------------------------------------------------------------

    def _seed_library(self) -> None:
        """Add a Hello World frame if the library is empty."""
        if self._frame_library.list_frames():
            return

        from providers.vestaboard.character_encoding import text_to_grid

        hello_grid = text_to_grid("HELLO WORLD", justify="center", align="center")
        hello_frame = LibraryFrame(
            frame_id=uuid.uuid4().hex,
            characters=hello_grid,
            creator="Tom",
            created_at=time.time(),
            rating=3,
            name="Hello World",
        )
        self._frame_library.add_frame(hello_frame)
        self.log("Seeded library with Hello World frame")
