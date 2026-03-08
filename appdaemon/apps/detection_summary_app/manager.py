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

import json
import math
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
from .population import compute_population_bounds, compute_population_consensus
from .narrative import NarrativeConfig, synthesize_run_narrative
from .publish_gate import should_publish_bundle
from .retention import delete_run_dir
from .profiles import (
    DetectionProfile,
    load_default_profile,
    load_profile_by_name,
    load_profile_from_dict,
)
from .prompting import (
    ScorePromptBuilder,
    ImagePromptBuilder,
    normalize_score_data,
    default_score_schema,
    schema_from_profile,
)
try:
    from providers.ai_providers.registry import (
        build_image_provider,
        build_multimodal_text_provider,
        build_simple_text_provider,
        multimodal_text_config_from_appdaemon_args,
        provider_config_from_appdaemon_args,
        simple_text_config_from_appdaemon_args,
    )
    from providers.ai_providers.types import ExternalDataGenError, ExternalImageGenError
    from providers.ha_provisioner import HAProvisioner
except Exception:  # pragma: no cover
    import sys

    # AppDaemon often only adds `appdaemon/apps` to sys.path. Our shared libraries
    # live at `appdaemon/providers`, so add the AppDaemon root directory.
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from providers.ai_providers.registry import (  # type: ignore
        build_image_provider,
        build_multimodal_text_provider,
        build_simple_text_provider,
        multimodal_text_config_from_appdaemon_args,
        provider_config_from_appdaemon_args,
        simple_text_config_from_appdaemon_args,
    )
    from providers.ai_providers.types import ExternalDataGenError, ExternalImageGenError  # type: ignore
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


def _provider_requires_api_key(provider: str) -> bool:
    return str(provider or "").strip().lower() in {"openai", "gemini"}


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


