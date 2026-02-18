"""Unit tests for detection_summary_app.retention."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.retention import prune_runs_to_max, recent_published_run_ids  # noqa: E402


def _write_summary(run_dir: Path, *, created_at_epoch: float) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_dir.name, "created_at_epoch": float(created_at_epoch)}
    (run_dir / "summary.json").write_text(json.dumps(payload), encoding="utf-8")


def test_recent_published_run_ids_orders_newest_first():
    with TemporaryDirectory() as td:
        runs_dir = Path(td) / "runs"
        _write_summary(runs_dir / "r1", created_at_epoch=100.0)
        _write_summary(runs_dir / "r2", created_at_epoch=300.0)
        _write_summary(runs_dir / "r3", created_at_epoch=200.0)

        got = recent_published_run_ids(runs_dir=runs_dir, max_options=10)
        assert got == ["r2", "r3", "r1"]


def test_prune_runs_to_max_deletes_oldest_when_over_capacity():
    with TemporaryDirectory() as td:
        runs_dir = Path(td) / "runs"
        _write_summary(runs_dir / "oldest", created_at_epoch=100.0)
        _write_summary(runs_dir / "middle", created_at_epoch=200.0)
        _write_summary(runs_dir / "newest", created_at_epoch=300.0)

        deleted = prune_runs_to_max(runs_dir=runs_dir, max_runs=2)
        assert deleted == 1
        assert not (runs_dir / "oldest").exists()
        assert (runs_dir / "middle").exists()
        assert (runs_dir / "newest").exists()

