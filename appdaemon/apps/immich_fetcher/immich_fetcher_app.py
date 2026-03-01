"""Immich photo fetcher – AppDaemon app.

Schedules periodic photo fetches from Immich, writes preview JPEGs
to ``output_dir`` (``/media/immich-photos/``), and publishes status
via ``sensor.immich_fetcher_status``.

Config comes from ``apps.yaml`` (defaults) and can be overridden at
runtime by the dashboard card via HA events.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import hassapi as hass

from immich_fetcher.immich_client import ImmichClient
from immich_fetcher.models import FetcherConfig, LocationAlias, PhotoFilter
from immich_fetcher.selectors import create_selector

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})

SENSOR_ENTITY_ID = "sensor.immich_fetcher_status"


class ImmichFetcherApp(hass.Hass):
    """AppDaemon app that fetches photos from Immich on a schedule."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        self._immich_url: str = args["immich_url"]
        self._immich_api_key: str = args["immich_api_key"]
        self._output_dir: str = args.get("output_dir", "/media/immich-photos")
        self._config_file: str = args.get(
            "config_file", "/media/immich-fetcher/config.json"
        )
        # Caches populated on startup / periodically
        self._people_map: Dict[str, str] = {}
        self._people_available: List[str] = []  # names ranked by photo count
        self._albums_available: List[Dict[str, Any]] = []

        # Fetch rotation
        self._active_filter_index: int = 0
        self._displaying_filter_index: int = 0
        self._empty_filter_names: set[str] = set()
        self._last_fetch: Optional[str] = None
        self._last_fetch_count: int = 0
        self._last_fetch_filter: Optional[str] = None
        self._status: str = "idle"
        self._fetch_handle: Optional[Any] = None

        # Load config (persisted file takes precedence over apps.yaml defaults)
        self._config = self._load_config(args)
        self.log(
            f"ImmichFetcherApp initialised: "
            f"output_dir={self._output_dir} "
            f"filters={len(self._config.filters)} "
            f"interval={self._config.update_interval_minutes}m",
            level="INFO",
        )

        # Ensure output directories exist
        os.makedirs(self._output_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self._config_file), exist_ok=True)

        # Startup: populate caches, publish sensor, schedule first fetch
        self.run_in(self._on_startup, 0)

        # Listen for dashboard events
        self.listen_event(self._on_update_config, "immich_fetcher_update_config")
        self.listen_event(self._on_refresh_now, "immich_fetcher_refresh_now")
        self.listen_event(self._on_sync_filter, "immich_fetcher_sync_filter")

    # ------------------------------------------------------------------
    # Config loading / persistence
    # ------------------------------------------------------------------

    def _load_config(self, args: dict) -> FetcherConfig:
        """Load config from persisted JSON, falling back to apps.yaml defaults."""
        cfg_path = Path(self._config_file)
        if cfg_path.is_file():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg = FetcherConfig.from_dict(raw)
                cfg.validate()
                self.log(
                    f"Loaded config from {self._config_file} "
                    f"({len(cfg.filters)} filters)",
                    level="INFO",
                )
                return cfg
            except Exception as exc:
                self.log(
                    f"Failed to load config from {self._config_file}: {exc}; "
                    "falling back to apps.yaml defaults",
                    level="WARNING",
                )

        return self._config_from_args(args)

    def _config_from_args(self, args: dict) -> FetcherConfig:
        """Build a FetcherConfig from apps.yaml ``default_filters`` + global keys."""
        raw_filters = args.get("default_filters", [])
        filters = [PhotoFilter.from_dict(f) for f in raw_filters]
        if not filters:
            filters = [PhotoFilter(name="Random", selection="all_photos")]
        return FetcherConfig(
            filters=filters,
            num_photos=int(args.get("num_photos", 10)),
            update_interval_minutes=int(args.get("update_interval_minutes", 60)),
            download_quality=args.get("download_quality", "preview"),
        )

    def _persist_config(self) -> None:
        """Write current config to the JSON file."""
        try:
            Path(self._config_file).write_text(
                json.dumps(self._config.to_dict(), indent=2),
                encoding="utf-8",
            )
            self.log(f"Config persisted to {self._config_file}", level="DEBUG")
        except Exception as exc:
            self.log(f"Failed to persist config: {exc}", level="ERROR")

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, kwargs: Any) -> None:
        """Async startup work wrapped in run_in callback."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        try:
            await self._refresh_caches()
        except Exception as exc:
            self.log(f"Cache refresh on startup failed: {exc}", level="WARNING")

        self._publish_sensor()
        await self._schedule_fetch()

        # Trigger an initial fetch immediately
        self.run_in(self._on_fetch_timer, 2)

    # ------------------------------------------------------------------
    # Immich cache refresh (people + albums)
    # ------------------------------------------------------------------

    async def _refresh_caches(self) -> None:
        async with self._create_client() as client:
            # People – API returns them ranked by photo count.
            # assetCount may or may not be present; we store only names
            # in rank order and use _people_map for name→id resolution.
            raw_people = await client.get_people()
            has_asset_count = bool(
                raw_people and "assetCount" in raw_people[0]
            )
            named = [p for p in raw_people if p.get("name")]
            if has_asset_count:
                named.sort(key=lambda p: p.get("assetCount", 0), reverse=True)
            self._people_map = {p["name"]: p["id"] for p in named}
            self._people_available = [p["name"] for p in named]

            # Albums
            raw_albums = await client.get_albums()
            self._albums_available = sorted(
                [
                    {
                        "name": a.get("albumName", ""),
                        "id": a["id"],
                        "asset_count": a.get("assetCount", 0),
                    }
                    for a in raw_albums
                    if a.get("albumName")
                ],
                key=lambda x: x["asset_count"],
                reverse=True,
            )

        self.log(
            f"Caches refreshed: {len(self._people_available)} people, "
            f"{len(self._albums_available)} albums | "
            f"top 10: {self._people_available[:10]}",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Sensor publication
    # ------------------------------------------------------------------

    def _publish_sensor(self) -> None:
        """Publish (or update) sensor.immich_fetcher_status."""
        next_fetch = self._compute_next_fetch_iso()
        attrs = {
            "filters": json.dumps(
                [f.to_dict() for f in self._config.filters]
            ),
            "location_aliases": json.dumps(
                {k: v.to_dict() for k, v in self._config.location_aliases.items()}
            ),
            "active_filter_index": self._displaying_filter_index,
            "empty_filters": json.dumps(sorted(self._empty_filter_names)),
            "last_fetch": self._last_fetch or "",
            "last_fetch_count": self._last_fetch_count,
            "last_fetch_filter": self._last_fetch_filter or "",
            "next_fetch": next_fetch,
            "status": self._status,
            "download_quality": self._config.download_quality,
            "num_photos": self._config.num_photos,
            "update_interval_minutes": self._config.update_interval_minutes,
            "people_available": json.dumps(self._people_available),
            "albums_available": json.dumps(self._albums_available),
            "favorite_people": json.dumps(self._config.favorite_people),
            "people_cutoff": self._config.people_cutoff,
            "show_favorite_people_only": self._config.show_favorite_people_only,
            "friendly_name": "Immich Fetcher Status",
            "icon": "mdi:image-multiple",
        }
        payload_size = len(json.dumps(attrs))
        self.log(
            f"Publishing sensor: people_available={len(self._people_available)}, "
            f"payload_size={payload_size} bytes",
            level="DEBUG",
        )
        self.set_state(SENSOR_ENTITY_ID, state=self._status, attributes=attrs)

    def _compute_next_fetch_iso(self) -> str:
        try:
            interval_s = self._config.update_interval_minutes * 60
            now = datetime.now(timezone.utc)
            from datetime import timedelta
            nxt = now + timedelta(seconds=interval_s)
            return nxt.isoformat()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Fetch scheduling
    # ------------------------------------------------------------------

    async def _schedule_fetch(self) -> None:
        """Schedule the periodic fetch timer."""
        interval_s = self._config.update_interval_minutes * 60
        self._fetch_handle = await self.run_every(
            self._on_fetch_timer,
            datetime.now().astimezone(),
            interval_s,
        )

    def _reschedule_fetch(self) -> None:
        """Cancel and re-schedule the periodic fetch (after config change)."""
        if self._fetch_handle is not None:
            try:
                self.cancel_timer(self._fetch_handle)
            except Exception:
                pass
            self._fetch_handle = None
        self.create_task(self._schedule_fetch())

    def _on_fetch_timer(self, kwargs: Any) -> None:
        self.create_task(self._do_fetch())

    # ------------------------------------------------------------------
    # Core fetch logic
    # ------------------------------------------------------------------

    async def _do_fetch(
        self, *, advance: bool = True, _skip_count: int = 0,
    ) -> None:
        if self._status == "fetching":
            self.log("Fetch already in progress, skipping", level="DEBUG")
            return

        if not self._config.filters:
            self.log("No filters configured, skipping fetch", level="WARNING")
            return

        idx = self._active_filter_index % len(self._config.filters)
        pf = self._config.filters[idx]

        self.log(
            f"Starting fetch: filter='{pf.name}' "
            f"(index={idx}/{len(self._config.filters)})",
            level="INFO",
        )
        self._status = "fetching"
        self._publish_sensor()

        try:
            asset_ids = await self._select_assets(pf)
            if not asset_ids:
                self.log(
                    f"No assets returned for filter '{pf.name}'",
                    level="WARNING",
                )
                self._empty_filter_names.add(pf.name)
                self._status = "idle"
                self._advance_filter_index()
                self._publish_sensor()
                # Auto-retry next filter (bounded to avoid infinite loop)
                if _skip_count < len(self._config.filters) - 1:
                    self.log(
                        "Skipping to next filter",
                        level="INFO",
                    )
                    await self._do_fetch(
                        advance=advance, _skip_count=_skip_count + 1,
                    )
                return

            # Selectors may return a large pool for variety (e.g. search_pool_size).
            # Sample down to num_photos for the actual download batch.
            batch = self._config.num_photos
            if len(asset_ids) > batch:
                self.log(
                    f"Sampling {batch} photos from pool of {len(asset_ids)}",
                    level="INFO",
                )
                asset_ids = asset_ids[:batch]

            count = await self._download_assets(asset_ids)

            self._displaying_filter_index = idx
            self._empty_filter_names.discard(pf.name)
            self._last_fetch = datetime.now(timezone.utc).isoformat()
            self._last_fetch_count = count
            self._last_fetch_filter = pf.name
            self._status = "idle"
            if advance:
                self._advance_filter_index()

            self.log(
                f"Fetch complete: {count} photos written for filter '{pf.name}'",
                level="INFO",
            )
            self._publish_sensor()

            self.fire_event("immich_fetcher_batch_ready", count=count, filter=pf.name)

        except Exception as exc:
            self._status = "error"
            self._empty_filter_names.add(pf.name)
            self.log(f"Fetch failed for filter '{pf.name}': {exc}", level="ERROR")
            self._advance_filter_index()
            self._publish_sensor()

    def _advance_filter_index(self) -> None:
        if self._config.filters:
            self._active_filter_index = (
                (self._active_filter_index + 1) % len(self._config.filters)
            )

    async def _select_assets(self, pf: PhotoFilter) -> List[str]:
        """Use the appropriate selector to get asset IDs."""
        album_id: Optional[str] = None
        if pf.selection == "album" and pf.album_name:
            async with self._create_client() as client:
                album_id = await client.resolve_album_name(pf.album_name)
            if album_id is None:
                self.log(
                    f"Album '{pf.album_name}' not found, skipping",
                    level="WARNING",
                )
                return []

        async with self._create_client() as client:
            selector = create_selector(
                client,
                pf,
                people_map=self._people_map,
                location_aliases=self._config.location_aliases,
                album_id=album_id,
            )
            return await selector.get_asset_ids(self._config.num_photos)

    async def _download_assets(self, asset_ids: List[str]) -> int:
        """Download preview JPEGs to output_dir, replacing existing files."""
        # Clear existing photos
        self._clear_output_dir()

        downloaded = 0
        async with self._create_client() as client:
            for i, aid in enumerate(asset_ids):
                try:
                    data = await client.download_preview(aid)
                    filename = f"photo_{i:04d}.jpg"
                    filepath = os.path.join(self._output_dir, filename)
                    with open(filepath, "wb") as f:
                        f.write(data)
                    downloaded += 1
                except Exception as exc:
                    self.log(
                        f"Failed to download asset {aid}: {exc}",
                        level="WARNING",
                    )

        return downloaded

    def _clear_output_dir(self) -> None:
        """Remove existing image files from output_dir."""
        try:
            for fname in os.listdir(self._output_dir):
                ext = os.path.splitext(fname)[1].lower()
                if ext in _IMAGE_EXTENSIONS:
                    os.remove(os.path.join(self._output_dir, fname))
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Event handlers (dashboard card interaction)
    # ------------------------------------------------------------------

    def _on_update_config(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Handle immich_fetcher_update_config from the dashboard card."""
        self.log(f"Received config update event: {list(data.keys())}", level="INFO")
        try:
            new_cfg = FetcherConfig.from_dict(data)
            new_cfg.validate()
        except Exception as exc:
            self.log(f"Invalid config update rejected: {exc}", level="ERROR")
            return

        # Log structural filter changes
        old_names = [f.name for f in self._config.filters]
        new_names = [f.name for f in new_cfg.filters]
        if old_names != new_names:
            added = [n for n in new_names if n not in old_names]
            removed = [n for n in old_names if n not in new_names]
            if added:
                self.log(f"Filters added: {added}", level="INFO")
            if removed:
                self.log(f"Filters removed: {removed}", level="INFO")
            if not added and not removed:
                self.log(
                    f"Filters reordered: {old_names} -> {new_names}", level="INFO"
                )

        # Log changes to aliases and favorites
        old_aliases = set(self._config.location_aliases.keys())
        new_aliases = set(new_cfg.location_aliases.keys())
        added_aliases = new_aliases - old_aliases
        removed_aliases = old_aliases - new_aliases
        if added_aliases:
            self.log(f"Location aliases added: {added_aliases}", level="INFO")
        if removed_aliases:
            self.log(f"Location aliases removed: {removed_aliases}", level="INFO")

        old_favs = set(self._config.favorite_people)
        new_favs = set(new_cfg.favorite_people)
        added_favs = new_favs - old_favs
        removed_favs = old_favs - new_favs
        if added_favs:
            self.log(f"Favorite people added: {added_favs}", level="INFO")
        if removed_favs:
            self.log(f"Favorite people removed: {removed_favs}", level="INFO")

        # Validate people names against the Immich cache
        if self._people_map:
            for filt in new_cfg.filters:
                if filt.people:
                    unknown = [n for n in filt.people if n not in self._people_map]
                    if unknown:
                        self.log(
                            f"Filter '{filt.name}' has unknown people: {unknown}",
                            level="WARNING",
                        )
            unknown_favs = [
                n for n in new_cfg.favorite_people if n not in self._people_map
            ]
            if unknown_favs:
                self.log(
                    f"Unknown favorite people: {unknown_favs}",
                    level="WARNING",
                )

        old_interval = self._config.update_interval_minutes
        self._config = new_cfg
        # Clamp indices to valid range after filter list changes
        if self._config.filters:
            max_idx = len(self._config.filters) - 1
            self._active_filter_index = min(
                self._active_filter_index, max_idx
            )
            self._displaying_filter_index = min(
                self._displaying_filter_index, max_idx
            )
        else:
            self._active_filter_index = 0
            self._displaying_filter_index = 0
        self._persist_config()

        if new_cfg.update_interval_minutes != old_interval:
            self._reschedule_fetch()

        self._publish_sensor()
        self.log("Config updated successfully", level="INFO")

    def _on_refresh_now(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Handle immich_fetcher_refresh_now: trigger an immediate fetch."""
        self.log("Manual refresh requested", level="INFO")
        self.create_task(self._do_fetch())

    def _on_sync_filter(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Jump to a specific filter index and fetch immediately."""
        if not self._config.filters:
            return
        idx = data.get("filter_index")
        requested_name = data.get("filter_name", "")
        if idx is None:
            return

        idx = int(idx) % len(self._config.filters)
        resolved_name = self._config.filters[idx].name

        if requested_name and resolved_name != requested_name:
            self.log(
                f"Sync index {idx} maps to '{resolved_name}' but card "
                f"requested '{requested_name}'; searching by name",
                level="WARNING",
            )
            found = False
            for i, f in enumerate(self._config.filters):
                if f.name == requested_name:
                    idx = i
                    resolved_name = f.name
                    found = True
                    break
            if not found:
                self.log(
                    f"Filter '{requested_name}' not found in backend, "
                    f"falling back to index {idx} ('{resolved_name}')",
                    level="WARNING",
                )

        self._active_filter_index = idx
        self.log(
            f"Sync requested for filter '{resolved_name}' (index={idx})",
            level="INFO",
        )
        self._publish_sensor()
        self.create_task(self._do_fetch(advance=False))

    # ------------------------------------------------------------------
    # Client factory
    # ------------------------------------------------------------------

    def _create_client(self) -> ImmichClient:
        return ImmichClient(
            base_url=self._immich_url,
            api_key=self._immich_api_key,
        )
