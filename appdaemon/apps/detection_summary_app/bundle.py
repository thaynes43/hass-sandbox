from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .capture import CaptureState
from .selection import ScoreResult, SelectionMeta


@dataclass
class TraceConfig:
    enabled: bool = False
    copy_selected_frames: bool = True
    copy_best_frame: bool = True
    max_copies: int = 50


@dataclass
class BundleConfig:
    snapshot_ha_dir: str
    bundle_runs_subdir: str
    bundle_best_filename: str
    external_generated_filename: str
    published_generated_filename: str
    write_bundle_json: bool
    trace: TraceConfig


def run_ha_dir(cfg: BundleConfig, run_id: str) -> str:
    return f"{cfg.snapshot_ha_dir}/{cfg.bundle_runs_subdir}/{run_id}"


def stable_generated_ha_path(cfg: BundleConfig) -> str:
    return f"{cfg.snapshot_ha_dir}/{cfg.published_generated_filename}"


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path) -> None:
    ensure_dir(dst.parent)
    dst.write_bytes(src.read_bytes())


def write_trace(
    *,
    local_run_dir: Path,
    frames_dir: Path,
    scored: dict[int, ScoreResult],
    meta: SelectionMeta,
    best_idx: int,
    cfg: TraceConfig,
) -> None:
    if not cfg.enabled:
        return

    trace_dir = local_run_dir / "trace"
    selected_dir = trace_dir / "selected"
    best_dir = trace_dir / "best"
    ensure_dir(selected_dir)
    ensure_dir(best_dir)

    # Cap copies defensively
    scored_items = list(scored.items())[: max(0, int(cfg.max_copies))]

    if cfg.copy_selected_frames:
        for idx, _res in scored_items:
            src = frames_dir / f"frame_{idx:03d}.jpg"
            if src.exists():
                copy_file(src, selected_dir / src.name)

    if cfg.copy_best_frame:
        src = frames_dir / f"frame_{best_idx:03d}.jpg"
        if src.exists():
            copy_file(src, best_dir / src.name)

    meta_out = {
        "budget": meta.budget,
        "scored_indices": meta.scored_indices,
        "probes": meta.probes,
        "cutoff_idx_inclusive": meta.cutoff_idx_inclusive,
        "best_idx": best_idx,
        "scored": {
            str(i): {
                "person_score": float(r.person_score),
                "face_score": float(r.face_score),
                "frame_score": float(r.frame_score),
                "pose": r.pose,
                "summary": r.summary,
            }
            for i, r in scored.items()
        },
    }
    (trace_dir / "meta.json").write_text(json.dumps(meta_out, indent=2, sort_keys=True), encoding="utf-8")


