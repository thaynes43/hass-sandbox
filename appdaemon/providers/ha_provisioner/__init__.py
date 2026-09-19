"""
HA entity provisioning library for AppDaemon apps.

Shared library (NOT an AppDaemon app) that apps use on startup to
auto-create required HA entities (scripts, helpers) via the HA REST API.
"""

from .exposure_client import (
    CONVERSATION_ASSISTANT,
    AssistExposureClient,
    ExposureChange,
)
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
    "AssistExposureClient",
    "ExposureChange",
    "CONVERSATION_ASSISTANT",
    "STATUS_UNREACHABLE",
    "build_local_url",
    "local_file_exists",
    "local_file_status",
]
