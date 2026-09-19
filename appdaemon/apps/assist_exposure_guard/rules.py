"""Pure deny-rule engine for the Assist exposure guard.

Deliberately free of AppDaemon, Home Assistant and network imports: the rules
here are the security boundary for the LLM voice agents, so they are plain
functions over plain data that can be unit-tested exhaustively without a
running system.

The input is one :class:`ExposedEntity` per entity currently exposed to the
``conversation`` assistant; the output is one :class:`Violation` per entity
that must not be.  Every entity produces **at most one** violation — the first
rule that matches, in the order below — so a notification reads as one line
per entity with the most specific reason available:

1. ``allow_entities``      — explicit per-entity override, beats every deny rule
2. ``deny_domains``        — whole domains that must never be exposed
3. ``deny_cover_device_classes`` — garage/gate/door covers ("open the garage")
4. ``deny_integrations``   — every entity from a given integration (registry ``platform``)
5. ``deny_entity_globs``   — ``fnmatch`` patterns over the entity id
6. ``switch_allowlist``    — the ``switch`` domain is DENY BY DEFAULT
7. ``script_allowlist_globs`` — an exposed script is an unrestricted LLM tool,
   so the shipped default names every allowed script explicitly

Why these defaults (ruling by the owner, 2026-09-18): HA's
``OnOffIntentHandler`` maps ``HassTurnOff`` on a lock to ``lock.unlock`` and
``HassTurnOn`` on a cover to ``cover.open_cover``, with no PIN and no
confirmation, and any exposed script is a tool the model may call with
whatever privileges that script has.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Defaults — mirrored in the app README and in apps-prod.yaml comments
# ---------------------------------------------------------------------------

#: Domains that must never reach a voice agent.  ``lock`` and ``cover`` are
#: directly actuatable through intents; ``camera``/``siren``/``alarm_control_panel``
#: are privacy/safety devices; ``button``/``input_button`` are single-press
#: actuators with no state to reason about (the ratgdo ``*_toggle_door``
#: buttons live here); ``automation``/``update``/``number``/``select`` and the
#: ``input_*`` helpers let a model reconfigure the house rather than operate it
#: (mode toggles, off-delays, hold colours).  ``scene`` is an arbitrary
#: state-applier like a script — ``HassTurnOn`` reaches ``scene.turn_on``, a
#: scene can reproduce a ``lock`` state, and HA auto-exposes the domain when
#: "expose new entities" is on.  Voice reaches scenes only through a curated
#: script (``script.voice_shades``); allow a single scene or helper by name
#: with ``allow_entities``.
DEFAULT_DENY_DOMAINS: Tuple[str, ...] = (
    "lock",
    "alarm_control_panel",
    "siren",
    "camera",
    "button",
    "input_button",
    "valve",
    "water_heater",
    "automation",
    "update",
    "number",
    "select",
    "lawn_mower",
    "scene",
    "input_boolean",
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
)

#: ``cover`` as a domain is allowed (window shades are a sensible voice
#: target), but these device classes are physical-security openings.
DEFAULT_DENY_COVER_DEVICE_CLASSES: Tuple[str, ...] = ("garage", "gate", "door")

#: Integrations (entity-registry ``platform``) that are denied wholesale —
#: pool/spa control surfaces where a wrong call is expensive or unsafe.
DEFAULT_DENY_INTEGRATIONS: Tuple[str, ...] = ("intellicenter", "gecko")

#: ``fnmatch`` patterns over the full entity id.  These are named devices
#: whose "on/off" is a network outage, an appliance mid-cycle, a camera's
#: privacy shutter, or a charging vehicle.
DEFAULT_DENY_ENTITY_GLOBS: Tuple[str, ...] = (
    "switch.power_distribution_*",
    "switch.usp_pdu_pro_*",
    "switch.*ebike*",
    "switch.*mombike*",
    "switch.dryer_power",
    "switch.washer_power",
    "switch.*printer*",
    "switch.server_room_ac_power_switch",
    "switch.unifi_network_*",
    "switch.*unifi_network_*",
    "switch.zigbee2mqtt_bridge_permit_join",
    "switch.*_privacy_mode",
    "switch.*_detections_*",
    "switch.ratgdov25i_*",
    # Garage opener bulbs: `light` is an allowed domain, but these are part of the opener.
    "light.ratgdov25i_*",
    "switch.spa_intouch3_switch",
    "switch.nrz120804q_*",
)

#: The ``switch`` domain is deny-by-default: a switch is exposed only by name.
#: Empty by default — populate it in apps.yaml as rooms are curated.
DEFAULT_SWITCH_ALLOWLIST: Tuple[str, ...] = ()

#: An exposed script is an unrestricted tool: whatever the script does, the
#: model can do.  So every exposed script is named **explicitly** — no
#: patterns.  A glob here would make a filename the security boundary: anyone
#: who later creates ``script.voice_<anything>`` would hand the voice agent a
#: tool nobody reviewed.  These sixteen are hand-curated and reviewed; adding a
#: seventeenth means adding it to this list in the same PR that creates it.
#:
#: The key is still matched with ``fnmatch``, so an operator *can* configure a
#: pattern — the shipped default simply does not use that power.
DEFAULT_SCRIPT_ALLOWLIST_GLOBS: Tuple[str, ...] = (
    # Basement room-mode tools (movie room)
    "script.voice_movie_room_bright",
    "script.voice_movie_room_dim",
    "script.voice_movie_room_red_night_mode",
    "script.voice_movie_room_ambient_scene",
    "script.voice_movie_room_color_toggle",
    # Basement room-mode tools (rumpus room)
    "script.voice_rumpus_room_bright",
    "script.voice_rumpus_room_dim",
    "script.voice_rumpus_room_color_toggle",
    # Parameterised Hunter Douglas gateway scene runner
    "script.voice_shades",
    "script.voice_primary_bathroom_lights_on",
    "script.voice_primary_bathroom_lights_off",
    "script.voice_primary_bathroom_shower_lights",
    "script.voice_cloffice_bright",
    "script.voice_lock_all_doors",
    "script.voice_close_garage_doors",
    # Music Assistant request handler
    "script.llm_script_for_music_assistant_voice_requests",
    # Primary bedroom modes
    "script.kellie_mobile_primary_bedroom_relaxed",
    "script.kellie_mobile_primary_bedroom_focused",
    "script.kellie_mobile_primary_bedroom_bedtime",
    "script.kellie_mobile_primary_bedroom_sleep",
)

#: Per-entity escape hatch that beats every deny rule.  Empty by default.
DEFAULT_ALLOW_ENTITIES: Tuple[str, ...] = ()

# Rule identifiers, so callers can group/count violations without parsing prose.
RULE_DOMAIN = "deny_domains"
RULE_COVER_DEVICE_CLASS = "deny_cover_device_classes"
RULE_INTEGRATION = "deny_integrations"
RULE_ENTITY_GLOB = "deny_entity_globs"
RULE_SWITCH_DEFAULT_DENY = "switch_allowlist"
RULE_SCRIPT_ALLOWLIST = "script_allowlist_globs"

COVER_DOMAIN = "cover"
SWITCH_DOMAIN = "switch"
SCRIPT_DOMAIN = "script"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExposedEntity:
    """One entity currently exposed to the assistant.

    ``platform`` is the entity-registry integration (empty when the entity has
    no registry entry); ``device_class`` comes from entity *state*, because the
    registry's partial dict does not carry it.  Both default to empty so a
    caller that cannot resolve them still gets the domain/glob/allowlist rules.
    """

    entity_id: str
    platform: str = ""
    device_class: str = ""


@dataclass(frozen=True)
class Violation:
    """An exposed entity that the deny rules say must not be exposed."""

    entity_id: str
    rule: str
    reason: str


@dataclass(frozen=True)
class GuardRules:
    """Resolved, normalised deny configuration."""

    deny_domains: Tuple[str, ...] = DEFAULT_DENY_DOMAINS
    deny_cover_device_classes: Tuple[str, ...] = DEFAULT_DENY_COVER_DEVICE_CLASSES
    deny_integrations: Tuple[str, ...] = DEFAULT_DENY_INTEGRATIONS
    deny_entity_globs: Tuple[str, ...] = DEFAULT_DENY_ENTITY_GLOBS
    switch_allowlist: Tuple[str, ...] = DEFAULT_SWITCH_ALLOWLIST
    script_allowlist_globs: Tuple[str, ...] = DEFAULT_SCRIPT_ALLOWLIST_GLOBS
    allow_entities: Tuple[str, ...] = DEFAULT_ALLOW_ENTITIES

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]] = None) -> "GuardRules":
        """Build rules from an app-YAML mapping, falling back to the defaults.

        A key present but empty (``switch_allowlist: []``) is honoured as an
        empty list rather than replaced by the default — that distinction is
        what lets an operator deliberately clear an allowlist.
        """
        config = config or {}
        return cls(
            deny_domains=_normalise(config, "deny_domains", DEFAULT_DENY_DOMAINS),
            deny_cover_device_classes=_normalise(
                config, "deny_cover_device_classes", DEFAULT_DENY_COVER_DEVICE_CLASSES
            ),
            deny_integrations=_normalise(
                config, "deny_integrations", DEFAULT_DENY_INTEGRATIONS
            ),
            deny_entity_globs=_normalise(
                config, "deny_entity_globs", DEFAULT_DENY_ENTITY_GLOBS
            ),
            switch_allowlist=_normalise(
                config, "switch_allowlist", DEFAULT_SWITCH_ALLOWLIST
            ),
            script_allowlist_globs=_normalise(
                config, "script_allowlist_globs", DEFAULT_SCRIPT_ALLOWLIST_GLOBS
            ),
            allow_entities=_normalise(config, "allow_entities", DEFAULT_ALLOW_ENTITIES),
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_entity(entity: ExposedEntity, rules: GuardRules) -> Optional[Violation]:
    """Return the first rule ``entity`` violates, or ``None`` if it is allowed."""
    entity_id = (entity.entity_id or "").strip().lower()
    if not entity_id:
        return None

    if entity_id in rules.allow_entities:
        return None

    # Derive the domain from the normalised id so a stray upper-case entity id
    # cannot slip past a deny rule.
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain in rules.deny_domains:
        return Violation(
            entity_id=entity_id,
            rule=RULE_DOMAIN,
            reason=f"domain {domain!r} is never exposed to Assist",
        )

    device_class = (entity.device_class or "").strip().lower()
    if domain == COVER_DOMAIN and device_class in rules.deny_cover_device_classes:
        return Violation(
            entity_id=entity_id,
            rule=RULE_COVER_DEVICE_CLASS,
            reason=f"cover device_class {device_class!r} can be opened by voice",
        )

    platform = (entity.platform or "").strip().lower()
    if platform and platform in rules.deny_integrations:
        return Violation(
            entity_id=entity_id,
            rule=RULE_INTEGRATION,
            reason=f"integration {platform!r} is never exposed to Assist",
        )

    pattern = _first_match(entity_id, rules.deny_entity_globs)
    if pattern is not None:
        return Violation(
            entity_id=entity_id,
            rule=RULE_ENTITY_GLOB,
            reason=f"matches denied pattern {pattern!r}",
        )

    if domain == SWITCH_DOMAIN and entity_id not in rules.switch_allowlist:
        # Deny by default: a switch is exposed by name, never by area.
        return Violation(
            entity_id=entity_id,
            rule=RULE_SWITCH_DEFAULT_DENY,
            reason="switches are deny-by-default and it is not in switch_allowlist",
        )

    if domain == SCRIPT_DOMAIN and _first_match(entity_id, rules.script_allowlist_globs) is None:
        return Violation(
            entity_id=entity_id,
            rule=RULE_SCRIPT_ALLOWLIST,
            reason="an exposed script is an unrestricted tool and it matches no script_allowlist_globs",
        )

    return None


def evaluate(entities: Iterable[ExposedEntity], rules: GuardRules) -> List[Violation]:
    """Evaluate every exposed entity, preserving input order."""
    violations: List[Violation] = []
    for entity in entities:
        violation = evaluate_entity(entity, rules)
        if violation is not None:
            violations.append(violation)
    return violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_match(entity_id: str, patterns: Sequence[str]) -> Optional[str]:
    """Return the first ``fnmatch`` pattern matching ``entity_id``.

    ``fnmatchcase`` rather than ``fnmatch``: the latter applies
    ``os.path.normcase``, which is a no-op on Linux but lowercases on Windows,
    so a rule would behave differently between the dev machine and the pod.
    Entity ids are already lowercased by the callers here.
    """
    for pattern in patterns:
        if fnmatchcase(entity_id, pattern):
            return pattern
    return None


def _normalise(
    config: Mapping[str, Any], key: str, default: Tuple[str, ...]
) -> Tuple[str, ...]:
    """Read ``key`` as a lowercased, de-duplicated tuple of non-empty strings."""
    if key not in config:
        return default
    raw = config.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return default

    seen: List[str] = []
    for item in raw:
        value = str(item).strip().lower()
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)
