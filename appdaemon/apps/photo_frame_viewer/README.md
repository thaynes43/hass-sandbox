# Photo Frame Viewer

Displays rotating photos from a source directory on a Lovelace dashboard card with slideshow controls: pause/resume, interval adjustment, auto-unpause configuration, and manual navigation.

## How it works

1. On startup, provisions a relay script, an image picker (`input_select`), and a status sensor.
2. Polls a source directory (typically `immich_fetcher` output) for image files.
3. Stages images for serving via HA shell commands that atomically swap content into `/config/www/photo-frame/live/<gen>/`.
4. **Verifies** the staged generation over HTTP before using it (see below), then marks it pending.
5. Cycles through images on a configurable interval, publishing the current image URL and runtime settings via the status sensor.
6. Dashboard card reads the sensor for the current image URL with cache-busting.
7. Dashboard-managed slide interval and auto-unpause settings persist across AppDaemon restarts via `state_dir/state.json`.

### Staging verification

**Invariant: a generation is never marked pending — and therefore never published — until Home Assistant is verifiably serving it.**

The staging `shell_command`'s return value is not trustworthy. Home Assistant kills every `shell_command` at a hard 60-second timeout; when the NFS source stalls mid-copy the shell dies before the atomic `mv`, so the generation directory never appears even though the service call "completed". The app used to publish on a fixed 3-second settle delay and put broken images on the wall display for 10+ minutes.

Instead, after firing the stage command the app polls the exact URL the card will load — `{ha_url}{ha_local_url_base}/{gen}/{first file}` — with an HTTP `HEAD`:

- First check after `stage_settle_delay_s`, then every `stage_verify_interval_s` until `stage_verify_timeout_s` (measured from the stage call).
- `200` means staged. Because the stage script's final step is an atomic directory `mv`, one file existing implies the whole generation exists.
- Anything else (404, connection error, timeout) means "not yet".
- On success the generation becomes pending as before. On deadline the app logs a `WARNING`, cleans up the dead generation, publishes nothing, and lets the next source poll re-stage automatically (the album title is preserved for the retry).
- While a generation is being verified, staging is latched — a new source batch waits for the current one to resolve. Because the deadline is only evaluated when a probe result arrives, an absolute-deadline watchdog (`stage_verify_timeout_s` + 15 s) abandons the generation and releases the latch even if the verification chain stops responding entirely. Without it a single lost callback would block every future stage until AppDaemon restarted.

`/local/...` is unauthenticated static content, so the probe sends **no** `Authorization` header. The HTTP call itself lives in `providers/ha_provisioner/local_file_check.py` (security policy S2).

The stage service call is issued with AppDaemon's `callback=` form so it returns immediately instead of pinning the app's worker thread for up to 60 seconds — a blocking call used to starve the slideshow tick.

If `ha_url` / `ha_url_env` is not configured, verification is impossible: the app logs a single `WARNING` at startup and falls back to the legacy behaviour (assume ready after `stage_settle_delay_s`).

## Dependencies

- `photo_frame_viewer.gen_helpers` — URL generation, fingerprinting, label building
- `providers.ha_provisioner.HAProvisioner` — HA entity provisioning
- `providers.ha_provisioner.local_file_exists()` — HTTP `HEAD` probe used to verify a staged generation
- `providers.secrets.resolve_secret()` — credential resolution

## Upstream dependencies

- `immich_fetcher` — provides the source photos in `source_dir`

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_select.{prefix}_photo_frame_image` | Input Select | Image picker dropdown |
| `sensor.{prefix}_photo_frame_status` | Virtual sensor | State (paused/playing), image URL, interval, and auto-unpause metadata |
| `script.{prefix}_photo_frame_relay` | Script | Card-to-AppDaemon relay |

Where `{prefix}` defaults to `wall_display` (configurable via `entity_prefix`).

## Associated card

`photo-frame-viewer-card.js` — Lovelace card for pause, interval slider, enable/disable auto-unpause, auto-unpause seconds, and next/previous navigation.

## Config (apps.yaml)

### Required

```yaml
photo_frame_viewer_wall_display:
  module: photo_frame_viewer.photo_frame_viewer_app
  class: PhotoFrameViewerApp
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  stage_shell_command: photo_frame_stage_gen
  cleanup_shell_command: photo_frame_cleanup_gen
