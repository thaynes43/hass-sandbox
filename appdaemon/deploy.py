#!/usr/bin/env python3
"""
Deploy AppDaemon configs from this repo to production.

Syncs appdaemon/apps/ to the production config directory (typically X:\\apps/).
Used by agents and humans.

NEVER deploys appdaemon.yaml or secrets.yaml — production gets them via Flux
(ConfigMaps). Local dev copies are .gitignored.

Usage:
    # Promote dev apps into apps-prod.yaml (repo-only, no deploy)
    python appdaemon/deploy.py --merge-dev-apps
    python appdaemon/deploy.py --merge-dev-apps --prod-media /mnt/other/media

    # Deploy to production
    python appdaemon/deploy.py --dry-run --target X:\\
    python appdaemon/deploy.py --target X:\\
    DEPLOY_TARGET=X:\\ python appdaemon/deploy.py

Environment:
    DEPLOY_TARGET  Default production path (default: X:\\)
"""

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

# Paths relative to this script
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SOURCE = SCRIPT_DIR
DEFAULT_TARGET = os.environ.get("DEPLOY_TARGET", "X:\\")
DEFAULT_PROD_MEDIA = "/mnt/cephfs-hdd/misc/hass-media"

# What to copy:
# - apps/: AppDaemon app modules + apps-prod.yaml (processed: disable stripped, deployed as apps.yaml)
# - providers/: shared libraries (ai_providers, ha_provisioner, photo_providers)
#
# NOTE: In production AppDaemon often only includes /conf/apps in sys.path.
# We deploy providers into apps/providers/ so imports work.
COPY_ITEMS = ["apps", "providers"]

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", ".cursor", "_state"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".swp", ".bak"}

# apps-dev.yaml is dev-only, never deployed. apps-prod.yaml is processed (not raw-copied).
EXCLUDE_FILES = {"apps-dev.yaml"}
APPS_PROD_FILE = "apps-prod.yaml"
APPS_YAML_FILE = "apps.yaml"


