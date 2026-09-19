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
        "scene.movie_night",
        "todo.shopping_list",
        "weather.forecast_home",
        "binary_sensor.front_door_is_locked",
    ],
)
def test_benign_domains_are_allowed(entity_id: str) -> None:
    assert evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES) is None


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


@pytest.mark.parametrize(
    "entity_id",
    [
        "script.voice_lock_all_doors",
        "script.voice_close_garage_doors",
        "script.llm_script_for_music_assistant_voice_requests",
        "script.kellie_mobile_primary_bedroom_bedtime",
        "script.kellie_mobile_primary_bedroom_sleep",
    ],
)
def test_allowlisted_scripts_stay_exposed(entity_id: str) -> None:
    assert evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES) is None


@pytest.mark.parametrize(
    "entity_id",
    [
        "script.open_the_garage",
        "script.disarm_alarm",
        "script.unlock_front_door",
        "script.kellie_mobile_kitchen_bedtime",
        "script.voicemail_check",  # 'voice_' prefix must not match 'voicemail'
    ],
)
def test_non_allowlisted_scripts_are_violations(entity_id: str) -> None:
    violation = evaluate_entity(ExposedEntity(entity_id), DEFAULT_RULES)
    assert violation is not None
    assert violation.rule == RULE_SCRIPT_ALLOWLIST


def test_script_allowlist_globs_are_configurable() -> None:
    rules = GuardRules.from_config({"script_allowlist_globs": ["script.ok_*"]})
    assert evaluate_entity(ExposedEntity("script.ok_thing"), rules) is None
    assert evaluate_entity(ExposedEntity("script.voice_lock_all_doors"), rules) is not None


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
    ) -> None:
        self.exposed = list(exposed or [])
        self.platforms = dict(platforms or {})
        self.list_error = list_error
        self.set_error = set_error
        self.last_assistant = ""
        self.set_exposure_calls: List[Dict[str, Any]] = []

    async def list_exposed_entities(self, assistant: str) -> List[str]:
        if self.list_error is not None:
            raise self.list_error
        self.last_assistant = assistant
        return list(self.exposed)

    async def list_entity_platforms(self) -> Dict[str, str]:
        return dict(self.platforms)

    async def set_exposure(
        self, entity_ids: Any, should_expose: bool, assistant: str
    ) -> int:
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
        return len(ids)


def _make_app(
    extra_args: Optional[Dict[str, Any]] = None,
    client: Optional[FakeExposureClient] = None,
    device_classes: Optional[Dict[str, str]] = None,
) -> AssistExposureGuard:
    app = AssistExposureGuard(MagicMock(), MagicMock())
    args = dict(BASE_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    classes = dict(device_classes or {})

    # AWAITED in the app — an AsyncMock, never a bare MagicMock, or the await
    # raises and every assertion below passes for the wrong reason.
    app.get_state = AsyncMock(
        side_effect=lambda entity_id, **kwargs: classes.get(entity_id)
    )
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
    app._build_client = lambda: client if client is not None else FakeExposureClient()
    return app


def _startup(app: AssistExposureGuard) -> None:
    _run(app._async_startup())


def _service_calls(app: AssistExposureGuard, service: str) -> List[Any]:
    return [call for call in app.call_service.call_args_list if call.args[0] == service]


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

    read = [call.args[0] for call in app.get_state.call_args_list]
    assert read == ["cover.garage_door", "cover.primary_bedroom_shades"]
    assert client.set_exposure_calls[0]["entity_ids"] == ["cover.garage_door"]


def test_unreadable_device_class_does_not_abort_the_run() -> None:
    client = FakeExposureClient(exposed=["cover.garage_door", "lock.front_door"])
    app = _make_app(client=client)
    app.get_state = AsyncMock(side_effect=RuntimeError("entity not found"))
    _startup(app)

    # The cover is unclassifiable, but the lock is still caught.
    assert client.set_exposure_calls[0]["entity_ids"] == ["lock.front_door"]


# --- notifications ---------------------------------------------------------


def test_notification_lists_entities_and_reasons_under_a_stable_id() -> None:
    client = FakeExposureClient(exposed=["lock.front_door", "script.open_the_garage"])
    app = _make_app(client=client)
    _startup(app)

    creates = _service_calls(app, "persistent_notification/create")
    assert len(creates) == 1
    kwargs = creates[0].kwargs
    assert kwargs["notification_id"] == "assist_exposure_guard"
    assert kwargs["title"] == "Assist exposure guard: 2 unsafe entities"
    assert "lock.front_door" in kwargs["message"]
    assert "domain 'lock' is never exposed" in kwargs["message"]
    assert "script.open_the_garage" in kwargs["message"]
    assert "Un-exposed from 'conversation'" in kwargs["message"]


def test_notification_title_is_singular_for_one_violation() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["lock.front_door"]))
    _startup(app)
    creates = _service_calls(app, "persistent_notification/create")
    assert creates[0].kwargs["title"] == "Assist exposure guard: 1 unsafe entity"


def test_notification_truncates_a_bulk_exposure_but_keeps_an_exact_count() -> None:
    locks = [f"lock.door_{i}" for i in range(MAX_DETAIL_LINES + 5)]
    app = _make_app(client=FakeExposureClient(exposed=locks))
    _startup(app)

    kwargs = _service_calls(app, "persistent_notification/create")[0].kwargs
    assert kwargs["title"] == f"Assist exposure guard: {len(locks)} unsafe entities"
    assert "…and 5 more" in kwargs["message"]
    assert kwargs["message"].count("lock.door_") == MAX_DETAIL_LINES


def test_no_violations_dismisses_a_notification_that_may_exist() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["light.kitchen"]))
    _startup(app)

    assert _service_calls(app, "persistent_notification/create") == []
    dismissals = _service_calls(app, "persistent_notification/dismiss")
    assert len(dismissals) == 1
    assert dismissals[0].kwargs["notification_id"] == "assist_exposure_guard"


def test_a_second_clean_run_does_not_dismiss_again() -> None:
    app = _make_app(client=FakeExposureClient(exposed=["light.kitchen"]))
    _startup(app)
    app.call_service.reset_mock()

    _run(app._run_check("interval"))
    assert _service_calls(app, "persistent_notification/dismiss") == []


def test_violations_then_a_clean_run_dismisses_the_notification() -> None:
    client = FakeExposureClient(exposed=["lock.front_door"])
    app = _make_app(client=client)
    _startup(app)
    assert len(_service_calls(app, "persistent_notification/create")) == 1

    client.exposed = ["light.kitchen"]
    app.call_service.reset_mock()
    _run(app._run_check("interval"))

    assert _service_calls(app, "persistent_notification/create") == []
    assert len(_service_calls(app, "persistent_notification/dismiss")) == 1


def test_mobile_notify_service_is_used_when_configured() -> None:
    app = _make_app(
        {"notify_service": "notify.mobile_app_toms_phone"},
        client=FakeExposureClient(exposed=["lock.front_door"]),
    )
    _startup(app)

    pushes = _service_calls(app, "notify/mobile_app_toms_phone")
    assert len(pushes) == 1
    assert "lock.front_door" in pushes[0].kwargs["message"]


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
