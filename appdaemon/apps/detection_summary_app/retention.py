from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class PublishedRun:
    run_id: str
    created_at_epoch: float
    run_dir: Path


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def delete_run_dir(run_dir: Path) -> bool:
    """
    Delete a per-run directory (idempotent). Returns True if it existed.
    """
    try:
        if not run_dir.exists():
            return False
        shutil.rmtree(run_dir, ignore_errors=True)
        return True
    except Exception:
        return False


def _read_summary_created_at(run_dir: Path) -> Optional[float]:
    p = run_dir / "summary.json"
    if not p.exists():
        return None
    try:
        parsed = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            return None
        return _safe_float(parsed.get("created_at_epoch"), default=None)  # type: ignore[arg-type]
    except Exception:
        return None


def list_published_runs(*, runs_dir: Path) -> list[PublishedRun]:
    """
    A run is considered \"published\" if it has summary.json.
    Returns newest-first by created_at_epoch (falling back to mtime).
    """
    out: list[PublishedRun] = []
    if not runs_dir.exists():
        return out
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        created = _read_summary_created_at(child)
        if created is None:
            continue
        out.append(PublishedRun(run_id=child.name, created_at_epoch=float(created), run_dir=child))

    # Newest first
    out.sort(key=lambda r: float(r.created_at_epoch), reverse=True)
    return out


def prune_runs_to_max(*, runs_dir: Path, max_runs: int) -> int:
    """
    Delete oldest published runs until we have at most max_runs.

    Uses summary.json created_at_epoch ordering (newest-first), and deletes from the tail.
    Returns deleted count.
    """
    max_runs = int(max_runs)
    if max_runs <= 0:
        return 0
    runs = list_published_runs(runs_dir=runs_dir)
    if len(runs) <= max_runs:
        return 0
    deleted = 0
    for r in runs[max_runs:]:
        if delete_run_dir(r.run_dir):
            deleted += 1
    return int(deleted)


def recent_published_run_ids(*, runs_dir: Path, max_options: int) -> list[str]:
    """
    Return newest-first published run IDs, capped.
    """
    max_options = max(1, int(max_options))
    runs = list_published_runs(runs_dir=runs_dir)
    return [r.run_id for r in runs[:max_options]]

