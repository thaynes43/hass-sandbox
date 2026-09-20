"""Unit tests for the Assist exposure guard.

Two halves:

* ``rules.py`` is pure, so it is tested directly with plain data — every deny
  rule class, the allow override, and config normalisation.
* ``AssistExposureGuard`` is tested with AppDaemon mocked out and a fake
  exposure client, so no HA, no WebSocket, no token.

``get_state`` is installed as an ``AsyncMock`` on purpose: the app awaits it,
and a plain ``MagicMock`` would make ``await`` raise, silently pushing the code
down its ``except`` branch and letting the assertions pass vacuously.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_appdaemon_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_appdaemon_root / "apps"))
sys.path.insert(0, str(_appdaemon_root))

from assist_exposure_guard.assist_exposure_guard import (  # noqa: E402
    MAX_DETAIL_LINES,
    AssistExposureGuard,
)
from assist_exposure_guard.rules import (  # noqa: E402
    DEFAULT_DENY_DOMAINS,
    DEFAULT_DENY_ENTITY_GLOBS,
    RULE_COVER_DEVICE_CLASS,
    RULE_DOMAIN,
    RULE_ENTITY_GLOB,
    RULE_INTEGRATION,
    RULE_SCRIPT_ALLOWLIST,
    RULE_SWITCH_DEFAULT_DENY,
    ExposedEntity,
    GuardRules,
    evaluate,
    evaluate_entity,
)
from providers.ha_provisioner.exposure_client import ExposureChange  # noqa: E402

DEFAULT_RULES = GuardRules.from_config({})


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# rules.py — pure deny-rule engine
# ===========================================================================


@pytest.mark.parametrize("domain", DEFAULT_DENY_DOMAINS)
def test_every_default_deny_domain_is_a_violation(domain: str) -> None:
    violation = evaluate_entity(ExposedEntity(f"{domain}.anything"), DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_DOMAIN
    assert domain in violation.reason


@pytest.mark.parametrize(
    "entity_id",
    [
        "light.upstairs_primary_bed_lights",
        "media_player.kitchen",
        "climate.second_floor_ecobee",
        "fan.primary_bedroom_fan_fan",
        "sensor.primary_bedroom_wave_plus_temperature",
        "todo.shopping_list",
        "weather.forecast_home",
        "binary_sensor.front_door_is_locked",
    ],
)
def test_benign_domains_are_allowed(entity_id: str) -> None:
    assert evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES) is None


@pytest.mark.parametrize(
    "entity_id",
    [
        # A scene is a state-applier like a script: it can reproduce a lock state,
        # and HA auto-exposes the domain when "expose new entities" is on.
        "scene.laundry_gateway_main_bedroom_open",
        "scene.unlock_everything",
        "input_boolean.cleaners_mode",
        "input_select.basement_movie_room_occupancy_off_delay",
        "input_number.outdoor_light_inovelli_led_color",
        "input_text.inovelli_manual_hold",
    ],
)
def test_scenes_and_input_helpers_are_denied_by_default(entity_id: str) -> None:
    violation = evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_DOMAIN


def test_a_single_scene_can_be_allowed_by_name() -> None:
    rules = GuardRules.from_config({"allow_entities": ["scene.movie_night"]})
    assert evaluate_entity(ExposedEntity("scene.movie_night"), rules) is None
    assert evaluate_entity(ExposedEntity("scene.other"), rules) is not None


@pytest.mark.parametrize("device_class", ["garage", "gate", "door"])
def test_dangerous_cover_device_classes_are_denied(device_class: str) -> None:
    entity = ExposedEntity("cover.ratgdov25i_4a0325_door", device_class=device_class)
    violation = evaluate_entity(entity, DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_COVER_DEVICE_CLASS
    assert device_class in violation.reason


@pytest.mark.parametrize("device_class", ["shade", "blind", "curtain", "window", ""])
def test_shade_covers_stay_exposed(device_class: str) -> None:
    entity = ExposedEntity("cover.primary_bedroom_shades", device_class=device_class)
    assert evaluate_entity(entity, DEFAULT_RULES) is None


def test_cover_device_class_is_case_insensitive() -> None:
    entity = ExposedEntity("cover.side_door", device_class="Garage")
    assert evaluate_entity(entity, DEFAULT_RULES) is not None


def test_device_class_only_applies_to_covers() -> None:
    """A garage-class binary_sensor is a read-only mirror, not an opener."""
    entity = ExposedEntity("binary_sensor.garage_door", device_class="garage")
    assert evaluate_entity(entity, DEFAULT_RULES) is None


@pytest.mark.parametrize("platform", ["intellicenter", "gecko"])
def test_denied_integrations_are_violations(platform: str) -> None:
    entity = ExposedEntity("sensor.pool_water_temperature", platform=platform)
    violation = evaluate_entity(entity, DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_INTEGRATION
    assert platform in violation.reason


def test_other_integrations_are_untouched() -> None:
    entity = ExposedEntity("light.kitchen", platform="hue")
    assert evaluate_entity(entity, DEFAULT_RULES) is None


@pytest.mark.parametrize(
    "entity_id",
    [
        "switch.power_distribution_outlet_3",
        "switch.usp_pdu_pro_port_1",
        "switch.toms_ebike_charger",
        "switch.mombike_charger",
        "switch.dryer_power",
        "switch.washer_power",
        "switch.office_printer_power",
        "switch.server_room_ac_power_switch",
        "switch.unifi_network_vpn_client",
        "switch.udm_unifi_network_firewall_rule",
        "switch.zigbee2mqtt_bridge_permit_join",
        "switch.front_door_camera_privacy_mode",
        "switch.garage_camera_detections_person",
        "switch.ratgdov25i_4a0325_led",
        "light.ratgdov25i_4a0325_light",
        "switch.spa_intouch3_switch",
        "switch.nrz120804q_config_led",
    ],
)
def test_denied_entity_globs_are_violations(entity_id: str) -> None:
    violation = evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_ENTITY_GLOB
    assert violation.reason.startswith("matches denied pattern ")


def test_every_default_glob_is_reachable() -> None:
    """Each shipped glob must be able to match something — no dead patterns."""
    for pattern in DEFAULT_DENY_ENTITY_GLOBS:
        sample = pattern.replace("*", "x")
        violation = evaluate_entity(
            ExposedEntity(sample), GuardRules.from_config({"switch_allowlist": [sample]})
        )
        assert violation is not None, f"glob {pattern!r} matched nothing"
        assert violation.rule == RULE_ENTITY_GLOB


def test_glob_denial_beats_the_switch_allowlist() -> None:
    """Adding a PDU outlet to switch_allowlist must not expose it."""
    rules = GuardRules.from_config(
        {"switch_allowlist": ["switch.power_distribution_outlet_3"]}
    )
    violation = evaluate_entity(
        ExposedEntity("switch.power_distribution_outlet_3"), rules
    )
    assert violation is not None
    assert violation.rule == RULE_ENTITY_GLOB


def test_switches_are_deny_by_default() -> None:
    violation = evaluate_entity(ExposedEntity("switch.patio_string_lights"), DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_SWITCH_DEFAULT_DENY
    assert "deny-by-default" in violation.reason


def test_switch_allowlist_permits_a_named_switch() -> None:
    rules = GuardRules.from_config(
        {"switch_allowlist": ["switch.patio_string_lights"]}
    )
    assert evaluate_entity(ExposedEntity("switch.patio_string_lights"), rules) is None
    # …and only that one.
    assert evaluate_entity(ExposedEntity("switch.other_thing"), rules) is not None


CURATED_SWITCHES = (
    "switch.back_yard_retaining_wall_lights_relay",
    "switch.shed_exterior_lights_shelly_relay",
    "switch.back_yard_backyard_motion_light_relay",
)


@pytest.mark.parametrize("entity_id", CURATED_SWITCHES)
def test_allowlisted_switches_stay_exposed(entity_id: str) -> None:
    """A shipped-allowlist switch passes; a sibling relay is still denied."""
    assert evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES) is None
    # The allowlist is by name, so the real shed-fan relay (occupancy-owned, not a light) is not swept in.
    sibling = evaluate_entity(
        ExposedEntity("switch.shed_fan_power_switch"), DEFAULT_RULES
    )
    assert sibling is not None
    assert sibling.rule == RULE_SWITCH_DEFAULT_DENY


def test_the_shipped_switch_allowlist_is_exactly_the_curated_set() -> None:
    assert DEFAULT_RULES.switch_allowlist == CURATED_SWITCHES


CURATED_SCRIPTS = (
    "script.voice_movie_room_bright",
    "script.voice_movie_room_dim",
    "script.voice_movie_room_red_night_mode",
    "script.voice_movie_room_ambient_scene",
    "script.voice_movie_room_color_toggle",
    "script.voice_rumpus_room_bright",
    "script.voice_rumpus_room_dim",
    "script.voice_rumpus_room_color_toggle",
    "script.voice_shades",
    "script.voice_primary_bathroom_lights_on",
    "script.voice_primary_bathroom_lights_off",
    "script.voice_primary_bathroom_shower_lights",
    "script.voice_cloffice_bright",
    "script.voice_kitchen_lights_off",
    "script.voice_entrance_all_off",
    "script.voice_lock_all_doors",
    "script.voice_close_garage_doors",
    "script.voice_hot_tub_mode_on",
    "script.voice_hot_tub_mode_off",
    "script.llm_script_for_music_assistant_voice_requests",
    "script.voice_move_music",
    "script.voice_group_music",
    "script.voice_thermostat",
    "script.kellie_mobile_primary_bedroom_relaxed",
    "script.kellie_mobile_primary_bedroom_focused",
    "script.kellie_mobile_primary_bedroom_bedtime",
    "script.kellie_mobile_primary_bedroom_sleep",
)


@pytest.mark.parametrize("entity_id", CURATED_SCRIPTS)
def test_allowlisted_scripts_stay_exposed(entity_id: str) -> None:
    assert evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES) is None


def test_the_shipped_script_allowlist_is_exactly_the_curated_set() -> None:
    assert DEFAULT_RULES.script_allowlist_globs == CURATED_SCRIPTS


def test_the_shipped_script_allowlist_contains_no_patterns() -> None:
    """A glob would make a filename the security boundary — see README."""
    for entry in DEFAULT_RULES.script_allowlist_globs:
        assert not any(ch in entry for ch in "*?["), entry


@pytest.mark.parametrize(
    "entity_id",
    [
        "script.open_the_garage",
        "script.disarm_alarm",
        "script.unlock_front_door",
        "script.kellie_mobile_kitchen_bedtime",
        "script.voicemail_check",
        # A new script named to look curated must NOT be allowed: the list is
        # the boundary, not the filename.
        "script.voice_anything",
        "script.voice_unlock_all_doors",
        "script.voice_movie_room_bright_2",
        "script.kellie_mobile_primary_bedroom_party",
    ],
)
def test_non_allowlisted_scripts_are_violations(entity_id: str) -> None:
    violation = evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_SCRIPT_ALLOWLIST


def test_script_allowlist_globs_are_configurable() -> None:
    """The key stays fnmatch-capable even though the default uses no patterns."""
    rules = GuardRules.from_config({"script_allowlist_globs": ["script.ok_*"]})
    assert evaluate_entity(ExposedEntity("script.ok_thing"), rules) is None
    assert evaluate_entity(ExposedEntity("script.voice_shades"), rules) is not None


@pytest.mark.parametrize(
    "entity",
    [
        ExposedEntity("lock.front_door"),
        ExposedEntity("cover.garage_door", device_class="garage"),
        ExposedEntity("sensor.pool_ph", platform="intellicenter"),
        ExposedEntity("switch.usp_pdu_pro_port_1"),
        ExposedEntity("switch.some_random_switch"),
        ExposedEntity("script.open_the_garage"),
    ],
)
def test_allow_entities_beats_every_deny_rule(entity: ExposedEntity) -> None:
    """The per-entity override is the documented escape hatch."""
    assert evaluate_entity(entity, DEFAULT_RULES) is not None
    rules = GuardRules.from_config({"allow_entities": [entity.entity_id]})
    assert evaluate_entity(entity, rules) is None


def test_upper_case_entity_ids_are_still_denied() -> None:
    violation = evaluate_entity(ExposedEntity("LOCK.Front_Door"), DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_DOMAIN
    assert violation.entity_id == "lock.front_door"


@pytest.mark.parametrize("entity_id", ["", "   ", "no_domain_at_all"])
def test_malformed_entity_ids_do_not_crash(entity_id: str) -> None:
    assert evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES) is None


def test_evaluate_returns_one_violation_per_entity_in_order() -> None:
    entities = [
        ExposedEntity("light.kitchen"),
        ExposedEntity("lock.front_door"),
        ExposedEntity("switch.usp_pdu_pro_port_1"),
        ExposedEntity("media_player.kitchen"),
        ExposedEntity("script.open_the_garage"),
    ]
    violations = evaluate(entities, DEFAULT_RULES)
    assert [v.entity_id for v in violations] == [
        "lock.front_door",
        "switch.usp_pdu_pro_port_1",
        "script.open_the_garage",
    ]
    assert [v.rule for v in violations] == [
        RULE_DOMAIN,
        RULE_ENTITY_GLOB,
        RULE_SCRIPT_ALLOWLIST,
    ]


def test_evaluate_on_a_clean_list_returns_nothing() -> None:
    entities = [ExposedEntity("light.kitchen"), ExposedEntity("media_player.kitchen")]
    assert evaluate(entities, DEFAULT_RULES) == []


# --- config normalisation --------------------------------------------------


def test_from_config_uses_defaults_for_absent_keys() -> None:
    rules = GuardRules.from_config({"unrelated": 1})
    assert rules.deny_domains == DEFAULT_DENY_DOMAINS
    assert rules.deny_entity_globs == DEFAULT_DENY_ENTITY_GLOBS


def test_from_config_honours_an_explicitly_empty_list() -> None:
    """Clearing a list must clear it, not silently restore the default."""
    rules = GuardRules.from_config({"deny_domains": []})
    assert rules.deny_domains == ()
    assert evaluate_entity(ExposedEntity("lock.front_door"), rules) is None


def test_from_config_treats_none_as_empty() -> None:
    assert GuardRules.from_config({"deny_integrations": None}).deny_integrations == ()


def test_from_config_accepts_a_bare_string() -> None:
    rules = GuardRules.from_config({"deny_integrations": "Gecko"})
    assert rules.deny_integrations == ("gecko",)


def test_from_config_lowercases_and_deduplicates() -> None:
    rules = GuardRules.from_config(
        {"switch_allowlist": ["Switch.Patio", "switch.patio", "  ", "switch.other"]}
    )
    assert rules.switch_allowlist == ("switch.patio", "switch.other")


def test_from_config_ignores_a_wrongly_typed_value() -> None:
    """A scalar where a list belongs falls back to the safe default."""
    rules = GuardRules.from_config({"deny_domains": 42})
    assert rules.deny_domains == DEFAULT_DENY_DOMAINS


# ===========================================================================
# AssistExposureGuard — the AppDaemon adapter
# ===========================================================================

BASE_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha.test:8123",
    "ha_token_env": "TOKEN",
    "check_interval_minutes": 15,
    "registry_debounce_s": 30,
    "enforce": True,
}


class FakeExposureClient:
    """Stand-in for ``AssistExposureClient`` — records calls, makes no I/O."""

    def __init__(
        self,
        exposed: Optional[List[str]] = None,
        platforms: Optional[Dict[str, str]] = None,
        list_error: Optional[Exception] = None,
        set_error: Optional[Exception] = None,
        unacceptable_ids: Optional[List[str]] = None,
    ) -> None:
        self.exposed = list(exposed or [])
        self.platforms = dict(platforms or {})
        self.platform_requests: List[List[str]] = []
        self.list_error = list_error
        self.set_error = set_error
        # Ids the real client filters out of the batch because HA would reject
        # the whole command for them — they stay exposed.
        self.unacceptable_ids = set(unacceptable_ids or [])
        self.last_assistant = ""
        self.set_exposure_calls: List[Dict[str, Any]] = []

    async def list_exposed_entities(self, assistant: str) -> List[str]:
        if self.list_error is not None:
            raise self.list_error
        self.last_assistant = assistant
        return list(self.exposed)

    async def list_entity_platforms(self, entity_ids: Any) -> Dict[str, str]:
        self.platform_requests.append(list(entity_ids))
        return {k: v for k, v in self.platforms.items() if k in set(self.platform_requests[-1])}

    async def set_exposure(
        self, entity_ids: Any, should_expose: bool, assistant: str
    ) -> ExposureChange:
        ids = list(entity_ids)
        self.set_exposure_calls.append(
            {
                "entity_ids": ids,
                "should_expose": should_expose,
                "assistant": assistant,
            }
        )
        if self.set_error is not None:
            raise self.set_error
        return ExposureChange(
            sent=[i for i in ids if i not in self.unacceptable_ids],
            skipped=[i for i in ids if i in self.unacceptable_ids],
        )


def _make_app(
    extra_args: Optional[Dict[str, Any]] = None,
    client: Optional[FakeExposureClient] = None,
    device_classes: Optional[Dict[str, str]] = None,
    build_client_error: Optional[Exception] = None,
    seed_attributes: Optional[Dict[str, Any]] = None,
) -> AssistExposureGuard:
    app = AssistExposureGuard(MagicMock(), MagicMock())
    args = dict(BASE_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    classes = dict(device_classes or {})
    sensor = str(args.get("status_sensor", "sensor.assist_exposure_guard"))

    def _get_state(entity_id: str, **kwargs: Any) -> Any:
        # Startup reads the status sensor back to recover the enforcement
        # record; everything else is a cover device_class read.
        if entity_id == sensor:
            return {"attributes": dict(seed_attributes)} if seed_attributes else None
        return classes.get(entity_id)

    # AWAITED in the app — an AsyncMock, never a bare MagicMock, or the await
    # raises and every assertion below passes for the wrong reason.
    app.get_state = AsyncMock(side_effect=_get_state)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    # Distinct handles: a shared MagicMock return value makes every
    # "cancelled the right timer" assertion vacuously true.
    handles = itertools.count()
    app.run_in = MagicMock(side_effect=lambda *a, **kw: f"handle-{next(handles)}")
    app.cancel_timer = MagicMock()
    app.create_task = MagicMock()
    app.log = MagicMock()

    app.initialize()

    def _build() -> Any:
        if build_client_error is not None:
            raise build_client_error
        return client if client is not None else FakeExposureClient()

    app._build_client = _build
    return app


def _startup(app: AssistExposureGuard) -> None:
    _run(app._async_startup())


def _service_calls(app: AssistExposureGuard, service: str) -> List[Any]:
    return [call for call in app.call_service.call_args_list if call.args[0] == service]


def _creates(app: AssistExposureGuard, notification_id: str) -> List[Any]:
    return [
        call
        for call in _service_calls(app, "persistent_notification/create")
        if call.kwargs["notification_id"] == notification_id
    ]


def _device_class_reads(app: AssistExposureGuard) -> List[str]:
    """Cover reads only — drop the startup read of the status sensor."""
    return [
        call.args[0]
        for call in app.get_state.call_args_list
        if call.args[0] != app._status_sensor
    ]


def _published_attributes(app: AssistExposureGuard) -> Dict[str, Any]:
    return app.set_state.call_args.kwargs["attributes"]


# --- startup wiring --------------------------------------------------------


def test_initialize_schedules_startup_and_reads_config() -> None:
    app = _make_app({"check_interval_minutes": 5, "enforce": False})
    assert app._interval_s == 300
    assert app._enforce is False
    assert app._assistant == "conversation"
    app.run_in.assert_called_once()
    assert app.run_in.call_args.args[0] == app._on_startup


def test_initialize_requires_a_token_env_name() -> None:
    app = AssistExposureGuard(MagicMock(), MagicMock())
    app.args = {"ha_url": "http://ha.test:8123"}
    app.log = MagicMock()
    with pytest.raises(ValueError, match="ha_token_env"):
        app.initialize()


def test_check_interval_has_a_floor() -> None:
    """A zero/negative interval would busy-loop the HA WebSocket."""
    assert _make_app({"check_interval_minutes": 0})._interval_s == 60


def test_startup_registers_the_registry_listener_and_timer_then_checks() -> None:
    client = FakeExposureClient(exposed=["light.kitchen"])
    app = _make_app(client=client)
    _startup(app)

    app.listen_event.assert_called_once_with(
        app._on_registry_updated, "entity_registry_updated"
    )
    app.run_every.assert_called_once_with(app._on_interval, "now+900", 900)
    # The startup check ran: a status sensor was published.
    app.set_state.assert_called_once()
    assert app.set_state.call_args.kwargs["state"] == "1"


# --- finding 2: a bad client must not leave the guard inert and silent -----


def test_a_failing_client_build_still_wires_the_triggers() -> None:
    """The regression this guards.

    Building the client during startup means a bad token aborts before
    listen_event/run_every run — the guard is then permanently inert AND
    invisible, the worst failure mode a security backstop has.
    """
    app = _make_app(build_client_error=ValueError("Required secret env var 'TOKEN'"))
    _startup(app)

    app.listen_event.assert_called_once_with(
        app._on_registry_updated, "entity_registry_updated"
    )
    app.run_every.assert_called_once_with(app._on_interval, "now+900", 900)


def test_a_failing_client_build_reports_itself_and_does_not_raise() -> None:
    app = _make_app(build_client_error=ValueError("Required secret env var 'TOKEN'"))
    _startup(app)

    assert app.set_state.call_args.kwargs["state"] == "unknown"
    attributes = _published_attributes(app)
    assert "Required secret env var" in attributes["last_error"]

    failures = _creates(app, CURRENT_ID)
    assert len(failures) == 1
    assert failures[0].kwargs["title"] == "Assist exposure guard: check failed"
    assert "NOT being enforced" in failures[0].kwargs["message"]
    assert any(call.kwargs.get("level") == "ERROR" for call in app.log.call_args_list)


def test_the_client_is_built_lazily_and_retried_on_the_next_tick() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    attempts = {"n": 0}

    def _build() -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("Required secret env var 'TOKEN' is not set.")
        return client

    app = _make_app()
    app._build_client = _build
    _startup(app)
    assert app._client is None  # not latched into a dead state

    _run(app._run_check("interval"))
    assert attempts["n"] == 2
    assert len(client.set_exposure_calls) == 1


def test_a_built_client_is_reused_across_checks() -> None:
    client = FakeExposureClient(exposed=["light.kitchen"])
    builds = itertools.count()
    app = _make_app()
    app._build_client = lambda: (next(builds), client)[1]
    _startup(app)
    _run(app._run_check("interval"))

    assert next(builds) == 1  # built exactly once


# --- enforcement -----------------------------------------------------------


def test_enforce_unexposes_every_violator_in_one_call() -> None:
    client = FakeExposureClient(
        exposed=[
            "light.kitchen",
            "lock.front_door",
            "switch.usp_pdu_pro_port_1",
            "script.open_the_garage",
        ]
    )
    app = _make_app(client=client)
    _startup(app)

    assert len(client.set_exposure_calls) == 1
    call = client.set_exposure_calls[0]
    assert call["entity_ids"] == [
        "lock.front_door",
        "switch.usp_pdu_pro_port_1",
        "script.open_the_garage",
    ]
    assert call["should_expose"] is False
    assert call["assistant"] == "conversation"


def test_report_only_mode_never_calls_set_exposure() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app({"enforce": False}, client=client)
    _startup(app)

    assert client.set_exposure_calls == []
    # …but it still notifies.
    creates = _service_calls(app, "persistent_notification/create")
    assert len(creates) == 1
    assert "enforce is off" in creates[0].kwargs["message"]


def test_clean_list_never_calls_set_exposure() -> None:
    client = FakeExposureClient(exposed=["light.kitchen", "media_player.kitchen"])
    app = _make_app(client=client)
    _startup(app)
    assert client.set_exposure_calls == []


def test_integration_denial_uses_the_registry_platform() -> None:
    client = FakeExposureClient(
        exposed=["sensor.pool_water_temperature", "sensor.bedroom_temperature"],
        platforms={
            "sensor.pool_water_temperature": "intellicenter",
            "sensor.bedroom_temperature": "airthings",
        },
    )
    app = _make_app(client=client)
    _startup(app)

    assert client.set_exposure_calls[0]["entity_ids"] == [
        "sensor.pool_water_temperature"
    ]


def test_cover_device_class_is_read_from_state_for_covers_only() -> None:
    client = FakeExposureClient(
        exposed=[
            "cover.garage_door",
            "cover.primary_bedroom_shades",
            "light.kitchen",
        ]
    )
    app = _make_app(
        client=client,
        device_classes={
            "cover.garage_door": "garage",
            "cover.primary_bedroom_shades": "shade",
        },
    )
    _startup(app)

    assert _device_class_reads(app) == [
        "cover.garage_door",
        "cover.primary_bedroom_shades",
    ]
    assert client.set_exposure_calls[0]["entity_ids"] == ["cover.garage_door"]


def test_unreadable_device_class_does_not_abort_the_run() -> None:
    client = FakeExposureClient(exposed=["cover.garage_door", "lock.front_door"])
    app = _make_app(client=client)
    app.get_state = AsyncMock(side_effect=RuntimeError("entity not found"))
    _startup(app)

    # The cover is unclassifiable, but the lock is still caught.
    assert client.set_exposure_calls[0]["entity_ids"] == ["lock.front_door"]


# --- notifications ---------------------------------------------------------


ENFORCED_ID = "assist_exposure_guard_enforced"
CURRENT_ID = "assist_exposure_guard"


def test_enforcement_is_recorded_under_its_own_notification_id() -> None:
    client = FakeExposureClient(exposed=["lock.front_door", "script.open_the_garage"])
    app = _make_app(client=client)
    _startup(app)

    assert _creates(app, CURRENT_ID) == []
    records = _creates(app, ENFORCED_ID)
    assert len(records) == 1
    kwargs = records[0].kwargs
    assert kwargs["title"] == "Assist exposure guard: un-exposed 2 entities"
    assert "lock.front_door" in kwargs["message"]
    assert "domain 'lock' is never exposed" in kwargs["message"]
    assert "script.open_the_garage" in kwargs["message"]
    assert "until you dismiss it" in kwargs["message"]


def test_enforcement_record_is_singular_for_one_violation() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["lock.front_door"]))
    _startup(app)
    assert (
        _creates(app, ENFORCED_ID)[0].kwargs["title"]
        == "Assist exposure guard: un-exposed 1 entity"
    )


def test_enforcement_record_truncates_a_bulk_exposure_but_keeps_an_exact_count() -> None:
    locks = [f"lock.door_{i}" for i in range(MAX_DETAIL_LINES + 5)]
    app = _make_app(client=FakeExposureClient(exposed=locks))
    _startup(app)

    kwargs = _creates(app, ENFORCED_ID)[0].kwargs
    assert kwargs["title"] == (
        f"Assist exposure guard: un-exposed {len(locks)} entities"
    )
    assert "…and 5 more" in kwargs["message"]
    assert kwargs["message"].count("lock.door_") == MAX_DETAIL_LINES


# --- finding 1: enforcement must not erase its own evidence ----------------


def test_the_clean_recheck_that_enforcement_causes_keeps_the_record() -> None:
    """The regression this guards.

    set_exposure makes HA fire entity_registry_updated, which arms this app's
    own debounce. The re-check finds a clean list — because the app just
    cleaned it — and previously that dismissed the report and zeroed the
    counts, deleting the only evidence anything happened.
    """
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(client=client)
    _startup(app)

    enforced_at = app._last_enforced
    assert enforced_at != "never"
    assert _creates(app, ENFORCED_ID)
    # Everything found was fixed, so the enforcing run itself clears any stale
    # current-state notice — and only that one.
    assert [
        call.kwargs["notification_id"]
        for call in _service_calls(app, "persistent_notification/dismiss")
    ] == [CURRENT_ID]

    # HA fires the registry event the un-expose caused; the debounce runs.
    client.exposed = []
    app.call_service.reset_mock()
    app._on_registry_updated("entity_registry_updated", {"action": "update"}, {})
    _run(app._on_debounced({}))

    # The enforcement record is untouched — nothing left to dismiss or create.
    assert _service_calls(app, "persistent_notification/dismiss") == []
    assert _creates(app, ENFORCED_ID) == []

    # …and so are the durable attributes.
    attributes = _published_attributes(app)
    assert attributes["last_enforced"] == enforced_at
    assert attributes["last_enforced_entities"] == "lock.front_door"
    # while the current-state attributes correctly report a clean list
    assert attributes["violations_last_run"] == "0"
    assert attributes["violating_entities"] == "none"


# --- an id HA will not accept is still exposed, and must be reported as such


MALFORMED = "switch.foo bar"


def test_a_partly_applied_batch_records_only_what_really_changed() -> None:
    """The regression this guards.

    ``set_exposure`` leaves malformed ids out of the batch — HA validates
    ``entity_ids`` all-or-nothing — so it can return without raising while one
    entity is untouched and STILL exposed. Recording that one as un-exposed
    would be a false all-clear on the app's most durable surface.
    """
    client = FakeExposureClient(
        exposed=["lock.front_door", MALFORMED], unacceptable_ids=[MALFORMED]
    )
    app = _make_app(client=client)
    _startup(app)

    # Both were sent to the client…
    assert client.set_exposure_calls[0]["entity_ids"] == ["lock.front_door", MALFORMED]

    # …but the enforcement record covers ONLY the one that applied.
    records = _creates(app, ENFORCED_ID)
    assert len(records) == 1
    assert records[0].kwargs["title"] == "Assist exposure guard: un-exposed 1 entity"
    assert "lock.front_door" in records[0].kwargs["message"]
    assert MALFORMED not in records[0].kwargs["message"]

    # And the one that did not apply is reported as still exposed.
    notices = _creates(app, CURRENT_ID)
    assert len(notices) == 1
    assert "UN-EXPOSE FAILED" in notices[0].kwargs["title"]
    assert "STILL EXPOSED" in notices[0].kwargs["message"]
    assert MALFORMED in notices[0].kwargs["message"]
    assert "lock.front_door" not in notices[0].kwargs["message"]


def test_a_partly_applied_batch_publishes_consistent_sensor_attributes() -> None:
    client = FakeExposureClient(
        exposed=["lock.front_door", MALFORMED], unacceptable_ids=[MALFORMED]
    )
    app = _make_app(client=client)
    _startup(app)

    attributes = _published_attributes(app)
    # Durable record: only what was actually un-exposed.
    assert attributes["last_enforced_entities"] == "lock.front_door"
    assert attributes["last_enforced"] != "never"
    # Current state: both were violations this run, and the error names the
    # one that is still exposed.
    assert attributes["violations_last_run"] == "2"
    assert MALFORMED in attributes["violating_entities"]
    assert MALFORMED in attributes["last_error"]
    assert "lock.front_door" in attributes["violating_entities"]


def test_a_partly_applied_batch_logs_the_unapplied_ids_as_an_error() -> None:
    client = FakeExposureClient(
        exposed=["lock.front_door", MALFORMED], unacceptable_ids=[MALFORMED]
    )
    app = _make_app(client=client)
    _startup(app)

    errors = [
        call.args[0]
        for call in app.log.call_args_list
        if call.kwargs.get("level") == "ERROR"
    ]
    assert any(MALFORMED in line for line in errors)


def test_an_unapplied_id_keeps_being_reported_every_run() -> None:
    """It cannot self-heal — only a human removing it ends this."""
    client = FakeExposureClient(exposed=[MALFORMED], unacceptable_ids=[MALFORMED])
    app = _make_app(client=client)
    _startup(app)
    app.call_service.reset_mock()

    _run(app._run_check("interval"))

    notices = _creates(app, CURRENT_ID)
    assert len(notices) == 1
    assert MALFORMED in notices[0].kwargs["message"]
    assert _creates(app, ENFORCED_ID) == []


def test_a_wholly_unacceptable_batch_writes_no_enforcement_record() -> None:
    client = FakeExposureClient(exposed=[MALFORMED], unacceptable_ids=[MALFORMED])
    app = _make_app(client=client)
    _startup(app)

    assert _creates(app, ENFORCED_ID) == []
    assert app._last_enforced == "never"
    assert app._last_enforced_entities == "none"

    notices = _creates(app, CURRENT_ID)
    assert len(notices) == 1
    assert MALFORMED in notices[0].kwargs["message"]


def test_a_fully_applied_batch_writes_no_still_exposed_notice() -> None:
    """The normal path is unchanged."""
    client = FakeExposureClient(exposed=["lock.front_door", "siren.alarm"])
    app = _make_app(client=client)
    _startup(app)

    assert _creates(app, CURRENT_ID) == []
    records = _creates(app, ENFORCED_ID)
    assert len(records) == 1
    assert records[0].kwargs["title"] == "Assist exposure guard: un-exposed 2 entities"
    assert _published_attributes(app)["last_enforced_entities"] == (
        "lock.front_door, siren.alarm"
    )
    assert _published_attributes(app)["last_error"] == "none"


def test_fixing_the_bad_id_clears_the_still_exposed_notice() -> None:
    """A stale "STILL EXPOSED" would otherwise outlive the problem.

    The next clean run would clear it, but that can be a whole check interval
    away for an entity whose change fires no registry event.
    """
    client = FakeExposureClient(
        exposed=["lock.front_door", MALFORMED], unacceptable_ids=[MALFORMED]
    )
    app = _make_app(client=client)
    _startup(app)
    assert _creates(app, CURRENT_ID)

    # The id is renamed by hand; both violations now apply.
    client.exposed = ["lock.front_door", "switch.foo_bar"]
    client.unacceptable_ids = set()
    app.call_service.reset_mock()
    _run(app._run_check("interval"))

    assert _creates(app, CURRENT_ID) == []
    assert [
        call.kwargs["notification_id"]
        for call in _service_calls(app, "persistent_notification/dismiss")
    ] == [CURRENT_ID]
    assert len(_creates(app, ENFORCED_ID)) == 1


def test_a_non_canonical_id_is_recognised_as_applied() -> None:
    """The partition depends on the caller and the provider normalising alike.

    If they ever drift, every violation reads as unapplied: the app would
    un-expose things correctly while reporting them as STILL EXPOSED forever,
    and write no enforcement record at all.
    """
    client = FakeExposureClient(exposed=[" Cover.Garage_Door "])
    app = _make_app(client=client, device_classes={"cover.garage_door": "garage"})
    _startup(app)

    assert client.set_exposure_calls[0]["entity_ids"] == ["cover.garage_door"]
    records = _creates(app, ENFORCED_ID)
    assert len(records) == 1
    assert "cover.garage_door" in records[0].kwargs["message"]
    assert _creates(app, CURRENT_ID) == []
    assert _published_attributes(app)["last_enforced_entities"] == "cover.garage_door"


def test_report_only_mode_is_unaffected_by_the_partition() -> None:
    client = FakeExposureClient(
        exposed=["lock.front_door", MALFORMED], unacceptable_ids=[MALFORMED]
    )
    app = _make_app({"enforce": False}, client=client)
    _startup(app)

    assert client.set_exposure_calls == []
    assert _creates(app, ENFORCED_ID) == []
    notices = _creates(app, CURRENT_ID)
    assert len(notices) == 1
    assert "enforce is off" in notices[0].kwargs["message"]
    assert "lock.front_door" in notices[0].kwargs["message"]
    assert MALFORMED in notices[0].kwargs["message"]


def test_durable_attributes_default_to_never_before_any_enforcement() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["light.kitchen"]))
    _startup(app)
    attributes = _published_attributes(app)
    assert attributes["last_enforced"] == "never"
    assert attributes["last_enforced_entities"] == "none"


def test_a_second_enforcement_replaces_the_record_with_a_fresh_one() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(client=client)
    _startup(app)
    first = app._last_enforced

    client.exposed = ["siren.alarm"]
    app.call_service.reset_mock()
    app._last_enforced = "2020-01-01T00:00:00+00:00"  # force a visible change
    _run(app._run_check("interval"))

    records = _creates(app, ENFORCED_ID)
    assert len(records) == 1  # same id — replaces, never stacks
    assert app._last_enforced != "2020-01-01T00:00:00+00:00"
    assert app._last_enforced_entities == "siren.alarm"
    # The body carries the NEW timestamp, not the one it replaced.
    assert records[0].kwargs["message"].startswith(app._last_enforced)
    assert "siren.alarm" in records[0].kwargs["message"]
    assert "lock.front_door" not in records[0].kwargs["message"]
    assert first != "never"


def test_report_only_mode_uses_the_clearable_current_state_channel() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app({"enforce": False}, client=client)
    _startup(app)

    assert _creates(app, ENFORCED_ID) == []
    reports = _creates(app, CURRENT_ID)
    assert len(reports) == 1
    assert "enforce is off" in reports[0].kwargs["message"]

    # Nothing was enforced, so the durable attributes stay empty…
    assert _published_attributes(app)["last_enforced"] == "never"

    # …and a later clean run clears this one, because it IS current state.
    client.exposed = ["light.kitchen"]
    app.call_service.reset_mock()
    _run(app._run_check("interval"))
    dismissals = _service_calls(app, "persistent_notification/dismiss")
    assert [call.kwargs["notification_id"] for call in dismissals] == [CURRENT_ID]


def test_no_violations_dismisses_a_notification_that_may_exist() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["light.kitchen"]))
    _startup(app)

    assert _service_calls(app, "persistent_notification/create") == []
    dismissals = _service_calls(app, "persistent_notification/dismiss")
    assert len(dismissals) == 1
    assert dismissals[0].kwargs["notification_id"] == CURRENT_ID


def test_a_second_clean_run_does_not_dismiss_again() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["light.kitchen"]))
    _startup(app)
    app.call_service.reset_mock()

    _run(app._run_check("interval"))
    assert _service_calls(app, "persistent_notification/dismiss") == []


def test_the_enforcement_record_is_seeded_back_after_a_reload() -> None:
    """An AppDaemon reload builds a new instance; the record must survive."""
    app = _make_app(
        client=FakeExposureClient(exposed=["light.kitchen"]),
        seed_attributes={
            "last_enforced": "2026-09-18T10:00:00-04:00",
            "last_enforced_entities": "lock.front_door, siren.alarm",
        },
    )
    _startup(app)

    attributes = _published_attributes(app)
    assert attributes["last_enforced"] == "2026-09-18T10:00:00-04:00"
    assert attributes["last_enforced_entities"] == "lock.front_door, siren.alarm"


def test_seeding_survives_a_missing_or_unreadable_sensor() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["light.kitchen"]))
    app.get_state = AsyncMock(side_effect=RuntimeError("no such entity"))
    _startup(app)
    assert _published_attributes(app)["last_enforced"] == "never"


def test_seeding_ignores_a_sensor_with_no_previous_record() -> None:
    app = _make_app(
        client=FakeExposureClient(exposed=["light.kitchen"]),
        seed_attributes={"last_enforced": "never", "violations_last_run": "0"},
    )
    _startup(app)
    assert _published_attributes(app)["last_enforced"] == "never"


def test_seeding_from_a_sensor_predating_the_record_keeps_never() -> None:
    """A sensor written by an older version has no last_enforced key at all.

    Seeding must not turn that absence into an empty string: AppDaemon keeps
    empty strings, so the attribute would render blank instead of `never`.
    """
    app = _make_app(
        client=FakeExposureClient(exposed=["light.kitchen"]),
        seed_attributes={"violations_last_run": "0", "violating_entities": "none"},
    )
    _startup(app)
    attributes = _published_attributes(app)
    assert attributes["last_enforced"] == "never"
    assert attributes["last_enforced_entities"] == "none"


def test_seeding_recovers_the_time_even_when_the_entity_list_is_missing() -> None:
    app = _make_app(
        client=FakeExposureClient(exposed=["light.kitchen"]),
        seed_attributes={"last_enforced": "2026-09-18T10:00:00-04:00"},
    )
    _startup(app)
    attributes = _published_attributes(app)
    assert attributes["last_enforced"] == "2026-09-18T10:00:00-04:00"
    assert attributes["last_enforced_entities"] == "none"


def test_mobile_notify_service_is_used_when_configured() -> None:
    app = _make_app(
        {"notify_service": "notify.mobile_app_toms_phone"},
        client=FakeExposureClient(exposed=["lock.front_door"]),
    )
    _startup(app)

    pushes = _service_calls(app, "notify/mobile_app_toms_phone")
    assert len(pushes) == 1
    assert "lock.front_door" in pushes[0].kwargs["message"]


def test_an_unchanged_report_only_finding_pushes_the_phone_once() -> None:
    """The persistent notification refreshes every run; the phone must not.

    A standing report-only finding (or a wedged HA) would otherwise buzz every
    check_interval_minutes until the owner mutes the app — and a muted app
    reports nothing at all.
    """
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(
        {"enforce": False, "notify_service": "notify/phone"}, client=client
    )
    _startup(app)
    assert len(_service_calls(app, "notify/phone")) == 1

    _run(app._run_check("interval"))
    _run(app._run_check("interval"))

    # Still one push, but the persistent notification was refreshed each time.
    assert len(_service_calls(app, "notify/phone")) == 1
    assert len(_creates(app, CURRENT_ID)) == 3


def test_a_changed_report_only_finding_pushes_again() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(
        {"enforce": False, "notify_service": "notify/phone"}, client=client
    )
    _startup(app)

    client.exposed = ["lock.front_door", "siren.alarm"]
    _run(app._run_check("interval"))
    assert len(_service_calls(app, "notify/phone")) == 2


def test_a_finding_that_clears_and_returns_pushes_again() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(
        {"enforce": False, "notify_service": "notify/phone"}, client=client
    )
    _startup(app)

    client.exposed = ["light.kitchen"]
    _run(app._run_check("interval"))
    client.exposed = ["lock.front_door"]
    _run(app._run_check("interval"))

    assert len(_service_calls(app, "notify/phone")) == 2


def test_a_repeating_check_failure_pushes_the_phone_once() -> None:
    client = FakeExposureClient(list_error=RuntimeError("HA unreachable"))
    app = _make_app({"notify_service": "notify/phone"}, client=client)
    _startup(app)
    _run(app._run_check("interval"))
    _run(app._run_check("interval"))

    assert len(_service_calls(app, "notify/phone")) == 1
    assert len(_creates(app, CURRENT_ID)) == 3


def test_a_different_check_failure_pushes_again() -> None:
    """"HA unreachable" and "unauthorized" need different responses."""
    client = FakeExposureClient(list_error=RuntimeError("HA unreachable"))
    app = _make_app({"notify_service": "notify/phone"}, client=client)
    _startup(app)

    client.list_error = RuntimeError("unauthorized")
    _run(app._run_check("interval"))

    pushes = _service_calls(app, "notify/phone")
    assert len(pushes) == 2
    assert "unauthorized" in pushes[1].kwargs["message"]


def test_every_enforcement_pushes_the_phone() -> None:
    """Enforcement is an event, not a condition — each one is news."""
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app({"notify_service": "notify/phone"}, client=client)
    _startup(app)
    client.exposed = ["lock.front_door"]  # re-exposed by hand, caught again
    _run(app._run_check("interval"))

    assert len(_service_calls(app, "notify/phone")) == 2


def test_no_mobile_notify_service_by_default() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["lock.front_door"]))
    _startup(app)
    assert app._notify_service == ""
    services = [call.args[0] for call in app.call_service.call_args_list]
    assert not any(service.startswith("notify/") for service in services)


@pytest.mark.parametrize(
    "configured,expected",
    [
        ("notify.mobile_app_x", "notify/mobile_app_x"),
        ("notify/mobile_app_x", "notify/mobile_app_x"),
        ("mobile_app_x", "notify/mobile_app_x"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_notify_service_normalisation(configured: str, expected: str) -> None:
    assert AssistExposureGuard._normalise_service(configured) == expected


# --- status sensor ---------------------------------------------------------


def test_status_sensor_publishes_strings_that_appdaemon_will_not_drop() -> None:
    """AppDaemon 4.5.13 drops attribute values equal to None or False — and
    ``0 == False`` — so zero counts and ``enforce: false`` must be strings."""
    app = _make_app({"enforce": False}, client=FakeExposureClient(exposed=[]))
    _startup(app)

    assert app.set_state.call_args.args[0] == "sensor.assist_exposure_guard"
    assert app.set_state.call_args.kwargs["state"] == "0"
    attributes = _published_attributes(app)
    assert attributes["violations_last_run"] == "0"
    assert attributes["enforce"] == "false"
    assert attributes["violating_entities"] == "none"
    assert attributes["last_error"] == "none"
    assert all(value for value in attributes.values())


def test_status_sensor_reports_violations_and_trigger() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["lock.front_door", "light.a"]))
    _startup(app)

    assert app.set_state.call_args.kwargs["state"] == "2"
    attributes = _published_attributes(app)
    assert attributes["violations_last_run"] == "1"
    assert attributes["violating_entities"] == "lock.front_door"
    assert attributes["enforce"] == "true"
    assert attributes["last_trigger"] == "startup"


def _publish_debug_line(app: AssistExposureGuard) -> str:
    lines = [
        call.args[0]
        for call in app.log.call_args_list
        if call.kwargs.get("level") == "DEBUG" and call.args[0].startswith("Published ")
    ]
    assert lines, "no DEBUG publish line was logged"
    return lines[-1]


def test_the_publish_debug_line_reports_state_and_violations_separately() -> None:
    """`state=` must be the sensor state (exposed count), not the violation count."""
    app = _make_app(client=FakeExposureClient(exposed=["lock.front_door", "light.a"]))
    _startup(app)

    line = _publish_debug_line(app)
    assert "state=2" in line
    assert "violations=1" in line


def test_the_publish_debug_line_says_unknown_when_the_check_failed() -> None:
    app = _make_app(client=FakeExposureClient(list_error=RuntimeError("boom")))
    _startup(app)

    line = _publish_debug_line(app)
    assert "state=unknown" in line
    assert "violations=unknown" in line


def test_custom_status_sensor_entity_id_is_honoured() -> None:
    app = _make_app(
        {"status_sensor": "sensor.guard_dev"}, client=FakeExposureClient(exposed=[])
    )
    _startup(app)
    assert app.set_state.call_args.args[0] == "sensor.guard_dev"


# --- failure handling ------------------------------------------------------


def test_a_failing_check_publishes_an_error_status_and_does_not_raise() -> None:
    client = FakeExposureClient(list_error=RuntimeError("boom at http://ha.test:8123"))
    app = _make_app(client=client)
    _startup(app)

    assert app.set_state.call_args.kwargs["state"] == "unknown"
    attributes = _published_attributes(app)
    assert attributes["violations_last_run"] == "unknown"
    assert "boom" in attributes["last_error"]
    # The HA host is redacted out of the frontend-visible attribute.
    assert "http://ha.test:8123" not in attributes["last_error"]
    assert "<ha_url>" in attributes["last_error"]
    assert any(
        call.kwargs.get("level") == "ERROR" for call in app.log.call_args_list
    )


def test_a_failed_un_expose_still_notifies_and_says_so() -> None:
    """'Exposed and I could not fix it' is the most urgent state — never silent."""
    client = FakeExposureClient(
        exposed=["lock.front_door"],
        set_error=RuntimeError("unauthorized at http://ha.test:8123"),
    )
    app = _make_app(client=client)
    _startup(app)

    creates = _service_calls(app, "persistent_notification/create")
    assert len(creates) == 1
    assert "UN-EXPOSE FAILED" in creates[0].kwargs["title"]
    assert "STILL EXPOSED" in creates[0].kwargs["message"]
    assert "http://ha.test:8123" not in creates[0].kwargs["message"]
    assert any(call.kwargs.get("level") == "ERROR" for call in app.log.call_args_list)


def test_a_failed_un_expose_keeps_the_real_counts_on_the_sensor() -> None:
    client = FakeExposureClient(
        exposed=["lock.front_door", "light.kitchen"],
        set_error=RuntimeError("nope"),
    )
    app = _make_app(client=client)
    _startup(app)

    assert app.set_state.call_args.kwargs["state"] == "2"
    attributes = _published_attributes(app)
    assert attributes["violations_last_run"] == "1"
    assert "nope" in attributes["last_error"]


def test_a_failed_un_expose_does_not_mark_the_run_as_crashed() -> None:
    """It is a handled failure, so the next trigger must still run."""
    client = FakeExposureClient(exposed=["lock.front_door"], set_error=RuntimeError("nope"))
    app = _make_app(client=client)
    _startup(app)
    assert app._check_in_flight is False

    client.set_error = None
    _run(app._run_check("interval"))
    assert len(client.set_exposure_calls) == 2


def test_a_failing_check_releases_the_in_flight_latch() -> None:
    client = FakeExposureClient(list_error=RuntimeError("boom"))
    app = _make_app(client=client)
    _startup(app)
    assert app._check_in_flight is False

    # A later healthy run still works.
    client.list_error = None
    client.exposed = ["lock.front_door"]
    _run(app._run_check("interval"))
    assert len(client.set_exposure_calls) == 1


def test_an_overlapping_trigger_is_skipped() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(client=client)
    _startup(app)
    client.set_exposure_calls.clear()

    app._check_in_flight = True
    _run(app._run_check("interval"))
    assert client.set_exposure_calls == []


# --- registry debounce -----------------------------------------------------


def test_registry_event_schedules_a_debounced_check() -> None:
    app = _make_app(client=FakeExposureClient(exposed=[]))
    _startup(app)
    app.run_in.reset_mock()

    app._on_registry_updated("entity_registry_updated", {"action": "update"}, {})

    app.run_in.assert_called_once()
    assert app.run_in.call_args.args == (app._on_debounced, 30)
    assert app._debounce_handle is not None


def test_a_burst_of_registry_events_cancels_the_previous_timer() -> None:
    app = _make_app(client=FakeExposureClient(exposed=[]))
    _startup(app)

    app._on_registry_updated("entity_registry_updated", {"action": "create"}, {})
    first_handle = app._debounce_handle
    app._on_registry_updated("entity_registry_updated", {"action": "update"}, {})
    second_handle = app._debounce_handle

    assert first_handle != second_handle
    app.cancel_timer.assert_called_once_with(first_handle)


def test_the_debounced_callback_runs_a_check_and_clears_the_handle() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(client=client)
    _startup(app)
    client.set_exposure_calls.clear()

    app._on_registry_updated("entity_registry_updated", {}, {})
    _run(app._on_debounced({}))

    assert app._debounce_handle is None
    assert len(client.set_exposure_calls) == 1
    assert _published_attributes(app)["last_trigger"] == "entity_registry_updated"


def test_terminate_cancels_a_pending_debounce() -> None:
    app = _make_app(client=FakeExposureClient(exposed=[]))
    _startup(app)
    app._on_registry_updated("entity_registry_updated", {}, {})
    handle = app._debounce_handle

    app.terminate()

    app.cancel_timer.assert_called_once_with(handle)
    assert app._debounce_handle is None


def test_cancelling_a_stale_timer_is_not_fatal() -> None:
    app = _make_app(client=FakeExposureClient(exposed=[]))
    _startup(app)
    app._on_registry_updated("entity_registry_updated", {}, {})
    app.cancel_timer = MagicMock(side_effect=ValueError("no such timer"))

    app.terminate()  # must not raise
    assert app._debounce_handle is None


# --- config plumbed through to the rules -----------------------------------


def test_app_config_reaches_the_rule_engine() -> None:
    client = FakeExposureClient(
        exposed=["switch.patio_string_lights", "switch.other_thing"]
    )
    app = _make_app(
        {"switch_allowlist": ["switch.patio_string_lights"]}, client=client
    )
    _startup(app)

    assert client.set_exposure_calls[0]["entity_ids"] == ["switch.other_thing"]


def test_allow_entities_from_app_config_is_respected() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app({"allow_entities": ["lock.front_door"]}, client=client)
    _startup(app)

    assert client.set_exposure_calls == []
    assert _service_calls(app, "persistent_notification/create") == []


def test_a_custom_assistant_is_passed_through() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app({"assistant": "cloud.alexa"}, client=client)
    _startup(app)

    assert client.last_assistant == "cloud.alexa"
    assert client.set_exposure_calls[0]["assistant"] == "cloud.alexa"


# ===========================================================================
# apps-prod.yaml restates every rule list, and a present key is authoritative —
# so the code defaults are dead code in production unless the two stay equal.
# ===========================================================================


def _load_prod_guard_config() -> Dict[str, Any]:
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    # apps-prod.yaml uses !secret / !include style tags elsewhere in the file.
    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    path = Path(__file__).resolve().parents[1] / "apps" / "apps-prod.yaml"
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_Loader)["assist_exposure_guard"]


@pytest.mark.parametrize(
    "key",
    [
        "deny_domains",
        "deny_cover_device_classes",
        "deny_integrations",
        "deny_entity_globs",
        "switch_allowlist",
        "script_allowlist_globs",
        "allow_entities",
    ],
)
def test_prod_yaml_rule_lists_equal_the_code_defaults(key: str) -> None:
    """Hardening a default without editing prod (or the reverse) must fail here."""
    config = _load_prod_guard_config()
    prod_rules = GuardRules.from_config(config)
    assert sorted(getattr(prod_rules, key)) == sorted(getattr(DEFAULT_RULES, key)), (
        f"apps-prod.yaml `{key}` and the rules.py default have drifted apart"
    )


def test_prod_yaml_enforces_and_pushes() -> None:
    config = _load_prod_guard_config()
    assert config.get("enforce") is True
    assert str(config.get("notify_service", "")).startswith("notify/")


def test_dev_yaml_restates_no_rule_list() -> None:
    """Dev inherits the defaults prod is pinned to; a restated copy would drift unseen."""
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    path = Path(__file__).resolve().parents[1] / "apps" / "apps-dev.yaml"
    with path.open(encoding="utf-8") as handle:
        config = yaml.load(handle, Loader=_Loader)["assist_exposure_guard_dev"]
    restated = {
        "deny_domains", "deny_cover_device_classes", "deny_integrations", "deny_entity_globs",
        "switch_allowlist", "script_allowlist_globs", "allow_entities",
    } & set(config)
    assert not restated
    # The only thing between a dev run and the single live exposure list.
    assert config.get("enforce") is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(False, False), ("false", False), ("False", False), ("no", False), ("off", False), (0, False),
     (True, True), ("true", True), ("yes", True), (1, True), (None, True), ("garbage", False), ("", False)],
)
def test_enforce_flag_parsing_never_reads_a_quoted_false_as_true(value: Any, expected: bool) -> None:
    from assist_exposure_guard.assist_exposure_guard import _as_bool

    assert _as_bool(value, True) is expected


def test_sensor_entity_lists_are_capped_like_the_notification() -> None:
    from assist_exposure_guard.assist_exposure_guard import MAX_DETAIL_LINES, _capped_join

    assert _capped_join([]) == "none"
    assert _capped_join(["light.a", "light.b"]) == "light.a, light.b"
    many = [f"scene.s{i}" for i in range(MAX_DETAIL_LINES + 130)]
    joined = _capped_join(many)
    assert joined.count("scene.") == MAX_DETAIL_LINES
    assert joined.endswith("…and 130 more")


def test_the_guard_asks_for_registry_entries_of_exposed_entities_only() -> None:
    """The whole registry is a 9.5 MB frame on the live instance (v1.18.0 failure)."""
    client = FakeExposureClient(
        exposed=["light.kitchen", "sensor.pool_ph"],
        platforms={"light.kitchen": "hue", "sensor.pool_ph": "intellicenter", "light.unexposed": "hue"},
    )
    app = _make_app(client=client)
    _startup(app)
    assert client.platform_requests == [["light.kitchen", "sensor.pool_ph"]]


def test_a_non_canonical_exposed_id_still_gets_its_platform() -> None:
    """The provider returns normalised keys; the lookup must normalise too or the
    integration deny rule silently misses (platform would read as empty)."""
    client = FakeExposureClient(exposed=[" Sensor.Pool_PH "], platforms={"sensor.pool_ph": "intellicenter"})
    client.list_entity_platforms = AsyncMock(return_value={"sensor.pool_ph": "intellicenter"})
    app = _make_app(client=client, extra_args={"enforce": False})
    _startup(app)
    assert app.set_state.call_args.kwargs["attributes"]["violations_last_run"] == "1"


def test_a_non_canonical_garage_cover_id_still_gets_its_device_class() -> None:
    """Same silent-miss shape as the platform lookup, on the higher-consequence rule."""
    client = FakeExposureClient(exposed=[" Cover.Garage_Door "])
    app = _make_app(
        client=client,
        extra_args={"enforce": False},
        device_classes={"cover.garage_door": "garage"},
    )
    _startup(app)
    attributes = app.set_state.call_args.kwargs["attributes"]
    assert attributes["violations_last_run"] == "1"
    assert attributes["violating_entities"] == "cover.garage_door"


def test_the_unapplied_id_list_in_last_error_is_capped() -> None:
    """last_error is republished every run (a malformed id cannot self-heal), so a
    bulk of them must not become a tens-of-KB attribute."""
    from assist_exposure_guard.assist_exposure_guard import MAX_DETAIL_LINES

    bad = [f"switch.bad id {i}" for i in range(MAX_DETAIL_LINES + 40)]
    client = FakeExposureClient(exposed=bad, unacceptable_ids=bad)
    app = _make_app(client=client)
    _startup(app)

    last_error = _published_attributes(app)["last_error"]
    assert last_error.count("switch.bad id") == MAX_DETAIL_LINES
    assert "…and 40 more" in last_error

