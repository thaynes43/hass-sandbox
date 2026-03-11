"""Persist sync state between runs (file hash, event UIDs, horizon)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional


@dataclass
class SyncState:
    file_hash: str = ""
    horizon_end: Optional[str] = None  # ISO date string
    events: Dict[str, str] = field(default_factory=dict)  # sync_key -> HA event UID
    last_sync_iso: str = ""


def load_sync_state(path: str | Path) -> SyncState:
    """Load sync state from a JSON file. Returns empty state if missing."""
    p = Path(path)
    if not p.exists():
        return SyncState()
    try:
        data = json.loads(p.read_text())
        return SyncState(
            file_hash=data.get("file_hash", ""),
            horizon_end=data.get("horizon_end"),
            events=data.get("events", {}),
            last_sync_iso=data.get("last_sync_iso", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return SyncState()


def save_sync_state(state: SyncState, path: str | Path) -> None:
    """Persist sync state to a JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "file_hash": state.file_hash,
        "horizon_end": state.horizon_end,
        "events": state.events,
        "last_sync_iso": state.last_sync_iso,
    }
    p.write_text(json.dumps(data, indent=2))
