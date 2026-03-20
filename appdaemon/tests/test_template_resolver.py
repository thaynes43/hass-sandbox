"""Unit tests for vestaboard_apps._shared.template_resolver."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Make vestaboard_apps importable without AppDaemon installed
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import pytest

from vestaboard_apps._shared.template_resolver import (
    find_template_entities,
    has_template,
    resolve_entities,
    resolve_template,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_getter(states: dict[str, str]):
    """Return a state_getter that looks up entity_id in *states*."""
    def getter(entity_id: str) -> Optional[str]:
        return states.get(entity_id)
    return getter


STATES_BASIC = {
    "sensor.ups_load": "85",
    "sensor.indoor_temp": "72",
}

STATES_UNAVAILABLE = {
    "sensor.ups_load": "unavailable",
    "sensor.indoor_temp": "unknown",
}


# ===========================================================================
# find_template_entities
# ===========================================================================


class TestFindTemplateEntities:
    def test_single_entity(self):
        result = find_template_entities("Load: {sensor.ups_load}W")
        assert result == ["sensor.ups_load"]

    def test_multiple_entities(self):
        result = find_template_entities(
            "Load: {sensor.ups_load}W Temp: {sensor.indoor_temp}C"
        )
        assert result == ["sensor.ups_load", "sensor.indoor_temp"]

    def test_no_placeholders(self):
        result = find_template_entities("Hello world, no placeholders here.")
        assert result == []

    def test_empty_string(self):
        result = find_template_entities("")
        assert result == []

    def test_duplicate_entity_deduplication(self):
        """Same entity appearing twice → returned once in first-seen order."""
        result = find_template_entities(
            "{sensor.ups_load} then again {sensor.ups_load}"
        )
        assert result == ["sensor.ups_load"]

    def test_multiple_entities_order_preserved(self):
        result = find_template_entities(
            "{sensor.indoor_temp} and {sensor.ups_load}"
        )
        assert result == ["sensor.indoor_temp", "sensor.ups_load"]

    def test_malformed_no_dot(self):
        """Pattern without a dot should not match."""
        result = find_template_entities("{not_an_entity}")
        assert result == []

    def test_malformed_uppercase_not_matched(self):
        """Uppercase domain/entity should not match (pattern is lower-case only)."""
        result = find_template_entities("{Sensor.Temp}")
        assert result == []

    def test_numbers_in_entity_name(self):
        """Numbers in entity name are valid."""
        result = find_template_entities("{sensor.apc_2700w_load}")
        assert result == ["sensor.apc_2700w_load"]

    def test_mixed_valid_and_invalid(self):
        """Only valid entity_id patterns are extracted."""
        result = find_template_entities(
            "{sensor.temp} {NotValid} {input_boolean.flag}"
        )
        assert result == ["sensor.temp", "input_boolean.flag"]

    def test_adjacent_placeholders(self):
        result = find_template_entities("{sensor.a}{sensor.b}")
        assert result == ["sensor.a", "sensor.b"]


# ===========================================================================
# resolve_template
# ===========================================================================


class TestResolveTemplate:
    def test_single_substitution(self):
        text, resolutions = resolve_template(
            "Load: {sensor.ups_load}W",
            make_getter(STATES_BASIC),
        )
        assert text == "Load: 85W"
        assert resolutions == {"sensor.ups_load": "85"}

    def test_multiple_substitutions(self):
        text, resolutions = resolve_template(
            "Load: {sensor.ups_load}W Temp: {sensor.indoor_temp}F",
            make_getter(STATES_BASIC),
        )
        assert text == "Load: 85W Temp: 72F"
        assert resolutions == {"sensor.ups_load": "85", "sensor.indoor_temp": "72"}

    def test_no_placeholders(self):
        text, resolutions = resolve_template("Hello world", make_getter({}))
        assert text == "Hello world"
        assert resolutions == {}

    def test_empty_string(self):
        text, resolutions = resolve_template("", make_getter({}))
        assert text == ""
        assert resolutions == {}

    def test_unavailable_state_becomes_na(self):
        text, resolutions = resolve_template(
            "Load: {sensor.ups_load}W",
            make_getter({"sensor.ups_load": "unavailable"}),
        )
        assert text == "Load: N/AW"
        assert resolutions["sensor.ups_load"] == "N/A"

    def test_unknown_state_becomes_na(self):
        text, resolutions = resolve_template(
            "Temp: {sensor.indoor_temp}F",
            make_getter({"sensor.indoor_temp": "unknown"}),
        )
        assert text == "Temp: N/AF"
        assert resolutions["sensor.indoor_temp"] == "N/A"

    def test_none_state_becomes_na(self):
        """Entity not in getter → None → N/A."""
        text, resolutions = resolve_template(
            "Val: {sensor.missing}",
            make_getter({}),
        )
        assert text == "Val: N/A"
        assert resolutions["sensor.missing"] == "N/A"

    def test_mixed_available_and_unavailable(self):
        text, resolutions = resolve_template(
            "{sensor.ups_load} / {sensor.indoor_temp}",
            make_getter({"sensor.ups_load": "85", "sensor.indoor_temp": "unavailable"}),
        )
        assert text == "85 / N/A"
        assert resolutions == {"sensor.ups_load": "85", "sensor.indoor_temp": "N/A"}

    def test_resolutions_dict_used_for_logging(self):
        """resolutions dict should map entity_id → resolved value."""
        _, resolutions = resolve_template(
            "{sensor.ups_load}",
            make_getter({"sensor.ups_load": "42"}),
        )
        assert resolutions == {"sensor.ups_load": "42"}

    # -----------------------------------------------------------------------
    # Overflow / truncation
    # -----------------------------------------------------------------------

    def test_no_truncation_when_within_limit(self):
        """Short resolved text should not be truncated."""
        text, _ = resolve_template(
            "{sensor.ups_load}",
            make_getter({"sensor.ups_load": "85"}),
            max_total_chars=132,
        )
        assert text == "85"

    def test_overflow_truncation_single_entity(self):
        """Single long entity value should be truncated to fit budget."""
        long_value = "A" * 200
        text, resolutions = resolve_template(
            "VAL: {sensor.test}",
            make_getter({"sensor.test": long_value}),
            max_total_chars=20,
        )
        # Static part "VAL: " is 5 chars; budget = 20 - 5 = 15
        assert len(text) <= 20
        assert resolutions["sensor.test"] == long_value[:15]

    def test_overflow_truncation_two_entities_proportional(self):
        """Two entities with equal-length values get equal budget shares."""
        value_a = "A" * 100
        value_b = "B" * 100
        # Static: "{} / {}" with placeholders removed = " / " = 3 chars
        # max_total_chars = 23; budget = 20
        # Each value gets 10 chars (half of budget)
        text, resolutions = resolve_template(
            "{sensor.a} / {sensor.b}",
            make_getter({"sensor.a": value_a, "sensor.b": value_b}),
            max_total_chars=23,
        )
        # Static " / " = 3, so resolved len = 3 + allotted_a + allotted_b <= 23
        assert len(text) <= 23
        # Both should be truncated (not the full 100-char value)
        assert len(resolutions["sensor.a"]) < 100
        assert len(resolutions["sensor.b"]) < 100

    def test_overflow_truncation_unequal_proportional(self):
        """Longer entity value gets a larger share of the budget."""
        short_value = "AB"      # 2 chars
        long_value = "C" * 98  # 98 chars  — total value chars = 100
        # Static: "" + " " + "" = 1 char; max_total_chars = 51
        # budget = 50; short gets 1 char (2/100 * 50 = 1), long gets 49 chars
        text, resolutions = resolve_template(
            "{sensor.short} {sensor.long}",
            make_getter({"sensor.short": short_value, "sensor.long": long_value}),
            max_total_chars=51,
        )
        assert len(text) <= 51
        assert len(resolutions["sensor.short"]) <= len(short_value)
        assert len(resolutions["sensor.long"]) <= len(long_value)
        # Long should get more chars than short
        assert len(resolutions["sensor.long"]) > len(resolutions["sensor.short"])

    def test_overflow_zero_budget(self):
        """When static text alone exceeds max_total_chars, values become empty."""
        # "ABCDE " is 6 chars static, max_total_chars=5
        text, resolutions = resolve_template(
            "ABCDE {sensor.test}",
            make_getter({"sensor.test": "hello"}),
            max_total_chars=5,
        )
        # budget = 5 - 6 = -1 → clamped to 0 → value becomes ""
        assert resolutions["sensor.test"] == ""
        # The resolved text is just the static part (entity replaced by "")
        assert text == "ABCDE "

    def test_exact_limit_no_truncation(self):
        """Exactly at the limit should not truncate."""
        # "VAL:85" = 6 chars; set limit to 6
        text, _ = resolve_template(
            "VAL:{sensor.test}",
            make_getter({"sensor.test": "85"}),
            max_total_chars=6,
        )
        assert text == "VAL:85"

    def test_custom_max_total_chars(self):
        text, _ = resolve_template(
            "{sensor.test}",
            make_getter({"sensor.test": "hello world"}),
            max_total_chars=5,
        )
        assert len(text) <= 5

    def test_default_max_is_132(self):
        """Default max_total_chars should be 132 (6×22)."""
        # Value of exactly 132 chars should pass through untruncated
        value = "X" * 132
        text, resolutions = resolve_template(
            "{sensor.test}",
            make_getter({"sensor.test": value}),
        )
        assert text == value
        assert resolutions["sensor.test"] == value

    def test_default_max_triggers_at_133(self):
        """Value of 133 chars should be truncated when using default limit."""
        value = "X" * 133
        text, _ = resolve_template(
            "{sensor.test}",
            make_getter({"sensor.test": value}),
        )
        assert len(text) <= 132


# ===========================================================================
# resolve_entities
# ===========================================================================


class TestResolveEntities:
    def test_single_entity_enriched(self):
        entities = [{"entity_id": "sensor.temp", "description": "Indoor temp"}]
        result = resolve_entities(entities, make_getter({"sensor.temp": "72"}))
        assert len(result) == 1
        assert result[0]["entity_id"] == "sensor.temp"
        assert result[0]["description"] == "Indoor temp"
        assert result[0]["current_value"] == "72"

    def test_multiple_entities_enriched(self):
        entities = [
            {"entity_id": "sensor.temp", "description": "Temp"},
            {"entity_id": "sensor.ups_load", "description": "UPS Load"},
        ]
        result = resolve_entities(
            entities,
            make_getter({"sensor.temp": "72", "sensor.ups_load": "85"}),
        )
        assert result[0]["current_value"] == "72"
        assert result[1]["current_value"] == "85"

    def test_unavailable_state_becomes_na(self):
        entities = [{"entity_id": "sensor.broken", "description": "Broken sensor"}]
        result = resolve_entities(
            entities,
            make_getter({"sensor.broken": "unavailable"}),
        )
        assert result[0]["current_value"] == "N/A"

    def test_unknown_state_becomes_na(self):
        entities = [{"entity_id": "sensor.temp", "description": "Temp"}]
        result = resolve_entities(
            entities,
            make_getter({"sensor.temp": "unknown"}),
        )
        assert result[0]["current_value"] == "N/A"

    def test_none_state_becomes_na(self):
        entities = [{"entity_id": "sensor.missing", "description": "Missing"}]
        result = resolve_entities(entities, make_getter({}))
        assert result[0]["current_value"] == "N/A"

    def test_original_dicts_not_mutated(self):
        """resolve_entities must not modify the input dicts."""
        original = {"entity_id": "sensor.temp", "description": "Temp"}
        entities = [original]
        resolve_entities(entities, make_getter({"sensor.temp": "72"}))
        assert "current_value" not in original

    def test_empty_list(self):
        result = resolve_entities([], make_getter({}))
        assert result == []

    def test_extra_keys_preserved(self):
        """Extra keys in input dicts should be passed through."""
        entities = [
            {
                "entity_id": "sensor.temp",
                "description": "Temp",
                "unit": "F",
                "extra": 42,
            }
        ]
        result = resolve_entities(entities, make_getter({"sensor.temp": "72"}))
        assert result[0]["unit"] == "F"
        assert result[0]["extra"] == 42
        assert result[0]["current_value"] == "72"

    def test_empty_entity_id_becomes_na(self):
        """Entry with empty entity_id falls back to N/A."""
        entities = [{"entity_id": "", "description": "No entity"}]
        result = resolve_entities(entities, make_getter({}))
        assert result[0]["current_value"] == "N/A"

    def test_missing_entity_id_key_becomes_na(self):
        """Entry missing entity_id key entirely falls back to N/A."""
        entities = [{"description": "No entity_id key"}]
        result = resolve_entities(entities, make_getter({}))
        assert result[0]["current_value"] == "N/A"


# ===========================================================================
# has_template
# ===========================================================================


class TestHasTemplate:
    def test_no_placeholder_returns_false(self):
        assert has_template("Hello world") is False

    def test_valid_entity_placeholder_returns_true(self):
        assert has_template("{sensor.temp} degrees") is True

    def test_none_returns_false(self):
        assert has_template(None) is False

    def test_empty_string_returns_false(self):
        assert has_template("") is False

    def test_malformed_no_dot_returns_false(self):
        assert has_template("{not_an_entity}") is False

    def test_malformed_uppercase_returns_false(self):
        assert has_template("{Sensor.Temp}") is False

    def test_multiple_placeholders_returns_true(self):
        assert has_template("{sensor.a} and {sensor.b}") is True

    def test_only_braces_no_dot_returns_false(self):
        assert has_template("{}") is False

    def test_partial_placeholder_returns_false(self):
        """Unclosed brace should not match."""
        assert has_template("{sensor.temp") is False

    def test_numbers_in_entity_name(self):
        assert has_template("{sensor.apc_2700w_load}") is True

    def test_template_with_valid_and_invalid_mixed(self):
        """At least one valid placeholder → True."""
        assert has_template("{not_entity} {sensor.temp}") is True
