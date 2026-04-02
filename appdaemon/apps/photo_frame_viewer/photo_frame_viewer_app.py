from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

# AppDaemon only adds `appdaemon/apps` to sys.path. Our shared libraries
# live at `appdaemon/providers`, so add the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from photo_frame_viewer.gen_helpers import (
    basename,
    build_label_maps,
    compute_fingerprint,
    gen_path_to_local_url,
    parse_gen_id_from_url,
    source_paths_to_gen_paths,
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


class PhotoFrameViewerApp(hass.Hass):
    """Photo frame viewer with generation-based atomic batch swap.

    Discovers available images by scanning ``source_dir`` directly via
    ``os.listdir()`` (AppDaemon has filesystem access to ``/media/``),
    copies them into a versioned "generation" directory under
    ``/config/www/photo-frame/live/<gen>/`` via HA ``shell_command``
    services, and keeps a dashboard ``input_select`` + virtual sensor
    in sync.

    State (pause, interval, image URL, cache-bust) is published on a
    virtual sensor ``sensor.<prefix>_photo_frame_status``.  An
    ``input_select.<prefix>_photo_frame_image`` helper is auto-provisioned
    for the image picker.  A relay script
    ``script.<prefix>_photo_frame_relay`` is provisioned to allow the
    Lovelace card to send commands without admin privileges.
    """

    IMAGE_EXTENSIONS: frozenset[str] = frozenset(
        {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    )

    DEFAULTS: dict[str, Any] = {
        "source_dir": "/media/immich-photos",
        "ha_local_url_base": "/local/photo-frame/live",
        "stage_shell_command": "photo_frame_stage_gen",
        "cleanup_shell_command": "photo_frame_cleanup_gen",
        "source_poll_interval_s": 30,
        "stage_settle_delay_s": 3,
        "fallback_image_path": "/config/www/immich-album/no-image.jpg",
        "options_max": 100,
        "refresh_options_every_s": 60,
        "auto_cycle": True,
        "reset_timer_on_manual_nav": True,
        "default_interval_s": 10,
        "pause_auto_resume_s": 600,
    }

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        cfg = {**self.DEFAULTS, **(self.args or {})}

        self.source_dir: str = str(cfg["source_dir"])
        self.ha_source_dir: str = str(cfg.get("ha_source_dir") or cfg["source_dir"])
        self.ha_local_url_base: str = str(cfg["ha_local_url_base"]).rstrip("/")
        self.stage_shell_command: str = str(cfg["stage_shell_command"])
        self.cleanup_shell_command: str = str(cfg["cleanup_shell_command"])
        self.source_poll_interval_s: float = max(5.0, float(cfg["source_poll_interval_s"]))
        self.stage_settle_delay_s: float = max(1.0, float(cfg["stage_settle_delay_s"]))
        # TODO: fallback_image_path currently points into source_dir (immich-photos), which
        # immich_fetcher clears on every run. The image will never exist; we show broken image
        # instead of a fallback. Move to a dir the fetcher doesn't touch (e.g. state_dir) or
        # have immich_fetcher preserve it.
        self.fallback_image_path: str = str(cfg["fallback_image_path"])
        self.options_max: int = max(1, int(cfg["options_max"]))
        self.refresh_options_every_s: float = max(10.0, float(cfg["refresh_options_every_s"]))
        self.auto_cycle: bool = _as_bool(cfg["auto_cycle"], True)
        self.reset_timer_on_manual_nav: bool = _as_bool(cfg["reset_timer_on_manual_nav"], True)
        self.pause_auto_resume_s: float = max(
            0.0, _safe_float(cfg.get("pause_auto_resume_s"), 600.0)
        )

        # Entity prefix — derives entity IDs for all provisioned entities.
        self._entity_prefix: str = self._derive_entity_prefix(cfg)

        # State persistence directory
        state_dir_default = f"/media/photo-frame-viewer/{self._entity_prefix}"
        self._state_dir: str = str(cfg.get("state_dir") or state_dir_default)
        self._state_file: str = os.path.join(self._state_dir, "state.json")

        # Derived entity IDs
        self.picker_entity_id: str = str(
            cfg.get("picker_entity_id")
            or f"input_select.{self._entity_prefix}_photo_frame_image"
        )
        self._sensor_entity_id: str = f"sensor.{self._entity_prefix}_photo_frame_status"
        self._relay_script_id: str = f"{self._entity_prefix}_photo_frame_relay"
        self._command_event: str = f"{self._entity_prefix}_photo_frame_command"

        # Internal state (replaces input_boolean and input_number helpers)
        self._paused: bool = False
        self._interval: float = self._load_interval(float(cfg.get("default_interval_s", 10)))
        self.pause_auto_resume_s = self._load_pause_auto_resume(self.pause_auto_resume_s)
        self._cache_bust: str = "0"
        self._pause_auto_resume_handle: Optional[Any] = None
        self._pause_auto_resume_deadline: Optional[float] = None

        # Filter name of the batch currently being displayed.  Updated only
        # when the viewer *actually* swaps to a new generation, not when the
        # fetcher finishes downloading (which races ahead of the viewer).
        self._displaying_filter_name: str = ""

        # Slideshow timer
        self._timer_handle: Optional[Any] = None
        self._periodic_handle: Optional[Any] = None
        self._poll_handle: Optional[Any] = None

        # Label maps (current live generation)
        self._label_to_path: dict[str, str] = {}
        self._path_to_label: dict[str, str] = {}
        self._last_published_local_url: Optional[str] = None
        self._last_published_source_path: Optional[str] = None

        # Generation state
        self._current_gen_id: Optional[str] = None
        self._current_fingerprint: Optional[str] = None
        self._next_gen_counter: int = 1
        self._staging_in_progress: bool = False

        # Pending generation (staged but not yet displayed)
        self._pending_gen_id: Optional[str] = None
        self._pending_labels: list[str] = []
        self._pending_label_to_path: dict[str, str] = {}
        self._pending_path_to_label: dict[str, str] = {}
        self._pending_fingerprint: Optional[str] = None
        self._pending_filter_name: str = ""

        # Filter name received from the most recent batch_ready event,
        # consumed when the staging flow captures it for the pending gen.
        self._staged_filter_name: str = ""

        # Label we expect back from the next HA state-change echo after a
        # programmatic picker update.  Cleared when the echo arrives so that
        # we never confuse our own call_service round-trip with a real user
        # navigation.  This replaces the old time-based debounce which was
        # too fragile — HA can take >1 s to propagate state changes under
        # load, causing the debounce window to expire and every auto-advance
        # to be mis-classified as "manual nav".
        self._programmatic_target: Optional[str] = None

        self.log(
            f"PhotoFrameViewerApp init "
            f"prefix={self._entity_prefix} "
            f"source_dir={self.source_dir} "
            f"ha_source_dir={self.ha_source_dir} "
            f"ha_local_url_base={self.ha_local_url_base} "
            f"picker={self.picker_entity_id} "
            f"sensor={self._sensor_entity_id}",
            level="INFO",
        )

        # Recover generation counter from previously published virtual sensor
        self._recover_gen_from_sensor()

        # Watch picker selection (manual nav or auto-advance echo).
        self.listen_state(self._on_picker_change, self.picker_entity_id)

        # Poll source directory for changes periodically.
        self._poll_handle = self.run_every(
            self._poll_for_changes_cb,
            self.datetime(),
            self.source_poll_interval_s,
        )

        # React to fetcher batch-ready events for faster detection.
        self.listen_event(self._on_batch_ready, "immich_fetcher_batch_ready")

        # Relay command listener — card sends commands via script that fires this event.
        self.listen_event(self._on_command, self._command_event)

        # Async startup: provision entities, then publish initial sensor state.
        self.run_in(self._on_startup, 0)

        # Kick off: always stage on startup to guarantee files exist on HA.
        # If we recovered a gen, populate the picker first so the dashboard
        # has *something* while the new gen stages.
        if self._current_gen_id is not None:
            self._refresh_picker_from_current_gen(reason="init")
            self._publish_selected_local_url(self._picker_value(), reason="init")
            self._current_fingerprint = None  # force re-stage
        self._poll_for_changes(reason="init")

        self._sync_timer(reason="init")

    # ------------------------------------------------------------------
    # Entity prefix derivation
    # ------------------------------------------------------------------

    def _derive_entity_prefix(self, cfg: dict) -> str:
        """Derive entity prefix from config or app instance name.

        Priority:
          1. ``entity_prefix`` key in apps.yaml
          2. Suffix after ``photo_frame_viewer_`` in the app instance name
          3. Fallback: ``wall_display``
        """
        explicit = cfg.get("entity_prefix")
        if explicit:
            return str(explicit)

        try:
            app_name = str(self.name) if hasattr(self, "name") else ""
        except Exception:
            app_name = ""

        marker = "photo_frame_viewer_"
        if marker in app_name:
            suffix = app_name[app_name.index(marker) + len(marker):]
            if suffix:
                return suffix

        return "wall_display"

    # ------------------------------------------------------------------
    # Interval persistence
    # ------------------------------------------------------------------

    def _load_runtime_state(self) -> dict[str, Any]:
        """Load persisted runtime settings from state file."""
        try:
            state_path = Path(self._state_file)
            if state_path.is_file():
                data = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            self.log(
                f"PhotoFrameViewerApp: failed to load runtime state: {exc}",
                level="WARNING",
            )
        return {}

    def _load_interval(self, default: float) -> float:
        """Load persisted interval from state file, or return default."""
        data = self._load_runtime_state()
        val = _safe_float(data.get("interval_seconds", default), default)
        if val > 0:
            return max(1.0, val)
        return max(1.0, default)

    def _load_pause_auto_resume(self, default: float) -> float:
        """Load persisted auto-resume seconds from state file, or return default."""
        data = self._load_runtime_state()
        return max(0.0, _safe_float(data.get("pause_auto_resume_s", default), default))

    def _save_runtime_state(self) -> None:
        """Persist runtime settings to state file."""
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            Path(self._state_file).write_text(
                json.dumps({
                    "interval_seconds": self._interval,
                    "pause_auto_resume_s": self.pause_auto_resume_s,
                }),
                encoding="utf-8",
            )
            self.log(
                "PhotoFrameViewerApp: runtime state persisted "
                f"(interval={self._interval}s pause_auto_resume_s={self.pause_auto_resume_s}s)",
                level="DEBUG",
            )
        except Exception as exc:
            self.log(
                f"PhotoFrameViewerApp: failed to save runtime state: {exc}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover_gen_from_sensor(self) -> None:
        """On startup, recover the generation counter from the existing virtual sensor."""
        try:
            state = self.get_state(self._sensor_entity_id, attribute="all")
            if not isinstance(state, dict):
                return
            attrs = state.get("attributes", {})
            raw = str(attrs.get("image_url", "")).strip()
        except Exception:
            return

        if not raw:
            return

        gen = parse_gen_id_from_url(raw, self.ha_local_url_base)
        if gen is not None:
            gen_int = int(gen)
            self._current_gen_id = gen
            self._next_gen_counter = gen_int + 1
            self._last_published_local_url = raw
            self.log(
                f"PhotoFrameViewerApp: recovered gen={gen} from sensor url={raw!r}, "
                f"next_counter={self._next_gen_counter}",
                level="INFO",
            )

    # ------------------------------------------------------------------
    # Startup + provisioning
    # ------------------------------------------------------------------

    def _on_startup(self, kwargs: Any) -> None:
        """Async startup work wrapped in run_in callback."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._provision_entities()
        self._publish_sensor_state()

    async def _provision_entities(self) -> None:
        """Provision relay script and input_select via ha_provisioner."""
        ha_url = str(resolve_arg_secret(self.args, "ha_url", default=""))
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping entity provisioning",
                level="WARNING",
            )
            return

        from providers.ha_provisioner import HAProvisioner

        prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)

        # Provision relay script
        try:
            prefix_title = self._entity_prefix.replace("_", " ").title()
            created = await prov.ensure_script(
                self._relay_script_id,
                {
                    "alias": f"{prefix_title} Photo Frame Relay",
                    "description": "Relays dashboard commands to AppDaemon",
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
                            "event": self._command_event,
                            "event_data": {
                                "command": "{{ command }}",
                                "payload": "{{ payload | default('{}') }}",
                            },
                        }
                    ],
                },
            )
            if created:
                self.log(
                    f"Relay script created: script.{self._relay_script_id}",
                    level="INFO",
                )
            else:
                self.log(
                    f"Relay script already exists: script.{self._relay_script_id}",
                    level="DEBUG",
                )
        except Exception as exc:
            self.log(
                f"PhotoFrameViewerApp: failed to provision relay script: {exc}",
                level="ERROR",
            )

        # Provision input_select picker helper
        try:
            picker_name = (
                f"{self._entity_prefix.replace('_', ' ').title()} Photo Frame Image"
            )
            created = await prov.ensure_helper("input_select", picker_name)
            if created:
                self.log(
                    f"Picker helper created: {self.picker_entity_id}",
                    level="INFO",
                )
            else:
                self.log(
                    f"Picker helper already exists: {self.picker_entity_id}",
                    level="DEBUG",
                )
        except Exception as exc:
            self.log(
                f"PhotoFrameViewerApp: failed to provision picker helper: {exc}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Virtual sensor publishing
    # ------------------------------------------------------------------

    def _publish_sensor_state(self) -> None:
        """Publish the virtual status sensor with all state as attributes."""
        status = "paused" if self._paused else "playing"
        self.set_state(
            self._sensor_entity_id,
            state=status,
            attributes={
                "image_url": self._last_published_local_url or "",
                "cache_bust": self._cache_bust,
                "paused": "true" if self._paused else "false",
                "interval_seconds": self._interval,
                "pause_auto_resume_s": self.pause_auto_resume_s,
                "pause_resume_at": self._pause_auto_resume_deadline,
                "current_gen": self._current_gen_id or "",
                "displaying_filter_name": self._displaying_filter_name,
                "friendly_name": f"{self._entity_prefix.replace('_', ' ').title()} Photo Frame",
            },
        )

    def _version_token_for_path(self, path: str) -> str:
        candidate = str(path or "").strip()
        if not candidate:
            return "0"
        try:
            return str(int(os.path.getmtime(candidate)))
        except Exception:
            return "0"

    # ------------------------------------------------------------------
    # Relay command handler
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Route relay commands from the dashboard card."""
        cmd = data.get("command")
        raw = data.get("payload", "{}")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            self.log(
                f"PhotoFrameViewerApp: invalid command payload: {raw!r}",
                level="WARNING",
            )
            return

        if cmd == "toggle_pause":
            self._handle_toggle_pause()
        elif cmd == "set_interval":
            self._handle_set_interval(payload)
        elif cmd == "set_pause_auto_resume":
            self._handle_set_pause_auto_resume(payload)
        elif cmd == "next":
            self._handle_next()
        elif cmd == "previous":
            self._handle_previous()
        elif cmd == "select":
            self._handle_select(payload)
        else:
            self.log(
                f"PhotoFrameViewerApp: unknown command: {cmd!r}",
                level="WARNING",
            )

    def _handle_toggle_pause(self) -> None:
        self._set_paused(not self._paused, reason="toggle_pause")

    def _set_paused(self, paused: bool, *, reason: str) -> None:
        self._paused = paused
        if paused:
            self._schedule_pause_auto_resume(reason=reason)
        else:
            self._cancel_pause_auto_resume()
        self.log(
            f"PhotoFrameViewerApp: paused={self._paused} reason={reason}",
            level="INFO",
        )
        self._sync_timer(reason=reason)
        self._publish_sensor_state()

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
        timeout = self.pause_auto_resume_s
        if timeout <= 0:
            return
        self._pause_auto_resume_deadline = time.time() + timeout
        self._pause_auto_resume_handle = self.run_in(
            self._handle_pause_auto_resume,
            timeout,
        )
        self.log(
            f"PhotoFrameViewerApp: auto-resume scheduled in {timeout:.1f}s "
            f"reason={reason}",
            level="INFO",
        )

    def _handle_pause_auto_resume(self, kwargs: Any) -> None:
        self._pause_auto_resume_handle = None
        self._pause_auto_resume_deadline = None
        if not self._paused:
            return
        self._set_paused(False, reason="auto_resume")

    def _handle_set_interval(self, payload: dict) -> None:
        raw = payload.get("seconds")
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            self.log(
                f"PhotoFrameViewerApp: set_interval invalid value: {raw!r}",
                level="WARNING",
            )
            return
        if seconds <= 0:
            self.log(
                f"PhotoFrameViewerApp: set_interval non-positive value: {seconds}",
                level="WARNING",
            )
            return
        self._interval = max(1.0, seconds)
        self._save_runtime_state()
        self.log(
            f"PhotoFrameViewerApp: interval={self._interval}s via relay",
            level="INFO",
        )
        self._sync_timer(reason="set_interval")
        self._publish_sensor_state()

    def _handle_set_pause_auto_resume(self, payload: dict) -> None:
        raw = payload.get("seconds", 0)
        seconds = _safe_float(raw, -1.0)
        if seconds < 0:
            self.log(
                f"PhotoFrameViewerApp: set_pause_auto_resume invalid value: {raw!r}",
                level="WARNING",
            )
            return
        self.pause_auto_resume_s = max(0.0, seconds)
        self._save_runtime_state()
        if self._paused:
            self._schedule_pause_auto_resume(reason="set_pause_auto_resume")
        else:
            self._cancel_pause_auto_resume()
        self.log(
            f"PhotoFrameViewerApp: pause_auto_resume_s={self.pause_auto_resume_s}s via relay",
            level="INFO",
        )
        self._publish_sensor_state()

    def _handle_next(self) -> None:
        self.call_service(
            "input_select/select_next",
            entity_id=self.picker_entity_id,
            cycle=True,
        )

    def _handle_previous(self) -> None:
        self.call_service(
            "input_select/select_previous",
            entity_id=self.picker_entity_id,
            cycle=True,
        )

    def _handle_select(self, payload: dict) -> None:
        image = payload.get("image", "")
        if image:
            self.call_service(
                "input_select/select_option",
                entity_id=self.picker_entity_id,
                option=image,
            )

    # ------------------------------------------------------------------
    # Source reading
    # ------------------------------------------------------------------

    def _read_source_file_list(self) -> list[str]:
        """Read the source file list by scanning source_dir on disk.

        AppDaemon has direct filesystem access to /media/ so we can
        use os.listdir() instead of relying on an HA folder sensor.
        """
        try:
            entries = os.listdir(self.source_dir)
        except FileNotFoundError:
            self.log(
                f"PhotoFrameViewerApp: source_dir not found: {self.source_dir}",
                level="WARNING",
            )
            return []
        except OSError as exc:
            self.log(
                f"PhotoFrameViewerApp: error reading source_dir: {exc}",
                level="WARNING",
            )
            return []

        out: list[str] = []
        for name in sorted(entries):
            ext = os.path.splitext(name)[1].lower()
            if ext in self.IMAGE_EXTENSIONS:
                out.append(os.path.join(self.source_dir, name))
        return out[: self.options_max]

    @staticmethod
    def _stat_files(paths: list[str]) -> dict[str, tuple[int, float]]:
        """Return {path: (size_bytes, mtime)} for each path that exists."""
        stats: dict[str, tuple[int, float]] = {}
        for p in paths:
            try:
                st = os.stat(p)
                stats[p] = (st.st_size, st.st_mtime)
            except OSError:
                pass
        return stats

    # ------------------------------------------------------------------
    # Change detection + staging
    # ------------------------------------------------------------------

    def _poll_for_changes(self, *, reason: str) -> None:
        """Check for source changes and trigger staging if needed."""
        source_paths = self._read_source_file_list()
        if not source_paths:
            self.log("PhotoFrameViewerApp: source file list empty, skipping poll", level="DEBUG")
            return

        fp = compute_fingerprint(source_paths, file_stats=self._stat_files(source_paths))
        if fp == self._current_fingerprint and self._current_gen_id is not None:
            return  # No change

        if self._staging_in_progress:
            self.log("PhotoFrameViewerApp: staging already in progress, skipping", level="DEBUG")
            return

        self.log(
            f"PhotoFrameViewerApp: source changed (reason={reason}) "
            f"old_fp={self._current_fingerprint!r} new_fp={fp[:12]}... "
            f"files={len(source_paths)}",
            level="INFO",
        )
        self._stage_new_generation(source_paths, fp)

    def _stage_new_generation(self, source_paths: list[str], fingerprint: str) -> None:
        """Call the staging shell_command and schedule the settle callback."""
        gen_id = str(self._next_gen_counter)
        self._next_gen_counter += 1
        self._staging_in_progress = True

        # If there's an existing pending gen that was never adopted, clean it up
        if self._pending_gen_id is not None:
            old_pending = self._pending_gen_id
            self._clear_pending()
            self.log(
                f"PhotoFrameViewerApp: replacing pending gen={old_pending} with gen={gen_id}",
                level="INFO",
            )
            self._call_cleanup(old_pending, reason="replace_pending")

        self.log(
            f"PhotoFrameViewerApp: staging gen={gen_id} from {self.source_dir} "
            f"({len(source_paths)} files)",
            level="INFO",
        )

        self.call_service(
            f"shell_command/{self.stage_shell_command}",
            source_dir=self.ha_source_dir,
            gen_id=gen_id,
        )

        # Stash staging context for the settle callback
        self._staging_gen_id = gen_id
        self._staging_source_paths = source_paths
        self._staging_fingerprint = fingerprint

        self.run_in(self._on_stage_settled, self.stage_settle_delay_s)

    def _on_stage_settled(self, kwargs: Any) -> None:
        """Called after the staging shell_command has had time to complete."""
        self._staging_in_progress = False

        gen_id = getattr(self, "_staging_gen_id", None)
        source_paths = getattr(self, "_staging_source_paths", [])
        fingerprint = getattr(self, "_staging_fingerprint", None)

        if not gen_id or not source_paths:
            self.log("PhotoFrameViewerApp: stage settled but no staging context", level="WARNING")
            self._staged_filter_name = ""
            return

        # Build gen paths and label maps for the new generation
        gen_paths = source_paths_to_gen_paths(source_paths, gen_id)
        labels, l2p, p2l = build_label_maps(gen_paths, self.options_max)

        self._pending_gen_id = gen_id
        self._pending_labels = labels
        self._pending_label_to_path = l2p
        self._pending_path_to_label = p2l
        self._pending_fingerprint = fingerprint
        self._pending_filter_name = self._staged_filter_name
        self._staged_filter_name = ""

        self.log(
            f"PhotoFrameViewerApp: gen={gen_id} ready as pending "
            f"({len(labels)} labels, filter={self._pending_filter_name!r})",
            level="INFO",
        )

        # Do NOT update picker options here. Changing options while the user
        # is viewing (especially paused) causes HA to reset the selected value,
        # which triggers an unwanted image change. Options are updated only
        # when the image would naturally change (tick advance or manual nav)
        # via _apply_pending_gen().

        # Exception: very first generation -- there's nothing displayed yet.
        if self._current_gen_id is None:
            self.log("PhotoFrameViewerApp: first gen, applying immediately", level="INFO")
            self._apply_pending_gen(reason="first_gen")

    # ------------------------------------------------------------------
    # Picker options management
    # ------------------------------------------------------------------

    def _refresh_picker_from_current_gen(self, *, reason: str) -> None:
        """Rebuild label maps from the current gen using the source file list.

        Used on startup recovery when we have a current gen but need to
        populate the in-memory maps.
        """
        source_paths = self._read_source_file_list()
        if not source_paths:
            source_paths = [self.fallback_image_path]

        gen_paths = source_paths_to_gen_paths(source_paths, self._current_gen_id or "1")
        labels, l2p, p2l = build_label_maps(gen_paths, self.options_max)

        self._label_to_path = l2p
        self._path_to_label = p2l
        self._current_fingerprint = compute_fingerprint(
            source_paths, file_stats=self._stat_files(source_paths)
        )

        existing_opts = self._picker_options()
        if existing_opts != labels:
            self.call_service(
                "input_select/set_options",
                entity_id=self.picker_entity_id,
                options=labels,
            )

    def _refresh_picker_options(self, *, reason: str) -> None:
        """Update the input_select options from the current generation's maps."""
        source_paths = self._read_source_file_list()
        if not source_paths:
            source_paths = [self.fallback_image_path]
        gen_id = self._current_gen_id or "1"
        gen_paths = source_paths_to_gen_paths(source_paths, gen_id)
        labels, label_to_path, path_to_label = build_label_maps(gen_paths, self.options_max)

        current_label = self._picker_value()
        current_path = self._label_to_path.get(current_label)
        current_basename = basename(current_path) if current_path else ""

        desired_label = ""
        if current_basename:
            for lbl, pth in label_to_path.items():
                if basename(pth) == current_basename:
                    desired_label = lbl
                    break
        if not desired_label and current_label in labels:
            desired_label = current_label
        if not desired_label:
            desired_label = labels[0] if labels else ""

        existing_opts = self._picker_options()
        if existing_opts != labels:
            self.log(
                f"PhotoFrameViewerApp: updating picker options "
                f"count={len(labels)} reason={reason}",
                level="INFO",
            )
            self.call_service(
                "input_select/set_options",
                entity_id=self.picker_entity_id,
                options=labels,
            )

        if desired_label and desired_label != current_label:
            self.call_service(
                "input_select/select_option",
                entity_id=self.picker_entity_id,
                option=desired_label,
            )

    # ------------------------------------------------------------------
    # Publish URL + generation swap
    # ------------------------------------------------------------------

    def _apply_pending_gen(self, *, reason: str) -> None:
        """Promote the pending generation to current and update the picker.

        Called at the moment the displayed image would naturally change
        (tick advance, manual nav, or first-gen init) so the user never
        sees an unexpected image swap.
        """
        if self._pending_gen_id is None:
            return

        # Promote the filter name so the card knows which filter is actually
        # being displayed (not the one the fetcher is already fetching next).
        if self._pending_filter_name:
            self._displaying_filter_name = self._pending_filter_name

        pending_labels = self._pending_labels[:]
        self._finalize_pending(reason=reason)

        existing_opts = self._picker_options()
        if existing_opts != pending_labels:
            self.log(
                f"PhotoFrameViewerApp: updating picker options "
                f"count={len(pending_labels)} reason=apply_{reason}",
                level="INFO",
            )
            self.call_service(
                "input_select/set_options",
                entity_id=self.picker_entity_id,
                options=pending_labels,
            )

        if pending_labels:
            first_label = pending_labels[0]
            self._programmatic_target = first_label
            self.call_service(
                "input_select/select_option",
                entity_id=self.picker_entity_id,
                option=first_label,
            )
            self._publish_selected_local_url(first_label, reason=reason)

    def _publish_selected_local_url(self, label: str, *, reason: str) -> None:
        """Publish the ``/local/...`` URL for the selected label via the virtual sensor."""
        label = str(label or "").strip()
        path = (self._label_to_path.get(label) or "").strip()

        if not path:
            path = self.fallback_image_path

        gen_id = self._current_gen_id
        local_url = gen_path_to_local_url(path, self.ha_local_url_base, gen_id or "")

        if self._last_published_local_url == local_url:
            return

        self.log(
            f"PhotoFrameViewerApp: publish url={local_url!r} "
            f"label={label!r} gen={gen_id} reason={reason}",
            level="DEBUG",
        )
        self._last_published_local_url = local_url
        self._last_published_source_path = path
        self._cache_bust = self._version_token_for_path(path)
        self._publish_sensor_state()

    def _finalize_pending(self, *, reason: str) -> None:
        """Promote pending gen to current and clean up the old gen."""
        old_gen = self._current_gen_id
        new_gen = self._pending_gen_id

        self._current_gen_id = new_gen
        self._current_fingerprint = self._pending_fingerprint
        self._label_to_path = self._pending_label_to_path.copy()
        self._path_to_label = self._pending_path_to_label.copy()

        self._clear_pending()

        self.log(
            f"PhotoFrameViewerApp: finalized swap old_gen={old_gen} -> "
            f"new_gen={new_gen} reason={reason}",
            level="INFO",
        )

        if old_gen is not None and old_gen != new_gen:
            self._call_cleanup(old_gen, reason=f"finalize_{reason}")

    def _clear_pending(self) -> None:
        self._pending_gen_id = None
        self._pending_labels = []
        self._pending_label_to_path = {}
        self._pending_path_to_label = {}
        self._pending_filter_name = ""
        self._pending_fingerprint = None

    def _call_cleanup(self, gen_id: str, *, reason: str) -> None:
        self.log(
            f"PhotoFrameViewerApp: cleanup gen={gen_id} reason={reason}",
            level="INFO",
        )
        self.call_service(
            f"shell_command/{self.cleanup_shell_command}",
            gen_id=gen_id,
        )

    # ------------------------------------------------------------------
    # HA state helpers
    # ------------------------------------------------------------------

    def _is_paused(self) -> bool:
        return self._paused

    def _interval_s(self) -> float:
        return self._interval

    def _picker_options(self) -> list[str]:
        st = self.get_state(self.picker_entity_id, attribute="all")
        if not isinstance(st, dict):
            return []
        attrs = st.get("attributes")
        if not isinstance(attrs, dict):
            return []
        opts = attrs.get("options")
        if isinstance(opts, list):
            return [str(o) for o in opts]
        return []

    def _picker_value(self) -> str:
        return str(self.get_state(self.picker_entity_id) or "").strip()

    # ------------------------------------------------------------------
    # Timer control
    # ------------------------------------------------------------------

    def _cancel_timer(self) -> None:
        handle = self._timer_handle
        if handle is None:
            return
        self._timer_handle = None
        try:
            if self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass

    def _schedule_next(self, *, reason: str) -> None:
        if self._is_paused():
            self._cancel_timer()
            return
        interval = self._interval_s()
        self._cancel_timer()
        self._timer_handle = self.run_in(self._on_tick, interval)
        self.log(
            f"PhotoFrameViewerApp: scheduled tick in {interval:.1f}s reason={reason}",
            level="DEBUG",
        )

    def _sync_timer(self, *, reason: str) -> None:
        if self._is_paused():
            self._cancel_timer()
            self.log(
                f"PhotoFrameViewerApp: paused; timer cancelled reason={reason}",
                level="INFO",
            )
            return
        self._schedule_next(reason=reason)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _poll_for_changes_cb(self, kwargs: Any) -> None:
        self._poll_for_changes(reason="poll")

    def _on_batch_ready(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Fetcher wrote a new batch — poll for changes immediately."""
        # Stash the filter name from the event so we can publish it when the
        # viewer actually swaps to the new generation (avoiding title drift).
        batch_filter = str(data.get("filter", "") or "").strip()
        if batch_filter:
            self._staged_filter_name = batch_filter
        self._poll_for_changes(reason="batch_ready")

    def _on_picker_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: Any) -> None:
        new_label = str(new or "").strip()
        if not new_label:
            return

        # Ignore attribute-only updates where the selected value didn't change.
        old_label = str(old or "").strip()
        if new_label == old_label:
            return

        # Absorb async state-change events from our own programmatic calls.
        if self._programmatic_target is not None and new_label == self._programmatic_target:
            self._programmatic_target = None
            self.log(
                f"PhotoFrameViewerApp: ignoring picker echo {new_label!r} "
                f"(programmatic)",
                level="DEBUG",
            )
            return
        self._programmatic_target = None

        # Manual navigation while a pending gen is queued.
        if self._pending_gen_id is not None:
            self.log(
                f"PhotoFrameViewerApp: manual nav to {new_label!r} (applying pending gen)",
                level="INFO",
            )
            self._apply_pending_gen(reason="manual_nav")
            if self.reset_timer_on_manual_nav:
                self._schedule_next(reason="manual_nav")
            return

        self.log(f"PhotoFrameViewerApp: manual nav to {new_label!r}", level="INFO")
        self._publish_selected_local_url(new_label, reason="manual_nav")
        if self.reset_timer_on_manual_nav:
            self._schedule_next(reason="manual_nav")

    def _on_tick(self, kwargs: Any) -> None:
        self._timer_handle = None
        if self._is_paused():
            self._cancel_timer()
            return

        if self._pending_gen_id is not None:
            self._apply_pending_gen(reason="tick")
            # Gen swap already selected photo_0000 and published its URL.
            # Advancing further would overwrite _programmatic_target causing
            # the echo for photo_0000 to be misclassified as manual nav,
            # which triggers a visible double-advance (rapid skip).
            self._schedule_next(reason="tick_gen_swap")
            return

        opts = self._picker_options()
        if len(opts) <= 1:
            self._schedule_next(reason="tick_no_options")
            return

        # Compute next label locally instead of calling select_next and
        # waiting for the async state-change round-trip.  This avoids the
        # race where the echo event arrives after the debounce flag clears.
        current = self._picker_value()
        try:
            idx = opts.index(current)
        except ValueError:
            idx = -1

        if self.auto_cycle:
            next_idx = (idx + 1) % len(opts)
        else:
            next_idx = min(idx + 1, len(opts) - 1)
        next_label = opts[next_idx]

        self._programmatic_target = next_label
        self.call_service(
            "input_select/select_option",
            entity_id=self.picker_entity_id,
            option=next_label,
        )
        self._publish_selected_local_url(next_label, reason="tick")
        self._schedule_next(reason="tick")
