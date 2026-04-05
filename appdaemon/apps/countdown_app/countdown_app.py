"""Countdown AppDaemon app.

Manages multiple countdowns with AI-generated background images.
Publishes a sensor (sensor.countdown_status) that drives a wall-display
countdown card.  Supports add/edit/delete countdowns and on-demand image
generation via a relay script pattern.

All external HTTP calls (AI image generation) are delegated to providers
(security rule S2).  Secrets are never stored in app code (rule S1).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# AppDaemon only adds `appdaemon/apps` to sys.path.  Providers live at
# `appdaemon/providers`, so append the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from providers.secrets import resolve_arg_secret

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENSOR_STATUS = "sensor.countdown_status"
RELAY_SCRIPT_ID = "countdown_relay"
COMMAND_EVENT = "countdown_command"

# Default text style applied to new countdowns
_DEFAULT_TEXT_STYLE: Dict[str, Any] = {
    "font_size": 50,
    "color": "#ffd24a",
    "position_y": 70,
    "text_shadow": True,
}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class CountdownApp(hass.Hass):
    """Wall-display countdown app with AI-generated background images."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        cfg = self.args or {}

        # --- HA provisioning ---
        self._ha_url: str = str(resolve_arg_secret(cfg, "ha_url", default=""))
        self._ha_token_env: str = str(cfg.get("ha_token_env", ""))

        # --- AI provider config ---
        # Stored as raw args; passed to registry lazily during image generation.
        self._args = cfg

        # --- Filesystem paths ---
        media_fs_root: str = str(
            resolve_arg_secret(cfg, "media_fs_root", default="/media")
        ).rstrip("/") or "/media"

        self._media_subdir: str = str(cfg.get("media_subdir", "countdown-app"))
        self._www_subdir: str = str(cfg.get("www_subdir", "countdown-app"))
        self._media_dir: str = os.path.join(media_fs_root, self._media_subdir)

        # --- Shell command for syncing images to www ---
        self._image_sync_shell_command: str = str(
            cfg.get("image_sync_shell_command", "countdown_sync_images")
        )

        # --- Refresh intervals ---
        self._rotation_interval_s: int = int(cfg.get("rotation_interval_s", 15))
        self._countdown_refresh_s: int = int(cfg.get("countdown_refresh_s", 60))

        # --- Ensure media directory exists ---
        os.makedirs(self._media_dir, exist_ok=True)

        # --- Load persisted state ---
        self._load_state()

        self.log(
            f"CountdownApp initializing — media_dir={self._media_dir!r} "
            f"www_subdir={self._www_subdir!r} "
            f"rotation_interval_s={self._rotation_interval_s} "
            f"countdown_refresh_s={self._countdown_refresh_s} "
            f"countdowns_loaded={len(self._countdowns)}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Provision relay, publish initial sensor, register listeners and timers."""
        await self._provision_relay()
        self._publish_sensor()
        self.listen_event(self._on_command, COMMAND_EVENT)
        self.run_every(self._on_countdown_tick, "now", self._countdown_refresh_s)
        self.run_every(
            self._on_rotation_tick,
            f"now+{self._rotation_interval_s}",
            self._rotation_interval_s,
        )
        self.log("CountdownApp started", level="INFO")

    # ------------------------------------------------------------------
    # Relay provisioning
    # ------------------------------------------------------------------

    async def _provision_relay(self) -> None:
        """Create the relay script if it does not exist."""
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
                RELAY_SCRIPT_ID,
                {
                    "alias": "Countdown Relay",
                    "description": "Relays countdown card commands to AppDaemon",
                    "mode": "queued",
                    "max": 10,
                    "fields": {
                        "command": {
                            "name": "Command",
                            "description": "Command name",
                            "required": True,
                            "selector": {"text": {}},
                        },
                        "payload": {
                            "name": "Payload",
                            "description": "JSON-encoded command data",
                            "required": False,
                            "selector": {"text": {}},
                        },
                    },
                    "sequence": [
                        {
                            "event": COMMAND_EVENT,
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
                f"Relay script.{RELAY_SCRIPT_ID} {msg}",
                level="INFO" if created else "DEBUG",
            )
        except Exception as exc:
            self.log(
                f"Failed to provision relay script: {exc!r}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _state_file_path(self) -> str:
        return os.path.join(self._media_dir, "countdowns.json")

    def _load_state(self) -> None:
        """Load countdowns from JSON file; initialise empty state if missing."""
        state_file = self._state_file_path()
        try:
            with open(state_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._countdowns: List[Dict[str, Any]] = list(data.get("countdowns", []))
            self._active_index: int = int(data.get("active_index", 0))
            self.log(
                f"State loaded from {state_file!r}: "
                f"{len(self._countdowns)} countdowns, active_index={self._active_index}",
                level="INFO",
            )
        except FileNotFoundError:
            self._countdowns = []
            self._active_index = 0
            self.log(
                f"No state file found at {state_file!r} — starting fresh",
                level="INFO",
            )
        except Exception as exc:
            self._countdowns = []
            self._active_index = 0
            self.log(
                f"Failed to load state from {state_file!r}: {exc!r} — starting fresh",
                level="ERROR",
            )

    def _save_state(self) -> None:
        """Atomically write countdowns state to JSON file."""
        state_file = self._state_file_path()
        tmp_file = state_file + ".tmp"
        data = {
            "countdowns": self._countdowns,
            "active_index": self._active_index,
        }
        try:
            with open(tmp_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_file, state_file)
            self.log(
                f"State saved to {state_file!r}: {len(self._countdowns)} countdowns",
                level="DEBUG",
            )
        except Exception as exc:
            self.log(
                f"Failed to save state to {state_file!r}: {exc!r}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Countdown logic
    # ------------------------------------------------------------------

    def _compute_countdown_text(self, countdown: Dict[str, Any]) -> str:
        """Return display text for a countdown.

        Returns "XD YH ZM" while counting down, "NOW" for the 24h window
        immediately after the target, or "" (empty) once more than 24h have
        elapsed past the target (caller should treat empty as expired/hidden).
        """
        target_str: str = countdown.get("target_datetime", "")
        if not target_str:
            return ""

        try:
            target = datetime.fromisoformat(target_str)
        except (ValueError, TypeError):
            self.log(
                f"Invalid target_datetime {target_str!r} for countdown {countdown.get('id')!r}",
                level="WARNING",
            )
            return ""

        now = datetime.now()
        delta = target - now

        if delta.total_seconds() > 0:
            total_seconds = int(delta.total_seconds())
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{days}D {hours}H {minutes}M"

        # Target has passed — show "NOW" for the first 24 hours
        elapsed = -delta.total_seconds()
        if elapsed < 86400:
            return "NOW"

        # More than 24h in the past — treat as expired
        return ""

    def _visible_countdowns(self) -> List[Dict[str, Any]]:
        """Return countdowns that are not hidden and not expired."""
        result: List[Dict[str, Any]] = []
        for c in self._countdowns:
            if c.get("hidden", False):
                continue
            if self._compute_countdown_text(c) == "":
                continue
            result.append(c)
        return result

    # ------------------------------------------------------------------
    # Timer callbacks
    # ------------------------------------------------------------------

    def _on_rotation_tick(self, kwargs: Any) -> None:
        """Advance the active index to the next visible countdown."""
        visible = self._visible_countdowns()
        if visible:
            self._active_index = (self._active_index + 1) % len(visible)
        self.log(
            f"Rotation tick — active_index={self._active_index} visible={len(visible)}",
            level="DEBUG",
        )
        self._publish_sensor()

    def _on_countdown_tick(self, kwargs: Any) -> None:
        """Refresh sensor to update countdown text values."""
        self.log("Countdown tick — refreshing sensor", level="DEBUG")
        self._publish_sensor()

    # ------------------------------------------------------------------
    # Sensor publishing
    # ------------------------------------------------------------------

    def _publish_sensor(self) -> None:
        """Write current countdown state to sensor.countdown_status."""
        visible = self._visible_countdowns()
        idx = self._active_index % len(visible) if visible else 0
        active = visible[idx] if visible else None

        state_text = self._compute_countdown_text(active) if active else "idle"

        self.log(
            f"Publishing sensor: state={state_text!r} visible={len(visible)} "
            f"total={len(self._countdowns)} active_index={self._active_index}",
            level="DEBUG",
        )

        self.set_state(
            SENSOR_STATUS,
            state=state_text,
            attributes={
                "active_countdown": self._serialize_countdown(active) if active else {},
                "countdowns": [self._serialize_countdown(c) for c in self._countdowns],
                "visible_count": len(visible),
                "active_index": self._active_index,
                "friendly_name": "Countdown Status",
            },
        )

    def _serialize_countdown(self, countdown: Dict[str, Any]) -> Dict[str, Any]:
        """Return countdown dict enriched with computed display fields."""
        result = dict(countdown)
        result["countdown_text"] = self._compute_countdown_text(countdown)

        filename = countdown.get("image_filename", "")
        if filename:
            image_path = os.path.join(self._media_dir, filename)
            try:
                mtime = int(os.path.getmtime(image_path))
            except OSError:
                mtime = 0
            result["image_url"] = f"/local/{self._www_subdir}/{filename}"
            result["image_version"] = mtime
        else:
            result["image_url"] = ""
            result["image_version"] = 0

        return result

    # ------------------------------------------------------------------
    # Command routing
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Route commands dispatched by the relay script."""
        cmd = data.get("command")
        raw = data.get("payload", "{}")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            self.log(f"Invalid command payload: {raw!r}", level="WARNING")
            return

        self.log(f"Received command: {cmd!r} payload_keys={list(payload.keys()) if isinstance(payload, dict) else []}", level="DEBUG")

        if cmd == "save_countdown":
            self._handle_save_countdown(payload)
        elif cmd == "delete_countdown":
            self._handle_delete_countdown(payload)
        elif cmd == "generate_image":
            self.create_task(self._handle_generate_image(payload))
        elif cmd == "update_style":
            self._handle_update_style(payload)
        elif cmd == "set_active":
            self._handle_set_active(payload)
        else:
            self.log(f"Unknown command: {cmd!r}", level="WARNING")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_save_countdown(self, payload: Dict[str, Any]) -> None:
        """Create or update a countdown from a card command."""
        countdown_id = payload.get("id", "")

        # Find existing countdown
        existing: Optional[Dict[str, Any]] = None
        for c in self._countdowns:
            if c.get("id") == countdown_id:
                existing = c
                break

        if existing is not None:
            # Update only fields present in payload
            for field in ("title", "subtitle", "target_datetime", "image_prompt"):
                if field in payload:
                    existing[field] = payload[field]
            if "text_style" in payload and isinstance(payload["text_style"], dict):
                if not isinstance(existing.get("text_style"), dict):
                    existing["text_style"] = dict(_DEFAULT_TEXT_STYLE)
                existing["text_style"].update(payload["text_style"])
            self.log(
                f"Countdown updated: id={countdown_id!r} title={existing.get('title')!r}",
                level="INFO",
            )
        else:
            # Create new countdown
            new_id = str(uuid.uuid4())
            text_style = dict(_DEFAULT_TEXT_STYLE)
            if isinstance(payload.get("text_style"), dict):
                text_style.update(payload["text_style"])
            new_countdown: Dict[str, Any] = {
                "id": new_id,
                "title": payload.get("title", ""),
                "subtitle": payload.get("subtitle", ""),
                "target_datetime": payload.get("target_datetime", ""),
                "image_prompt": payload.get("image_prompt", ""),
                "image_filename": "",
                "text_style": text_style,
                "created_at": datetime.now().isoformat(),
                "hidden": False,
            }
            self._countdowns.append(new_countdown)
            self.log(
                f"Countdown created: id={new_id!r} title={new_countdown['title']!r} "
                f"target={new_countdown['target_datetime']!r}",
                level="INFO",
            )

        self._save_state()
        self._publish_sensor()

    def _handle_delete_countdown(self, payload: Dict[str, Any]) -> None:
        """Remove a countdown and its image file."""
        countdown_id = payload.get("id", "")
        if not countdown_id:
            self.log("delete_countdown command missing 'id'", level="WARNING")
            return

        to_remove: Optional[Dict[str, Any]] = None
        for c in self._countdowns:
            if c.get("id") == countdown_id:
                to_remove = c
                break

        if to_remove is None:
            self.log(
                f"delete_countdown: countdown not found: {countdown_id!r}",
                level="WARNING",
            )
            return

        self._countdowns.remove(to_remove)

        # Delete image file if present
        filename = to_remove.get("image_filename", "")
        if filename:
            image_path = os.path.join(self._media_dir, filename)
            try:
                os.remove(image_path)
                self.log(
                    f"Deleted image file: {image_path!r}",
                    level="INFO",
                )
            except OSError as exc:
                self.log(
                    f"Could not delete image file {image_path!r}: {exc!r}",
                    level="WARNING",
                )

        # Cap active_index to valid range
        if self._countdowns:
            self._active_index = min(self._active_index, len(self._countdowns) - 1)
        else:
            self._active_index = 0

        self.log(
            f"Countdown deleted: id={countdown_id!r} title={to_remove.get('title')!r}",
            level="INFO",
        )
        self._save_state()
        self._publish_sensor()

    async def _handle_generate_image(self, payload: Dict[str, Any]) -> None:
        """Generate a background image for a countdown using the configured AI provider."""
        countdown_id = payload.get("id", "")
        prompt = payload.get("prompt", "")

        if not countdown_id:
            self.log("generate_image command missing 'id'", level="WARNING")
            return

        countdown: Optional[Dict[str, Any]] = None
        for c in self._countdowns:
            if c.get("id") == countdown_id:
                countdown = c
                break

        if countdown is None:
            self.log(
                f"generate_image: countdown not found: {countdown_id!r}",
                level="WARNING",
            )
            return

        if not prompt:
            prompt = countdown.get("image_prompt", "")

        if not prompt:
            self.log(
                f"generate_image: no prompt for countdown {countdown_id!r}",
                level="WARNING",
            )
            return

        # Build image provider (lazy import to avoid startup failures)
        try:
            from providers.ai_providers.registry import (
                provider_config_from_appdaemon_args,
                build_image_provider,
            )
            cfg = provider_config_from_appdaemon_args(self._args)
            provider = build_image_provider(cfg)
        except Exception as exc:
            self.log(
                f"generate_image: failed to build image provider: {exc!r}",
                level="ERROR",
            )
            return

        filename = f"{countdown['id']}.png"
        output_path = os.path.join(self._media_dir, filename)

        # Signal generating state so card can show loading indicator
        countdown["generating"] = True
        self._publish_sensor()

        self.log(
            f"Generating image for countdown {countdown_id!r} "
            f"title={countdown.get('title')!r} prompt={prompt!r} "
            f"output={output_path!r}",
            level="INFO",
        )

        try:
            result = provider.edit_image(
                input_image_paths=[],
                prompt=prompt,
                output_image_path=output_path,
            )
            countdown["image_filename"] = filename
            countdown.pop("generating", None)
            self.log(
                f"Image generated for countdown {countdown_id!r}: "
                f"output={output_path!r} result_keys={list(result.keys()) if isinstance(result, dict) else 'n/a'}",
                level="INFO",
            )
            # Sync image to www directory
            try:
                self.call_service(f"shell_command/{self._image_sync_shell_command}")
            except Exception as sync_exc:
                self.log(
                    f"Image sync shell command failed: {sync_exc!r}",
                    level="WARNING",
                )
        except Exception as exc:
            countdown.pop("generating", None)
            self.log(
                f"Image generation failed for countdown {countdown_id!r}: {exc!r}",
                level="ERROR",
            )

        self._save_state()
        self._publish_sensor()

    def _handle_update_style(self, payload: Dict[str, Any]) -> None:
        """Update text_style fields for a specific countdown."""
        countdown_id = payload.get("id", "")
        if not countdown_id:
            self.log("update_style command missing 'id'", level="WARNING")
            return

        countdown: Optional[Dict[str, Any]] = None
        for c in self._countdowns:
            if c.get("id") == countdown_id:
                countdown = c
                break

        if countdown is None:
            self.log(
                f"update_style: countdown not found: {countdown_id!r}",
                level="WARNING",
            )
            return

        new_style = payload.get("text_style", {})
        if not isinstance(new_style, dict):
            self.log(
                f"update_style: text_style must be a dict, got {type(new_style).__name__}",
                level="WARNING",
            )
            return

        if not isinstance(countdown.get("text_style"), dict):
            countdown["text_style"] = dict(_DEFAULT_TEXT_STYLE)

        countdown["text_style"].update(new_style)
        self.log(
            f"Style updated for countdown {countdown_id!r}: {new_style}",
            level="INFO",
        )
        self._save_state()
        self._publish_sensor()

    def _handle_set_active(self, payload: Dict[str, Any]) -> None:
        """Manually set the active countdown index."""
        index = payload.get("index", 0)
        self._active_index = int(index)
        self.log(
            f"Active index set to {self._active_index}",
            level="DEBUG",
        )
        self._publish_sensor()
