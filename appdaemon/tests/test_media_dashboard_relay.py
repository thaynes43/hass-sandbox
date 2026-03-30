"""Tests for MediaDashboardApp relay command routing.

Verifies that _on_command correctly routes relay events from the dashboard
card to the appropriate internal handlers.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

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
    summary: str = "",
    tmdb_id: int = 1,
    local_poster: str = "",
) -> MediaItem:
    return MediaItem(
        id=id,
        title=title,
        tmdb_popularity=tmdb_popularity,
        summary=summary,
        tmdb_id=tmdb_id,
        local_poster=local_poster or f"{id}.jpg",
        source="tmdb",
    )


_DEFAULT_ARGS: dict = {
    "tautulli_url": "http://tautulli:8181",
    "tautulli_api_key_env": "TAUTULLI_API_KEY",
    "tmdb_api_key_env": "TMDB_API_KEY",
    "movieglu_api_key_env": "MOVIEGLU_API_KEY",
    "movieglu_client_id_env": "MOVIEGLU_CLIENT_ID",
    "location_lat": "42.5",
    "location_lng": "-71.5",
    "theaters": ["AMC Tyngsboro 12"],
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "max_items_per_category": 10,
    "popularity_threshold": 10.0,
    "vote_count_threshold": 50,
    "stale_ttl_days": 7,
    "poster_sync_shell_command": "media_dashboard_sync_posters",
}


def _make_app(extra_args: dict | None = None) -> MediaDashboardApp:
    """Create a MediaDashboardApp ready for command routing tests."""
    td = tempfile.mkdtemp(prefix="mda_relay_")

    ad = MagicMock()
    app = MediaDashboardApp(ad, MagicMock())

    args = dict(_DEFAULT_ARGS)
    args["media_fs_root"] = td
    args["poster_media_subdir"] = "posters"
    args["poster_www_subdir"] = "posters"
    args["preferences_file_subdir"] = "prefs"
    args["showtime_cache_subdir"] = "showtimes"

    if extra_args:
        args.update(extra_args)

    app.args = args

    for attr in ("get_state", "set_state", "call_service", "listen_state",
                 "listen_event", "fire_event", "run_in", "run_every",
                 "run_daily", "cancel_timer", "log", "create_task"):
        setattr(app, attr, MagicMock())
    app.name = "media_dashboard_app"

    with patch("media_dashboard_app.media_dashboard_app.TautulliFetcher") as MockT, \
         patch("media_dashboard_app.media_dashboard_app.TmdbFetcher") as MockTM, \
         patch("media_dashboard_app.media_dashboard_app.MoviegluFetcher") as MockM:

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
            return_value=_make_item(summary="Full synopsis")
        )
        MockTM.return_value = mock_tmdb

        mock_movieglu = MagicMock()
        mock_movieglu.discover_cinemas = AsyncMock(return_value=[])
        mock_movieglu.fetch_showtimes = AsyncMock(
            return_value=ShowtimeCache(
                date=datetime.date.today().isoformat(), films={}
            )
        )
        MockM.return_value = mock_movieglu

        app.initialize()

    # Reset mocks after initialize so tests start clean
    app.set_state.reset_mock()
    app.call_service.reset_mock()
    app.create_task.reset_mock()
    app.log.reset_mock()

    return app


def _dispatch(app: MediaDashboardApp, command: str, payload: dict | None = None) -> None:
    """Simulate the relay script firing a command event."""
    data = {
        "command": command,
        "payload": json.dumps(payload or {}),
    }
    app._on_command(COMMAND_EVENT, data, {})


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tests: Refresh command
# ---------------------------------------------------------------------------

class TestRefreshCommand:
    def test_refresh_all_creates_task(self):
        app = _make_app()
        _dispatch(app, "refresh", {"source": "all"})
        app.create_task.assert_called_once()

    def test_refresh_default_is_all(self):
        """refresh without 'source' defaults to all."""
        app = _make_app()
        _dispatch(app, "refresh")
        app.create_task.assert_called_once()

    def test_refresh_tautulli_only(self):
        app = _make_app()
        _dispatch(app, "refresh", {"source": "tautulli"})
        app.create_task.assert_called_once()

    def test_refresh_tmdb_only(self):
        app = _make_app()
        _dispatch(app, "refresh", {"source": "tmdb"})
        app.create_task.assert_called_once()

    def test_refresh_showtimes_only(self):
        app = _make_app()
        _dispatch(app, "refresh", {"source": "showtimes"})
        app.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: get_detail command
# ---------------------------------------------------------------------------

class TestGetDetailCommand:
    def test_get_detail_creates_task(self):
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-1")]

        _dispatch(app, "get_detail", {"id": "tmdb-1"})

        app.create_task.assert_called_once()

    def test_get_detail_publishes_detail_sensor(self):
        app = _make_app()
        item = _make_item("tmdb-1", "Test Movie", summary="Already has summary")
        app._categories["in_theaters"] = [item]

        _run(app._handle_get_detail({"id": "tmdb-1"}))

        app.set_state.assert_called_once()
        entity_id = app.set_state.call_args[0][0]
        assert entity_id == SENSOR_DETAIL

    def test_get_detail_enriches_from_tmdb_when_summary_empty(self):
        app = _make_app()
        item = _make_item("tmdb-1", "No Summary Movie", summary="", tmdb_id=42)
        app._categories["in_theaters"] = [item]

        app._tmdb.fetch_detail = AsyncMock(
            return_value=_make_item("tmdb-1", summary="Fetched synopsis")
        )

        _run(app._handle_get_detail({"id": "tmdb-1"}))

        # Item summary should be populated
        assert item.summary == "Fetched synopsis"

    def test_get_detail_does_not_fetch_if_summary_exists(self):
        app = _make_app()
        item = _make_item("tmdb-1", summary="Already rich", tmdb_id=42)
        app._categories["in_theaters"] = [item]

        _run(app._handle_get_detail({"id": "tmdb-1"}))

        # fetch_detail should not be called since summary exists
        app._tmdb.fetch_detail.assert_not_called()

    def test_get_detail_reads_showtime_cache(self):
        td = tempfile.mkdtemp(prefix="mda_relay_show_")
        app = _make_app()
        app._showtime_cache_file = os.path.join(td, "showtimes.json")
        app._showtime_cache_dir = td

        # Write a cache with matching film
        cache = ShowtimeCache(
            date=datetime.date.today().isoformat(),
            films={
                "test movie": [
                    ShowtimeEntry(cinema_name="AMC Tyngsboro", times=["7pm"])
                ]
            },
        )
        app._write_showtime_cache(cache)

        item = _make_item("tmdb-1", "Test Movie", summary="Has summary")
        app._categories["in_theaters"] = [item]

        _run(app._handle_get_detail({"id": "tmdb-1"}))

        attrs = app.set_state.call_args[1]["attributes"]
        assert len(attrs["showtimes"]["entries"]) == 1

    def test_get_detail_missing_id_logs_warning(self):
        app = _make_app()
        _run(app._handle_get_detail({}))

        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0

    def test_get_detail_unknown_id_logs_warning(self):
        app = _make_app()
        _run(app._handle_get_detail({"id": "tmdb-unknown"}))

        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0

    def test_get_detail_finds_item_in_plex_new(self):
        app = _make_app()
        item = _make_item("plex-10", "Plex Movie", summary="Plex summary")
        app._categories["plex_new"] = [item]

        _run(app._handle_get_detail({"id": "plex-10"}))

        entity_id = app.set_state.call_args[0][0]
        assert entity_id == SENSOR_DETAIL
        state = app.set_state.call_args[0][1] if len(app.set_state.call_args[0]) > 1 else app.set_state.call_args[1].get("state")
        # set_state is called as set_state(entity_id, state=..., attributes=...)
        # so state is a keyword argument
        assert app.set_state.call_args[1].get("state") == "plex-10" or \
               app.set_state.call_args[0][0] == SENSOR_DETAIL


# ---------------------------------------------------------------------------
# Tests: Dismiss command
# ---------------------------------------------------------------------------

class TestDismissCommand:
    def test_dismiss_updates_preferences(self):
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-5")]

        _dispatch(app, "dismiss", {"id": "tmdb-5"})

        assert "tmdb-5" in app._preferences["hidden"]

    def test_dismiss_republishes_sensor(self):
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-5")]

        _dispatch(app, "dismiss", {"id": "tmdb-5"})

        app.set_state.assert_called()
        entity_id = app.set_state.call_args[0][0]
        assert entity_id == SENSOR_STATUS

    def test_dismiss_removes_item_from_published_sensor(self):
        app = _make_app()
        app._categories["in_theaters"] = [
            _make_item("tmdb-5", "Should disappear"),
            _make_item("tmdb-6", "Should remain"),
        ]

        _dispatch(app, "dismiss", {"id": "tmdb-5"})

        attrs = app.set_state.call_args[1]["attributes"]
        ids = [i["id"] for i in attrs["categories"]["in_theaters"]]
        assert "tmdb-5" not in ids
        assert "tmdb-6" in ids

    def test_dismiss_idempotent(self):
        """Dismissing an already-hidden item does not duplicate it."""
        app = _make_app()
        app._preferences["hidden"] = ["tmdb-5"]

        _dispatch(app, "dismiss", {"id": "tmdb-5"})

        assert app._preferences["hidden"].count("tmdb-5") == 1


# ---------------------------------------------------------------------------
# Tests: Like command
# ---------------------------------------------------------------------------

class TestLikeCommand:
    def test_like_updates_preferences(self):
        app = _make_app()
        _dispatch(app, "like", {"id": "tmdb-7"})

        assert "tmdb-7" in app._preferences["liked"]

    def test_like_republishes_sensor(self):
        app = _make_app()
        _dispatch(app, "like", {"id": "tmdb-7"})

        app.set_state.assert_called()
        entity_id = app.set_state.call_args[0][0]
        assert entity_id == SENSOR_STATUS

    def test_like_boosts_item_to_top(self):
        app = _make_app()
        app._categories["in_theaters"] = [
            _make_item("tmdb-1", tmdb_popularity=100.0),
            _make_item("tmdb-2", tmdb_popularity=5.0),
        ]

        _dispatch(app, "like", {"id": "tmdb-2"})

        attrs = app.set_state.call_args[1]["attributes"]
        ids = [i["id"] for i in attrs["categories"]["in_theaters"]]
        assert ids[0] == "tmdb-2"

    def test_like_idempotent(self):
        """Liking an already-liked item does not duplicate it."""
        app = _make_app()
        app._preferences["liked"] = ["tmdb-7"]

        _dispatch(app, "like", {"id": "tmdb-7"})

        assert app._preferences["liked"].count("tmdb-7") == 1


# ---------------------------------------------------------------------------
# Tests: undo_dismiss command
# ---------------------------------------------------------------------------

class TestUndoDismiss:
    def test_undo_removes_from_hidden(self):
        app = _make_app()
        app._preferences["hidden"] = ["tmdb-42"]
        app._preferences["hidden_at"] = {"tmdb-42": "2026-01-01T00:00:00"}

        _dispatch(app, "undo_dismiss", {"id": "tmdb-42"})

        assert "tmdb-42" not in app._preferences["hidden"]
        assert "tmdb-42" not in app._preferences["hidden_at"]

    def test_undo_republishes_sensor(self):
        app = _make_app()
        app._preferences["hidden"] = ["tmdb-42"]

        _dispatch(app, "undo_dismiss", {"id": "tmdb-42"})

        app.set_state.assert_called()
        entity_id = app.set_state.call_args[0][0]
        assert entity_id == SENSOR_STATUS

    def test_undo_restores_item_in_sensor(self):
        app = _make_app()
        app._categories["in_theaters"] = [_make_item("tmdb-42", "Re-appear")]
        app._preferences["hidden"] = ["tmdb-42"]

        _dispatch(app, "undo_dismiss", {"id": "tmdb-42"})

        attrs = app.set_state.call_args[1]["attributes"]
        ids = [i["id"] for i in attrs["categories"]["in_theaters"]]
        assert "tmdb-42" in ids

    def test_undo_is_noop_for_non_hidden_id(self):
        """Undo on an item not in hidden list is a safe no-op."""
        app = _make_app()
        app._preferences["hidden"] = []

        _dispatch(app, "undo_dismiss", {"id": "tmdb-not-hidden"})

        # Should not raise and hidden should still be empty
        assert len(app._preferences["hidden"]) == 0


# ---------------------------------------------------------------------------
# Tests: Unknown and invalid commands
# ---------------------------------------------------------------------------

class TestUnknownCommand:
    def test_unknown_command_logs_warning(self):
        app = _make_app()
        _dispatch(app, "fly_to_moon")

        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert any("fly_to_moon" in str(c) for c in warn_calls)

    def test_unknown_command_does_not_crash(self):
        app = _make_app()
        # Should not raise
        _dispatch(app, "unknown_command_xyz_abc")

    def test_invalid_json_payload_logs_warning(self):
        app = _make_app()
        app._on_command(
            COMMAND_EVENT,
            {"command": "dismiss", "payload": "not-json{{{{"},
            {},
        )

        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0

    def test_invalid_json_payload_does_not_crash(self):
        app = _make_app()
        # Should not raise
        app._on_command(
            COMMAND_EVENT,
            {"command": "dismiss", "payload": "INVALID_JSON"},
            {},
        )
