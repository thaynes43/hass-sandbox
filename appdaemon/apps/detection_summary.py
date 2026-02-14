"""
Generic detection-summary producer.

Listens to a configurable trigger entity and produces SummaryBundles for a
configurable camera. Intended to be instantiated multiple times (one per
camera/zone/use-case) with different prompts and triggers.

Snapshot path mapping (Home Assistant):
- `camera.snapshot` writes inside the HA container to `snapshot_ha_dir` (typically under `/config/www/...`).
- Anything under `/config/www/<path>` is served by Home Assistant at `/local/<path>`.
  Example: `/config/www/detection-summary/garage/slot_00.jpg` -> `/local/detection-summary/garage/slot_00.jpg`.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import hassapi as hass

from detection_summary_store import STORE as DETECTION_SUMMARY_STORE

# Keep shared (non-AppDaemon-app) code out of app modules. We still need imports to work
# when AppDaemon's sys.path only includes `appdaemon/apps`, so we add the parent dir as
# a fallback.
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

    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from ai_providers.registry import (
        build_data_provider,
        build_image_provider,
        data_provider_config_from_appdaemon_args,
        provider_config_from_appdaemon_args,
    )
    from ai_providers.types import ExternalDataGenError, ExternalImageGenError


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


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
    # HA container path should be POSIX-style. We keep it as-is but normalize
    # redundant slashes and remove trailing slash.
    return str(PurePosixPath(path))


def _strip_posix_prefix(path: str, prefix: str) -> Optional[str]:
    """
    If `path` starts with `prefix` (both interpreted as POSIX paths), return the
    remainder without a leading slash. Otherwise return None.
    """
    p = str(PurePosixPath(path))
    pref = str(PurePosixPath(prefix))
    if p == pref:
        return ""
    if p.startswith(pref.rstrip("/") + "/"):
        return p[len(pref.rstrip("/")) + 1 :]
    return None


def _join_web(base: str, filename: str) -> str:
    base = base.rstrip("/")
    return f"{base}/{filename}"


def _extract_ai_task_response(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    AppDaemon service return values wrap the HA result; structure varies by HA service.
    We try a few known shapes for AI Task.
    """
    if not isinstance(res, dict) or not res.get("success"):
        return None
    result = res.get("result")
    if isinstance(result, dict):
        # Some services return {"response": {...}}
        if isinstance(result.get("response"), dict):
            return result["response"]
        # Others might return the response dict directly
        return result
    return None


