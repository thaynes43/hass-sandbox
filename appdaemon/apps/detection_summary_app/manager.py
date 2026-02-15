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
    stable_generated_ha_path,
    write_trace,
)
from .capture import CaptureConfig, CaptureState, CapturedFrame, next_delay_s, should_stop_capture
from .selection import ScoreResult, adaptive_select_and_score

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
        "snapshot_interval_s": 3,
        "off_grace_s": 15,
        "capture_max_s": 300,
        # cooldown
        "cooldown_s": 60,
        "cooldown_backoff_max_s": 1800,
        # selection/scoring
        "analyze_max_snapshots": 10,
        "no_people_threshold": 1.0,
        "external_data_parallelism": 4,
        # provider + model
        "ai_data_enabled": True,
        "external_data_provider": "openai",
        "external_data_model": "gpt-5.2",
        "external_data_timeout_s": 60,
        "external_data_max_output_tokens": 300,
        "external_data_image_detail": "low",
        # output field names
        "data_person_score_field": "person_score",
        "data_face_score_field": "face_score",
        "data_frame_score_field": "frame_score",
        "data_pose_field": "pose",
        "data_summary_field": "summary",
        # image generation
        "external_image_gen_enabled": True,
        "external_image_gen_provider": "openai",
        "external_image_gen_model": "gpt-image-1.5",
        "external_image_gen_timeout_s": 90,
        "external_image_gen_wait_for_best_s": 5,
        "external_generated_filename": "generated.png",
        "bundle_best_filename": "best.jpg",
        # stable published generated file name
        "published_generated_filename": "detection_summary_generated.png",
        # dirs
        "bundle_runs_subdir": "runs",
        "captured_subdir": "captured",
        # storage
        "storage_backend": "media",
        "media_fs_root": "/media",
        "write_bundle_json": True,
        # local_file camera for stable generated
        "generated_image_camera_entity_id": None,
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
        self.camera_entity_id: str = self.args["camera_entity_id"]
        self.trigger_entity_id: str = self.args["trigger_entity_id"]
        self.snapshot_ha_dir: str = _normalize_posix_path(self.args["snapshot_ha_dir"])
        self.data_instructions: str = self.args["data_instructions"]
        self.data_structure: dict[str, Any] = self.args.get("data_structure") or {}

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

        self.external_data_provider: str = str(self.args.get("external_data_provider", self.DEFAULTS["external_data_provider"])).strip().lower()
        self.external_data_api_key: Optional[str] = self.args.get("external_data_api_key") or self.args.get("external_image_gen_api_key")
        self.external_data_base_url: str = str(self.args.get("external_data_base_url", self.args.get("external_image_gen_base_url", "https://api.openai.com")))
        self.external_data_model: str = str(self.args.get("external_data_model", self.DEFAULTS["external_data_model"]))
        self.external_data_timeout_s: float = _safe_float(self.args.get("external_data_timeout_s", self.DEFAULTS["external_data_timeout_s"]))
        self.external_data_max_output_tokens: int = int(self.args.get("external_data_max_output_tokens", self.DEFAULTS["external_data_max_output_tokens"]))
        self.external_data_image_detail: str = str(self.args.get("external_data_image_detail", self.DEFAULTS["external_data_image_detail"]))

        self.data_person_score_field: str = str(self.args.get("data_person_score_field", self.DEFAULTS["data_person_score_field"]))
        self.data_face_score_field: str = str(self.args.get("data_face_score_field", self.DEFAULTS["data_face_score_field"]))
        self.data_frame_score_field: str = str(self.args.get("data_frame_score_field", self.DEFAULTS["data_frame_score_field"]))
        self.data_pose_field: str = str(self.args.get("data_pose_field", self.DEFAULTS["data_pose_field"]))
        self.data_summary_field: str = str(self.args.get("data_summary_field", self.DEFAULTS["data_summary_field"]))

        self.external_image_gen_enabled: bool = _as_bool(self.args.get("external_image_gen_enabled", self.DEFAULTS["external_image_gen_enabled"]))
        self.external_image_gen_wait_for_best_s: float = _safe_float(
            self.args.get("external_image_gen_wait_for_best_s", self.DEFAULTS["external_image_gen_wait_for_best_s"])
        )
        self.external_generated_filename: str = str(self.args.get("external_generated_filename", self.DEFAULTS["external_generated_filename"]))
        self.bundle_best_filename: str = str(self.args.get("bundle_best_filename", self.DEFAULTS["bundle_best_filename"]))
        self.image_instructions: str = str(self.args.get("image_instructions") or "").strip()

        self.published_generated_filename: str = str(
            self.args.get("published_generated_filename", self.DEFAULTS["published_generated_filename"])
        ).strip() or str(self.DEFAULTS["published_generated_filename"])

        self.bundle_runs_subdir: str = str(self.args.get("bundle_runs_subdir", self.DEFAULTS["bundle_runs_subdir"])).strip("/") or "runs"
        self.captured_subdir: str = str(self.args.get("captured_subdir", self.DEFAULTS["captured_subdir"])).strip("/") or "captured"

        self.storage_backend: str = str(self.args.get("storage_backend", self.DEFAULTS["storage_backend"])).strip().lower()
        self.media_fs_root: str = str(self.args.get("media_fs_root", self.DEFAULTS["media_fs_root"])).rstrip("/") or "/media"

        self.write_bundle_json: bool = _as_bool(self.args.get("write_bundle_json", self.DEFAULTS["write_bundle_json"]), default=True)
        self.generated_image_camera_entity_id: Optional[str] = self.args.get("generated_image_camera_entity_id")

        self.trace_cfg = TraceConfig(
            enabled=_as_bool(self.args.get("trace_enabled", self.DEFAULTS["trace_enabled"])),
            copy_selected_frames=_as_bool(self.args.get("trace_copy_selected_frames", self.DEFAULTS["trace_copy_selected_frames"]), default=True),
            copy_best_frame=_as_bool(self.args.get("trace_copy_best_frame", self.DEFAULTS["trace_copy_best_frame"]), default=True),
            max_copies=int(self.args.get("trace_max_copies", self.DEFAULTS["trace_max_copies"])),
        )

        self.log_snapshot_events: bool = _as_bool(self.args.get("log_snapshot_events", self.DEFAULTS["log_snapshot_events"]), default=True)
        self.log_llm_events: bool = _as_bool(self.args.get("log_llm_events", self.DEFAULTS["log_llm_events"]), default=True)

        if self.storage_backend != "media":
            raise ValueError("DetectionSummary v2 requires storage_backend='media'")
        if self.ai_data_enabled and self.external_data_provider == "openai" and not self.external_data_api_key:
            raise ValueError("external_data_api_key is required for external_data_provider='openai'")
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
            f"camera={self.camera_entity_id}, backend={self.storage_backend}, base={self.snapshot_ha_dir}",
            level="INFO",
        )

        self.listen_state(self._on_trigger, self.trigger_entity_id, new=self.trigger_to)

    def _ha_path_to_local_fs(self, ha_path: str) -> Path:
        remainder = _strip_posix_prefix(ha_path, "/media")
        if remainder is None:
            return Path(ha_path)
        return Path(self.media_fs_root) / remainder

    def _get_data_provider(self):
        if self._data_provider is not None:
            return self._data_provider
        cfg = data_provider_config_from_appdaemon_args(
            {
                **self.args,
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
            run.bundle = bundle
        except Exception as e:
            run.bundle = {"run_id": run.capture.run_id, "bundle_key": self.bundle_key, "error": repr(e)}
        finally:
            self.run_in(self._finalize, 0, run_id=run.capture.run_id)

    def _build_bundle(self, run: _Run) -> dict[str, Any]:
        cfg = BundleConfig(
            snapshot_ha_dir=self.snapshot_ha_dir,
            bundle_runs_subdir=self.bundle_runs_subdir,
            bundle_best_filename=self.bundle_best_filename,
            external_generated_filename=self.external_generated_filename,
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
        expected_keys = list((self.data_structure or {}).keys())
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
                data = provider.generate_data_from_image(
                    input_image_path=str(local_path),
                    instructions=str(self.data_instructions),
                    expected_keys=expected_keys,
                )
            except ExternalDataGenError as e:
                self.log(f"DetectionSummary[{self.bundle_key}]: data gen failed for {local_path}: {e!r}", level="WARNING")
            except Exception as e:
                self.log(f"DetectionSummary[{self.bundle_key}]: data gen error for {local_path}: {e!r}", level="WARNING")
            if not isinstance(data, dict):
                data = {}
            person = _safe_float(data.get(self.data_person_score_field, data.get("score")), default=0.0)
            face = _safe_float(data.get(self.data_face_score_field), default=0.0)
            frame = _safe_float(data.get(self.data_frame_score_field), default=person)
            pose = str(data.get(self.data_pose_field) or "").strip().lower()
            summary = str(data.get(self.data_summary_field, data.get("summary", "")) or "").strip()
            if self.log_llm_events:
                elapsed = time.time() - t0
                self.log(
                    f"DetectionSummary[{self.bundle_key}]: LLM score done run_id={run_id} idx={i} "
                    f"elapsed_s={elapsed:.3f} person={person:.2f} face={face:.2f} frame={frame:.2f} pose={pose!r} "
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
                "person_score": person,
                "face_score": face,
                "frame_score": frame,
                "pose": pose,
                "summary_preview": summary[:160],
            }
            return i, ScoreResult(person, face, frame, pose, summary, data), ev

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
        best_idx = int(meta.best_idx)

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

        # Create best.jpg for this run
        best_src = frames_dir / f"frame_{best_idx:03d}.jpg"
        best_dst = local_run_dir / self.bundle_best_filename
        if best_src.exists():
            best_dst.write_bytes(best_src.read_bytes())

        # Generate image from best.jpg to per-run generated.png, then mirror to stable
        generated_image: Optional[dict[str, Any]] = None
        if self.external_image_gen_enabled:
            in_path = best_dst
            out_path = local_run_dir / self.external_generated_filename
            # wait for best to exist
            if self.external_image_gen_wait_for_best_s > 0:
                deadline = time.time() + float(self.external_image_gen_wait_for_best_s)
                while time.time() < deadline and not in_path.exists():
                    time.sleep(0.2)
            if in_path.exists():
                try:
                    provider_cfg = provider_config_from_appdaemon_args(self.args)
                    img_provider = build_image_provider(provider_cfg)
                    if not getattr(img_provider, "capabilities", None) or not img_provider.capabilities.supports_image_to_image:
                        raise ExternalImageGenError("image provider does not support image-to-image")
                    self.log(
                        f"DetectionSummary[{self.bundle_key}]: image gen start run_id={run_id} "
                        f"in={in_path} out={out_path} prompt_len={len(self.image_instructions)}",
                        level="INFO",
                    )
                    generated_image = img_provider.edit_image(
                        input_image_path=str(in_path),
                        prompt=str(self.image_instructions),
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
                            "input_path": str(in_path),
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
            cfg=cfg,
            llm_events=llm_events,
        )
        maybe_write_bundle_json(local_run_dir=local_run_dir, bundle=bundle, enabled=self.write_bundle_json)
        return bundle

    def _finalize(self, kwargs) -> None:
        active = self._active
        if not active or kwargs.get("run_id") != active.capture.run_id:
            return
        try:
            bundle = active.bundle or {}
            gen = bundle.get("generated_image") if isinstance(bundle, dict) else None

            # Update the local_file camera to point at the stable generated file path (HA path)
            if isinstance(gen, dict) and self.generated_image_camera_entity_id:
                try:
                    file_path = stable_generated_ha_path(
                        BundleConfig(
                            snapshot_ha_dir=self.snapshot_ha_dir,
                            bundle_runs_subdir=self.bundle_runs_subdir,
                            bundle_best_filename=self.bundle_best_filename,
                            external_generated_filename=self.external_generated_filename,
                            published_generated_filename=self.published_generated_filename,
                            write_bundle_json=self.write_bundle_json,
                            trace=self.trace_cfg,
                        )
                    )
                    self.call_service(
                        "local_file/update_file_path",
                        target={"entity_id": self.generated_image_camera_entity_id},
                        file_path=file_path,
                    )
                    gen["image_url"] = f"/api/camera_proxy/{self.generated_image_camera_entity_id}"
                except Exception as e:
                    self.log(f"DetectionSummary[{self.bundle_key}]: failed to update generated local_file camera: {e!r}", level="WARNING")

            DETECTION_SUMMARY_STORE.publish_bundle(self.bundle_key, bundle)
            DETECTION_SUMMARY_STORE.cleanup(_safe_float(self.args.get("retention_hours", 24), default=24))

            # Event for consumers
            summary = ((bundle.get("best") or {}).get("summary") if isinstance(bundle, dict) else "") or ""
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

