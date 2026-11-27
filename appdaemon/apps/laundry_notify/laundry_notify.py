"""
Washer and dryer completion push notifications.

Thin AppDaemon wrapper around ``coordinator.LaundryCoordinator``: listens to the
washer and dryer status sensors, sends the push notifications the coordinator
asks for, and runs the wet-clothes reminder timer. See README.md for the rules.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Optional

import hassapi as hass

from laundry_notify.coordinator import (
    BOTH_COMPLETE,
    DRYER,
    DRYER_COMPLETE,
    POLICY_NOTIFY_NOW,
    WASHER,
    WASHER_COMPLETE,
    WASHER_REMINDER,
    LaundryCoordinator,
    is_active,
)


def format_duration(minutes: float) -> str:
    minutes = max(0, int(round(minutes)))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"


def build_message(kind: str, *, washer_waiting_minutes: Optional[float] = None) -> tuple[str, str]:
    """Return (title, message) for a notification kind."""
    if kind == WASHER_COMPLETE:
        return "Washer complete", "The wash is done and ready to move to the dryer."
    if kind == DRYER_COMPLETE:
        return "Dryer complete", "The dryer is done and ready to empty."
    if kind == BOTH_COMPLETE:
        return (
            "Washer & dryer complete",
            "Empty the dryer and move the wash over.",
        )
    if kind == WASHER_REMINDER:
        waited = (
            f" for {format_duration(washer_waiting_minutes)}"
            if washer_waiting_minutes is not None
            else ""
        )
        return (
            "Washer still needs switching",
            f"Wet clothes have been sitting in the washer{waited}.",
        )
    raise ValueError(f"unknown notification kind {kind!r}")


class LaundryNotify(hass.Hass):
    """Push notifications for washer/dryer completion plus a wet-clothes reminder."""

    DEFAULTS: dict[str, Any] = {
        "washer_status_entity": "sensor.washer_current_status",
        "dryer_status_entity": "sensor.dryer_current_status",
        "notify_services": [
            "notify.mobile_app_toms_iphone_15_pro",
            "notify.mobile_app_toms_iphone_air",
            "notify.mobile_app_kellies_iphone_air",
        ],
        "dryer_first_policy": POLICY_NOTIFY_NOW,
        "washer_reminder_minutes": 120,
        "notification_url": "",
        # Survives AppDaemon re-initialising the app (every HA restart): stops a
        # sent reminder being sent again. Default: <AppDaemon config dir>/...
        "state_file": "",
    }

    def initialize(self) -> None:
        self._washer_entity = str(self._arg("washer_status_entity"))
        self._dryer_entity = str(self._arg("dryer_status_entity"))
        self._reminder_minutes = float(self._arg("washer_reminder_minutes"))
        self._reminder_handle = None
        self._washer_end_time: Optional[dt.datetime] = None

        self._coord = LaundryCoordinator(dryer_first_policy=str(self._arg("dryer_first_policy")))
        washer_state = self.get_state(self._washer_entity)
        dryer_state = self.get_state(self._dryer_entity)
        self._coord.seed(washer_state, dryer_state)

        self._state_path = self._resolve_state_path()
        saved = self._load_saved()
        self._saved = saved
        if saved.get("washer_end"):
            self._washer_end_time = self._parse_time(saved.get("washer_end"))
        self._drop_hold_if_already_notified()

        washer_now = self._coord.last_state.get(WASHER)
        if washer_now is not None and washer_now != "end":
            # Washer known to be empty/running: drop any old episode. While it is
            # unavailable (HA still starting) keep the file for the next init.
            self._save({})
        elif self._coord.restore_reminder():
            self._restore_reminder(saved)

        self.listen_state(self._on_status, self._washer_entity, machine=WASHER)
        self.listen_state(self._on_status, self._dryer_entity, machine=DRYER)
        self.log(
            f"Laundry notify ready: washer={self._washer_entity}={washer_state!r} "
            f"dryer={self._dryer_entity}={dryer_state!r} "
            f"policy={self._coord.dryer_first_policy} reminder={self._reminder_minutes:g} min",
            level="INFO",
        )

    # ---------------------------------------------------------------- restart state
    def _resolve_state_path(self) -> Optional[Path]:
        configured = str(self._arg("state_file") or "").strip()
        if configured:
            return Path(configured)
        base = getattr(self, "config_dir", None)
        return Path(base) / "laundry_notify_state.json" if base else None

    def _load_saved(self) -> dict:
        if self._state_path is None or not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self.log(f"Ignoring unreadable state file {self._state_path}: {exc}", level="WARNING")
            return {}

    def _save(self, data: dict) -> None:
        self._saved = data
        if self._state_path is None:
            return
        try:
            self._state_path.write_text(json.dumps(data))
        except Exception as exc:
            self.log(f"Could not write state file {self._state_path}: {exc}", level="WARNING")

    def _update_saved(self, **changes) -> None:
        data = dict(getattr(self, "_saved", {}) or {})
        data.update(changes)
        self._save(data)

    def _drop_hold_if_already_notified(self) -> None:
        """A rebuilt washer hold is wrong if this load's push already went out.

        Only a notified load writes reminder_due / reminder_sent; a real hold
        never does. Washer in "end" + dryer running a different load after the
        push looks like a hold to seed(), so the saved episode decides.
        """
        saved = getattr(self, "_saved", {}) or {}
        if self._coord.holding_washer and ("reminder_due" in saved or "reminder_sent" in saved):
            self._coord.holding_washer = False

    def _restore_reminder(self, saved: dict) -> None:
        """Washer is sitting in "end" at startup and has been notified.

        Prefer the saved episode: a Home Assistant restart resets last_changed,
        so it can't tell how long the clothes have sat or whether the reminder
        already went out. Without a saved episode (first run, pod restart) fall
        back to last_changed and skip a reminder whose time has passed.
        """
        self._saved = saved
        if saved.get("washer_end"):
            self._washer_end_time = self._parse_time(saved.get("washer_end"))
            if saved.get("reminder_sent"):
                self._coord.reminder_armed = False
                self.log("Washer reminder already sent for this load; not re-arming")
                return
            due = self._parse_time(saved.get("reminder_due"))
            if due is not None:
                # Due while we were down and never sent: send it now.
                self._schedule_reminder(max(1.0, (due - self._now()).total_seconds()), persist=False)
                return
        ended = self._parse_time(self.get_state(self._washer_entity, attribute="last_changed"))
        self._washer_end_time = ended
        remaining = self._reminder_minutes * 60
        if ended is not None:
            remaining -= (self._now() - ended).total_seconds()
        if remaining > 0:
            self._schedule_reminder(remaining)
        else:
            self._coord.reminder_armed = False

    # ---------------------------------------------------------------- helpers
    def _arg(self, key: str) -> Any:
        return self.args.get(key, self.DEFAULTS[key])

    def _now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    @staticmethod
    def _parse_time(value: Any) -> Optional[dt.datetime]:
        if not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed

    # ---------------------------------------------------------------- callbacks
    def _on_status(self, entity, attribute, old, new, kwargs) -> None:
        machine = kwargs.get("machine")
        prev = self._coord.last_state.get(WASHER)
        actions = self._coord.update(machine, new)
        now_state = self._coord.last_state.get(WASHER)
        if machine == WASHER and now_state == "end" and is_active(prev):
            # A real finish (not an unavailable -> end blip): start a new episode.
            self._washer_end_time = self._now()
            self._save({"washer_end": self._washer_end_time.isoformat()})
        elif machine == WASHER and prev == "end" and now_state != "end":
            self._save({})
        elif machine == WASHER and prev is None and now_state == "end":
            # Washer was unavailable at startup and has come back sitting full.
            self._coord.holding_washer = is_active(self._coord.last_state.get(DRYER))
            self._drop_hold_if_already_notified()
            if self._coord.restore_reminder():
                self._restore_reminder(self._saved)
        elif machine == WASHER and prev is None and now_state is not None:
            self._save({})
        self.log(
            f"{machine} {old!r} -> {new!r}: "
            f"{[a.notification or a.kind for a in actions] or 'no action'} "
            f"(holding washer={self._coord.holding_washer} dryer={self._coord.holding_dryer})",
            level="INFO" if actions else "DEBUG",
        )
        self._apply(actions)

    def _on_reminder(self, kwargs) -> None:
        self._reminder_handle = None
        actions = self._coord.reminder_due()
        if actions:
            self._update_saved(reminder_sent=True, reminder_due=None)
        self.log(f"Washer reminder timer fired: {[a.notification for a in actions] or 'washer already switched'}")
        self._apply(actions)

    def _apply(self, actions) -> None:
        for action in actions:
            if action.kind == "notify":
                self._send(action.notification)
            elif action.kind == "arm_reminder":
                self._schedule_reminder(self._reminder_minutes * 60)
            elif action.kind == "cancel_reminder":
                self._cancel_reminder()
                self.log("Washer reminder cancelled (washer was switched)")

    def _schedule_reminder(self, seconds: float, *, persist: bool = True) -> None:
        self._cancel_reminder()
        self._reminder_handle = self.run_in(self._on_reminder, max(1, int(seconds)))
        if persist:
            due = self._now() + dt.timedelta(seconds=seconds)
            self._update_saved(reminder_due=due.isoformat(), reminder_sent=False)
        self.log(f"Washer reminder armed for {format_duration(seconds / 60)}")

    def _cancel_reminder(self) -> None:
        if self._reminder_handle is not None:
            try:
                self.cancel_timer(self._reminder_handle)
            except Exception as exc:  # timer may already have fired
                self.log(f"Washer reminder cancel ignored: {exc}", level="DEBUG")
            self._reminder_handle = None

    def _send(self, kind: str) -> None:
        waited = None
        if kind == WASHER_REMINDER and self._washer_end_time is not None:
            waited = (self._now() - self._washer_end_time).total_seconds() / 60
        title, message = build_message(kind, washer_waiting_minutes=waited)
        services = self._arg("notify_services")
        url = str(self._arg("notification_url") or "").strip()
        self.log(f"Sending {kind}: {title!r} to {len(services)} service(s)", level="INFO")
        for svc in services:
            service = svc.replace(".", "/", 1) if "." in svc else f"notify/{svc}"
            # One thread per notification group on iOS; the reminder replaces
            # the washer push instead of stacking.
            data: dict[str, Any] = {"tag": "laundry_dryer" if kind == DRYER_COMPLETE else "laundry_washer",
                                    "group": "laundry"}
            if url:
                data["url"] = url
                data["clickAction"] = url
            try:
                self.call_service(service, title=title, message=message, data=data)
            except Exception as exc:
                self.log(f"Notify via {svc} failed: {exc}", level="WARNING")
