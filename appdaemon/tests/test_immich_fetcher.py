"""Integration tests for ImmichFetcherApp.

Mocks AppDaemon methods and aiohttp HTTP calls – no real network or
filesystem access required.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
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

from immich_fetcher.immich_fetcher_app import ImmichFetcherApp, SENSOR_ENTITY_ID
from immich_fetcher.models import FetcherConfig
from providers.photo_providers.types import PhotoFilter, PhotoAlbum, PhotoMetadata, PhotoPerson


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _fake_resolve_secret(name: str) -> str:
    """Return test values for env var names used by apps (only tokens/API keys)."""
    m = {
        "IMMICH_API_KEY": "test-key",
        "TOKEN": "tok",
    }
    return m.get(name, "")


@pytest.fixture(autouse=True)
def patch_resolve_secret():
    """Patch resolve_secret so apps can initialize without real env vars."""
    with patch("providers.secrets.resolve_secret", side_effect=_fake_resolve_secret):
        yield


PEOPLE_API_RESPONSE: List[Dict[str, Any]] = [
    {"id": "uuid-alice", "name": "Alice", "assetCount": 50},
    {"id": "uuid-bob", "name": "Bob", "assetCount": 5},
    {"id": "uuid-charlie", "name": "Charlie", "assetCount": 200},
]

ALBUMS_API_RESPONSE: List[Dict[str, Any]] = [
    {"id": "alb-1", "albumName": "Family 2024", "assetCount": 120},
    {"id": "alb-2", "albumName": "Vacation", "assetCount": 30},
]


def _make_app(
    *,
    output_dir: str = "",
    config_file: str = "",
    extra_args: dict | None = None,
) -> ImmichFetcherApp:
    """Create an ImmichFetcherApp with mocked AppDaemon methods."""
    ad = MagicMock()
    config = MagicMock()
    app = ImmichFetcherApp(ad, config)

    base_args: dict[str, Any] = {
        "immich_url": "https://immich.example.com",
        "immich_api_key_env": "IMMICH_API_KEY",
        "ha_url": "http://ha:8123",
        "ha_token_env": "TOKEN",
        "output_dir": output_dir or "/tmp/test-immich-photos",
        "config_file": config_file or "/tmp/test-immich-fetcher/config.json",
        "update_interval_minutes": 60,
        "num_photos": 5,
        "download_quality": "preview",
        "default_filters": [
            {"name": "Random", "selection": "all_photos"},
        ],
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.cancel_timer = MagicMock()
    app.timer_running = MagicMock(return_value=False)
    app.datetime = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()

    return app


def _set_state_calls(app: ImmichFetcherApp) -> list:
    return [c for c in app.set_state.call_args_list]


def _fire_event_calls(app: ImmichFetcherApp, event_type: str) -> list:
    return [
        c for c in app.fire_event.call_args_list
        if c.args and c.args[0] == event_type
    ]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_loads_from_args_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "nonexistent", "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            assert len(app._config.filters) == 1
            assert app._config.filters[0].name == "Random"
            assert app._config.num_photos == 5

    def test_loads_from_file_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            saved_cfg = FetcherConfig(
                filters=[
                    PhotoFilter(name="Search", selection="search", search_query="cats"),
                ],
                num_photos=20,
            )
            Path(cfg_path).write_text(json.dumps(saved_cfg.to_dict()))

            app = _make_app(config_file=cfg_path, output_dir=os.path.join(td, "photos"))
            app.initialize()
            assert len(app._config.filters) == 1
            assert app._config.filters[0].name == "Search"
            assert app._config.num_photos == 20

    def test_falls_back_on_invalid_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            Path(cfg_path).write_text("NOT VALID JSON !!!")

            app = _make_app(config_file=cfg_path, output_dir=os.path.join(td, "photos"))
            app.initialize()
            assert app._config.filters[0].name == "Random"

    def test_falls_back_on_invalid_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            Path(cfg_path).write_text(json.dumps({"filters": []}))

            app = _make_app(config_file=cfg_path, output_dir=os.path.join(td, "photos"))
            app.initialize()
            assert app._config.filters[0].name == "Random"


class TestConfigPersistence:
    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            app = _make_app(
                config_file=cfg_path,
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app._config = FetcherConfig(
                filters=[
                    PhotoFilter(name="MyFilter", selection="all_photos"),
                ],
                num_photos=15,
                download_quality="fullsize",
            )
            app._persist_config()

            raw = json.loads(Path(cfg_path).read_text())
            assert raw["num_photos"] == 15
            assert raw["download_quality"] == "fullsize"
            assert raw["filters"][0]["name"] == "MyFilter"


# ---------------------------------------------------------------------------
# Initialize
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_registers_event_listeners(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()

            event_names = [c.args[1] for c in app.listen_event.call_args_list]
            assert "immich_fetcher_command" in event_names

    def test_creates_output_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "deep", "photos")
            cfg_dir = os.path.join(td, "deep", "config")
            cfg_file = os.path.join(cfg_dir, "config.json")
            app = _make_app(output_dir=out_dir, config_file=cfg_file)
            app.initialize()
            assert os.path.isdir(out_dir)
            assert os.path.isdir(cfg_dir)

    def test_schedules_startup(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            assert app.run_in.call_count >= 1

    def test_initialize_supports_env_backed_urls(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "immich_url": "",
                    "immich_url_env": "IMMICH_URL",
                    "ha_url": "",
                    "ha_url_env": "HA_URL",
                },
            )
            with patch(
                "providers.secrets.resolve_secret",
                side_effect=lambda name: {
                    "IMMICH_URL": "https://immich.example.com",
                    "HA_URL": "http://ha:8123",
                    "TOKEN": "tok",
                    "IMMICH_API_KEY": "test-key",
                }[name],
            ):
                app.initialize()

            assert getattr(app._provider, "_base_url", None) == "https://immich.example.com"


# ---------------------------------------------------------------------------
# Cache refresh
# ---------------------------------------------------------------------------

class TestCacheRefresh:
    @pytest.mark.asyncio
    async def test_refresh_caches(self):
        metadata = PhotoMetadata(
            people=[
                PhotoPerson("Charlie", 200),
                PhotoPerson("Alice", 50),
                PhotoPerson("Bob", 5),
            ],
            albums=[
                PhotoAlbum("Family 2024", "alb-1", 120),
                PhotoAlbum("Vacation", "alb-2", 30),
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()

            app._provider.refresh_metadata = AsyncMock(return_value=metadata)
            await app._refresh_caches()

            # people_available is a list of names (strings) in metadata order
            assert isinstance(app._people_available, list)
            assert all(isinstance(n, str) for n in app._people_available)
            assert "Alice" in app._people_available
            assert "Bob" in app._people_available
            assert "Charlie" in app._people_available

            # Albums
            assert len(app._albums_available) == 2
            assert app._albums_available[0]["name"] == "Family 2024"


# ---------------------------------------------------------------------------
# Sensor publication
# ---------------------------------------------------------------------------

class TestSensorPublish:
    def test_publishes_sensor(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app._status = "idle"
            app._last_fetch = "2025-01-01T00:00:00Z"
            app._last_fetch_count = 10
            app._last_fetch_filter = "Random"
            app._publish_sensor()

            assert app.set_state.called
            last_call = app.set_state.call_args
            assert last_call.args[0] == SENSOR_ENTITY_ID
            attrs = last_call.kwargs.get("attributes", {})
            assert attrs["status"] == "idle"
            assert attrs["last_fetch_count"] == 10
            assert attrs["last_fetch_filter"] == "Random"
            assert "filters" in attrs
            assert "people_available" in attrs
            assert "albums_available" in attrs


# ---------------------------------------------------------------------------
# Fetch logic
# ---------------------------------------------------------------------------

class TestFetchLogic:
    @pytest.mark.asyncio
    async def test_fetch_downloads_and_fires_event(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                return_value=["asset-1", "asset-2", "asset-3"]
            )
            app._provider.download_photo = AsyncMock(
                return_value=b"\xff\xd8\xff\xe0FAKE_JPEG"
            )
            await app._do_fetch()

            # Files written to output dir
            files = os.listdir(out_dir)
            assert len(files) == 3
            assert all(f.endswith(".jpg") for f in files)

            # Event fired
            batch_events = _fire_event_calls(app, "immich_fetcher_batch_ready")
            assert len(batch_events) == 1
            assert batch_events[0].kwargs["count"] == 3
            assert batch_events[0].kwargs["filter"] == "Random"

            assert app._status == "idle"
            assert app._last_fetch_count == 3

    @pytest.mark.asyncio
    async def test_fetch_rotates_filters(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "Filter A", "selection": "all_photos"},
                        {"name": "Filter B", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                return_value=["asset-1"]
            )
            app._provider.download_photo = AsyncMock(return_value=b"JPEG")
            assert app._active_filter_index == 0
            await app._do_fetch()
            assert app._last_fetch_filter == "Filter A"
            assert app._active_filter_index == 1

            await app._do_fetch()
            assert app._last_fetch_filter == "Filter B"
            assert app._active_filter_index == 0  # wraps around

    @pytest.mark.asyncio
    async def test_fetch_clears_old_files(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            # Pre-populate with old files
            Path(os.path.join(out_dir, "old_photo.jpg")).write_bytes(b"OLD")
            Path(os.path.join(out_dir, "keep_me.txt")).write_text("not an image")

            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                return_value=["new-asset"]
            )
            app._provider.download_photo = AsyncMock(return_value=b"NEW")
            await app._do_fetch()

            files = set(os.listdir(out_dir))
            assert "old_photo.jpg" not in files  # cleared
            assert "keep_me.txt" in files  # non-image preserved
            assert "photo_0000.jpg" in files  # new file

    @pytest.mark.asyncio
    async def test_fetch_no_assets_skips(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(return_value=[])
            await app._do_fetch()

            assert app._status == "idle"
            batch_events = _fire_event_calls(app, "immich_fetcher_batch_ready")
            assert len(batch_events) == 0  # no event when no assets

    @pytest.mark.asyncio
    async def test_fetch_error_sets_status(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                side_effect=Exception("Network error")
            )
            await app._do_fetch()

            assert app._status == "error"

    @pytest.mark.asyncio
    async def test_fetch_samples_from_large_pool(self):
        """When selector returns more IDs than num_photos, only num_photos are downloaded."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={"num_photos": 5},
            )
            app.initialize()

            pool_ids = [f"asset-{i}" for i in range(50)]
            app._provider.fetch_photo_ids = AsyncMock(return_value=pool_ids)
            app._provider.download_photo = AsyncMock(return_value=b"\xff\xd8JPEG")
            await app._do_fetch()

            files = os.listdir(out_dir)
            assert len(files) == 5  # only num_photos downloaded, not full pool
            assert app._provider.download_photo.call_count == 5
            assert app._last_fetch_count == 5

    @pytest.mark.asyncio
    async def test_concurrent_fetch_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
            )
            app.initialize()
            app._status = "fetching"  # simulate already fetching

            app._provider.fetch_photo_ids = AsyncMock(return_value=["a1"])
            await app._do_fetch()

            # fetch_photo_ids never called because fetch was skipped
            app._provider.fetch_photo_ids.assert_not_called()


