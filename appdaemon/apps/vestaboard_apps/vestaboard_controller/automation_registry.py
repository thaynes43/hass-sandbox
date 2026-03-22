"""Automation registry — manages RemoteAutomationProxy instances and config store."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional


class RemoteAutomationProxy:
    """Stores metadata from event-based automation registration.

    Created when the controller receives a ``register_automation`` event from
    an automation app.  Provides the same interface that the controller uses
    when communicating back to automation apps (config, preview, etc.) without
    requiring a direct Python object reference.
    """

    def __init__(self, data: dict) -> None:
        self.name = data["automation_id"]
        self.automation_type = data.get("automation_type", "")
        self.display_name = data.get("display_name", "Automation")
        self.display_description = data.get("display_description", "")
        self.default_ttl_s = data.get("default_ttl_s")
        self.default_max_age_s = data.get("default_max_age_s")
        self.default_should_expire = data.get("default_should_expire", False)
        self.DEFAULT_UI_CONFIG = data.get("DEFAULT_UI_CONFIG", {})
        self._config_schema = data.get("config_schema", {})

        # preview_frame arrives as a JSON string to avoid HA zero-stripping
        raw_preview = data.get("preview_frame", "")
        if isinstance(raw_preview, str) and raw_preview:
            try:
                self._preview_frame = json.loads(raw_preview)
            except Exception:
                self._preview_frame = [[0] * 22 for _ in range(6)]
        elif isinstance(raw_preview, list):
            self._preview_frame = raw_preview
        else:
            self._preview_frame = [[0] * 22 for _ in range(6)]

        self._effective_config: dict[str, Any] = dict(self.DEFAULT_UI_CONFIG)
        self._next_fire_time: Optional[float] = None

    def get_config_schema(self) -> dict:
        return self._config_schema

    def get_preview_frame(self) -> list[list[int]]:
        return self._preview_frame

    def get_effective_config(self) -> dict[str, Any]:
        return dict(self._effective_config)

    def get_resolved_ttl_s(self) -> Optional[int]:
        ttl_min = self._effective_config.get("ttl_minutes")
        if ttl_min is not None:
            return int(float(ttl_min) * 60)
        return self.default_ttl_s

    def get_resolved_should_expire(self) -> bool:
        return bool(self._effective_config.get("should_expire", self.default_should_expire))

    def update_config(self, config: dict) -> None:
        self._effective_config.update(config)


class AutomationRegistry:
    """Manages registered automation proxies and their persisted config.

    Pure Python — no AppDaemon dependency.  Uses a ``log_fn`` callable for
    logging (same pattern as ``FrameQueue``).
    """

    def __init__(
        self,
        config_store: Any,
        log_fn: Callable[[str], None],
    ) -> None:
        self._automations: dict[str, RemoteAutomationProxy] = {}
        self._config_store = config_store
        self._log = log_fn

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, payload: dict) -> tuple[RemoteAutomationProxy, bool]:
        """Register (or re-register) an automation.

        Returns ``(proxy, is_reregistration)``.
        """
        auto_id = str(payload.get("automation_id", "")).strip()
        if not auto_id:
            self._log("register_automation: missing automation_id in payload")
            raise ValueError("missing automation_id")

        old_proxy = self._automations.get(auto_id)
        old_fire_time = getattr(old_proxy, "_next_fire_time", None) if old_proxy else None

        proxy = RemoteAutomationProxy(payload)
        if old_fire_time is not None:
            proxy._next_fire_time = old_fire_time
        self._automations[auto_id] = proxy

        reregistered = old_proxy is not None
        self._log(
            f"Automation {'re-' if reregistered else ''}registered: {auto_id!r} "
            f"(type={proxy.automation_type!r})"
            f"{f' | preserved next_fire_time={old_fire_time}' if old_fire_time else ''}"
        )

        # Seed config store defaults
        if self._config_store:
            defaults = proxy.DEFAULT_UI_CONFIG
            if defaults and self._config_store.seed(auto_id, defaults):
                self._config_store.save()
                self._log(f"Seeded config defaults for {auto_id!r}")

        # Apply persisted config to proxy
        stored_config: Optional[dict] = None
        if self._config_store:
            stored = self._config_store.get(auto_id)
            if stored:
                proxy.update_config(stored)
                stored_config = stored

        return proxy, reregistered

    def get_stored_config(self, auto_id: str) -> Optional[dict]:
        """Return persisted config for an automation, or None."""
        if self._config_store:
            stored = self._config_store.get(auto_id)
            return stored if stored else None
        return None

    def deregister(self, auto_id: str) -> Optional[RemoteAutomationProxy]:
        """Remove an automation. Returns the removed proxy, or None."""
        return self._automations.pop(auto_id, None)

    def get(self, auto_id: str) -> Optional[RemoteAutomationProxy]:
        """Look up a proxy by automation_id."""
        return self._automations.get(auto_id)

    # ------------------------------------------------------------------
    # Find
    # ------------------------------------------------------------------

    def find_by_type(self, automation_type: str) -> tuple[Optional[str], Optional[RemoteAutomationProxy]]:
        """Find a registered automation by its automation_type."""
        for auto_id, auto in self._automations.items():
            if getattr(auto, "automation_type", "") == automation_type:
                return auto_id, auto
        return None, None

    def find(self, *candidate_ids: str) -> tuple[Optional[str], Optional[RemoteAutomationProxy]]:
        """Find first matching automation by trying multiple IDs, then by type."""
        for aid in candidate_ids:
            auto = self._automations.get(aid)
            if auto is not None:
                return aid, auto
        for aid in candidate_ids:
            found_id, found = self.find_by_type(aid)
            if found is not None:
                return found_id, found
        return None, None

    # ------------------------------------------------------------------
    # Config and state management
    # ------------------------------------------------------------------

    def activate(self, auto_id: str) -> Optional[RemoteAutomationProxy]:
        """Persist enabled=True. Returns the proxy, or None if not found."""
        proxy = self._automations.get(auto_id)
        if proxy is None:
            return None
        if self._config_store is not None:
            self._config_store.update(auto_id, {"enabled": True})
            self._log(f"Persisted enabled=True for {auto_id!r}")
        return proxy

    def deactivate(self, auto_id: str) -> Optional[RemoteAutomationProxy]:
        """Persist enabled=False. Returns the proxy, or None if not found."""
        proxy = self._automations.get(auto_id)
        if proxy is None:
            return None
        if self._config_store is not None:
            self._config_store.update(auto_id, {"enabled": False})
            self._log(f"Persisted enabled=False for {auto_id!r}")
        return proxy

    def set_config(self, auto_id: str, new_config: dict) -> Optional[RemoteAutomationProxy]:
        """Update config for a registered automation. Returns the proxy, or None."""
        proxy = self._automations.get(auto_id)
        if proxy is None:
            return None

        if self._config_store is not None:
            self._config_store.update(auto_id, new_config)
            self._log(f"Config persisted for {auto_id!r}")

        if isinstance(proxy, RemoteAutomationProxy):
            proxy.update_config(new_config)

        return proxy

    def update_next_fire_time(self, auto_id: str, next_fire_time: float) -> None:
        """Update a proxy's next_fire_time for status display."""
        proxy = self._automations.get(auto_id)
        if proxy is not None:
            import time as _t
            delta = next_fire_time - _t.time()
            self._log(
                f"Next fire time updated: {auto_id!r} -> {delta / 60:.1f} min from now"
            )
            proxy._next_fire_time = next_fire_time

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def all_automations(self) -> list[tuple[str, RemoteAutomationProxy]]:
        """Return list of (automation_id, proxy) pairs."""
        return list(self._automations.items())
