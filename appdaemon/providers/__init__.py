"""
Shared provider packages for AppDaemon apps.

Contains ai_providers, ha_provisioner, photo_providers.
"""

from pathlib import Path

# Load .env before any provider code runs (dev). Prod uses K8s envFrom.
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parents[1] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv optional if env vars set by K8s
