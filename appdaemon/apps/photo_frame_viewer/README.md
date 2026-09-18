# Photo Frame Viewer

Displays rotating photos from a source directory on a Lovelace dashboard card with slideshow controls: pause/resume, interval adjustment, auto-unpause configuration, and manual navigation.

## How it works

1. On startup, provisions a relay script, an image picker (`input_select`), and a status sensor.
2. Polls a source directory (typically `immich_fetcher` output) for image files.
3. Stages images for serving via HA shell commands that atomically swap content into `/config/www/photo-frame/live/<gen>/`.
4. **Verifies** the staged generation over HTTP before using it (see below), then marks it pending.
5. Cycles through images on a configurable interval, publishing the current image URL and runtime settings via the status sensor.
6. Dashboard card reads the sensor for the current image URL with cache-busting.
7. Dashboard-managed slide interval and auto-unpause settings persist across AppDaemon restarts via `state_dir/state.json`, alongside the displayed album title and the generation counter (`next_gen`).

### Staging verification

**Invariant: a generation is never marked pending — and therefore never published — until Home Assistant is verifiably serving it.**

The staging `shell_command`'s return value is not trustworthy. Home Assistant kills every `shell_command` at a hard 60-second timeout; when the NFS source stalls mid-copy the shell dies before the atomic `mv`, so the generation directory never appears even though the service call "completed". The app used to publish on a fixed 3-second settle delay and put broken images on the wall display for 10+ minutes.

Instead, after firing the stage command the app polls the exact URL the card will load — `{ha_url}{ha_local_url_base}/{gen}/{first file}` — with an HTTP `HEAD`:

- First check after `stage_settle_delay_s`, then every `stage_verify_interval_s` until `stage_verify_timeout_s` (measured from the stage call).
- `200` means staged. Because the stage script's final step is an atomic directory `mv`, one file existing implies the whole generation exists.
- Anything else (404, connection error, timeout) means "not yet".
- On success the generation becomes pending as before. On deadline the app logs a `WARNING`, cleans up the dead generation, publishes nothing, and lets the next source poll re-stage automatically (the album title is preserved for the retry).
- While a generation is being verified, staging is latched — a new source batch waits for the current one to resolve.
- **Album titles are attributed by fingerprint, not by arrival time.** The fetcher writes every file, *then* publishes its sensor, *then* fires `immich_fetcher_batch_ready`, so the viewer's periodic poll can stage the new album's complete files a moment before the event lands. On each titled event the viewer fingerprints what is on disk and routes the title to whichever generation those files belong to: the one being staged, one already pending, the one already on screen (retitled in place, persisted, and republished without moving the image), or — if the files match nothing known — the stage about to start. Routing purely by arrival time strands the title in either direction: an early poll leaves new photos under the old album's name, and a late event retitles a generation it does not describe.
- If a generation is abandoned, its title is handed back for the automatic retry unless a newer batch has since claimed it.
- Because the deadline is only evaluated when a probe result arrives, an absolute-deadline watchdog abandons the generation and releases the latch even if the verification chain stops responding entirely. Without it a single lost callback would block every future stage until AppDaemon restarted. It fires at `stage_verify_timeout_s + stage_verify_interval_s + <probe timeout> + 5 s` — derived, not fixed, because the last probe can be scheduled as late as the timeout and then take a full probe timeout to answer. A fixed margin would pre-empt that at any large `stage_verify_interval_s` and abandon a healthy generation.

#### Generation ids are unique across reloads

AppDaemon constructs a **new app instance** on every reload — a module change, or simply an HA-websocket reconnect — and `initialize()` always re-stages. The generation counter is seeded from the generation currently *displayed*, so two instances coming up while a stage is still in flight used to hand out the same id. Observed in production: `staging gen=19` and `staging gen=22` each logged twice, seconds apart, by two instances. Because the HA-side worker is detached, the loser's cleanup can then delete a directory the winner has already verified and promoted; a reused id can also make the probe answer `200` from a leftover directory still holding the previous album.

The counter is therefore persisted as `next_gen` in `state_dir/state.json` and **advanced before the stage command is sent**, so an instance that starts mid-stage reads a value beyond anything in flight. On startup the app takes `max(id recovered from the sensor, persisted next_gen)`: the sensor wins if the state file was rolled back or restored, the state file wins if a generation is in flight that the sensor has not caught up with. A missing, unparseable, negative or absurd `next_gen` falls back to the sensor value rather than poisoning the counter. A failed state write is logged at `ERROR` and does not block staging — the degradation is "ids may collide after a reload", not a frozen slideshow.

