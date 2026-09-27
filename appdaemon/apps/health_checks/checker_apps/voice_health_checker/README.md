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
      entity_id: conversation.phone_assist_local
      reachability_url: http://llama-server.ai.svc.cluster.local:8080/health
      reachability_name: Local LLM
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
  - **ok** when an installed program for `service` holds an installed model (`asr`; detail: model name and round trip) or voices (`tts`; detail: program, voice count and round trip). Every installed program is searched, and a program, model or voice listed without an `installed` field counts as installed.
  - **critical** on a timeout, a refused or closed connection, a malformed reply, or nothing installed.
- **agent**: the agent entity must exist and not be `unavailable` (its integration is loaded).
  - With a `reachability_url`, an anonymous GET must then answer below HTTP 500. In production that is the local llama-server's `/health`. It answers 503 while a model loads, so a loading model reads **critical**, and a load that outlasts the controller's 300 s for-gate pages. A restarting pod is critical too (connection refused). For a cloud LLM, a 401 on its API proves it is up without the checker holding a key.
  - **critical** otherwise.
  - The check sees neither a slow model (a thermally throttled GPU still answers `/health`) nor, for a cloud LLM, an exhausted quota.

There is no cross-check. Each check is a separate service, and any one of them down breaks voice, so each goes **critical** on its own. A sustained critical pages after the controller's `alert_for_seconds.critical` like any checker, and it can be muted from the health card.

Uses `wyoming_check()` and `http_reachable_check()` from `shared/check_utils.py`.

## Dependencies

None in production: the local LLM needs no internet. A check that reaches a cloud LLM should declare `dependency: cloud`, so an internet outage (already paged by Cloud) shows it as `unknown` instead of paging twice.

## Manual Setup

None. No HA entities are provisioned. The AppDaemon pod reaches the servers by their cluster service names. On 2026-09-26, from the pod, a `describe` to both speech servers succeeded and llama-server `/health` answered 200 `{"status":"ok"}` in 13 ms.
