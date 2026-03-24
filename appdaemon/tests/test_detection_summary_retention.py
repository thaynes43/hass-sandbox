"""Unit tests for detection_summary_app.retention."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.retention import archive_runs, prune_runs_to_max, recent_published_run_ids  # noqa: E402


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


def test_archive_runs_moves_oldest_to_monthly_folders():
    with TemporaryDirectory() as td:
        runs_dir = Path(td) / "runs"
        # Jan 15 2026, Feb 10 2026, Mar 5 2026
        _write_summary(runs_dir / "jan-run", created_at_epoch=1768521600.0)  # 2026-01-15
        _write_summary(runs_dir / "feb-run", created_at_epoch=1770768000.0)  # 2026-02-10
        _write_summary(runs_dir / "mar-run", created_at_epoch=1772870400.0)  # 2026-03-05

        archived = archive_runs(runs_dir=runs_dir, keep=1)
        assert archived == 2

        # Newest stays in runs/
        assert (runs_dir / "mar-run" / "summary.json").exists()
        # Older moved to archive
        assert (runs_dir / "archive" / "2026-01" / "jan-run" / "summary.json").exists()
        assert (runs_dir / "archive" / "2026-02" / "feb-run" / "summary.json").exists()
        # No longer in runs/
        assert not (runs_dir / "jan-run").exists()
        assert not (runs_dir / "feb-run").exists()


def test_archive_runs_no_op_when_under_keep():
    with TemporaryDirectory() as td:
        runs_dir = Path(td) / "runs"
        _write_summary(runs_dir / "r1", created_at_epoch=100.0)
        _write_summary(runs_dir / "r2", created_at_epoch=200.0)

        archived = archive_runs(runs_dir=runs_dir, keep=5)
        assert archived == 0
        assert (runs_dir / "r1").exists()
        assert (runs_dir / "r2").exists()
        assert not (runs_dir / "archive").exists()


def test_archive_runs_skips_already_archived():
    with TemporaryDirectory() as td:
        runs_dir = Path(td) / "runs"
        _write_summary(runs_dir / "old-run", created_at_epoch=1768521600.0)  # 2026-01-15
        _write_summary(runs_dir / "new-run", created_at_epoch=1772870400.0)  # 2026-03-05

        # Pre-create archive destination
        archive_dest = runs_dir / "archive" / "2026-01" / "old-run"
        archive_dest.mkdir(parents=True)
        (archive_dest / "summary.json").write_text("{}", encoding="utf-8")

        archived = archive_runs(runs_dir=runs_dir, keep=1)
        assert archived == 1
        # Source removed even though dest already existed
        assert not (runs_dir / "old-run").exists()
        # Archive still intact
        assert (archive_dest / "summary.json").exists()


def test_archive_runs_does_not_treat_archive_dir_as_a_run():
    """The archive/ dir itself has no summary.json and must not be listed as a run."""
    with TemporaryDirectory() as td:
        runs_dir = Path(td) / "runs"
        _write_summary(runs_dir / "r1", created_at_epoch=100.0)
        _write_summary(runs_dir / "r2", created_at_epoch=200.0)
        _write_summary(runs_dir / "r3", created_at_epoch=300.0)

        # First pass — archive r1
        archived = archive_runs(runs_dir=runs_dir, keep=2)
        assert archived == 1
        assert (runs_dir / "archive").exists()

        # Second pass — archive/ dir should not confuse the count
        archived2 = archive_runs(runs_dir=runs_dir, keep=2)
        assert archived2 == 0

