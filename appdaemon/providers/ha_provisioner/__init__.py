"""
HA entity provisioning library for AppDaemon apps.

Shared library (NOT an AppDaemon app) that apps use on startup to
auto-create required HA entities (scripts, helpers) via the HA REST API.
"""

from .ha_admin_client import HaAdminClient
from .local_file_check import (
    STATUS_UNREACHABLE,
    build_local_url,
    local_file_exists,
    local_file_status,
)
from .provisioner import HAProvisioner

__all__ = [
    "HAProvisioner",
    "HaAdminClient",
    "STATUS_UNREACHABLE",
    "build_local_url",
    "local_file_exists",
    "local_file_status",
]