def _normalize_structure(value: Any) -> Any:
    """
    AppDaemon/HASS service calls often drop None-valued keys from dicts.
    For AI Task `structure`, YAML like:

      selector:
        text:

    parses as {"selector": {"text": None}} and would be serialized with `text` dropped,
    leaving an empty selector {} which HA rejects.

    This normalizer converts None -> {} recursively so selector keys are preserved.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return {k: _normalize_structure(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_structure(v) for v in value]
    return value


def _wait_for_file(path: Path, *, timeout_s: float = 2.0, poll_s: float = 0.1) -> bool:
    """
    Wait for a file to exist. Useful because `camera.snapshot` may return before the file
    is visible on the shared filesystem mount.
    """
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(float(poll_s))
    return path.exists()


@dataclass
class _RunState:
    run_id: str
    started_ts: float
    candidates: list[dict[str, Any]]
    capture_idx: int = 0
    bundle: Optional[dict[str, Any]] = None


class DetectionSummary(hass.Hass):
    DEFAULTS = {
        "trigger_to": "on",
        "ai_task_entity_id": "ai_task.openai_ai_task",
        "task_name": "detection summary",
        "max_snapshots": 5,
        "snapshot_interval_s": 5,
        "ring_size": 10,
        "cooldown_s": 60,
        "retention_hours": 24,
        # If false, we *only* capture snapshots (fast). No per-snapshot AI scoring/summaries.
        # This is useful for initial image collection and tuning snapshot cadence.
        "ai_data_enabled": True,
        "generate_image_enabled": False,
        # External (non-HA) image generation (preferred for /media backend)
        "external_image_gen_enabled": False,
        "external_image_gen_provider": "openai",
        "external_image_gen_model": "gpt-image-1.5",
        "external_image_gen_size": "1024x1024",
        "external_image_gen_quality": "medium",
        "external_image_gen_output_format": "png",
        "external_image_gen_timeout_s": 90,
        # Wait a bit for HA to finish writing best.jpg to /media
        "external_image_gen_wait_for_best_s": 5,
        "external_generated_filename": "generated.png",
        # Mirror the most recent generated image to a stable file in the zone directory.
        # Example: /media/detection-summary/garage/detection_summary_generated.png
        "published_generated_filename": "detection_summary_generated.png",
        "media_content_type": "image/jpeg",
        # Storage backend:
        # - "www": write under /config/www and use /local URLs
        # - "media": write under /media and prefer local_file camera proxy URLs
        "storage_backend": "www",
        # When storage_backend=media, HA writes under `/media/...` inside its container.
        # AppDaemon may see the same share mounted somewhere else (e.g. WSL path).
        # This maps HA's `/media` to a local filesystem directory.
        # - Production (AppDaemon pod mounts /media): /media
        # - WSL dev example: /mnt/cephfs-hdd/misc/hass-media
        "media_fs_root": "/media",
        "write_bundle_json": False,
        # External data generation (AppDaemon -> provider API)
        "external_data_provider": "openai",
        "external_data_model": "gpt-5.2",
        "external_data_timeout_s": 60,
        "external_data_max_output_tokens": 300,
        "external_data_image_detail": "low",
        # Data output field names (lets us evolve the JSON schema without hardcoding keys)
        "data_person_score_field": "person_score",
        "data_face_score_field": "face_score",
        "data_frame_score_field": "frame_score",
        "data_pose_field": "pose",
        "data_summary_field": "summary",
        # Best-frame selection tuning (scores are expected on a 0..10 scale)
        "best_min_person_score": 1.0,
        # Logging (can be verbose)
        "log_snapshot_events": False,
        "log_llm_events": False,
    }

    def initialize(self) -> None:
        # Required args
        self.bundle_key: str = self.args["bundle_key"]
        self.camera_entity_id: str = self.args["camera_entity_id"]
        self.trigger_entity_id: str = self.args["trigger_entity_id"]
        self.storage_backend: str = str(self.args.get("storage_backend", self.DEFAULTS["storage_backend"])).lower()
        if self.storage_backend not in {"www", "media"}:
            raise ValueError("storage_backend must be 'www' or 'media'")

        # Base directory where HA writes snapshots.
        # For backend=www, this is typically /config/www/detection-summary/<bundle_key>
        # For backend=media, this is typically /media/detection-summary/<bundle_key>
        self.snapshot_ha_dir: str = _normalize_posix_path(self.args["snapshot_ha_dir"])
        self.media_fs_root: str = str(self.args.get("media_fs_root", self.DEFAULTS["media_fs_root"])).rstrip("/") or "/media"

        # For backend=www only. Optional otherwise.
        self.web_path_base: str = str(self.args.get("web_path_base", "")).rstrip("/")
        self.data_instructions: str = self.args["data_instructions"]
        self.data_structure: dict[str, Any] = _normalize_structure(self.args["data_structure"])

        # Optional args
        self.trigger_to: str = self.args.get("trigger_to", self.DEFAULTS["trigger_to"])
        # Only used for legacy HA-side image generation (`generate_image_enabled`).
        self.ai_task_entity_id: str = self.args.get("ai_task_entity_id", self.DEFAULTS["ai_task_entity_id"])
        self.task_name: str = self.args.get("task_name", self.DEFAULTS["task_name"])
        self.max_snapshots: int = int(self.args.get("max_snapshots", self.DEFAULTS["max_snapshots"]))
        self.ai_data_enabled: bool = _as_bool(self.args.get("ai_data_enabled", self.DEFAULTS["ai_data_enabled"]), default=True)
        self.snapshot_interval_s: float = _safe_float(
            self.args.get("snapshot_interval_s", self.DEFAULTS["snapshot_interval_s"]),
            default=float(self.DEFAULTS["snapshot_interval_s"]),
        )
        self.ring_size: int = int(self.args.get("ring_size", self.DEFAULTS["ring_size"]))
        self.cooldown_s: float = _safe_float(self.args.get("cooldown_s", self.DEFAULTS["cooldown_s"]))
        self.retention_hours: float = _safe_float(
            self.args.get("retention_hours", self.DEFAULTS["retention_hours"])
        )
        self.generate_image_enabled: bool = _as_bool(
            self.args.get("generate_image_enabled", self.DEFAULTS["generate_image_enabled"])
        )
        self.image_instructions: Optional[str] = self.args.get("image_instructions")

        # External image generation (AppDaemon -> provider API -> /media write)
        self.external_image_gen_enabled: bool = _as_bool(
            self.args.get("external_image_gen_enabled", self.DEFAULTS["external_image_gen_enabled"])
        )
        self.external_image_gen_provider: str = str(
            self.args.get("external_image_gen_provider", self.DEFAULTS["external_image_gen_provider"])
        ).strip().lower()
        self.external_image_gen_api_key: Optional[str] = self.args.get("external_image_gen_api_key")
        self.external_image_gen_base_url: str = str(self.args.get("external_image_gen_base_url", "https://api.openai.com"))
        self.external_image_gen_model: str = str(
            self.args.get("external_image_gen_model", self.DEFAULTS["external_image_gen_model"])
        )
        self.external_image_gen_size: str = str(
            self.args.get("external_image_gen_size", self.DEFAULTS["external_image_gen_size"])
        )
        self.external_image_gen_quality: str = str(
            self.args.get("external_image_gen_quality", self.DEFAULTS["external_image_gen_quality"])
        )
        self.external_image_gen_output_format: str = str(
            self.args.get("external_image_gen_output_format", self.DEFAULTS["external_image_gen_output_format"])
        )
        self.external_image_gen_timeout_s: float = _safe_float(
            self.args.get("external_image_gen_timeout_s", self.DEFAULTS["external_image_gen_timeout_s"]),
            default=float(self.DEFAULTS["external_image_gen_timeout_s"]),
        )
        self.external_image_gen_wait_for_best_s: float = _safe_float(
            self.args.get("external_image_gen_wait_for_best_s", self.DEFAULTS["external_image_gen_wait_for_best_s"]),
            default=float(self.DEFAULTS["external_image_gen_wait_for_best_s"]),
        )
        self.external_generated_filename: str = str(
            self.args.get("external_generated_filename", self.DEFAULTS["external_generated_filename"])
        ).strip() or str(self.DEFAULTS["external_generated_filename"])
        self.media_content_type: str = self.args.get(
            "media_content_type", self.DEFAULTS["media_content_type"]
        )
        # External data generation (AppDaemon -> provider API -> JSON)
        self.external_data_provider: str = str(
            self.args.get("external_data_provider", self.DEFAULTS["external_data_provider"])
        ).strip().lower()
        self.external_data_api_key: Optional[str] = self.args.get("external_data_api_key") or self.args.get(
            "external_image_gen_api_key"
        )
        self.external_data_base_url: str = str(
            self.args.get("external_data_base_url", self.args.get("external_image_gen_base_url", "https://api.openai.com"))
        )
        self.external_data_model: str = str(self.args.get("external_data_model", self.DEFAULTS["external_data_model"]))
        self.external_data_timeout_s: float = _safe_float(
            self.args.get("external_data_timeout_s", self.DEFAULTS["external_data_timeout_s"]),
            default=float(self.DEFAULTS["external_data_timeout_s"]),
        )
        self.external_data_max_output_tokens: int = int(
            self.args.get("external_data_max_output_tokens", self.DEFAULTS["external_data_max_output_tokens"])
        )
        self.external_data_image_detail: str = str(
            self.args.get("external_data_image_detail", self.DEFAULTS["external_data_image_detail"])
        )
        # Data schema field mapping
        self.data_person_score_field: str = str(
            self.args.get("data_person_score_field", self.DEFAULTS["data_person_score_field"])
        ).strip() or str(self.DEFAULTS["data_person_score_field"])
        self.data_face_score_field: str = str(
            self.args.get("data_face_score_field", self.DEFAULTS["data_face_score_field"])
        ).strip() or str(self.DEFAULTS["data_face_score_field"])
        self.data_frame_score_field: str = str(
            self.args.get("data_frame_score_field", self.DEFAULTS["data_frame_score_field"])
        ).strip() or str(self.DEFAULTS["data_frame_score_field"])
        self.data_pose_field: str = str(
            self.args.get("data_pose_field", self.DEFAULTS["data_pose_field"])
        ).strip() or str(self.DEFAULTS["data_pose_field"])
        self.data_summary_field: str = str(
            self.args.get("data_summary_field", self.DEFAULTS["data_summary_field"])
        ).strip() or str(self.DEFAULTS["data_summary_field"])
        self.best_min_person_score: float = _safe_float(
            self.args.get("best_min_person_score", self.DEFAULTS["best_min_person_score"]),
            default=float(self.DEFAULTS["best_min_person_score"]),
        )
        self.log_snapshot_events: bool = _as_bool(
            self.args.get("log_snapshot_events", self.DEFAULTS["log_snapshot_events"])
        )
        self.log_llm_events: bool = _as_bool(self.args.get("log_llm_events", self.DEFAULTS["log_llm_events"]))
        # Where to place the “bundle artifacts” (best snapshot, etc.). This is still
        # written by Home Assistant via camera.snapshot; AppDaemon does not write files
        # directly into HA’s /config.
        #
        # By default, place it under: <snapshot_ha_dir>/runs/<run_id>/best.jpg
        self.bundle_runs_subdir: str = str(self.args.get("bundle_runs_subdir", "runs")).strip("/") or "runs"
        self.bundle_best_filename: str = str(self.args.get("bundle_best_filename", "best.jpg"))
        # Keep the raw ring-buffer snapshots separate from the bundle artifacts.
        # Raw snapshots go under: <snapshot_ha_dir>/<buffer_subdir>/slot_XX.jpg
        self.buffer_subdir: str = str(self.args.get("buffer_subdir", "buffer")).strip("/") or "buffer"

        # Stable published generated filename (under snapshot_ha_dir).
        self.published_generated_filename: str = str(
            self.args.get("published_generated_filename", self.DEFAULTS["published_generated_filename"])
        ).strip() or str(self.DEFAULTS["published_generated_filename"])

        # Optional: local_file cameras that HA exposes. If set, we update file_path to the
        # best artifact and then use the camera proxy URL for notifications.
        self.best_image_camera_entity_id: Optional[str] = self.args.get("best_image_camera_entity_id")
        self.generated_image_camera_entity_id: Optional[str] = self.args.get("generated_image_camera_entity_id")
        # Note: scoring now happens via external data providers reading the saved image file.

        # Optional: write a summary.json in the bundle run dir (only meaningful when AppDaemon
        # can write to the same filesystem, e.g. /media mounted into AppDaemon).
        # Default behavior:
        # - backend=media: write summary.json by default (AppDaemon can read/write the shared /media mount)
        # - backend=www: don't write summary.json by default
        self.write_bundle_json: bool = _as_bool(
            self.args.get("write_bundle_json"),
            default=self.storage_backend == "media",
        )

        if (self.generate_image_enabled or self.external_image_gen_enabled) and not self.image_instructions:
            raise ValueError("image_instructions is required when image generation is enabled")
        if self.external_image_gen_enabled:
            if self.storage_backend != "media":
                raise ValueError("external_image_gen_enabled requires storage_backend='media' (AppDaemon must read/write images)")
            # OpenAI requires an API key; other providers may not.
            if self.external_image_gen_provider == "openai" and not self.external_image_gen_api_key:
                raise ValueError("external_image_gen_api_key is required for external_image_gen_provider='openai'")

        if self.ai_data_enabled:
            # We only support offloading to external providers when AppDaemon can read images.
            if self.storage_backend != "media":
                raise ValueError("ai_data_enabled requires storage_backend='media' (AppDaemon must read snapshot files)")
            # OpenAI requires an API key; other providers may not.
            if self.external_data_provider == "openai" and not self.external_data_api_key:
                raise ValueError("external_data_api_key is required for external_data_provider='openai'")

        if self.generate_image_enabled and not self.ai_task_entity_id:
            raise ValueError("ai_task_entity_id is required when generate_image_enabled is true")

        # Internal state
        self._in_flight = False
        self._last_run_ts = 0.0
        self._next_slot = 0
        self._data_provider = None
        self._active_run: Optional[_RunState] = None

        # Ensure expected directories exist on the shared filesystem when using /media.
        if self.storage_backend == "media":
            try:
                base = self._ha_path_to_local_fs(self.snapshot_ha_dir)
                (base).mkdir(parents=True, exist_ok=True)
                (base / self.buffer_subdir).mkdir(parents=True, exist_ok=True)
                (base / self.bundle_runs_subdir).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise RuntimeError(
                    f"DetectionSummary[{self.bundle_key}]: failed to create media directories under "
                    f"{self.media_fs_root!r} for {self.snapshot_ha_dir!r}: {e!r}"
                ) from e
            # Resume ring-buffer slot index from existing files, so we don't restart at 0
            # after app restarts (or container reschedules).
            self._next_slot = self._infer_next_slot_from_disk()

        self.log(
            f"DetectionSummary[{self.bundle_key}]: trigger={self.trigger_entity_id} -> {self.trigger_to}, "
            f"camera={self.camera_entity_id}, backend={self.storage_backend}, base={self.snapshot_ha_dir}",
            level="INFO",
        )
        self.listen_state(self._on_trigger, self.trigger_entity_id, new=self.trigger_to)

    def _infer_next_slot_from_disk(self) -> int:
        """
        Find the highest existing slot_NN.jpg and return the next index.
        Only works when storage_backend=media and AppDaemon can read the buffer directory.
        """
        if self.storage_backend != "media":
            return 0
        try:
            buf_dir = self._ha_path_to_local_fs(f"{self.snapshot_ha_dir}/{self.buffer_subdir}")
            if not buf_dir.exists():
                return 0
            max_slot = -1
            for p in buf_dir.glob("slot_*.jpg"):
                stem = p.stem  # slot_00
                if not stem.startswith("slot_"):
                    continue
                raw = stem[len("slot_") :]
                try:
                    n = int(raw)
                except Exception:
                    continue
                if n > max_slot:
                    max_slot = n
            return max_slot + 1
        except Exception:
            return 0

    def _ha_path_to_local_fs(self, ha_path: str) -> Path:
        """
        Convert an HA container path to a local filesystem Path that AppDaemon can access.

        For backend=www, AppDaemon should not be reading/writing HA's /config, so we just
        return the path as-is.

        For backend=media, HA uses `/media/...`. In production, AppDaemon also mounts the
        same share at `/media`, so the mapping is identity. In dev (WSL), the share may be
        mounted elsewhere, so we map `/media/<rest>` -> `<media_fs_root>/<rest>`.
        """
        if self.storage_backend != "media":
            return Path(ha_path)

        remainder = _strip_posix_prefix(ha_path, "/media")
        if remainder is None:
            # Unexpected path; don't guess.
            return Path(ha_path)
        return Path(self.media_fs_root) / remainder

    def _get_data_provider(self):
        """
        Lazily build and cache the external data provider.
        Kept as a method so unit tests can stub provider behavior easily.
        """
        if self._data_provider is not None:
            return self._data_provider
        cfg = data_provider_config_from_appdaemon_args(
            {
                **self.args,
                # Ensure defaults are visible to the provider config loader even if not in args.
                "external_data_provider": self.external_data_provider,
                "external_data_api_key": self.external_data_api_key,
                "external_data_base_url": self.external_data_base_url,
                "external_data_model": self.external_data_model,
                "external_data_timeout_s": self.external_data_timeout_s,
                "external_data_max_output_tokens": self.external_data_max_output_tokens,
                "external_data_image_detail": self.external_data_image_detail,
            }
        )
        self._data_provider = build_data_provider(cfg)
        return self._data_provider

    def _on_trigger(self, entity_id, attribute, old, new, kwargs) -> None:
        now = time.time()
        if self._in_flight:
            self.log(f"DetectionSummary[{self.bundle_key}]: run already in progress; ignoring trigger", level="DEBUG")
            return
        if self.cooldown_s > 0 and (now - self._last_run_ts) < self.cooldown_s:
            self.log(
                f"DetectionSummary[{self.bundle_key}]: cooldown active ({now - self._last_run_ts:.1f}s); ignoring trigger",
                level="DEBUG",
            )
            return
        self.log(
            f"DetectionSummary[{self.bundle_key}]: trigger fired ({entity_id} {old!r}->{new!r}); starting run",
            level="INFO",
        )
        self._in_flight = True
        self._start_run()

    def _start_run(self) -> None:
        """
        Start a run without blocking a single AppDaemon callback for the full duration.
        We capture snapshots via scheduled callbacks and do LLM/image processing in a
        background thread, then publish results in a final short callback.
        """
        run_id = str(uuid.uuid4())
        started = time.time()
        self._active_run = _RunState(
            run_id=run_id,
            started_ts=started,
            candidates=[],
            capture_idx=0,
        )
        n = max(1, int(self.max_snapshots))
        interval = float(self.snapshot_interval_s)
        approx = max(0.0, (n - 1) * max(0.0, interval))
        spacing = "back-to-back" if interval <= 0 else f"spaced {interval:.1f}s apart"
        self.log(
            f"DetectionSummary[{self.bundle_key}]: run_id={run_id} capturing {n} snapshot(s) {spacing} "
            f"(approx run capture window ~{approx:.1f}s)",
            level="INFO",
        )
        self.run_in(self._capture_step, 0, run_id=run_id)

    def _capture_step(self, kwargs) -> None:
        rs = self._active_run
        if not rs or kwargs.get("run_id") != rs.run_id:
            return
        i = int(rs.capture_idx)
        if i >= max(1, self.max_snapshots):
            # capture complete; process in background thread
            self._start_processing_thread(rs)
            return

        slot = self._next_slot % max(1, self.ring_size)
        self._next_slot += 1
        filename = f"slot_{slot:02d}.jpg"
        ha_path = f"{self.snapshot_ha_dir}/{self.buffer_subdir}/{filename}"
        web_path = (
            _join_web(_join_web(self.web_path_base, self.buffer_subdir), filename) if self.web_path_base else ""
        )

        # Ask HA to write snapshot (fast callback)
        try:
            self.call_service("camera/snapshot", entity_id=self.camera_entity_id, filename=ha_path)
        except Exception as e:
            self.log(
                f"DetectionSummary[{self.bundle_key}]: snapshot failed for {filename}: {e!r}",
                level="WARNING",
            )
        else:
            if self.log_snapshot_events:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: run_id={rs.run_id} snapshot {i + 1}/{max(1, self.max_snapshots)} "
                    f"saved {ha_path}",
                    level="INFO",
                )

        rs.candidates.append(
            {
                "idx": i,
                "image_filename": filename,
                "image_web_path": web_path,
                "image_ha_path": ha_path,
                "ai_score": 0.0,
                "ai_summary": "",
                "ai_structured": {},
            }
        )
        rs.capture_idx += 1

        delay_s = float(self.snapshot_interval_s) if self.snapshot_interval_s > 0 else 0.0
        self.run_in(self._capture_step, delay_s, run_id=rs.run_id)

    def _start_processing_thread(self, rs: "_RunState") -> None:
        def _worker():
            self._process_run_background(rs)

        t = threading.Thread(target=_worker, name=f"detection_summary_{self.bundle_key}_{rs.run_id[:8]}")
        t.daemon = True
        t.start()

    def _process_run_background(self, rs: "_RunState") -> None:
        """
        Background thread: do slow work (LLM calls, image generation, file I/O) off the
        AppDaemon callback threads. No HA service calls in here.
        """
        try:
            bundle = self._build_bundle_from_captured(rs)
            rs.bundle = bundle
        except Exception as e:
            rs.bundle = {"run_id": rs.run_id, "bundle_key": self.bundle_key, "error": repr(e)}
        finally:
            # Finalize/publish on a short AppDaemon callback so we can safely call HA services.
            self.run_in(self._finalize_run, 0, run_id=rs.run_id)

    def _finalize_run(self, kwargs) -> None:
        rs = self._active_run
        if not rs or kwargs.get("run_id") != rs.run_id:
            return
        started = float(rs.started_ts)
        try:
            bundle = rs.bundle or {}
            # Update local_file cameras (HA service calls) for generated image, if configured.
            artifacts = (bundle.get("bundle_artifacts") or {}) if isinstance(bundle, dict) else {}
            bundle_ha_dir = artifacts.get("bundle_ha_dir") if isinstance(artifacts, dict) else None
            gen = bundle.get("generated_image") if isinstance(bundle, dict) else None
            if isinstance(gen, dict) and self.generated_image_camera_entity_id and gen.get("output_path"):
                try:
                    published_gen_ha_path = f"{self.snapshot_ha_dir}/{self.published_generated_filename}"
                    self.call_service(
                        "local_file/update_file_path",
                        target={"entity_id": self.generated_image_camera_entity_id},
                        file_path=published_gen_ha_path,
                    )
                    gen["image_url"] = f"/api/camera_proxy/{self.generated_image_camera_entity_id}"
                except Exception as e:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: failed to update generated local_file camera: {e!r}",
                        level="WARNING",
                    )

            DETECTION_SUMMARY_STORE.publish_bundle(self.bundle_key, bundle)
            DETECTION_SUMMARY_STORE.cleanup(self.retention_hours)

            if self.log_llm_events and isinstance(bundle, dict):
                dbg = bundle.get("debug") or {}
                events = dbg.get("llm_events") if isinstance(dbg, dict) else None
                if isinstance(events, list) and events:
                    for ev in events:
                        if not isinstance(ev, dict):
                            continue
                        if ev.get("type") == "data":
                            self.log(
                                f"DetectionSummary[{self.bundle_key}]: run_id={rs.run_id} LLM(data) "
                                f"{ev.get('image_filename')} model={ev.get('model')} "
                                f"t={ev.get('elapsed_s')}s person={ev.get('person_score')} face={ev.get('face_score')} frame={ev.get('frame_score')} "
                                f"pose={ev.get('pose')} summary={ev.get('summary_preview')!r}",
                                level="INFO",
                            )
                        elif ev.get("type") == "image_edit":
                            self.log(
                                f"DetectionSummary[{self.bundle_key}]: run_id={rs.run_id} LLM(image_edit) "
                                f"model={ev.get('model')} t={ev.get('elapsed_s')}s out={ev.get('output_path')}",
                                level="INFO",
                            )

            # Log one concise line + optionally debug.
            best_score = (bundle.get("best") or {}).get("score") if isinstance(bundle, dict) else None
            self.log(
                f"DetectionSummary[{self.bundle_key}]: published run_id={rs.run_id} score={best_score} "
                f"best={((bundle.get('debug') or {}).get('best_selected_filename')) if isinstance(bundle, dict) else ''}",
                level="INFO",
            )
        except Exception as e:
            self.log(f"DetectionSummary[{self.bundle_key}]: finalize failed: {e!r}", level="ERROR")
        finally:
            self._last_run_ts = started
            self._in_flight = False
            self._active_run = None

    def _build_bundle_from_captured(self, rs: "_RunState") -> dict[str, Any]:
        """
        Assemble a bundle from already-captured candidates (buffer slot_XX.jpg files).
        Runs external LLM calls + image gen and writes artifacts/summary.json on disk.
        """
        t0 = time.time()
        # IMPORTANT: Candidates were captured already by scheduled callbacks.
        # Do NOT capture again here. Use the captured slot paths for scoring/ranking.
        candidates = [dict(c) for c in (rs.candidates or [])]
        bundle = self._generate_bundle_from_candidates(run_id=rs.run_id, started_ts=rs.started_ts, candidates=candidates)
        dbg = bundle.get("debug") if isinstance(bundle, dict) else None
        if isinstance(dbg, dict):
            dbg["background_elapsed_s"] = round(time.time() - t0, 3)
        return bundle

    def _generate_bundle_from_candidates(
        self, *, run_id: str, started_ts: float, candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Build a SummaryBundle given already-captured candidates (slot_XX.jpg paths).
        This is the production path (capture happens elsewhere).
        """
        live_camera_media_id = f"media-source://camera/{self.camera_entity_id}"
        expected_keys = list(self.data_structure.keys())
        llm_events: list[dict[str, Any]] = []

        # TODO(feature): Support "capture many, analyze max N".
        # Today, `max_snapshots` determines both:
        # - how many snapshots we capture in the burst, and
        # - how many snapshots we send to the LLM provider for scoring.
        #
        # Next step: capture a larger burst (or continuous ring buffer), then subselect
        # a smaller set of candidates to send to the provider (cheap heuristics, or a light model).

        # 1) Score each candidate via external provider (AppDaemon-side), if enabled.
        if self.ai_data_enabled:
            provider = self._get_data_provider()
            for c in candidates:
                data: dict[str, Any] = {}
                local_path = self._ha_path_to_local_fs(str(c.get("image_ha_path") or ""))
                t0 = time.time()
                try:
                    _wait_for_file(local_path, timeout_s=2.0, poll_s=0.1)
                    data = provider.generate_data_from_image(
                        input_image_path=str(local_path),
                        instructions=str(self.data_instructions),
                        expected_keys=expected_keys,
                    )
                except ExternalDataGenError as e:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: external data generation failed for {c.get('image_filename')}: {e!r}",
                        level="WARNING",
                    )
                except Exception as e:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: unexpected data generation failure for {c.get('image_filename')}: {e!r}",
                        level="WARNING",
                    )
                if not isinstance(data, dict):
                    data = {}

                summary_val = data.get(self.data_summary_field, data.get("summary", ""))
                person_score_val = data.get(self.data_person_score_field, data.get("score"))
                face_score_val = data.get(self.data_face_score_field)
                frame_score_val = data.get(self.data_frame_score_field)
                pose_val = data.get(self.data_pose_field)

                person_score = _safe_float(person_score_val, default=0.0)
                face_score = _safe_float(face_score_val, default=0.0)
                frame_score = _safe_float(frame_score_val, default=person_score)
                pose = str(pose_val or "").strip().lower()

                c["ai_structured"] = data
                c["ai_score"] = person_score
                c["ai_person_score"] = person_score
                c["ai_face_score"] = face_score
                c["ai_frame_score"] = frame_score
                c["ai_pose"] = pose
                c["ai_summary"] = str(summary_val or "").strip()

                meta = data.get("_meta") if isinstance(data, dict) else None
                llm_events.append(
                    {
                        "type": "data",
                        "image_filename": c.get("image_filename"),
                        "input_path": str(local_path),
                        "elapsed_s": (meta or {}).get("elapsed_s", round(time.time() - t0, 3)) if isinstance(meta, dict) else round(time.time() - t0, 3),
                        "model": (meta or {}).get("model") if isinstance(meta, dict) else None,
                        "person_score": person_score,
                        "face_score": face_score,
                        "frame_score": frame_score,
                        "pose": pose,
                        "summary_preview": (c.get("ai_summary") or "")[:160],
                    }
                )

        # 2) Choose best frame
        pose_rank = {"standing": 3, "stationary": 3, "sitting": 2, "walking": 1, "moving": 1, "none": 0, "": 0}

        def _pick_key(c: dict[str, Any]) -> tuple:
            person = _safe_float(c.get("ai_person_score", c.get("ai_score")), default=0.0)
            face = _safe_float(c.get("ai_face_score"), default=0.0)
            frame = _safe_float(c.get("ai_frame_score", person), default=person)
            pose = str(c.get("ai_pose") or "").strip().lower()
            has_person = 1 if person >= float(self.best_min_person_score) else 0
            has_summary = 1 if str(c.get("ai_summary") or "").strip() else 0
            return (
                has_person,
                face,
                frame,
                pose_rank.get(pose, 0),
                person,
                has_summary,
                int(c.get("idx") or 0),
            )

        if not candidates:
            candidates = [
                {
                    "idx": 0,
                    "image_filename": "",
                    "image_web_path": "",
                    "image_ha_path": "",
                    "ai_score": 0.0,
                    "ai_summary": "",
                    "ai_structured": {},
                }
            ]
        best = max(candidates, key=_pick_key)
        best_idx = int(best.get("idx") or 0)

        # 3) Run dir paths
        bundle_ha_dir = _normalize_posix_path(f"{self.snapshot_ha_dir}/{self.bundle_runs_subdir}/{run_id}")
        bundle_web_base = (
            _join_web(self.web_path_base, f"{self.bundle_runs_subdir}/{run_id}") if self.web_path_base else ""
        )
        best_artifact_web_path = _join_web(bundle_web_base, self.bundle_best_filename) if bundle_web_base else ""

        # 4) Write best.jpg (exact chosen best for backend=media)
        if self.storage_backend == "media":
            try:
                src = self._ha_path_to_local_fs(str(best.get("image_ha_path") or ""))
                dst_dir = self._ha_path_to_local_fs(bundle_ha_dir)
                dst = dst_dir / self.bundle_best_filename
                dst_dir.mkdir(parents=True, exist_ok=True)
                if src and src.exists():
                    dst.write_bytes(src.read_bytes())
                else:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: best source not found to copy: {src}",
                        level="WARNING",
                    )
                # (No stable "best" mirror by default; garage notifications only use generated image.)
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: failed to create best artifact: {e!r}",
                    level="WARNING",
                )
        else:
            try:
                self.call_service(
                    "camera/snapshot",
                    entity_id=self.camera_entity_id,
                    filename=f"{bundle_ha_dir}/{self.bundle_best_filename}",
                )
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: failed to write bundle best snapshot: {e!r}",
                    level="WARNING",
                )

        best_image_url: str = ""  # local_file proxy URL set during finalize callback

        # 5) External image generation (edit) uses best.jpg as input
        generated_image: Optional[dict[str, Any]] = None
        if self.external_image_gen_enabled:
            local_bundle_dir = self._ha_path_to_local_fs(bundle_ha_dir)
            in_path = local_bundle_dir / self.bundle_best_filename
            out_path = local_bundle_dir / self.external_generated_filename

            if self.external_image_gen_wait_for_best_s > 0:
                _wait_for_file(in_path, timeout_s=float(self.external_image_gen_wait_for_best_s), poll_s=0.2)

            if in_path.exists():
                try:
                    provider_cfg = provider_config_from_appdaemon_args(self.args)
                    provider = build_image_provider(provider_cfg)
                    if not getattr(provider, "capabilities", None) or not provider.capabilities.supports_image_to_image:
                        raise ExternalImageGenError(
                            f"Provider {getattr(provider, 'name', 'unknown')} does not support image-to-image"
                        )
                    generated_image = provider.edit_image(
                        input_image_path=str(in_path),
                        prompt=str(self.image_instructions),
                        output_image_path=str(out_path),
                    )
                    # Mirror generated output to a stable path for local_file cameras.
                    try:
                        published_gen = self._ha_path_to_local_fs(
                            f"{self.snapshot_ha_dir}/{self.published_generated_filename}"
                        )
                        published_gen.parent.mkdir(parents=True, exist_ok=True)
                        if out_path.exists():
                            published_gen.write_bytes(out_path.read_bytes())
                            # Prefer stable output_path so finalize can update local_file consistently.
                            generated_image["output_path"] = str(published_gen)
                    except Exception as e:
                        self.log(
                            f"DetectionSummary[{self.bundle_key}]: failed to mirror generated image to stable path: {e!r}",
                            level="WARNING",
                        )
                    llm_events.append(
                        {
                            "type": "image_edit",
                            "input_path": str(in_path),
                            "output_path": str((generated_image or {}).get("output_path") or out_path),
                            "elapsed_s": (generated_image or {}).get("elapsed_s"),
                            "model": (generated_image or {}).get("model"),
                        }
                    )
                except ExternalImageGenError as e:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: external image generation failed: {e!r}",
                        level="WARNING",
                    )
            else:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: best artifact not found for external image generation: {in_path}",
                    level="WARNING",
                )

        elif self.generate_image_enabled:
            # Legacy HA ai_task path (kept for compatibility)
            img_res = self.call_service(
                "ai_task/generate_image",
                service_data={
                    "entity_id": self.ai_task_entity_id,
                    "task_name": f"{self.task_name} image",
                    "instructions": self.image_instructions,
                    "attachments": [
                        {
                            "media_content_id": live_camera_media_id,
                            "media_content_type": self.media_content_type,
                        }
                    ],
                },
            )
            img_resp = _extract_ai_task_response(img_res) or {}
            if isinstance(img_resp, dict) and img_resp:
                generated_image = {
                    "created_at_utc": _utc_iso(time.time()),
                    "media_source_id": img_resp.get("media_source_id"),
                    "url": img_resp.get("url"),
                    "revised_prompt": img_resp.get("revised_prompt"),
                    "model": img_resp.get("model"),
                }

        published_ts = time.time()
        bundle: dict[str, Any] = {
            "run_id": run_id,
            # IMPORTANT: `created_at_*` is used for matching windows in DetectionSummaryStore.
            # Use publish time (end of processing) so consumers like GarageDoorNotify can match
            # the bundle to later device events (door open/close) without widening windows.
            "created_at_epoch": published_ts,
            "created_at_utc": _utc_iso(published_ts),
            # Also include capture start time for debugging/analysis.
            "capture_started_epoch": started_ts,
            "capture_started_utc": _utc_iso(started_ts),
            "bundle_key": self.bundle_key,
            "camera_entity_id": self.camera_entity_id,
            "trigger_entity_id": self.trigger_entity_id,
            "bundle_artifacts": {
                "bundle_ha_dir": bundle_ha_dir,
                "bundle_web_base": bundle_web_base,
                "best_artifact_web_path": best_artifact_web_path,
                "best_candidate_web_path": best.get("image_web_path", ""),
                "best_candidate_ha_path": best.get("image_ha_path", ""),
            },
            "candidates": candidates,
            "best_idx": best_idx,
            "best": {
                "image_web_path": best.get("image_web_path", ""),
                "image_url": best_image_url or best_artifact_web_path or best.get("image_web_path", ""),
                "summary": best.get("ai_summary", ""),
                "score": best.get("ai_person_score", best.get("ai_score", 0.0)),
                "person_score": best.get("ai_person_score", best.get("ai_score", 0.0)),
                "face_score": best.get("ai_face_score", 0.0),
                "frame_score": best.get("ai_frame_score", best.get("ai_person_score", best.get("ai_score", 0.0))),
                "pose": best.get("ai_pose", ""),
                "ai_structured": best.get("ai_structured", {}),
            },
            "generated_image": generated_image,
            "debug": {
                "data_instructions": self.data_instructions,
                "expected_keys": expected_keys,
                "external_data_provider": self.external_data_provider,
                "external_data_model": self.external_data_model,
                "external_data_timeout_s": self.external_data_timeout_s,
                "external_data_max_output_tokens": self.external_data_max_output_tokens,
                "external_data_image_detail": self.external_data_image_detail,
                "external_image_gen_enabled": self.external_image_gen_enabled,
                "external_image_gen_provider": self.external_image_gen_provider,
                "external_image_gen_model": self.external_image_gen_model,
                "external_image_gen_size": self.external_image_gen_size,
                "external_image_gen_quality": self.external_image_gen_quality,
                "image_instructions": self.image_instructions,
                "ranking_order_idx": [int(x.get("idx") or 0) for x in sorted(candidates, key=_pick_key, reverse=True)],
                "best_selected_idx": best_idx,
                "best_selected_filename": best.get("image_filename"),
                "best_selected_ha_path": best.get("image_ha_path"),
                "best_min_person_score": float(self.best_min_person_score),
                "data_person_score_field": self.data_person_score_field,
                "data_face_score_field": self.data_face_score_field,
                "data_frame_score_field": self.data_frame_score_field,
                "data_pose_field": self.data_pose_field,
                "data_summary_field": self.data_summary_field,
                "llm_events": llm_events,
                "published_at_epoch": published_ts,
                "published_at_utc": _utc_iso(published_ts),
            },
            "consumed": False,
            "consumed_at_utc": None,
        }

        if self.write_bundle_json:
            try:
                path = self._ha_path_to_local_fs(bundle_ha_dir) / "summary.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                import json

                path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: failed to write summary.json: {e!r}",
                    level="WARNING",
                )

        return bundle

    def _generate_bundle(self, *, run_id: str, started_ts: float) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        live_camera_media_id = f"media-source://camera/{self.camera_entity_id}"
        expected_keys = list(self.data_structure.keys())

        for i in range(max(1, self.max_snapshots)):
            slot = self._next_slot % max(1, self.ring_size)
            self._next_slot += 1
            filename = f"slot_{slot:02d}.jpg"
            ha_path = f"{self.snapshot_ha_dir}/{self.buffer_subdir}/{filename}"
            web_path = (
                _join_web(_join_web(self.web_path_base, self.buffer_subdir), filename)
                if self.web_path_base
                else ""
            )

            # 1) Ask HA to write a snapshot to its /config PVC
            self.call_service("camera/snapshot", entity_id=self.camera_entity_id, filename=ha_path)

            # NOTE: we *do not* score/process inline here.
            # Keeping snapshot capture lightweight ensures we always capture at snapshot_interval_s cadence.
            candidates.append(
                {
                    "idx": i,
                    "image_filename": filename,
                    "image_web_path": web_path,
                    "image_ha_path": ha_path,
                    "ai_score": 0.0,
                    "ai_summary": "",
                    "ai_structured": {},
                }
            )

            # spacing between samples
            if i < (self.max_snapshots - 1) and self.snapshot_interval_s > 0:
                # AppDaemon's `sleep()` is async-only; this app is synchronous.
                time.sleep(self.snapshot_interval_s)

        # 2) Optionally score/process snapshots after capture.
        #
        # This keeps capture cadence stable, at the cost of doing the AI work later.
        if self.ai_data_enabled:
            provider = self._get_data_provider()
            for c in candidates:
                data: dict[str, Any] = {}
                try:
                    local_path = self._ha_path_to_local_fs(str(c["image_ha_path"]))
                    _wait_for_file(local_path, timeout_s=2.0, poll_s=0.1)
                    data = provider.generate_data_from_image(
                        input_image_path=str(local_path),
                        instructions=str(self.data_instructions),
                        expected_keys=expected_keys,
                    )
                except ExternalDataGenError as e:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: external data generation failed for {c.get('image_filename')}: {e!r}",
                        level="WARNING",
                    )
                except Exception as e:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: unexpected data generation failure for {c.get('image_filename')}: {e!r}",
                        level="WARNING",
                    )
                if not isinstance(data, dict):
                    data = {}
                # Extract fields (support both new schema + legacy {score, summary}).
                summary_val = data.get(self.data_summary_field, data.get("summary", ""))
                person_score_val = data.get(self.data_person_score_field, data.get("score"))
                frame_score_val = data.get(self.data_frame_score_field)
                pose_val = data.get(self.data_pose_field)

                person_score = _safe_float(person_score_val, default=0.0)
                frame_score = _safe_float(frame_score_val, default=person_score)
                pose = str(pose_val or "").strip().lower()

                c["ai_structured"] = data
                # Keep `ai_score` as the person-presence score (used by store ranking across runs).
                c["ai_score"] = person_score
                c["ai_person_score"] = person_score
                c["ai_frame_score"] = frame_score
                c["ai_pose"] = pose
                c["ai_summary"] = str(summary_val or "").strip()

        # Select best frame for notification.
        # Keep bundle/store "score" as person-presence, but choose best image using
        # frame quality + pose when person is present.
        pose_rank = {"standing": 3, "stationary": 3, "sitting": 2, "walking": 1, "moving": 1, "none": 0, "": 0}

        def _pick_key(c: dict[str, Any]) -> tuple:
            person = _safe_float(c.get("ai_person_score", c.get("ai_score")), default=0.0)
            frame = _safe_float(c.get("ai_frame_score", person), default=person)
            pose = str(c.get("ai_pose") or "").strip().lower()
            has_person = 1 if person >= float(self.best_min_person_score) else 0
            has_summary = 1 if str(c.get("ai_summary") or "").strip() else 0
            return (
                has_person,
                frame,
                pose_rank.get(pose, 0),
                person,
                has_summary,
                int(c.get("idx") or 0),
            )

        best = max(candidates, key=_pick_key)
        best_idx = int(best.get("idx") or 0)

        # (best already selected above)

        # Bundle artifact location (inside HA /config/www + /local)
        bundle_ha_dir = _normalize_posix_path(f"{self.snapshot_ha_dir}/{self.bundle_runs_subdir}/{run_id}")
        bundle_web_base = (
            _join_web(self.web_path_base, f"{self.bundle_runs_subdir}/{run_id}") if self.web_path_base else ""
        )
        best_artifact_web_path = _join_web(bundle_web_base, self.bundle_best_filename) if bundle_web_base else ""

        # Create best.jpg in the run folder.
        #
        # - backend=media: copy the chosen best slot image so "best.jpg" exactly matches ranking
        # - backend=www: fall back to a fresh snapshot because AppDaemon cannot read/copy HA's /config
        if self.storage_backend == "media":
            try:
                src = self._ha_path_to_local_fs(str(best.get("image_ha_path") or ""))
                dst_dir = self._ha_path_to_local_fs(bundle_ha_dir)
                dst = dst_dir / self.bundle_best_filename
                dst_dir.mkdir(parents=True, exist_ok=True)
                if src and src.exists():
                    dst.write_bytes(src.read_bytes())
                else:
                    # Last resort: ask HA to write a snapshot to the bundle dir
                    self.call_service(
                        "camera/snapshot",
                        entity_id=self.camera_entity_id,
                        filename=f"{bundle_ha_dir}/{self.bundle_best_filename}",
                    )
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: failed to create best artifact: {e!r}",
                    level="WARNING",
                )
        else:
            try:
                self.call_service(
                    "camera/snapshot",
                    entity_id=self.camera_entity_id,
                    filename=f"{bundle_ha_dir}/{self.bundle_best_filename}",
                )
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: failed to write bundle best snapshot: {e!r}",
                    level="WARNING",
                )

        # If configured, point a local_file camera at the best artifact and use camera proxy URL.
        best_image_url: str = ""
        if self.best_image_camera_entity_id:
            try:
                self.call_service(
                    "local_file/update_file_path",
                    target={"entity_id": self.best_image_camera_entity_id},
                    file_path=f"{bundle_ha_dir}/{self.bundle_best_filename}",
                )
                best_image_url = f"/api/camera_proxy/{self.best_image_camera_entity_id}"
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: failed to update best local_file camera: {e!r}",
                    level="WARNING",
                )

        generated_image: Optional[dict[str, Any]] = None
        if self.external_image_gen_enabled:
            # External generation uses the best artifact file as input and writes a generated image alongside it.
            local_bundle_dir = self._ha_path_to_local_fs(bundle_ha_dir)
            in_path = local_bundle_dir / self.bundle_best_filename
            out_path = local_bundle_dir / self.external_generated_filename

            # HA just wrote best.jpg; wait briefly for it to appear on the shared mount.
            if self.external_image_gen_wait_for_best_s > 0:
                deadline = time.time() + float(self.external_image_gen_wait_for_best_s)
                while time.time() < deadline and not in_path.exists():
                    time.sleep(0.2)

            if in_path.exists():
                try:
                    provider_cfg = provider_config_from_appdaemon_args(self.args)
                    provider = build_image_provider(provider_cfg)
                    if not getattr(provider, "capabilities", None) or not provider.capabilities.supports_image_to_image:
                        raise ExternalImageGenError(
                            f"Provider {getattr(provider, 'name', 'unknown')} does not support image-to-image"
                        )
                    generated_image = provider.edit_image(
                        input_image_path=str(in_path),
                        prompt=str(self.image_instructions),
                        output_image_path=str(out_path),
                    )
                    # Optionally expose generated image via a local_file camera proxy URL.
                    if self.generated_image_camera_entity_id:
                        try:
                            self.call_service(
                                "local_file/update_file_path",
                                target={"entity_id": self.generated_image_camera_entity_id},
                                file_path=str(out_path),
                            )
                            generated_image["image_url"] = f"/api/camera_proxy/{self.generated_image_camera_entity_id}"
                        except Exception as e:
                            self.log(
                                f"DetectionSummary[{self.bundle_key}]: failed to update generated local_file camera: {e!r}",
                                level="WARNING",
                            )
                except ExternalImageGenError as e:
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: external image generation failed: {e!r}",
                        level="WARNING",
                    )
            else:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: best artifact not found for external image generation: {in_path}",
                    level="WARNING",
                )

        elif self.generate_image_enabled:
            img_res = self.call_service(
                "ai_task/generate_image",
                service_data={
                    "entity_id": self.ai_task_entity_id,
                    "task_name": f"{self.task_name} image",
                    "instructions": self.image_instructions,
                    "attachments": [
                        {
                            "media_content_id": live_camera_media_id,
                            "media_content_type": self.media_content_type,
                        }
                    ],
                },
            )
            img_resp = _extract_ai_task_response(img_res) or {}
            # Expected keys per HA docs: media_source_id, url, revised_prompt, model, mime_type, width, height
            if isinstance(img_resp, dict) and img_resp:
                generated_image = {
                    "created_at_utc": _utc_iso(time.time()),
                    "media_source_id": img_resp.get("media_source_id"),
                    "url": img_resp.get("url"),
                    "revised_prompt": img_resp.get("revised_prompt"),
                    "model": img_resp.get("model"),
                }

        # Optional: write a summary.json alongside bundle artifacts (only works when AppDaemon
        # has filesystem access to the same directory, e.g. /media mounted in AppDaemon).
        if self.write_bundle_json:
            try:
                path = self._ha_path_to_local_fs(bundle_ha_dir) / "summary.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                # Minimal JSON (full bundle is returned below; we write after constructing it)
            except Exception:
                # we'll try again after bundle is built
                pass

        bundle: dict[str, Any] = {
            "run_id": run_id,
            "created_at_epoch": started_ts,
            "created_at_utc": _utc_iso(started_ts),
            "bundle_key": self.bundle_key,
            "camera_entity_id": self.camera_entity_id,
            "trigger_entity_id": self.trigger_entity_id,
            "bundle_artifacts": {
                "bundle_ha_dir": bundle_ha_dir,
                "bundle_web_base": bundle_web_base,
                "best_artifact_web_path": best_artifact_web_path,
                # Reference to the chosen “best” candidate in the raw snapshot ring:
                "best_candidate_web_path": best.get("image_web_path", ""),
                "best_candidate_ha_path": best.get("image_ha_path", ""),
            },
            "candidates": candidates,
            "best_idx": best_idx,
            "best": {
                "image_web_path": best["image_web_path"],
                "image_url": best_image_url or best_artifact_web_path or best.get("image_web_path", ""),
                "summary": best["ai_summary"],
                # Store/bundle score remains person-presence
                "score": best.get("ai_person_score", best["ai_score"]),
                "person_score": best.get("ai_person_score", best["ai_score"]),
                "frame_score": best.get("ai_frame_score", best.get("ai_person_score", best["ai_score"])),
                "pose": best.get("ai_pose", ""),
                "ai_structured": best["ai_structured"],
            },
            "generated_image": generated_image,
            "debug": {
                "data_instructions": self.data_instructions,
                "expected_keys": expected_keys,
                "external_data_provider": self.external_data_provider,
                "external_data_model": self.external_data_model,
                "external_data_timeout_s": self.external_data_timeout_s,
                "external_data_max_output_tokens": self.external_data_max_output_tokens,
                "external_data_image_detail": self.external_data_image_detail,
                "external_image_gen_enabled": self.external_image_gen_enabled,
                "external_image_gen_provider": self.external_image_gen_provider,
                "external_image_gen_model": self.external_image_gen_model,
                "external_image_gen_size": self.external_image_gen_size,
                "external_image_gen_quality": self.external_image_gen_quality,
                "image_instructions": self.image_instructions,
                "ranking_order_idx": [
                    int(x["idx"])
                    for x in sorted(
                        candidates,
                        key=lambda c: (
                            -_pick_key(c)[0],
                            -_pick_key(c)[1],
                            -_pick_key(c)[2],
                            -_pick_key(c)[3],
                            -_pick_key(c)[4],
                            -_pick_key(c)[5],
                        ),
                    )
                ],
                "best_selected_idx": best_idx,
                "best_selected_filename": best.get("image_filename"),
                "best_selected_ha_path": best.get("image_ha_path"),
                "best_min_person_score": float(self.best_min_person_score),
                "data_person_score_field": self.data_person_score_field,
                "data_frame_score_field": self.data_frame_score_field,
                "data_pose_field": self.data_pose_field,
                "data_summary_field": self.data_summary_field,
            },
            "consumed": False,
            "consumed_at_utc": None,
        }
        if self.write_bundle_json:
            try:
                path = self._ha_path_to_local_fs(bundle_ha_dir) / "summary.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                import json

                path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
            except Exception as e:
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: failed to write summary.json: {e!r}",
                    level="WARNING",
                )
        return bundle

