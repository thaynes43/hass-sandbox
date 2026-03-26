# Cloud Checker

## Overview

A simple internet connectivity checker that verifies the host running AppDaemon can reach the internet. Performs HTTP GET requests against one or more configurable URLs on a periodic interval.

## Configuration

```yaml
cloud_checker:
  module: health_checks.checker_apps.cloud_checker.cloud_checker
  class: CloudChecker
  checker_id: cloud
  checker_name: Cloud
  check_interval_s: 120
  force_fail: false
  checks:
    - url: https://www.google.com
      name: Google
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `checker_id` | string | `"cloud"` | Unique checker identifier |
| `checker_name` | string | `"Cloud"` | Display name in dashboard |
| `check_interval_s` | int | `120` | Seconds between check cycles |
| `check_timeout_s` | int | `5` | HTTP request timeout per URL |
| `force_fail` | bool | `false` | When true, always reports critical (for testing dependency overrides) |
| `checks` | list | `[]` | List of `{url, name}` dicts to check |

## Check Logic

Each configured URL produces one named check:
- **ok**: HTTP 2xx response received
- **critical**: Timeout, connection error, or non-2xx response

Uses `http_check()` from `shared/check_utils.py`.

## Dependencies

Cloud is a root-level dependency. Other checkers depend on it:
- `cielo` (Cielo Home) — cloud-connected HVAC
- `lock_batteries` (Schlage) — cloud-connected locks

## Manual Setup

None required. No HA entities are provisioned.
