"""Dashboard Notify — AI-generated notification carousel for wall displays."""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta
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
from providers.secrets import resolve_arg_secret


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _next_boundary_seconds(hour: int, minute: int, now: float) -> float:
    """Return seconds from now until the next occurrence of HH:MM (today or tomorrow)."""
    dt_now = datetime.fromtimestamp(now)
    candidate = dt_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= dt_now:
        candidate += timedelta(days=1)
    return (candidate - dt_now).total_seconds()


def _seconds_until_next_matching_day(
    hour: int, minute: int, days: list[str], now: float
) -> float:
    """Return seconds until the next HH:MM on one of the listed days."""
    dt_now = datetime.fromtimestamp(now)
    day_names = [d.strip().lower() for d in days]
    for offset in range(8):
        candidate = (dt_now + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if offset == 0 and candidate <= dt_now:
            continue
        day_name = candidate.strftime("%a").lower()
        if day_name in day_names:
            return (candidate - dt_now).total_seconds()
    # Fallback: 7 days
    return 7 * 86400.0


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
            resolve_arg_secret(cfg, "media_fs_root", default="/media")
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
        self._pause_auto_resume_s: float = max(
            0.0, _safe_float(cfg.get("pause_auto_resume_s"), 600.0)
        )
        self._notification_text_target_chars: int = max(
            1, _safe_int(cfg.get("notification_text_target_chars"), 180)
        )
        self._notification_text_truncate_suffix: str = str(
            cfg.get("notification_text_truncate_suffix", "...")
        )

        # HA credentials for provisioning
        self._ha_url: str = str(resolve_arg_secret(cfg, "ha_url", default=""))
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
        self._pause_auto_resume_handle: Optional[Any] = None
        self._pause_auto_resume_deadline: Optional[float] = None
        self._placeholder_generated_at: float = 0.0
        self._placeholder_url: str = ""
        self._placeholder_path: str = ""
        self._active_generations: set[str] = set()

        # Schedule handles: {nid: {"start": handle_or_None, "end": handle_or_None}}
        self._schedule_handles: dict[str, dict[str, Any]] = {}

        # Expiry handles: {nid: handle}
        self._expiry_handles: dict[str, Any] = {}

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

        # Schedule one-time startup reconciliation (after a short delay to let AD settle)
        self.run_in(self._startup_reconcile, 5)

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

    def _format_notification_text(self, text: Any) -> str:
        """Normalize notification text to a fixed display width.

        This keeps the wall-display card height stable by ensuring each
        notification body is exactly `notification_text_target_chars` long.
        """
        normalized = " ".join(str(text or "").split())
        target = self._notification_text_target_chars
        suffix = self._notification_text_truncate_suffix

        if len(normalized) > target:
            if target <= len(suffix):
                return suffix[:target]
            return normalized[: target - len(suffix)] + suffix

        return normalized.ljust(target)

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
    # Startup reconciliation — one-time, replaces the old _tick poller
    # ------------------------------------------------------------------

    def _startup_reconcile(self, kwargs: Any) -> None:
        """One-time startup reconciliation.

        - Evaluates each configured notification against current time and
          starts generation for already-active schedules.
        - Backfills recent detection-summary bundles still within TTL.
        - Installs the placeholder if there are no active notifications.
        - Schedules per-config start/end boundary timers going forward.
        """
        now = time.time()
        self.log("Startup reconciliation running (now=%d)", int(now))

        # Evaluate scheduled notifications
        for config in self._notification_configs:
            nid = config.get("id", "")
            if not nid:
                continue
            if self._is_schedule_active(config.get("schedule"), now):
                if not self._manager.has(nid) and nid not in self._active_generations:
                    self.log("Startup: schedule '%s' is active, enqueuing generation", nid)
                    self._request_notification_generation(config, now)
            # Schedule boundary timers for all configs
            self._schedule_next_boundaries(config, now)

        # Backfill detection-summary bundles
        if _as_bool(self._detection_hook.get("enabled"), False):
            self._backfill_detection_bundles(now)

        # Install placeholder if nothing active yet
        if self._manager.count() == 0 and not self._active_generations:
            self._request_placeholder_generation(now)

        self._publish_state()
        self.log("Startup reconciliation complete")

    def _backfill_detection_bundles(self, now: float) -> None:
        """Scan on-disk detection-summary tree for runs still within TTL.

        Walks all ``runs/`` directories under ``<media_fs_root>/detection-summary/``
        and reads ``bundle_key`` from each ``summary.json`` to match against the
        configured ``bundle_keys``.  This avoids any assumed mapping between
        ``bundle_key`` and the filesystem path (which is controlled by each
        entrance's ``snapshot_ha_dir``).
        """
        allowed_keys = set(self._detection_hook.get("bundle_keys", []))
        ttl_s = int(self._detection_hook.get("ttl_s", 7200))
        max_per_key = int(self._detection_hook.get("backfill_max_per_key", 3))

        ds_root = os.path.join(self._media_fs_root, "detection-summary")
        if not os.path.isdir(ds_root):
            return

        # Collect all eligible (bundle_key, created_at, run_dir) tuples
        candidates: dict[str, list[tuple[float, str, str]]] = {}  # key -> [(created, run_id, run_dir)]
        for dirpath, dirnames, filenames in os.walk(ds_root):
            if "summary.json" not in filenames or "generated.png" not in filenames:
                continue
            summary_path = os.path.join(dirpath, "summary.json")
            try:
                with open(summary_path, "r") as f:
                    summary_data = json.load(f)
                summary_obj = summary_data.get("summary", {})
                if not isinstance(summary_obj, dict):
                    continue
                bundle_key = summary_obj.get("bundle_key", "")
                if bundle_key not in allowed_keys:
                    continue
                created_at = float(summary_obj.get("created_at_epoch", 0))
            except Exception:
                continue
            if created_at + ttl_s <= now:
                continue
            run_id = summary_obj.get("run_id", os.path.basename(dirpath))
            candidates.setdefault(bundle_key, []).append((created_at, run_id, dirpath))

        for bundle_key, runs in candidates.items():
            # Most recent first, cap per key
            runs.sort(key=lambda x: x[0], reverse=True)
            for created_at, run_id, run_dir in runs[:max_per_key]:
                nid = f"detection_{bundle_key}_{run_id}"
                if self._manager.has(nid):
                    continue

                generated_image = os.path.join(run_dir, "generated.png")
                filename = f"{nid}.png"
                dest_path = os.path.join(self._media_dir, "staged", filename)
                try:
                    shutil.copy2(generated_image, dest_path)
                except Exception as e:
                    self.log(
                        "Backfill: failed to copy %s: %s", generated_image, e,
                        level="WARNING"
                    )
                    continue

                expires_at = created_at + ttl_s
                local_url = f"/local/{self._www_subdir}/{filename}"
                try:
                    with open(os.path.join(run_dir, "summary.json"), "r") as f:
                        summary_data = json.load(f)
                    summary_obj = summary_data.get("summary", {})
                    if isinstance(summary_obj, dict):
                        summary_text = self._format_notification_text(
                            summary_obj.get("run_text")
                            or summary_obj.get("text")
                            or "Detection event"
                        )
                    else:
                        summary_text = self._format_notification_text(summary_obj)
                except Exception:
                    summary_text = self._format_notification_text("Detection event")
                self.log(
                    "Backfill: %s/%s — %s (expires in %ds)",
                    bundle_key, run_id, summary_text, int(expires_at - now),
                )

                notification = Notification(
                    id=nid,
                    notification_class="PreexistingImage",
                    text=summary_text,
                    image_path=dest_path,
                    local_url=local_url,
                    created_at=created_at,
                    expires_at=expires_at,
                    priority=priority_for_class("PreexistingImage"),
                    source_id=bundle_key,
                )
                self._manager.add(notification)
                self._schedule_expiry(nid, expires_at, now)
                self.log(
                    "Backfill: added detection notification %s (expires in %.0fs)",
                    nid, expires_at - now
                )

        if self._manager.count() > 0:
            self.call_service("shell_command/" + self._stage_shell_command)

    # ------------------------------------------------------------------
    # Schedule boundary timers
    # ------------------------------------------------------------------

    def _schedule_next_boundaries(self, config: dict[str, Any], now: float) -> None:
        """Schedule the next start and end boundary timers for a config."""
        nid = config.get("id", "")
        if not nid:
            return
        schedule = config.get("schedule")
        if not schedule:
            return

        start_str = str(schedule.get("start", "00:00"))
        end_str = str(schedule.get("end", "23:59"))
        days = schedule.get("days")

        start_parts = start_str.split(":")
        end_parts = end_str.split(":")
        start_h, start_m = int(start_parts[0]), int(start_parts[1])
        end_h, end_m = int(end_parts[0]), int(end_parts[1])

        if nid not in self._schedule_handles:
            self._schedule_handles[nid] = {"start": None, "end": None}

        # Cancel existing handles
        for key in ("start", "end"):
            h = self._schedule_handles[nid].get(key)
            if h is not None:
                try:
                    self.cancel_timer(h)
                except Exception:
                    pass
            self._schedule_handles[nid][key] = None

        # Schedule next start boundary
        if days:
            delay_start = _seconds_until_next_matching_day(start_h, start_m, days, now)
        else:
            delay_start = _next_boundary_seconds(start_h, start_m, now)

        handle_start = self.run_in(
            self._on_schedule_start,
            delay_start,
            nid=nid,
            config=config,
        )
        self._schedule_handles[nid]["start"] = handle_start
        self.log(
            "Schedule '%s': next start in %.0fs (at %s)",
            nid, delay_start, start_str
        )

        # Schedule next end boundary
        if days:
            delay_end = _seconds_until_next_matching_day(end_h, end_m, days, now)
        else:
            delay_end = _next_boundary_seconds(end_h, end_m, now)

        handle_end = self.run_in(
            self._on_schedule_end,
            delay_end,
            nid=nid,
            config=config,
        )
        self._schedule_handles[nid]["end"] = handle_end
        self.log(
            "Schedule '%s': next end in %.0fs (at %s)",
            nid, delay_end, end_str
        )

    def _on_schedule_start(self, kwargs: Any) -> None:
        """Fired when a schedule's start boundary arrives."""
        nid = kwargs.get("nid", "")
        config = kwargs.get("config", {})
        now = time.time()
        self.log("Schedule start fired for '%s'", nid)

        if not self._manager.has(nid) and nid not in self._active_generations:
            self._request_notification_generation(config, now)

        # Schedule next start (next occurrence after this one)
        schedule = config.get("schedule", {})
        days = schedule.get("days")
        start_str = str(schedule.get("start", "00:00"))
        start_parts = start_str.split(":")
        start_h, start_m = int(start_parts[0]), int(start_parts[1])
        if days:
            delay = _seconds_until_next_matching_day(start_h, start_m, days, now)
        else:
            delay = _next_boundary_seconds(start_h, start_m, now)
        handle = self.run_in(self._on_schedule_start, delay, nid=nid, config=config)
        self._schedule_handles.setdefault(nid, {})["start"] = handle

    def _on_schedule_end(self, kwargs: Any) -> None:
        """Fired when a schedule's end boundary arrives."""
        nid = kwargs.get("nid", "")
        config = kwargs.get("config", {})
        now = time.time()
        self.log("Schedule end fired for '%s'", nid)

        # Cancel any pending expiry timer — we're removing it explicitly now
        self._cancel_expiry(nid)
        if self._manager.has(nid):
            self._manager.remove(nid)
            self.log("Schedule '%s' expired at end boundary, removed", nid)
            self._sync_staged_dir()
            self._publish_state()

        # Transition to placeholder if empty
        self._maybe_request_placeholder(now)

        # Schedule next end (next occurrence after this one)
        schedule = config.get("schedule", {})
        days = schedule.get("days")
        end_str = str(schedule.get("end", "23:59"))
        end_parts = end_str.split(":")
        end_h, end_m = int(end_parts[0]), int(end_parts[1])
        if days:
            delay = _seconds_until_next_matching_day(end_h, end_m, days, now)
        else:
            delay = _next_boundary_seconds(end_h, end_m, now)
        handle = self.run_in(self._on_schedule_end, delay, nid=nid, config=config)
        self._schedule_handles.setdefault(nid, {})["end"] = handle

    # ------------------------------------------------------------------
    # Expiry timers
    # ------------------------------------------------------------------

    def _schedule_expiry(self, nid: str, expires_at: float, now: float) -> None:
        """Schedule a one-shot expiry callback for a notification."""
        self._cancel_expiry(nid)
        delay = max(0.0, expires_at - now)
        handle = self.run_in(self._on_notification_expired, delay, nid=nid)
        self._expiry_handles[nid] = handle
        self.log("Expiry timer set for '%s' in %.0fs", nid, delay)

    def _cancel_expiry(self, nid: str) -> None:
        """Cancel any outstanding expiry timer for a notification."""
        h = self._expiry_handles.pop(nid, None)
        if h is not None:
            try:
                self.cancel_timer(h)
            except Exception:
                pass

    def _on_notification_expired(self, kwargs: Any) -> None:
        """Fired when a notification's TTL expires."""
        nid = kwargs.get("nid", "")
        if not self._manager.has(nid):
            return
        self._manager.remove(nid)
        self.log("Notification '%s' expired, removed", nid)
        self._sync_staged_dir()
        self._publish_state()
        self._maybe_request_placeholder(time.time())

    def _maybe_request_placeholder(self, now: float) -> None:
        """Request placeholder generation if the manager is now empty."""
        if self._manager.count() == 0 and not self._active_generations:
            self._request_placeholder_generation(now)

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
            self.call_service("shell_command/" + self._stage_shell_command)

    # ------------------------------------------------------------------
    # Image generation — two-phase: request on AD thread, work off-thread
    # ------------------------------------------------------------------

    def _start_generation_thread(self, job: dict[str, Any]) -> None:
        """Start a background worker thread for image generation.

        The worker performs provider API calls and filesystem work only.
        It schedules a completion callback back on the AppDaemon thread via run_in.
        """
        job_id = job["job_id"]
        kind = job["kind"]
        thread_name = f"dashboard_notify_gen_{job_id[:16]}"
        self.log("Starting generation thread '%s' (kind=%s)", thread_name, kind)

        def _worker():
            if kind == "notification":
                result = self._generate_notification_background(job)
                self.run_in(self._complete_notification_generation, 0, result=result)
            elif kind == "placeholder":
                result = self._generate_placeholder_background(job)
                self.run_in(self._complete_placeholder_generation, 0, result=result)
            else:
                self.log("Unknown generation kind: %s", kind, level="WARNING")

        t = threading.Thread(target=_worker, name=thread_name)
        t.daemon = True
        t.start()

    # -- Scheduled notification generation --

    def _request_notification_generation(
        self, config: dict[str, Any], now: float
    ) -> None:
        """Reserve a generation slot and start the background worker (AppDaemon thread)."""
        if not self._image_provider:
            self.log("No image provider configured, skipping generation")
            return

        nid = config["id"]
        if nid in self._active_generations:
            return

        self._active_generations.add(nid)

        notification_class = config.get("class", "BasicTextImage")
        text = self._format_notification_text(config.get("text", ""))
        prompt_hint = config.get("prompt_hint", "")
        ttl_s = int(config.get("ttl_s", self._default_ttl_s))
        timestamp = int(now)
        filename = f"{nid}_{timestamp}.png"

        job: dict[str, Any] = {
            "job_id": f"{nid}_{timestamp}",
            "kind": "notification",
            "nid": nid,
            "notification_class": notification_class,
            "text": text,
            "prompt_hint": prompt_hint,
            "ttl_s": ttl_s,
            "now": now,
            "filename": filename,
            "output_path": os.path.join(self._media_dir, "generated", filename),
            "staged_path": os.path.join(self._media_dir, "staged", filename),
            "local_url": f"/local/{self._www_subdir}/{filename}",
            "www_subdir": self._www_subdir,
        }

        self.log("Requesting notification generation for '%s' (job_id=%s)", nid, job["job_id"])
        self._start_generation_thread(job)

    def _generate_notification_background(self, job: dict[str, Any]) -> dict[str, Any]:
        """Worker: build prompt, call provider, write files. No HA calls allowed here."""
        nid = job["nid"]
        result: dict[str, Any] = {
            "job_id": job["job_id"],
            "kind": "notification",
            "nid": nid,
            "success": False,
            "error": None,
        }
        try:
            prompt = build_notification_prompt(
                notification_text=job["text"],
                prompt_hint=job["prompt_hint"],
                image_class=job["notification_class"],
            )

            self.log(
                "Generation thread: calling image provider for notification '%s'", nid
            )
            provider_result = self._image_provider.edit_image(
                input_image_paths=[],
                prompt=prompt,
                output_image_path=job["output_path"],
            )
            self.log(
                "Generation thread: notification '%s' API complete model=%s elapsed_s=%s",
                nid,
                provider_result.get("model", "?"),
                provider_result.get("elapsed_s", "?"),
            )

            shutil.copy2(job["output_path"], job["staged_path"])
            self.log("Generation thread: staged notification '%s' → %s", nid, job["staged_path"])

            result.update({
                "success": True,
                "notification_class": job["notification_class"],
                "text": job["text"],
                "output_path": job["output_path"],
                "staged_path": job["staged_path"],
                "local_url": job["local_url"],
                "now": job["now"],
                "ttl_s": job["ttl_s"],
            })
        except Exception as e:
            result["error"] = str(e)
            self.log("Generation thread: error for notification '%s': %s", nid, e, level="WARNING")
        return result

    def _complete_notification_generation(self, kwargs: Any) -> None:
        """AppDaemon completion callback for notification generation."""
        result = kwargs.get("result", {})
        nid = result.get("nid", "")

        # Always release the generation lock
        self._active_generations.discard(nid)

        if not result.get("success"):
            self.log(
                "Notification generation failed for '%s': %s",
                nid, result.get("error"), level="WARNING"
            )
            return

        # Stale-job guard: if already present, skip
        if self._manager.has(nid):
            self.log("Notification '%s' already exists (stale completion), skipping", nid)
            return

        now = time.time()
        created_at = result["now"]
        ttl_s = result["ttl_s"]
        expires_at = created_at + ttl_s

        notification = Notification(
            id=nid,
            notification_class=result["notification_class"],
            text=result["text"],
            image_path=result["output_path"],
            local_url=result["local_url"],
            created_at=created_at,
            expires_at=expires_at,
            priority=priority_for_class(result["notification_class"]),
            source_id=nid,
        )
        self._manager.add(notification)
        self._schedule_expiry(nid, expires_at, now)

        # Stage to www
        self.call_service("shell_command/" + self._stage_shell_command)

        self.log("Notification '%s' generated and added (ttl=%ds)", nid, ttl_s)
        self._publish_state()

    # -- Placeholder generation --

    def _request_placeholder_generation(self, now: float) -> None:
        """Reserve placeholder slot and start the background worker (AppDaemon thread)."""
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

        timestamp = int(now)
        filename = f"no_notifications_{timestamp}.png"
        job: dict[str, Any] = {
            "job_id": f"placeholder_{timestamp}",
            "kind": "placeholder",
            "now": now,
            "filename": filename,
            "output_path": os.path.join(self._media_dir, "generated", filename),
            "staged_path": os.path.join(self._media_dir, "staged", filename),
            "local_url": f"/local/{self._www_subdir}/{filename}",
        }

        self.log("Requesting placeholder generation (job_id=%s)", job["job_id"])
        self._start_generation_thread(job)

    def _generate_placeholder_background(self, job: dict[str, Any]) -> dict[str, Any]:
        """Worker: build prompt, call provider, write files. No HA calls allowed here."""
        result: dict[str, Any] = {
            "job_id": job["job_id"],
            "kind": "placeholder",
            "success": False,
            "error": None,
        }
        try:
            prompt = build_placeholder_prompt()

            self.log("Generation thread: calling image provider for placeholder")
            provider_result = self._image_provider.edit_image(
                input_image_paths=[],
                prompt=prompt,
                output_image_path=job["output_path"],
            )
            self.log(
                "Generation thread: placeholder API complete model=%s elapsed_s=%s",
                provider_result.get("model", "?"),
                provider_result.get("elapsed_s", "?"),
            )

            shutil.copy2(job["output_path"], job["staged_path"])
            self.log("Generation thread: staged placeholder → %s", job["staged_path"])

            result.update({
                "success": True,
                "output_path": job["output_path"],
                "staged_path": job["staged_path"],
                "local_url": job["local_url"],
                "now": job["now"],
            })
        except Exception as e:
            result["error"] = str(e)
            self.log("Generation thread: error for placeholder: %s", e, level="WARNING")
        return result

    def _complete_placeholder_generation(self, kwargs: Any) -> None:
        """AppDaemon completion callback for placeholder generation."""
        result = kwargs.get("result", {})

        # Always release the generation lock
        self._active_generations.discard("placeholder")

        if not result.get("success"):
            self.log(
                "Placeholder generation failed: %s",
                result.get("error"), level="WARNING"
            )
            return

        # Stale-job guard: if real notifications appeared while we were generating, discard
        if self._manager.count() > 0:
            self.log("Placeholder completed but notifications now exist, discarding placeholder")
            return

        self._placeholder_url = result["local_url"]
        self._placeholder_path = result["staged_path"]
        self._placeholder_generated_at = result["now"]

        # Stage to www
        self.call_service("shell_command/" + self._stage_shell_command)

        self.log("Placeholder image generated and staged")
        self._publish_state()

        # Schedule next placeholder refresh
        now = time.time()
        next_refresh_delay = self._no_notification_refresh_s - (now - self._placeholder_generated_at)
        next_refresh_delay = max(60.0, next_refresh_delay)
        self.run_in(self._on_placeholder_refresh, next_refresh_delay)
        self.log("Placeholder refresh scheduled in %.0fs", next_refresh_delay)

    def _on_placeholder_refresh(self, kwargs: Any) -> None:
        """Fire placeholder regeneration if we're still in idle state."""
        now = time.time()
        if self._manager.count() == 0:
            self._request_placeholder_generation(now)

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

        # Find the generated image on disk.  The directory structure varies per
        # entrance (controlled by snapshot_ha_dir) so we search by run_id (UUID).
        ds_root = Path(self._media_fs_root) / "detection-summary"
        matches = list(ds_root.glob(f"**/runs/{run_id}/generated.png"))
        if not matches:
            self.log(
                "Detection hook: no generated image found for run_id=%s under %s",
                run_id, ds_root,
            )
            return
        generated_image = str(matches[0])

        nid = f"detection_{bundle_key}_{run_id}"
        ttl_s = int(self._detection_hook.get("ttl_s", 7200))
        now = time.time()

        # Deduplicate — may have been backfilled on startup
        if self._manager.has(nid):
            self.log("Detection notification '%s' already exists, skipping duplicate", nid)
            return

        # Copy to our media dir
        filename = f"{nid}.png"
        dest_path = os.path.join(self._media_dir, "staged", filename)
        try:
            shutil.copy2(generated_image, dest_path)
            self.call_service("shell_command/" + self._stage_shell_command)
        except Exception as e:
            self.log("Detection hook copy failed: %s", e, level="WARNING")
            return

        expires_at = now + ttl_s
        local_url = f"/local/{self._www_subdir}/{filename}"
        notification = Notification(
            id=nid,
            notification_class="PreexistingImage",
            text=self._format_notification_text(summary),
            image_path=dest_path,
            local_url=local_url,
            created_at=now,
            expires_at=expires_at,
            priority=priority_for_class("PreexistingImage"),
            source_id=bundle_key,
        )
        self._manager.add(notification)
        self._schedule_expiry(nid, expires_at, now)
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
            self._set_paused(not self._paused, reason="toggle_pause")
        elif command == "dismiss":
            if count > 0 and 0 <= self._current_index < count:
                nid = notifications[self._current_index].id
                self._cancel_expiry(nid)
                self._manager.remove(nid)
                if self._current_index >= self._manager.count():
                    self._current_index = 0
                self.log("Dismissed notification: %s", nid)
                self._maybe_request_placeholder(time.time())
        else:
            self.log("Unknown command: %s", command, level="WARNING")
            return

        self._publish_state()

    def _set_paused(self, paused: bool, *, reason: str) -> None:
        self._paused = paused
        if paused:
            self._schedule_pause_auto_resume(reason=reason)
        else:
            self._cancel_pause_auto_resume()

    def _cancel_pause_auto_resume(self) -> None:
        handle = self._pause_auto_resume_handle
        self._pause_auto_resume_handle = None
        self._pause_auto_resume_deadline = None
        if handle is None:
            return
        try:
            if self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass

    def _schedule_pause_auto_resume(self, *, reason: str) -> None:
        self._cancel_pause_auto_resume()
        if self._pause_auto_resume_s <= 0:
            return
        self._pause_auto_resume_deadline = time.time() + self._pause_auto_resume_s
        self._pause_auto_resume_handle = self.run_in(
            self._handle_pause_auto_resume,
            self._pause_auto_resume_s,
        )
        self.log(
            "DashboardNotify auto-resume scheduled in %.1fs reason=%s",
            self._pause_auto_resume_s,
            reason,
        )

    def _handle_pause_auto_resume(self, kwargs: Any) -> None:
        self._pause_auto_resume_handle = None
        self._pause_auto_resume_deadline = None
        if not self._paused:
            return
        self._set_paused(False, reason="auto_resume")
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

        # Build notification list with stable per-file cache keys.
        # Using time.time() here forces every client to re-fetch the bitmap on
        # every state update (pause, swipe, dot change), which is especially bad
        # for the iOS app WebView.
        notif_list = []
        for n in notifications:
            notif_list.append({
                "id": n.id,
                "text": n.text,
                "image_url": self._versioned_media_url(n.local_url, n.image_path),
                "class": n.notification_class,
                "priority": n.priority,
                "source_id": n.source_id,
                "created_at": n.created_at,
                "expires_at": n.expires_at,
            })

        state_text = f"{count} notification{'s' if count != 1 else ''}"

        attrs: dict[str, Any] = {
            "notifications": notif_list,
            "active_index": self._current_index,
            "paused": self._paused,
            "carousel_interval_s": self._carousel_interval_s,
            "pause_auto_resume_s": self._pause_auto_resume_s,
            "pause_resume_at": self._pause_auto_resume_deadline,
        }

        # Add placeholder info when no notifications
        if count == 0 and self._placeholder_url:
            attrs["placeholder_url"] = self._versioned_media_url(
                self._placeholder_url,
                self._placeholder_path,
            )

        self.set_state(self.SENSOR_ENTITY, state=state_text, attributes=attrs)

    def _versioned_media_url(self, local_url: str, file_path: str | None) -> str:
        """Return a cache-stable URL that only changes when the file changes."""
        if not local_url:
            return ""
        if not file_path:
            return local_url
        try:
            version = int(os.path.getmtime(file_path))
        except OSError:
            return local_url
        return f"{local_url}?v={version}"

    # ------------------------------------------------------------------
    # Schedule check utility (kept for tests and schedule active check)
    # ------------------------------------------------------------------

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
