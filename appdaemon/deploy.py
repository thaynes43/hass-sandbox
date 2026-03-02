#!/usr/bin/env python3
"""
Deploy AppDaemon configs from this repo to production.

Syncs appdaemon/apps/ to the production config directory (typically X:\apps/).
Used by agents and humans.

NEVER deploys appdaemon.yaml or secrets.yaml — production gets them via Flux
(ConfigMaps). Local dev copies are .gitignored.

Usage:
    python appdaemon/deploy.py
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

# What to copy:
# - apps/: AppDaemon app modules + apps-base.yaml (deployed as apps.yaml)
# - ai_providers/: shared provider code used by apps
# - ha_provisioner/: HA entity provisioning library used by apps
#
# NOTE: In production AppDaemon often only includes `/conf/apps` in sys.path.
# We therefore deploy shared libraries into `apps/<lib>/` so imports like
# `import ai_providers...` / `import ha_provisioner...` work without
# requiring appdaemon.yaml import_paths changes.
COPY_ITEMS = ["apps", "ai_providers", "ha_provisioner"]

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", ".cursor", "_state"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".swp", ".bak"}

# apps-dev.yaml is dev-only; apps-base.yaml is deployed as apps.yaml in production.
EXCLUDE_FILES = {"apps-dev.yaml"}
RENAME_FILES = {"apps-base.yaml": "apps.yaml"}


def should_exclude(path: Path) -> bool:
    if path.name in EXCLUDE_DIRS:
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def _merge_apps_yaml(
    *,
    base_path: Path,
    overlay_path: Path,
) -> str:
    """
    Merge overlay (apps-dev.yaml) onto base (apps-base.yaml) at the top-level keys.

    - New apps in overlay are added
    - Existing app configs in overlay replace base entries entirely

    Uses ruamel.yaml so custom tags like `!secret` survive round-trip.
    """
    try:
        from ruamel.yaml import YAML  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Missing dependency ruamel.yaml. Install with: pip install -r appdaemon/requirements.txt"
        ) from e

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True

    def _load(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {}
        data = yaml.load(p.read_text(encoding="utf-8"))  # noqa: S506 (local file)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"Expected mapping at YAML root: {p}")
        return data

    base = _load(base_path)
    overlay = _load(overlay_path)
    for k, v in overlay.items():
        base[k] = v

    # Dump to string
    from io import StringIO

    buf = StringIO()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.dump(base, buf)
    out = buf.getvalue()
    # Ensure trailing newline for nicer diffs
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def deploy(
    source: Path,
    target: Path,
    *,
    dry_run: bool = False,
    merge_dev_apps: bool = False,
) -> int:
    target = Path(target)
    source = Path(source)
    if not source.is_dir():
        print(f"ERROR: Source not found: {source}", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"ERROR: Target not found: {target}", file=sys.stderr)
        return 1

    merged_apps_yaml: Optional[str] = None
    if merge_dev_apps:
        base_path = source / "apps" / "apps-base.yaml"
        overlay_path = source / "apps" / "apps-dev.yaml"
        try:
            merged_apps_yaml = _merge_apps_yaml(base_path=base_path, overlay_path=overlay_path)
        except Exception as e:
            print(f"ERROR: Failed to merge apps YAML: {e!r}", file=sys.stderr)
            return 1

    copied = 0
    for item in COPY_ITEMS:
        src = source / item
        # Shared libraries (anything other than "apps") go into apps/<lib>/ so they
        # are importable under the default prod sys.path (/conf/apps).
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
                    dst_name = RENAME_FILES.get(f, f)
                    dst_file = dst_dir / dst_name

                    # Optional: deploy a merged apps.yaml without requiring manual copy/paste
                    # between apps-dev.yaml and apps-base.yaml.
                    if (
                        merged_apps_yaml is not None
                        and item == "apps"
                        and rel == Path(".")
                        and f == "apps-base.yaml"
                    ):
                        if dry_run:
                            print(f"[dry-run] {src_file} -> {dst_file} (merged with apps-dev.yaml)")
                        else:
                            dst_dir.mkdir(parents=True, exist_ok=True)
                            dst_file.write_text(merged_apps_yaml, encoding="utf-8")
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
            "Merge apps/apps-dev.yaml onto apps/apps-base.yaml at top-level keys and deploy the merged result "
            "as apps/apps.yaml (avoids manual copy/paste)."
        ),
    )
    args = parser.parse_args()
    target = Path(args.target).resolve()
    source = SOURCE
    if not target.exists():
        print(f"ERROR: Target directory does not exist: {target}", file=sys.stderr)
        return 1
    return deploy(source, target, dry_run=args.dry_run, merge_dev_apps=bool(args.merge_dev_apps))


if __name__ == "__main__":
    sys.exit(main())
