# ImageGen Health Checker

Page-only watchdog for the ComfyUI image-generation service. ComfyUI going down — or its queue freezing mid-job — is the canonical symptom of the GPU falling off the PCI bus on the Proxmox host, a failure only a **host reboot** fixes. There is nothing safe to automate, so this checker has no repair support: it detects, escalates, and pages.

## Checks

Both checks come from a single lightweight poll of ComfyUI's `GET /prompt` endpoint (`exec_info.queue_remaining`) via `ComfyUIStatusClient`:

| Check | Method | Healthy When |
|-------|--------|-------------|
| API Reachable | HTTP GET `{comfyui_url}/prompt` | Endpoint responds with a valid payload |
| Queue Progress | Track `queue_remaining` across polls | Queue empty, or a job completed (counter decreased) within `queue_stuck_after_s` |

### API Reachable

- **ok** — endpoint responding (detail includes current `queue_remaining`)
- **warning** — unreachable, but for less than `unreachable_after_s` (default 15m); brief restarts and pod reschedules don't page
- **critical** — unreachable beyond the threshold — likely GPU/host failure; needs human attention

While the endpoint is unreachable, Queue Progress reports **unknown** (no data to judge).

### Queue Progress

The wedge signature is a non-zero `queue_remaining` with **no job completing**. Only a *decrease* (a job finished) or an empty queue counts as progress — increases are producer activity, and during a GPU wedge the detection pipeline keeps submitting, so the counter *climbs* while nothing executes. Treating any change as progress would mask exactly the failure this checker exists to catch.

- **ok** — queue empty, or a job completed (counter decreased) within `queue_stuck_after_s`
- **critical** — non-zero queue with no completion for more than `queue_stuck_after_s` (default 30m), counter flat *or climbing*

### Cold-start nuance

The first generation after a ComfyUI restart takes ~8.5 minutes (model load) — far longer than a normal job, but it *completes*, so the queue counter moves well inside the 30-minute threshold. The stuck detector therefore cannot false-positive on cold starts; don't be tempted to lower `queue_stuck_after_s` below the cold-start time plus a healthy margin.

### In-memory queue semantics

ComfyUI's queue is held in memory only. A ComfyUI restart resets `queue_remaining` to 0, which this checker simply reads as healthy — queued jobs that were lost in the restart are the *submitters'* problem (providers time out and retry), not a health-check signal. The flip side: a stuck queue observed here is real back-pressure on the live process, not stale persisted state.

### Restart seeding

Detection clocks live in memory, but image deploys roll the AppDaemon pod hands-off. On startup the checker reads its own previous state from `sensor.health_check_status` (which survives the restart): a pre-restart `warning`/`critical` API check re-seeds the unreachable clock, and a pre-restart stuck-queue critical re-arms the stuck clock (clearing only on a genuine decrease or an empty queue). Without this, a restart mid-outage would stop re-posting the firing alert — a false `[RESOLVED]` after Alertmanager's `resolve_timeout` — and restart the 15/30-minute clocks from zero.

## Why No Auto-Heal

When the GPU drops off the PCI bus, QEMU asserts on the passthrough device — restarting the ComfyUI pod or even the VM does not recover it; only a Proxmox **host** reboot does. An automated "repair" would either do nothing (pod restart) or be far too destructive to automate (host reboot, taking every guest down). The checker registers with `supports_repair: false` and relies on the page.

## Self-Provisioned Entities

None — with no auto-repair there are no helpers to provision, and no HA token is required (`ha_url`/`ha_token_env` are not used).

## Configuration Reference

```yaml
imagegen_health_checker:
  module: health_checks.checker_apps.imagegen_health_checker.imagegen_health_checker
  class: ImageGenHealthChecker
  checker_id: imagegen                                     # Unique ID (default: imagegen)
  checker_name: Image Gen                                  # Display name on cards (default: Image Gen)
  comfyui_url: http://comfyui.ai.svc.cluster.local:8188    # Required — in-cluster service URL, not a secret (no default)
  check_interval_s: 120                                    # Poll frequency (default: 120)
  request_timeout_s: 10                                    # Per-request HTTP timeout (default: 10)
  queue_stuck_after_s: 1800                                # Stuck-queue threshold (default: 1800 = 30m)
  unreachable_after_s: 900                                 # Unreachable warning→critical escalation (default: 900 = 15m)
  alerting:
    alertname: ImageGenQueueStuck                          # Default would be ImageGenUnhealthy
```

If `comfyui_url` is missing, API Reachable reports **critical** (`comfyui_url not configured`) and Queue Progress reports **unknown**.

## Alerting

The `alerting` block is passed through to the controller at registration and consumed by its Alertmanager bridge (`shared/alertmanager_bridge.py`):

- Checker **critical** (stuck queue, or unreachable past threshold) → one `ImageGenQueueStuck` alert with `severity=critical` — the cluster's Alertmanager routes only critical to Pushover, so this **pages the phone**
- The early-unreachable **warning** phase maps to `severity=warning` — visible in Alertmanager/Grafana, no page
- On recovery the bridge posts `endsAt=now` for an immediate `[RESOLVED]` notification

## Dependencies

- `providers/ai_providers/comfyui` — `ComfyUIStatusClient` for the `GET /prompt` queue poll (read-only; independent of the image-generation provider)
- `health_check_controller` — registration/status via HA events (never `get_app`); Alertmanager mirroring lives in the controller, not here