# ---------------------------------------------------------------------------
# Album selection
# ---------------------------------------------------------------------------

class TestAlbumSelection:
    @pytest.mark.asyncio
    async def test_album_selection_resolves_name(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "Album", "selection": "album", "album_name": "Family 2024"},
                    ]
                },
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                return_value=["a1", "a2"]
            )
            app._provider.download_photo = AsyncMock(return_value=b"JPEG")
            await app._do_fetch()

            assert app._last_fetch_count == 2

    @pytest.mark.asyncio
    async def test_album_not_found_skips(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "Missing", "selection": "album", "album_name": "Nonexistent"},
                    ]
                },
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(return_value=[])
            await app._do_fetch()

            assert app._status == "idle"
            batch_events = _fire_event_calls(app, "immich_fetcher_batch_ready")
            assert len(batch_events) == 0


# ---------------------------------------------------------------------------
# Config update event
# ---------------------------------------------------------------------------

class TestConfigUpdateEvent:
    def test_valid_update_applied(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app.set_state.reset_mock()

            new_config_data = {
                "filters": [
                    {"name": "Updated", "selection": "all_photos"},
                ],
                "num_photos": 25,
                "update_interval_minutes": 30,
                "download_quality": "preview",
            }
            app._handle_update_config(new_config_data)

            assert app._config.num_photos == 25
            assert len(app._config.filters) == 1
            assert app._config.filters[0].name == "Updated"
            assert app._active_filter_index == 0

            # Config persisted to file
            raw = json.loads(Path(os.path.join(td, "config.json")).read_text())
            assert raw["num_photos"] == 25

            # Sensor re-published
            assert app.set_state.called

    def test_invalid_update_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            original_filters = app._config.filters[:]

            # Empty filters is invalid
            app._handle_update_config({"filters": [], "num_photos": 5})

            # Config unchanged
            assert app._config.filters == original_filters

    def test_interval_change_reschedules(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()

            # Simulate that _schedule_fetch ran (as it would in _async_startup)
            app._fetch_handle = MagicMock()
            app.cancel_timer.reset_mock()
            app.create_task.reset_mock()

            new_config_data = {
                "filters": [{"name": "X", "selection": "all_photos"}],
                "num_photos": 5,
                "update_interval_minutes": 15,
                "download_quality": "preview",
            }
            app._handle_update_config(new_config_data)

            assert app._config.update_interval_minutes == 15
            assert app.cancel_timer.called
            # _reschedule_fetch calls create_task(_schedule_fetch())
            assert app.create_task.called


# ---------------------------------------------------------------------------
# Refresh-now event
# ---------------------------------------------------------------------------

class TestRefreshNow:
    def test_refresh_now_triggers_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app.create_task.reset_mock()

            app._on_command(
                "immich_fetcher_command",
                {"command": "refresh_now"},
                {},
            )
            assert app.create_task.called


# ---------------------------------------------------------------------------
# Photo frame viewer: _read_source_file_list with os.listdir
# ---------------------------------------------------------------------------

class TestViewerSourceReading:
    def test_reads_image_files_from_dir(self):
        """Verify the updated viewer reads from the filesystem."""
        mock_hass_mod = MagicMock()
        mock_hass_mod.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
        sys.modules["hassapi"] = mock_hass_mod
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

        from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp

        ad = MagicMock()
        config = MagicMock()
        viewer = PhotoFrameViewerApp(ad, config)

        with tempfile.TemporaryDirectory() as td:
            # Create some test files
            Path(os.path.join(td, "photo1.jpg")).write_bytes(b"JPEG1")
            Path(os.path.join(td, "photo2.png")).write_bytes(b"PNG")
            Path(os.path.join(td, "readme.txt")).write_text("not an image")
            Path(os.path.join(td, "photo3.webp")).write_bytes(b"WEBP")

            viewer.source_dir = td
            viewer.options_max = 100
            viewer.log = MagicMock()

            files = viewer._read_source_file_list()

            assert len(files) == 3
            basenames = [os.path.basename(f) for f in files]
            assert "photo1.jpg" in basenames
            assert "photo2.png" in basenames
            assert "photo3.webp" in basenames
            assert "readme.txt" not in basenames

            # All paths are absolute
            for f in files:
                assert os.path.isabs(f)

    def test_missing_dir_returns_empty(self):
        from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp

        ad = MagicMock()
        config = MagicMock()
        viewer = PhotoFrameViewerApp(ad, config)
        viewer.source_dir = "/nonexistent/path/that/does/not/exist"
        viewer.options_max = 100
        viewer.log = MagicMock()

        files = viewer._read_source_file_list()
        assert files == []

    def test_respects_options_max(self):
        from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp

        ad = MagicMock()
        config = MagicMock()
        viewer = PhotoFrameViewerApp(ad, config)

        with tempfile.TemporaryDirectory() as td:
            for i in range(20):
                Path(os.path.join(td, f"img_{i:03d}.jpg")).write_bytes(b"J")

            viewer.source_dir = td
            viewer.options_max = 5
            viewer.log = MagicMock()

            files = viewer._read_source_file_list()
            assert len(files) == 5


# ---------------------------------------------------------------------------
# Selector: pool → batch selection
# ---------------------------------------------------------------------------

from providers.photo_providers.immich_selectors import (
    AllPhotosSelector,
    AlbumSelector,
    SearchSelector,
    create_selector,
)
from providers.photo_providers.types import LocationAlias


class TestSearchSelectorPoolSampling:
    """SearchSelector with randomize returns the full shuffled pool."""

    @pytest.mark.asyncio
    async def test_randomized_returns_full_pool(self):
        pf = PhotoFilter(
            name="Test",
            selection="search",
            search_query="sunset",
            randomize=True,
            search_pool_size=100,
        )
        pool = [{"id": f"asset-{i}"} for i in range(100)]
        client = AsyncMock()
        client.search_smart = AsyncMock(return_value=pool)

        sel = SearchSelector(client, pf)
        ids = await sel.get_asset_ids(count=10)

        assert len(ids) == 100
        assert len(set(ids)) == 100
        call_body = client.search_smart.call_args[0][0]
        assert call_body["size"] == 100

    @pytest.mark.asyncio
    async def test_randomized_shuffles_order(self):
        """Full pool should be shuffled so download order varies."""
        pf = PhotoFilter(
            name="Test",
            selection="search",
            search_query="sunset",
            randomize=True,
            search_pool_size=200,
        )
        pool = [{"id": f"asset-{i}"} for i in range(200)]
        client = AsyncMock()
        client.search_smart = AsyncMock(return_value=pool)

        sel = SearchSelector(client, pf)
        orders = []
        for _ in range(5):
            ids = await sel.get_asset_ids(count=20)
            orders.append(tuple(ids[:10]))

        unique_orders = len(set(orders))
        assert unique_orders >= 2, "Expected shuffled order across runs"

    @pytest.mark.asyncio
    async def test_non_randomized_returns_top_n(self):
        pf = PhotoFilter(
            name="Test",
            selection="search",
            search_query="sunset",
            randomize=False,
            search_pool_size=100,
        )
        pool = [{"id": f"asset-{i}"} for i in range(50)]
        client = AsyncMock()
        client.search_smart = AsyncMock(return_value=pool)

        sel = SearchSelector(client, pf)
        ids = await sel.get_asset_ids(count=10)

        assert ids == [f"asset-{i}" for i in range(10)]
        call_body = client.search_smart.call_args[0][0]
        assert call_body["size"] == 10

    @pytest.mark.asyncio
    async def test_pool_smaller_than_requested(self):
        pf = PhotoFilter(
            name="Test",
            selection="search",
            search_query="rare",
            randomize=True,
            search_pool_size=50,
        )
        pool = [{"id": f"asset-{i}"} for i in range(3)]
        client = AsyncMock()
        client.search_smart = AsyncMock(return_value=pool)

        sel = SearchSelector(client, pf)
        ids = await sel.get_asset_ids(count=10)

        assert len(ids) == 3  # returns all available


class TestAlbumSelectorRandomize:
    @pytest.mark.asyncio
    async def test_randomized_samples_from_album(self):
        pf = PhotoFilter(
            name="Album Test",
            selection="album",
            album_name="Vacation",
            randomize=True,
        )
        assets = [{"id": f"a-{i}"} for i in range(50)]
        client = AsyncMock()
        client.get_album_assets = AsyncMock(return_value=assets)

        sel = AlbumSelector(client, pf, album_id="alb-1")
        ids = await sel.get_asset_ids(count=5)

        assert len(ids) == 5
        assert len(set(ids)) == 5

    @pytest.mark.asyncio
    async def test_non_randomized_returns_first_n(self):
        pf = PhotoFilter(
            name="Album Test",
            selection="album",
            album_name="Vacation",
            randomize=False,
        )
        assets = [{"id": f"a-{i}"} for i in range(50)]
        client = AsyncMock()
        client.get_album_assets = AsyncMock(return_value=assets)

        sel = AlbumSelector(client, pf, album_id="alb-1")
        ids = await sel.get_asset_ids(count=5)

        assert ids == [f"a-{i}" for i in range(5)]


class TestAllPhotosSelectorRequestSize:
    @pytest.mark.asyncio
    async def test_requests_count_photos(self):
        pf = PhotoFilter(
            name="Random",
            selection="all_photos",
            randomize=True,
        )
        client = AsyncMock()
        client.search_random = AsyncMock(
            return_value=[{"id": f"r-{i}"} for i in range(15)]
        )

        sel = AllPhotosSelector(client, pf)
        ids = await sel.get_asset_ids(count=15)

        assert len(ids) == 15
        call_body = client.search_random.call_args[0][0]
        assert call_body["size"] == 15


class TestLocationAliasInSelector:
    @pytest.mark.asyncio
    async def test_alias_expands_to_city_state_country(self):
        pf = PhotoFilter(
            name="Disney",
            selection="all_photos",
            randomize=True,
            location="Disney World",
        )
        alias = LocationAlias(
            city="Celebration", state="Florida", country="United States of America"
        )
        client = AsyncMock()
        client.search_random = AsyncMock(return_value=[{"id": "x"}])

        sel = AllPhotosSelector(
            client, pf,
            location_aliases={"Disney World": alias},
        )
        ids = await sel.get_asset_ids(count=5)

        call_body = client.search_random.call_args[0][0]
        assert call_body["city"] == "Celebration"
        assert call_body["state"] == "Florida"
        assert call_body["country"] == "United States of America"

    @pytest.mark.asyncio
    async def test_raw_location_falls_back_to_city(self):
        pf = PhotoFilter(
            name="Raw",
            selection="all_photos",
            randomize=True,
            location="Paris",
        )
        client = AsyncMock()
        client.search_random = AsyncMock(return_value=[{"id": "x"}])

        sel = AllPhotosSelector(client, pf, location_aliases={})
        await sel.get_asset_ids(count=5)

        call_body = client.search_random.call_args[0][0]
        assert call_body["city"] == "Paris"
        assert "state" not in call_body
        assert "country" not in call_body


# ---------------------------------------------------------------------------
# Sync filter: advance vs no-advance
# ---------------------------------------------------------------------------

class TestSyncFilter:
    @pytest.mark.asyncio
    async def test_sync_does_not_advance_index(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                        {"name": "C", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                return_value=["asset-1"]
            )
            app._provider.download_photo = AsyncMock(return_value=b"JPEG")

            # Sync filter index 2 (C)
            app._active_filter_index = 2
            await app._do_fetch(advance=False)

            assert app._last_fetch_filter == "C"
            assert app._active_filter_index == 2  # stayed

    @pytest.mark.asyncio
    async def test_normal_fetch_advances_index(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                        {"name": "C", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                return_value=["asset-1"]
            )
            app._provider.download_photo = AsyncMock(return_value=b"JPEG")

            assert app._active_filter_index == 0
            await app._do_fetch()  # default advance=True
            assert app._active_filter_index == 1

    def test_sync_name_mismatch_resolves_by_name(self):
        """If card sends index 2 but name='C' and backend has C at 1, use 1."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "C", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()
            app.create_task.reset_mock()

            app._on_command(
                "immich_fetcher_command",
                {"command": "sync_filter", "payload": json.dumps({"filter_index": 2, "filter_name": "C"})},
                {},
            )

            assert app._active_filter_index == 1
            assert app.create_task.called
            warn_msgs = [
                str(c.args[0]) for c in app.log.call_args_list
                if c.args and "searching by name" in str(c.args[0])
            ]
            assert len(warn_msgs) == 1

    def test_sync_name_not_found_falls_back_to_index(self):
        """If card sends a name that doesn't exist, fall back to index."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()
            app.create_task.reset_mock()

            app._on_command(
                "immich_fetcher_command",
                {"command": "sync_filter", "payload": json.dumps({"filter_index": 1, "filter_name": "Nonexistent"})},
                {},
            )

            assert app._active_filter_index == 1
            assert app.create_task.called
            warn_msgs = [
                str(c.args[0]) for c in app.log.call_args_list
                if c.args and "not found" in str(c.args[0])
            ]
            assert len(warn_msgs) == 1

    def test_sync_matching_name_uses_index(self):
        """If name and index agree, use index directly (no warning)."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()
            app.create_task.reset_mock()

            app._on_command(
                "immich_fetcher_command",
                {"command": "sync_filter", "payload": json.dumps({"filter_index": 0, "filter_name": "A"})},
                {},
            )

            assert app._active_filter_index == 0
            assert app.create_task.called
            warn_msgs = [
                str(c.args[0]) for c in app.log.call_args_list
                if c.args and "searching by name" in str(c.args[0])
            ]
            assert len(warn_msgs) == 0

    @pytest.mark.asyncio
    async def test_config_update_preserves_index(self):
        """Config save should clamp index, not reset to 0."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                        {"name": "C", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()
            app._active_filter_index = 2

            new_config_data = {
                "filters": [
                    {"name": "A", "selection": "all_photos"},
                    {"name": "B", "selection": "all_photos"},
                    {"name": "C", "selection": "all_photos"},
                ],
                "num_photos": 5,
                "update_interval_minutes": 60,
                "download_quality": "preview",
            }
            app._handle_update_config(new_config_data)

            assert app._active_filter_index == 2  # preserved, not reset to 0

    @pytest.mark.asyncio
    async def test_config_update_clamps_index_when_filters_removed(self):
        """If active index exceeds new filter count, clamp to last."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                        {"name": "C", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()
            app._active_filter_index = 2

            new_config_data = {
                "filters": [
                    {"name": "A", "selection": "all_photos"},
                ],
                "num_photos": 5,
                "update_interval_minutes": 60,
                "download_quality": "preview",
            }
            app._handle_update_config(new_config_data)

            assert app._active_filter_index == 0  # clamped to len-1

    @pytest.mark.asyncio
    async def test_config_update_logs_filter_additions(self):
        """Config update should log when filters are added."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()

            new_config_data = {
                "filters": [
                    {"name": "A", "selection": "all_photos"},
                    {"name": "B", "selection": "all_photos"},
                ],
                "num_photos": 5,
                "update_interval_minutes": 60,
                "download_quality": "preview",
            }
            app._handle_update_config(new_config_data)

            log_messages = [
                str(call.args[0]) for call in app.log.call_args_list
                if call.args
            ]
            assert any("Filters added: ['B']" in m for m in log_messages)

    @pytest.mark.asyncio
    async def test_config_update_does_not_auto_fetch(self):
        """Config save should not trigger run_in auto-fetch."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app.run_in.reset_mock()

            new_config_data = {
                "filters": [{"name": "X", "selection": "all_photos"}],
                "num_photos": 5,
                "update_interval_minutes": 60,
                "download_quality": "preview",
            }
            app._handle_update_config(new_config_data)

            # run_in should NOT have been called for auto-fetch
            for call in app.run_in.call_args_list:
                # The _on_fetch_timer pattern: run_in(callback, 1)
                if len(call.args) >= 2 and call.args[1] == 1:
                    pytest.fail("Config update should not trigger run_in auto-fetch")


# ---------------------------------------------------------------------------
# Empty filter tracking
# ---------------------------------------------------------------------------

class TestEmptyFilterTracking:
    @pytest.mark.asyncio
    async def test_all_empty_filters_does_not_move_displaying_index(self):
        """When ALL filters return 0 assets, _displaying_filter_index stays put."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "Empty", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(return_value=[])

            app._displaying_filter_index = 0
            app._active_filter_index = 1

            await app._do_fetch()

            assert app._displaying_filter_index == 0  # unchanged
            assert "Empty" in app._empty_filter_names
            assert "A" in app._empty_filter_names  # retry tried A too

    @pytest.mark.asyncio
    async def test_empty_filter_skips_to_next_successful(self):
        """Empty filter auto-retries and the next successful filter takes over."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                        {"name": "Empty", "selection": "all_photos"},
                        {"name": "B", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()

            # First call (Empty) returns nothing, second call (B) returns a photo
            app._provider.fetch_photo_ids = AsyncMock(
                side_effect=[[], ["asset-1"]]
            )
            app._provider.download_photo = AsyncMock(return_value=b"JPEG")

            app._displaying_filter_index = 0
            app._active_filter_index = 1  # points at "Empty"

            await app._do_fetch(advance=False)

            assert app._displaying_filter_index == 2  # moved to B
            assert "Empty" in app._empty_filter_names
            assert "B" not in app._empty_filter_names
            assert app._last_fetch_filter == "B"

    @pytest.mark.asyncio
    async def test_successful_fetch_clears_empty_flag(self):
        """A filter that previously returned empty gets cleared on success."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "A", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()
            app._empty_filter_names = {"A"}

            app._provider.fetch_photo_ids = AsyncMock(
                return_value=["asset-1"]
            )
            app._provider.download_photo = AsyncMock(return_value=b"JPEG")

            await app._do_fetch()

            assert "A" not in app._empty_filter_names
            assert app._displaying_filter_index == 0

    @pytest.mark.asyncio
    async def test_error_adds_to_empty_set(self):
        """A filter that errors also gets added to empty set."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
            )
            app.initialize()

            app._provider.fetch_photo_ids = AsyncMock(
                side_effect=Exception("Network error")
            )

            filter_name = app._config.filters[0].name
            await app._do_fetch()

            assert filter_name in app._empty_filter_names

    @pytest.mark.asyncio
    async def test_empty_filters_published_in_sensor(self):
        """The empty_filters attribute appears in the sensor publish."""
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "photos")
            os.makedirs(out_dir)
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=out_dir,
                extra_args={
                    "default_filters": [
                        {"name": "Works", "selection": "all_photos"},
                        {"name": "Broken", "selection": "all_photos"},
                    ]
                },
            )
            app.initialize()
            app._empty_filter_names = {"Broken"}

            app.set_state.reset_mock()
            app._publish_sensor()

            call_args = app.set_state.call_args
            attrs = call_args.kwargs.get("attributes") or call_args[1].get("attributes", {})
            empty_raw = attrs.get("empty_filters", "[]")
            empty_parsed = json.loads(empty_raw) if isinstance(empty_raw, str) else empty_raw
            assert "Broken" in empty_parsed


# ---------------------------------------------------------------------------
# F5: _async_startup / _provision_relay integration
# ---------------------------------------------------------------------------

class TestProvisionRelay:
    @pytest.mark.asyncio
    async def test_provision_relay_calls_ensure_script(self):
        """_provision_relay passes correct relay config to HAProvisioner."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "ha_url": "http://ha:8123",
                    "ha_token_env": "TOKEN",
                },
            )
            app.initialize()

            mock_prov = MagicMock()
            mock_prov.ensure_script = AsyncMock(return_value=False)

            with patch(
                "providers.ha_provisioner.HAProvisioner",
                return_value=mock_prov,
            ) as mock_cls:
                await app._provision_relay()

            mock_cls.assert_called_once_with(ha_url="http://ha:8123", ha_token_env="TOKEN")
            mock_prov.ensure_script.assert_called_once()

            call_args = mock_prov.ensure_script.call_args
            script_id = call_args.args[0]
            config = call_args.args[1]

            assert script_id == "immich_fetcher_relay"
            assert config["alias"] == "Immich Fetcher Relay"
            assert config["mode"] == "queued"
            assert "command" in config["fields"]
            assert "payload" in config["fields"]
            assert config["sequence"][0]["event"] == "immich_fetcher_command"

    @pytest.mark.asyncio
    async def test_provision_relay_skips_without_credentials(self):
        """_provision_relay returns early when ha_url_env/ha_token_env are missing."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={"ha_url": "", "ha_token_env": ""},  # override base_args to skip provisioning
            )
            app.initialize()

            with patch(
                "providers.ha_provisioner.HAProvisioner",
            ) as mock_cls:
                await app._provision_relay()

            mock_cls.assert_not_called()
            app.log.assert_any_call(
                "ha_url / ha_token_env not configured — skipping relay provisioning",
                level="WARNING",
            )

    @pytest.mark.asyncio
    async def test_provision_relay_catches_errors(self):
        """_provision_relay logs errors but doesn't crash the app."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
                extra_args={
                    "ha_url": "http://ha:8123",
                    "ha_token_env": "TOKEN",
                },
            )
            app.initialize()

            mock_prov = MagicMock()
            mock_prov.ensure_script = AsyncMock(side_effect=RuntimeError("connection refused"))

            with patch(
                "providers.ha_provisioner.HAProvisioner",
                return_value=mock_prov,
            ):
                await app._provision_relay()

            error_logs = [
                c for c in app.log.call_args_list
                if c.args and "Failed to provision relay" in str(c.args[0])
            ]
            assert len(error_logs) == 1


# ---------------------------------------------------------------------------
# F6: _on_command end-to-end for update_config
# ---------------------------------------------------------------------------

class TestOnCommandRouting:
    def test_update_config_routed_through_on_command(self):
        """update_config via _on_command exercises JSON deserialization."""
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()

            new_cfg = {
                "filters": [
                    {"name": "Renamed", "selection": "all_photos"},
                ],
                "num_photos": 12,
                "update_interval_minutes": 15,
                "download_quality": "preview",
            }

            app._on_command(
                "immich_fetcher_command",
                {"command": "update_config", "payload": json.dumps(new_cfg)},
                {},
            )

            assert app._config.filters[0].name == "Renamed"
            assert app._config.num_photos == 12


# ---------------------------------------------------------------------------
# F7: _on_command edge cases
# ---------------------------------------------------------------------------

class TestOnCommandEdgeCases:
    def test_unknown_command_logs_warning(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app.log.reset_mock()

            app._on_command(
                "immich_fetcher_command",
                {"command": "nonexistent_action"},
                {},
            )

            warning_logs = [
                c for c in app.log.call_args_list
                if "Unknown command" in str(c.args[0])
                and c.kwargs.get("level") == "WARNING"
            ]
            assert len(warning_logs) == 1

    def test_malformed_json_payload_logs_warning(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app.log.reset_mock()

            app._on_command(
                "immich_fetcher_command",
                {"command": "update_config", "payload": "NOT VALID JSON{{{"},
                {},
            )

            warning_logs = [
                c for c in app.log.call_args_list
                if "Invalid command payload" in str(c.args[0])
                and c.kwargs.get("level") == "WARNING"
            ]
            assert len(warning_logs) == 1
            assert app._config.filters[0].name == "Random"

    def test_missing_command_key_logs_warning(self):
        with tempfile.TemporaryDirectory() as td:
            app = _make_app(
                config_file=os.path.join(td, "config.json"),
                output_dir=os.path.join(td, "photos"),
            )
            app.initialize()
            app.log.reset_mock()

            app._on_command(
                "immich_fetcher_command",
                {"payload": "{}"},
                {},
            )

            warning_logs = [
                c for c in app.log.call_args_list
                if "Unknown command" in str(c.args[0])
                and c.kwargs.get("level") == "WARNING"
            ]
            assert len(warning_logs) == 1