def should_exclude(path: Path) -> bool:
    if path.name in EXCLUDE_DIRS:
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def _get_yaml():
    try:
        from ruamel.yaml import YAML  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Missing dependency ruamel.yaml. Install with: pip install -r appdaemon/requirements.txt"
        ) from e

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _load_yaml(yaml, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.load(path.read_text(encoding="utf-8"))  # noqa: S506 (local file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at YAML root: {path}")
    return data


def _replace_media_prefix(obj: Any, dev_prefix: str, prod_prefix: str = "/media") -> Any:
    """Recursively replace string values starting with dev_prefix with prod_prefix."""
    if isinstance(obj, str):
        if obj.startswith(dev_prefix):
            return prod_prefix + obj[len(dev_prefix) :]
        return obj
    if isinstance(obj, dict):
        return {k: _replace_media_prefix(v, dev_prefix, prod_prefix) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_media_prefix(v, dev_prefix, prod_prefix) for v in obj]
    return obj


def _strip_disable(data: dict[str, Any]) -> dict[str, Any]:
    """Remove disable: true from each app config."""
    result = {}
    for k, v in data.items():
        if isinstance(v, dict) and "disable" in v:
            out = dict(v)
            del out["disable"]
            result[k] = out
        else:
            result[k] = v
    return result


def _ensure_trailing_newline(s: str) -> str:
    if s and not s.endswith("\n"):
        return s + "\n"
    return s


def merge_dev_apps(
    *,
    apps_prod_path: Path,
    apps_dev_path: Path,
    prod_media: str,
) -> None:
    """
    Promote dev apps into apps-prod.yaml (repo-only).

    - Reads apps-dev.yaml and apps-prod.yaml
    - For each dev app: strips _dev suffix from key, converts dev media paths to /media,
      adds disable: true
    - Merges into apps-prod.yaml, writes back
    """
    yaml = _get_yaml()
    prod = _load_yaml(yaml, apps_prod_path)
    dev = _load_yaml(yaml, apps_dev_path)

    for dev_key, dev_config in dev.items():
        if not dev_key.endswith("_dev"):
            continue
        prod_key = dev_key[:-4]  # strip _dev
        config = dict(dev_config)
        config["disable"] = True
        config = _replace_media_prefix(config, prod_media)
        prod[prod_key] = config

    from io import StringIO

    buf = StringIO()
    yaml.dump(prod, buf)
    apps_prod_path.write_text(_ensure_trailing_newline(buf.getvalue()), encoding="utf-8")
    print(f"Updated {apps_prod_path} with promoted dev apps. Review before deploying.")


def process_apps_prod_for_deploy(apps_prod_path: Path) -> str:
    """Read apps-prod.yaml, strip disable flags, return YAML string for apps.yaml."""
    yaml = _get_yaml()
    data = _load_yaml(yaml, apps_prod_path)
    data = _strip_disable(data)

    from io import StringIO

    buf = StringIO()
    yaml.dump(data, buf)
    return _ensure_trailing_newline(buf.getvalue())


def deploy(
    source: Path,
    target: Path,
    *,
    dry_run: bool = False,
) -> int:
    target = Path(target)
    source = Path(source)
    if not source.is_dir():
        print(f"ERROR: Source not found: {source}", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"ERROR: Target directory not found: {target}", file=sys.stderr)
        return 1

    apps_prod_path = source / "apps" / APPS_PROD_FILE
    if not apps_prod_path.exists():
        print(f"ERROR: {APPS_PROD_FILE} not found: {apps_prod_path}", file=sys.stderr)
        return 1

    apps_yaml_content = process_apps_prod_for_deploy(apps_prod_path)
    target_apps_yaml = target / "apps" / APPS_YAML_FILE

    copied = 0
    for item in COPY_ITEMS:
        src = source / item
        dst = (target / item) if item == "apps" else (target / "apps" / item)
        if not src.exists():
            continue
        if src.is_dir():
            for root, dirs, files in os.walk(src, topdown=True):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if not should_exclude(root_path / d)]
                rel = root_path.relative_to(src)
                dst_dir = dst / rel
                for f in files:
                    if should_exclude(Path(f)):
                        continue
                    src_file = root_path / f
                    dst_file = dst_dir / f

                    # apps-prod.yaml -> processed apps.yaml (not raw copy)
                    if item == "apps" and rel == Path(".") and f == APPS_PROD_FILE:
                        if dry_run:
                            print(f"[dry-run] {src_file} -> {target_apps_yaml} (strip disable)")
                        else:
                            target_apps_yaml.parent.mkdir(parents=True, exist_ok=True)
                            target_apps_yaml.write_text(apps_yaml_content, encoding="utf-8")
                        copied += 1
                        continue

                    if dry_run:
                        print(f"[dry-run] {src_file} -> {dst_file}")
                    else:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst_file)
                    copied += 1
        else:
            if dry_run:
                print(f"[dry-run] {src} -> {dst}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            copied += 1

    if not dry_run and copied > 0:
        print(f"Deployed {copied} file(s) to {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy AppDaemon configs to production")
    parser.add_argument(
        "--target",
        "-t",
        default=DEFAULT_TARGET,
        help=f"Production config directory (default: {DEFAULT_TARGET} or DEPLOY_TARGET)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be copied without copying",
    )
    parser.add_argument(
        "--merge-dev-apps",
        action="store_true",
        help=(
            "Promote dev apps from apps-dev.yaml into apps-prod.yaml (repo-only). "
            "Strips _dev suffix, converts media paths to /media, adds disable: true. "
            "Does NOT deploy. Let user review apps-prod.yaml before running deploy."
        ),
    )
    parser.add_argument(
        "--prod-media",
        default=DEFAULT_PROD_MEDIA,
        help=(
            f"Dev media root path to replace with /media during --merge-dev-apps "
            f"(default: {DEFAULT_PROD_MEDIA})"
        ),
    )
    args = parser.parse_args()

    if args.merge_dev_apps:
        apps_prod_path = SOURCE / "apps" / APPS_PROD_FILE
        apps_dev_path = SOURCE / "apps" / "apps-dev.yaml"
        if not apps_prod_path.exists():
            print(f"ERROR: {APPS_PROD_FILE} not found: {apps_prod_path}", file=sys.stderr)
            return 1
        if not apps_dev_path.exists():
            print(f"ERROR: apps-dev.yaml not found: {apps_dev_path}", file=sys.stderr)
            return 1
        merge_dev_apps(
            apps_prod_path=apps_prod_path,
            apps_dev_path=apps_dev_path,
            prod_media=args.prod_media.rstrip("/"),
        )
        return 0

    target = Path(args.target).resolve()
    if not target.exists():
        print(f"ERROR: Target directory does not exist: {target}", file=sys.stderr)
        return 1
    return deploy(source=SOURCE, target=target, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
