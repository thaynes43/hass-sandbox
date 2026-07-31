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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# AppDaemon only adds `appdaemon/apps` to sys.path. Our shared libraries
# live at `appdaemon/providers`, so add the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from immich_fetcher.models import FetcherConfig
from providers.photo_providers.immich_data_provider import ImmichDataProvider
from providers.photo_providers.types import PhotoFilter, PhotoProvider
from providers.secrets import resolve_arg_secret

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

        self._output_dir: str = args.get("output_dir", "/media/immich-photos")
        self._config_file: str = args.get(
            "config_file", "/media/immich-fetcher/config.json"
        )
        self._provider: PhotoProvider = ImmichDataProvider(
            base_url=str(resolve_arg_secret(args, "immich_url", required=True)),
            api_key_env=args["immich_api_key_env"],
        )
        # Metadata caches are refreshed on startup and periodically while running.
        self._people_available: List[str] = []  # names ranked by photo count
        self._albums_available: List[Dict[str, Any]] = []
        self._metadata_last_refresh: Optional[datetime] = None
        self._metadata_refresh_minutes: int = int(
            args.get("metadata_refresh_minutes", 30)
        )

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

        # Recover the filter index from the sensor so the fetcher card's
        # active indicator stays correct across restarts.
        self._recover_filter_index_from_sensor()
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

        # Startup: provision relay script, populate caches, publish sensor, schedule first fetch
        self.run_in(self._on_startup, 0)

        # Single event listener for all card commands (routed via relay script)
        self.listen_event(self._on_command, "immich_fetcher_command")

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover_filter_index_from_sensor(self) -> None:
        """Recover the active/displaying filter index from the existing sensor.

        Without this, both indices reset to 0 on restart, causing the
        fetcher card's active indicator to jump to the first filter and
        the next fetch to restart from the beginning of the list.
        """
        try:
            state = self.get_state(SENSOR_ENTITY_ID, attribute="all")
            if not isinstance(state, dict):
                return
            attrs = state.get("attributes", {})
        except Exception:
            return

        # Recover last_fetch_filter so the viewer card fallback stays correct
        recovered_filter = str(attrs.get("last_fetch_filter", "") or "").strip()
        if recovered_filter:
            self._last_fetch_filter = recovered_filter

        # Recover the displaying index (used by the fetcher card's active indicator)
        try:
            recovered_idx = int(attrs.get("active_filter_index", 0))
        except (TypeError, ValueError):
            recovered_idx = 0

        if self._config.filters and 0 <= recovered_idx < len(self._config.filters):
            self._displaying_filter_index = recovered_idx
            # Resume fetching from the NEXT filter after the one last displayed
            self._active_filter_index = (recovered_idx + 1) % len(self._config.filters)
            self.log(
                f"ImmichFetcherApp: recovered filter index={recovered_idx} "
                f"('{self._config.filters[recovered_idx].name}'), "
                f"next fetch index={self._active_filter_index}",
                level="INFO",
            )

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
        # Auto-provision the relay script for card-to-AppDaemon communication
        await self._provision_relay()

        try:
            await self._refresh_caches_if_stale(
                force=True, reason="startup"
            )
        except Exception as exc:
            self.log(f"Cache refresh on startup failed: {exc}", level="WARNING")

        self._publish_sensor()
        await self._schedule_fetch()

        # Trigger an initial fetch immediately
        self.run_in(self._on_fetch_timer, 2)

    async def _provision_relay(self) -> None:
        """Create script.immich_fetcher_relay if it doesn't exist."""
        ha_url = str(resolve_arg_secret(self.args, "ha_url", default=""))
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping relay provisioning",
                level="WARNING",
            )
            return

        from providers.ha_provisioner import HAProvisioner

        prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)
        try:
            created = await prov.ensure_script("immich_fetcher_relay", {
                "alias": "Immich Fetcher Relay",
                "description": "Relays dashboard commands to AppDaemon",
                "mode": "queued",
                "max": 10,
                "fields": {
                    "command": {
                        "name": "Command",
                        "description": "Command name",
                        "required": True,
                        "selector": {"text": {}},
                    },
                    "payload": {
                        "name": "Payload",
                        "description": "JSON-encoded command data",
                        "required": False,
                        "selector": {"text": {}},
                    },
                },
                "sequence": [{
                    "event": "immich_fetcher_command",
                    "event_data": {
                        "command": "{{ command }}",
                        "payload": "{{ payload | default('{}') }}",
                    },
                }],
            })
            if created:
                self.log("Relay script created: script.immich_fetcher_relay", level="INFO")
            else:
                self.log("Relay script already exists", level="DEBUG")
        except Exception as exc:
            self.log(f"Failed to provision relay script: {exc}", level="ERROR")

    # ------------------------------------------------------------------
    # Immich cache refresh (people + albums)
    # ------------------------------------------------------------------

    async def _refresh_caches(self) -> None:
        metadata = await self._provider.refresh_metadata()
        self._people_available = [p.name for p in metadata.people]
        self._albums_available = [
            {"name": a.name, "id": a.id, "asset_count": a.asset_count}
            for a in metadata.albums
        ]
        self._metadata_last_refresh = datetime.now(timezone.utc)
        self.log(
            f"Caches refreshed: {len(self._people_available)} people, "
            f"{len(self._albums_available)} albums | "
            f"top 10: {self._people_available[:10]}",
            level="INFO",
        )

    async def _refresh_caches_if_stale(
        self, *, force: bool = False, reason: str = "unspecified",
    ) -> None:
        """Refresh metadata cache if stale (or when forced)."""
        if force:
            self.log(f"Refreshing metadata cache ({reason})", level="DEBUG")
            await self._refresh_caches()
            return

        if self._metadata_last_refresh is None:
            self.log(
                f"Refreshing metadata cache ({reason}; never refreshed yet)",
                level="DEBUG",
            )
            await self._refresh_caches()
            return

        now = datetime.now(timezone.utc)
        refresh_after = self._metadata_last_refresh + timedelta(
            minutes=self._metadata_refresh_minutes
        )
        if now >= refresh_after:
            self.log(
                f"Refreshing metadata cache ({reason}; stale)",
                level="DEBUG",
            )
            await self._refresh_caches()

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
            "paused_filters": json.dumps(
                [f.name for f in self._config.filters if f.paused]
            ),
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
        self, *, advance: bool = True, respect_paused: bool = True,
        _skip_count: int = 0,
    ) -> None:
        if self._status == "fetching":
            self.log("Fetch already in progress, skipping", level="DEBUG")
            return

        if not self._config.filters:
            self.log("No filters configured, skipping fetch", level="WARNING")
            return

        idx = self._active_filter_index % len(self._config.filters)
        if respect_paused:
            unpaused_idx = self._next_unpaused_index(idx)
            if unpaused_idx is None:
                self.log(
                    "All filters are paused; skipping fetch",
                    level="INFO",
                )
                self._publish_sensor()
                return
            idx = unpaused_idx
            self._active_filter_index = idx

        try:
            await self._refresh_caches_if_stale(reason="before_fetch")
        except Exception as exc:
            self.log(
                f"Metadata cache refresh failed before fetch; using cached values: {exc}",
                level="WARNING",
            )

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
                # Auto-retry next unpaused filter (bounded to avoid infinite loop)
                if _skip_count < self._unpaused_filter_count() - 1:
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

    def _next_unpaused_index(self, start: int) -> Optional[int]:
        """First unpaused filter index at or after ``start`` (wrapping), or None."""
        n = len(self._config.filters)
        for offset in range(n):
            idx = (start + offset) % n
            if not self._config.filters[idx].paused:
                return idx
        return None

    def _unpaused_filter_count(self) -> int:
        return sum(1 for f in self._config.filters if not f.paused)

    async def _select_assets(self, pf: PhotoFilter) -> List[str]:
        """Use the photo provider to get asset IDs matching the filter."""
        return await self._provider.fetch_photo_ids(
            pf,
            self._config.num_photos,
            location_aliases=self._config.location_aliases,
        )

    async def _download_assets(self, asset_ids: List[str]) -> int:
        """Download preview JPEGs to output_dir, replacing existing files."""
        self._clear_output_dir()
        downloaded = 0
        for i, aid in enumerate(asset_ids):
            try:
                data = await self._provider.download_photo(aid)
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
    # Command handler (all card interactions come through the relay script)
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Route commands dispatched by the relay script."""
        cmd = data.get("command")
        raw = data.get("payload", "{}")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            self.log(f"Invalid command payload: {raw}", level="WARNING")
            return

        if cmd == "update_config":
            self._handle_update_config(payload)
        elif cmd == "sync_filter":
            self._handle_sync_filter(payload)
        elif cmd == "refresh_now":
            self.log("Manual refresh requested via relay", level="INFO")
            self.create_task(self._do_fetch())
        else:
            self.log(f"Unknown command: {cmd}", level="WARNING")

    def _handle_update_config(self, data: dict) -> None:
        """Apply a config update from the dashboard card."""
        self.log(f"Received config update: {list(data.keys())}", level="INFO")
        try:
            new_cfg = FetcherConfig.from_dict(data)
            new_cfg.validate()
        except Exception as exc:
            self.log(f"Invalid config update rejected: {exc}", level="ERROR")
            return

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

        old_paused = {f.name for f in self._config.filters if f.paused}
        new_paused = {f.name for f in new_cfg.filters if f.paused}
        newly_paused = new_paused - old_paused
        resumed = old_paused - new_paused
        if newly_paused:
            self.log(f"Filters paused: {sorted(newly_paused)}", level="INFO")
        if resumed:
            self.log(f"Filters resumed: {sorted(resumed)}", level="INFO")

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

        known_people = set(self._people_available)
        if known_people:
            for filt in new_cfg.filters:
                if filt.people:
                    unknown = [n for n in filt.people if n not in known_people]
                    if unknown:
                        self.log(
                            f"Filter '{filt.name}' has unknown people: {unknown}",
                            level="WARNING",
                        )
            unknown_favs = [
                n for n in new_cfg.favorite_people if n not in known_people
            ]
            if unknown_favs:
                self.log(
                    f"Unknown favorite people: {unknown_favs}",
                    level="WARNING",
                )

        old_interval = self._config.update_interval_minutes
        self._config = new_cfg
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

        # If the filter on display is paused and this save changed pause state,
        # don't leave its photos up until the next timer tick — fetch the next
        # unpaused filter right away.
        filters = self._config.filters
        displaying_paused = bool(
            filters and filters[self._displaying_filter_index].paused
        )
        if (
            displaying_paused
            and (newly_paused or resumed)
            and self._unpaused_filter_count() > 0
        ):
            self.log(
                f"Displaying filter "
                f"'{filters[self._displaying_filter_index].name}' is paused; "
                "fetching next unpaused filter now",
                level="INFO",
            )
            self.create_task(self._do_fetch())

    def _handle_sync_filter(self, data: dict) -> None:
        """Sync a specific filter (fetch with it immediately)."""
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
        if self._config.filters[idx].paused:
            self.log(
                f"Filter '{resolved_name}' is paused; explicit sync fetches it anyway",
                level="INFO",
            )
        self.log(
            f"Sync requested for filter '{resolved_name}' (index={idx})",
            level="INFO",
        )
        self._publish_sensor()
        # Explicit sync overrides pause — the user asked for this exact filter
        self.create_task(self._do_fetch(advance=False, respect_paused=False))
