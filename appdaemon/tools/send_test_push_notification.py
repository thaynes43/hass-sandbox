from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _safe_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _default_appdaemon_config_paths() -> tuple[Path, Path]:
    # repo/appdaemon/tools/send_test_push_notification.py -> repo/appdaemon/appdaemon.yaml
    appdaemon_dir = Path(__file__).resolve().parents[1]
    return appdaemon_dir / "appdaemon.yaml", appdaemon_dir / "secrets.yaml"


def _is_secret_ref(val: object) -> bool:
    tag = getattr(val, "tag", None)
    tag_value = getattr(tag, "value", None)
    return tag_value == "!secret"


def resolve_ha_base_url_and_token(
    *,
    ha_base_url: str = "",
    ha_token: str = "",
    appdaemon_yaml: str | None = None,
    secrets_yaml: str | None = None,
) -> tuple[str, str]:
    """
    Resolve Home Assistant base URL + token.

    Priority:
    1) Explicit args / env overrides
    2) appdaemon.yaml (plugins.HASS.ha_url + token: !secret <key>)
    3) secrets.yaml (lookup secret key)
    """
    base = (ha_base_url or "").strip() or _env("HA_BASE_URL")
    tok = (ha_token or "").strip() or _env("HA_TOKEN")
    if base and tok:
        return base, tok

    cfg_path, sec_path = _default_appdaemon_config_paths()
    if appdaemon_yaml:
        cfg_path = Path(appdaemon_yaml)
    if secrets_yaml:
        sec_path = Path(secrets_yaml)

    if not cfg_path.exists():
        raise RuntimeError(f"appdaemon config not found: {cfg_path}")

    cfg_text = cfg_path.read_text(encoding="utf-8")

    # Minimal extraction: we only care about HASS plugin ha_url + token.
    # We intentionally avoid full YAML parsing to keep this tool dependency-free.
    ha_url = ""
    token_raw: str = ""
    token_secret_key: str = ""

    # Find ha_url anywhere (scoped enough for our config style).
    m = re.search(r"(?m)^\s*ha_url:\s*(.+?)\s*$", cfg_text)
    if m:
        ha_url = m.group(1).strip().strip("'\"")

    # token can be `token: !secret token` or `token: "<value>"`
    m = re.search(r"(?m)^\s*token:\s*!secret\s+([A-Za-z0-9_]+)\s*$", cfg_text)
    if m:
        token_secret_key = m.group(1).strip()
    else:
        m = re.search(r"(?m)^\s*token:\s*(.+?)\s*$", cfg_text)
        if m:
            token_raw = m.group(1).strip().strip("'\"")

    if not base:
        base = ha_url
    if not tok:
        if token_secret_key:
            if not sec_path.exists():
                raise RuntimeError(f"secrets file not found: {sec_path} (needed for !secret {token_secret_key})")
            sec_text = sec_path.read_text(encoding="utf-8")
            # Minimal secrets parsing: `key: "value"` / `key: value`
            pat = rf"(?m)^\s*{re.escape(token_secret_key)}\s*:\s*(.+?)\s*$"
            mm = re.search(pat, sec_text)
            if mm:
                tok = mm.group(1).strip().strip("'\"")
        elif token_raw:
            tok = token_raw

    if not base:
        raise RuntimeError(
            "Could not resolve HA base url (set HA_BASE_URL or configure appdaemon.yaml plugins.HASS.ha_url)"
        )
    if not tok:
        raise RuntimeError(
            "Could not resolve HA token (set HA_TOKEN or configure appdaemon.yaml plugins.HASS.token + secrets.yaml)"
        )
    return base, tok


def build_mobile_app_data(*, dashboard_path: str, run_id: str) -> dict:
    dashboard_path = (dashboard_path or "").strip() or "/garage-notify/summary"
    rid = (run_id or "").strip()
    data: dict = {
        # Body tap deep-links (iOS uses url, Android uses clickAction)
        "url": dashboard_path,
        "clickAction": dashboard_path,
    }
    # iOS note: actionable notification buttons require long-press to reveal on iPhone.
    # Keep the default payload "tap-to-open" only.
    return data


def call_ha_notify(
    *,
    ha_base_url: str,
    ha_token: str,
    notify_service: str,
    title: str,
    message: str,
    data: dict,
) -> dict:
    base = ha_base_url.rstrip("/")
    svc = notify_service.strip()
    if svc.startswith("notify."):
        svc = svc.split(".", 1)[1]
    if "/" in svc:
        # allow passing notify/mobile_app_xxx too
        svc = svc.split("/", 1)[1]
    if not svc:
        raise ValueError("notify_service is required (e.g. notify.mobile_app_toms_iphone_air)")

    url = f"{base}/api/services/notify/{svc}"
    body = {"title": title, "message": message, "data": data or {}}
    req = urllib.request.Request(
        url=url,
        method="POST",
        data=_safe_json(body),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ha_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw) if raw else {"status": "ok"}
            except Exception:
                return {"status": "ok", "raw": raw}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HA notify failed: {e.code} {e.reason}; {detail}") from e


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Send a test push notification via Home Assistant.")
    p.add_argument("--ha-base-url", default=_env("HA_BASE_URL"), help="Override HA base url (else read from appdaemon.yaml)")
    p.add_argument("--ha-token", default=_env("HA_TOKEN"), help="Override HA token (else read from secrets.yaml)")
    p.add_argument("--appdaemon-yaml", default=_env("APPDAEMON_YAML"), help="Path to appdaemon.yaml (default: ../appdaemon.yaml)")
    p.add_argument(
        "--secrets-yaml",
        default=_env("APPDAEMON_SECRETS_YAML"),
        help="Path to secrets.yaml (default: ../secrets.yaml)",
    )
    p.add_argument("--notify-service", default=_env("HA_NOTIFY_SERVICE", "notify.mobile_app_toms_iphone_air"))
    p.add_argument("--dashboard-path", default=_env("DS_DASHBOARD_PATH", "/garage-notify/summary"))
    p.add_argument("--run-id", default=_env("DS_TEST_RUN_ID"))
    p.add_argument("--title", default=_env("DS_TEST_TITLE", "Garage Notify Test"))
    p.add_argument("--message", default=_env("DS_TEST_MESSAGE", "Tap Open to select the run on the dashboard."))
    args = p.parse_args(argv)

    try:
        ha_base_url, ha_token = resolve_ha_base_url_and_token(
            ha_base_url=args.ha_base_url,
            ha_token=args.ha_token,
            appdaemon_yaml=args.appdaemon_yaml or None,
            secrets_yaml=args.secrets_yaml or None,
        )
    except Exception as e:
        print(f"Failed to resolve Home Assistant config: {e}", file=sys.stderr)
        return 2

    data = build_mobile_app_data(dashboard_path=args.dashboard_path, run_id=args.run_id)
    res = call_ha_notify(
        ha_base_url=ha_base_url,
        ha_token=ha_token,
        notify_service=args.notify_service,
        title=args.title,
        message=args.message,
        data=data,
    )
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

