"""
Garage RATGDO door open/close notifications.

Sends push notifications when either RATGDO garage door reports open or closed.
Consolidates rapid open->closed (or closed->open) into a single "was open/closed
for n minutes m seconds" notification to avoid two alerts when someone leaves
or arrives.
"""

import time

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
        "consolidation_delay": 300,  # seconds to wait for second transition
    }

    def initialize(self):
        """Register state listeners for each door."""
        self._pending = {}  # entity_id -> {state, timestamp, handle, door_name, from_display}

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

        # Check if we have a pending transition for the opposite state
        opposite = "closed" if new == "open" else "open"
        pending = self._pending.get(entity_id)
        if pending and pending["state"] == opposite:
            # Second transition within delay: consolidate
            self._cancel_pending(entity_id)
            duration_secs = time.time() - pending["timestamp"]
            title, message = self._build_consolidated_notification(
                door_name, was_open=(opposite == "open"), duration_secs=duration_secs
            )
            self._send_notifications(title, message)
            return

        # No matching pending: schedule single notification after delay
        delay = self.args.get("consolidation_delay", self.DEFAULTS["consolidation_delay"])
        handle = self.run_in(
            self._on_delay_expired,
            delay,
            entity_id=entity_id,
            new_state=new,
            door_name=door_name,
            from_display=from_display,
        )
        self._pending[entity_id] = {
            "state": new,
            "timestamp": time.time(),
            "handle": handle,
            "door_name": door_name,
            "from_display": from_display,
        }

    def _on_delay_expired(self, kwargs):
        """Called when consolidation delay expires: send single notification."""
        entity_id = kwargs["entity_id"]
        pending = self._pending.pop(entity_id, None)
        if not pending:
            return
        title, message = self._build_notification(
            pending["door_name"], pending["state"], pending["from_display"]
        )
        self._send_notifications(title, message)

    def _cancel_pending(self, entity_id: str) -> None:
        """Cancel pending timer for entity and remove from _pending."""
        pending = self._pending.pop(entity_id, None)
        if pending and "handle" in pending:
            self.cancel_timer(pending["handle"])

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

    def _format_duration(self, seconds: float) -> str:
        """Format seconds as 'n minutes and m seconds'."""
        total = int(round(seconds))
        mins, secs = divmod(total, 60)
        m = "minute" if mins == 1 else "minutes"
        s = "second" if secs == 1 else "seconds"
        return f"{mins} {m} and {secs} {s}"

    def _build_consolidated_notification(
        self, door_name: str, was_open: bool, duration_secs: float
    ) -> tuple[str, str]:
        """Build (title, message) for consolidated open->closed or closed->open."""
        duration_str = self._format_duration(duration_secs)
        if was_open:
            title = f"{door_name} Opened & Closed"
            message = f"{door_name} was open for {duration_str} before it closed."
        else:
            title = f"{door_name} Closed & Opened"
            message = f"{door_name} was closed for {duration_str} before it opened."
        return title, message

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