```

`ha_url_env` (or `ha_url`) is required for staging verification as well as for provisioning. Without it the app cannot check whether a generation actually landed.

### Optional (with defaults)

| Key | Default | Description |
|-----|---------|-------------|
| `source_dir` | `/media/immich-photos` | Directory to scan for photos |
| `ha_source_dir` | (same as source_dir) | HA-side path if filesystem differs |
| `ha_local_url_base` | `/local/photo-frame/live` | Base URL for served images |
| `source_poll_interval_s` | `30` | Poll disk for source changes |
| `stage_settle_delay_s` | `3` | Delay before the **first** staging verification check |
| `stage_verify_interval_s` | `5` | Delay between subsequent verification checks |
| `stage_verify_timeout_s` | `240` | Give up on a generation this long after the stage call and re-stage |
| `fallback_image_path` | `/config/www/immich-album/no-image.jpg` | Fallback when source is empty |
| `options_max` | `100` | Max options in picker |
| `auto_cycle` | `true` | Auto-advance images |
| `reset_timer_on_manual_nav` | `true` | Restart timer on manual selection |
| `default_interval_s` | `10` | Slideshow interval in seconds |
| `pause_auto_resume_s` | `600` | Automatically resume after this many paused seconds (`0` disables) |
| `entity_prefix` | `wall_display` | Prefix for all provisioned entity IDs |
| `state_dir` | `/media/photo-frame-viewer/{prefix}` | Persisted state directory for dashboard-managed settings like interval and auto-unpause |

## Manual setup required

These cannot be auto-provisioned and must be configured manually:

### Shell commands (`configuration.yaml`)

The stage command runs its work in a **detached** background subshell:

```yaml
shell_command:
  photo_frame_stage_gen: >-
    /bin/sh -c 'set -e;
    live_root="/config/www/photo-frame/live";
    src="{{ source_dir }}";
    gen="{{ gen_id }}";
    keep="{{ keep_gens | default("") }}";
    dest="$live_root/$gen";
    tmp="$live_root/.staging-$gen";
    lock="$live_root/.stage.lock";
    log="$live_root/.stage.log";

    [ -n "$gen" ] || { echo "gen_id empty"; exit 2; };
    case "$keep" in *[!0-9\ ]*) keep="";; esac;
    [ -d "$live_root" ] || mkdir -p "$live_root";
    if [ -f "$log" ] && [ "$(wc -c < "$log")" -gt 65536 ]; then : > "$log"; fi;

    (
      exec 9>"$lock";
      i=0;
      until flock -n 9; do
        i=$((i+1));
        if [ "$i" -ge 150 ]; then echo "$(date +%FT%T) gen=$gen lock busy, giving up"; exit 3; fi;
        sleep 1;
      done;
      find "$live_root" -maxdepth 1 -type d -name ".staging-*" -mmin +60 -exec rm -rf -- {} \; 2>/dev/null || true;
      rm -rf -- "$tmp" "$dest";
      mkdir -p "$tmp";
      if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
        cp -a "$src"/. "$tmp"/;
        mv "$tmp" "$dest";
        echo "$(date +%FT%T) gen=$gen staged $(ls "$dest" | wc -l) files";
        if [ -n "$keep" ]; then
          for d in "$live_root"/*/; do
            [ -d "$d" ] || continue;
            n=$(basename "$d");
            case " $keep $gen " in *" $n "*) continue;; esac;
            rm -rf -- "$d";
            echo "$(date +%FT%T) gen=$gen pruned unreferenced generation $n";
          done;
        fi;
      else
        rm -rf -- "$tmp";
        echo "$(date +%FT%T) gen=$gen source empty or missing: $src";
        exit 1;
      fi
    ) >> "$log" 2>&1 < /dev/null &'

  photo_frame_cleanup_gen: >-
    /bin/sh -c 'set -e;
    target="/config/www/photo-frame/live/{{ gen_id }}";
    if [ -d "$target" ]; then rm -rf "$target"; fi'
```

**Why detached.** Home Assistant kills every `shell_command` at a hard 60-second timeout. The source is an NFS mount that intermittently stalls ~100 s mid-copy, so the inline version was killed before its atomic `mv` and the generation directory never appeared. Detaching the copy into a background subshell lets it finish the `mv` regardless of how long HA waits. The command's return value no longer matters to the app — it verifies over HTTP instead — so returning immediately costs nothing. Progress and failures go to `/config/www/photo-frame/live/.stage.log` (self-truncating at 64 KB), which is the only place the copy's outcome is recorded. The `flock` serialises concurrent stages, and the old `-mmin +60` sweep still reaps abandoned `.staging-*` directories.

**Why the keep-list prune.** Detaching creates a second leak: a generation the app abandons can still be completed by its worker minutes later — the app's `photo_frame_cleanup_gen` already ran against a directory that did not exist yet — and nothing would ever reclaim it. So every stage call carries `keep_gens`, a space-separated list of the generations the app still needs, and a successful stage deletes every other generation directory it finds (the one it just staged is always kept). That reclaims late-landing orphans and any left over from earlier gen-counter epochs. The list is safe because the app's staging latch blocks a second stage while one is in flight, so the only generation that can become current before the prune runs is the one being staged. The script refuses a non-numeric `keep` value and prunes nothing when the field is empty, so an old app paired with this script is merely a no-op, as is an old script paired with the new app (it ignores the extra variable).

**One viewer instance per `live` directory.** Generation ids are plain per-instance counters, so two instances sharing a live root already collided on directory names; with the prune they would now also delete each other's generations. Give each display its own `ha_local_url_base` (and matching shell commands) if you run more than one.

The app works with **both** the old inline command and this detached one. With the old command a >60 s stall simply fails verification and the next poll re-stages.

Restart Home Assistant after changing `shell_command:`.

### Directory structure

`/config/www/photo-frame/live/` must exist inside the HA container (created by shell commands on first run).

### Lovelace resource

Register the card JS file as a Lovelace resource with cache-busting `?v=N` query param.

## Troubleshooting

### Broken images on the display

Broken images mean a published URL is 404ing. Since the staging-verification change this should no longer be reachable through the staging path — check the AppDaemon log for:

```
PhotoFrameViewerApp: staging gen=<n> FAILED verification (reason=<deadline|watchdog>)
after <s>s (<k> checks) last_status=<n> — HA never served
'/local/photo-frame/live/<n>/<file>': <hint>
```

That line says the app correctly refused to publish the generation. **`last_status` tells you which subsystem to look at** — this is the whole point of the field, because a frozen display looks identical in both cases:

| `last_status` | Meaning | Self-heals? |
|---|---|---|
| `404` | The generation directory never appeared — the copy did not land. A staging problem. | Yes, on the next poll |
| `-1` | HA could not be reached at all (connection error or timeout). | Only if HA was merely restarting — otherwise **no**: check `ha_url` and whether HA is up |
| `301` / `302` / `401` / `403` / `405` / `5xx` | HA answered, but not with the file — `ha_url` is almost certainly wrong (scheme, host, or a proxy in front of HA). | **No** — fix the config |
| `None` | No probe result was ever observed; the verification chain stopped responding and the watchdog gave up. | Unknown |

Only `404` is a staging problem. For anything else, `.stage.log` will cheerfully report `staged 20 files` and send you hunting in the wrong place.

For a `404` (or a `None`), read the HA-side log inside the HA container:

```bash
tail -50 /config/www/photo-frame/live/.stage.log
```

- `source empty or missing` — the fetcher wrote nothing to `source_dir`.
- `lock busy, giving up` — a previous stage is still running (a long NFS stall).
- `pruned unreferenced generation <n>` — normal housekeeping, see the keep-list note above.
- No line at all for that gen — the copy is still in flight, or the shell was killed before the detached subshell started.

If instead you see `staging verification is DISABLED` at startup, the app has no `ha_url` and is publishing on the old unverified settle delay — configure `ha_url_env`.
