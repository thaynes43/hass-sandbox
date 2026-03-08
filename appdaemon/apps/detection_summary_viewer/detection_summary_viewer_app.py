"""
DetectionSummaryViewer AppDaemon app.

Manages the run picker, selected run display, viewer cache staging, and
notification action handling for a detection_summary bundle.

Fully decoupled from detection_summary_app: communicates via
  - HA event ``detection_summary/run_published`` (fired by detection_summary_app)
  - Shared filesystem under ``snapshot_ha_dir``
  - ``detection_summary_store`` (shared in-process store)

Self-provisions:
  - ``input_select.{bundle_key}_detection_summary_run_id``   (run picker)
  - ``input_text.{bundle_key}_detection_summary_selected``   (selected summary text)
  - ``script.{bundle_key}_detection_summary_relay``           (dashboard relay)
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import hassapi as hass

from detection_summary_store import STORE as DETECTION_SUMMARY_STORE

from .viewer_cache import ViewerCache, ViewerCacheConfig

try:
    from providers.ha_provisioner import HAProvisioner
except Exception:  # pragma: no cover
    import sys

    # AppDaemon often only adds `appdaemon/apps` to sys.path. Our shared libraries
    # live at `appdaemon/providers`, so add the AppDaemon root directory.
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from providers.ha_provisioner import HAProvisioner  # type: ignore


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


def _normalize_posix_path(path: str) -> str:
    return str(PurePosixPath(path))


def _strip_posix_prefix(path: str, prefix: str) -> Optional[str]:
    p = str(PurePosixPath(path))
    pref = str(PurePosixPath(prefix))
    if p == pref:
        return ""
    if p.startswith(pref.rstrip("/") + "/"):
        return p[len(pref.rstrip("/")) + 1 :]
    return None


# ---------------------------------------------------------------------------
# Retention utilities (filesystem only; no coupling to detection_summary_app)
# ---------------------------------------------------------------------------


def _safe_float_val(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _read_run_created_at(run_dir: Path) -> Optional[float]:
    p = run_dir / "summary.json"
    if not p.exists():
        return None
    try:
        parsed = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            return None
        val = parsed.get("created_at_epoch")
        if val is None:
            return None
        return _safe_float_val(val)
    except Exception:
        return None


def _list_published_run_ids(runs_dir: Path) -> list[tuple[str, float]]:
    """Return (run_id, created_at_epoch) for published runs, newest-first."""
    out: list[tuple[str, float]] = []
    if not runs_dir.exists():
        return out
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        created = _read_run_created_at(child)
        if created is None:
            continue
        out.append((child.name, float(created)))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def _prune_runs_to_max(runs_dir: Path, max_runs: int) -> int:
    max_runs = int(max_runs)
    if max_runs <= 0:
        return 0
    runs = _list_published_run_ids(runs_dir)
    if len(runs) <= max_runs:
        return 0
    deleted = 0
    for run_id, _ in runs[max_runs:]:
        try:
            shutil.rmtree(runs_dir / run_id, ignore_errors=True)
            deleted += 1
        except Exception:
            pass
    return deleted


def _recent_published_run_ids(runs_dir: Path, max_options: int) -> list[str]:
    max_options = max(1, int(max_options))
    runs = _list_published_run_ids(runs_dir)
    return [r[0] for r in runs[:max_options]]


# ---------------------------------------------------------------------------
# Dashboard helper format functions (module-level; pure, easy to unit test)
# ---------------------------------------------------------------------------


def _format_timing_value(timing: dict) -> str:
    """Format summary.json timing block with date context for dashboard display."""
    started = float(timing.get("capture_started_epoch") or 0)
    ended = float(timing.get("capture_ended_epoch") or 0)
    duration = float(timing.get("capture_duration_s") or 0)
    start_dt = time.gmtime(started) if started else None
    end_dt = time.gmtime(ended) if ended else None
    if start_dt:
        start_str = time.strftime("%Y-%m-%d %H:%M:%S", start_dt)
    else:
        start_str = "?"
    if end_dt:
        same_day = bool(start_dt) and (
            start_dt.tm_year == end_dt.tm_year
            and start_dt.tm_mon == end_dt.tm_mon
            and start_dt.tm_mday == end_dt.tm_mday
        )
        end_str = time.strftime("%H:%M:%S", end_dt) if same_day else time.strftime("%Y-%m-%d %H:%M:%S", end_dt)
    else:
        end_str = "?"
    h = int(duration // 3600)
    m = int((duration % 3600) // 60)
    s = int(duration % 60)
    elapsed = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{start_str} → {end_str} ({elapsed})"


def _format_cooldown_value(cooldown: dict) -> str:
    """Format summary.json cooldown_state block for dashboard display."""
    effective = float(cooldown.get("effective_cooldown_s") or 0)
    base = float(cooldown.get("base_cooldown_s") or effective or 0)
    increments = int(cooldown.get("backoff_increments") or 0)
    expires = float(cooldown.get("cooldown_expires_at_epoch") or 0)
    if expires > time.time():
        expires_str = time.strftime("%H:%M:%SZ", time.gmtime(expires))
        status = f"on cooldown until {expires_str}"
    else:
        status = "ready"
    backoff_info = f"x{increments} ({effective:.0f}s, base {base:.0f}s)" if increments > 0 else f"{base:.0f}s base"
    return f"{status} | {backoff_info}"


# ---------------------------------------------------------------------------
# Main app class
# ---------------------------------------------------------------------------


class DetectionSummaryViewer(hass.Hass):
    """Viewer/dashboard coordinator for a detection_summary bundle.

    Listens for ``detection_summary/run_published`` events, stages run artifacts
    into the viewer folder, and keeps the run picker input_select + selected
    summary text in sync.
    """

    DEFAULTS: dict[str, Any] = {
        # filesystem
        "bundle_runs_subdir": "runs",
        "captured_subdir": "captured",
        "media_fs_root": "/media",
        # viewer cache
        "viewer_enabled": True,
        "viewer_stage_subdir": "viewer_stage",
        "viewer_www_subdir": "viewer",
        "viewer_refresh_shell_command": "ds_refresh_detection_summary_viewer_www",
        # run picker
        "run_picker_max_options": 25,
        # optional auto-reset to latest after inactivity (0 = disabled)
        "selected_auto_reset_s": 900,
        # stable selected filenames (legacy materialization fallback)
        "selected_best_filename": "detection_summary_selected_best.jpg",
        "selected_generated_filename": "detection_summary_selected_generated.png",
        # bundle artifact filenames (for materialization lookups)
        "bundle_best_filename": "best.jpg",
        "external_generated_filename": "generated.png",
        # notification action prefix (e.g. "GARAGE_DS_VIEW"); None = disabled
        "notification_action_prefix": None,
    }

    def initialize(self) -> None:
        # Required
        self.bundle_key: str = self.args["bundle_key"]
        self.snapshot_ha_dir: str = _normalize_posix_path(self.args["snapshot_ha_dir"])

        hass_entities = self.args.get("hass_entities") or {}
        if not isinstance(hass_entities, dict):
            hass_entities = {}

        self.media_fs_root: str = (
            str(self.args.get("media_fs_root", self.DEFAULTS["media_fs_root"])).rstrip("/") or "/media"
        )

        # Filesystem layout
        self.bundle_runs_subdir: str = (
            str(self.args.get("bundle_runs_subdir", self.DEFAULTS["bundle_runs_subdir"])).strip("/") or "runs"
        )
        self.captured_subdir: str = (
            str(self.args.get("captured_subdir", self.DEFAULTS["captured_subdir"])).strip("/") or "captured"
        )

        # Run picker / selected helpers (auto-derived from bundle_key when absent from config)
        self.run_picker_entity_id: str = (
            hass_entities.get("run_picker_entity_id")
            or f"input_select.{self.bundle_key}_detection_summary_run_id"
        )
        self.run_picker_max_options: int = int(
            self.args.get("run_picker_max_options", self.DEFAULTS["run_picker_max_options"])
        )
        self.selected_summary_text_entity_id: str = (
            hass_entities.get("selected_summary_text_entity_id")
            or f"input_text.{self.bundle_key}_detection_summary_selected"
        )
        self.timing_helper_entity_id: str = (
            hass_entities.get("timing_helper_entity_id")
            or f"input_text.{self.bundle_key}_detection_summary_timing"
        )
        self.cooldown_helper_entity_id: str = (
            hass_entities.get("cooldown_helper_entity_id")
            or f"input_text.{self.bundle_key}_detection_summary_cooldown"
        )
        self.selected_best_image_camera_entity_id: Optional[str] = hass_entities.get(
            "selected_best_image_camera_entity_id"
        )
        self.selected_generated_image_camera_entity_id: Optional[str] = hass_entities.get(
            "selected_generated_image_camera_entity_id"
        )

        # Stable selected filenames (legacy materialization)
        self.selected_best_filename: str = (
            str(self.args.get("selected_best_filename", self.DEFAULTS["selected_best_filename"])).strip()
            or str(self.DEFAULTS["selected_best_filename"])
        )
        self.selected_generated_filename: str = (
            str(self.args.get("selected_generated_filename", self.DEFAULTS["selected_generated_filename"])).strip()
            or str(self.DEFAULTS["selected_generated_filename"])
        )

        # Bundle artifact filenames (for materialization path lookups)
        self.bundle_best_filename: str = (
            str(self.args.get("bundle_best_filename", self.DEFAULTS["bundle_best_filename"])).strip()
            or str(self.DEFAULTS["bundle_best_filename"])
        )
        self.external_generated_filename: str = (
            str(self.args.get("external_generated_filename", self.DEFAULTS["external_generated_filename"])).strip()
            or str(self.DEFAULTS["external_generated_filename"])
        )

        # Auto-reset picker to latest after inactivity
        self.selected_auto_reset_s: float = _safe_float(
            self.args.get("selected_auto_reset_s", self.DEFAULTS["selected_auto_reset_s"]),
            default=0.0,
        )
        self._selected_last_set_ts: float = 0.0

        # Notification action prefix (e.g. "GARAGE_DS_VIEW")
        self.notification_action_prefix: Optional[str] = (
            str(self.args.get("notification_action_prefix") or "").strip() or None
        )

        # Viewer cache
        self.viewer_enabled: bool = _as_bool(
            self.args.get("viewer_enabled", self.DEFAULTS["viewer_enabled"]), default=True
        )
        self.viewer_stage_subdir: str = (
            str(self.args.get("viewer_stage_subdir", self.DEFAULTS["viewer_stage_subdir"])).strip("/")
            or "viewer_stage"
        )
        if "viewer_www_subdir" in self.args:
            val = self.args["viewer_www_subdir"]
            self.viewer_www_subdir = (
                str(val).strip("/") if val is not None else ""
            )
        else:
            self.viewer_www_subdir: str = str(self.DEFAULTS["viewer_www_subdir"])
        self.viewer_refresh_shell_command: str = (
            str(self.args.get("viewer_refresh_shell_command", self.DEFAULTS["viewer_refresh_shell_command"])).strip()
            or str(self.DEFAULTS["viewer_refresh_shell_command"])
        )

        self._viewer_cache: Optional[ViewerCache] = None
        if self.viewer_enabled:
            self._viewer_cache = ViewerCache(
                cfg=ViewerCacheConfig(
                    snapshot_ha_dir=self.snapshot_ha_dir,
                    bundle_runs_subdir=self.bundle_runs_subdir,
                    captured_subdir=self.captured_subdir,
                    viewer_stage_subdir=self.viewer_stage_subdir,
                    viewer_www_subdir=self.viewer_www_subdir,
                    refresh_shell_command=self.viewer_refresh_shell_command,
                ),
                ha_path_to_local_fs=self._ha_path_to_local_fs,
                call_service=self.call_service,
                log=self.log,
            )

        # Serializes selected-run materialization so rapid picker changes don't interleave.
        self._materialize_lock = threading.Lock()
        self._materialize_seq = 0

        # Ensure the runs directory exists
        base = self._ha_path_to_local_fs(self.snapshot_ha_dir)
        (base / self.bundle_runs_subdir).mkdir(parents=True, exist_ok=True)

        self.log(
            f"DetectionSummaryViewer[{self.bundle_key}]: run_picker={self.run_picker_entity_id} "
            f"viewer_enabled={self.viewer_enabled} base={self.snapshot_ha_dir}",
            level="INFO",
        )

        self.run_in(self._async_startup_wrapper, 0)

    def _ha_path_to_local_fs(self, ha_path: str) -> Path:
        remainder = _strip_posix_prefix(ha_path, "/media")
        if remainder is None:
            return Path(ha_path)
        return Path(self.media_fs_root) / remainder

    # --- async startup --------------------------------------------------

    def _async_startup_wrapper(self, kwargs) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._provision_entities()

        # React to new runs published by detection_summary_app
        self.listen_event(self._on_run_published, "detection_summary/run_published")

        # Keep selected artifacts in sync when the user changes the picker
        self.listen_state(self._on_run_picker_change, self.run_picker_entity_id)

        # Allow selecting a specific run via notification action buttons
        if self.notification_action_prefix:
            self.listen_event(self._on_mobile_app_notification_action, "mobile_app_notification_action")

        # Keep picker options fresh even if no new runs are published
        self.run_every(self._sync_run_picker_periodic, "now", 600)
        # Sync shortly after startup; mounts/integrations may not be ready at init time
        self.run_in(self._sync_run_picker_periodic, 2)
        self.run_in(self._sync_run_picker_periodic, 15)

        if float(self.selected_auto_reset_s) > 0:
            self.run_every(self._maybe_auto_reset_picker, "now", 60)

    async def _provision_entities(self) -> None:
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        bk = self.bundle_key
        bk_display = bk.replace("_", " ").title()
        prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)

        for helper_type, name, extra_kwargs in [
            ("input_select", f"{bk_display} Detection Summary Run Id", {"options": ["loading"]}),
            ("input_text", f"{bk_display} Detection Summary Selected", {"max": 255}),
            ("input_text", f"{bk_display} Detection Summary Timing", {"max": 255}),
            ("input_text", f"{bk_display} Detection Summary Cooldown", {"max": 255}),
        ]:
            try:
                created = await prov.ensure_helper(helper_type, name, **extra_kwargs)
                level = "INFO" if created else "DEBUG"
                slug = prov._helper_slug(helper_type, name)
                entity_id = f"{helper_type}.{slug}"
                msg = "created" if created else "already exists"
                self.log(f"DetectionSummaryViewer[{bk}]: helper {entity_id} {msg}", level=level)
            except Exception as exc:
                self.log(
                    f"DetectionSummaryViewer[{bk}]: failed to provision {helper_type} '{name}': {exc!r}",
                    level="ERROR",
                )

        relay_id = f"{bk}_detection_summary_relay"
        try:
            created = await prov.ensure_script(
                relay_id,
                {
                    "alias": f"Detection Summary {bk_display} Relay",
                    "description": "Relays dashboard commands to AppDaemon",
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
                            "event": f"{bk}_detection_summary_command",
                            "event_data": {
                                "command": "{{ command }}",
                                "payload": "{{ payload | default('{}') }}",
                            },
                        }
                    ],
                },
            )
            level = "INFO" if created else "DEBUG"
            msg = "created" if created else "already exists"
            self.log(f"DetectionSummaryViewer[{bk}]: relay script.{relay_id} {msg}", level=level)
        except Exception as exc:
            self.log(
                f"DetectionSummaryViewer[{bk}]: failed to provision relay script.{relay_id}: {exc!r}",
                level="ERROR",
            )

    # --- event handler: new run published --------------------------------

    def _on_run_published(self, event_name, data, kwargs) -> None:
        """Called when detection_summary_app fires detection_summary/run_published."""
        try:
            if not isinstance(data, dict):
                return
            if data.get("bundle_key") != self.bundle_key:
                return
            run_id = str(data.get("run_id") or "").strip()
            if not run_id:
                return
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: run_published run_id={run_id}",
                level="INFO",
            )
            # Stage + refresh viewer folder so the new run is available
            self._sync_run_picker_periodic({})
            # Select the newly published run if it staged successfully
            try:
                st = self.get_state(self.run_picker_entity_id, attribute="all") or {}
                attrs = st.get("attributes") if isinstance(st, dict) else None
                options = (attrs.get("options") if isinstance(attrs, dict) else None) or []
                options = [str(o) for o in options] if isinstance(options, list) else []
            except Exception:
                options = []
            if run_id in options:
                try:
                    self._selected_last_set_ts = time.time()
                    self.call_service(
                        "input_select/select_option",
                        target={"entity_id": self.run_picker_entity_id},
                        option=run_id,
                    )
                except Exception as e:
                    self.log(
                        f"DetectionSummaryViewer[{self.bundle_key}]: failed to select new run_id={run_id}: {e!r}",
                        level="WARNING",
                    )
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: _on_run_published failed: {e!r}",
                level="WARNING",
            )

    # --- selected-run viewer helpers ------------------------------------

    def _update_selected_summary_text(self, run_id: str, *, local_run_dir: Optional[Path] = None) -> None:
        if not self.selected_summary_text_entity_id:
            return
        text = ""
        try:
            bundle = DETECTION_SUMMARY_STORE.get_bundle_by_run_id(self.bundle_key, run_id, include_consumed=True)
            if isinstance(bundle, dict):
                rn = bundle.get("run_narrative")
                if isinstance(rn, dict):
                    text = str(rn.get("run_summary") or "").strip()
                if not text:
                    text = str(((bundle.get("best") or {}).get("summary") or "")).strip()
            if not text:
                if local_run_dir is None:
                    local_run_dir = (
                        self._ha_path_to_local_fs(self.snapshot_ha_dir) / self.bundle_runs_subdir / run_id
                    )
                p = local_run_dir / "summary.json"
                if p.exists():
                    parsed = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        rn = parsed.get("run_narrative")
                        if isinstance(rn, dict):
                            text = str(rn.get("run_summary") or "").strip()
                        if not text:
                            text = str(((parsed.get("best") or {}).get("summary") or "")).strip()
        except Exception:
            text = text or ""

        value = str(text or "").strip()
        if len(value) > 255:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: selected summary exceeded 255 chars; truncating run_id={run_id}",
                level="WARNING",
            )
            value = value[:252] + "..."
        try:
            self.call_service(
                "input_text/set_value",
                entity_id=self.selected_summary_text_entity_id,
                value=value,
            )
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: update selected summary helper failed: {e!r}",
                level="WARNING",
            )

    def _update_run_detail_helpers(self, run_id: str, *, local_run_dir: Optional[Path] = None) -> None:
        """Read summary.json for run_id and update timing + cooldown input_text helpers."""
        if not self.timing_helper_entity_id and not self.cooldown_helper_entity_id:
            return
        if local_run_dir is None:
            local_run_dir = (
                self._ha_path_to_local_fs(self.snapshot_ha_dir) / self.bundle_runs_subdir / run_id
            )
        summary_path = local_run_dir / "summary.json"
        if not summary_path.exists():
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: _update_run_detail_helpers: summary.json missing for run_id={run_id}",
                level="WARNING",
            )
            return
        try:
            parsed = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: _update_run_detail_helpers: failed to read summary.json for run_id={run_id}: {e!r}",
                level="WARNING",
            )
            return

        if self.timing_helper_entity_id:
            timing = (parsed.get("summary") or {}).get("timing") if isinstance(parsed, dict) else None
            if timing is None:
                self.log(
                    f"DetectionSummaryViewer[{self.bundle_key}]: _update_run_detail_helpers: timing block absent for run_id={run_id}",
                    level="WARNING",
                )
            else:
                try:
                    value = _format_timing_value(timing)
                    if len(value) > 255:
                        value = value[:252] + "..."
                    self.call_service(
                        "input_text/set_value",
                        entity_id=self.timing_helper_entity_id,
                        value=value,
                    )
                except Exception as e:
                    self.log(
                        f"DetectionSummaryViewer[{self.bundle_key}]: _update_run_detail_helpers: failed to set timing helper for run_id={run_id}: {e!r}",
                        level="WARNING",
                    )

        if self.cooldown_helper_entity_id:
            cooldown = parsed.get("cooldown_state") if isinstance(parsed, dict) else None
            if cooldown is None:
                self.log(
                    f"DetectionSummaryViewer[{self.bundle_key}]: _update_run_detail_helpers: cooldown_state absent for run_id={run_id} (old run or backoff disabled)",
                    level="WARNING",
                )
                try:
                    self.call_service(
                        "input_text/set_value",
                        entity_id=self.cooldown_helper_entity_id,
                        value="unknown",
                    )
                except Exception as e:
                    self.log(
                        f"DetectionSummaryViewer[{self.bundle_key}]: _update_run_detail_helpers: failed to clear cooldown helper for run_id={run_id}: {e!r}",
                        level="WARNING",
                    )
            else:
                try:
                    value = _format_cooldown_value(cooldown)
                    if len(value) > 255:
                        value = value[:252] + "..."
                    self.call_service(
                        "input_text/set_value",
                        entity_id=self.cooldown_helper_entity_id,
                        value=value,
                    )
                except Exception as e:
                    self.log(
                        f"DetectionSummaryViewer[{self.bundle_key}]: _update_run_detail_helpers: failed to set cooldown helper for run_id={run_id}: {e!r}",
                        level="WARNING",
                    )

    def _select_run_from_www_cache(self, run_id: str) -> None:
        """Repoint the two selected local_file cameras to staged viewer files for run_id."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return

        best_cam = str(self.selected_best_image_camera_entity_id or "").strip()
        gen_cam = str(self.selected_generated_image_camera_entity_id or "").strip()
        if not self._viewer_cache:
            return
        best_path, gen_path = self._viewer_cache.selected_file_paths_for_run(run_id)
        if best_cam and best_path:
            self.call_service("local_file/update_file_path", entity_id=best_cam, file_path=best_path)
        if gen_cam and gen_path:
            self.call_service("local_file/update_file_path", entity_id=gen_cam, file_path=gen_path)

    def _parse_run_id_from_action(self, action: Any) -> Optional[str]:
        s = str(action or "").strip()
        prefix = str(self.notification_action_prefix or "").strip()
        if not prefix:
            return None
        if not s.startswith(f"{prefix}:"):
            return None
        run_id = s.split(":", 1)[1].strip()
        return run_id or None

    def _resolve_run_id_from_options(self, token: str, options: list[str]) -> Optional[str]:
        """
        Resolve a run token to a full run_id present in picker options.

        Supports full run_ids and short UUID prefixes (e.g. first 8 chars).
        """
        token = str(token or "").strip()
        if not token:
            return None
        if token in options:
            return token
        matches = [o for o in (options or []) if str(o).startswith(token)]
        if len(matches) == 1:
            return str(matches[0])
        return None

    def _on_mobile_app_notification_action(self, event_name, data, kwargs) -> None:
        try:
            if not isinstance(data, dict):
                return
            run_id = self._parse_run_id_from_action(data.get("action"))
            if not run_id:
                return
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: notification action select run_id={run_id}",
                level="INFO",
            )
            # Refresh picker (stages + refreshes viewer folder) then select
            self._sync_run_picker_periodic({})
            try:
                st = self.get_state(self.run_picker_entity_id, attribute="all") or {}
                attrs = st.get("attributes") if isinstance(st, dict) else None
                options = (attrs.get("options") if isinstance(attrs, dict) else None) or []
                options = [str(o) for o in options] if isinstance(options, list) else []
            except Exception:
                options = []
            resolved = self._resolve_run_id_from_options(run_id, options)
            if not resolved:
                self.log(
                    f"DetectionSummaryViewer[{self.bundle_key}]: notification action run_id not in picker options "
                    f"(not staged); ignoring run_id={run_id}",
                    level="WARNING",
                )
                return
            self.call_service(
                "input_select/select_option",
                target={"entity_id": self.run_picker_entity_id},
                option=resolved,
            )
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: notification action handler failed: {e!r}",
                level="WARNING",
            )

    def _on_run_picker_change(self, entity_id, attribute, old, new, kwargs) -> None:
        try:
            run_id = str(new or "").strip()
            if not run_id or run_id == str(old or "").strip():
                return
            self._selected_last_set_ts = time.time()
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: run picker changed -> {run_id}", level="INFO"
            )
            self._select_run_from_www_cache(run_id)
            self._update_selected_summary_text(run_id)
            self._update_run_detail_helpers(run_id)
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: run picker change failed: {e!r}", level="WARNING"
            )

    def _maybe_auto_reset_picker(self, kwargs) -> None:
        try:
            if float(self.selected_auto_reset_s) <= 0:
                return
            if float(self._selected_last_set_ts) <= 0:
                return
            if (time.time() - float(self._selected_last_set_ts)) < float(self.selected_auto_reset_s):
                return
            self.call_service(
                "input_select/select_first",
                target={"entity_id": self.run_picker_entity_id},
            )
        except Exception:
            return

    def _sync_run_picker_periodic(self, kwargs) -> None:
        """Prune old runs and sync picker options with disk."""
        max_retained_runs = int(self.args.get("max_retained_runs", 100))
        runs_dir = self._ha_path_to_local_fs(self.snapshot_ha_dir) / self.bundle_runs_subdir
        try:
            pruned = _prune_runs_to_max(runs_dir=runs_dir, max_runs=int(max_retained_runs))
            options = _recent_published_run_ids(runs_dir=runs_dir, max_options=int(self.run_picker_max_options))
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: sync run picker runs_dir={runs_dir} "
                f"max_retained_runs={int(max_retained_runs)} pruned={int(pruned)} options={len(options)}",
                level="INFO",
            )
            if not options:
                return
            if self._viewer_cache:
                staged = self._viewer_cache.stage_run_ids_to_media(options)
                if not staged:
                    self.log(
                        f"DetectionSummaryViewer[{self.bundle_key}]: viewer staging produced no runs; "
                        f"skipping picker update",
                        level="WARNING",
                    )
                    return
                self._viewer_cache.refresh_www_from_stage()
                options = staged

            current = str(self.get_state(self.run_picker_entity_id) or "").strip()
            self.call_service(
                "input_select/set_options",
                target={"entity_id": self.run_picker_entity_id},
                options=options,
            )
            if not current or current.lower() in {"unknown", "unavailable", "loading"} or current not in options:
                self.call_service(
                    "input_select/select_option",
                    target={"entity_id": self.run_picker_entity_id},
                    option=options[0],
                )
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: sync run picker failed runs_dir={runs_dir}: {e!r}",
                level="WARNING",
            )

    def _set_run_picker_value(self, run_id: str) -> None:
        """Ensure picker contains run_id, then select it."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return
        try:
            state = self.get_state(self.run_picker_entity_id, attribute="all") or {}
            current = str(state.get("state") if isinstance(state, dict) else "").strip()
            attrs = state.get("attributes") if isinstance(state, dict) else None
            options = (attrs.get("options") if isinstance(attrs, dict) else None) or []
            if not isinstance(options, list):
                options = []
            options = [str(o) for o in options if str(o).strip() and str(o).strip().lower() != "loading"]
            new_options = [run_id] + [o for o in options if o != run_id]
            new_options = new_options[: max(1, int(self.run_picker_max_options))]
            self.call_service(
                "input_select/set_options",
                target={"entity_id": self.run_picker_entity_id},
                options=new_options,
            )
            self.call_service(
                "input_select/select_option",
                target={"entity_id": self.run_picker_entity_id},
                option=run_id,
            )
            self._selected_last_set_ts = time.time()
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: failed to set run picker: {e!r}", level="WARNING"
            )

    def _add_run_id_to_picker(self, run_id: str) -> bool:
        """Add run_id to picker options (newest-first). Returns True if we selected it."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return False
        try:
            state = self.get_state(self.run_picker_entity_id, attribute="all") or {}
            current = str(state.get("state") if isinstance(state, dict) else "").strip()
            attrs = state.get("attributes") if isinstance(state, dict) else None
            options = (attrs.get("options") if isinstance(attrs, dict) else None) or []
            if not isinstance(options, list):
                options = []
            options = [str(o) for o in options if str(o).strip() and str(o).strip().lower() != "loading"]
            new_options = [run_id] + [o for o in options if o != run_id]
            new_options = new_options[: max(1, int(self.run_picker_max_options))]

            should_select = False
            if not current or current.lower() in {"unknown", "unavailable", "loading"}:
                should_select = True
            if current and current not in new_options:
                should_select = True
            if float(self.selected_auto_reset_s) > 0 and float(self._selected_last_set_ts) > 0:
                if (time.time() - float(self._selected_last_set_ts)) >= float(self.selected_auto_reset_s):
                    should_select = True

            self.call_service(
                "input_select/set_options",
                target={"entity_id": self.run_picker_entity_id},
                options=new_options,
            )
            if should_select:
                self.call_service(
                    "input_select/select_option",
                    target={"entity_id": self.run_picker_entity_id},
                    option=run_id,
                )
                self._selected_last_set_ts = time.time()
                return True
        except Exception as e:
            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: failed to update run picker: {e!r}", level="WARNING"
            )
        return False

    def _atomic_copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)

    def _materialize_selected_run(self, run_id: str) -> None:
        """Copy per-run artifacts to stable selected filenames and update selected summary text."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return

        try:
            self._materialize_seq = int(getattr(self, "_materialize_seq", 0)) + 1
        except Exception:
            self._materialize_seq = 1
        my_seq = int(self._materialize_seq)

        def _stat(p: Path) -> str:
            try:
                if not p.exists():
                    return "missing"
                st = p.stat()
                return f"exists size={int(st.st_size)} mtime={float(st.st_mtime):.3f}"
            except Exception as e:
                return f"stat_error={e!r}"

        local_run_dir = (
            self._ha_path_to_local_fs(self.snapshot_ha_dir) / self.bundle_runs_subdir / run_id
        )

        best_src = local_run_dir / self.bundle_best_filename
        gen_src = local_run_dir / self.external_generated_filename

        base_local = self._ha_path_to_local_fs(self.snapshot_ha_dir)
        best_dst = base_local / self.selected_best_filename
        gen_dst = base_local / self.selected_generated_filename

        lock = getattr(self, "_materialize_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._materialize_lock = lock

        with lock:
            if int(getattr(self, "_materialize_seq", 0)) != my_seq:
                return

            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: materialize selected start seq={my_seq} "
                f"run_id={run_id} run_dir={local_run_dir} best_src={_stat(best_src)} gen_src={_stat(gen_src)}",
                level="INFO",
            )

            best_copied = False
            gen_copied = False

            try:
                if best_src.exists():
                    self._atomic_copy(best_src, best_dst)
                    best_copied = True
                else:
                    best_idx = None
                    try:
                        p = local_run_dir / "summary.json"
                        if p.exists():
                            parsed = json.loads(p.read_text(encoding="utf-8"))
                            if isinstance(parsed, dict):
                                best_idx = parsed.get("best_idx") or (parsed.get("summary") or {}).get("best_idx")
                    except Exception:
                        best_idx = None
                    if best_idx is not None:
                        alt = local_run_dir / self.captured_subdir / f"frame_{int(best_idx):03d}.jpg"
                        if alt.exists():
                            self._atomic_copy(alt, best_dst)
                            best_copied = True
                            self.log(
                                f"DetectionSummaryViewer[{self.bundle_key}]: materialize selected best fallback "
                                f"seq={my_seq} run_id={run_id} best_idx={int(best_idx)} src={alt.name}",
                                level="WARNING",
                            )
            except Exception as e:
                self.log(
                    f"DetectionSummaryViewer[{self.bundle_key}]: materialize selected best failed "
                    f"seq={my_seq} run_id={run_id}: {e!r}",
                    level="WARNING",
                )

            if int(getattr(self, "_materialize_seq", 0)) != my_seq:
                self.log(
                    f"DetectionSummaryViewer[{self.bundle_key}]: materialize selected abort before generated "
                    f"(newer selection) seq={my_seq} run_id={run_id}",
                    level="INFO",
                )
                return

            try:
                if gen_src.exists():
                    self._atomic_copy(gen_src, gen_dst)
                    gen_copied = True
                else:
                    if best_copied and best_dst.exists():
                        self._atomic_copy(best_dst, gen_dst)
                        gen_copied = True
                        self.log(
                            f"DetectionSummaryViewer[{self.bundle_key}]: materialize selected generated missing; "
                            f"using best as fallback seq={my_seq} run_id={run_id}",
                            level="WARNING",
                        )
            except Exception as e:
                self.log(
                    f"DetectionSummaryViewer[{self.bundle_key}]: materialize selected generated failed "
                    f"seq={my_seq} run_id={run_id}: {e!r}",
                    level="WARNING",
                )

            self.log(
                f"DetectionSummaryViewer[{self.bundle_key}]: materialize selected done seq={my_seq} "
                f"run_id={run_id} best_dst={_stat(best_dst)} gen_dst={_stat(gen_dst)} "
                f"copied_best={best_copied} copied_gen={gen_copied}",
                level="INFO",
            )

        self._update_selected_summary_text(run_id, local_run_dir=local_run_dir)
        self._update_run_detail_helpers(run_id, local_run_dir=local_run_dir)
