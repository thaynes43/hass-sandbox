"""
Shared provider packages for AppDaemon apps.

Contains ai_providers, ha_provisioner, photo_providers.
"""

from pathlib import Path

# Load .env before any provider code runs (dev). Prod uses K8s envFrom.
try:
    from dotenv import load_dotenv

    _providers_dir = Path(__file__).resolve().parent
    # appdaemon/.env (next to providers/) or repo-root .env (two levels up)
    for _candidate in [_providers_dir.parent / ".env", _providers_dir.parents[1] / ".env"]:
        if _candidate.exists():
            load_dotenv(_candidate)
            break
except ImportError:
    pass  # python-dotenv optional if env vars set by K8s
