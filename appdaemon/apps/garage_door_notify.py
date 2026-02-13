"""
Garage RATGDO door open/close notifications.

Sends push notifications when either RATGDO garage door reports open or closed.
Ported from notify_garage_ratgdo_door_open_close_tom_iphone.yaml with configurable
doors and notify targets.
"""

import hassapi as hass


class GarageDoorNotify(hass.Hass):
    """Notify on garage door open/close via RATGDO."""

    DEFAULTS = {
        "doors": [
            "cover.ratgdov25i_4a0325_door",
            "cover.ratgdov25i_dbfa50_door",
        ],
        "notify_services": [
            "notify.mobile_app_toms_iphone_15_pro",
            "notify.mobile_app_toms_iphone_air",
            "notify.mobile_app_kellies_iphone_air",
        ],
    }

    def initialize(self):
        """Register state listeners for each door."""
        self.log(f"Namespaces: {self.list_namespaces()}")
        self.log(f"Door state: {self.get_state('cover.ratgdov25i_4a0325_door')}")
        covers = self.get_state("cover") or {}
        self.log(f"Cover count: {len(covers)}")
        self.log(f"Has entity? {'cover.ratgdov25i_4a0325_door' in covers}")

        doors = self.args.get("doors", self.DEFAULTS["doors"])
        self.log(f"Listening for open/closed on {doors}", level="INFO")
        for entity_id in doors:
            self.listen_state(self._on_door_state, entity_id, new="open")
            self.listen_state(self._on_door_state, entity_id, new="closed")

    def _on_door_state(self, entity_id, attribute, old, new, kwargs):
        """Handle door state change to open or closed."""
        self.log(f"Door state: {entity_id} {old!r} -> {new!r}", level="DEBUG")
        if not self._should_notify(old, new):
            self.log(f"Skipping notify: old={old!r} new={new!r}", level="DEBUG")
            return
        door_name = self._door_name(entity_id)
        from_display = self._from_state_display(old)
        title, message = self._build_notification(door_name, new, from_display)
        self._send_notifications(title, message)

    def _should_notify(self, old: str, new: str) -> bool:
        """Skip unknown/unavailable or no actual state change."""
        if old is None or new is None:
            return False
        if old in ("unknown", "unavailable") or new in ("unknown", "unavailable"):
            return False
        return old != new

    def _door_name(self, entity_id: str) -> str:
        """Get friendly name or fall back to entity_id."""
        name = self.get_state(entity_id, attribute="friendly_name")
        return name if name else entity_id

    def _from_state_display(self, old: str) -> str:
        """Map opening/closing to closed/open for display."""
        if old == "opening":
            return "closed"
        if old == "closing":
            return "open"
        return old if old else "unknown"

    def _build_notification(self, door_name: str, to_state: str, from_display: str) -> tuple[str, str]:
        """Build (title, message) for the notification."""
        action = "Opened" if to_state == "open" else "Closed"
        title = f"{door_name} {action}"
        message = f"{door_name} is now {to_state} (was {from_display})."
        return title, message

    def _send_notifications(self, title: str, message: str) -> None:
        """Send notification to all configured services."""
        services = self.args.get("notify_services", self.DEFAULTS["notify_services"])
        self.log(f"Sending notification: {title!r} to {len(services)} service(s)", level="INFO")
        for svc in services:
            # notify.mobile_app_xxx -> notify/mobile_app_xxx
            service = svc.replace(".", "/", 1) if "." in svc else f"notify/{svc}"
            self.call_service(service, title=title, message=message)
