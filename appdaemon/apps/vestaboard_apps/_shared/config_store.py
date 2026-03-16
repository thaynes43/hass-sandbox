"""Persistent UI-editable automation configuration stored in YAML."""

from __future__ import annotations

import yaml
from pathlib import Path
from typing import Optional


class AutomationConfigStore:
    """Reads/writes per-automation configuration from a YAML file.

    Used by the controller to persist UI-editable settings (enabled, ttl,
    frequency ranges, etc.) across restarts. The file is seeded with
    defaults from each automation's DEFAULT_UI_CONFIG on first run.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._config: dict[str, dict] = {}

    def load(self) -> dict[str, dict]:
        """Read YAML config file. Returns empty dict if file doesn't exist."""
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
        else:
            self._config = {}
        return self._config

    def save(self) -> None:
        """Atomic write: write to .tmp then rename."""
        tmp = self._path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(self._config, f, default_flow_style=False, sort_keys=False)
        tmp.rename(self._path)

    def seed(self, automation_id: str, defaults: dict) -> bool:
        """If automation_id not in config, write defaults. Idempotent."""
        if automation_id not in self._config:
            self._config[automation_id] = dict(defaults)
            return True
        return False

    def get(self, automation_id: str) -> dict:
        """Get config for one automation. Returns empty dict if not found."""
        return dict(self._config.get(automation_id, {}))

    def update(self, automation_id: str, partial_config: dict) -> None:
        """Merge partial update into automation config and save."""
        if automation_id not in self._config:
            self._config[automation_id] = {}
        self._config[automation_id].update(partial_config)
        self.save()
