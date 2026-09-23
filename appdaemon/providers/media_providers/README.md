# media_providers

HTTP clients and fetchers behind the media dashboard: Plex (via Tautulli), TMDb, Google showtimes (via SerpApi) and the MDbList ratings aggregator. Pure Python — no AppDaemon imports — so every module here is unit-testable without a running AppDaemon.

Consumed by `appdaemon/apps/media_dashboard_app/`. All outbound HTTP for that app lives here (security rule S2); API keys arrive as `_env` variable *names* and are resolved at construction time through `providers.secrets.resolve_secret()` (S1/S7).

## Layers

| Layer | Responsibility |
|-------|----------------|
| `*_client.py` | Transport only: one method per endpoint, `aiohttp` in, parsed JSON out. No business rules. |
| `*_fetcher.py` | Normalisation: endpoint JSON → `MediaItem` / `ShowtimeCache`, plus filtering, poster download and multi-page/multi-theater aggregation. |
| `types.py` | Shared dataclasses: `MediaItem`, `ShowtimeEntry`, `ShowtimeCache`, `CinemaInfo`, `FetchResult`. |

## Modules

| Module | What it does |
|--------|--------------|
| `tautulli_client.py` | Tautulli REST API — recently added, home stats, `pms_image_proxy` poster bytes (keeps the Plex token server-side). |
| `tautulli_fetcher.py` | Recently-added movies/shows → `MediaItem`, TMDb cross-reference, poster caching. |
| `tmdb_client.py` | TMDb v3 — `now_playing`, `upcoming`, `trending`, `discover`, `genre/movie/list`, movie/TV detail. |
| `tmdb_fetcher.py` | `fetch_in_theaters()` (now-playing pages 1-`NOW_PLAYING_PAGES` + trending, `release_type` records which), `fetch_coming_soon()` (upcoming + digital discover), `fetch_detail()`, `download_posters()`, popularity/vote/genre filters. |
| `serpapi_client.py` | SerpApi Google search. `get_showtimes()` always pins the locale (`hl=en`, `gl=us`, `google_domain=google.com`) — without it the exit node picks the language and a theater can come back in another language entirely. |
| `serpapi_fetcher.py` | One search per configured theater → a `ShowtimeCache` keyed by lowercase film title, day labels normalised to ISO dates. An unrecognised day label logs one WARNING per theater and falls back to today. |
| `mdblist_client.py` | MDbList ratings (IMDb, Rotten Tomatoes critics/audience, Metacritic) with `extract_ratings()` as a pure parser. |

## Contracts

- **`FetchResult`** — every fetcher returns one: `items`, `status` (`"ok"` / `"error"`), `error_message`. Fetchers never raise for upstream failure; the app decides whether to retain last-known-good data.
- **`ShowtimeCache`** — `date` is the *local* calendar date of the fetch; `films` maps a lowercase film title to `ShowtimeEntry` rows that each carry an ISO `date`. The consuming app judges freshness by calendar day and drops entries dated before today (see the app README).
- **`MediaItem.release_type`** — `"in_theaters"` (TMDb now-playing), `"trending"` (trending only), `"theatrical"` / `"digital"` (coming soon).

## Tests

`appdaemon/tests/test_tautulli_fetcher.py`, `test_tmdb_fetcher.py`, `test_serpapi_client.py`, `test_serpapi_fetcher.py`, `test_mdblist_client.py`. No test makes a real request — SerpApi in particular is a 250-search/month plan, so live calls belong in `tests/integration-tests/` behind an env gate.
