"""
Dashboard viewer cache utilities.

Goal: make dashboard switching instant and cache-safe by pre-staging a bounded set
of runs into HA `/config/www` (served as `/local`) using HA's own access to both
`/media` and `/config/www`.

AppDaemon can always write to `/media` but cannot reach `/config`. Therefore:

- AppDaemon copies the required run images into a single staging dir under `/media`
  with stable, unique filenames: `<run_id>_best.jpg` and `<run_id>_generated.png`.
- AppDaemon calls a HA `shell_command` that wipes the destination `/config/www/.../viewer/`
  folder and copies everything from staging to destination.
- AppDaemon updates the input_select options to match exactly what exists in the viewer folder.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ViewerCacheConfig:
    snapshot_ha_dir: str  # e.g. /media/detection-summary/garage
    bundle_runs_subdir: str  # e.g. runs
    captured_subdir: str  # e.g. captured
    viewer_stage_subdir: str  # e.g. viewer_stage
    viewer_www_subdir: str  # e.g. viewer
    refresh_shell_command: str  # e.g. ds_refresh_detection_summary_viewer_www


class ViewerCache:
    """
    Helper used by DetectionSummary; kept separate so `manager.py` stays orchestration-focused.
    """

    def __init__(
        self,
        *,
        cfg: ViewerCacheConfig,
        ha_path_to_local_fs: Any,
        call_service: Any,
        log: Any,
    ) -> None:
        self.cfg = cfg
        self._ha_path_to_local_fs = ha_path_to_local_fs
        self._call_service = call_service
        self._log = log

    def _snapshot_rel(self) -> Optional[str]:
        snap = (self.cfg.snapshot_ha_dir or "").strip()
        if not snap.startswith("/media/"):
            return None
        return snap[len("/media/") :]

    def _runs_root_local(self) -> Path:
        return self._ha_path_to_local_fs(self.cfg.snapshot_ha_dir) / self.cfg.bundle_runs_subdir

    def _stage_dir_local(self) -> Path:
        return self._ha_path_to_local_fs(self.cfg.snapshot_ha_dir) / self.cfg.viewer_stage_subdir

    def _www_dir_ha(self) -> Optional[str]:
        rel = self._snapshot_rel()
        if not rel:
            return None
        return f"/config/www/{rel}/{self.cfg.viewer_www_subdir}"

    def _resolve_best_src(self, run_dir: Path) -> Optional[Path]:
        best_src = run_dir / "best.jpg"
        if best_src.exists():
            return best_src
        try:
            p = run_dir / "summary.json"
            if p.exists():
                import json as _json

                parsed = _json.loads(p.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    # Prefer summary.best_idx; fall back to best.idx if present.
                    best_idx = int(
                        (parsed.get("summary") or {}).get("best_idx")
                        or (parsed.get("best") or {}).get("idx")
                        or -1
                    )
                    if best_idx >= 0:
                        cand = run_dir / self.cfg.captured_subdir / f"{best_idx}.jpg"
                        if cand.exists():
                            return cand
        except Exception:
            return None
        return None

    def stage_run_ids_to_media(self, requested_run_ids: list[str]) -> list[str]:
        """
        Stage requested run_ids into `/media/.../<viewer_stage_subdir>/` as:
        - `<run_id>_best.jpg`
        - `<run_id>_generated.png`

        Returns the list of run_ids successfully staged (in the same order).
        """
        stage_dir = self._stage_dir_local()
        stage_dir.mkdir(parents=True, exist_ok=True)

        # Clear previous stage so refresh can wipe+fill with no leftovers.
        for p in stage_dir.iterdir():
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                # Best-effort cleanup; staging will overwrite filenames anyway.
                continue

        runs_root = self._runs_root_local()
        ok: list[str] = []

        for rid in requested_run_ids:
            run_id = str(rid or "").strip()
            if not run_id:
                continue
            run_dir = runs_root / run_id
            if not run_dir.exists():
                continue
            best_src = self._resolve_best_src(run_dir)
            if not best_src or not best_src.exists():
                continue
            gen_src = run_dir / "generated.png"
            if not gen_src.exists():
                gen_src = best_src

            best_dst = stage_dir / f"{run_id}_best.jpg"
            gen_dst = stage_dir / f"{run_id}_generated.png"
            try:
                shutil.copyfile(best_src, best_dst)
                shutil.copyfile(gen_src, gen_dst)
                ok.append(run_id)
            except Exception as e:
                self._log(
                    f"ViewerCache: stage failed run_id={run_id} best_src={best_src} gen_src={gen_src} "
                    f"stage_dir={stage_dir}: {e!r}",
                    level="WARNING",
                )

        self._log(
            f"ViewerCache: staged runs={len(ok)}/{len(requested_run_ids)} stage_dir={stage_dir}",
            level="INFO",
        )
        return ok

    def refresh_www_from_stage(self) -> None:
        """
        Ask HA to wipe+fill `/config/www/.../<viewer_www_subdir>/` from the stage dir.
        """
        snapshot_rel = self._snapshot_rel()
        www_dir = self._www_dir_ha()
        if not snapshot_rel or not www_dir:
            raise ValueError("snapshot_ha_dir must start with /media/ to refresh www viewer cache")
        self._log(
            f"ViewerCache: refresh www start snapshot_rel={snapshot_rel} www_dir={www_dir} "
            f"shell_command={self.cfg.refresh_shell_command}",
            level="INFO",
        )
        self._call_service(
            f"shell_command/{self.cfg.refresh_shell_command}",
            snapshot_rel=snapshot_rel,
            viewer_www_subdir=self.cfg.viewer_www_subdir,
            viewer_stage_subdir=self.cfg.viewer_stage_subdir,
        )
        self._log(
            f"ViewerCache: refresh www done snapshot_rel={snapshot_rel} www_dir={www_dir}",
            level="INFO",
        )

    def selected_file_paths_for_run(self, run_id: str) -> tuple[Optional[str], Optional[str]]:
        """
        Returns HA `/config/www/...` file paths for the best/generated staged files for `run_id`.
        """
        snapshot_rel = self._snapshot_rel()
        if not snapshot_rel:
            return (None, None)
        run_id = str(run_id or "").strip()
        if not run_id:
            return (None, None)
        base = f"/config/www/{snapshot_rel}/{self.cfg.viewer_www_subdir}"
        return (f"{base}/{run_id}_best.jpg", f"{base}/{run_id}_generated.png")

