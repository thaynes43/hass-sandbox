"""Dashboard Notify — AI-generated notification carousel for wall displays."""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from dashboard_notify.notification_manager import (
    Notification,
    NotificationManager,
    priority_for_class,
)
from dashboard_notify.prompt_builder import (
    build_notification_prompt,
    build_placeholder_prompt,
)
from providers.ai_providers.registry import (
    build_image_provider,
    provider_config_from_appdaemon_args,
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class DashboardNotify(hass.Hass):
    """Wall-display notification carousel with AI-generated images."""

    SENSOR_ENTITY = "sensor.dashboard_notify_status"
    RELAY_SCRIPT_ID = "dashboard_notify_relay"
    COMMAND_EVENT = "dashboard_notify_command"

    def initialize(self) -> None:
        cfg = self.args or {}

        # Core paths
        # media_fs_root: local filesystem path that maps to HA's /media.
        # In prod (AppDaemon pod), this is /media (shared mount).
        # In dev, override to e.g. /mnt/cephfs-hdd/misc/hass-media.
        self._media_fs_root: str = str(
            cfg.get("media_fs_root", "/media")
        ).rstrip("/") or "/media"
        # media_subdir: subdirectory under media_fs_root for this app's files
        self._media_subdir: str = "dashboard-notify"
        self._media_dir: str = os.path.join(self._media_fs_root, self._media_subdir)
        self._www_subdir: str = str(cfg.get("www_subdir", "dashboard-notify"))
        self._stage_shell_command: str = str(
            cfg.get("stage_shell_command", "dashboard_notify_stage")
        )

        # Carousel settings
        self._carousel_interval_s: int = int(cfg.get("carousel_interval_s", 10))
        self._default_ttl_s: int = int(cfg.get("default_ttl_s", 3600))
        self._no_notification_refresh_s: int = int(
            cfg.get("no_notification_refresh_s", 3600)
        )

        # HA credentials for provisioning
        self._ha_url: str = str(cfg.get("ha_url", ""))
        self._ha_token_env: str = str(cfg.get("ha_token_env", ""))

        # Notification configs from YAML
        self._notification_configs: list[dict[str, Any]] = list(
            cfg.get("notifications") or []
        )

        # Detection summary hook
        self._detection_hook: dict[str, Any] = dict(
            cfg.get("detection_summary_hook") or {}
        )

        # State
        self._manager = NotificationManager()
        self._current_index: int = 0
        self._paused: bool = False
        self._placeholder_generated_at: float = 0.0
        self._placeholder_url: str = ""
        self._active_generations: set[str] = set()

        # Build image provider
        try:
            img_cfg = provider_config_from_appdaemon_args(cfg)
            self._image_provider = build_image_provider(img_cfg)
            self.log("Image provider: %s", self._image_provider.name)
        except Exception as e:
            self.log("Failed to build image provider: %s", e, level="WARNING")
            self._image_provider = None

        # Ensure directories exist
        os.makedirs(os.path.join(self._media_dir, "staged"), exist_ok=True)
        os.makedirs(os.path.join(self._media_dir, "generated"), exist_ok=True)

        # Schedule async startup (provisioning)
        self.run_in(self._async_startup, 0)

        # Schedule checker + expiry pruner (every 60s, first tick after 5s)
        self.run_in(self._tick, 5)
        self.run_every(self._tick, "now+65", 60)

        # Carousel advance timer
        self.run_every(self._carousel_advance, "now+15", self._carousel_interval_s)

        # Listen for relay commands from the card
        self.listen_event(self._handle_command, self.COMMAND_EVENT)

        # Listen for detection summary events
        if _as_bool(self._detection_hook.get("enabled"), False):
            self.listen_event(
                self._handle_detection_published,
                "detection_summary/run_published",
            )
            self.log(
                "Detection summary hook enabled for bundles: %s",
                self._detection_hook.get("bundle_keys", []),
            )

        # Publish initial sensor state
        self._publish_state()
        self.log("DashboardNotify initialized with %d notification configs",
                 len(self._notification_configs))

    # ------------------------------------------------------------------
    # Async startup — provisioning
    # ------------------------------------------------------------------

    def _async_startup(self, kwargs: Any) -> None:
        self.create_task(self._provision_entities())

    async def _provision_entities(self) -> None:
        """Provision relay script via HAProvisioner."""
        ha_url = self._ha_url
        ha_token_env = self._ha_token_env
        if not ha_url or not ha_token_env:
            self.log("Skipping provisioning — no HA credentials configured")
            return

        try:
            from providers.ha_provisioner import HAProvisioner

            prov = HAProvisioner(ha_url, ha_token_env)

            script_config = {
                "alias": "Dashboard Notify Relay",
                "description": "Relay card commands to AppDaemon via event",
                "mode": "queued",
                "max": 5,
                "fields": {
                    "command": {
                        "description": "Command name",
                        "example": "next",
                        "required": True,
                        "selector": {"text": {}},
                    },
                    "payload": {
                        "description": "JSON payload",
                        "example": "{}",
                        "required": False,
                        "default": "{}",
                        "selector": {"text": {}},
                    },
                },
                "sequence": [
                    {
                        "event": self.COMMAND_EVENT,
                        "event_data": {
                            "command": "{{ command }}",
                            "payload": "{{ payload }}",
                        },
                    }
                ],
            }

            created = await prov.ensure_script(self.RELAY_SCRIPT_ID, script_config)
            if created:
                self.log("Relay script created: script.%s", self.RELAY_SCRIPT_ID)
            else:
                self.log("Relay script already exists: script.%s", self.RELAY_SCRIPT_ID)

        except Exception as e:
            self.log("Error provisioning relay script: %s", e, level="WARNING")

    # ------------------------------------------------------------------
    # Schedule checker + expiry (runs every 60s)
    # ------------------------------------------------------------------

    def _tick(self, kwargs: Any) -> None:
        now = time.time()
        self.log("Tick: %d active, %d generating", self._manager.count(), len(self._active_generations))

        # Prune expired
        expired = self._manager.prune_expired(now)
        if expired:
            self.log("Pruned %d expired notifications: %s", len(expired), expired)
            self._sync_staged_dir()

        # Check notification schedules
        for config in self._notification_configs:
            nid = config.get("id", "")
            if not nid:
                continue
            if self._is_schedule_active(config.get("schedule"), now):
                if not self._manager.has(nid) and nid not in self._active_generations:
                    self._generate_notification(config, now)
            # If schedule inactive, let TTL handle expiry naturally

        # Handle placeholder when no notifications
        if self._manager.count() == 0:
            self._ensure_placeholder(now)

        self._publish_state()

    def _is_schedule_active(
        self, schedule: dict[str, Any] | None, now: float
    ) -> bool:
        """Check if current time falls within schedule window."""
        if not schedule:
            return False

        dt = datetime.fromtimestamp(now)
        current_minutes = dt.hour * 60 + dt.minute

        start_str = str(schedule.get("start", "00:00"))
        end_str = str(schedule.get("end", "23:59"))

        start_parts = start_str.split(":")
        end_parts = end_str.split(":")
        start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
        end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])

        # Check day filter
        days = schedule.get("days")
        if days:
            day_name = dt.strftime("%a").lower()
            if day_name not in [str(d).lower() for d in days]:
                return False

        # Handle overnight windows (e.g. 21:30 - 01:00)
        if start_minutes > end_minutes:
            return current_minutes >= start_minutes or current_minutes < end_minutes
        else:
            return start_minutes <= current_minutes < end_minutes

    # ------------------------------------------------------------------
    # File staging
    # ------------------------------------------------------------------

    def _sync_staged_dir(self) -> None:
        """Remove staged files that are no longer active.

        Keeps the staged directory in sync with active notifications +
        placeholder so the shell command only copies live assets to www.
        """
        staged_dir = os.path.join(self._media_dir, "staged")
        if not os.path.isdir(staged_dir):
            return

        # Collect filenames that should be kept
        keep: set[str] = set()
        for n in self._manager.active_notifications():
            keep.add(os.path.basename(n.image_path))
            # staged filename may differ from generated filename
            staged_name = os.path.basename(n.local_url.split("?")[0])
            keep.add(staged_name)
        if self._placeholder_url:
            keep.add(os.path.basename(self._placeholder_url.split("?")[0]))

        removed = 0
        for fname in os.listdir(staged_dir):
            if fname.endswith(".png") and fname not in keep:
                try:
                    os.remove(os.path.join(staged_dir, fname))
                    removed += 1
                except OSError:
                    pass
        if removed:
            self.log("Cleaned %d stale files from staged dir", removed)
            self._stage_to_www()

    def _stage_to_www(self) -> None:
        """Call staging shell command immediately and schedule a retry.

        The retry handles CephFS propagation delay in dev environments
        where AppDaemon and HA mount the same network storage but writes
        may not be immediately visible across mounts.
        """
        self.call_service("shell_command/" + self._stage_shell_command)
        self.run_in(self._stage_retry, 5)

    def _stage_retry(self, kwargs: Any) -> None:
        """Delayed retry of staging shell command."""
        self.call_service("shell_command/" + self._stage_shell_command)

    # ------------------------------------------------------------------
    # Image generation
    # ------------------------------------------------------------------

    def _generate_notification(self, config: dict[str, Any], now: float) -> None:
        """Generate an AI image for a notification config (synchronous)."""
        if not self._image_provider:
            self.log("No image provider configured, skipping generation")
            return

        nid = config["id"]
        self._active_generations.add(nid)

        try:
            notification_class = config.get("class", "BasicTextImage")
            text = config.get("text", "")
            prompt_hint = config.get("prompt_hint", "")
            ttl_s = int(config.get("ttl_s", self._default_ttl_s))

            prompt = build_notification_prompt(
                notification_text=text,
                prompt_hint=prompt_hint,
                image_class=notification_class,
            )

            timestamp = int(now)
            filename = f"{nid}_{timestamp}.png"
            output_path = os.path.join(
                self._media_dir, "generated", filename
            )

            self.log("Generating image for notification '%s' → %s", nid, output_path)
            result = self._image_provider.edit_image(
                input_image_paths=[],
                prompt=prompt,
                output_image_path=output_path,
            )
            self.log(
                "Notification '%s' API call complete: model=%s elapsed_s=%s",
                nid,
                result.get("model", "?"),
                result.get("elapsed_s", "?"),
            )

            # Stage for HA serving
            staged_path = os.path.join(
                self._media_dir, "staged", filename
            )
            shutil.copy2(output_path, staged_path)
            self.log("Staged notification '%s' → %s", nid, staged_path)
            self._stage_to_www()

            local_url = f"/local/{self._www_subdir}/{filename}"

            notification = Notification(
                id=nid,
                notification_class=notification_class,
                text=text,
                image_path=output_path,
                local_url=local_url,
                created_at=now,
                expires_at=now + ttl_s,
                priority=priority_for_class(notification_class),
                source_id=nid,
            )
            self._manager.add(notification)
            self.log("Notification '%s' generated and added (ttl=%ds)", nid, ttl_s)
            self._publish_state()

        except Exception as e:
            self.log(
                "Error generating notification '%s': %s", nid, e, level="WARNING"
            )
        finally:
            self._active_generations.discard(nid)

    def _ensure_placeholder(self, now: float) -> None:
        """Generate a placeholder image when no notifications are active."""
        if not self._image_provider:
            return
        if (
            self._placeholder_url
            and (now - self._placeholder_generated_at) < self._no_notification_refresh_s
        ):
            return
        if "placeholder" in self._active_generations:
            return

        self._active_generations.add("placeholder")

        try:
            prompt = build_placeholder_prompt()
            timestamp = int(now)
            filename = f"no_notifications_{timestamp}.png"
            output_path = os.path.join(
                self._media_dir, "generated", filename
            )

            self.log("Generating placeholder image → %s", output_path)
            result = self._image_provider.edit_image(
                input_image_paths=[],
                prompt=prompt,
                output_image_path=output_path,
            )
            self.log(
                "Placeholder API call complete: model=%s elapsed_s=%s",
                result.get("model", "?"),
                result.get("elapsed_s", "?"),
            )

            staged_path = os.path.join(
                self._media_dir, "staged", filename
            )
            shutil.copy2(output_path, staged_path)
            self.log("Staged placeholder → %s", staged_path)
            self._stage_to_www()

            self._placeholder_url = f"/local/{self._www_subdir}/{filename}"
            self._placeholder_generated_at = now
            self.log("Placeholder image generated and staged")
            self._publish_state()

        except Exception as e:
            self.log(
                "Error generating placeholder: %s", e, level="WARNING"
            )
        finally:
            self._active_generations.discard("placeholder")

    # ------------------------------------------------------------------
    # Detection summary hook
    # ------------------------------------------------------------------

    def _handle_detection_published(self, event: str, data: dict, kwargs: Any) -> None:
        """Handle detection_summary/run_published events."""
        bundle_key = data.get("bundle_key", "")
        allowed_keys = self._detection_hook.get("bundle_keys", [])
        if bundle_key not in allowed_keys:
            return

        run_id = data.get("run_id", "")
        summary = data.get("summary", "Detection event")

        # Derive the generated image filesystem path from bundle_key + run_id.
        # Detection summary stores images at:
        #   <media_fs_root>/detection-summary/<bundle_key>/runs/<run_id>/generated.png
        generated_image = os.path.join(
            self._media_fs_root,
            "detection-summary",
            bundle_key,
            "runs",
            run_id,
            "generated.png",
        )

        if not os.path.exists(generated_image):
            self.log(
                "Detection hook: no generated image at %s", generated_image
            )
            return

        nid = f"detection_{bundle_key}_{run_id}"
        ttl_s = int(self._detection_hook.get("ttl_s", 7200))
        now = time.time()

        # Copy to our media dir
        filename = f"{nid}.png"
        dest_path = os.path.join(self._media_dir, "staged", filename)
        try:
            shutil.copy2(generated_image, dest_path)
            self._stage_to_www()
        except Exception as e:
            self.log("Detection hook copy failed: %s", e, level="WARNING")
            return

        local_url = f"/local/{self._www_subdir}/{filename}"
        notification = Notification(
            id=nid,
            notification_class="PreexistingImage",
            text=str(summary)[:200],
            image_path=dest_path,
            local_url=local_url,
            created_at=now,
            expires_at=now + ttl_s,
            priority=priority_for_class("PreexistingImage"),
            source_id=bundle_key,
        )
        self._manager.add(notification)
        self.log("Detection notification added: %s (ttl=%ds)", nid, ttl_s)
        self._publish_state()

    # ------------------------------------------------------------------
    # Carousel + relay commands
    # ------------------------------------------------------------------

    def _carousel_advance(self, kwargs: Any) -> None:
        """Advance the carousel to the next notification."""
        if self._paused:
            return
        notifications = self._manager.active_notifications()
        if len(notifications) <= 1:
            return
        self._current_index = (self._current_index + 1) % len(notifications)
        self._publish_state()

    def _handle_command(self, event: str, data: dict, kwargs: Any) -> None:
        """Handle relay commands from the card."""
        command = data.get("command", "")
        notifications = self._manager.active_notifications()
        count = len(notifications)

        if command == "next":
            if count > 0:
                self._current_index = (self._current_index + 1) % count
        elif command == "previous":
            if count > 0:
                self._current_index = (self._current_index - 1) % count
        elif command == "toggle_pause":
            self._paused = not self._paused
        elif command == "dismiss":
            if count > 0 and 0 <= self._current_index < count:
                nid = notifications[self._current_index].id
                self._manager.remove(nid)
                if self._current_index >= self._manager.count():
                    self._current_index = 0
                self.log("Dismissed notification: %s", nid)
        else:
            self.log("Unknown command: %s", command, level="WARNING")
            return

        self._publish_state()

    # ------------------------------------------------------------------
    # Sensor state publishing
    # ------------------------------------------------------------------

    def _publish_state(self) -> None:
        """Publish current state to the virtual sensor."""
        notifications = self._manager.active_notifications()
        count = len(notifications)

        # Clamp index
        if count == 0:
            self._current_index = 0
        elif self._current_index >= count:
            self._current_index = count - 1

        # Build notification list with cache-bust URLs
        cache_bust = int(time.time())
        notif_list = []
        for n in notifications:
            notif_list.append({
                "id": n.id,
                "text": n.text,
                "image_url": f"{n.local_url}?t={cache_bust}",
                "class": n.notification_class,
                "priority": n.priority,
                "source_id": n.source_id,
            })

        state_text = f"{count} notification{'s' if count != 1 else ''}"

        attrs: dict[str, Any] = {
            "notifications": notif_list,
            "active_index": self._current_index,
            "paused": self._paused,
            "carousel_interval_s": self._carousel_interval_s,
        }

        # Add placeholder info when no notifications
        if count == 0 and self._placeholder_url:
            attrs["placeholder_url"] = f"{self._placeholder_url}?t={cache_bust}"

        self.set_state(self.SENSOR_ENTITY, state=state_text, attributes=attrs)
