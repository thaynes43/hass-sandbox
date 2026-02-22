from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


ROOT = Path(r"d:\labspace\hass-sandbox")
APPDAEMON_YAML = ROOT / "appdaemon" / "appdaemon.yaml"
SECRETS_YAML = ROOT / "appdaemon" / "secrets.yaml"


def _load_yaml_safe(path: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)
    if not isinstance(data, dict):
        raise TypeError(f"Expected mapping in {path}")
    return data


def _ha_url() -> str:
    # appdaemon.yaml contains tags like "!secret", so parse ha_url as plain text.
    text = APPDAEMON_YAML.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    m = re.search(r"(?m)^\s*ha_url:\s*(\S+)\s*$", text)
    if not m:
        raise RuntimeError(f"Could not find ha_url in {APPDAEMON_YAML}")
    return m.group(1).rstrip("/")


def _token() -> str:
    secrets = _load_yaml_safe(SECRETS_YAML)
    token = secrets.get("token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(f"Missing token in {SECRETS_YAML}")
    return token


def _req_json(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        _ha_url() + path,
        method=method,
        data=data,
        headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 204:
                return None
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"{method} {path} failed: {e.code} {e.reason} {body[:400]}") from e


def _transform_triggers(triggers: Any) -> Any:
    if not isinstance(triggers, list):
        return triggers
    out: list[Any] = []
    for t in triggers:
        if isinstance(t, dict) and "platform" in t and "trigger" not in t:
            t = dict(t)
            t["trigger"] = t.pop("platform")
        out.append(t)
    return out


def _transform_actions(node: Any) -> Any:
    if isinstance(node, list):
        return [_transform_actions(x) for x in node]
    if not isinstance(node, dict):
        return node

    out = {k: _transform_actions(v) for k, v in node.items()}

    if "service" in out and "action" not in out:
        out["action"] = out.pop("service")
    if "data_template" in out and "data" not in out:
        out["data"] = out.pop("data_template")
    if "wait_for_trigger" in out:
        wft = out["wait_for_trigger"]
        if isinstance(wft, list):
            out["wait_for_trigger"] = _transform_triggers(wft)

    return out


def _transform_automation_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    if "trigger" in out and "triggers" not in out:
        out["triggers"] = _transform_triggers(out.pop("trigger"))
    if "condition" in out and "conditions" not in out:
        out["conditions"] = out.pop("condition")
    if "action" in out and "actions" not in out:
        out["actions"] = _transform_actions(out.pop("action"))

    if "triggers" in out:
        out["triggers"] = _transform_triggers(out["triggers"])
    if "actions" in out:
        out["actions"] = _transform_actions(out["actions"])
    if "conditions" not in out:
        out["conditions"] = []
    return out


def _automation_config(entity_id: str) -> dict[str, Any]:
    st = _req_json("GET", f"/api/states/{entity_id}")
    attrs = st.get("attributes") or {}
    cfg_id = attrs.get("id")

    if isinstance(cfg_id, str) and cfg_id:
        # Newer HA versions support direct lookup by config id.
        try:
            cfg = _req_json("GET", f"/api/config/automation/config/{cfg_id}")
            if isinstance(cfg, dict):
                return _transform_automation_config(cfg)
        except Exception:
            pass

    cfg_list = _req_json("GET", "/api/config/automation/config")
    if isinstance(cfg_list, list) and isinstance(cfg_id, str) and cfg_id:
        for item in cfg_list:
            if isinstance(item, dict) and item.get("id") == cfg_id:
                return _transform_automation_config(item)

    raise RuntimeError(f"Could not find config for {entity_id} (attributes.id={cfg_id!r})")


def _automation_config_id(entity_id: str) -> str:
    st = _req_json("GET", f"/api/states/{entity_id}")
    cfg_id = (st.get("attributes") or {}).get("id")
    if not isinstance(cfg_id, str) or not cfg_id.strip():
        raise RuntimeError(f"Automation has no attributes.id: {entity_id}")
    return cfg_id


def _load_yaml_any(path: Path) -> Any:
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as f:
        return yaml.load(f)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pann = sub.add_parser("announce", help="Call assist_satellite.announce")
    pann.add_argument("entity_id")
    pann.add_argument("message")
    pann.add_argument("--preannounce", action=argparse.BooleanOptionalAction, default=True)

    pexp = sub.add_parser("export-automation", help="Print automation YAML to stdout")
    pexp.add_argument("entity_id")

    psvc = sub.add_parser("services", help="Print service schema for a domain")
    psvc.add_argument("domain")

    pupd = sub.add_parser("update-automation", help="Update automation config from a YAML file")
    pupd.add_argument("entity_id")
    pupd.add_argument("yaml_path")

    ptrig = sub.add_parser("trigger-automation", help="Trigger an automation now")
    ptrig.add_argument("entity_id")

    args = p.parse_args(argv[1:])

    if args.cmd == "announce":
        res = _req_json(
            "POST",
            "/api/services/assist_satellite/announce",
            {"entity_id": args.entity_id, "message": args.message, "preannounce": bool(args.preannounce)},
        )
        # Service responses can be large; keep it short.
        print(json.dumps(res, indent=2, sort_keys=True)[:2000])
        return 0

    if args.cmd == "export-automation":
        cfg = _automation_config(args.entity_id)
        yaml = YAML()
        yaml.default_flow_style = False
        yaml.indent(mapping=2, sequence=4, offset=2)
        yaml.dump(cfg, sys.stdout)
        return 0

    if args.cmd == "services":
        data = _req_json("GET", "/api/services")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("domain") == args.domain:
                    print(json.dumps(item, indent=2, sort_keys=True))
                    return 0
        raise RuntimeError(f"Domain not found: {args.domain}")

    if args.cmd == "update-automation":
        cfg_id = _automation_config_id(args.entity_id)
        cfg = _load_yaml_any(Path(args.yaml_path))
        if not isinstance(cfg, dict):
            raise RuntimeError(f"Expected mapping YAML in {args.yaml_path}")

        # UI/editor accepts the automation config object (id/alias/triggers/actions/etc).
        # Try the most specific endpoint first; fall back to list endpoint if needed.
        try:
            res = _req_json("POST", f"/api/config/automation/config/{cfg_id}", cfg)
        except Exception:
            res = _req_json("POST", "/api/config/automation/config", cfg)
        print(json.dumps(res, indent=2, sort_keys=True)[:2000])
        return 0

    if args.cmd == "trigger-automation":
        res = _req_json("POST", "/api/services/automation/trigger", {"entity_id": args.entity_id})
        print(json.dumps(res, indent=2, sort_keys=True)[:2000])
        return 0

    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

