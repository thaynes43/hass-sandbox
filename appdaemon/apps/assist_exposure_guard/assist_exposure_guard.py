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
3. If ``enforce`` is true, un-expose every violator Home Assistant will accept
   with one WebSocket command (a malformed id is left out and stays reported —
   see *Partial enforcement* in the README).  If it is false, report only.
4. Notify on two separate channels, because they answer different questions:
   an **enforcement record** under ``<notification_id>_enforced`` (an action
   already taken — never auto-cleared, only the user dismisses it) and a
   **current-state** notification under ``<notification_id>`` (report-only
   findings, a failed un-expose, or a failed check — cleared by the next clean
   run, or by the run that un-exposes the last violator).  Conflating them made enforcement erase its own evidence:
   ``expose_entity`` itself fires ``entity_registry_updated``, whose debounced
   re-check finds the list clean seconds later.
5. Publish ``sensor.assist_exposure_guard`` so the state of the guard itself
   is visible on a dashboard — with ``last_enforced``/``last_enforced_entities``
   outliving that clean re-check.

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


def _as_bool(value: Any, default: bool) -> bool:
    """Parse a YAML flag without ``bool("false") is True``.

    ``enforce`` is the only thing between a dev run and the single live
    exposure list, so a quoted ``"false"`` must not read as enabled.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        # Anything unrecognised reads as report-only: the non-destructive mode
        # still notifies, whereas a typo that enabled writes would not be seen.
        return text in ("true", "yes", "on", "1")
    return bool(value)


def _capped_join(entity_ids: List[str]) -> str:
    """Join ids for a sensor attribute, capped like the log and notification.

    A bulk "expose this area" click can produce hundreds of violations; the exact
    count lives in ``violations_last_run``, so the attribute only needs a sample.
    """
    if not entity_ids:
        return "none"
    shown = ", ".join(entity_ids[:MAX_DETAIL_LINES])
    extra = len(entity_ids) - MAX_DETAIL_LINES
    return f"{shown}, …and {extra} more" if extra > 0 else shown


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
        self._enforce = _as_bool(args.get("enforce"), self.DEFAULTS["enforce"])
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

        self._enforced_notification_id = f"{self._notification_id}_enforced"

        self._client: Optional[AssistExposureClient] = None
        self._debounce_handle: Any = None
        self._check_in_flight = False
        # Tracks the CURRENT-STATE notification only (report-only findings, a
        # failed un-expose, or a failed check).  Seeded True so the first clean
        # run clears one that outlived an AppDaemon restart.  The enforcement
        # record under `_enforced_notification_id` is never touched by this.
        self._notification_active = True
        # Durable record of the last enforcement.  It must survive the clean
        # run that enforcement itself causes, so it lives here rather than
        # being derived from the current exposure list.
        self._last_enforced = "never"
        self._last_enforced_entities = "none"
        # Fingerprint of the current-state condition already pushed to a phone.
        # The persistent notification is idempotent (same id, updated in
        # place), but a mobile push is not: an unchanged report-only finding or
        # a wedged HA would otherwise buzz every check_interval_minutes until
        # the owner mutes the app — and a muted app reports nothing at all.
        self._pushed_fingerprint = ""

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
        """Wire the triggers FIRST, then check.

        The client is built lazily inside the guarded check path on purpose: a
        bad token or URL makes ``AssistExposureClient`` raise, and building it
        here would abort startup before ``listen_event``/``run_every`` ran,
        leaving the guard permanently inert *and* silent — the worst possible
        failure for a security backstop.  Wired first, a construction failure
        is just one failed check that reports itself and retries on the next
        tick.
        """
        self.listen_event(self._on_registry_updated, self._registry_event)
        self.run_every(self._on_interval, f"now+{self._interval_s}", self._interval_s)
        self.log(
            f"AssistExposureGuard started — listening for {self._registry_event!r}, "
            f"checking every {self._interval_s}s",
            level="INFO",
        )
        await self._seed_enforcement_record()
        await self._run_check("startup")

    async def _seed_enforcement_record(self) -> None:
        """Recover ``last_enforced*`` from the sensor left by a previous run.

        An AppDaemon reload builds a brand-new instance, and the enforcement
        record must not be erased by one — the persistent notification it
        pairs with is HA-side and survives.  The sensor is the app's own, so
        reading it back is the cheapest durable store available here.
        """
        try:
            attributes = await self.get_state(self._status_sensor, attribute="all")
        except Exception as exc:  # noqa: BLE001 — a missing sensor is normal
            self.log(f"No previous status sensor to seed from: {exc!r}", level="DEBUG")
            return
        if not isinstance(attributes, dict):
            return
        previous = attributes.get("attributes")
        if not isinstance(previous, dict):
            return
        last_enforced = str(previous.get("last_enforced") or "").strip()
        if not last_enforced or last_enforced == "never":
            return
        self._last_enforced = last_enforced
        self._last_enforced_entities = str(
            previous.get("last_enforced_entities") or "none"
        ).strip() or "none"
        self.log(
            f"Recovered enforcement record from {self._status_sensor}: "
            f"last_enforced={self._last_enforced!r}",
            level="INFO",
        )

    def _ensure_client(self) -> AssistExposureClient:
        """Build the client on first use; a failure here is a failed check.

        Left as ``None`` on failure so the next tick retries rather than
        latching the app into a dead state.
        """
        if self._client is None:
            self._client = self._build_client()
        return self._client

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
            # A guard that cannot run is indistinguishable from a guard with
            # nothing to do unless it says so somewhere a human looks.
            self._notify_check_failed(trigger, message)
            self._publish_status(
                exposed_count=None, violations=[], trigger=trigger, error=message
            )
        finally:
            self._check_in_flight = False

    async def _check(self, trigger: str) -> None:
        client = self._ensure_client()

        raw_ids = await client.list_exposed_entities(self._assistant)
        # Normalise ONCE and use that single value everywhere — the registry
        # lookup, the state read for device_class and the rule engine — so a
        # non-canonical id can never be resolved on one path and missed on another.
        exposed_ids = list(
            dict.fromkeys(
                text for text in (str(raw).strip().lower() for raw in raw_ids) if text
            )
        )
        platforms = await client.list_entity_platforms(exposed_ids)
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

        # Partition by what the write ACTUALLY did, never by "it did not
        # raise". set_exposure leaves malformed ids out of the batch (HA
        # validates entity_ids all-or-nothing), so a clean return can still
        # mean "one of these is untouched and still exposed". Recording that
        # one as un-exposed would be a false all-clear on this app's most
        # durable surface.
        applied: List[Violation] = []
        unapplied: List[Violation] = list(violations)
        enforce_error = ""
        if self._enforce:
            # Enforcement failure is caught here rather than in _run_check so
            # the owner is still notified: "these are exposed and I could NOT
            # take them away" is the most urgent state this app can be in, and
            # an ERROR log alone is easy to miss.
            try:
                change = await client.set_exposure(
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
                sent = set(change.sent)
                applied = [v for v in violations if v.entity_id in sent]
                unapplied = [v for v in violations if v.entity_id not in sent]
                if applied:
                    self.log(
                        f"Un-exposed {len(applied)} entity(s) from "
                        f"{self._assistant!r}",
                        level="INFO",
                    )
                if unapplied:
                    rejected = _capped_join([v.entity_id for v in unapplied])
                    enforce_error = (
                        f"Home Assistant cannot accept {len(unapplied)} malformed "
                        f"entity id(s), so they are STILL exposed: {rejected}"
                    )
                    self.log(
                        f"Left {len(unapplied)} of {len(violations)} violation(s) "
                        f"exposed — {enforce_error}",
                        level="ERROR",
                    )

        if applied:
            # Enforcement is self-erasing if it reports through the
            # current-state channel: set_exposure makes HA fire
            # entity_registry_updated, this app's own debounce re-checks, the
            # list is now clean, and the report would be dismissed and the
            # counts zeroed — deleting the only evidence that anything
            # happened. So an enforcement is recorded, not reported: its own
            # notification id, never auto-cleared, plus durable attributes.
            # It covers ONLY what really changed.
            self._record_enforcement(applied)

        if unapplied:
            # Still exposed after this run: report-only mode, a failed write,
            # or ids HA will not accept. Current state, so it repeats every
            # run and clears itself once the entity is gone.
            self._notify_unenforced(unapplied, enforce_error)
        elif applied:
            # Everything that was wrong is now fixed, so a "STILL EXPOSED"
            # notice left by an earlier partial run would be a lie. The next
            # clean run would clear it, but that can be a whole check interval
            # away for an entity whose change fires no registry event.
            self._clear_notification()

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

    def _violation_lines(self, violations: List[Violation]) -> List[str]:
        lines = [
            f"- {violation.entity_id} — {violation.reason}"
            for violation in violations[:MAX_DETAIL_LINES]
        ]
        if len(violations) > MAX_DETAIL_LINES:
            lines.append(f"- …and {len(violations) - MAX_DETAIL_LINES} more")
        return lines

    def _record_enforcement(self, violations: List[Violation]) -> None:
        """Record an enforcement that already happened — a log, not a report.

        Written under its own ``notification_id`` and **never** dismissed by
        this app: only the user clears it.  A later enforcement replaces the
        body with a freshly timestamped one under the same id, so the record
        stays a single entry rather than a stack.
        """
        stamp = self._now_iso()
        entity_ids = [violation.entity_id for violation in violations]
        self._last_enforced = stamp
        self._last_enforced_entities = _capped_join(list(entity_ids))

        plural = "entity" if len(violations) == 1 else "entities"
        title = (
            f"Assist exposure guard: un-exposed {len(violations)} {plural}"
        )
        lines = [
            f"{stamp} — removed from {self._assistant!r} because they must never "
            f"be voice-callable. They are no longer exposed; this notice stays "
            f"until you dismiss it.",
            "",
        ]
        lines += self._violation_lines(violations)
        message = "\n".join(lines)

        self.call_service(
            "persistent_notification/create",
            title=title,
            message=message,
            notification_id=self._enforced_notification_id,
        )
        self.log(
            f"Enforcement recorded under {self._enforced_notification_id!r}: "
            f"{len(violations)} entity(s) un-exposed at {stamp}",
            level="INFO",
        )
        self._push_mobile(title, message)

    def _notify_unenforced(
        self, violations: List[Violation], enforce_error: str = ""
    ) -> None:
        """Report violations that are STILL exposed — current state, clearable."""
        plural = "entity" if len(violations) == 1 else "entities"
        title = f"Assist exposure guard: {len(violations)} unsafe {plural}"
        if enforce_error:
            title = f"{title} — UN-EXPOSE FAILED"
            header = (
                f"STILL EXPOSED to {self._assistant!r} — un-exposing them failed "
                f"({enforce_error}). Remove them in Settings → Voice assistants → "
                f"Expose:"
            )
        else:
            header = (
                f"Exposed to {self._assistant!r} but must not be "
                f"(enforce is off — nothing was changed):"
            )
        message = "\n".join([header] + self._violation_lines(violations))
        self._publish_current_state_notification(
            title,
            message,
            fingerprint="unenforced|"
            + ",".join(violation.entity_id for violation in violations)
            + f"|{enforce_error}",
        )
        self.log(
            f"Persistent notification {self._notification_id!r} updated: "
            f"{len(violations)} violation(s) still exposed "
            f"(enforce_error={bool(enforce_error)})",
            level="INFO",
        )

    def _notify_check_failed(self, trigger: str, error: str) -> None:
        """Make a guard that cannot run visible, not just quietly absent."""
        self._publish_current_state_notification(
            "Assist exposure guard: check failed",
            f"{self._now_iso()} — the exposure check (trigger {trigger!r}) could "
            f"not run: {error}\n\nThe deny rules are NOT being enforced until "
            f"this clears. It retries on the next scheduled check.",
            fingerprint=f"check_failed|{error}",
        )

    def _publish_current_state_notification(
        self, title: str, message: str, *, fingerprint: str
    ) -> None:
        """Refresh the current-state notification; push to a phone only on change."""
        self.call_service(
            "persistent_notification/create",
            title=title,
            message=message,
            notification_id=self._notification_id,
        )
        self._notification_active = True
        if fingerprint != self._pushed_fingerprint:
            self._pushed_fingerprint = fingerprint
            self._push_mobile(title, message)

    def _push_mobile(self, title: str, message: str) -> None:
        if not self._notify_service:
            return
        self.call_service(self._notify_service, title=title, message=message)
        self.log(
            f"Mobile notification sent via {self._notify_service!r}",
            level="INFO",
        )

    def _clear_notification(self) -> None:
        """Clear the CURRENT-STATE notification only.

        The enforcement record under ``_enforced_notification_id`` is
        deliberately untouched: the clean run that reaches here is usually the
        *result* of an enforcement, and dismissing the record would erase the
        only evidence of it.
        """
        # Always reset the push dedup: the condition is over, so if it comes
        # back it is news again.
        self._pushed_fingerprint = ""
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
            "violating_entities": _capped_join(
                [violation.entity_id for violation in violations]
            ),
            # Durable: these describe an action already taken, so they must
            # survive the clean run that the action itself causes.
            "last_enforced": self._last_enforced,
            "last_enforced_entities": self._last_enforced_entities,
            "last_run": self._now_iso(),
            "last_trigger": trigger,
            "last_error": error or "none",
        }
        state = "unknown" if exposed_count is None else str(exposed_count)
        self.set_state(
            self._status_sensor,
            state=state,
            attributes=attributes,
        )
        self.log(
            f"Published {self._status_sensor} state={state} "
            f"violations={attributes['violations_last_run']}",
            level="DEBUG",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

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
