"""
Detection summary store (Python-only persistence).

This module provides a small in-process store with optional JSON persistence on disk.
Multiple AppDaemon apps in the same process can publish and consume "SummaryBundle"
objects keyed by bundle_key (e.g. "garage").
"""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


@dataclass(frozen=True)
class StoreConfig:
    state_path: Path
    max_bundles_per_key: int = 50


class DetectionSummaryStore:
    """
    Thread-safe store with:
    - in-memory access (fast)
    - JSON persistence (durable across AppDaemon restarts)
    - wait/notify for consumers (Condition variable)
    """

    def __init__(self, config: Optional[StoreConfig] = None):
        default_state_path = Path(
            os.environ.get(
                "DETECTION_SUMMARY_STATE_PATH",
                str(Path(__file__).resolve().parent / "_state" / "detection_summary_store.json"),
            )
        )
        self._config = config or StoreConfig(state_path=default_state_path)
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._data: dict[str, Any] = {"version": 1, "bundles": {}}
        self._load()

    # --- persistence -----------------------------------------------------

    def _load(self) -> None:
        path = self._config.state_path
        try:
            if not path.exists():
                return
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "bundles" in parsed:
                self._data = parsed
        except Exception:
            # If corrupted, keep memory store working; next save will overwrite.
            return

    def _save(self) -> None:
        path = self._config.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    # --- public API ------------------------------------------------------

    def publish_bundle(self, bundle_key: str, bundle: dict[str, Any]) -> None:
        """
        Publish a new SummaryBundle under bundle_key.
        Bundle is deep-copied so callers can mutate their copy safely.
        """
        created_at_epoch = _safe_float(bundle.get("created_at_epoch"), default=time.time())
        bundle = deepcopy(bundle)
        bundle.setdefault("created_at_epoch", created_at_epoch)
        bundle.setdefault("created_at_utc", _utc_iso(created_at_epoch))
        bundle.setdefault("bundle_key", bundle_key)
        bundle.setdefault("consumed", False)

        with self._cv:
            bundles = self._data.setdefault("bundles", {}).setdefault(bundle_key, [])
            bundles.append(bundle)
            # Keep newest first and cap length
            bundles.sort(key=lambda b: _safe_float(b.get("created_at_epoch")), reverse=True)
            del bundles[self._config.max_bundles_per_key :]
            self._save()
            self._cv.notify_all()

    def get_best_bundle(
        self,
        bundle_key: str,
        window_start_epoch: float,
        window_end_epoch: float,
        *,
        include_consumed: bool = False,
        max_age_s: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Return the best eligible bundle (highest score; tie-breaker newest).
        """
        now = time.time()
        with self._lock:
            bundles: list[dict[str, Any]] = self._data.get("bundles", {}).get(bundle_key, []) or []
            eligible: list[dict[str, Any]] = []
            for b in bundles:
                created = _safe_float(b.get("created_at_epoch"))
                if created < window_start_epoch or created > window_end_epoch:
                    continue
                if (not include_consumed) and b.get("consumed"):
                    continue
                if max_age_s is not None and (now - created) > max_age_s:
                    continue
                eligible.append(b)

            if not eligible:
                return None

            def score(bundle: dict[str, Any]) -> float:
                best = bundle.get("best") or {}
                return _safe_float(best.get("score"))

            eligible.sort(
                key=lambda b: (score(b), _safe_float(b.get("created_at_epoch"))),
                reverse=True,
            )
            return deepcopy(eligible[0])

    def wait_for_bundle(
        self,
        bundle_key: str,
        window_start_epoch: float,
        window_end_epoch: float,
        timeout_s: float,
        *,
        include_consumed: bool = False,
        max_age_s: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Wait up to timeout_s for an eligible bundle to appear.
        """
        deadline = time.time() + max(0.0, timeout_s)
        with self._cv:
            while True:
                found = self.get_best_bundle(
                    bundle_key,
                    window_start_epoch,
                    window_end_epoch,
                    include_consumed=include_consumed,
                    max_age_s=max_age_s,
                )
                if found:
                    return found
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)

    def get_bundle_by_run_id(
        self,
        bundle_key: str,
        run_id: str,
        *,
        include_consumed: bool = False,
    ) -> Optional[dict[str, Any]]:
        """
        Return a bundle by run_id (deep-copied), or None.
        """
        with self._lock:
            bundles: list[dict[str, Any]] = self._data.get("bundles", {}).get(bundle_key, []) or []
            for b in bundles:
                if str(b.get("run_id")) != str(run_id):
                    continue
                if (not include_consumed) and b.get("consumed"):
                    return None
                return deepcopy(b)
        return None

    def wait_for_run_id(
        self,
        bundle_key: str,
        run_id: str,
        *,
        timeout_s: float,
        include_consumed: bool = False,
    ) -> Optional[dict[str, Any]]:
        """
        Wait up to timeout_s for a specific run_id to appear.
        """
        deadline = time.time() + max(0.0, float(timeout_s))
        with self._cv:
            while True:
                found = self.get_bundle_by_run_id(bundle_key, run_id, include_consumed=include_consumed)
                if found:
                    return found
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)

    def mark_consumed(self, bundle_key: str, run_id: str) -> bool:
        """
        Mark a bundle as consumed (idempotent). Returns True if found.
        """
        with self._cv:
            bundles: list[dict[str, Any]] = self._data.get("bundles", {}).get(bundle_key, []) or []
            for b in bundles:
                if str(b.get("run_id")) == str(run_id):
                    if not b.get("consumed"):
                        b["consumed"] = True
                        b["consumed_at_utc"] = _utc_iso(time.time())
                        self._save()
                        self._cv.notify_all()
                    return True
            return False

    def cleanup(self, retention_hours: float) -> None:
        """
        Prune bundles older than retention_hours.
        """
        cutoff = time.time() - (max(0.0, retention_hours) * 3600.0)
        with self._cv:
            bundles_by_key: dict[str, list[dict[str, Any]]] = self._data.get("bundles", {}) or {}
            changed = False
            for key, bundles in list(bundles_by_key.items()):
                new_bundles = [b for b in bundles if _safe_float(b.get("created_at_epoch")) >= cutoff]
                if len(new_bundles) != len(bundles):
                    bundles_by_key[key] = new_bundles
                    changed = True
            if changed:
                self._save()
                self._cv.notify_all()


# Module-level shared store instance (shared across apps in the same AD process).
STORE = DetectionSummaryStore()

