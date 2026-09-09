"""Shared auto-repair configuration machinery for the repairable checkers.

Seven checkers (device, device group, fan, spa, shade gateway, Protect,
network protocol) expose the same two HA helpers to the health dashboard
card:

* ``input_boolean.<checker_id>_health_auto_repair`` — the kill switch
* ``input_number.<checker_id>_health_auto_repair_delay`` — the dwell, minutes

Reading them, provisioning them, clamping the delay and applying a card
command are identical in all seven, so they live here rather than being
copy-pasted.  ``appdaemon/apps/`` may not hold shared libraries
(``.agents/rules/appdaemon-coding-guidelines.md``), with
``health_checks/shared/`` as the package's established exception — the same
place ``check_utils`` and ``alertmanager_bridge`` live.  Import it the same
way::

    _health_checks_root = str(Path(__file__).resolve().parents[2])
    if _health_checks_root not in sys.path:
        sys.path.insert(0, _health_checks_root)

    from shared.auto_repair_config import AutoRepairConfigMixin, UNAVAILABLE_STATES

Host-class contract
-------------------
The mixin assumes it is mixed into an ``appdaemon.plugins.hass.hassapi.Hass``
subclass that provides:

* ``self._checker_id`` — set before ``_init_auto_repair_config`` is called
* ``self._report_repair_status_only()`` — publish the repair state to the
  controller without re-running the checks
* ``self._repair_status`` and ``self._auto_repair_deadline`` — the repair
  ladder :meth:`_stand_down_pending_repair` stands down

Wiring, per checker::

    def initialize(self):
        ...
        self._init_auto_repair_config(self.args or {})

    async def _async_startup(self):
        ...
        await self._provision_auto_repair_helpers(prov)
        await self._refresh_auto_repair_config()

    def _on_repair_command(self, event_name, data, kwargs):
        ...
        elif action == "update_repair_config":
            self._handle_repair_config_command(data)

Why the helper can be unreadable at all
---------------------------------------
``ensure_helper`` creates the helper over the HA REST API at app startup.
AppDaemon does not learn about a new entity from that call: its local copy of
the plugin state is loaded once at connect and then re-fetched by the utility
loop every ``refresh_delay``
(``appdaemon/models/config/plugin.py:22`` — ``timedelta(minutes=10)`` by
default, not overridden in our ``appdaemon.yaml``; the refresh itself is
``plugin_management.py:512``, and ``state.update_namespace_state`` merges the
newly-seen entities in).  So between "helper created" and the next refresh,
``get_state`` returns ``None`` for it — a window of **up to ten minutes**, not
"the whole first run".

That window is still long enough to matter: ``str(None) == "on"`` is False, so
a naive read silently caches "auto-repair disabled".  That is how 1.17.0
shipped inert.  Two things close it:

1. :meth:`_provision_auto_repair_helpers` calls ``self.add_entity`` right
   after creating a helper (``adapi.py:794``), which inserts it into
   AppDaemon's local state immediately — so in practice the window is zero.
   It registers only a value Home Assistant confirmed, though: if the
   initial seed fails, nothing is registered and the full window is back.
   That is deliberate — see the comments there for why a guessed value is
   worse than an unreadable helper.
2. :meth:`_refresh_auto_repair_config` treats an unreadable read as *no
   evidence* and keeps the cached value — but only until the helper has been
   read successfully once.  After that, an unreadable read means the helper
   was deleted or went unavailable, and the kill switch **fails closed**
   (cached → ``False``) exactly as ``main`` behaved before the guard existed.
   There is no safe "closed" value for the delay, so the delay keeps its last
   good value either way.
"""

from __future__ import annotations

from typing import Any, Optional

#: States that mean "no usable reading", not a real value.
UNAVAILABLE_STATES = ("unavailable", "unknown", "none", "")