`/local/...` is unauthenticated static content, so the probe sends **no** `Authorization` header. The HTTP call itself lives in `providers/ha_provisioner/local_file_check.py` (security policy S2).

The stage service call is issued with AppDaemon's `callback=` form so it returns immediately instead of pinning the app's worker thread for up to 60 seconds — a blocking call used to starve the slideshow tick.

If `ha_url` / `ha_url_env` is not configured, verification is impossible: the app logs a single `WARNING` at startup and falls back to the legacy behaviour (assume ready after `stage_settle_delay_s`).

## Dependencies

- `photo_frame_viewer.gen_helpers` — URL generation, fingerprinting, label building
- `providers.ha_provisioner.HAProvisioner` — HA entity provisioning
- `providers.ha_provisioner.local_file_status()` — HTTP `HEAD` probe used to verify a staged generation (plus `STATUS_UNREACHABLE`; the watchdog margin is derived from `DEFAULT_TIMEOUT_S`, imported from the `providers.ha_provisioner.local_file_check` submodule — it is not re-exported by the package)
- `providers.secrets.resolve_arg_secret()` — resolves `ha_url` / `ha_url_env` (and, through `HAProvisioner`, the token env var)

## Upstream dependencies

- `immich_fetcher` — provides the source photos in `source_dir`, and fires `immich_fetcher_batch_ready` with the album title, which the viewer attributes to a generation by source fingerprint

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
| `refresh_options_every_s` | `60` | Accepted for backward compatibility and validated (min 10), but currently **unused** — picker options are refreshed when a generation is adopted, not on a timer |
| `auto_cycle` | `true` | Auto-advance images |
| `reset_timer_on_manual_nav` | `true` | Restart timer on manual selection |
| `default_interval_s` | `10` | Slideshow interval in seconds |
| `pause_auto_resume_s` | `600` | Automatically resume after this many paused seconds (`0` disables) |
| `entity_prefix` | `wall_display` | Prefix for all provisioned entity IDs |
| `state_dir` | `/media/photo-frame-viewer/{prefix}` | Directory holding `state.json`: dashboard-managed settings (interval, auto-unpause), the displayed album title, and `next_gen` (the generation counter, which must survive reloads) |

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
    case "$gen" in *[!0-9]*) keep="";; esac;
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
      if [ -d "$dest" ]; then echo "$(date +%FT%T) gen=$gen already staged, leaving it untouched"; exit 0; fi;
      rm -rf -- "$tmp";
      mkdir -p "$tmp";
      if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
        cp -a "$src"/. "$tmp"/;
        mv "$tmp" "$dest";
        echo "$(date +%FT%T) gen=$gen staged $(ls "$dest" | wc -l) files";
        if [ -n "$keep" ]; then
          for d in "$live_root"/*/; do
            [ -d "$d" ] || continue;
            n=$(basename "$d");
            case "$n" in *[!0-9]*) continue;; esac;
            [ "$n" -lt "$gen" ] || continue;
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

**Why the keep-list prune.** Detaching creates a second leak: a generation the app abandons can still be completed by its worker minutes later — the app's `photo_frame_cleanup_gen` already ran against a directory that did not exist yet — and nothing would ever reclaim it. So every stage call carries `keep_gens`, a space-separated list of the generations the app still needs, and a successful stage deletes every other generation directory **numbered lower than the one it just staged** (which is itself always kept). That reclaims late-landing orphans on the next stage. The list alone is *not* a sufficient safety argument: `_abandon_staging` releases the app's latch while the abandoned generation's detached worker may still be alive, so two workers can be queued on the lock with different keep lists, and `flock` gives waiters no ordering. Restricting the prune to **older** generations closes that: a stale worker can never delete a generation staged after it, whatever its keep list says, so the live generation is safe for any `stage_verify_timeout_s` (not only while it exceeds the script's 150 s lock wait). The price is deliberate: a leftover from an earlier gen-counter epoch that carries a *higher* number is left alone — a harmless leak, chosen over any chance of deleting what is on screen. Non-numeric directories are never pruned, and a non-numeric `gen` prunes nothing. The worker also never destroys an existing `live/<gen>`: if the directory is already there, a twin worker for the same id staged it (an AppDaemon reload mid-stage can issue the same id twice), so it logs `already staged, leaving it untouched` and exits without copying or pruning — otherwise the twin's cleanup would wipe a generation the app had already verified and put on screen. The script refuses a non-numeric `keep` value and prunes nothing when the field is empty, so an old app paired with this script is merely a no-op, as is an old script paired with the new app (it ignores the extra variable).

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
