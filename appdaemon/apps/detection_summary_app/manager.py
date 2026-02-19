"""
DetectionSummary AppDaemon app entrypoint.

This is the orchestrator that:
- starts a run on motion `off->on`
- captures frames while motion is on (stops when off for off_grace_s or capture_max_s)
- selects and scores up to a budget of frames
- generates an illustration from the best frame
- mirrors the latest generated image to a stable filename in the zone directory
- publishes a bundle and fires events
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import hassapi as hass

from detection_summary_store import STORE as DETECTION_SUMMARY_STORE

from .bundle import (
    BundleConfig,
    TraceConfig,
    build_bundle_dict,
    maybe_write_bundle_json,
    run_ha_dir,
    stable_best_ha_path,
    stable_generated_ha_path,
    write_trace,
)
from .capture import CaptureConfig, CaptureState, CapturedFrame, next_delay_s, should_stop_capture
from .selection import ScoreResult, SelectionMeta, adaptive_select_and_score
from .population import augment_image_instructions, compute_population_bounds
from .narrative import NarrativeConfig, synthesize_run_narrative
from .publish_gate import should_publish_bundle
from .retention import delete_run_dir, prune_runs_to_max, recent_published_run_ids
from .viewer_cache import ViewerCache, ViewerCacheConfig

try:
    from ai_providers.registry import (
        build_data_provider,
        build_image_provider,
        data_provider_config_from_appdaemon_args,
        provider_config_from_appdaemon_args,
    )
    from ai_providers.types import ExternalDataGenError, ExternalImageGenError
except Exception:  # pragma: no cover
    import sys

    # AppDaemon often only adds `appdaemon/apps` to sys.path. Our shared libraries
    # live at `appdaemon/ai_providers`, so add the AppDaemon root directory.
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from ai_providers.registry import (  # type: ignore
        build_data_provider,
        build_image_provider,
        data_provider_config_from_appdaemon_args,
        provider_config_from_appdaemon_args,
    )
    from ai_providers.types import ExternalDataGenError, ExternalImageGenError  # type: ignore


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


@dataclass
class _Run:
    capture: CaptureState
    bundle: Optional[dict[str, Any]] = None


class DetectionSummary(hass.Hass):
    DEFAULTS = {
        "trigger_to": "on",
        "task_name": "detection summary",
        # capture
        "snapshot_interval_s": 2.5,
        "off_grace_s": 15,
        "capture_max_s": 300,
        # cooldown
        "cooldown_s": 60,
        "cooldown_backoff_max_s": 1800,
        # selection/scoring
        "analyze_max_snapshots": 10,
        "no_people_threshold": 1.0,
        "external_data_parallelism": 4,
        # AI scoring and generation
        "ai_data_enabled": True,
        # If the best scored frame has person_score below this value, skip bundle + image generation.
        # (Future: extend to "people OR animals" detection.)
        "best_min_person_score": 2,
        # If any analyzed frame contains >= this many animals, publish even if person_score is low.
        "best_min_animal_count": 1,
        # image generation
        "external_image_gen_enabled": True,
        "external_image_gen_wait_for_best_s": 5,
        "external_generated_filename": "generated.png",
        "bundle_best_filename": "best.jpg",
        # stable published best file name (for a local_file camera to point at a constant path).
        "published_best_filename": "detection_summary_best.jpg",
        # stable published generated file name
        "published_generated_filename": "detection_summary_generated.png",
        # dirs
        "bundle_runs_subdir": "runs",
        "captured_subdir": "captured",
        # storage (always uses HA /media; shared with AppDaemon via media_fs_root mapping)
        "media_fs_root": "/media",
        "write_bundle_json": True,
        # local_file camera for stable generated
        "generated_image_camera_entity_id": None,
        # Optional: local_file camera for the *best* image.
        "best_image_camera_entity_id": None,
        # Optional: HA helper (input_text) to store the most recent summary text.
        "summary_text_entity_id": None,
        # Selected-run viewer (HA dashboard + helpers)
        # - input_select contains run_ids (newest first)
        # - selected_* artifacts are stable files that the dashboard shows
        "run_picker_entity_id": None,
        "run_picker_max_options": 25,
        "selected_summary_text_entity_id": None,
        "selected_best_image_camera_entity_id": None,
        "selected_generated_image_camera_entity_id": None,
        "selected_best_filename": "detection_summary_selected_best.jpg",
        "selected_generated_filename": "detection_summary_selected_generated.png",
        # Viewer cache (dashboard): stage required runs into `/media` with stable renamed files,
        # then ask HA (shell_command) to wipe+fill `/config/www/.../<viewer_www_subdir>/`.
        "viewer_enabled": True,
        "viewer_stage_subdir": "viewer_stage",
        "viewer_www_subdir": "viewer",
        "viewer_refresh_shell_command": "ds_refresh_detection_summary_viewer_www",
        # Optional: if >0, reset picker to latest after inactivity.
        "selected_auto_reset_s": 900,
        # Run-level narrative summary (second LLM step; text-only)
        "run_narrative_enabled": True,
        "run_narrative_max_chars": 220,
        "run_narrative_instructions": None,
        # trace
        "trace_enabled": False,
        "trace_copy_selected_frames": True,
        "trace_copy_best_frame": True,
        "trace_max_copies": 50,
        # logging
        "log_snapshot_events": True,
        "log_llm_events": True,
    }

    def initialize(self) -> None:
        # Required args
        self.bundle_key: str = self.args["bundle_key"]
        hass_entities = self.args.get("hass_entities")
        if not isinstance(hass_entities, dict):
            raise ValueError("hass_entities is required and must be a dict")
        if not hass_entities.get("camera_entity_id"):
            raise ValueError("hass_entities.camera_entity_id is required")
        if not hass_entities.get("trigger_entity_id"):
            raise ValueError("hass_entities.trigger_entity_id is required")
        self.camera_entity_id: str = str(hass_entities["camera_entity_id"])
        self.trigger_entity_id: str = str(hass_entities["trigger_entity_id"])
        self.snapshot_ha_dir: str = _normalize_posix_path(self.args["snapshot_ha_dir"])
        self.data_instructions: str = self.args["data_instructions"]
        # Fixed model output schema we depend on.
        # Keep this in code so `apps.yaml` stays minimal and consistent.
        self.expected_keys: list[str] = [
            "male_count",
            "female_count",
            "animal_count",
            "person_score",
            "face_score",
            "frame_score",
            "pose",
            "summary",
        ]

        # Config
        self.trigger_to: str = str(self.args.get("trigger_to", self.DEFAULTS["trigger_to"]))
        self.task_name: str = str(self.args.get("task_name", self.DEFAULTS["task_name"]))

        self.snapshot_interval_s: float = _safe_float(self.args.get("snapshot_interval_s", self.DEFAULTS["snapshot_interval_s"]))
        self.off_grace_s: float = _safe_float(self.args.get("off_grace_s", self.DEFAULTS["off_grace_s"]))
        self.capture_max_s: float = _safe_float(self.args.get("capture_max_s", self.DEFAULTS["capture_max_s"]))

        self.cooldown_s: float = _safe_float(self.args.get("cooldown_s", self.DEFAULTS["cooldown_s"]))
        self.cooldown_backoff_max_s: float = _safe_float(
            self.args.get("cooldown_backoff_max_s", self.DEFAULTS["cooldown_backoff_max_s"])
        )
        self._effective_cooldown_s: float = float(self.cooldown_s)

        self.analyze_max_snapshots: int = int(self.args.get("analyze_max_snapshots", self.args.get("max_snapshots", self.DEFAULTS["analyze_max_snapshots"])))
        self.no_people_threshold: float = _safe_float(self.args.get("no_people_threshold", self.DEFAULTS["no_people_threshold"]))
        self.external_data_parallelism: int = int(
            self.args.get("external_data_parallelism", self.DEFAULTS["external_data_parallelism"])
        )

        self.ai_data_enabled: bool = _as_bool(self.args.get("ai_data_enabled", self.DEFAULTS["ai_data_enabled"]), default=True)
        self.best_min_person_score: float = _safe_float(
            self.args.get("best_min_person_score", self.DEFAULTS["best_min_person_score"]),
            default=float(self.DEFAULTS["best_min_person_score"]),
        )
        self.best_min_animal_count: int = int(self.args.get("best_min_animal_count", self.DEFAULTS["best_min_animal_count"]))

        self.external_image_gen_enabled: bool = _as_bool(self.args.get("external_image_gen_enabled", self.DEFAULTS["external_image_gen_enabled"]))
        self.external_image_gen_wait_for_best_s: float = _safe_float(
            self.args.get("external_image_gen_wait_for_best_s", self.DEFAULTS["external_image_gen_wait_for_best_s"])
        )
        self.external_generated_filename: str = str(self.args.get("external_generated_filename", self.DEFAULTS["external_generated_filename"]))
        self.bundle_best_filename: str = str(self.args.get("bundle_best_filename", self.DEFAULTS["bundle_best_filename"]))
        self.image_instructions: str = str(self.args.get("image_instructions") or "").strip()

        self.published_best_filename: str = str(
            self.args.get("published_best_filename", self.DEFAULTS["published_best_filename"])
        ).strip() or str(self.DEFAULTS["published_best_filename"])

        self.published_generated_filename: str = str(
            self.args.get("published_generated_filename", self.DEFAULTS["published_generated_filename"])
        ).strip() or str(self.DEFAULTS["published_generated_filename"])

        self.bundle_runs_subdir: str = str(self.args.get("bundle_runs_subdir", self.DEFAULTS["bundle_runs_subdir"])).strip("/") or "runs"
        self.captured_subdir: str = str(self.args.get("captured_subdir", self.DEFAULTS["captured_subdir"])).strip("/") or "captured"

        self.media_fs_root: str = str(self.args.get("media_fs_root", self.DEFAULTS["media_fs_root"])).rstrip("/") or "/media"

        # Viewer cache: keep dashboard files in `/config/www` small + bounded.
        self.viewer_enabled: bool = _as_bool(self.args.get("viewer_enabled", self.DEFAULTS["viewer_enabled"]), default=True)
        self.viewer_stage_subdir: str = str(self.args.get("viewer_stage_subdir", self.DEFAULTS["viewer_stage_subdir"])).strip("/") or "viewer_stage"
        self.viewer_www_subdir: str = str(self.args.get("viewer_www_subdir", self.DEFAULTS["viewer_www_subdir"])).strip("/") or "viewer"
        self.viewer_refresh_shell_command: str = str(
            self.args.get("viewer_refresh_shell_command", self.DEFAULTS["viewer_refresh_shell_command"])
        ).strip() or str(self.DEFAULTS["viewer_refresh_shell_command"])
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

        self.write_bundle_json: bool = _as_bool(self.args.get("write_bundle_json", self.DEFAULTS["write_bundle_json"]), default=True)
        self.generated_image_camera_entity_id: Optional[str] = hass_entities.get("generated_image_camera_entity_id")
        self.best_image_camera_entity_id: Optional[str] = hass_entities.get("best_image_camera_entity_id")
        self.summary_text_entity_id: Optional[str] = hass_entities.get("summary_text_entity_id")

        # Selected-run viewer config
        self.run_picker_entity_id: Optional[str] = hass_entities.get("run_picker_entity_id")
        self.run_picker_max_options: int = int(
            self.args.get("run_picker_max_options", self.DEFAULTS["run_picker_max_options"])
        )
        self.selected_summary_text_entity_id: Optional[str] = hass_entities.get("selected_summary_text_entity_id")
        self.selected_best_image_camera_entity_id: Optional[str] = hass_entities.get("selected_best_image_camera_entity_id")
        self.selected_generated_image_camera_entity_id: Optional[str] = hass_entities.get("selected_generated_image_camera_entity_id")
        self.selected_best_filename: str = str(
            self.args.get("selected_best_filename", self.DEFAULTS["selected_best_filename"])
        ).strip() or str(self.DEFAULTS["selected_best_filename"])
        self.selected_generated_filename: str = str(
            self.args.get("selected_generated_filename", self.DEFAULTS["selected_generated_filename"])
        ).strip() or str(self.DEFAULTS["selected_generated_filename"])
        self.selected_auto_reset_s: float = _safe_float(
            self.args.get("selected_auto_reset_s", self.DEFAULTS["selected_auto_reset_s"]),
            default=0.0,
        )
        self._selected_last_set_ts: float = 0.0
        self.run_narrative_enabled: bool = _as_bool(
            self.args.get("run_narrative_enabled", self.DEFAULTS["run_narrative_enabled"]),
            default=True,
        )
        self.run_narrative_max_chars: int = int(
            self.args.get("run_narrative_max_chars", self.DEFAULTS["run_narrative_max_chars"])
        )
        self.run_narrative_instructions: Optional[str] = self.args.get(
            "run_narrative_instructions", self.DEFAULTS["run_narrative_instructions"]
        )

        self.trace_cfg = TraceConfig(
            enabled=_as_bool(self.args.get("trace_enabled", self.DEFAULTS["trace_enabled"])),
            copy_selected_frames=_as_bool(self.args.get("trace_copy_selected_frames", self.DEFAULTS["trace_copy_selected_frames"]), default=True),
            copy_best_frame=_as_bool(self.args.get("trace_copy_best_frame", self.DEFAULTS["trace_copy_best_frame"]), default=True),
            max_copies=int(self.args.get("trace_max_copies", self.DEFAULTS["trace_max_copies"])),
        )

        self.log_snapshot_events: bool = _as_bool(self.args.get("log_snapshot_events", self.DEFAULTS["log_snapshot_events"]), default=True)
        self.log_llm_events: bool = _as_bool(self.args.get("log_llm_events", self.DEFAULTS["log_llm_events"]), default=True)

        ai_conf = self.args.get("ai_provider_conf") or {}
        if not isinstance(ai_conf, dict):
            ai_conf = {}
        provider = str(ai_conf.get("provider", "openai") or "").strip().lower()
        api_key = str(ai_conf.get("api_key") or "").strip()
        if (self.ai_data_enabled or self.external_image_gen_enabled) and provider == "openai" and not api_key:
            raise ValueError("ai_provider_conf.api_key is required for ai_provider_conf.provider='openai'")
        if self.external_image_gen_enabled and not self.image_instructions:
            raise ValueError("image_instructions is required when external_image_gen_enabled is true")

        # internal state
        self._in_flight = False
        self._last_run_ts = 0.0
        self._data_provider = None
        self._active: Optional[_Run] = None

        # ensure directories exist on shared mount
        base = self._ha_path_to_local_fs(self.snapshot_ha_dir)
        (base).mkdir(parents=True, exist_ok=True)
        (base / self.bundle_runs_subdir).mkdir(parents=True, exist_ok=True)

        self.log(
            f"DetectionSummary[{self.bundle_key}]: trigger={self.trigger_entity_id} -> {self.trigger_to}, "
            f"camera={self.camera_entity_id}, base={self.snapshot_ha_dir}",
            level="INFO",
        )

        self.listen_state(self._on_trigger, self.trigger_entity_id, new=self.trigger_to)

        # Selected-run viewer wiring (optional)
        if self.run_picker_entity_id:
            # Serialize selected-run materialization so rapid picker changes can't interleave
            # "best" and "generated" file updates across different runs.
            self._materialize_lock = threading.Lock()
            self._materialize_seq = 0
            # Keep selected artifacts in sync when the user changes the picker.
            self.listen_state(self._on_run_picker_change, self.run_picker_entity_id)
            # Allow selecting a specific run via notification action buttons.
            self.listen_event(self._on_mobile_app_notification_action, "mobile_app_notification_action")
            # Keep picker options reasonably fresh even if no new runs are published.
            self.run_every(self._sync_run_picker_periodic, "now", 600)
            # Also sync shortly after startup; mounts/integrations may not be ready at init time.
            self.run_in(self._sync_run_picker_periodic, 2)
            self.run_in(self._sync_run_picker_periodic, 15)
            # Optional auto-reset to latest after inactivity.
            if float(self.selected_auto_reset_s) > 0:
                self.run_every(self._maybe_auto_reset_picker, "now", 60)

    def _ha_path_to_local_fs(self, ha_path: str) -> Path:
        remainder = _strip_posix_prefix(ha_path, "/media")
        if remainder is None:
            return Path(ha_path)
        return Path(self.media_fs_root) / remainder

    # --- selected-run viewer helpers ------------------------------------
    # Viewer-cache details live in `detection_summary_app/viewer_cache.py`.
    # This class only orchestrates: compute picker options → stage → refresh → repoint cameras.

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
                # fall back to on-disk bundle JSON
                if local_run_dir is None:
                    local_run_dir = (self._ha_path_to_local_fs(self.snapshot_ha_dir) / self.bundle_runs_subdir / run_id)
                p = local_run_dir / "summary.json"
                if p.exists():
                    import json as _json

                    parsed = _json.loads(p.read_text(encoding="utf-8"))
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
                f"DetectionSummary[{self.bundle_key}]: selected summary exceeded 255 chars; truncating run_id={run_id}",
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
                f"DetectionSummary[{self.bundle_key}]: update selected summary helper failed: {e!r}",
                level="WARNING",
            )

    def _select_run_from_www_cache(self, run_id: str) -> None:
        """
        Select a run by repointing the two selected local_file cameras to the staged
        files in `/config/www/.../<viewer_www_subdir>/`:
        - `<run_id>_best.jpg`
        - `<run_id>_generated.png`
        """
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
        # Expected format: GARAGE_DS_VIEW:<run_id>
        if not s.startswith("GARAGE_DS_VIEW:"):
            return None
        run_id = s.split(":", 1)[1].strip()
        return run_id or None

    def _resolve_run_id_from_options(self, token: str, options: list[str]) -> Optional[str]:
        """
        Resolve a run token to a full run_id present in picker options.

        We primarily expect full run_ids, but iOS notification action identifiers can be truncated,
        so we also support short UUID prefixes (e.g., first 8 chars) as long as the match is unique.
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
            if not self.run_picker_entity_id:
                return
            if not isinstance(data, dict):
                return
            run_id = self._parse_run_id_from_action(data.get("action"))
            if not run_id:
                return
            self.log(f"DetectionSummary[{self.bundle_key}]: notification action select run_id={run_id}", level="INFO")
            # Keep viewer/picker consistent: only select run_ids that are already staged.
            # Refresh the picker (which stages + refreshes the viewer folder) then select.
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
                    f"DetectionSummary[{self.bundle_key}]: notification action run_id not in picker options (not staged); ignoring run_id={run_id}",
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
                f"DetectionSummary[{self.bundle_key}]: notification action handler failed: {e!r}",
                level="WARNING",
            )

    def _on_run_picker_change(self, entity_id, attribute, old, new, kwargs) -> None:
        try:
            run_id = str(new or "").strip()
            if not run_id or run_id == str(old or "").strip():
                return
            self._selected_last_set_ts = time.time()
            self.log(f"DetectionSummary[{self.bundle_key}]: run picker changed -> {run_id}", level="INFO")
            # Selection should be instant: the picker only changes camera file paths.
            self._select_run_from_www_cache(run_id)
            self._update_selected_summary_text(run_id)
        except Exception as e:
            self.log(f"DetectionSummary[{self.bundle_key}]: run picker change failed: {e!r}", level="WARNING")

    def _maybe_auto_reset_picker(self, kwargs) -> None:
        try:
            if not self.run_picker_entity_id:
                return
            if float(self.selected_auto_reset_s) <= 0:
                return
            if float(self._selected_last_set_ts) <= 0:
                return
            if (time.time() - float(self._selected_last_set_ts)) < float(self.selected_auto_reset_s):
                return
            # Reset to latest (first option).
            self.call_service(
                "input_select/select_first",
                target={"entity_id": self.run_picker_entity_id},
            )
        except Exception:
            return

    def _sync_run_picker_periodic(self, kwargs) -> None:
        """
        Periodically prune old runs and keep the run picker options in sync with disk.
        """
        if not self.run_picker_entity_id:
            return
        max_retained_runs = int(self.args.get("max_retained_runs", 100))
        runs_dir = self._ha_path_to_local_fs(self.snapshot_ha_dir) / self.bundle_runs_subdir
        try:
            pruned = prune_runs_to_max(runs_dir=runs_dir, max_runs=int(max_retained_runs))
            options = recent_published_run_ids(runs_dir=runs_dir, max_options=int(self.run_picker_max_options))
            self.log(
                f"DetectionSummary[{self.bundle_key}]: sync run picker runs_dir={runs_dir} "
                f"max_retained_runs={int(max_retained_runs)} pruned={int(pruned)} options={len(options)}",
                level="INFO",
            )
            if not options:
                return
            # Stage required run_ids into `/media` and ask HA to wipe+fill `/config/www`
            # so the picker always matches what exists in the www viewer folder.
            if self._viewer_cache:
                staged = self._viewer_cache.stage_run_ids_to_media(options)
                if not staged:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: viewer staging produced no runs; skipping picker update",
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
                f"DetectionSummary[{self.bundle_key}]: sync run picker failed runs_dir={runs_dir}: {e!r}",
                level="WARNING",
            )
            return

    def _set_run_picker_value(self, run_id: str) -> None:
        """Ensure the picker contains run_id, then select it."""
        if not self.run_picker_entity_id:
            return
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
            self.log(f"DetectionSummary[{self.bundle_key}]: failed to set run picker: {e!r}", level="WARNING")

    def _add_run_id_to_picker(self, run_id: str) -> bool:
        """
        Add run_id to picker options (newest-first). Returns True if we selected it.
        """
        if not self.run_picker_entity_id:
            return False
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
            self.log(f"DetectionSummary[{self.bundle_key}]: failed to update run picker: {e!r}", level="WARNING")
        return False

    def _atomic_copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        # Important: do NOT preserve mtime/metadata.
        # HA/local_file camera refresh behavior is more reliable when the stable file's mtime changes.
        # (Similar to `cp` without `-p`.)
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)

    def _materialize_selected_run(self, run_id: str) -> None:
        """Copy per-run artifacts to stable selected filenames and update local_file cameras + helper text."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return
        # Sequence number used to cancel stale work when the picker changes rapidly.
        # We check this between copy steps to prevent mixed-run images.
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

        cfg = BundleConfig(
            snapshot_ha_dir=self.snapshot_ha_dir,
            bundle_runs_subdir=self.bundle_runs_subdir,
            bundle_best_filename=self.bundle_best_filename,
            external_generated_filename=self.external_generated_filename,
            published_best_filename=self.published_best_filename,
            published_generated_filename=self.published_generated_filename,
            write_bundle_json=self.write_bundle_json,
            trace=self.trace_cfg,
        )
        ha_run_dir = run_ha_dir(cfg, run_id)
        local_run_dir = self._ha_path_to_local_fs(ha_run_dir)

        best_src = local_run_dir / self.bundle_best_filename
        gen_src = local_run_dir / self.external_generated_filename

        base_local = self._ha_path_to_local_fs(self.snapshot_ha_dir)
        best_dst = base_local / self.selected_best_filename
        gen_dst = base_local / self.selected_generated_filename

        lock = getattr(self, "_materialize_lock", None)
        if lock is None:
            # Shouldn't happen, but keep behavior safe.
            lock = threading.Lock()
            self._materialize_lock = lock

        with lock:
            # If a newer selection arrived while we were waiting, abort.
            if int(getattr(self, "_materialize_seq", 0)) != my_seq:
                return

            self.log(
                f"DetectionSummary[{self.bundle_key}]: materialize selected start seq={my_seq} run_id={run_id} "
                f"run_dir={local_run_dir} best_src={_stat(best_src)} gen_src={_stat(gen_src)}",
                level="INFO",
            )

            best_copied = False
            gen_copied = False

            try:
                if best_src.exists():
                    self._atomic_copy(best_src, best_dst)
                    best_copied = True
                else:
                    # Fallback: derive best frame from summary.json and copy the captured frame.
                    best_idx = None
                    try:
                        p = local_run_dir / "summary.json"
                        if p.exists():
                            import json as _json

                            parsed = _json.loads(p.read_text(encoding="utf-8"))
                            if isinstance(parsed, dict):
                                best_idx = parsed.get("best_idx") or (parsed.get("summary") or {}).get("best_idx")
                    except Exception:
                        best_idx = None
                    if best_idx is not None:
                        alt = (local_run_dir / self.captured_subdir / f"frame_{int(best_idx):03d}.jpg")
                        if alt.exists():
                            self._atomic_copy(alt, best_dst)
                            best_copied = True
                            self.log(
                                f"DetectionSummary[{self.bundle_key}]: materialize selected best fallback "
                                f"seq={my_seq} run_id={run_id} best_idx={int(best_idx)} src={alt.name}",
                                level="WARNING",
                            )
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: materialize selected best failed seq={my_seq} run_id={run_id}: {e!r}",
                    level="WARNING",
                )

            # If a newer selection arrived, stop before touching generated to avoid mixed-run pairs.
            if int(getattr(self, "_materialize_seq", 0)) != my_seq:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: materialize selected abort before generated "
                    f"(newer selection) seq={my_seq} run_id={run_id}",
                    level="INFO",
                )
                return

            try:
                if gen_src.exists():
                    self._atomic_copy(gen_src, gen_dst)
                    gen_copied = True
                else:
                    # Fallback: avoid leaving a stale generated image from a different run.
                    # If generated.png is missing for this run, mirror best into the generated slot.
                    if best_copied and best_dst.exists():
                        self._atomic_copy(best_dst, gen_dst)
                        gen_copied = True
                        self.log(
                            f"DetectionSummary[{self.bundle_key}]: materialize selected generated missing; "
                            f"using best as fallback seq={my_seq} run_id={run_id}",
                            level="WARNING",
                        )
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: materialize selected generated failed seq={my_seq} run_id={run_id}: {e!r}",
                    level="WARNING",
                )

            self.log(
                f"DetectionSummary[{self.bundle_key}]: materialize selected done seq={my_seq} run_id={run_id} "
                f"best_dst={_stat(best_dst)} gen_dst={_stat(gen_dst)} copied_best={best_copied} copied_gen={gen_copied}",
                level="INFO",
            )

        # Update selected summary helper (prefer bundle store; fall back to summary.json).
        self._update_selected_summary_text(run_id, local_run_dir=local_run_dir)

    def _get_data_provider(self):
        if self._data_provider is not None:
            return self._data_provider
        cfg = data_provider_config_from_appdaemon_args(self.args)
        self._data_provider = build_data_provider(cfg)
        return self._data_provider

    def _on_trigger(self, entity_id, attribute, old, new, kwargs) -> None:
        now = time.time()
        if self._in_flight:
            return
        if self._effective_cooldown_s > 0 and (now - self._last_run_ts) < self._effective_cooldown_s:
            return

        run_id = str(uuid.uuid4())
        self._in_flight = True
        self._active = _Run(
            capture=CaptureState(
                run_id=run_id,
                started_ts=now,
                frames=[],
                capture_idx=0,
                last_motion_state=True,
                last_motion_change_ts=now,
                motion_on_total_s=0.0,
            )
        )
        self.fire_event(
            "detection_summary/run_started",
            bundle_key=self.bundle_key,
            run_id=run_id,
            started_ts=now,
            trigger_entity_id=self.trigger_entity_id,
            camera_entity_id=self.camera_entity_id,
        )
        self.log(
            f"DetectionSummary[{self.bundle_key}]: run_id={run_id} capturing while motion is ON; "
            f"stop after OFF for {self.off_grace_s:.0f}s (cap {self.capture_max_s:.0f}s)",
            level="INFO",
        )
        self.run_in(self._capture_tick, 0, run_id=run_id)

    def _capture_tick(self, kwargs) -> None:
        active = self._active
        if not active or kwargs.get("run_id") != active.capture.run_id:
            return

        now = time.time()
        motion_state = self.get_state(self.trigger_entity_id)
        motion_is_on = str(motion_state) == str(self.trigger_to)

        # Track motion-on duration separately from capture duration (off-grace adds buffer).
        if active.capture.last_motion_state is None:
            active.capture.last_motion_state = bool(motion_is_on)
            active.capture.last_motion_change_ts = now
        elif bool(motion_is_on) != bool(active.capture.last_motion_state):
            if active.capture.last_motion_state and active.capture.last_motion_change_ts is not None:
                active.capture.motion_on_total_s += max(0.0, now - float(active.capture.last_motion_change_ts))
            active.capture.last_motion_state = bool(motion_is_on)
            active.capture.last_motion_change_ts = now

        cap_cfg = CaptureConfig(
            snapshot_interval_s=self.snapshot_interval_s,
            off_grace_s=self.off_grace_s,
            capture_max_s=self.capture_max_s,
        )

        if should_stop_capture(now=now, cfg=cap_cfg, state=active.capture, motion_is_on=motion_is_on):
            ended = float(active.capture.ended_ts or now)
            # Finalize motion-on accumulation up to the point motion ended (or capture ended).
            if active.capture.last_motion_state and active.capture.last_motion_change_ts is not None:
                active.capture.motion_on_total_s += max(0.0, ended - float(active.capture.last_motion_change_ts))
                active.capture.last_motion_change_ts = ended
            self.fire_event(
                "detection_summary/run_capture_done",
                bundle_key=self.bundle_key,
                run_id=active.capture.run_id,
                captured_count=len(active.capture.frames),
                ended_ts=ended,
                timed_out=bool(active.capture.timed_out),
            )
            self._start_processing_thread(active)
            return

        if motion_is_on:
            i = int(active.capture.capture_idx)
            frame_name = f"frame_{i:03d}.jpg"
            ha_dir = run_ha_dir(
                BundleConfig(
                    snapshot_ha_dir=self.snapshot_ha_dir,
                    bundle_runs_subdir=self.bundle_runs_subdir,
                    bundle_best_filename=self.bundle_best_filename,
                    external_generated_filename=self.external_generated_filename,
                    published_best_filename=self.published_best_filename,
                    published_generated_filename=self.published_generated_filename,
                    write_bundle_json=self.write_bundle_json,
                    trace=self.trace_cfg,
                ),
                active.capture.run_id,
            )
            ha_path = f"{ha_dir}/{self.captured_subdir}/{frame_name}"
            try:
                self.call_service("camera/snapshot", entity_id=self.camera_entity_id, filename=ha_path)
                if self.log_snapshot_events:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: run_id={active.capture.run_id} captured {frame_name} -> {ha_path}",
                        level="INFO",
                    )
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: snapshot failed for {frame_name}: {e!r}",
                    level="WARNING",
                )
            active.capture.frames.append(CapturedFrame(idx=i, filename=frame_name, image_ha_path=ha_path, captured_ts=now))
            active.capture.capture_idx += 1

        delay = next_delay_s(cfg=cap_cfg, state=active.capture, motion_is_on=motion_is_on)
        self.run_in(self._capture_tick, delay, run_id=active.capture.run_id)

    def _start_processing_thread(self, run: _Run) -> None:
        self.log(
            f"DetectionSummary[{self.bundle_key}]: run_id={run.capture.run_id} capture complete "
            f"(captured_count={int(run.capture.capture_idx)} timed_out={bool(run.capture.timed_out)}); "
            f"starting background processing",
            level="INFO",
        )

        def _worker():
            self._process_background(run)

        t = threading.Thread(target=_worker, name=f"detection_summary_{self.bundle_key}_{run.capture.run_id[:8]}")
        t.daemon = True
        t.start()

    def _process_background(self, run: _Run) -> None:
        try:
            bundle = self._build_bundle(run)
            # bundle=None means "intentionally skipped" (e.g. no people)
            run.bundle = bundle
        except Exception as e:
            # Treat build failures like skipped runs: don't publish, and clean up the run directory.
            run.bundle = None
            try:
                cfg = BundleConfig(
                    snapshot_ha_dir=self.snapshot_ha_dir,
                    bundle_runs_subdir=self.bundle_runs_subdir,
                    bundle_best_filename=self.bundle_best_filename,
                    external_generated_filename=self.external_generated_filename,
                    published_best_filename=self.published_best_filename,
                    published_generated_filename=self.published_generated_filename,
                    write_bundle_json=self.write_bundle_json,
                    trace=self.trace_cfg,
                )
                local_run_dir = self._ha_path_to_local_fs(run_ha_dir(cfg, run.capture.run_id))
                delete_run_dir(local_run_dir)
            except Exception:
                pass
            self.log(f"DetectionSummary[{self.bundle_key}]: build failed run_id={run.capture.run_id}: {e!r}", level="WARNING")
        finally:
            self.run_in(self._finalize, 0, run_id=run.capture.run_id)

    def _build_bundle(self, run: _Run) -> Optional[dict[str, Any]]:
        cfg = BundleConfig(
            snapshot_ha_dir=self.snapshot_ha_dir,
            bundle_runs_subdir=self.bundle_runs_subdir,
            bundle_best_filename=self.bundle_best_filename,
            external_generated_filename=self.external_generated_filename,
            published_best_filename=self.published_best_filename,
            published_generated_filename=self.published_generated_filename,
            write_bundle_json=self.write_bundle_json,
            trace=self.trace_cfg,
        )

        run_id = run.capture.run_id
        ha_dir = run_ha_dir(cfg, run_id)
        local_run_dir = self._ha_path_to_local_fs(ha_dir)
        frames_dir = local_run_dir / self.captured_subdir

        # Score function (LLM)
        provider = self._get_data_provider()
        expected_keys = list(self.expected_keys or [])
        llm_events: list[dict[str, Any]] = []

        def score_one(i: int) -> tuple[int, ScoreResult, dict[str, Any]]:
            local_path = frames_dir / f"frame_{i:03d}.jpg"
            t0 = time.time()
            data: dict[str, Any] = {}
            try:
                if self.log_llm_events:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: LLM score start run_id={run_id} idx={i} path={local_path}",
                        level="INFO",
                    )
                # wait briefly for snapshot visibility on shared mount
                deadline = time.time() + 2.0
                while time.time() < deadline and not local_path.exists():
                    time.sleep(0.1)
                # Always append our required schema + guidance. This keeps `apps.yaml` prompts smaller
                # while ensuring we still get hardened structured output for scoring + image-gen facts.
                instr = str(self.data_instructions or "").strip()
                instr = (
                    instr
                    + "\n\nAdditional required fields:\n"
                    + "- male_count: integer count of men/boys visible in the frame (0 if none)\n"
                    + "- female_count: integer count of women/girls visible in the frame (0 if none)\n"
                    + "- animal_count: integer count of animals/pets visible in the frame (0 if none)\n"
                    + "\nScoring guidance:\n"
                    + "- If animals are present and clearly visible, increase frame_score appropriately.\n"
                    + "- face_score can be influenced by clearly visible faces of people OR animals.\n"
                    + "- pose should describe the main subject (person or animal) when possible.\n"
                    + "- summary should mention animals when they are relevant, but remain 1 sentence <= 140 characters.\n"
                )
                data = provider.generate_data_from_image(
                    input_image_path=str(local_path),
                    instructions=instr,
                    expected_keys=expected_keys,
                )
            except ExternalDataGenError as e:
                self.log(f"DetectionSummary[{self.bundle_key}]: data gen failed for {local_path}: {e!r}", level="WARNING")
            except Exception as e:
                self.log(f"DetectionSummary[{self.bundle_key}]: data gen error for {local_path}: {e!r}", level="WARNING")
            if not isinstance(data, dict):
                data = {}
            male_count = int(_safe_float(data.get("male_count"), default=0.0))
            female_count = int(_safe_float(data.get("female_count"), default=0.0))
            animal_count = int(_safe_float(data.get("animal_count"), default=0.0))
            person = _safe_float(data.get("person_score", data.get("score")), default=0.0)
            face = _safe_float(data.get("face_score"), default=0.0)
            frame = _safe_float(data.get("frame_score"), default=person)
            pose = str(data.get("pose") or "").strip().lower()
            summary = str(data.get("summary", "") or "").strip()
            if self.log_llm_events:
                elapsed = time.time() - t0
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: LLM score done run_id={run_id} idx={i} "
                    f"elapsed_s={elapsed:.3f} person={person:.2f} face={face:.2f} frame={frame:.2f} pose={pose!r} "
                    f"counts=(m={male_count},f={female_count},a={animal_count}) "
                    f"summary_preview={summary[:120]!r} keys={sorted(list(data.keys()))[:20]}",
                    level="INFO",
                )
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: LLM raw run_id={run_id} idx={i} data={data!r}",
                    level="DEBUG",
                )
            ev = {
                "type": "data",
                "frame_idx": i,
                "image_filename": f"frame_{i:03d}.jpg",
                "elapsed_s": round(time.time() - t0, 3),
                "model": (data.get("_meta") or {}).get("model") if isinstance(data.get("_meta"), dict) else None,
                "male_count": male_count,
                "female_count": female_count,
                "animal_count": animal_count,
                "person_score": person,
                "face_score": face,
                "frame_score": frame,
                "pose": pose,
                "summary_preview": summary[:160],
            }
            return i, ScoreResult(male_count, female_count, animal_count, person, face, frame, pose, summary, data), ev

        def score_index(i: int) -> ScoreResult:
            ii, res, ev = score_one(int(i))
            llm_events.append(ev)
            return res

        def score_indices(indices: list[int]) -> dict[int, ScoreResult]:
            out: dict[int, ScoreResult] = {}
            if not indices:
                return out
            # Bounded parallelism for provider calls
            from concurrent.futures import ThreadPoolExecutor, as_completed

            max_workers = max(1, int(self.external_data_parallelism))
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(score_one, int(i)): int(i) for i in indices}
                for fut in as_completed(futs):
                    ii, res, ev = fut.result()
                    out[int(ii)] = res
                    llm_events.append(ev)
            return out

        total_frames = int(run.capture.capture_idx)
        scored, meta = adaptive_select_and_score(
            total_frames=total_frames,
            budget=self.analyze_max_snapshots,
            score_index=score_index,
            score_indices=score_indices,
            seed=run_id,
            no_people_threshold=self.no_people_threshold,
        )
        if not isinstance(meta, SelectionMeta):
            # This should never happen. If it does, it usually indicates mixed code versions at runtime.
            try:
                import inspect

                sel_file = inspect.getsourcefile(adaptive_select_and_score) or "unknown"
            except Exception:
                sel_file = "unknown"
            if isinstance(meta, dict):
                meta_hint = f"dict keys={sorted(list(meta.keys()))[:20]}"
            else:
                meta_hint = f"attrs={sorted([a for a in dir(meta) if not a.startswith('_')])[:20]}"
            raise TypeError(
                f"adaptive_select_and_score returned unexpected meta type={type(meta)!r} ({meta_hint}) from {sel_file}"
            )
        best_idx = int(meta.best_idx)
        best_res = scored.get(best_idx)
        best_person = float(getattr(best_res, "person_score", 0.0) if best_res else 0.0)

        self.log(
            f"DetectionSummary[{self.bundle_key}]: selection run_id={run_id} captured={total_frames} "
            f"budget={int(self.analyze_max_snapshots)} scored={len(scored)} best_idx={best_idx} cutoff={meta.cutoff_idx_inclusive}",
            level="INFO",
        )
        self.log(
            f"DetectionSummary[{self.bundle_key}]: selection detail run_id={run_id} "
            f"probes={meta.probes} scored_indices={meta.scored_indices}",
            level="DEBUG",
        )

        # Write trace artifacts (optional)
        write_trace(
            local_run_dir=local_run_dir,
            frames_dir=frames_dir,
            scored=scored,
            meta=meta,
            best_idx=best_idx,
            cfg=self.trace_cfg,
        )

        # Publish bundles when we have meaningful subjects: people OR animals.
        if not should_publish_bundle(
            scored=scored,
            best_person_score=best_person,
            best_min_person_score=float(self.best_min_person_score),
            best_min_animal_count=int(self.best_min_animal_count),
        ):
            self.log(
                f"DetectionSummary[{self.bundle_key}]: run_id={run_id} no bundle generated "
                f"(best_person_score={best_person:.2f} < best_min_person_score={float(self.best_min_person_score):.2f} "
                f"and no animals detected); "
                f"skipping image generation + store publish",
                level="INFO",
            )
            # Delete empty/skipped runs so they don't show up in viewers/pickers.
            if delete_run_dir(local_run_dir):
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: deleted skipped run directory run_id={run_id} dir={local_run_dir}",
                    level="INFO",
                )
            return None

        # --- Run-level narrative summary (text-only LLM over per-frame facts) ---
        run_narrative: Optional[dict[str, Any]] = None
        try:
            if self.run_narrative_enabled:
                # Gather a compact chronological list of scored frame facts with timestamps.
                idx_to_frame = {int(f.idx): f for f in (run.capture.frames or []) if getattr(f, "idx", None) is not None}
                facts: list[dict[str, Any]] = []
                for idx in sorted([int(i) for i in meta.scored_indices]):
                    fr = scored.get(int(idx))
                    cap = idx_to_frame.get(int(idx))
                    if not fr or not cap:
                        continue
                    t_s = max(0.0, float(getattr(cap, "captured_ts", 0.0) or 0.0) - float(run.capture.started_ts))
                    try:
                        person_count = max(0, int(getattr(fr, "male_count", 0) or 0) + int(getattr(fr, "female_count", 0) or 0))
                    except Exception:
                        person_count = 0
                    facts.append(
                        {
                            "idx": int(idx),
                            "t_s": round(float(t_s), 3),
                            "summary": str(getattr(fr, "summary", "") or "").strip(),
                            "pose": str(getattr(fr, "pose", "") or "").strip(),
                            "person_count": int(person_count),
                            "person_score": float(getattr(fr, "person_score", 0.0) or 0.0),
                            "face_score": float(getattr(fr, "face_score", 0.0) or 0.0),
                            "frame_score": float(getattr(fr, "frame_score", 0.0) or 0.0),
                        }
                    )
                run_narrative = synthesize_run_narrative(
                    provider=provider,
                    run_id=run_id,
                    bundle_key=self.bundle_key,
                    frame_facts=facts,
                    instructions=self.run_narrative_instructions,
                    cfg=NarrativeConfig(enabled=True, max_chars=int(self.run_narrative_max_chars)),
                )
                if run_narrative:
                    narrative_meta = run_narrative.get("_narrative_meta") if isinstance(run_narrative, dict) else None
                    if isinstance(narrative_meta, dict) and narrative_meta.get("was_truncated"):
                        self.log(
                            f"DetectionSummary[{self.bundle_key}]: run narrative truncated run_id={run_id} "
                            f"original_len={narrative_meta.get('original_len')} max_chars={narrative_meta.get('max_chars')}",
                            level="WARNING",
                        )
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: run narrative done run_id={run_id} "
                        f"len={len(str(run_narrative.get('run_summary') or ''))} conf={run_narrative.get('confidence')}",
                        level="INFO",
                    )
        except Exception as e:
            self.log(f"DetectionSummary[{self.bundle_key}]: run narrative failed: {e!r}", level="WARNING")

        # Create best.jpg for this run
        best_src = frames_dir / f"frame_{best_idx:03d}.jpg"
        best_dst = local_run_dir / self.bundle_best_filename
        if best_src.exists():
            best_dst.write_bytes(best_src.read_bytes())

        # Mirror best.jpg to a stable path under the zone dir (for a local_file camera to point at).
        try:
            stable_best_local = self._ha_path_to_local_fs(stable_best_ha_path(cfg))
            stable_best_local.parent.mkdir(parents=True, exist_ok=True)
            if best_dst.exists():
                stable_best_local.write_bytes(best_dst.read_bytes())
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: mirrored best run_id={run_id} stable={stable_best_local}",
                    level="INFO",
                )
        except Exception as e:
            self.log(f"DetectionSummary[{self.bundle_key}]: failed to mirror best image: {e!r}", level="WARNING")

        # Generate image from best.jpg to per-run generated.png, then mirror to stable
        generated_image: Optional[dict[str, Any]] = None
        population_bounds = compute_population_bounds(scored)
        if self.external_image_gen_enabled:
            out_path = local_run_dir / self.external_generated_filename
            # wait for best to exist
            if self.external_image_gen_wait_for_best_s > 0:
                deadline = time.time() + float(self.external_image_gen_wait_for_best_s)
                while time.time() < deadline and not best_dst.exists():
                    time.sleep(0.2)

            def _pick_best_idx_with_max(sc: dict[int, ScoreResult], get_count) -> Optional[int]:
                if not sc:
                    return None
                try:
                    max_val = max(int(get_count(r) or 0) for r in sc.values())
                except Exception:
                    return None
                if int(max_val) <= 0:
                    return None
                cands: list[tuple[int, ScoreResult]] = []
                for ii, rr in sc.items():
                    try:
                        if int(get_count(rr) or 0) == int(max_val):
                            cands.append((int(ii), rr))
                    except Exception:
                        continue
                if not cands:
                    return None
                return max(
                    cands,
                    key=lambda t: (
                        float(getattr(t[1], "frame_score", 0.0) or 0.0),
                        float(getattr(t[1], "face_score", 0.0) or 0.0),
                        float(getattr(t[1], "person_score", 0.0) or 0.0),
                    ),
                )[0]

            # Provide multiple reference frames to reduce "phantom" additions:
            # - best overall frame (always)
            # - best-scoring frame that contains the max animals / max males / max females (deduped)
            best_animals_idx = _pick_best_idx_with_max(scored, lambda r: getattr(r, "animal_count", 0))
            best_males_idx = _pick_best_idx_with_max(scored, lambda r: getattr(r, "male_count", 0))
            best_females_idx = _pick_best_idx_with_max(scored, lambda r: getattr(r, "female_count", 0))

            candidate_idxs: list[int] = [int(best_idx)]
            for extra in (best_animals_idx, best_males_idx, best_females_idx):
                if extra is None:
                    continue
                ii = int(extra)
                if ii not in candidate_idxs:
                    candidate_idxs.append(ii)

            # Ensure we send more than one reference when we have more than one scored frame.
            # This helps prevent "phantom" subjects when the best frame is missing a transient subject.
            min_refs = 2
            max_refs = 4

            def _ref_rank(res: ScoreResult) -> tuple:
                # Similar spirit to selection._pick_key, tuned for reference quality.
                has_subject = 1 if (int(getattr(res, "animal_count", 0) or 0) > 0 or float(getattr(res, "person_score", 0.0) or 0.0) > 0) else 0
                has_summary = 1 if (str(getattr(res, "summary", "") or "").strip()) else 0
                return (
                    has_subject,
                    float(getattr(res, "frame_score", 0.0) or 0.0),
                    float(getattr(res, "face_score", 0.0) or 0.0),
                    float(getattr(res, "person_score", 0.0) or 0.0),
                    int(getattr(res, "animal_count", 0) or 0),
                    has_summary,
                )

            if len(scored) > 1 and len(candidate_idxs) < min_refs:
                ranked = sorted([(int(ii), rr) for ii, rr in (scored or {}).items() if rr is not None], key=lambda t: _ref_rank(t[1]), reverse=True)
                for ii, _rr in ranked:
                    if ii not in candidate_idxs:
                        candidate_idxs.append(int(ii))
                    if len(candidate_idxs) >= min_refs:
                        break

            if len(candidate_idxs) > max_refs:
                candidate_idxs = candidate_idxs[:max_refs]

            # Build input paths (best.jpg + additional captured frames).
            input_paths: list[Path] = []
            for ii in candidate_idxs:
                p = best_dst if int(ii) == int(best_idx) else (frames_dir / f"frame_{int(ii):03d}.jpg")
                if p.exists():
                    input_paths.append(p)
                else:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: image gen missing candidate frame idx={int(ii)} path={p}",
                        level="WARNING",
                    )
            if not input_paths and best_dst.exists():
                input_paths = [best_dst]

            if input_paths:
                try:
                    # TODO(future): Add a "prompt-writer" step (LLM) that generates the image-edit prompt.
                    # Requirement: maximize style/theme variety across runs without anchoring on hard-coded examples,
                    # while keeping contents consistent with the chosen best frame.
                    provider_cfg = provider_config_from_appdaemon_args(self.args)
                    img_provider = build_image_provider(provider_cfg)
                    if not getattr(img_provider, "capabilities", None) or not img_provider.capabilities.supports_image_to_image:
                        raise ExternalImageGenError("image provider does not support image-to-image")

                    # Narrative context is helpful, but should not be treated as a "hard rule" about subject count.
                    narrative_text = ""
                    if isinstance(run_narrative, dict):
                        narrative_text = str(run_narrative.get("run_summary") or "").strip()

                    # Compact per-frame notes for the images we are providing.
                    idx_to_frame = {int(f.idx): f for f in (run.capture.frames or []) if getattr(f, "idx", None) is not None}
                    notes: list[str] = []
                    for ii in sorted({int(best_idx)} | {int(i) for i in candidate_idxs}):
                        rr = scored.get(int(ii))
                        if not rr:
                            continue
                        cap = idx_to_frame.get(int(ii))
                        t_s = None
                        if cap is not None:
                            try:
                                t_s = max(0.0, float(getattr(cap, "captured_ts", 0.0) or 0.0) - float(run.capture.started_ts))
                            except Exception:
                                t_s = None
                        summary = str(getattr(rr, "summary", "") or "").strip()
                        try:
                            m = int(getattr(rr, "male_count", 0) or 0)
                            f = int(getattr(rr, "female_count", 0) or 0)
                            a = int(getattr(rr, "animal_count", 0) or 0)
                        except Exception:
                            m, f, a = 0, 0, 0
                        time_part = f" t={t_s:.1f}s" if isinstance(t_s, (int, float)) else ""
                        notes.append(f"- frame_{int(ii):03d}.jpg{time_part}: {summary or '(no summary)'} (m={m}, f={f}, animals={a})")

                    base_prompt = augment_image_instructions(str(self.image_instructions), population_bounds)
                    prompt_lines: list[str] = [base_prompt, ""]
                    prompt_lines.extend(
                        [
                            "Reference frames:",
                            f"- You are provided {len(input_paths)} image(s) captured close in time during ONE motion detection event.",
                            "- These frames are only a subset of the event; people/animals may enter/leave between frames.",
                            "",
                            "Critical constraints:",
                            "- ONLY include people and animals that are clearly present in at least ONE of the provided reference frames.",
                            "- Do NOT invent/add animals or extra people that are not visible in any provided frame (avoid 'phantom' animals).",
                            "- If you are uncertain whether a subject exists, OMIT it rather than hallucinating it.",
                            "- Any counts mentioned elsewhere (summaries/analysis) are soft context, not hard rules for adding subjects.",
                            "",
                            "Scene composition guidance:",
                            "- Generate ONE coherent illustration that captures the essence of what happened across the provided frames.",
                            "- Exact positioning/poses do not need to match a single frame; it can be a composite of the event.",
                            "- Use the narrative context below for mood/intent, but do not add subjects that are not visible in the frames.",
                        ]
                    )
                    if narrative_text:
                        prompt_lines.extend(["", "Narrative context:", narrative_text])
                    if notes:
                        prompt_lines.extend(["", "Frame notes (for the provided references):", *notes])

                    prompt = "\n".join([ln.rstrip() for ln in prompt_lines]).strip()
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: image gen start run_id={run_id} "
                        f"inputs={len(input_paths)} out={out_path} prompt_len={len(prompt)}",
                        level="INFO",
                    )
                    generated_image = img_provider.edit_image(
                        input_image_paths=[str(p) for p in input_paths],
                        prompt=prompt,
                        output_image_path=str(out_path),
                    )
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: image gen done run_id={run_id} "
                        f"elapsed_s={(generated_image or {}).get('elapsed_s')} model={(generated_image or {}).get('model')} "
                        f"output_exists={out_path.exists()}",
                        level="INFO",
                    )
                    llm_events.append(
                        {
                            "type": "image_edit",
                            "input_paths": [str(p) for p in input_paths],
                            "output_path": str(out_path),
                            "elapsed_s": (generated_image or {}).get("elapsed_s"),
                            "model": (generated_image or {}).get("model"),
                        }
                    )
                    # mirror to stable filename under zone dir
                    stable_local = self._ha_path_to_local_fs(stable_generated_ha_path(cfg))
                    stable_local.parent.mkdir(parents=True, exist_ok=True)
                    if out_path.exists():
                        stable_local.write_bytes(out_path.read_bytes())
                        generated_image["output_path"] = str(stable_local)
                        self.log(
                            f"DetectionSummary[{self.bundle_key}]: image gen mirrored run_id={run_id} stable={stable_local}",
                            level="INFO",
                        )
                except ExternalImageGenError as e:
                    self.log(f"DetectionSummary[{self.bundle_key}]: image generation failed: {e!r}", level="WARNING")

        # best image url is set in finalize after updating local_file camera
        if not isinstance(meta, SelectionMeta):
            raise TypeError(f"internal error: selection meta was overwritten: type={type(meta)!r}")
        bundle = build_bundle_dict(
            bundle_key=self.bundle_key,
            camera_entity_id=self.camera_entity_id,
            trigger_entity_id=self.trigger_entity_id,
            run_id=run_id,
            capture=run.capture,
            scored=scored,
            selection_meta=meta,
            best_idx=best_idx,
            best_image_url="",
            generated_image=generated_image,
            run_narrative=run_narrative,
            cfg=cfg,
            llm_events=llm_events,
            population_bounds=population_bounds,
        )
        maybe_write_bundle_json(local_run_dir=local_run_dir, bundle=bundle, enabled=self.write_bundle_json)
        return bundle

    def _finalize(self, kwargs) -> None:
        active = self._active
        if not active or kwargs.get("run_id") != active.capture.run_id:
            return
        try:
            # If we skipped bundle generation (e.g. no people), do not publish.
            if active.bundle is None:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: run_id={active.capture.run_id} finalized with no bundle (skipped)",
                    level="INFO",
                )
                self._effective_cooldown_s = float(self.cooldown_s)
                return

            bundle = active.bundle or {}
            gen = bundle.get("generated_image") if isinstance(bundle, dict) else None
            best = bundle.get("best") if isinstance(bundle, dict) else None

            # Do not call HA services for local_file cameras here.
            # The cameras point to stable paths; we only overwrite the underlying files.
            if isinstance(gen, dict) and self.generated_image_camera_entity_id:
                gen["image_url"] = f"/api/camera_proxy/{self.generated_image_camera_entity_id}"
            if isinstance(best, dict) and self.best_image_camera_entity_id:
                best["image_url"] = f"/api/camera_proxy/{self.best_image_camera_entity_id}"

            DETECTION_SUMMARY_STORE.publish_bundle(self.bundle_key, bundle)

            # Event for consumers
            # Prefer run narrative summary when present; fall back to best-frame summary.
            run_text = ""
            if isinstance(bundle, dict):
                rn = bundle.get("run_narrative")
                if isinstance(rn, dict):
                    run_text = str(rn.get("run_summary") or "").strip()
            summary = run_text or (((bundle.get("best") or {}).get("summary") if isinstance(bundle, dict) else "") or "")
            created_at = float(bundle.get("created_at_epoch", time.time())) if isinstance(bundle, dict) else time.time()
            gen_url = ""
            if isinstance(gen, dict):
                gen_url = str(gen.get("image_url") or "")
            if isinstance(bundle, dict):
                best = bundle.get("best") or {}
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: bundle run_id={active.capture.run_id} "
                    f"best_summary_len={len(str(best.get('summary') or ''))} "
                    f"person={best.get('person_score')} face={best.get('face_score')} frame={best.get('frame_score')} "
                    f"generated_url={(gen_url or '')!r}",
                    level="INFO",
                )
            self.fire_event(
                "detection_summary/run_published",
                bundle_key=self.bundle_key,
                run_id=active.capture.run_id,
                created_at_epoch=created_at,
                summary=summary,
                generated_image_url=gen_url,
            )

            # Optionally publish summary text into a helper for dashboards/mobile deep-links.
            if self.summary_text_entity_id:
                try:
                    value = str(summary or "").strip()
                    # input_text max is 255 (HA enforced); truncate defensively.
                    if len(value) > 255:
                        value = value[:252] + "..."
                    self.call_service(
                        "input_text/set_value",
                        entity_id=self.summary_text_entity_id,
                        value=value,
                    )
                except Exception as e:
                    self.log(f"DetectionSummary[{self.bundle_key}]: failed to update summary_text_entity_id: {e!r}", level="WARNING")

            # Retention + run picker sync (newest-first, bounded).
            if self.run_picker_entity_id:
                try:
                    # Refresh viewer + picker so the newest run is present under `/config/www/.../viewer/`
                    # *before* we select it (prevents missing-file errors on mobile).
                    self._sync_run_picker_periodic({})
                    # Prefer selecting the newly published run_id when it's available.
                    try:
                        st = self.get_state(self.run_picker_entity_id, attribute="all") or {}
                        attrs = st.get("attributes") if isinstance(st, dict) else None
                        options = (attrs.get("options") if isinstance(attrs, dict) else None) or []
                        options = [str(o) for o in options] if isinstance(options, list) else []
                    except Exception:
                        options = []
                    if active.capture.run_id in options:
                        self._selected_last_set_ts = time.time()
                        self.call_service(
                            "input_select/select_option",
                            target={"entity_id": self.run_picker_entity_id},
                            option=active.capture.run_id,
                        )
                except Exception as e:
                    self.log(f"DetectionSummary[{self.bundle_key}]: run picker sync failed: {e!r}", level="WARNING")

            # Cooldown backoff behavior
            if active.capture.timed_out:
                self._effective_cooldown_s = min(
                    float(self.cooldown_backoff_max_s),
                    max(float(self.cooldown_s), float(self._effective_cooldown_s) * 2.0),
                )
            else:
                self._effective_cooldown_s = float(self.cooldown_s)

            best_file = ((bundle.get("debug") or {}).get("selection_meta") or {}).get("best_idx") if isinstance(bundle, dict) else None
            self.log(
                f"DetectionSummary[{self.bundle_key}]: published run_id={active.capture.run_id} "
                f"best_idx={best_file} cooldown={self._effective_cooldown_s:.0f}s",
                level="INFO",
            )
        finally:
            self._last_run_ts = float(active.capture.started_ts)
            self._in_flight = False
            self._active = None