def build_bundle_dict(
    *,
    bundle_key: str,
    camera_entity_id: str,
    trigger_entity_id: str,
    run_id: str,
    capture: CaptureState,
    scored: dict[int, ScoreResult],
    selection_meta: SelectionMeta,
    best_idx: int,
    best_image_url: str,
    generated_image: Optional[dict[str, Any]],
    cfg: BundleConfig,
    llm_events: list[dict[str, Any]],
) -> dict[str, Any]:
    published_ts = time.time()
    ha_dir = run_ha_dir(cfg, run_id)

    def _rank_key(res: ScoreResult) -> tuple:
        has_person = 1 if float(res.person_score) > 0 else 0
        has_summary = 1 if (res.summary or "").strip() else 0
        pose = (res.pose or "").strip().lower()
        pose_rank = {"standing": 3, "stationary": 3, "sitting": 2, "walking": 1, "moving": 1}.get(pose, 0)
        return (has_person, float(res.face_score), float(res.frame_score), pose_rank, float(res.person_score), has_summary)

    def _cand(idx: int) -> dict[str, Any]:
        fr = scored.get(idx)
        cap_fr = next((f for f in capture.frames if f.idx == idx), None)
        return {
            "idx": idx,
            "image_filename": (cap_fr.filename if cap_fr else f"frame_{idx:03d}.jpg"),
            "image_ha_path": (cap_fr.image_ha_path if cap_fr else ""),
            "ai_person_score": getattr(fr, "person_score", 0.0) if fr else 0.0,
            "ai_face_score": getattr(fr, "face_score", 0.0) if fr else 0.0,
            "ai_frame_score": getattr(fr, "frame_score", 0.0) if fr else 0.0,
            "ai_pose": getattr(fr, "pose", "") if fr else "",
            "ai_summary": getattr(fr, "summary", "") if fr else "",
            "ai_structured": getattr(fr, "structured", {}) if fr else {},
        }

    best_res = scored.get(best_idx)
    best_summary = (best_res.summary if best_res else "").strip()

    capture_ended_epoch = float(capture.ended_ts or published_ts)
    capture_duration_s = max(0.0, capture_ended_epoch - float(capture.started_ts))
    motion_on_s = max(0.0, float(getattr(capture, "motion_on_total_s", 0.0) or 0.0))
    buffer_overhang_s = max(0.0, capture_duration_s - motion_on_s)

    ranked = sorted(
        [{"idx": i, "rank_key": _rank_key(r)} for i, r in scored.items()],
        key=lambda x: x["rank_key"],
        reverse=True,
    )
    ranked_indices = [int(x["idx"]) for x in ranked]

    candidates = [_cand(i) for i in selection_meta.scored_indices]

    # Summarized LLM events for quick scanning.
    # Keep these small and structured (full per-frame raw data lives under candidates[*].ai_structured._meta).
    data_events = [e for e in (llm_events or []) if isinstance(e, dict) and e.get("type") == "data"]
    image_events = [e for e in (llm_events or []) if isinstance(e, dict) and e.get("type") != "data"]
    data_by_idx = {int(e.get("frame_idx")): e for e in data_events if e.get("frame_idx") is not None}
    summarized_llm_events: list[dict[str, Any]] = []
    # Put scored frames in "best-to-worst" order when possible.
    for i in ranked_indices:
        ev = data_by_idx.get(int(i))
        if ev:
            summarized_llm_events.append(ev)
    # Include any remaining data events (in original order).
    for ev in data_events:
        if ev not in summarized_llm_events:
            summarized_llm_events.append(ev)
    # Append non-data events (e.g. image_edit) at the end.
    summarized_llm_events.extend(image_events)

    bundle: dict[str, Any] = {}
    # Human-first summary block (intentionally first in file output).
    bundle["summary"] = {
        "run_id": run_id,
        "bundle_key": bundle_key,
        "created_at_epoch": published_ts,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(published_ts)),
        "best_idx": int(best_idx),
        "text": best_summary,
        "scores": {
            "person_score": float(best_res.person_score if best_res else 0.0),
            "face_score": float(best_res.face_score if best_res else 0.0),
            "frame_score": float(best_res.frame_score if best_res else 0.0),
        },
        "timing": {
            "capture_started_epoch": float(capture.started_ts),
            "capture_ended_epoch": capture_ended_epoch,
            "capture_duration_s": round(capture_duration_s, 3),
            "motion_detected_s": round(motion_on_s, 3),
            "buffer_overhang_s": round(buffer_overhang_s, 3),
            "capture_timed_out": bool(capture.timed_out),
            "captured_frames": int(getattr(capture, "capture_idx", len(getattr(capture, "frames", []) or []))),
            "scored_frames": int(len(scored)),
        },
        "generated_image_url": (generated_image or {}).get("image_url") if isinstance(generated_image, dict) else None,
        "summarized_llm_events": summarized_llm_events,
    }

    # Keep AI artifacts near the top for readability.
    bundle["generated_image"] = generated_image
    bundle["best_idx"] = int(best_idx)
    bundle["best"] = {
        "summary": best_summary,
        "score": float(best_res.person_score if best_res else 0.0),
        "person_score": float(best_res.person_score if best_res else 0.0),
        "face_score": float(best_res.face_score if best_res else 0.0),
        "frame_score": float(best_res.frame_score if best_res else 0.0),
        "pose": best_res.pose if best_res else "",
        "image_url": best_image_url,
        "ai_structured": best_res.structured if best_res else {},
    }
    bundle["candidates"] = candidates

    # Remaining structured metadata (useful for tooling/deserialization).
    bundle["run_id"] = run_id
    bundle["created_at_epoch"] = published_ts
    bundle["created_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(published_ts))
    bundle["capture_started_epoch"] = float(capture.started_ts)
    bundle["capture_ended_epoch"] = capture_ended_epoch
    bundle["capture_duration_s"] = round(capture_duration_s, 3)
    bundle["motion_detected_s"] = round(motion_on_s, 3)
    bundle["buffer_overhang_s"] = round(buffer_overhang_s, 3)
    bundle["capture_timed_out"] = bool(capture.timed_out)
    bundle["bundle_key"] = bundle_key
    bundle["camera_entity_id"] = camera_entity_id
    bundle["trigger_entity_id"] = trigger_entity_id
    bundle["bundle_artifacts"] = {
        "bundle_ha_dir": ha_dir,
        "captured_subdir": "captured",
        "best_ha_path": f"{ha_dir}/{cfg.bundle_best_filename}",
        "generated_ha_path": f"{ha_dir}/{cfg.external_generated_filename}",
        "stable_generated_ha_path": stable_generated_ha_path(cfg),
    }

    # Selection trace/debugging (structured for future tooling).
    bundle["debug"] = {
        "selection_trace": {
            "budget": selection_meta.budget,
            "scored_indices": selection_meta.scored_indices,
            "probes": selection_meta.probes,
            "cutoff_idx_inclusive": selection_meta.cutoff_idx_inclusive,
            "best_idx": selection_meta.best_idx,
            "ranked_indices_best_to_worst": ranked_indices,
        }
    }
    bundle["consumed"] = False
    bundle["consumed_at_utc"] = None

    return bundle


def maybe_write_bundle_json(*, local_run_dir: Path, bundle: dict[str, Any], enabled: bool) -> None:
    if not enabled:
        return
    ensure_dir(local_run_dir)
    # Keep insertion order for human readability (we intentionally put `summary` first).
    (local_run_dir / "summary.json").write_text(json.dumps(bundle, indent=2, sort_keys=False), encoding="utf-8")

