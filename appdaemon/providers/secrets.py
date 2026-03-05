"""Secret resolution from environment variables.

In production, env vars are injected by Kubernetes (envFrom: secretRef).
In dev, python-dotenv loads them from appdaemon/.env (gitignored).
"""

from __future__ import annotations

import os


def resolve_secret(env_var_name: str) -> str:
    """Resolve a secret from an environment variable.

    In production, env vars are injected by Kubernetes ExternalSecret.
    In dev, python-dotenv loads them from appdaemon/.env (gitignored).
    """
    value = os.environ.get(env_var_name, "")
    if not value:
        raise ValueError(
            f"Required secret env var '{env_var_name}' is not set. "
            "Set it in your .env file or environment."
        )
    return value
