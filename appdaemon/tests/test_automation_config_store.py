"""Tests for AutomationConfigStore — YAML-backed automation config persistence."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from vestaboard_controller_app.automation_config_store import AutomationConfigStore


# ---------------------------------------------------------------------------
# Load / non-existent file
# ---------------------------------------------------------------------------


class TestLoad:
    def test_load_nonexistent_file_returns_empty(self, tmp_path):
        """Loading from a non-existent file returns an empty dict."""
        store = AutomationConfigStore(str(tmp_path / "does_not_exist.yaml"))
        result = store.load()
        assert result == {}

    def test_load_existing_file(self, tmp_path):
        """Loading from an existing YAML file populates config."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(
                {"clock": {"enabled": True, "ttl_minutes": 5}},
                default_flow_style=False,
            )
        )
        store = AutomationConfigStore(str(config_path))
        result = store.load()
        assert result == {"clock": {"enabled": True, "ttl_minutes": 5}}

    def test_load_empty_file_returns_empty(self, tmp_path):
        """Loading from an empty YAML file returns empty dict."""
        config_path = tmp_path / "empty.yaml"
        config_path.write_text("")
        store = AutomationConfigStore(str(config_path))
        result = store.load()
        assert result == {}


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


class TestSeed:
    def test_seed_new_automation_returns_true(self, tmp_path):
        """Seeding a new automation_id returns True and stores defaults."""
        store = AutomationConfigStore(str(tmp_path / "config.yaml"))
        store.load()
        result = store.seed("clock", {"enabled": True, "ttl_minutes": None})
        assert result is True
        assert store.get("clock") == {"enabled": True, "ttl_minutes": None}

    def test_seed_existing_automation_returns_false(self, tmp_path):
        """Seeding an existing automation_id returns False and does NOT overwrite."""
        store = AutomationConfigStore(str(tmp_path / "config.yaml"))
        store.load()
        store.seed("clock", {"enabled": True, "ttl_minutes": 5})
        # Try to seed again with different defaults
        result = store.seed("clock", {"enabled": False, "ttl_minutes": 99})
        assert result is False
        # Original values preserved
        assert store.get("clock")["enabled"] is True
        assert store.get("clock")["ttl_minutes"] == 5


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_existing_returns_copy(self, tmp_path):
        """get() returns a copy of the config dict, not a reference."""
        store = AutomationConfigStore(str(tmp_path / "config.yaml"))
        store.load()
        store.seed("clock", {"enabled": True})

        cfg = store.get("clock")
        cfg["enabled"] = False  # mutate the returned copy
        # Original should be unchanged
        assert store.get("clock")["enabled"] is True

    def test_get_missing_returns_empty_dict(self, tmp_path):
        """get() for an unknown automation_id returns empty dict."""
        store = AutomationConfigStore(str(tmp_path / "config.yaml"))
        store.load()
        assert store.get("nonexistent") == {}


# ---------------------------------------------------------------------------
# Update and save
# ---------------------------------------------------------------------------


class TestUpdateAndSave:
    def test_update_merges_and_saves(self, tmp_path):
        """update() merges partial config and persists to file."""
        config_path = tmp_path / "config.yaml"
        store = AutomationConfigStore(str(config_path))
        store.load()
        store.seed("clock", {"enabled": True, "ttl_minutes": 5})
        store.save()

        # Update a single field
        store.update("clock", {"ttl_minutes": 10})

        # Verify in memory
        assert store.get("clock")["ttl_minutes"] == 10
        assert store.get("clock")["enabled"] is True  # untouched

        # Verify persisted to disk
        with open(config_path) as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["clock"]["ttl_minutes"] == 10
        assert on_disk["clock"]["enabled"] is True

    def test_update_creates_automation_if_missing(self, tmp_path):
        """update() on unknown automation_id creates a new entry."""
        config_path = tmp_path / "config.yaml"
        store = AutomationConfigStore(str(config_path))
        store.load()

        store.update("new_auto", {"enabled": False})
        assert store.get("new_auto") == {"enabled": False}

    def test_atomic_write_uses_tmp_file(self, tmp_path):
        """save() writes to .tmp then renames (no .tmp file left behind)."""
        config_path = tmp_path / "config.yaml"
        store = AutomationConfigStore(str(config_path))
        store.load()
        store.seed("clock", {"enabled": True})
        store.save()

        # .tmp should not exist after save
        tmp_file = config_path.with_suffix(".tmp")
        assert not tmp_file.exists()
        # .yaml should exist
        assert config_path.exists()


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------


class TestFullCycle:
    def test_seed_save_reload_update_cycle(self, tmp_path):
        """Full seed -> save -> reload -> update cycle."""
        config_path = tmp_path / "config.yaml"

        # Phase 1: seed and save
        store1 = AutomationConfigStore(str(config_path))
        store1.load()
        store1.seed("clock", {"enabled": True, "ttl_minutes": None})
        store1.seed("messages", {"enabled": False, "frequency_min_minutes": 30})
        store1.save()

        # Phase 2: reload in new instance
        store2 = AutomationConfigStore(str(config_path))
        loaded = store2.load()
        assert "clock" in loaded
        assert "messages" in loaded
        assert loaded["clock"]["enabled"] is True
        assert loaded["messages"]["frequency_min_minutes"] == 30

        # Phase 3: update and verify
        store2.update("messages", {"frequency_min_minutes": 60, "enabled": True})
        assert store2.get("messages")["frequency_min_minutes"] == 60
        assert store2.get("messages")["enabled"] is True

        # Phase 4: verify persistence
        store3 = AutomationConfigStore(str(config_path))
        store3.load()
        assert store3.get("messages")["frequency_min_minutes"] == 60

    def test_save_creates_parent_directories(self, tmp_path):
        """save() should create parent dirs if they don't exist."""
        config_path = tmp_path / "nested" / "dir" / "config.yaml"
        store = AutomationConfigStore(str(config_path))
        store.load()
        store.seed("clock", {"enabled": True})
        store.save()
        assert config_path.exists()
