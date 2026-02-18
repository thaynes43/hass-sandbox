"""Integration-ish test: send a real push notification to Tom's iPhone Air.

This is intentionally skipped unless you explicitly opt-in with env vars,
since it causes a real side effect (sending a push).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _safe_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@pytest.mark.skipif(
    not _env("DS_SEND_PUSH_CONFIRM")
    or not _env("DS_TEST_RUN_ID"),
    reason="requires DS_SEND_PUSH_CONFIRM and DS_TEST_RUN_ID (opt-in real push)",
)
def test_send_push_notification_to_toms_iphone_air() -> None:
    # Ensure `appdaemon/` is on sys.path so we can reuse the payload builder.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.send_test_push_notification import (  # noqa: E402
        build_mobile_app_data,
        resolve_ha_base_url_and_token,
    )

    base, token = resolve_ha_base_url_and_token(
        ha_base_url=_env("HA_BASE_URL"),
        ha_token=_env("HA_TOKEN"),
        appdaemon_yaml=_env("APPDAEMON_YAML") or None,
        secrets_yaml=_env("APPDAEMON_SECRETS_YAML") or None,
    )
    base = str(base).rstrip("/")
    run_id = _env("DS_TEST_RUN_ID")
    dashboard_path = _env("DS_DASHBOARD_PATH") or "/garage-notify/summary"

    # Side-effect: sends real push notification
    url = f"{base}/api/services/notify/mobile_app_toms_iphone_air"
    body = {
        "title": _env("DS_TEST_TITLE") or "Garage Notify Test (manual)",
        "message": _env("DS_TEST_MESSAGE") or "Tap Open to select the run on the dashboard.",
        "data": build_mobile_app_data(dashboard_path=dashboard_path, run_id=run_id),
    }
    req = urllib.request.Request(
        url=url,
        method="POST",
        data=_safe_json(body),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            assert 200 <= int(resp.status) < 300
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise AssertionError(f"HA notify failed: {e.code} {e.reason}; {detail}") from e

