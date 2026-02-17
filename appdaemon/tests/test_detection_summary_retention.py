"""Unit tests for detection_summary_app.retention."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.retention import prune_old_runs, recent_published_run_ids  # noqa: E402


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


def test_prune_old_runs_deletes_older_than_retention_hours():
    with TemporaryDirectory() as td:
        runs_dir = Path(td) / "runs"
        now = time.time()
        _write_summary(runs_dir / "old", created_at_epoch=now - 10 * 3600)
        _write_summary(runs_dir / "new", created_at_epoch=now - 60)

        deleted = prune_old_runs(runs_dir=runs_dir, retention_hours=1)
        assert deleted == 1
        assert not (runs_dir / "old").exists()
        assert (runs_dir / "new").exists()

