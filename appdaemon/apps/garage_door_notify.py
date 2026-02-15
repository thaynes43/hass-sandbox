"""
Garage RATGDO door open/close notifications.

Sends push notifications when either RATGDO garage door reports open or closed.
Consolidates rapid open->closed (or closed->open) into a single "was open/closed
for n minutes m seconds" notification to avoid two alerts when someone leaves
or arrives.
"""

import threading
import time
from typing import Any, Optional

import hassapi as hass

from detection_summary_store import STORE as DETECTION_SUMMARY_STORE


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
        # Detection summary attachment (optional)
        "ai_enabled": False,
        "ai_bundle_key": "garage",
        "ai_wait_timeout_s": 30,
        "ai_max_bundle_age_s": 120,
        "ai_window_pad_s": 5,
        # Event-driven coordination with DetectionSummary (preferred).
        "ai_use_detection_summary_events": True,
        "ai_run_started_lookback_s": 900,
    }

    def initialize(self):
        """Register state listeners for each door."""
        self._pending = {}  # entity_id -> {state, timestamp, handle, door_name, from_display}
        self._latest_run_started: dict[str, dict[str, Any]] = {}

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
            self._latest_run_started[bundle_key] = {"run_id": run_id, "started_ts": started_ts}
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
            window_start = pending["timestamp"] - self._ai_window_pad_s()
            window_end = time.time()
            self._send_notifications_with_optional_ai_async(
                title, message, window_start_epoch=window_start, window_end_epoch=window_end
            )
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
        window_start = pending["timestamp"] - self._ai_window_pad_s()
        window_end = time.time()
        self._send_notifications_with_optional_ai_async(
            title, message, window_start_epoch=window_start, window_end_epoch=window_end
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

    def _send_notifications(self, title: str, message: str, image_web_path: Optional[str] = None) -> None:
        """Send notification to all configured services."""
        services = self.args.get("notify_services", self.DEFAULTS["notify_services"])
        self.log(f"Sending notification: {title!r} to {len(services)} service(s)", level="INFO")
        for svc in services:
            # notify.mobile_app_xxx -> notify/mobile_app_xxx
            service = svc.replace(".", "/", 1) if "." in svc else f"notify/{svc}"
            if image_web_path:
                self.call_service(service, title=title, message=message, data={"image": image_web_path})
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

    def _get_detection_summary(self, window_start_epoch: float, window_end_epoch: float) -> Optional[dict]:
        """
        If enabled, fetch or wait briefly for a detection summary bundle and return:
        {summary, image_web_path, run_id}.
        """
        if not self._ai_enabled():
            return None

        t0 = time.time()
        bundle_key = self._ai_bundle_key()
        max_age_s = self._ai_max_bundle_age_s()

        # Event-driven path: if we saw a recent run start, wait specifically for that run_id.
        if self._ai_use_events():
            run_id = self._get_latest_run_id(bundle_key)
            if run_id:
                bundle = DETECTION_SUMMARY_STORE.get_bundle_by_run_id(bundle_key, run_id, include_consumed=False)
                if not bundle:
                    timeout_s = self._ai_wait_timeout_s()
                    if timeout_s > 0:
                        bundle = DETECTION_SUMMARY_STORE.wait_for_run_id(
                            bundle_key,
                            run_id,
                            timeout_s=timeout_s,
                            include_consumed=False,
                        )
                if bundle:
                    best = bundle.get("best") or {}
                    generated = bundle.get("generated_image") or {}
                    DETECTION_SUMMARY_STORE.mark_consumed(bundle_key, run_id)

                    gen_url = (generated.get("image_url") or "").strip()
                    gen_web_path = (generated.get("image_web_path") or "").strip()
                    self.log(
                        f"AI summary(event): run_id={run_id} waited={time.time()-t0:.3f}s "
                        f"image={'generated' if gen_url or gen_web_path else 'none'}",
                        level="INFO",
                    )
                    return {
                        "run_id": run_id,
                        "summary": best.get("summary") or "",
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
                # IMPORTANT:
                # The detection-summary bundle may be published *after* this door-event window_end_epoch
                # (e.g. door close notification fires before LLM/image processing finishes).
                # When we choose to wait, extend the eligible window to include bundles published
                # during the wait interval.
                wait_window_end = max(float(window_end_epoch), time.time()) + float(timeout_s)
                bundle = DETECTION_SUMMARY_STORE.wait_for_bundle(
                    bundle_key,
                    window_start_epoch,
                    wait_window_end,
                    timeout_s=timeout_s,
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
        run_id = str(bundle.get("run_id", ""))
        # Mark consumed so the next door event prefers a fresh bundle.
        if run_id:
            DETECTION_SUMMARY_STORE.mark_consumed(bundle_key, run_id)

        # Prefer generated image only (garage notifications use the illustration).
        gen_url = (generated.get("image_url") or "").strip()
        gen_web_path = (generated.get("image_web_path") or "").strip()
        image_url = gen_url
        image_web_path = gen_web_path
        self.log(
            f"AI summary: run_id={run_id} waited={time.time()-t0:.3f}s summary_len={len(str(best.get('summary') or ''))} "
            f"image={'generated' if gen_url or gen_web_path else 'none'}",
            level="INFO",
        )
        return {
            "run_id": run_id,
            "summary": best.get("summary") or "",
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
    ) -> None:
        """
        Avoid blocking AppDaemon callback threads while waiting for DetectionSummary bundles.
        Fetch/wait in a background thread, then schedule the actual HA notify calls back
        on the AppDaemon thread via run_in(..., 0).
        """

        if not self._ai_enabled():
            self._send_notifications(title, message)
            return

        def _worker():
            ai = self._get_detection_summary(window_start_epoch, window_end_epoch)

            # Prefer generated image when available, otherwise best image.
            image = ""
            if ai:
                image = (ai.get("image") or "").strip()
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
            )

        t = threading.Thread(target=_worker, name="garage_door_notify_ai")
        t.daemon = True
        t.start()

    def _send_notifications_async_callback(self, kwargs):
        title = kwargs.get("title") or ""
        message = kwargs.get("message") or ""
        image = (kwargs.get("image") or "").strip()
        self._send_notifications(title, message, image_web_path=image if image else None)
