"""Tests for NotificationManager — add/remove/prune/priority ordering/TTL."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from dashboard_notify.notification_manager import (
    Notification,
    NotificationManager,
    priority_for_class,
)


def _make_notification(
    nid: str = "test_1",
    notification_class: str = "BasicTextImage",
    text: str = "Test notification",
    created_at: float = 1000.0,
    ttl_s: float = 3600.0,
    priority: int | None = None,
    source_id: str = "test",
) -> Notification:
    if priority is None:
        priority = priority_for_class(notification_class)
    return Notification(
        id=nid,
        notification_class=notification_class,
        text=text,
        image_path=f"/media/dashboard-notify/{nid}.png",
        local_url=f"/local/dashboard-notify/{nid}.png",
        created_at=created_at,
        expires_at=created_at + ttl_s,
        priority=priority,
        source_id=source_id,
    )


class TestAdd:
    def test_add_notification(self):
        mgr = NotificationManager()
        n = _make_notification()
        mgr.add(n)
        assert mgr.count() == 1
        assert mgr.has("test_1")

    def test_add_replaces_by_id(self):
        mgr = NotificationManager()
        n1 = _make_notification(text="first")
        n2 = _make_notification(text="second")
        mgr.add(n1)
        mgr.add(n2)
        assert mgr.count() == 1
        assert mgr.get("test_1").text == "second"

    def test_add_multiple_different_ids(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="a"))
        mgr.add(_make_notification(nid="b"))
        mgr.add(_make_notification(nid="c"))
        assert mgr.count() == 3


class TestRemove:
    def test_remove_existing(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="x"))
        assert mgr.remove("x") is True
        assert mgr.count() == 0

    def test_remove_nonexistent(self):
        mgr = NotificationManager()
        assert mgr.remove("nonexistent") is False

    def test_remove_does_not_affect_others(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="a"))
        mgr.add(_make_notification(nid="b"))
        mgr.remove("a")
        assert mgr.count() == 1
        assert mgr.has("b")


class TestPruneExpired:
    def test_prune_removes_expired(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="old", created_at=100, ttl_s=100))
        mgr.add(_make_notification(nid="new", created_at=100, ttl_s=10000))
        expired = mgr.prune_expired(now=300)
        assert expired == ["old"]
        assert mgr.count() == 1
        assert mgr.has("new")

    def test_prune_no_expired(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(created_at=1000, ttl_s=3600))
        expired = mgr.prune_expired(now=1500)
        assert expired == []
        assert mgr.count() == 1

    def test_prune_all_expired(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="a", created_at=100, ttl_s=10))
        mgr.add(_make_notification(nid="b", created_at=100, ttl_s=20))
        expired = mgr.prune_expired(now=200)
        assert set(expired) == {"a", "b"}
        assert mgr.count() == 0

    def test_prune_exact_boundary(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="exact", created_at=100, ttl_s=100))
        # expires_at = 200, now = 200 → should be pruned (expired AT boundary)
        expired = mgr.prune_expired(now=200)
        assert expired == ["exact"]


class TestPriorityOrdering:
    def test_higher_priority_first(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="low", notification_class="PreexistingImage"))
        mgr.add(_make_notification(nid="high", notification_class="UrgentImage"))
        mgr.add(_make_notification(nid="mid", notification_class="BasicTextImage"))
        active = mgr.active_notifications()
        assert active[0].id == "high"
        assert active[1].id == "mid"
        assert active[2].id == "low"

    def test_same_priority_newer_first(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="older", created_at=100))
        mgr.add(_make_notification(nid="newer", created_at=200))
        active = mgr.active_notifications()
        assert active[0].id == "newer"
        assert active[1].id == "older"


class TestToSerializable:
    def test_serializable_format(self):
        mgr = NotificationManager()
        mgr.add(_make_notification(nid="test", text="Hello"))
        result = mgr.to_serializable()
        assert len(result) == 1
        assert result[0]["id"] == "test"
        assert result[0]["text"] == "Hello"
        assert "image_url" in result[0]
        assert "class" in result[0]
        assert "priority" in result[0]

    def test_empty_serializable(self):
        mgr = NotificationManager()
        assert mgr.to_serializable() == []


class TestPriorityMap:
    def test_known_classes(self):
        assert priority_for_class("UrgentImage") == 100
        assert priority_for_class("BasicTextImage") == 50
        assert priority_for_class("FunPictureImage") == 25
        assert priority_for_class("PreexistingImage") == 10

    def test_unknown_class_defaults_to_50(self):
        assert priority_for_class("SomethingNew") == 50
