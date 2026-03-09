"""
Generic door open/close notifications.

Sends push notifications when a configured door entity reports open or closed.
Aggregates all open/close events within the consolidation window into a single
notification to avoid duplicate alerts when a door opens and closes multiple times
before a bundle is generated.

- 1 event: "Garage Door Opened" / "Garage Door Closed"
- 2 events: "Garage Door Opened & Closed" (was open for N seconds)
- 3+ events: "In the last N minutes, the Garage Door was opened and closed twice"

Each new state change cancels the pending timer and restarts it, so only one
notification fires per inactivity window.

Supports both cover entities (open/closed states) and binary_sensor entities
(on/off states) via config-driven state mapping (door_open_state / door_closed_state).
"""

import threading
import time
from typing import Any, Optional

import hassapi as hass

from detection_summary_store import STORE as DETECTION_SUMMARY_STORE


class DoorNotify(hass.Hass):
    """Notify on door open/close via configurable entity type."""

    DEFAULTS = {
        "doors": [],  # must be configured per-instance; no default door entities
        "notify_services": [
            "notify.mobile_app_toms_iphone_15_pro",
            "notify.mobile_app_toms_iphone_air",
            "notify.mobile_app_kellies_iphone_air",
        ],
        "consolidation_delay": 60,  # seconds to wait for second transition
        # State values for this door entity type.
        # Covers use "open"/"closed" (default); binary_sensors use "on"/"off".
        "door_open_state": "open",
        "door_closed_state": "closed",
        # Detection summary attachment (optional)
        "ai_enabled": False,
        "ai_bundle_key": "garage",
        "ai_wait_timeout_s": 30,
        "ai_max_bundle_age_s": 120,
        "ai_window_pad_s": 5,
        # Event-driven coordination with DetectionSummary (preferred).
        "ai_use_detection_summary_events": True,
        "ai_run_started_lookback_s": 900,
        # Mobile deep-link when tapping the notification.
        # For custom dashboards: /<dashboard_url_path>/<view_path>
        # Example: /detection-summary/garage
        "notification_url": "/detection-summary/garage",
    }

    def initialize(self):
        """Register state listeners for each door."""
        self._pending = {}  # entity_id -> {state, timestamp, handle, door_name, from_display}
        self._latest_run_started: dict[str, dict[str, Any]] = {}
        self._run_started_signal: dict[str, threading.Event] = {}

        # State values for this door entity type (configurable for cover vs binary_sensor).
        self._open_state = str(self.args.get("door_open_state", self.DEFAULTS["door_open_state"]))
        self._closed_state = str(self.args.get("door_closed_state", self.DEFAULTS["door_closed_state"]))

        # Intermediate state map for _from_state_display.
        # Cover entities have "opening" -> "closed" and "closing" -> "open" transitions.
        # Binary sensors have no intermediate states.
        if self._open_state == "open" and self._closed_state == "closed":
            default_intermediate_map: dict[str, str] = {"opening": "closed", "closing": "open"}
        else:
            default_intermediate_map = {}
        self._intermediate_state_map: dict[str, str] = self.args.get(
            "intermediate_state_map", default_intermediate_map
        )

        doors = self.args.get("doors", self.DEFAULTS["doors"])
        self.log(
            f"Listening for {self._open_state!r}/{self._closed_state!r} on {doors}",
            level="INFO",
        )
        for entity_id in doors:
            self.log(f"Door entity: {entity_id} state={self.get_state(entity_id)!r}", level="DEBUG")
            self.listen_state(self._on_door_state, entity_id, new=self._open_state)
            self.listen_state(self._on_door_state, entity_id, new=self._closed_state)

        # Subscribe to detection-summary events so we can wait by run_id rather than time windows.
        if self._ai_enabled() and self._ai_use_events():
            self.listen_event(self._on_detection_summary_run_started, "detection_summary/run_started")

    def _ai_use_events(self) -> bool:
        return bool(
            self.args.get(
                "ai_use_detection_summary_events", self.DEFAULTS["ai_use_detection_summary_events"]
            )
        )

    def _ai_run_started_lookback_s(self) -> float:
        return float(self.args.get("ai_run_started_lookback_s", self.DEFAULTS["ai_run_started_lookback_s"]))

    def _on_detection_summary_run_started(self, event_name, data, kwargs) -> None:
        try:
            if not isinstance(data, dict):
                return
            bundle_key = str(data.get("bundle_key") or "")
            run_id = str(data.get("run_id") or "")
            started_ts = float(data.get("started_ts") or 0.0)
            if not bundle_key or not run_id:
                return
            latest = getattr(self, "_latest_run_started", None)
            if latest is None:
                self._latest_run_started = {}
                latest = self._latest_run_started
            latest[bundle_key] = {"run_id": run_id, "started_ts": started_ts}

            # Signal any waiters that a run_id is available.
            signals = getattr(self, "_run_started_signal", None)
            if signals is None:
                self._run_started_signal = {}
                signals = self._run_started_signal
            ev = signals.setdefault(bundle_key, threading.Event())
            ev.set()

            # If we have a pending door notification within the consolidation window, attach this run_id
            # so the eventual notification (either consolidated or delayed) can wait deterministically.
            now = time.time()
            delay = float(self.args.get("consolidation_delay", self.DEFAULTS["consolidation_delay"]))
            pad = self._ai_window_pad_s()
            attached = 0
            for _eid, p in (self._pending or {}).items():
                ts = float(p.get("timestamp") or 0.0)
                if ts <= 0:
                    continue
                if (now - ts) > (delay + pad):
                    continue
                if started_ts and started_ts < (ts - pad):
                    continue
                if p.get("ai_run_id"):
                    continue
                p["ai_run_id"] = run_id
                p["ai_run_started_ts"] = started_ts
                attached += 1
            if attached:
                self.log(
                    f"AI summary(event): attached run_id={run_id} to {attached} pending door notification(s)",
                    level="INFO",
                )
        except Exception:
            return

    def _get_latest_run_id(self, bundle_key: str) -> Optional[str]:
        info = getattr(self, "_latest_run_started", {}).get(bundle_key)
        if not info:
            return None
        run_id = str(info.get("run_id") or "")
        started_ts = float(info.get("started_ts") or 0.0)
        if not run_id:
            return None
        if started_ts > 0 and (time.time() - started_ts) > self._ai_run_started_lookback_s():
            return None
        return run_id

    def _on_door_state(self, entity_id, attribute, old, new, kwargs):
        """Handle door state change to open or closed.

        All events for a door are accumulated into a list.  Each new event cancels
        the existing consolidation timer and restarts it, so only ONE notification
        fires per inactivity window — regardless of how many open/close cycles occur
        before the timer expires.
        """
        self.log(f"Door state: {entity_id} {old!r} -> {new!r}", level="DEBUG")
        if not self._should_notify(old, new):
            self.log(f"Skipping notify: old={old!r} new={new!r}", level="DEBUG")
            return

        door_name = self._door_name(entity_id)
        from_display = self._from_state_display(old)
        now = time.time()

        pending = self._pending.get(entity_id)
        if pending:
            # Cancel the existing timer and append this event to the running list.
            if "handle" in pending:
                self.cancel_timer(pending["handle"])
            pending["events"].append({"state": new, "timestamp": now})
            pending["state"] = new
        else:
            # First event for this door: create the pending entry.
            pending = {
                "events": [{"state": new, "timestamp": now}],
                "state": new,
                "timestamp": now,  # first-event time; used by ai_run_id attachment logic
                "door_name": door_name,
                "from_display": from_display,
                "ai_run_id": None,
                "ai_run_started_ts": None,
            }
            self._pending[entity_id] = pending

        # (Re-)schedule the consolidation timer.
        delay = self.args.get("consolidation_delay", self.DEFAULTS["consolidation_delay"])
        handle = self.run_in(
            self._on_delay_expired,
            delay,
            entity_id=entity_id,
        )
        pending["handle"] = handle
        self.log(
            f"Door pending: {entity_id} state={new!r} events={len(pending['events'])} delay={float(delay):.0f}s",
            level="INFO",
        )

    def _on_delay_expired(self, kwargs):
        """Called when consolidation delay expires: send notification summarising all events."""
        entity_id = kwargs["entity_id"]
        pending = self._pending.pop(entity_id, None)
        if not pending:
            return
        events = pending.get("events") or []
        door_name = pending["door_name"]
        from_display = pending["from_display"]
        first_timestamp = pending["timestamp"]
        title, message = self._build_notification_from_events(door_name, events, from_display)
        window_start = first_timestamp - self._ai_window_pad_s()
        window_end = time.time()
        preferred_run_id = pending.get("ai_run_id")
        self._send_notifications_with_optional_ai_async(
            title,
            message,
            window_start_epoch=window_start,
            window_end_epoch=window_end,
            preferred_run_id=str(preferred_run_id) if preferred_run_id else None,
        )

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
        """Get display name: config override > HA friendly_name > entity_id."""
        door_names = self.args.get("door_names", {})
        if isinstance(door_names, dict) and entity_id in door_names:
            return door_names[entity_id]
        name = self.get_state(entity_id, attribute="friendly_name")
        return name if name else entity_id

    def _from_state_display(self, old: str) -> str:
        """Map intermediate states (e.g. opening/closing for covers) using configurable map."""
        if old is None:
            return "unknown"
        mapped = self._intermediate_state_map.get(old)
        if mapped is not None:
            return mapped
        return old

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

    def _state_label(self, raw_state: str) -> str:
        """Map raw state values to human-readable labels (e.g., 'on' → 'open')."""
        if raw_state == self._open_state:
            return "open"
        if raw_state == self._closed_state:
            return "closed"
        return raw_state

    def _build_notification(self, door_name: str, to_state: str, from_display: str) -> tuple[str, str]:
        """Build (title, message) for the notification."""
        action = "Opened" if to_state == self._open_state else "Closed"
        title = f"{door_name} {action}"
        message = f"{door_name} is now {self._state_label(to_state)} (was {self._state_label(from_display)})."
        return title, message

    def _build_notification_from_events(
        self,
        door_name: str,
        events: list[dict],
        from_display: str,
    ) -> tuple[str, str]:
        """Dispatch to the right notification builder based on how many events accumulated."""
        if not events:
            # Defensive fallback — should never happen.
            return f"{door_name} Activity", f"{door_name} had door activity."
        if len(events) == 1:
            return self._build_notification(door_name, events[0]["state"], from_display)
        if len(events) == 2:
            first, second = events
            was_open = first["state"] == self._open_state
            duration_secs = second["timestamp"] - first["timestamp"]
            return self._build_consolidated_notification(door_name, was_open=was_open, duration_secs=duration_secs)
        # 3+ events: produce an aggregated summary.
        activity_secs = events[-1]["timestamp"] - events[0]["timestamp"]
        return self._build_multi_event_notification(door_name, events, activity_secs)

    def _build_multi_event_notification(
        self, door_name: str, events: list[dict], activity_secs: float
    ) -> tuple[str, str]:
        """Build a phone-friendly summary for 3+ open/close events within a single window.

        Examples
        --------
        2 opens + 2 closes   → "In the last 3 minutes, the Garage Door was opened and closed twice."
        2 opens + 1 close    → "In the last minute, the Garage Door was opened and closed, then left open."
        3 opens + 2 closes   → "In the last 5 minutes, the Garage Door was opened and closed twice, then left open."
        """
        open_count = sum(1 for e in events if e["state"] == self._open_state)
        close_count = sum(1 for e in events if e["state"] == self._closed_state)

        def _count_phrase(n: int) -> str:
            return {1: "once", 2: "twice"}.get(n, f"{n} times")

        # Time qualifier
        if activity_secs < 60:
            time_phrase = "a short time"
        elif activity_secs < 120:
            time_phrase = "the last minute"
        else:
            mins = int(round(activity_secs / 60))
            time_phrase = f"the last {mins} minutes"

        if open_count == close_count:
            # Equal opens and closes: symmetric cycles.
            action = f"opened and closed {_count_phrase(open_count)}"
        elif open_count == close_count + 1:
            # Ends open.
            if close_count == 0:
                action = f"opened {_count_phrase(open_count)}"
            else:
                action = f"opened and closed {_count_phrase(close_count)}, then left open"
        elif close_count == open_count + 1:
            # Ends closed.
            if open_count == 0:
                action = f"closed {_count_phrase(close_count)}"
            else:
                action = f"closed and opened {_count_phrase(open_count)}, then left closed"
        else:
            # Unexpected imbalance — generic fallback.
            action = f"changed state {len(events)} times"

        title = f"{door_name} Multiple Events"
        message = f"In {time_phrase}, the {door_name} was {action}."
        return title, message

    def _send_notifications(
        self,
        title: str,
        message: str,
        image_web_path: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
    ) -> None:
        """Send notification to all configured services."""
        services = self.args.get("notify_services", self.DEFAULTS["notify_services"])
        notification_url = str(self.args.get("notification_url", self.DEFAULTS["notification_url"]) or "").strip()
        self.log(f"Sending notification: {title!r} to {len(services)} service(s)", level="INFO")
        for svc in services:
            # notify.mobile_app_xxx -> notify/mobile_app_xxx
            service = svc.replace(".", "/", 1) if "." in svc else f"notify/{svc}"
            data: dict[str, Any] = {}
            if image_web_path:
                data["image"] = image_web_path
            # Home Assistant Companion App deep-link (works across iOS/Android; keep both keys for compatibility).
            if notification_url:
                data["url"] = notification_url
                data["clickAction"] = notification_url
            if data:
                self.call_service(service, title=title, message=message, data=data)
            else:
                self.call_service(service, title=title, message=message)

    def _ai_enabled(self) -> bool:
        return bool(self.args.get("ai_enabled", self.DEFAULTS["ai_enabled"]))

    def _ai_bundle_key(self) -> str:
        return str(self.args.get("ai_bundle_key", self.DEFAULTS["ai_bundle_key"]))

    def _ai_wait_timeout_s(self) -> float:
        return float(self.args.get("ai_wait_timeout_s", self.DEFAULTS["ai_wait_timeout_s"]))

    def _ai_max_bundle_age_s(self) -> float:
        return float(self.args.get("ai_max_bundle_age_s", self.DEFAULTS["ai_max_bundle_age_s"]))

    def _ai_window_pad_s(self) -> float:
        return float(self.args.get("ai_window_pad_s", self.DEFAULTS["ai_window_pad_s"]))

    def _append_ai_summary(self, message: str, summary: Optional[str]) -> str:
        summary = (summary or "").strip()
        if not summary:
            return message
        return f"{message}\n\n{summary}"

    def _get_detection_summary(
        self,
        window_start_epoch: float,
        window_end_epoch: float,
        *,
        preferred_run_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        If enabled, fetch or wait briefly for a detection summary bundle and return:
        {summary, image_web_path, run_id}.
        """
        if not self._ai_enabled():
            return None

        t0 = time.time()
        bundle_key = self._ai_bundle_key()
        max_age_s = self._ai_max_bundle_age_s()

        # Event-driven path: if we have (or later discover) a run_id, wait specifically for that run_id.
        if self._ai_use_events():
            run_id = (preferred_run_id or "").strip() or self._get_latest_run_id(bundle_key)
            if run_id:
                self.log(
                    f"AI summary(event): start bundle_key={bundle_key} run_id={run_id} timeout_s={self._ai_wait_timeout_s():.0f}",
                    level="INFO",
                )
                bundle = DETECTION_SUMMARY_STORE.get_bundle_by_run_id(bundle_key, run_id, include_consumed=False)
                if not bundle:
                    timeout_s = self._ai_wait_timeout_s()
                    if timeout_s > 0:
                        self.log(
                            f"AI summary(event): waiting bundle_key={bundle_key} run_id={run_id} timeout_s={timeout_s:.0f}",
                            level="INFO",
                        )
                        bundle = DETECTION_SUMMARY_STORE.wait_for_run_id(
                            bundle_key,
                            run_id,
                            timeout_s=timeout_s,
                            include_consumed=False,
                        )
                if bundle:
                    best = bundle.get("best") or {}
                    generated = bundle.get("generated_image") or {}
                    run_narrative = bundle.get("run_narrative") or {}
                    DETECTION_SUMMARY_STORE.mark_consumed(bundle_key, run_id)

                    gen_url = (generated.get("image_url") or "").strip()
                    gen_web_path = (generated.get("image_web_path") or "").strip()
                    run_summary = ""
                    if isinstance(run_narrative, dict):
                        run_summary = str(run_narrative.get("run_summary") or "").strip()
                    self.log(
                        f"AI summary(event): run_id={run_id} waited={time.time()-t0:.3f}s "
                        f"image={'generated' if gen_url or gen_web_path else 'none'}",
                        level="INFO",
                    )
                    return {
                        "run_id": run_id,
                        "summary": run_summary or (best.get("summary") or ""),
                        "image_url": gen_url,
                        "image_web_path": gen_web_path,
                        "image": gen_url or gen_web_path,
                    }

        bundle = DETECTION_SUMMARY_STORE.get_best_bundle(
            bundle_key,
            window_start_epoch,
            window_end_epoch,
            include_consumed=False,
            max_age_s=max_age_s,
        )

        if not bundle:
            timeout_s = self._ai_wait_timeout_s()
            if timeout_s > 0:
                self.log(
                    f"AI summary(window): waiting bundle_key={bundle_key} timeout_s={timeout_s:.0f} "
                    f"window=({window_start_epoch:.0f},{window_end_epoch:.0f})",
                    level="INFO",
                )
                # IMPORTANT:
                # The detection-summary bundle may be published *after* this door-event window_end_epoch
                # (e.g. door close notification fires before LLM/image processing finishes).
                # When we choose to wait, extend the eligible window to include bundles published
                # during the wait interval.
                wait_window_end = max(float(window_end_epoch), time.time()) + float(timeout_s)

                # If events are enabled, we might receive a run_started during this wait window.
                # To allow switching to deterministic run_id waits, we do short wait slices.
                deadline = time.time() + float(timeout_s)
                slice_s = 2.0
                if self._ai_use_events():
                    signals = getattr(self, "_run_started_signal", None)
                    if signals is None:
                        self._run_started_signal = {}
                        signals = self._run_started_signal
                    signal = signals.setdefault(bundle_key, threading.Event())
                    # We only want to catch a *new* run_started during our wait.
                    signal.clear()

                while time.time() < deadline and not bundle:
                    remaining = deadline - time.time()
                    this_wait = min(slice_s, max(0.0, remaining))
                    if this_wait <= 0:
                        break

                    # If a run_started event arrives, switch to waiting for that run_id.
                    if self._ai_use_events():
                        signals = getattr(self, "_run_started_signal", None)
                        if signals is None:
                            self._run_started_signal = {}
                            signals = self._run_started_signal
                        signal = signals.setdefault(bundle_key, threading.Event())
                        if signal.wait(timeout=this_wait):
                            run_id = self._get_latest_run_id(bundle_key)
                            if run_id:
                                self.log(
                                    f"AI summary(switch): run_started observed during window-wait; switching to run_id={run_id}",
                                    level="INFO",
                                )
                                bundle = DETECTION_SUMMARY_STORE.wait_for_run_id(
                                    bundle_key,
                                    run_id,
                                    timeout_s=max(0.0, deadline - time.time()),
                                    include_consumed=False,
                                )
                                break

                    bundle = DETECTION_SUMMARY_STORE.wait_for_bundle(
                        bundle_key,
                        window_start_epoch,
                        wait_window_end,
                        timeout_s=this_wait,
                        include_consumed=False,
                        max_age_s=max_age_s,
                    )

        if not bundle:
            self.log(
                f"AI summary: none (bundle_key={bundle_key} window=({window_start_epoch:.0f},{window_end_epoch:.0f}) "
                f"max_age_s={max_age_s:.0f} waited={time.time()-t0:.3f}s)",
                level="DEBUG",
            )
            return None

        best = bundle.get("best") or {}
        generated = bundle.get("generated_image") or {}
        run_narrative = bundle.get("run_narrative") or {}
        run_id = str(bundle.get("run_id", ""))
        # Mark consumed so the next door event prefers a fresh bundle.
        if run_id:
            DETECTION_SUMMARY_STORE.mark_consumed(bundle_key, run_id)

        # Prefer generated image only (notifications use the illustration).
        gen_url = (generated.get("image_url") or "").strip()
        gen_web_path = (generated.get("image_web_path") or "").strip()
        image_url = gen_url
        image_web_path = gen_web_path
        run_summary = ""
        if isinstance(run_narrative, dict):
            run_summary = str(run_narrative.get("run_summary") or "").strip()
        self.log(
            f"AI summary: run_id={run_id} waited={time.time()-t0:.3f}s summary_len={len(str(best.get('summary') or ''))} "
            f"image={'generated' if gen_url or gen_web_path else 'none'}",
            level="INFO",
        )
        return {
            "run_id": run_id,
            "summary": run_summary or (best.get("summary") or ""),
            "image_url": image_url,
            "image_web_path": image_web_path,
            "image": image_url or image_web_path,
        }

    def _send_notifications_with_optional_ai_async(
        self,
        title: str,
        message: str,
        *,
        window_start_epoch: float,
        window_end_epoch: float,
        preferred_run_id: Optional[str] = None,
    ) -> None:
        """
        Avoid blocking AppDaemon callback threads while waiting for DetectionSummary bundles.
        Fetch/wait in a background thread, then schedule the actual HA notify calls back
        on the AppDaemon thread via run_in(..., 0).
        """

        if not self._ai_enabled():
            self._send_notifications(title, message)
            return

        self.log(
            f"AI notify: background fetch start window=({window_start_epoch:.0f},{window_end_epoch:.0f}) "
            f"bundle_key={self._ai_bundle_key()} use_events={self._ai_use_events()} timeout_s={self._ai_wait_timeout_s():.0f} "
            f"preferred_run_id={(preferred_run_id or '')!r}",
            level="INFO",
        )

        def _worker():
            ai = self._get_detection_summary(window_start_epoch, window_end_epoch, preferred_run_id=preferred_run_id)

            # Prefer generated image when available, otherwise best image.
            image = ""
            run_id = ""
            if ai:
                image = (ai.get("image") or "").strip()
                run_id = str(ai.get("run_id") or "").strip()
            final_title = title
            final_message = message
            final_image = image
            if ai:
                final_message = self._append_ai_summary(message, ai.get("summary"))

            # Schedule notify back on AD thread.
            self.run_in(
                self._send_notifications_async_callback,
                0,
                title=final_title,
                message=final_message,
                image=final_image,
                run_id=run_id,
            )

        t = threading.Thread(target=_worker, name=f"door_notify_{self._ai_bundle_key()}_ai")
        t.daemon = True
        t.start()

    def _send_notifications_async_callback(self, kwargs):
        title = kwargs.get("title") or ""
        message = kwargs.get("message") or ""
        image = (kwargs.get("image") or "").strip()
        run_id = (kwargs.get("run_id") or "").strip()
        self._send_notifications(title, message, image_web_path=image if image else None, run_id=run_id if run_id else None)
