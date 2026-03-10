"""Notification lifecycle manager for dashboard_notify.

Manages a pool of active notifications with TTL-based expiry, priority
ordering, and add/remove/prune operations.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Notification:
    """A single dashboard notification."""

    id: str
    notification_class: str  # "PreexistingImage", "BasicTextImage", "FunPictureImage"
    text: str
    image_path: str  # filesystem path (e.g. /media/dashboard-notify/...)
    local_url: str  # HA URL for card (e.g. /local/dashboard-notify/...)
    created_at: float  # epoch
    expires_at: float  # epoch (created_at + ttl_s)
    priority: int  # class-based (Urgent=100, Basic=50, Fun=25, Preexisting=10)
    source_id: str  # config notification id or bundle_key


PRIORITY_MAP: dict[str, int] = {
    "UrgentImage": 100,
    "BasicTextImage": 50,
    "FunPictureImage": 25,
    "PreexistingImage": 10,
}


def priority_for_class(notification_class: str) -> int:
    """Return priority value for a notification class."""
    return PRIORITY_MAP.get(notification_class, 50)


class NotificationManager:
    """Manages a pool of active notifications."""

    def __init__(self) -> None:
        self._notifications: dict[str, Notification] = {}

    def add(self, notification: Notification) -> None:
        """Add or replace a notification by id."""
        self._notifications[notification.id] = notification

    def remove(self, notification_id: str) -> bool:
        """Remove a notification by id. Returns True if it existed."""
        return self._notifications.pop(notification_id, None) is not None

    def get(self, notification_id: str) -> Notification | None:
        """Get a notification by id."""
        return self._notifications.get(notification_id)

    def has(self, notification_id: str) -> bool:
        """Check if a notification exists."""
        return notification_id in self._notifications

    def prune_expired(self, now: float | None = None) -> list[str]:
        """Remove expired notifications. Returns list of removed ids."""
        if now is None:
            now = time.time()
        expired = [
            nid for nid, n in self._notifications.items()
            if n.expires_at <= now
        ]
        for nid in expired:
            del self._notifications[nid]
        return expired

    def active_notifications(self) -> list[Notification]:
        """Return active notifications sorted by priority desc, then created_at desc."""
        return sorted(
            self._notifications.values(),
            key=lambda n: (n.priority, n.created_at),
            reverse=True,
        )

    def count(self) -> int:
        """Return count of active notifications."""
        return len(self._notifications)

    def to_serializable(self) -> list[dict[str, Any]]:
        """Convert active notifications to serializable list for sensor attributes."""
        return [
            {
                "id": n.id,
                "text": n.text,
                "image_url": n.local_url,
                "class": n.notification_class,
                "priority": n.priority,
                "source_id": n.source_id,
                "expires_at": n.expires_at,
            }
            for n in self.active_notifications()
        ]