def _compute_backoff_cooldown(
    bundle_generated: bool,
    now: float,
    last_image_ts: float,
    prev_cooldown_s: float,
    base_cooldown_s: float,
    window_n: float,
    max_cooldown_s: float,
) -> tuple[float, str]:
    if not bundle_generated:
        return base_cooldown_s, "skipped_run"
    if last_image_ts <= 0.0:
        return base_cooldown_s, "first_image"
    delta = now - last_image_ts
    window = window_n * prev_cooldown_s
    if delta <= window:
        new_cd = min(max_cooldown_s, prev_cooldown_s * 2.0)
        return new_cd, f"backed_off delta={delta:.1f}s <= window={window:.1f}s"
    return base_cooldown_s, f"reset delta={delta:.1f}s > window={window:.1f}s"


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
        "cooldown_s": 150,
        "cooldown_backoff_max_s": 1800,
        "cooldown_backoff_window_n": 3,
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
        "debug_preserve_run_dirs": False,
        # local_file camera for stable generated
        "generated_image_camera_entity_id": None,
        # Optional: local_file camera for the *best* image.
        "best_image_camera_entity_id": None,
        # Optional: HA helper (input_text) to store the most recent summary text.
        "summary_text_entity_id": None,
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

        # Load detection profile
        profile_ref = self.args.get("detection_profile")
        if profile_ref is None:
            self._profile: DetectionProfile = load_default_profile()
        elif isinstance(profile_ref, str):
            self._profile = load_profile_by_name(profile_ref)
        elif isinstance(profile_ref, dict):
            self._profile = load_profile_from_dict(profile_ref)
        else:
            self._profile = load_default_profile()

        # Schema-driven scoring; prompt builders own policy.
        self._score_schema = schema_from_profile(self._profile)
        self._score_prompt_builder = ScorePromptBuilder(schema=self._score_schema, profile=self._profile)
        self._image_prompt_builder = ImagePromptBuilder()

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
        self.cooldown_backoff_window_n: float = _safe_float(
            self.args.get("cooldown_backoff_window_n", self.DEFAULTS["cooldown_backoff_window_n"]),
            default=3.0,
        )
        self._effective_cooldown_s: float = float(self.cooldown_s)
        self._last_image_generated_ts: float = 0.0

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

        self.media_fs_root = str(self.args.get("media_fs_root", self.DEFAULTS["media_fs_root"])).rstrip("/") or "/media"

        self.write_bundle_json: bool = _as_bool(self.args.get("write_bundle_json", self.DEFAULTS["write_bundle_json"]), default=True)
        self.debug_preserve_run_dirs: bool = _as_bool(
            self.args.get("debug_preserve_run_dirs", self.DEFAULTS["debug_preserve_run_dirs"]),
            default=False,
        )
        self.generated_image_camera_entity_id: Optional[str] = hass_entities.get("generated_image_camera_entity_id")
        self.best_image_camera_entity_id: Optional[str] = hass_entities.get("best_image_camera_entity_id")

        # Helper entity ID: auto-derive from bundle_key when not explicitly provided.
        self.summary_text_entity_id: Optional[str] = (
            hass_entities.get("summary_text_entity_id")
            or f"input_text.{self.bundle_key}_detection_summary"
        )

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
        # Capability-pointer config (bundle refs): simple_text/multimodal/image may be
        # either a plain string ref or a dict like {bundle: "...", base_url: ...}.
        is_pointer_config = any(
            (
                isinstance(ai_conf.get(k), str) and str(ai_conf.get(k)).strip()
            )
            or (
                isinstance(ai_conf.get(k), dict)
                and str((ai_conf.get(k) or {}).get("bundle") or (ai_conf.get(k) or {}).get("ref") or "").strip()
            )
            for k in ("simple_text", "multimodal", "image")
        )
        provider = str(ai_conf.get("provider", "openai") or "").strip().lower()
        api_key_env = str(ai_conf.get("api_key_env") or "").strip()
        api_key = str(ai_conf.get("api_key") or "").strip()
        needs_text_provider = bool(self.ai_data_enabled or self.run_narrative_enabled)
        # Inline config requires api_key_env; pointer config gets credentials from bundle resolution
        if not is_pointer_config:
            if needs_text_provider and _provider_requires_api_key(provider) and not (api_key_env or api_key):
                raise ValueError(
                    f"ai_provider_conf.api_key_env is required for ai_provider_conf.provider={provider!r} "
                    "when AI scoring or run narrative is enabled"
                )
            if self.external_image_gen_enabled and _provider_requires_api_key(provider) and not (api_key_env or api_key):
                raise ValueError(
                    f"ai_provider_conf.api_key_env is required for ai_provider_conf.provider={provider!r} "
                    "when external image generation is enabled"
                )
        if self.external_image_gen_enabled and not self.image_instructions:
            raise ValueError("image_instructions is required when external_image_gen_enabled is true")

        # internal state
        self._in_flight = False
        self._last_run_ts = 0.0
        self._multimodal_provider = None
        self._simple_text_provider = None
        self._active: Optional[_Run] = None

        if self.ai_data_enabled:
            self._get_multimodal_provider()
        if self.run_narrative_enabled:
            self._get_simple_text_provider()
        if self.external_image_gen_enabled:
            img_cfg = provider_config_from_appdaemon_args(self.args)
            image_provider = build_image_provider(img_cfg)
            if not getattr(image_provider, "capabilities", None) or not getattr(
                image_provider.capabilities, "supports_image_to_image", False
            ):
                raise ValueError(
                    f"ai_provider_conf provider={img_cfg.provider!r} does not support image-to-image generation"
                )

        # ensure directories exist on shared mount
        base = self._ha_path_to_local_fs(self.snapshot_ha_dir)
        (base).mkdir(parents=True, exist_ok=True)
        (base / self.bundle_runs_subdir).mkdir(parents=True, exist_ok=True)

        self.log(
            f"DetectionSummary[{self.bundle_key}]: trigger={self.trigger_entity_id} -> {self.trigger_to}, "
            f"camera={self.camera_entity_id}, base={self.snapshot_ha_dir}, "
            f"debug_preserve_run_dirs={self.debug_preserve_run_dirs}",
            level="INFO",
        )

        # Schedule async startup: provisions HA entities then wires all listeners.
        self.run_in(self._async_startup_wrapper, 0)

    def _ha_path_to_local_fs(self, ha_path: str) -> Path:
        remainder = _strip_posix_prefix(ha_path, "/media")
        if remainder is None:
            return Path(ha_path)
        return Path(self.media_fs_root) / remainder

    def _patch_summary_json_cooldown(self, run_id: str, cooldown_state: dict) -> None:
        """Patch an existing summary.json with cooldown_state after _finalize computes the new cooldown."""
        if not self.write_bundle_json:
            return
        local_run_dir = (
            self._ha_path_to_local_fs(self.snapshot_ha_dir) / self.bundle_runs_subdir / run_id
        )
        summary_path = local_run_dir / "summary.json"
        if not summary_path.exists():
            self.log(
                f"DetectionSummary[{self.bundle_key}]: _patch_summary_json_cooldown: summary.json missing for run_id={run_id}",
                level="WARNING",
            )
            return
        try:
            existing = json.loads(summary_path.read_text(encoding="utf-8"))
            existing["cooldown_state"] = cooldown_state
            summary_path.write_text(json.dumps(existing, indent=2, sort_keys=False), encoding="utf-8")
        except Exception as e:
            self.log(
                f"DetectionSummary[{self.bundle_key}]: _patch_summary_json_cooldown failed for run_id={run_id}: {e!r}",
                level="WARNING",
            )

    # --- async startup --------------------------------------------------

    def _async_startup_wrapper(self, kwargs) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._provision_entities()
        # Wire the trigger listener after provisioning so the summary_text helper exists.
        self.listen_state(self._on_trigger, self.trigger_entity_id, new=self.trigger_to)

    async def _provision_entities(self) -> None:
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            self.log(
                f"DetectionSummary[{self.bundle_key}]: ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        bk = self.bundle_key
        bk_display = bk.replace("_", " ").title()
        prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)

        # Provision only the latest summary-text helper.
        # The run picker, selected summary, and relay script are provisioned by detection_summary_viewer.
        try:
            created = await prov.ensure_helper("input_text", f"{bk_display} Detection Summary", max=255)
            level = "INFO" if created else "DEBUG"
            slug = prov._helper_slug("input_text", f"{bk_display} Detection Summary")
            entity_id = f"input_text.{slug}"
            msg = "created" if created else "already exists"
            self.log(f"DetectionSummary[{bk}]: helper {entity_id} {msg}", level=level)
        except Exception as exc:
            self.log(
                f"DetectionSummary[{bk}]: failed to provision input_text '{bk_display} Detection Summary': {exc!r}",
                level="ERROR",
            )

    def _get_multimodal_provider(self):
        if self._multimodal_provider is not None:
            return self._multimodal_provider
        cfg = multimodal_text_config_from_appdaemon_args(self.args)
        self._multimodal_provider = build_multimodal_text_provider(cfg)
        return self._multimodal_provider

    def _get_simple_text_provider(self):
        if self._simple_text_provider is not None:
            return self._simple_text_provider
        cfg = simple_text_config_from_appdaemon_args(self.args)
        self._simple_text_provider = build_simple_text_provider(cfg)
        return self._simple_text_provider

    def _on_trigger(self, entity_id, attribute, old, new, kwargs) -> None:
        now = time.time()
        if self._in_flight:
            return
        if self._effective_cooldown_s > 0 and (now - self._last_run_ts) < self._effective_cooldown_s:
            elapsed = now - self._last_run_ts
            remaining = self._effective_cooldown_s - elapsed
            self.log(
                f"DetectionSummary[{self.bundle_key}]: trigger SUPPRESSED by cooldown\n"
                f"  entity={self.trigger_entity_id}\n"
                f"  elapsed={elapsed:.1f}s remaining={remaining:.1f}s\n"
                f"  effective_cooldown={self._effective_cooldown_s:.0f}s (base={self.cooldown_s:.0f}s)",
                level="WARNING",
            )
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
                if self.debug_preserve_run_dirs:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: preserving failed run directory "
                        f"run_id={run.capture.run_id} dir={local_run_dir}",
                        level="WARNING",
                    )
                else:
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

        # Score function (LLM) — uses multimodal provider (image+text -> JSON)
        multimodal_provider = self._get_multimodal_provider() if self.ai_data_enabled else None
        expected_keys = self._score_schema.expected_keys()
        llm_events: list[dict[str, Any]] = []

        def score_one(i: int) -> tuple[int, ScoreResult, dict[str, Any]]:
            local_path = frames_dir / f"frame_{i:03d}.jpg"
            t0 = time.time()
            data: dict[str, Any] = {}
            try:
                if not self.ai_data_enabled:
                    data = {}
                else:
                    if self.log_llm_events:
                        self.log(
                            f"DetectionSummary[{self.bundle_key}]: LLM score start run_id={run_id} idx={i} path={local_path}",
                            level="INFO",
                        )
                    # wait briefly for snapshot visibility on shared mount
                    deadline = time.time() + 2.0
                    while time.time() < deadline and not local_path.exists():
                        time.sleep(0.1)
                    instr = self._score_prompt_builder.build(self.data_instructions)
                    data = multimodal_provider.generate_from_image(
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
            res = normalize_score_data(data, schema=self._score_schema)
            male_count = res.male_count
            female_count = res.female_count
            animal_count = res.animal_count
            person = res.person_score
            face = res.face_score
            frame = res.frame_score
            pose = res.pose
            summary = res.summary
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
            return i, res, ev

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
            profile=self._profile,
        ):
            self.log(
                f"DetectionSummary[{self.bundle_key}]: run_id={run_id} no bundle generated "
                f"(best_person_score={best_person:.2f} < best_min_person_score={float(self.best_min_person_score):.2f} "
                f"and no animals detected); "
                f"skipping image generation + store publish",
                level="INFO",
            )
            # Delete empty/skipped runs so they don't show up in viewers/pickers unless debugging.
            if self.debug_preserve_run_dirs:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: preserving skipped run directory "
                    f"run_id={run_id} dir={local_run_dir}",
                    level="WARNING",
                )
            elif delete_run_dir(local_run_dir):
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
                simple_text_provider = self._get_simple_text_provider()
                run_narrative = synthesize_run_narrative(
                    provider=simple_text_provider,
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
        consensus_bounds = compute_population_consensus(scored, self._profile)
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
                    if not getattr(img_provider, "capabilities", None) or not getattr(
                        img_provider.capabilities, "supports_image_to_image", False
                    ):
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

                    prompt = self._image_prompt_builder.build(
                        base_instructions=str(self.image_instructions),
                        population_bounds=population_bounds,
                        narrative_text=narrative_text,
                        frame_notes=notes if notes else None,
                        input_paths_count=len(input_paths),
                        bundle_augmentation=getattr(provider_cfg, "image_prompt_augmentation", None),
                        style_profile_id=getattr(provider_cfg, "style_profile", None),
                        environment_variant_id=getattr(provider_cfg, "style_variant_profile", None),
                        consensus_bounds=consensus_bounds,
                        profile=self._profile,
                    )
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
            profile=self._profile,
        )
        maybe_write_bundle_json(local_run_dir=local_run_dir, bundle=bundle, enabled=self.write_bundle_json)
        return bundle

    def _finalize(self, kwargs) -> None:
        active = self._active
        if not active or kwargs.get("run_id") != active.capture.run_id:
            return
        try:
            if active.bundle is None:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: run_id={active.capture.run_id} finalized with no bundle (skipped)",
                    level="INFO",
                )
                new_cd, reason = _compute_backoff_cooldown(
                    bundle_generated=False,
                    now=0.0,
                    last_image_ts=0.0,
                    prev_cooldown_s=self._effective_cooldown_s,
                    base_cooldown_s=float(self.cooldown_s),
                    window_n=self.cooldown_backoff_window_n,
                    max_cooldown_s=float(self.cooldown_backoff_max_s),
                )
                self._effective_cooldown_s = new_cd
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: cooldown {reason} -> {new_cd:.0f}s",
                    level="DEBUG",
                )
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

            now = time.time()
            new_cd, reason = _compute_backoff_cooldown(
                bundle_generated=True,
                now=now,
                last_image_ts=self._last_image_generated_ts,
                prev_cooldown_s=self._effective_cooldown_s,
                base_cooldown_s=float(self.cooldown_s),
                window_n=self.cooldown_backoff_window_n,
                max_cooldown_s=float(self.cooldown_backoff_max_s),
            )
            self._effective_cooldown_s = new_cd
            self._last_image_generated_ts = now
            log_level = "WARNING" if reason.startswith("backed_off") else "INFO"
            self.log(
                f"DetectionSummary[{self.bundle_key}]: cooldown {reason} -> {new_cd:.0f}s",
                level=log_level,
            )

            cooldown_state = {
                "base_cooldown_s": float(self.cooldown_s),
                "effective_cooldown_s": new_cd,
                "backoff_increments": max(0, round(math.log2(new_cd / float(self.cooldown_s)))) if new_cd > float(self.cooldown_s) else 0,
                "finalized_at_epoch": now,
                "cooldown_expires_at_epoch": now + new_cd,
                "cooldown_expires_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + new_cd)),
            }
            self._patch_summary_json_cooldown(active.capture.run_id, cooldown_state)

            best_file = ((bundle.get("debug") or {}).get("selection_meta") or {}).get("best_idx") if isinstance(bundle, dict) else None
            self.log(
                f"DetectionSummary[{self.bundle_key}]: published run_id={active.capture.run_id} "
                f"best_idx={best_file} cooldown={self._effective_cooldown_s:.0f}s",
                level="INFO",
            )
        finally:
            self._last_run_ts = time.time()
            self._in_flight = False
            self._active = None