#: The three repair statuses :meth:`AutoRepairConfigMixin._stand_down_pending_repair`
#: has to reason about.
#:
#: They are *duplicated* here rather than imported. Each of the seven checkers
#: defines its own equal ``REPAIR_*`` constants at module scope, and its own
#: test module imports them from there; the canonical home is therefore
#: per-checker, and there is no single module the mixin could import from.
#: Importing any one checker would also drag ``hassapi`` and that checker's
#: ``sys.path`` bootstrap into ``shared/`` — a cycle, since every checker
#: imports ``shared.auto_repair_config``. Two string literals are the cheaper
#: coupling, and ``TestTheStandDownConstantsMatchEveryChecker`` in
#: ``test_auto_repair_config_contract.py`` fails the moment a checker's copy
#: drifts from these.
REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_SUCCESS = "success"


def _is_readable(state: Any) -> bool:
    """Whether a ``get_state`` answer is a real reading rather than a gap."""
    return state is not None and str(state).lower() not in UNAVAILABLE_STATES


class AutoRepairConfigMixin:
    """The auto-repair toggle/delay helpers: provision, read, clamp, apply.

    Subclasses override the four class attributes to describe their own delay
    helper.  Note that the bounds are used in two places — :meth:`_clamp_delay`
    and the ``ensure_helper`` call in
    :meth:`_provision_auto_repair_helpers` — so both read the same numbers,
    but ``ensure_helper`` is **create-only**: it never reconciles ``min`` /
    ``max`` / ``step`` on a helper that already exists.  Changing a bound here
    therefore does NOT change a helper already living in HA.  Edit that helper
    by hand (or delete it and let the next startup re-provision it) when you
    change these.
    """

    #: Bounds of the auto-repair delay helper, in minutes. Every path that can
    #: set the cached delay clamps to these: HA silently rejects a set_value
    #: outside the helper's range without AppDaemon raising, so an unclamped
    #: cache would hold a value the helper never accepted — and a delay of 0
    #: collapses the dwell gate entirely.
    DELAY_MIN_MIN = 1
    DELAY_MIN_MAX = 60
    #: ``step`` for the input_number helper.
    DELAY_STEP = 1
    #: Fallback when ``auto_repair_delay_min_default`` is absent or unparseable.
    DELAY_MIN_DEFAULT = 5
    #: Fallback when ``auto_repair_enabled_default`` is absent.
    AUTO_REPAIR_ENABLED_DEFAULT = False

    #: Default so :meth:`_stand_down_pending_repair` always has the attribute
    #: to write. Five checkers set their own in ``initialize()`` and publish
    #: it; the fan and device-group checkers derive their published detail
    #: from per-device state and simply never read this one.
    _repair_detail: str = ""

    # ------------------------------------------------------------------
    # Entity ids
    # ------------------------------------------------------------------

    def _auto_repair_toggle_entity(self) -> str:
        return f"input_boolean.{self._checker_id}_health_auto_repair"

    def _auto_repair_delay_entity(self) -> str:
        return f"input_number.{self._checker_id}_health_auto_repair_delay"

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_auto_repair_config(self, args: dict) -> None:
        """Set up the cached auto-repair config. Call from ``initialize()``."""
        args = args or {}

        self._auto_repair_enabled_default: bool = bool(
            args.get(
                "auto_repair_enabled_default", self.AUTO_REPAIR_ENABLED_DEFAULT
            )
        )

        #: Latches the out-of-range warning so the read path says it once per
        #: episode instead of every check cycle. Must exist before the first
        #: _clamp_delay call below.
        self._delay_clamped_logged: bool = False

        raw_default = args.get(
            "auto_repair_delay_min_default", self.DELAY_MIN_DEFAULT
        )
        parsed_default = self._parse_delay(raw_default)
        if parsed_default is None:
            self.log(
                f"Unparseable auto_repair_delay_min_default "
                f"{raw_default!r} — using {self.DELAY_MIN_DEFAULT}m",
                level="WARNING",
            )
            parsed_default = self.DELAY_MIN_DEFAULT
        # Clamped here so every path that can set the cached delay obeys the
        # helper's bounds, config included. loud=True: this runs exactly once,
        # so there is nothing to rate-limit, and it must not touch the read
        # path's once-per-episode latch (see _clamp_delay).
        self._auto_repair_delay_min_default: int = self._clamp_delay(
            parsed_default, loud=True
        )

        # Cached auto-repair config, refreshed each check cycle.
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

        #: None until the first read attempt; then whether it succeeded.
        #: Drives the transition-only logging in _refresh_auto_repair_config.
        self._toggle_readable: Optional[bool] = None
        self._delay_readable: Optional[bool] = None
        #: Whether each helper has EVER been read successfully.
        #:
        #: Both of these drive the vanished-helper guard in the card-command
        #: path, and there they mean the same thing for both helpers: a
        #: pre-read that comes back None is provisioning lag *before* the
        #: first sighting — the entity does exist, ``ensure_helper`` just made
        #: it, so the write is right — and a deleted helper *after* one, where
        #: writing is worse than useless because Home Assistant answers
        #: success for a service call that matched nothing and the cache would
        #: move on a write that changed nothing.
        #:
        #: What is NOT symmetric is the read path. Only the toggle fails
        #: **closed** when it loses a helper it had seen: a kill switch nobody
        #: can read must stop repairing. The delay has no such rule and never
        #: will — there is no safe "closed" delay, so an unreadable read
        #: always keeps the last good value. The two flags are therefore not
        #: interchangeable, and only one of them gates a fail-closed.
        self._toggle_ever_readable: bool = False
        self._delay_ever_readable: bool = False

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    async def _provision_auto_repair_helpers(self, prov: Any) -> None:
        """Create the toggle and delay helpers if they do not exist.

        *prov* is an :class:`providers.ha_provisioner.HAProvisioner`.  Both
        creations are best-effort and independent: a failure to make one must
        not skip the other.
        """
        toggle_entity = self._auto_repair_toggle_entity()
        try:
            created = await prov.ensure_helper(
                "input_boolean",
                f"{self._checker_id} Health Auto Repair",
            )
            if created:
                self.log(f"Provisioned {toggle_entity}", level="INFO")
                # AppDaemon does not learn about an entity created over the
                # REST API until its next full plugin-state refresh, so
                # register it locally now (see the module docstring) — AFTER
                # any seed, and only with a state HA actually confirmed.
                # Registering before the seed would read back "off" one line
                # later and start a default-on checker one check interval
                # inert (the 1.17.0 shape); registering the intended value
                # blind would trust a write HA may have rejected for up to
                # refresh_delay.
                if self._auto_repair_enabled_default:
                    # A freshly created input_boolean is off, so without this
                    # the repair would be provisioned and then never run. Only
                    # applied on creation — a later manual "off" is never
                    # overridden.
                    seeded_on = await self._seed_helper(
                        "input_boolean/turn_on",
                        toggle_entity,
                        f"Auto-repair default-enabled via {toggle_entity}",
                    )
                    # A FAILED seed registers nothing — the same rule the
                    # delay below has always followed. "off" would be a
                    # fabrication, not a truth: the commonest failure is a
                    # websocket TIMEOUT, where HA very probably DID execute
                    # turn_on and only the reply was lost. Registering that
                    # fiction caches auto-repair disabled, latches
                    # _toggle_ever_readable on it (so the next unreadable read
                    # fails closed on a lie) and says nothing in the log.
                    # Leaving the entity unregistered keeps it unreadable, so
                    # the configured default stands and every refresh WARNs
                    # "not readable" until the plugin refresh brings HA's
                    # truth.
                    if seeded_on:
                        await self._add_local_entity(toggle_entity, "on")
                else:
                    # Nothing was written, so nothing can have failed: a
                    # freshly created input_boolean really is off.
                    await self._add_local_entity(toggle_entity, "off")
        except Exception as exc:
            self.log(
                f"Failed to provision auto-repair toggle: {exc!r}", level="ERROR"
            )

        delay_entity = self._auto_repair_delay_entity()
        try:
            created = await prov.ensure_helper(
                "input_number",
                f"{self._checker_id} Health Auto Repair Delay",
                min=self.DELAY_MIN_MIN,
                max=self.DELAY_MIN_MAX,
                step=self.DELAY_STEP,
                unit_of_measurement="min",
                mode="box",
            )
            if created:
                self.log(f"Provisioned {delay_entity}", level="INFO")
                seeded = await self._seed_helper(
                    "input_number/set_value",
                    delay_entity,
                    f"Auto-repair delay default "
                    f"{self._auto_repair_delay_min_default}m set on "
                    f"{delay_entity}",
                    value=self._auto_repair_delay_min_default,
                )
                # Register locally only with a value HA confirmed. There is no
                # safe direction to guess a delay in, so a failed seed leaves
                # the entity unregistered: the read guard keeps the cached
                # default until the next plugin refresh brings the truth.
                if seeded:
                    await self._add_local_entity(
                        delay_entity, self._auto_repair_delay_min_default
                    )
        except Exception as exc:
            self.log(
                f"Failed to provision auto-repair delay: {exc!r}", level="ERROR"
            )

    async def _add_local_entity(self, entity_id: str, state: Any) -> None:
        """Register a just-created helper in AppDaemon's local state.

        ``adapi.add_entity`` only touches AppDaemon's own copy of the
        namespace (``state.py:600``), which is exactly what is stale here.
        Best-effort: without it the helper is merely unreadable until the next
        ``refresh_delay``, which the read guard already survives.
        """
        try:
            await self.add_entity(entity_id, state)
        except Exception as exc:
            self.log(
                f"Could not register {entity_id} in AppDaemon's local state "
                f"({exc!r}) — it stays unreadable until the next plugin "
                f"state refresh",
                level="DEBUG",
            )

    async def _seed_helper(
        self, service: str, entity_id: str, success_msg: str, **data: Any
    ) -> bool:
        """Write a just-created helper's initial value; True if HA accepted it."""
        try:
            result = await self.call_service(service, entity_id=entity_id, **data)
        except Exception as exc:
            self.log(
                f"Failed to seed {entity_id} via {service}: {exc!r}",
                level="WARNING",
            )
            return False
        if self._service_ok(result):
            self.log(success_msg, level="INFO")
            return True
        self.log(
            f"Home Assistant did not accept the initial value for "
            f"{entity_id} ({service}, result={result!r})",
            level="WARNING",
        )
        return False

    # ------------------------------------------------------------------
    # Service-call result contract
    # ------------------------------------------------------------------

    def _service_ok(self, result: Any) -> bool:
        """Whether an **awaited** ``call_service`` actually reached HA.

        Evidence, read from the installed AppDaemon 4.5.13 in the production
        pod (``/usr/local/lib/python3.12/site-packages/appdaemon``):

        * ``adapi.py:1925`` — ``call_service`` is wrapped in
          ``utils.sync_decorator``.  On the event loop that returns an
          ``asyncio.Task`` (``utils.py:393-396``), so an un-awaited call still
          runs — which is why the fire-and-forget call sites elsewhere in
          these checkers are correct.  Awaiting it yields the value below.
        * ``adapi.py:2022`` → ``services.py:280`` → the hass plugin's
          ``call_plugin_service`` (``hassplugin.py:719``) →
          ``websocket_send_json`` (``hassplugin.py:354``).
        * **Success**: ``hassplugin.py:436`` returns Home Assistant's
          websocket result envelope with ``ad_status`` merged in, i.e.
          ``{"id": N, "type": "result", "success": True, "result": {...},
          "ad_status": "OK", "ad_duration": 0.01}``.
        * **Websocket timeout**: ``hassplugin.py:422-426`` — ``{"success":
          False, "ad_status": "TIMEOUT", ...}``.  ``TERMINATING`` is the same
          shape during shutdown (``:427-431``).
        * **HA rejected the write** (e.g. a set_value outside the helper's
          range): HA answers ``success: False`` with an ``error`` dict, which
          is returned verbatim plus ``ad_status: "OK"``.
        * **Plugin disconnected**: ``call_plugin_service`` carries
          ``@hass_check`` (``hassplugin.py:718``), which swallows the call and
          returns ``None`` (``plugins/hass/utils.py:34-48``);
          ``websocket_send_json`` also returns ``None`` when the connect event
          is clear (``:379-381``).  ``services.py``'s ``warning_decorator``
          likewise returns ``None`` after an unexpected exception.

        So ``None`` is never a normal success for a Home Assistant service
        call — every path that produces it is a failure — and the only
        positive evidence of success is a dict that does not say
        ``success: False``.  Non-dict, non-None returns (``database/history``
        returns a list) are accepted rather than guessed at.
        """
        if result is None:
            return False
        if isinstance(result, dict):
            if result.get("success") is False:
                return False
            status = result.get("ad_status")
            if status is not None and status != "OK":
                return False
        return True

    # ------------------------------------------------------------------
    # Reading the helpers
    # ------------------------------------------------------------------

    async def _refresh_auto_repair_config(self) -> None:
        """Refresh the cached toggle/delay from their HA helpers.

        See the module docstring for why a read can come back ``None`` and
        what the guard does about it.  The two helpers are read in separate
        ``try`` blocks so a failure on one still refreshes the other.
        """
        entity_id = self._auto_repair_toggle_entity()
        try:
            enabled_state = await self.get_state(entity_id)
            readable = _is_readable(enabled_state)
            if readable:
                self._cached_auto_repair_enabled = (
                    str(enabled_state).lower() == "on"
                )
                self._toggle_ever_readable = True
            elif self._toggle_ever_readable:
                # The helper was readable and now is not: it has been deleted
                # or gone unavailable. A kill switch that cannot be read must
                # fail closed — the cached "on" would otherwise keep repairing
                # with no way for anyone to stop it.
                self._cached_auto_repair_enabled = False
            # Log the transitions, not every cycle: an operator needs the
            # window to have a visible open and close, without a message
            # every check_interval_s for as long as it lasts.
            if readable != self._toggle_readable:
                if readable and self._toggle_readable is None:
                    # A clean start with a readable helper is not the *close*
                    # of an unreadable window — only announce that if one was
                    # open.
                    pass
                elif readable:
                    self.log(
                        f"{entity_id} is readable again — auto-repair "
                        f"{'enabled' if self._cached_auto_repair_enabled else 'disabled'} "
                        f"from the helper",
                        level="INFO",
                    )
                elif self._toggle_ever_readable:
                    self.log(
                        f"{entity_id} was readable and is not any more "
                        f"(state={enabled_state!r}) — auto-repair forced OFF "
                        f"until the helper comes back",
                        level="WARNING",
                    )
                else:
                    self.log(
                        f"{entity_id} not readable (state={enabled_state!r}) — "
                        f"running on the cached default, auto-repair "
                        f"{'enabled' if self._cached_auto_repair_enabled else 'disabled'}",
                        level="WARNING",
                    )
                self._toggle_readable = readable
        except Exception as exc:
            self.log(f"Failed to read auto-repair toggle: {exc!r}", level="WARNING")

        entity_id = self._auto_repair_delay_entity()
        try:
            delay_state = await self.get_state(entity_id)
            parsed = (
                self._parse_delay(delay_state)
                if _is_readable(delay_state)
                else None
            )
            delay_readable = parsed is not None
            if delay_readable:
                self._cached_auto_repair_delay_min = self._clamp_delay(parsed)
                self._delay_ever_readable = True
            # No fail-closed counterpart: there is no safe "closed" delay, so
            # an unreadable read always keeps the last good value.
            if delay_readable != self._delay_readable:
                if delay_readable and self._delay_readable is None:
                    pass
                elif delay_readable:
                    self.log(
                        f"{entity_id} is readable again — auto-repair delay "
                        f"{self._cached_auto_repair_delay_min}m from the helper",
                        level="INFO",
                    )
                else:
                    self.log(
                        f"{entity_id} not readable (state={delay_state!r}) — "
                        f"using the cached default of "
                        f"{self._cached_auto_repair_delay_min}m",
                        level="WARNING",
                    )
                self._delay_readable = delay_readable
        except Exception as exc:
            self.log(f"Failed to read auto-repair delay: {exc!r}", level="WARNING")

    def _read_auto_repair_config(self) -> tuple[bool, int]:
        """Return the cached ``(enabled, delay_min)`` — sync-safe."""
        return self._cached_auto_repair_enabled, self._cached_auto_repair_delay_min

    def _auto_repair_state_fields(self) -> dict:
        """The auto-repair slice of ``repair_state``, for every checker.

        Merged into each checker's ``_build_repair_state`` with
        ``**self._auto_repair_state_fields()``.

        ``auto_repair_enabled`` and ``auto_repair_delay_min`` keep exactly the
        names and types they have always had — the dashboard card and the
        Shepherd runbooks read them.  ``auto_repair_delay_bounds`` is the
        addition: the card used to hard-code ``min=1 max=60 step=1`` on its
        delay input, which is right for six checkers and wrong for the shade
        gateway (15/360/15).  From the card a spinner nudge on the shade row
        therefore clamped 120 down to 60, and nothing above 60 could be typed
        at all.  The backend already clamped to the real bounds, so the loss
        was the card's alone — publishing them is what lets it render the
        input the checker actually accepts.

        The AppDaemon ``set_state`` attribute gotcha (falsy values are dropped,
        ``True`` becomes ``"true"``) does not bite here: every bound is a
        non-zero int.  A ``0`` bound would silently vanish from the published
        attributes and the card would fall back — but ``0`` is not a legal
        bound for any of these (a zero delay collapses the dwell gate, which
        is why ``DELAY_MIN_MIN`` is 1 at its lowest).
        """
        enabled, delay_min = self._read_auto_repair_config()
        return {
            "auto_repair_enabled": enabled,
            "auto_repair_delay_min": delay_min,
            "auto_repair_delay_bounds": {
                "min": self.DELAY_MIN_MIN,
                "max": self.DELAY_MIN_MAX,
                "step": self.DELAY_STEP,
            },
        }

    # ------------------------------------------------------------------
    # Delay parsing and clamping
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_delay(value: Any) -> Optional[int]:
        """Parse a delay to whole minutes, or ``None`` if it is not a number.

        ``int(float(...))`` accepts the ``"17.0"`` an input_number read (or a
        card command) produces.  ``float("inf")`` parses and then raises
        ``OverflowError`` on ``int()``; ``float("nan")`` raises ``ValueError``.
        Both are hand-editable helper values, so both are caught.
        """
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None

    def _clamp_delay(self, value: int, *, loud: bool = False) -> int:
        """Clamp a delay to the helper's bounds, saying so when it bites.

        logging-standards puts "validation failure with fallback" at WARNING,
        and overriding what an operator asked for must not be silent.  But the
        helper read runs every check cycle, so an out-of-range value sitting in
        the helper would warn every ``check_interval_s`` for as long as it sat
        there.  The read path therefore latches the warning once per
        out-of-range episode (``_delay_clamped_logged``, cleared as soon as an
        in-range value is seen).

        ``loud=True`` bypasses the latch: it is used by the card command path
        (an operator has just typed a value and must always be told it was
        overridden) and by the one-shot clamp of the configured default in
        :meth:`_init_auto_repair_config`.

        The latch itself belongs to the read path alone. A ``loud`` clamp must
        neither set nor clear it: an out-of-range ``auto_repair_delay_min_default``
        in the app YAML would otherwise latch at startup and permanently
        silence the once-per-episode warning the read path owes the operator
        about the *helper's* value — and a single out-of-range value typed
        into the card would do the same until the next in-range read.
        """
        clamped = max(self.DELAY_MIN_MIN, min(self.DELAY_MIN_MAX, value))
        out_of_range = clamped != value
        if out_of_range and (loud or not self._delay_clamped_logged):
            self.log(
                f"Auto-repair delay {value}m is outside the permitted "
                f"{self.DELAY_MIN_MIN}-{self.DELAY_MIN_MAX}m range — "
                f"using {clamped}m",
                level="WARNING",
            )
        if not loud:
            self._delay_clamped_logged = out_of_range
        return clamped

    # ------------------------------------------------------------------
    # Standing a repair down
    # ------------------------------------------------------------------

    def _stand_down_pending_repair(self, reason: str) -> None:
        """Drop a countdown (or a stale success) that can no longer be honoured.

        Called from every place auto-repair stops being allowed to act: each
        checker's ``_evaluate_auto_repair`` when the toggle reads off, and
        :meth:`_apply_toggle_command` the moment an operator unchecks it from
        the card. All seven used to carry their own copy of this, and the card
        path had none at all — so unchecking the toggle left a countdown and a
        Cancel button on screen, and the page held, for up to a full check
        interval.

        Why ``success`` stands down too, not just ``pending``: both are
        ``alertmanager_bridge`` *repair-hold* states, and the bridge withholds
        the critical page for up to ``repair_hold_cap_s`` (1800 s) while
        either is up. A ``pending`` counting down to a repair that can never
        start, or a ``success`` from a repair that is no longer allowed to be
        repeated, would each suppress the page for exactly the outage the
        operator has just said will not self-heal.

        ``failed`` and ``in_progress`` are left alone on purpose: the bridge
        releases on ``failed``, and both carry a detail a human needs (the
        24 h cap message, "Restarting ESP32…") that this reason must not
        overwrite. ``failed`` also owns ``_auto_repair_deadline`` as its
        backoff retry time in the Protect checker, so clearing it there would
        re-arm an immediate retry.

        The reason is still recorded when the checker is already ``idle`` —
        that is the network checker's ``_hold`` semantics, which this replaces
        at the auto-repair-disabled call site: the card's detail line should
        say *why* nothing is happening.
        """
        if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS):
            if self._repair_status == REPAIR_PENDING:
                self.log(f"{reason} — cancelling pending auto-repair", level="INFO")
            else:
                self.log(f"{reason} — clearing stale repair success", level="INFO")
            self._repair_status = REPAIR_IDLE
            self._auto_repair_deadline = None
        if self._repair_status == REPAIR_IDLE:
            self._repair_detail = reason

    # ------------------------------------------------------------------
    # Card commands
    # ------------------------------------------------------------------

    def _handle_repair_config_command(self, data: dict) -> None:
        """Sync entry point for the ``update_repair_config`` event arm."""
        self.create_task(self._update_repair_config(data))

    async def _update_repair_config(self, data: dict) -> None:
        """Apply an ``update_repair_config`` command from the dashboard card.

        Async because every ``get_state``/``call_service`` here has to be
        awaited: the cache may only be updated once the write is known to have
        landed, and ``_service_ok`` can only judge an awaited result.

        The cache is mirrored from the command — rather than waiting for the
        next helper read — because during the provisioning window that read
        can still be ``None``.  An operator turning auto-repair off would
        otherwise see it off in the card and in HA while the checker kept
        repairing.  Caching a write that *failed* is the mirror-image bug, so
        the cache update lives strictly in the success paths.
        """
        auto_enabled = data.get("auto_repair_enabled")
        delay_min = data.get("auto_repair_delay_min")
        self.log(
            f"Repair config update requested: "
            f"auto_repair_enabled={auto_enabled}, "
            f"auto_repair_delay_min={delay_min}",
            level="INFO",
        )

        if auto_enabled is not None:
            await self._apply_toggle_command(bool(auto_enabled))

        if delay_min is not None:
            await self._apply_delay_command(delay_min)

        # Publish immediately. The controller's copy of repair_state is only
        # refreshed on report_status, so without this the card re-renders from
        # the stale sensor for up to a check interval and the toggle appears
        # to spring back.
        self._report_repair_status_only()

    async def _apply_toggle_command(self, desired_enabled: bool) -> None:
        entity_id = self._auto_repair_toggle_entity()
        desired = "on" if desired_enabled else "off"

        try:
            current = await self.get_state(entity_id)
        except Exception as exc:
            self.log(
                f"Could not read {entity_id} before writing it: {exc!r}",
                level="WARNING",
            )
            current = None

        if _is_readable(current):
            # A read that came back with a real value is a sighting, whatever
            # it said. Without this the card path could set the cache while
            # _toggle_ever_readable stayed False, and a later deleted helper
            # would keep the cached "on" instead of failing closed.
            self._toggle_ever_readable = True

        if current is not None and str(current).lower() == desired:
            # Already where the operator wants it — no service call needed,
            # and the read itself is proof enough to cache.
            self._settle_toggle(desired_enabled)
            self.log(
                f"Auto-repair {'enabled' if desired_enabled else 'disabled'} "
                f"({entity_id} was already {desired})",
                level="INFO",
            )
            return

        if current is None and self._toggle_ever_readable:
            # The helper was readable and now is not: it has been deleted (or
            # gone unavailable), and _refresh_auto_repair_config has already
            # failed the cache closed. Firing the service anyway would be
            # worse than useless — Home Assistant answers *success* for an
            # entity service call that matched nothing. Read from the HA in
            # the production pod (2026.6.1,
            # homeassistant/helpers/service.py:767-807): unmatched ids go to
            # `referenced.log_missing(missing, _LOGGER)`, a log line and
            # nothing else, and the empty candidate list then `return None`
            # without raising. So _service_ok would say yes and the cache
            # would be updated for a write that changed nothing at all.
            # Write nothing, cache nothing, and say so.
            self.log(
                f"{entity_id} is not available — skipping update",
                level="WARNING",
            )
            return
        # current is None and the helper has never been seen: that is
        # provisioning lag, not a vanished helper (the entity does exist in
        # HA — ensure_helper just made it), so the write below is right.

        service = (
            "input_boolean/turn_on" if desired_enabled else "input_boolean/turn_off"
        )
        try:
            ok = self._service_ok(
                await self.call_service(service, entity_id=entity_id)
            )
        except Exception as exc:
            self.log(
                f"Failed to update auto-repair toggle: {exc!r}", level="ERROR"
            )
            return

        if ok:
            self._settle_toggle(desired_enabled)
            self.log(
                f"Auto-repair {'enabled' if desired_enabled else 'disabled'}",
                level="INFO",
            )
        else:
            self.log(
                f"Home Assistant did not accept {service} on {entity_id} — "
                f"auto-repair left "
                f"{'enabled' if self._cached_auto_repair_enabled else 'disabled'}",
                level="ERROR",
            )

    def _settle_toggle(self, desired_enabled: bool) -> None:
        """Cache a toggle write HA has confirmed, and act on a switch-off.

        Turning auto-repair off has to stand a countdown down here and not
        merely at the next ``_evaluate_auto_repair``: ``_update_repair_config``
        republishes immediately afterwards, so without this the card would
        re-render a live countdown and a Cancel button next to a box the
        operator has just unchecked, and ``alertmanager_bridge`` would keep
        holding the page, for up to a whole check interval.
        """
        self._cached_auto_repair_enabled = desired_enabled
        if not desired_enabled:
            self._stand_down_pending_repair("Auto-repair disabled")

    async def _apply_delay_command(self, raw_delay: Any) -> None:
        entity_id = self._auto_repair_delay_entity()

        parsed = self._parse_delay(raw_delay)
        if parsed is None:
            self.log(
                f"Ignoring unparseable auto_repair_delay_min: {raw_delay!r}",
                level="WARNING",
            )
            return
        desired_delay = self._clamp_delay(parsed, loud=True)

        try:
            current_state = await self.get_state(entity_id)
        except Exception as exc:
            self.log(
                f"Could not read {entity_id} before writing it: {exc!r}",
                level="WARNING",
            )
            current_state = None

        current_val = self._parse_delay(current_state)
        if current_val is not None:
            # A pre-read that came back with a real number is a sighting, the
            # same way the toggle's is — the card path reads these helpers too,
            # and a helper first seen here must still count as seen.
            self._delay_ever_readable = True

        if current_val == desired_delay:
            self._cached_auto_repair_delay_min = desired_delay
            self.log(
                f"Auto-repair delay {desired_delay}m set "
                f"({entity_id} already held it)",
                level="INFO",
            )
            return

        if current_state is None and self._delay_ever_readable:
            # Deleted (or gone unavailable) since we last read it — the same
            # trap as the toggle: HA answers success for a set_value that
            # matched no entity, so _service_ok would say yes and the cache
            # would move on a write that changed nothing. Keyed on the raw
            # state, not on current_val: an input_number holding something
            # unparseable still exists and should be written to.
            self.log(
                f"{entity_id} is not available — skipping update",
                level="WARNING",
            )
            return
        # current_state is None and never seen: provisioning lag, so write.

        try:
            ok = self._service_ok(
                await self.call_service(
                    "input_number/set_value",
                    entity_id=entity_id,
                    value=desired_delay,
                )
            )
        except Exception as exc:
            self.log(f"Failed to update auto-repair delay: {exc!r}", level="ERROR")
            return

        if ok:
            self._cached_auto_repair_delay_min = desired_delay
            self.log(f"Auto-repair delay {desired_delay}m set", level="INFO")
        else:
            self.log(
                f"Home Assistant did not accept input_number/set_value on "
                f"{entity_id} — auto-repair delay left at "
                f"{self._cached_auto_repair_delay_min}m",
                level="ERROR",
            )
