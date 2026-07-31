# Immich Fetcher

Periodically fetches photos from an [Immich](https://immich.app/) photo library and writes preview JPEGs to disk. Supports dynamic filter configuration from a dashboard card with persistent storage across restarts.

## How it works

1. On startup, provisions a relay script and a status sensor in HA.
2. Connects to the Immich API, caches people and album metadata.
3. Runs a fetch cycle on a configurable interval (default 60 min).
4. Each cycle applies the active filter (all_photos, search, or album), downloads photos, and writes them to `output_dir`.
5. Rotates through configured filters automatically; skips empty filters gracefully. Filters can be paused from the card (`paused: true`) — the rotation skips them until resumed, so a collection of filters can be kept configured with only some enabled. An explicit per-filter "fetch now" still works on a paused filter. If every filter is paused, the cycle idles without fetching.
6. Publishes status (last fetch, next fetch, active filter, people/albums available) via a virtual sensor.

## Dependencies

- `providers.photo_providers.ImmichDataProvider` — photo source abstraction
- `providers.ha_provisioner.HAProvisioner` — HA entity provisioning
- `providers.secrets.resolve_secret()` — credential resolution

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `sensor.immich_fetcher_status` | Virtual sensor | Fetch status, filters, people/albums metadata |
| `script.immich_fetcher_relay` | Script | Card-to-AppDaemon relay for dashboard commands |

## Associated card

`immich-fetcher-card.js` — Lovelace card for filter editing (including per-filter pause/resume), manual refresh, metadata viewing.

## Config (apps.yaml)

### Required

```yaml
immich_fetcher:
  module: immich_fetcher.immich_fetcher_app
  class: ImmichFetcherApp
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  immich_url_env: IMMICH_URL
  immich_api_key_env: IMMICH_API_KEY
```

### Optional (with defaults)

| Key | Default | Description |
|-----|---------|-------------|
| `output_dir` | `/media/immich-photos` | Directory for downloaded photos |
| `config_file` | `/media/immich-fetcher/config.json` | Persisted config JSON path |
| `update_interval_minutes` | `60` | Fetch frequency |
| `num_photos` | `10` | Photos per fetch |
| `download_quality` | `preview` | `preview`, `fullsize`, or `original` |
| `default_filters` | `[]` | Array of PhotoFilter objects |

## Manual setup

- Immich server must be accessible from the AppDaemon pod/host.
- `IMMICH_API_KEY` env var must be set (Kubernetes ExternalSecret or `.env`).

## Downstream consumers

- `photo_frame_viewer` reads photos from `output_dir` to display on dashboards.
