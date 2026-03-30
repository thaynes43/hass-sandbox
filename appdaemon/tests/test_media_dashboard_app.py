"""Unit tests for MediaDashboardApp.

Mocks AppDaemon methods, HAProvisioner, and all three fetchers —
no real network or HA access required.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from media_dashboard_app.media_dashboard_app import (
    MediaDashboardApp,
    SENSOR_STATUS,
    SENSOR_DETAIL,
    COMMAND_EVENT,
)
from providers.media_providers.types import (
    FetchResult,
    MediaItem,
    ShowtimeCache,
    ShowtimeEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    id: str = "tmdb-1",
    title: str = "Test Movie",
    tmdb_popularity: float = 50.0,
    added_at: str = "",
    release_date: str = "",
    source: str = "tmdb",
    tmdb_id: int = 1,
    summary: str = "",
    has_showtimes: bool = False,
    local_poster: str = "",
) -> MediaItem:
    return MediaItem(
        id=id,
        title=title,
        tmdb_popularity=tmdb_popularity,
        added_at=added_at,
        release_date=release_date,
        source=source,
        tmdb_id=tmdb_id,
        summary=summary,
        has_showtimes=has_showtimes,
        local_poster=local_poster or f"{id}.jpg",
    )


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


def _days_ago_iso(days: int) -> str:
    dt = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=days)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

_DEFAULT_ARGS: Dict[str, Any] = {
    "tautulli_url": "http://tautulli:8181",
    "tautulli_api_key_env": "TAUTULLI_API_KEY",
    "tmdb_api_key_env": "TMDB_API_KEY",
    "serpapi_api_key_env": "SERPAPI_KEY",
    "location": "Westford, MA",
    "theaters": ["AMC Tyngsboro 12"],
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "max_items_per_category": 10,
    "popularity_threshold": 10.0,
    "vote_count_threshold": 50,
    "stale_ttl_days": 7,
    "poster_sync_shell_command": "media_dashboard_sync_posters",
}


def _make_app(extra_args: dict | None = None, tmpdir: str | None = None) -> MediaDashboardApp:
    """Create a MediaDashboardApp with mocked AD methods and temp directories."""
    td = tmpdir or tempfile.mkdtemp(prefix="mda_test_")

    ad = MagicMock()
    app = MediaDashboardApp(ad, MagicMock())

    args = dict(_DEFAULT_ARGS)
    # Point filesystem paths to temp dir
    args["media_fs_root"] = td
    args["poster_media_subdir"] = "posters"
    args["poster_www_subdir"] = "posters"
    args["preferences_file_subdir"] = "prefs"
    args["showtime_cache_subdir"] = "showtimes"

    if extra_args:
        args.update(extra_args)

    app.args = args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.run_daily = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()
    app.name = "media_dashboard_app"

    # Patch fetcher constructors so initialize() doesn't try to resolve real secrets
    with patch("media_dashboard_app.media_dashboard_app.TautulliFetcher") as MockT, \
         patch("media_dashboard_app.media_dashboard_app.TmdbFetcher") as MockTM, \
         patch("media_dashboard_app.media_dashboard_app.SerpApiFetcher") as MockS:

        mock_tautulli = MagicMock()
        mock_tautulli.fetch_recently_added = AsyncMock(
            return_value=FetchResult(items=[], status="ok")
        )
        mock_tautulli.download_posters = AsyncMock(return_value=0)
        MockT.return_value = mock_tautulli

        mock_tmdb = MagicMock()
        mock_tmdb.fetch_in_theaters = AsyncMock(
            return_value=FetchResult(items=[], status="ok")
        )
        mock_tmdb.fetch_coming_soon = AsyncMock(
            return_value=FetchResult(items=[], status="ok")
        )
        mock_tmdb.download_posters = AsyncMock(return_value=0)
        mock_tmdb.fetch_detail = AsyncMock(
            return_value=_make_item(summary="A great film")
        )
        MockTM.return_value = mock_tmdb

        mock_serpapi = MagicMock()
        mock_serpapi.fetch_showtimes = AsyncMock(
            return_value=ShowtimeCache(date=datetime.date.today().isoformat(), films={})
        )
        MockS.return_value = mock_serpapi

        app.initialize()

    return app


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _startup(app: MediaDashboardApp) -> None:
    """Run the async startup coroutine with provisioner mocked out."""
    mock_prov = MagicMock()
    mock_prov.ensure_script = AsyncMock(return_value=False)
    with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
        _run(app._async_startup())


# ---------------------------------------------------------------------------
# Tests: Initialization
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.run_in.assert_called_once()
        callback = app.run_in.call_args[0][0]
        delay = app.run_in.call_args[0][1]
        assert callable(callback)
        assert delay == 0

    def test_initialize_creates_directories(self):
        td = tempfile.mkdtemp(prefix="mda_dirs_")
        app = _make_app(tmpdir=td)
        assert os.path.isdir(os.path.join(td, "posters"))
        assert os.path.isdir(os.path.join(td, "prefs"))
        assert os.path.isdir(os.path.join(td, "showtimes"))

    def test_initialize_fetchers_instantiated(self):
        td = tempfile.mkdtemp(prefix="mda_fetch_")
        with patch("media_dashboard_app.media_dashboard_app.TautulliFetcher") as MockT, \
             patch("media_dashboard_app.media_dashboard_app.TmdbFetcher") as MockTM, \
             patch("media_dashboard_app.media_dashboard_app.SerpApiFetcher") as MockS:

            MockT.return_value = MagicMock()
            MockTM.return_value = MagicMock()
            MockS.return_value = MagicMock()

            ad = MagicMock()
            app = MediaDashboardApp(ad, MagicMock())
            args = dict(_DEFAULT_ARGS)
            args["media_fs_root"] = td
            args["poster_media_subdir"] = "posters"
            args["preferences_file_subdir"] = "prefs"
            args["showtime_cache_subdir"] = "showtimes"
            app.args = args
            for attr in ("get_state", "set_state", "call_service", "listen_state",
                         "listen_event", "fire_event", "run_in", "run_every",
                         "cancel_timer", "log", "create_task"):
                setattr(app, attr, MagicMock())

            app.initialize()

            MockT.assert_called_once()
            MockTM.assert_called_once()
            MockS.assert_called_once()

    def test_initialize_loads_preferences_from_file(self):
        td = tempfile.mkdtemp(prefix="mda_prefs_")
        prefs_dir = os.path.join(td, "prefs")
        os.makedirs(prefs_dir, exist_ok=True)
        prefs_file = os.path.join(prefs_dir, "preferences.json")
        prefs_data = {
            "hidden": ["tmdb-999"],
            "liked": ["plex-111"],
            "hidden_at": {"tmdb-999": "2026-01-01T00:00:00"},
            "liked_at": {"plex-111": "2026-01-02T00:00:00"},
        }
        with open(prefs_file, "w") as f:
            json.dump(prefs_data, f)

        app = _make_app(tmpdir=td)
        assert "tmdb-999" in app._preferences["hidden"]
        assert "plex-111" in app._preferences["liked"]


# ---------------------------------------------------------------------------
# Tests: Sensor publication
# ---------------------------------------------------------------------------

class TestPublishSensor:
    def test_sensor_attributes_match_schema(self):
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-1", "Movie A")]
        app._fetch_status = {"tautulli": "ok", "tmdb": "ok", "serpapi": "ok"}

        app._publish_sensor()

        app.set_state.assert_called()
        call = app.set_state.call_args
        entity_id = call[0][0]
        state = call[1]["state"]
        attrs = call[1]["attributes"]

        assert entity_id == SENSOR_STATUS
        assert state == "ok"
        assert "categories" in attrs
        assert "fetch_status" in attrs
        assert "last_updated" in attrs
        assert "friendly_name" in attrs
        assert "icon" in attrs
        assert "in_theaters" in attrs["categories"]
        assert "plex_new" in attrs["categories"]
        assert "coming_soon" in attrs["categories"]

    def test_sensor_state_all_ok(self):
        app = _make_app()
        app._fetch_status = {"tautulli": "ok", "tmdb": "ok", "serpapi": "ok"}
        app._publish_sensor()
        assert app.set_state.call_args[1]["state"] == "ok"

    def test_sensor_state_partial_when_some_error(self):
        app = _make_app()
        app._fetch_status = {"tautulli": "ok", "tmdb": "error", "serpapi": "ok"}
        app._publish_sensor()
        assert app.set_state.call_args[1]["state"] == "partial"

    def test_sensor_state_error_when_all_error(self):
        app = _make_app()
        app._fetch_status = {"tautulli": "error", "tmdb": "error", "serpapi": "error"}
        app._publish_sensor()
        assert app.set_state.call_args[1]["state"] == "error"

    def test_caps_items_at_max(self):
        app = _make_app(extra_args={"max_items_per_category": 3})
        app._categories["in_theaters"] = [
            _make_item(f"tmdb-{i}", f"Movie {i}", tmdb_popularity=float(100 - i))
            for i in range(15)
        ]
        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        assert len(attrs["categories"]["in_theaters"]) == 3

    def test_items_serialized_as_dicts(self):
        app = _make_app()
        app._categories["plex_new"] = [_make_item("plex-1", "Plex Movie")]
        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        items = attrs["categories"]["plex_new"]
        assert len(items) == 1
        assert isinstance(items[0], dict)
        assert items[0]["id"] == "plex-1"
        assert items[0]["title"] == "Plex Movie"


# ---------------------------------------------------------------------------
# Tests: Preferences
# ---------------------------------------------------------------------------

class TestPreferences:
    def test_dismiss_adds_to_hidden(self):
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-42", "Hide Me")]

        app._handle_dismiss({"id": "tmdb-42"})

        assert "tmdb-42" in app._preferences["hidden"]

    def test_like_adds_to_liked(self):
        app = _make_app()
        app._categories["plex_new"] = [_make_item("plex-7", "Love It")]

        app._handle_like({"id": "plex-7"})

        assert "plex-7" in app._preferences["liked"]

    def test_undo_dismiss_removes_from_hidden(self):
        app = _make_app()
        app._preferences["hidden"] = ["tmdb-42"]
        app._preferences["hidden_at"] = {"tmdb-42": "2026-01-01T00:00:00"}

        app._handle_undo_dismiss({"id": "tmdb-42"})

        assert "tmdb-42" not in app._preferences["hidden"]
        assert "tmdb-42" not in app._preferences["hidden_at"]

    def test_preferences_persist_to_file_round_trip(self):
        td = tempfile.mkdtemp(prefix="mda_roundtrip_")
        app = _make_app(tmpdir=td)

        app._handle_dismiss({"id": "tmdb-99"})
        app._handle_like({"id": "plex-55"})

        # Create a second app instance pointing at the same dir
        app2 = _make_app(tmpdir=td)

        assert "tmdb-99" in app2._preferences["hidden"]
        assert "plex-55" in app2._preferences["liked"]

    def test_hidden_items_excluded_from_sensor(self):
        app = _make_app()
        app._categories["in_theaters"] = [
            _make_item("tmdb-1", "Visible Movie"),
            _make_item("tmdb-2", "Hidden Movie"),
        ]
        app._preferences["hidden"] = ["tmdb-2"]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        ids = [item["id"] for item in attrs["categories"]["in_theaters"]]
        assert "tmdb-1" in ids
        assert "tmdb-2" not in ids

    def test_liked_items_boosted_to_top(self):
        app = _make_app()
        app._categories["in_theaters"] = [
            _make_item("tmdb-1", "Normal", tmdb_popularity=100.0),
            _make_item("tmdb-2", "Liked", tmdb_popularity=5.0),
        ]
        app._preferences["liked"] = ["tmdb-2"]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        ids = [item["id"] for item in attrs["categories"]["in_theaters"]]
        assert ids[0] == "tmdb-2"  # liked item first despite lower popularity

    def test_dismiss_missing_id_logs_warning(self):
        app = _make_app()
        app._handle_dismiss({})
        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0

    def test_like_missing_id_logs_warning(self):
        app = _make_app()
        app._handle_like({})
        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0

    def test_undo_dismiss_missing_id_logs_warning(self):
        app = _make_app()
        app._handle_undo_dismiss({})
        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0


# ---------------------------------------------------------------------------
# Tests: Partial failure retention
# ---------------------------------------------------------------------------

class TestPartialFailure:
    def test_tautulli_failure_retains_plex_items(self):
        app = _make_app()
        original_items = [_make_item("plex-1", "Original Plex Movie")]
        app._categories["plex_new"] = list(original_items)

        # Make the fetcher raise
        app._tautulli.fetch_recently_added = AsyncMock(
            side_effect=RuntimeError("Tautulli down")
        )

        _run(app._refresh_tautulli())

        # Items retained
        assert len(app._categories["plex_new"]) == 1
        assert app._categories["plex_new"][0].id == "plex-1"

    def test_tmdb_failure_retains_categories(self):
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-1", "In Theaters")]
        app._categories["coming_soon"] = [_make_item("tmdb-2", "Coming Soon")]

        app._tmdb.fetch_in_theaters = AsyncMock(
            side_effect=RuntimeError("TMDb down")
        )

        _run(app._refresh_tmdb())

        assert len(app._categories["in_theaters"]) == 1
        assert len(app._categories["coming_soon"]) == 1

    def test_fetch_status_updated_on_tautulli_error(self):
        app = _make_app()
        app._tautulli.fetch_recently_added = AsyncMock(
            side_effect=RuntimeError("Network error")
        )

        _run(app._refresh_tautulli())

        assert app._fetch_status["tautulli"] == "error"

    def test_fetch_status_updated_on_tmdb_error(self):
        app = _make_app()
        app._tmdb.fetch_in_theaters = AsyncMock(
            side_effect=RuntimeError("TMDb gone")
        )

        _run(app._refresh_tmdb())

        assert app._fetch_status["tmdb"] == "error"

    def test_fetch_status_ok_on_success(self):
        app = _make_app()
        app._tautulli.fetch_recently_added = AsyncMock(
            return_value=FetchResult(items=[_make_item("plex-1")], status="ok")
        )
        app._tautulli.download_posters = AsyncMock(return_value=0)

        _run(app._refresh_tautulli())

        assert app._fetch_status["tautulli"] == "ok"

    def test_tautulli_fetch_result_error_status_triggers_retention(self):
        """FetchResult with status='error' also triggers retention."""
        app = _make_app()
        original = [_make_item("plex-1")]
        app._categories["plex_new"] = list(original)

        app._tautulli.fetch_recently_added = AsyncMock(
            return_value=FetchResult(status="error", error_message="API error")
        )

        _run(app._refresh_tautulli())

        assert app._categories["plex_new"] == original
        assert app._fetch_status["tautulli"] == "error"


# ---------------------------------------------------------------------------
# Tests: Stale TTL
# ---------------------------------------------------------------------------

class TestStaleTTL:
    def test_old_items_evicted(self):
        app = _make_app(extra_args={"stale_ttl_days": 7})
        items = [
            _make_item("tmdb-1", added_at=_days_ago_iso(10)),
            _make_item("tmdb-2", added_at=_days_ago_iso(3)),
        ]

        result = app._apply_stale_ttl(items)

        ids = [i.id for i in result]
        assert "tmdb-1" not in ids
        assert "tmdb-2" in ids

    def test_fresh_items_retained(self):
        app = _make_app(extra_args={"stale_ttl_days": 7})
        items = [
            _make_item("tmdb-1", added_at=_days_ago_iso(1)),
            _make_item("tmdb-2", release_date=_days_ago_iso(2)[:10]),
        ]

        result = app._apply_stale_ttl(items)

        assert len(result) == 2

    def test_no_date_items_retained(self):
        """Items with no added_at or release_date are not evicted."""
        app = _make_app(extra_args={"stale_ttl_days": 7})
        items = [_make_item("tmdb-1")]  # no dates

        result = app._apply_stale_ttl(items)

        assert len(result) == 1

    def test_stale_ttl_zero_disables_eviction(self):
        app = _make_app(extra_args={"stale_ttl_days": 0})
        items = [_make_item("tmdb-1", added_at=_days_ago_iso(999))]

        result = app._apply_stale_ttl(items)

        assert len(result) == 1

    def test_release_date_used_when_added_at_missing(self):
        app = _make_app(extra_args={"stale_ttl_days": 5})
        old_date = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        items = [_make_item("tmdb-1", release_date=old_date)]

        result = app._apply_stale_ttl(items)

        assert len(result) == 0


# ---------------------------------------------------------------------------
# Tests: Poster cleanup
# ---------------------------------------------------------------------------

class TestPosterCleanup:
    def test_removes_stale_posters(self):
        td = tempfile.mkdtemp(prefix="mda_posters_")
        app = _make_app(tmpdir=td)

        poster_dir = app._poster_dir
        # Create active and stale poster files
        Path(poster_dir, "tmdb-1.jpg").write_bytes(b"active")
        Path(poster_dir, "tmdb-stale.jpg").write_bytes(b"stale")

        app._categories["in_theaters"] = [
            _make_item("tmdb-1", local_poster="tmdb-1.jpg")
        ]
        app._categories["plex_new"] = []
        app._categories["coming_soon"] = []

        app._cleanup_stale_posters()

        assert os.path.exists(os.path.join(poster_dir, "tmdb-1.jpg"))
        assert not os.path.exists(os.path.join(poster_dir, "tmdb-stale.jpg"))

    def test_shell_command_called_after_cleanup(self):
        td = tempfile.mkdtemp(prefix="mda_shell_")
        app = _make_app(tmpdir=td)

        app._categories["in_theaters"] = []
        app._categories["plex_new"] = []
        app._categories["coming_soon"] = []

        app.call_service.reset_mock()
        app._cleanup_stale_posters()

        app.call_service.assert_called_with(
            f"shell_command/{app._poster_sync_shell_command}"
        )

    def test_cleanup_does_not_delete_active_posters(self):
        td = tempfile.mkdtemp(prefix="mda_active_")
        app = _make_app(tmpdir=td)

        poster_dir = app._poster_dir
        Path(poster_dir, "plex-10.jpg").write_bytes(b"active")
        Path(poster_dir, "tmdb-20.jpg").write_bytes(b"active")

        app._categories["plex_new"] = [_make_item("plex-10", local_poster="plex-10.jpg")]
        app._categories["in_theaters"] = [_make_item("tmdb-20", local_poster="tmdb-20.jpg")]
        app._categories["coming_soon"] = []

        app._cleanup_stale_posters()

        assert os.path.exists(os.path.join(poster_dir, "plex-10.jpg"))
        assert os.path.exists(os.path.join(poster_dir, "tmdb-20.jpg"))


# ---------------------------------------------------------------------------
# Tests: Startup
# ---------------------------------------------------------------------------

class TestStartup:
    def test_startup_provisions_relay(self):
        app = _make_app()
        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=True)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        mock_prov.ensure_script.assert_called_once()
        script_id = mock_prov.ensure_script.call_args[0][0]
        assert script_id == "media_dashboard_relay"

    def test_startup_relay_script_fires_correct_event(self):
        app = _make_app()
        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        script_config = mock_prov.ensure_script.call_args[0][1]
        assert script_config["sequence"][0]["event"] == COMMAND_EVENT

    def test_startup_schedules_refresh_timers(self):
        app = _make_app()
        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        # run_every called 3 times (tautulli, tmdb, showtimes)
        assert app.run_every.call_count == 3

    def test_startup_registers_event_listener(self):
        app = _make_app()
        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        app.listen_event.assert_called_once()
        event_name = app.listen_event.call_args[0][1]
        assert event_name == COMMAND_EVENT

    def test_startup_skips_provisioning_without_ha_url(self):
        app = _make_app(extra_args={"ha_url": None})
        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        app.log.assert_any_call(
            "ha_url / ha_token_env not configured — skipping provisioning",
            level="WARNING",
        )
        mock_prov.ensure_script.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Detail sensor
# ---------------------------------------------------------------------------

class TestDetailSensor:
    def test_publish_detail_sets_correct_entity(self):
        app = _make_app()
        item = _make_item("tmdb-1", "Detail Movie", summary="Great film")
        showtimes = {"entries": [{"cinema_name": "AMC", "times": ["7pm"]}]}

        app._publish_detail(item, showtimes)

        app.set_state.assert_called_once()
        entity_id = app.set_state.call_args[0][0]
        assert entity_id == SENSOR_DETAIL

    def test_publish_detail_includes_summary(self):
        app = _make_app()
        item = _make_item("tmdb-1", summary="Fantastic film")
        showtimes = {}

        app._publish_detail(item, showtimes)

        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs.get("summary") == "Fantastic film"

    def test_publish_detail_includes_showtimes(self):
        app = _make_app()
        item = _make_item("tmdb-1")
        showtimes = {"entries": [{"cinema_name": "AMC", "times": ["1pm", "4pm"]}]}

        app._publish_detail(item, showtimes)

        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["showtimes"] == showtimes


# ---------------------------------------------------------------------------
# Tests: Showtime cache
# ---------------------------------------------------------------------------

class TestShowtimeCache:
    def test_write_and_read_cache(self):
        td = tempfile.mkdtemp(prefix="mda_st_")
        app = _make_app(tmpdir=td)

        cache = ShowtimeCache(
            date=datetime.date.today().isoformat(),
            films={
                "thunderbolts": [
                    ShowtimeEntry(cinema_name="AMC Tyngsboro", times=["1:00", "4:30"])
                ]
            },
        )

        app._write_showtime_cache(cache)
        loaded = app._read_showtime_cache()

        assert loaded is not None
        assert loaded.date == cache.date
        assert "thunderbolts" in loaded.films

    def test_get_showtimes_for_item_matches_by_title(self):
        td = tempfile.mkdtemp(prefix="mda_show_")
        app = _make_app(tmpdir=td)

        cache = ShowtimeCache(
            date=datetime.date.today().isoformat(),
            films={
                "thunderbolts": [
                    ShowtimeEntry(cinema_name="AMC Tyngsboro", times=["7pm"])
                ]
            },
        )
        app._write_showtime_cache(cache)

        item = _make_item("tmdb-1", title="Thunderbolts")
        result = app._get_showtimes_for_item(item)

        assert len(result["entries"]) == 1
        assert result["entries"][0]["cinema_name"] == "AMC Tyngsboro"

    def test_get_showtimes_returns_empty_when_no_cache(self):
        td = tempfile.mkdtemp(prefix="mda_noshow_")
        app = _make_app(tmpdir=td)

        item = _make_item("tmdb-1", title="No Showtimes")
        result = app._get_showtimes_for_item(item)

        assert result["entries"] == []

    def test_get_showtimes_omits_stale_cache(self):
        td = tempfile.mkdtemp(prefix="mda_stale_")
        app = _make_app(tmpdir=td)

        old_date = (
            datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(hours=50)
        ).isoformat()
        cache = ShowtimeCache(
            date=old_date,
            films={"oldfilm": [ShowtimeEntry(cinema_name="AMC", times=["noon"])]},
        )
        app._write_showtime_cache(cache)

        item = _make_item("tmdb-1", title="Oldfilm")
        result = app._get_showtimes_for_item(item)

        # Too old — omit
        assert result["entries"] == []
        assert "too old" in result.get("note", "").lower()


# ---------------------------------------------------------------------------
# Tests: Hidden eligible
# ---------------------------------------------------------------------------

class TestHiddenEligible:
    def test_hidden_eligible_appears_in_sensor_attributes(self):
        """hidden_eligible key is always present in sensor attributes, even when empty."""
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-1", "Visible Movie")]
        # No hidden preferences

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        assert "hidden_eligible" in attrs
        assert isinstance(attrs["hidden_eligible"], dict)

    def test_hidden_eligible_contains_dismissed_items(self):
        """Items in preferences hidden list appear in hidden_eligible, NOT in main categories."""
        app = _make_app()
        app._categories["in_theaters"] = [
            _make_item("tmdb-1", "Visible Movie"),
            _make_item("tmdb-2", "Hidden Movie"),
        ]
        app._preferences["hidden"] = ["tmdb-2"]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        # tmdb-2 should not appear in main category
        main_ids = [item["id"] for item in attrs["categories"]["in_theaters"]]
        assert "tmdb-2" not in main_ids
        # tmdb-2 should appear in hidden_eligible
        assert "in_theaters" in attrs["hidden_eligible"]
        hidden_ids = [item["id"] for item in attrs["hidden_eligible"]["in_theaters"]]
        assert "tmdb-2" in hidden_ids

    def test_hidden_eligible_respects_stale_ttl(self):
        """Stale hidden items (older than stale_ttl_days) are NOT in hidden_eligible."""
        app = _make_app(extra_args={"stale_ttl_days": 7})
        # Item with added_at older than TTL
        stale_item = _make_item("plex-old", "Old Plex Movie", added_at=_days_ago_iso(10))
        app._categories["plex_new"] = [stale_item]
        app._preferences["hidden"] = ["plex-old"]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        # The stale item doesn't make it past _apply_stale_ttl, so it should
        # not appear in hidden_eligible
        in_plex_hidden = attrs["hidden_eligible"].get("plex_new", [])
        hidden_ids = [item["id"] for item in in_plex_hidden]
        assert "plex-old" not in hidden_ids

    def test_hidden_eligible_capped_at_max(self):
        """No more than max_items_per_category hidden items per category."""
        app = _make_app(extra_args={"max_items_per_category": 3})
        # Create 5 hidden items
        items = [_make_item(f"tmdb-{i}", f"Movie {i}") for i in range(5)]
        app._categories["in_theaters"] = items
        app._preferences["hidden"] = [f"tmdb-{i}" for i in range(5)]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        hidden_in_theaters = attrs["hidden_eligible"].get("in_theaters", [])
        assert len(hidden_in_theaters) <= 3

    def test_hidden_eligible_items_have_hidden_flag(self):
        """Each item in hidden_eligible has hidden: True."""
        app = _make_app()
        app._categories["in_theaters"] = [
            _make_item("tmdb-1", "Visible"),
            _make_item("tmdb-2", "Hidden"),
        ]
        app._preferences["hidden"] = ["tmdb-2"]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        hidden_items = attrs["hidden_eligible"].get("in_theaters", [])
        assert len(hidden_items) == 1
        assert hidden_items[0]["hidden"] is True

    def test_hidden_eligible_items_have_poster_url(self):
        """Hidden items with local_poster get a /local/.../poster.jpg URL."""
        app = _make_app()
        item = _make_item("tmdb-5", "Poster Movie", local_poster="tmdb-5.jpg")
        app._categories["in_theaters"] = [item]
        app._preferences["hidden"] = ["tmdb-5"]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        hidden_items = attrs["hidden_eligible"].get("in_theaters", [])
        assert len(hidden_items) == 1
        poster_url = hidden_items[0].get("poster", "")
        assert poster_url.startswith("/local/")
        assert "tmdb-5.jpg" in poster_url

    def test_hidden_eligible_empty_when_no_hidden_prefs(self):
        """No hidden preferences results in an empty hidden_eligible dict."""
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-1", "Some Movie")]
        # Explicitly clear hidden preferences
        app._preferences["hidden"] = []

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["hidden_eligible"] == {}

    def test_hidden_eligible_category_absent_when_no_hidden_items_in_cat(self):
        """Categories without hidden items don't appear as keys in hidden_eligible."""
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-1", "Visible")]
        app._categories["plex_new"] = [_make_item("plex-1", "Plex Hidden")]
        app._categories["coming_soon"] = [_make_item("tmdb-cs", "Coming Soon Visible")]
        # Only hide one plex item
        app._preferences["hidden"] = ["plex-1"]

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        hidden_eligible = attrs["hidden_eligible"]
        # plex_new should appear because it has a hidden item
        assert "plex_new" in hidden_eligible
        # in_theaters and coming_soon should NOT appear (no hidden items there)
        assert "in_theaters" not in hidden_eligible
        assert "coming_soon" not in hidden_eligible

    def test_undo_dismiss_moves_item_from_hidden_to_visible(self):
        """Full lifecycle: item starts hidden, undo_dismiss makes it visible and removes from hidden_eligible."""
        app = _make_app()
        item = _make_item("tmdb-10", "Restored Movie")
        app._categories["in_theaters"] = [item]
        app._preferences["hidden"] = ["tmdb-10"]
        app._preferences["hidden_at"] = {"tmdb-10": _now_iso()}

        # Confirm item is in hidden_eligible before undo
        app._publish_sensor()
        app.set_state.reset_mock()

        attrs_before = app.set_state.call_args  # None after reset — call again
        app._publish_sensor()
        attrs_before = app.set_state.call_args[1]["attributes"]
        assert "tmdb-10" in [i["id"] for i in attrs_before["hidden_eligible"].get("in_theaters", [])]
        assert "tmdb-10" not in [i["id"] for i in attrs_before["categories"].get("in_theaters", [])]

        # Undo the dismiss
        app._handle_undo_dismiss({"id": "tmdb-10"})

        # Verify final sensor state
        attrs_after = app.set_state.call_args[1]["attributes"]
        # Item should now be visible in main category
        main_ids = [i["id"] for i in attrs_after["categories"].get("in_theaters", [])]
        assert "tmdb-10" in main_ids
        # Item should no longer be in hidden_eligible
        hidden_ids = [i["id"] for i in attrs_after["hidden_eligible"].get("in_theaters", [])]
        assert "tmdb-10" not in hidden_ids
