"""
Prometheus Alertmanager client library for AppDaemon apps.

Shared library (NOT an AppDaemon app) used by the health-check
controller to raise and resolve alerts in the cluster's Alertmanager.
"""

from .alertmanager_client import AlertmanagerClient, AlertmanagerError

__all__ = ["AlertmanagerClient", "AlertmanagerError"]
