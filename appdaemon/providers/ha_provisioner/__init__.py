"""
HA entity provisioning library for AppDaemon apps.

Shared library (NOT an AppDaemon app) that apps use on startup to
auto-create required HA entities (scripts, helpers) via the HA REST API.
"""

from .provisioner import HAProvisioner

__all__ = ["HAProvisioner"]
