"""Media Dashboard AppDaemon app.

Orchestrates Tautulli, TMDb, and SerpApi fetchers to produce a media
dashboard sensor that drives the wall-display media card.  Supports
user preference commands (dismiss / like / undo_dismiss) and showtime
lookups via a relay script pattern.

All external HTTP calls are delegated to providers (security rule S2).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# AppDaemon only adds `appdaemon/apps` to sys.path.  Providers live at
# `appdaemon/providers`, so append the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from providers.media_providers.tautulli_fetcher import TautulliFetcher
from providers.media_providers.tmdb_fetcher import TmdbFetcher
from providers.media_providers.serpapi_fetcher import SerpApiFetcher
from providers.media_providers.types import MediaItem, ShowtimeCache
from providers.secrets import resolve_arg_secret

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENSOR_STATUS = "sensor.media_dashboard_status"
SENSOR_DETAIL = "sensor.media_dashboard_detail"
RELAY_SCRIPT_ID = "media_dashboard_relay"
COMMAND_EVENT = "media_dashboard_command"

# Showtimes older than this many hours are noted as stale but still shown
_SHOWTIME_STALE_WARN_HOURS = 24
# Showtimes older than this many hours are omitted entirely
_SHOWTIME_STALE_OMIT_HOURS = 48


class MediaDashboardApp(hass.Hass):
    """Wall-display media dashboard with Plex, TMDb, and theater showtimes."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        cfg = self.args or {}

        # --- Tautulli config ---
        self._tautulli_url: str = str(cfg.get("tautulli_url", ""))
        self._tautulli_api_key_env: str = str(cfg.get("tautulli_api_key_env", ""))

        # --- TMDb config ---
        self._tmdb_api_key_env: str = str(cfg.get("tmdb_api_key_env", ""))

        # --- SerpApi config ---
        self._serpapi_api_key_env: str = str(cfg.get("serpapi_api_key_env", ""))
        self._location: str = str(cfg.get("location", ""))
        self._theaters: List[str] = list(cfg.get("theaters", []))

        # --- Refresh intervals ---
        self._tautulli_refresh_interval: int = int(cfg.get("tautulli_refresh_interval", 7200))
        self._tmdb_refresh_interval: int = int(cfg.get("tmdb_refresh_interval", 43200))
        self._showtimes_refresh_interval: int = int(cfg.get("showtimes_refresh_interval", 86400))

        # --- Item filtering ---
        self._max_items_per_category: int = int(cfg.get("max_items_per_category", 10))
        self._popularity_threshold: float = float(cfg.get("popularity_threshold", 10.0))
        self._vote_count_threshold: int = int(cfg.get("vote_count_threshold", 50))
        self._genre_filter: List[str] = list(cfg.get("genre_filter", []))
        self._stale_ttl_days: int = int(cfg.get("stale_ttl_days", 7))

        # --- Filesystem paths ---
        media_fs_root: str = str(
            resolve_arg_secret(cfg, "media_fs_root", default="/media")
        ).rstrip("/") or "/media"

        self._poster_media_subdir: str = str(cfg.get("poster_media_subdir", "media-dashboard/posters"))
        self._poster_www_subdir: str = str(cfg.get("poster_www_subdir", "media-dashboard/posters"))
        self._preferences_file_subdir: str = str(cfg.get("preferences_file_subdir", "media-dashboard"))
        self._showtime_cache_subdir: str = str(cfg.get("showtime_cache_subdir", "media-dashboard"))

        self._poster_dir: str = os.path.join(media_fs_root, self._poster_media_subdir)
        self._preferences_dir: str = os.path.join(media_fs_root, self._preferences_file_subdir)
        self._showtime_cache_dir: str = os.path.join(media_fs_root, self._showtime_cache_subdir)

        self._preferences_file: str = os.path.join(self._preferences_dir, "preferences.json")
        self._showtime_cache_file: str = os.path.join(self._showtime_cache_dir, "showtime-cache.json")

        # Poster image settings
        self._poster_width: int = int(cfg.get("poster_width", 342))
        self._poster_sync_shell_command: str = str(
            cfg.get("poster_sync_shell_command", "media_dashboard_sync_posters")
        )

        # --- HA provisioning ---
        self._ha_url: str = str(resolve_arg_secret(cfg, "ha_url", default=""))
        self._ha_token_env: str = str(cfg.get("ha_token_env", ""))

        # --- Ensure directories exist ---
        os.makedirs(self._poster_dir, exist_ok=True)
        os.makedirs(self._preferences_dir, exist_ok=True)
        os.makedirs(self._showtime_cache_dir, exist_ok=True)

        # --- State ---
        self._categories: Dict[str, List[MediaItem]] = {
            "in_theaters": [],
            "plex_new": [],
            "coming_soon": [],
        }
        self._fetch_status: Dict[str, str] = {
            "tautulli": "pending",
            "tmdb": "pending",
            "serpapi": "pending",
        }
        self._preferences: Dict[str, Any] = {
            "hidden": [],
            "liked": [],
            "hidden_at": {},
            "liked_at": {},
        }

        # --- Load persisted preferences ---
        self._load_preferences()

        # --- Instantiate fetchers ---
        self._tautulli = TautulliFetcher(
            base_url=self._tautulli_url,
            api_key_env=self._tautulli_api_key_env,
            poster_dir=self._poster_dir,
            poster_width=self._poster_width,
        )
        self._tmdb = TmdbFetcher(
            api_key_env=self._tmdb_api_key_env,
            poster_dir=self._poster_dir,
            poster_width=self._poster_width,
            popularity_threshold=self._popularity_threshold,
            vote_count_threshold=self._vote_count_threshold,
            genre_filter=self._genre_filter if self._genre_filter else None,
        )
        self._serpapi = SerpApiFetcher(
            api_key_env=self._serpapi_api_key_env,
            location=self._location,
            theater_names=self._theaters,
        )

        self.log(
            f"MediaDashboardApp initializing: "
            f"tautulli_url={self._tautulli_url}, "
            f"location={self._location}, "
            f"theaters={self._theaters}, "
            f"poster_dir={self._poster_dir}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Provision relay, fetch data, register listeners."""
        await self._provision_relay()

        # Initial data fetch
        await self._refresh_all()

        # Register relay command listener
        self.listen_event(self._on_command, COMMAND_EVENT)

        # Schedule periodic refreshes
        self.run_every(
            self._on_tautulli_refresh_timer,
            f"now+{self._tautulli_refresh_interval}",
            self._tautulli_refresh_interval,
        )
        self.run_every(
            self._on_tmdb_refresh_timer,
            f"now+{self._tmdb_refresh_interval}",
            self._tmdb_refresh_interval,
        )
        self.run_every(
            self._on_showtimes_refresh_timer,
            f"now+{self._showtimes_refresh_interval}",
            self._showtimes_refresh_interval,
        )

        self.log("MediaDashboardApp started", level="INFO")

    async def _provision_relay(self) -> None:
        """Create the relay script if it does not exist."""
        if not self._ha_url or not self._ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        from providers.ha_provisioner import HAProvisioner

        prov = HAProvisioner(ha_url=self._ha_url, ha_token_env=self._ha_token_env)

        try:
            created = await prov.ensure_script(RELAY_SCRIPT_ID, {
                "alias": "Media Dashboard Relay",
                "description": "Relays dashboard card commands to AppDaemon",
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
                    "event": COMMAND_EVENT,
                    "event_data": {
                        "command": "{{ command }}",
                        "payload": "{{ payload | default('{}') }}",
                    },
                }],
            })
            msg = "created" if created else "already exists"
            self.log(
                f"Relay script.{RELAY_SCRIPT_ID} {msg}",
                level="INFO" if created else "DEBUG",
            )
        except Exception as exc:
            self.log(
                f"Failed to provision relay script: {exc!r}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Refresh timer callbacks
    # ------------------------------------------------------------------

    def _on_tautulli_refresh_timer(self, kwargs: Any) -> None:
        self.create_task(self._refresh_tautulli())

    def _on_tmdb_refresh_timer(self, kwargs: Any) -> None:
        self.create_task(self._refresh_tmdb())

    def _on_showtimes_refresh_timer(self, kwargs: Any) -> None:
        self.create_task(self._refresh_showtimes())

    # ------------------------------------------------------------------
    # Refresh methods (stale-retention pattern)
    # ------------------------------------------------------------------

    async def _refresh_tautulli(self) -> None:
        """Fetch recently-added from Tautulli; retain last-good on failure."""
        self.log("Refreshing Tautulli data", level="DEBUG")
        try:
            result = await self._tautulli.fetch_recently_added(count=50)
            if result.status == "error":
                raise RuntimeError(result.error_message)

            await self._tautulli.download_posters(result.items)
            self._categories["plex_new"] = result.items
            self._fetch_status["tautulli"] = "ok"
            self._sync_posters()
            self._publish_sensor()
            self.log(
                f"Tautulli refresh complete: {len(result.items)} items",
                level="INFO",
            )
        except Exception as exc:
            self.log(
                f"Tautulli refresh failed (retaining previous data): {exc!r}",
                level="ERROR",
            )
            self._fetch_status["tautulli"] = "error"
            self._publish_sensor()

    async def _refresh_tmdb(self) -> None:
        """Fetch in-theaters and coming-soon from TMDb; retain last-good on failure."""
        self.log("Refreshing TMDb data", level="DEBUG")
        try:
            theaters_result = await self._tmdb.fetch_in_theaters()
            coming_result = await self._tmdb.fetch_coming_soon()

            if theaters_result.status == "error":
                raise RuntimeError(theaters_result.error_message)
            if coming_result.status == "error":
                raise RuntimeError(coming_result.error_message)

            all_items = theaters_result.items + coming_result.items
            await self._tmdb.download_posters(all_items)

            self._categories["in_theaters"] = theaters_result.items
            self._categories["coming_soon"] = coming_result.items
            self._fetch_status["tmdb"] = "ok"
            self._sync_posters()
            self._publish_sensor()
            self.log(
                f"TMDb refresh complete: "
                f"{len(theaters_result.items)} in_theaters, "
                f"{len(coming_result.items)} coming_soon",
                level="INFO",
            )
        except Exception as exc:
            self.log(
                f"TMDb refresh failed (retaining previous data): {exc!r}",
                level="ERROR",
            )
            self._fetch_status["tmdb"] = "error"
            self._publish_sensor()

    async def _refresh_showtimes(self) -> None:
        """Fetch showtimes from SerpApi; retain cached version on failure."""
        self.log("Refreshing showtimes", level="DEBUG")
        try:
            cache = await self._serpapi.fetch_showtimes()

            # Write cache to disk
            self._write_showtime_cache(cache)

            # Update has_showtimes flags on in_theaters items
            film_titles_lower = {t.lower() for t in cache.films.keys()}
            for item in self._categories["in_theaters"]:
                item.has_showtimes = item.title.lower() in film_titles_lower

            self._fetch_status["serpapi"] = "ok"
            self._publish_sensor()
            self.log(
                f"Showtime refresh complete: {len(cache.films)} films",
                level="INFO",
            )
        except Exception as exc:
            self.log(
                f"Showtime refresh failed (retaining cached data): {exc!r}",
                level="ERROR",
            )
            self._fetch_status["serpapi"] = "error"
            self._publish_sensor()

    async def _refresh_all(self) -> None:
        """Run all three refreshes sequentially to avoid API rate limits."""
        await self._refresh_tautulli()
        await self._refresh_tmdb()
        await self._refresh_showtimes()

    # ------------------------------------------------------------------
    # Sensor publication
    # ------------------------------------------------------------------

    def _publish_sensor(self) -> None:
        """Build and publish the main status sensor."""
        published_categories: Dict[str, List[dict]] = {}

        for cat_name, items in self._categories.items():
            filtered = self._apply_stale_ttl(items)
            filtered = self._apply_preferences(filtered)
            capped = filtered[: self._max_items_per_category]
            published_categories[cat_name] = [item.to_dict() for item in capped]

        # Compute overall state
        statuses = list(self._fetch_status.values())
        if all(s == "ok" for s in statuses):
            overall = "ok"
        elif all(s == "error" for s in statuses):
            overall = "error"
        else:
            overall = "partial"

        attrs = {
            "categories": published_categories,
            "fetch_status": dict(self._fetch_status),
            "last_updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "friendly_name": "Media Dashboard Status",
            "icon": "mdi:movie-roll",
        }

        self.log(
            f"Publishing sensor: state={overall}, "
            f"in_theaters={len(published_categories.get('in_theaters', []))}, "
            f"plex_new={len(published_categories.get('plex_new', []))}, "
            f"coming_soon={len(published_categories.get('coming_soon', []))}",
            level="DEBUG",
        )
        self.set_state(SENSOR_STATUS, state=overall, attributes=attrs)

    def _publish_detail(self, item: MediaItem, showtimes: dict) -> None:
        """Publish full item detail (including summary + showtimes) to detail sensor."""
        attrs = item.to_detail_dict()
        attrs["showtimes"] = showtimes
        attrs["showtimes_date"] = datetime.date.today().isoformat()
        attrs["friendly_name"] = f"Media Dashboard Detail: {item.title}"

        self.log(f"Publishing detail sensor for item: {item.id}", level="DEBUG")
        self.set_state(SENSOR_DETAIL, state=item.id, attributes=attrs)

    # ------------------------------------------------------------------
    # Command handler
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Route commands dispatched by the relay script."""
        cmd = data.get("command")
        raw = data.get("payload", "{}")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            self.log(f"Invalid command payload: {raw!r}", level="WARNING")
            return

        self.log(f"Received command: {cmd}", level="DEBUG")

        if cmd == "refresh":
            source = payload.get("source", "all")
            if source == "tautulli":
                self.create_task(self._refresh_tautulli())
            elif source == "tmdb":
                self.create_task(self._refresh_tmdb())
            elif source == "showtimes":
                self.create_task(self._refresh_showtimes())
            else:
                self.create_task(self._refresh_all())
        elif cmd == "get_detail":
            self.create_task(self._handle_get_detail(payload))
        elif cmd == "dismiss":
            self._handle_dismiss(payload)
        elif cmd == "like":
            self._handle_like(payload)
        elif cmd == "undo_dismiss":
            self._handle_undo_dismiss(payload)
        else:
            self.log(f"Unknown command: {cmd!r}", level="WARNING")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _handle_get_detail(self, payload: dict) -> None:
        """Find item, optionally enrich from TMDb, look up showtimes, publish detail."""
        item_id = payload.get("id", "")
        if not item_id:
            self.log("get_detail command missing 'id'", level="WARNING")
            return

        # Find item across all categories
        item: Optional[MediaItem] = None
        for items in self._categories.values():
            for candidate in items:
                if candidate.id == item_id:
                    item = candidate
                    break
            if item is not None:
                break

        if item is None:
            self.log(f"get_detail: item not found: {item_id}", level="WARNING")
            return

        # Enrich from TMDb if summary is empty and we have a tmdb_id
        if item.tmdb_id and not item.summary:
            try:
                detailed = await self._tmdb.fetch_detail(item.tmdb_id, item.media_type)
                item.summary = detailed.summary
                if detailed.runtime_min:
                    item.runtime_min = detailed.runtime_min
                if detailed.genres:
                    item.genres = detailed.genres
            except Exception as exc:
                self.log(
                    f"TMDb detail fetch failed for {item_id}: {exc!r}",
                    level="WARNING",
                )

        # Read showtime cache from disk and find matching showtimes
        showtimes = self._get_showtimes_for_item(item)

        self._publish_detail(item, showtimes)

    def _handle_dismiss(self, payload: dict) -> None:
        """Add item to hidden list and re-publish sensor."""
        item_id = payload.get("id", "")
        if not item_id:
            self.log("dismiss command missing 'id'", level="WARNING")
            return

        if item_id not in self._preferences["hidden"]:
            self._preferences["hidden"].append(item_id)
            self._preferences["hidden_at"][item_id] = datetime.datetime.now().isoformat()
        self._save_preferences()
        self._publish_sensor()
        self.log(f"Dismissed item: {item_id}", level="INFO")

    def _handle_like(self, payload: dict) -> None:
        """Add item to liked list and re-publish sensor."""
        item_id = payload.get("id", "")
        if not item_id:
            self.log("like command missing 'id'", level="WARNING")
            return

        if item_id not in self._preferences["liked"]:
            self._preferences["liked"].append(item_id)
            self._preferences["liked_at"][item_id] = datetime.datetime.now().isoformat()
        self._save_preferences()
        self._publish_sensor()
        self.log(f"Liked item: {item_id}", level="INFO")

    def _handle_undo_dismiss(self, payload: dict) -> None:
        """Remove item from hidden list and re-publish sensor."""
        item_id = payload.get("id", "")
        if not item_id:
            self.log("undo_dismiss command missing 'id'", level="WARNING")
            return

        if item_id in self._preferences["hidden"]:
            self._preferences["hidden"].remove(item_id)
            self._preferences["hidden_at"].pop(item_id, None)
        self._save_preferences()
        self._publish_sensor()
        self.log(f"Undo-dismissed item: {item_id}", level="INFO")

    # ------------------------------------------------------------------
    # Preference helpers
    # ------------------------------------------------------------------

    def _apply_preferences(self, items: List[MediaItem]) -> List[MediaItem]:
        """Filter out hidden items and sort: liked first, then by popularity."""
        hidden = set(self._preferences.get("hidden", []))
        liked = set(self._preferences.get("liked", []))

        visible = [item for item in items if item.id not in hidden]

        # Sort: liked items first, then by tmdb_popularity descending
        visible.sort(
            key=lambda x: (0 if x.id in liked else 1, -x.tmdb_popularity)
        )
        return visible

    def _apply_stale_ttl(self, items: List[MediaItem]) -> List[MediaItem]:
        """Remove items older than stale_ttl_days based on added_at or release_date."""
        if self._stale_ttl_days <= 0:
            return list(items)

        cutoff = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
            days=self._stale_ttl_days
        )

        result = []
        for item in items:
            date_str = item.added_at or item.release_date
            if not date_str:
                result.append(item)
                continue
            try:
                # Parse as ISO date or datetime
                if "T" in date_str:
                    # Full ISO datetime — may or may not have tz info
                    dt = datetime.datetime.fromisoformat(date_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                else:
                    # Date only (YYYY-MM-DD)
                    dt = datetime.datetime.fromisoformat(date_str).replace(
                        tzinfo=datetime.timezone.utc
                    )
                if dt >= cutoff:
                    result.append(item)
                else:
                    self.log(
                        f"Evicting stale item {item.id} (date={date_str})",
                        level="DEBUG",
                    )
            except (ValueError, TypeError):
                # Unparseable date — keep the item
                result.append(item)

        return result

    def _load_preferences(self) -> None:
        """Load preferences from the JSON file; reset to defaults on any error."""
        try:
            with open(self._preferences_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._preferences = {
                "hidden": list(data.get("hidden", [])),
                "liked": list(data.get("liked", [])),
                "hidden_at": dict(data.get("hidden_at", {})),
                "liked_at": dict(data.get("liked_at", {})),
            }
            self.log(
                f"Loaded preferences: "
                f"{len(self._preferences['hidden'])} hidden, "
                f"{len(self._preferences['liked'])} liked",
                level="INFO",
            )
        except FileNotFoundError:
            self.log("No preferences file found — starting fresh", level="INFO")
        except Exception as exc:
            self.log(
                f"Failed to load preferences ({exc!r}) — starting fresh",
                level="WARNING",
            )

    def _save_preferences(self) -> None:
        """Persist preferences to the JSON file."""
        try:
            os.makedirs(self._preferences_dir, exist_ok=True)
            with open(self._preferences_file, "w", encoding="utf-8") as fh:
                json.dump(self._preferences, fh, indent=2)
        except Exception as exc:
            self.log(f"Failed to save preferences: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Showtime cache helpers
    # ------------------------------------------------------------------

    def _write_showtime_cache(self, cache: ShowtimeCache) -> None:
        """Write a ShowtimeCache to disk."""
        try:
            os.makedirs(self._showtime_cache_dir, exist_ok=True)
            with open(self._showtime_cache_file, "w", encoding="utf-8") as fh:
                json.dump(cache.to_dict(), fh, indent=2)
        except Exception as exc:
            self.log(f"Failed to write showtime cache: {exc!r}", level="ERROR")

    def _read_showtime_cache(self) -> Optional[ShowtimeCache]:
        """Read the showtime cache from disk. Returns None if missing or corrupt."""
        try:
            with open(self._showtime_cache_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return ShowtimeCache.from_dict(data)
        except FileNotFoundError:
            return None
        except Exception as exc:
            self.log(f"Failed to read showtime cache: {exc!r}", level="WARNING")
            return None

    def _get_showtimes_for_item(self, item: MediaItem) -> dict:
        """Find showtimes for an item from the on-disk cache.

        Returns a dict with:
        - ``entries``: list of {cinema_name, times} for each matching cinema
        - ``stale``: boolean (cache is >24h old)
        - ``note``: optional human-readable staleness message
        """
        cache = self._read_showtime_cache()
        if cache is None:
            return {"entries": [], "note": "No showtime data available"}

        # Staleness check
        stale = False
        note = ""
        if cache.date:
            try:
                cache_dt = datetime.datetime.fromisoformat(cache.date).replace(
                    tzinfo=datetime.timezone.utc
                )
                age_hours = (
                    datetime.datetime.now(tz=datetime.timezone.utc) - cache_dt
                ).total_seconds() / 3600
                if age_hours >= _SHOWTIME_STALE_OMIT_HOURS:
                    return {"entries": [], "note": "Showtime data is too old (>48h)"}
                if age_hours >= _SHOWTIME_STALE_WARN_HOURS:
                    stale = True
                    note = f"Showtime data may be stale (fetched {cache.date})"
            except (ValueError, TypeError):
                pass

        # Match by title (case-insensitive)
        title_lower = item.title.lower()
        entries = []
        for film_title, showtime_list in cache.films.items():
            if film_title.lower() == title_lower or title_lower in film_title.lower():
                for st_entry in showtime_list:
                    entries.append({
                        "cinema_name": st_entry.cinema_name,
                        "times": st_entry.times,
                    })

        result: dict = {"entries": entries}
        if stale:
            result["stale"] = True
            result["note"] = note
        return result

    # ------------------------------------------------------------------
    # Poster management
    # ------------------------------------------------------------------

    def _sync_posters(self) -> None:
        """Trigger the shell_command that rsync/copies posters to www."""
        self.call_service(f"shell_command/{self._poster_sync_shell_command}")

    def _cleanup_stale_posters(self) -> None:
        """Delete poster files not referenced by any active category item."""
        active_posters: set = set()
        for items in self._categories.values():
            for item in items:
                if item.local_poster:
                    active_posters.add(item.local_poster)

        try:
            for fname in os.listdir(self._poster_dir):
                if fname not in active_posters:
                    fpath = os.path.join(self._poster_dir, fname)
                    try:
                        os.remove(fpath)
                        self.log(f"Removed stale poster: {fname}", level="DEBUG")
                    except OSError as exc:
                        self.log(
                            f"Failed to remove stale poster {fname}: {exc!r}",
                            level="WARNING",
                        )
            self._sync_posters()
        except OSError as exc:
            self.log(f"Failed to list poster dir: {exc!r}", level="ERROR")
