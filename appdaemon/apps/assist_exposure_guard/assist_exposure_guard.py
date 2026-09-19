"""Assist exposure guard — keep the dangerous set out of Home Assistant's voice agents.

Home Assistant's Assist exposure list is the only security boundary in front
of the LLM voice agents.  An exposed lock can be unlocked by voice with no PIN
(``HassTurnOff`` on a lock maps to ``lock.unlock``), an exposed garage cover
can be opened (``HassTurnOn`` maps to ``cover.open_cover``), and any exposed
script is an unrestricted tool.  Exposure is one global list — there is no
per-pipeline, per-agent or per-satellite scoping — so a single accidental
"expose everything in this area" click in the UI is enough to hand a voice
agent the front door.

This app is the backstop for that click:

1. On startup, every ``check_interval_minutes``, and (debounced) whenever HA
   fires ``entity_registry_updated``, list the entities exposed to the
   ``conversation`` assistant.
2. Evaluate them against the deny rules in :mod:`rules` (a pure module).
3. If ``enforce`` is true, un-expose every violator with one WebSocket
   command.  If it is false, report only.
4. Either way, raise a persistent notification (stable ``notification_id``, so
   it updates rather than stacks) and optionally a mobile push.
5. Publish ``sensor.assist_exposure_guard`` so the state of the guard itself
   is visible on a dashboard.

The app needs no HA entities of its own beyond that sensor, which ``set_state``
creates implicitly — there is nothing for ``ha_provisioner`` to provision.  All
HA HTTP/WebSocket traffic goes through
``providers.ha_provisioner.AssistExposureClient`` (security rule S2); the admin
token is referenced by env-var name only (``ha_token_env``), never by value.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# AppDaemon only adds `appdaemon/apps` to sys.path.  Providers live at
# `appdaemon/providers`, so append the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from assist_exposure_guard.rules import (
    ExposedEntity,
    GuardRules,
    Violation,
    evaluate,
)
from providers.ha_provisioner import CONVERSATION_ASSISTANT, AssistExposureClient
from providers.secrets import resolve_arg_secret

SENSOR_ENTITY_ID = "sensor.assist_exposure_guard"
NOTIFICATION_ID = "assist_exposure_guard"
REGISTRY_EVENT = "entity_registry_updated"

#: Cap on per-entity WARNING lines and on notification bullet points. A bulk
#: "expose this whole area" click can produce dozens of violations at once;
#: the count is always exact, only the enumeration is truncated.
MAX_DETAIL_LINES = 20


class AssistExposureGuard(hass.Hass):
    """Enforce the Assist exposure deny rules on a schedule and on registry changes."""

    DEFAULTS: Dict[str, Any] = {
        "assistant": CONVERSATION_ASSISTANT,
        "check_interval_minutes": 15,
        "registry_debounce_s": 30,
        "enforce": True,
        "notify_service": "",  # e.g. notify/mobile_app_toms_phone — unset by default
        "notification_id": NOTIFICATION_ID,
        "status_sensor": SENSOR_ENTITY_ID,
        "registry_event": REGISTRY_EVENT,
    }

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}
        # NOTE: AppDaemon injects the app key as args["name"]; never use
        # "name" as a config key here.
        self._assistant = str(args.get("assistant", self.DEFAULTS["assistant"])).strip()
        self._enforce = bool(args.get("enforce", self.DEFAULTS["enforce"]))
        self._interval_s = max(
            60,
            int(
                float(
                    args.get(
                        "check_interval_minutes", self.DEFAULTS["check_interval_minutes"]
                    )
                )
                * 60
            ),
        )
        self._debounce_s = max(
            1, int(args.get("registry_debounce_s", self.DEFAULTS["registry_debounce_s"]))
        )
        self._notify_service = self._normalise_service(
            str(args.get("notify_service", self.DEFAULTS["notify_service"]) or "")
        )
        self._notification_id = str(
            args.get("notification_id", self.DEFAULTS["notification_id"])
        )
        self._status_sensor = str(
            args.get("status_sensor", self.DEFAULTS["status_sensor"])
        )
        self._registry_event = str(
            args.get("registry_event", self.DEFAULTS["registry_event"])
        )
        self._rules = GuardRules.from_config(args)
        self._ha_url = str(resolve_arg_secret(args, "ha_url", required=True))
        self._ha_token_env = str(args.get("ha_token_env") or "").strip()
        if not self._ha_token_env:
            raise ValueError(
                "assist_exposure_guard requires 'ha_token_env' in apps.yaml — "
                "the exposure WebSocket commands need an admin token"
            )

        self._client: Optional[AssistExposureClient] = None
        self._debounce_handle: Any = None
        self._check_in_flight = False
        # Assume a notification may have survived an AppDaemon restart, so the
        # first clean run clears a stale one rather than leaving it on screen.
        self._notification_active = True

        self.log(
            f"AssistExposureGuard initializing — assistant={self._assistant!r} "
            f"enforce={self._enforce} interval_s={self._interval_s} "
            f"debounce_s={self._debounce_s} sensor={self._status_sensor!r} "
            f"notify_service={self._notify_service or 'none'!r} "
            f"deny_domains={len(self._rules.deny_domains)} "
            f"deny_globs={len(self._rules.deny_entity_globs)} "
            f"switch_allowlist={len(self._rules.switch_allowlist)} "
            f"allow_entities={len(self._rules.allow_entities)}",
            level="INFO",
        )
        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Dict[str, Any]) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        self._client = self._build_client()
        self.listen_event(self._on_registry_updated, self._registry_event)
        self.run_every(self._on_interval, f"now+{self._interval_s}", self._interval_s)
        self.log(
            f"AssistExposureGuard started — listening for {self._registry_event!r}, "
            f"checking every {self._interval_s}s",
            level="INFO",
        )
        await self._run_check("startup")

    def _build_client(self) -> AssistExposureClient:
        """Seam for tests: the only place the real client is constructed."""
        return AssistExposureClient(
            ha_url=self._ha_url, ha_token_env=self._ha_token_env
        )

    def terminate(self) -> None:
        self._cancel_debounce()

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    async def _on_interval(self, kwargs: Dict[str, Any]) -> None:
        await self._run_check("interval")

    def _on_registry_updated(
        self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]
    ) -> None:
        """Debounce registry churn — a bulk exposure change fires many events.

        Logged at DEBUG: the registry fires on every rename, area assignment
        and integration reload, so an INFO line here would be pure noise.
        """
        action = (data or {}).get("action") if isinstance(data, dict) else None
        self.log(
            f"Registry event {self._registry_event!r} action={action!r} — "
            f"debouncing exposure check for {self._debounce_s}s",
            level="DEBUG",
        )
        self._cancel_debounce()
        self._debounce_handle = self.run_in(self._on_debounced, self._debounce_s)

    async def _on_debounced(self, kwargs: Dict[str, Any]) -> None:
        self._debounce_handle = None
        await self._run_check(self._registry_event)

    def _cancel_debounce(self) -> None:
        handle = self._debounce_handle
        self._debounce_handle = None
        if handle is None:
            return
        try:
            self.cancel_timer(handle)
        except Exception as exc:  # noqa: BLE001 — a fired/stale handle is harmless
            self.log(f"Could not cancel debounce timer: {exc!r}", level="DEBUG")

    # ------------------------------------------------------------------
    # The check
    # ------------------------------------------------------------------

    async def _run_check(self, trigger: str) -> None:
        """Run one check, never raising and never leaving the latch set.

        ``_check_in_flight`` is released in ``finally`` inside this same
        coroutine — there is no callback hop that could strand it — so no
        watchdog timer is needed to recover it.
        """
        if self._check_in_flight:
            self.log(
                f"Exposure check already running — skipping trigger={trigger!r}",
                level="DEBUG",
            )
            return
        self._check_in_flight = True
        try:
            await self._check(trigger)
        except Exception as exc:  # noqa: BLE001 — the guard must survive HA hiccups
            message = self._redact(f"{type(exc).__name__}: {exc}")
            self.log(
                f"Exposure check failed (trigger={trigger!r}): {message}",
                level="ERROR",
            )
            self._publish_status(
                exposed_count=None, violations=[], trigger=trigger, error=message
            )
        finally:
            self._check_in_flight = False

    async def _check(self, trigger: str) -> None:
        client = self._client
        if client is None:  # pragma: no cover — startup always sets it first
            raise RuntimeError("Exposure client not initialised")

        exposed_ids = await client.list_exposed_entities(self._assistant)
        platforms = await client.list_entity_platforms()
        entities = [
            ExposedEntity(
                entity_id=entity_id,
                platform=platforms.get(entity_id, ""),
                device_class=await self._device_class(entity_id),
            )
            for entity_id in exposed_ids
        ]

        violations = evaluate(entities, self._rules)
        if not violations:
            self.log(
                f"Assist exposure OK (trigger={trigger!r}): {len(entities)} exposed "
                f"entity(s), no violations",
                level="INFO",
            )
            self._clear_notification()
            self._publish_status(
                exposed_count=len(entities), violations=violations, trigger=trigger
            )
            return

        self.log(
            f"Assist exposure violations (trigger={trigger!r}): {len(violations)} of "
            f"{len(entities)} exposed entity(s) break the deny rules "
            f"(enforce={self._enforce})",
            level="WARNING",
        )
        for violation in violations[:MAX_DETAIL_LINES]:
            self.log(
                f"Exposure violation: {violation.entity_id} — {violation.reason} "
                f"[{violation.rule}]",
                level="WARNING",
            )
        if len(violations) > MAX_DETAIL_LINES:
            self.log(
                f"…and {len(violations) - MAX_DETAIL_LINES} further violation(s) "
                f"not listed",
                level="WARNING",
            )

        enforced = False
        enforce_error = ""
        if self._enforce:
            # Enforcement failure is caught here rather than in _run_check so
            # the owner is still notified: "these are exposed and I could NOT
            # take them away" is the most urgent state this app can be in, and
            # an ERROR log alone is easy to miss.
            try:
                count = await client.set_exposure(
                    [violation.entity_id for violation in violations],
                    should_expose=False,
                    assistant=self._assistant,
                )
            except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                enforce_error = self._redact(f"{type(exc).__name__}: {exc}")
                self.log(
                    f"Failed to un-expose {len(violations)} entity(s) from "
                    f"{self._assistant!r}: {enforce_error}",
                    level="ERROR",
                )
            else:
                enforced = True
                self.log(
                    f"Un-exposed {count} entity(s) from {self._assistant!r}",
                    level="INFO",
                )

        self._notify(violations, enforced=enforced, enforce_error=enforce_error)
        self._publish_status(
            exposed_count=len(entities),
            violations=violations,
            trigger=trigger,
            error=enforce_error,
        )

    async def _device_class(self, entity_id: str) -> str:
        """Read ``device_class`` from state — only covers need it.

        The entity registry's partial dict does not carry ``device_class``, so
        it has to come from state.  Only the ``cover`` domain has a
        device-class rule, and a full run would otherwise issue one state read
        per exposed entity for nothing.
        """
        if not entity_id.startswith("cover."):
            return ""
        try:
            value = await self.get_state(entity_id, attribute="device_class")
        except Exception as exc:  # noqa: BLE001 — a missing entity is not fatal
            # Fail OPEN, deliberately: an unreadable device_class most often
            # means the entity is briefly unavailable, and denying on that
            # would un-expose window shades during an HA blip — which a human
            # then has to re-expose by hand. The cost of failing closed is
            # higher than one more check interval of a backstop being blind.
            self.log(
                f"Could not read device_class for {entity_id}: {exc!r} — treating it "
                f"as unclassified, so cover device-class rules cannot apply this run",
                level="WARNING",
            )
            return ""
        return str(value or "").strip().lower()

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------

    def _notify(
        self,
        violations: List[Violation],
        *,
        enforced: bool,
        enforce_error: str = "",
    ) -> None:
        plural = "entity" if len(violations) == 1 else "entities"
        title = f"Assist exposure guard: {len(violations)} unsafe {plural}"
        if enforce_error:
            title = f"{title} — UN-EXPOSE FAILED"
            header = (
                f"STILL EXPOSED to {self._assistant!r} — un-exposing them failed "
                f"({enforce_error}). Remove them in Settings → Voice assistants → "
                f"Expose:"
            )
        elif enforced:
            header = (
                f"Un-exposed from {self._assistant!r} — these must never be "
                f"voice-callable:"
            )
        else:
            header = (
                f"Exposed to {self._assistant!r} but must not be "
                f"(enforce is off — nothing was changed):"
            )
        lines = [header]
        lines += [
            f"- {violation.entity_id} — {violation.reason}"
            for violation in violations[:MAX_DETAIL_LINES]
        ]
        if len(violations) > MAX_DETAIL_LINES:
            lines.append(f"- …and {len(violations) - MAX_DETAIL_LINES} more")
        message = "\n".join(lines)

        self.call_service(
            "persistent_notification/create",
            title=title,
            message=message,
            notification_id=self._notification_id,
        )
        self._notification_active = True
        self.log(
            f"Persistent notification {self._notification_id!r} updated: "
            f"{len(violations)} violation(s) enforced={enforced}",
            level="INFO",
        )

        if self._notify_service:
            self.call_service(self._notify_service, title=title, message=message)
            self.log(
                f"Mobile notification sent via {self._notify_service!r}",
                level="INFO",
            )

    def _clear_notification(self) -> None:
        if not self._notification_active:
            return
        self.call_service(
            "persistent_notification/dismiss", notification_id=self._notification_id
        )
        self._notification_active = False
        self.log(
            f"Persistent notification {self._notification_id!r} dismissed — "
            f"exposure list is clean",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Status sensor
    # ------------------------------------------------------------------

    def _publish_status(
        self,
        *,
        exposed_count: Optional[int],
        violations: List[Violation],
        trigger: str,
        error: str = "",
    ) -> None:
        """Publish the guard's own health as ``sensor.assist_exposure_guard``.

        ``exposed_count is None`` means the check itself could not run, so the
        counts are reported as ``unknown`` rather than as a stale or invented
        zero.  ``error`` is informational and can accompany a real count — an
        enforcement failure knows exactly how many entities are exposed.

        Every value below is a **non-empty string** on purpose. AppDaemon
        4.5.13 runs ``set_state`` kwargs through ``utils.clean_http_kwargs``,
        which turns ``True`` into ``"true"`` and then drops every entry equal
        to ``None`` or ``False`` — and ``0 == False``, so an integer ``0``
        count or a ``False`` flag simply vanishes from the published state.
        That applies to ``state`` itself as well, so the count is stringified.
        """
        attributes = {
            "friendly_name": "Assist Exposure Guard",
            "icon": "mdi:shield-account",
            "assistant": self._assistant,
            "enforce": "true" if self._enforce else "false",
            "violations_last_run": (
                "unknown" if exposed_count is None else str(len(violations))
            ),
            "violating_entities": (
                ", ".join(violation.entity_id for violation in violations) or "none"
            ),
            "last_run": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_trigger": trigger,
            "last_error": error or "none",
        }
        self.set_state(
            self._status_sensor,
            state="unknown" if exposed_count is None else str(exposed_count),
            attributes=attributes,
        )
        self.log(
            f"Published {self._status_sensor} state={attributes['violations_last_run']} "
            f"violations exposed={exposed_count}",
            level="DEBUG",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_service(service: str) -> str:
        """Accept ``notify.mobile_app_x``/``notify/mobile_app_x``/``mobile_app_x``."""
        service = service.strip()
        if not service:
            return ""
        if "/" in service:
            return service
        if "." in service:
            return service.replace(".", "/", 1)
        return f"notify/{service}"

    def _redact(self, text: str) -> str:
        """Strip the configured HA URL out of an error string.

        ``aiohttp`` quotes the URL it failed on, and this text reaches both the
        log and the status sensor's ``last_error`` attribute (which is
        frontend-visible). The token never appears in a URL — it travels in a
        header and in the WebSocket auth frame — but the host does.
        """
        if self._ha_url and self._ha_url in text:
            text = text.replace(self._ha_url, "<ha_url>")
        return text
