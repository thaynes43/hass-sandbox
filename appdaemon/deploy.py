#!/usr/bin/env python3
"""
Deploy AppDaemon configs from this repo to production.

Syncs appdaemon/apps/ to the production config directory (typically X:\apps/).
Used by agents and humans.

NEVER deploys appdaemon.yaml or secrets.yaml — production injects these via
Kubernetes ExternalSecret. Local dev copies are .gitignored.

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

# Paths relative to this script
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SOURCE = SCRIPT_DIR
DEFAULT_TARGET = os.environ.get("DEPLOY_TARGET", "X:\\")

# What to copy:
# - apps/: AppDaemon app modules + apps.yaml
# - ai_providers/: shared provider code used by apps (must be importable in prod)
#
# NOTE: In production AppDaemon often only includes `/conf/apps` in sys.path.
# We therefore deploy `ai_providers/` into `apps/ai_providers/` so imports like
# `import ai_providers...` work without requiring appdaemon.yaml import_paths changes.
COPY_ITEMS = ["apps", "ai_providers"]

EXCLUDE_DIRS = {".venv", "__pycache__", ".git", ".cursor"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".swp", ".bak"}


def should_exclude(path: Path) -> bool:
    if path.name in EXCLUDE_DIRS:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def deploy(source: Path, target: Path, dry_run: bool = False) -> int:
    target = Path(target)
    source = Path(source)
    if not source.is_dir():
        print(f"ERROR: Source not found: {source}", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"ERROR: Target not found: {target}", file=sys.stderr)
        return 1

    copied = 0
    for item in COPY_ITEMS:
        src = source / item
        # Keep `ai_providers/` importable under the default prod sys.path (/conf/apps).
        dst = (target / "apps" / item) if item == "ai_providers" else (target / item)
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
    args = parser.parse_args()
    target = Path(args.target).resolve()
    source = SOURCE
    if not target.exists():
        print(f"ERROR: Target directory does not exist: {target}", file=sys.stderr)
        return 1
    return deploy(source, target, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
