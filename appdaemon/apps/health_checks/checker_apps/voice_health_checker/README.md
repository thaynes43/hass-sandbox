# Voice Health Checker

## Overview

Checks whether the voice assistant stack actually works: the speech servers and the LLM agent behind the Assist pipelines. Home Assistant cannot tell. Its Wyoming `stt.*`/`tts.*` entities and its `conversation.*` agents stay "available" while the servers behind them are down, and only go unavailable for a moment when an entry reloads.

The Tom Mobile **Phone Assist** card reads this checker's status for its icon (`agent-docs/tom-mobile-dashboard.md`). The same speech servers serve the Kitchen and Rumpus Room voice boxes, so a failure here breaks those too.

## Configuration

```yaml
voice_health_checker:
  module: health_checks.checker_apps.voice_health_checker.voice_health_checker
  class: VoiceHealthChecker
  checker_id: voice
  checker_name: Voice
  check_interval_s: 120
  check_timeout_s: 5
  checks:
    - name: Speech to Text
      type: wyoming
      service: asr
      host: whisper.ai.svc.cluster.local
      port: 10300
    - name: Text to Speech
      type: wyoming
      service: tts
      host: kokoro.ai.svc.cluster.local
      port: 10210
    - name: Assistant
      type: agent
      entity_id: conversation.phone_assist
      reachability_url: https://api.openai.com/v1/models
      reachability_name: OpenAI
      dependency: cloud
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `checker_id` | string | `"voice"` | Unique checker identifier |
| `checker_name` | string | `"Voice"` | Display name in dashboard |
| `check_interval_s` | int | `120` | Seconds between check cycles |
| `check_timeout_s` | int | `5` | Timeout per probe |
| `checks` | list | `[]` | Named checks, see below. A malformed entry is logged and skipped |

Each entry in `checks` has a `name`, a `type`, and optionally a `dependency` (a checker id whose outage masks this check as `unknown`):

- **`type: wyoming`**: `service` (`asr` = speech-to-text, `tts` = text-to-speech), `host`, `port`.
- **`type: agent`**: `entity_id` (the conversation agent), optional `reachability_url` and `reachability_name` (the LLM API and how its detail line names it).

## Check Logic

- **wyoming**: sends a Wyoming `describe` event, the same handshake HA's Wyoming integration uses, and reads the `info` reply.
  - **ok** when it lists an installed program for `service`: for `asr`, one with an installed model (detail: model name and round trip); for `tts`, one with voices (detail: program, voice count and round trip).
  - **critical** on a timeout, a refused or closed connection, a malformed reply, or nothing installed.
- **agent**: the agent entity must exist and not be `unavailable` (its integration is loaded).
  - With a `reachability_url`, an anonymous GET must then answer below HTTP 500. A 401 proves the API is up and reachable without the checker holding its key.
  - **critical** otherwise.
  - The check cannot see an exhausted quota or a revoked key: that would need a real, billed request on every cycle.

There is no cross-check. Each check is a separate service, and any one of them down breaks voice, so each goes **critical** on its own. A sustained critical pages after the controller's `alert_for_seconds.critical` like any checker, and it can be muted from the health card.

Uses `wyoming_check()` and `http_reachable_check()` from `shared/check_utils.py`.

## Dependencies

- `cloud`: the **Assistant** check declares it, so an internet outage (already paged by Cloud) shows the agent as `unknown` instead of paging twice.

## Manual Setup

None. No HA entities are provisioned. The AppDaemon pod reaches the speech servers by their cluster service names: a `describe` from the pod to both servers, and a GET to the OpenAI API (401), all succeeded on 2026-09-26.
