"""Regression tests for filter validation, recurring dates and confirmed saves.

The Node harness uses the real card, DOM events and relay JSON. Its fake clock
exercises delayed acknowledgements and timeouts without sleeping or load tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
HARNESS = TESTS / "cards" / "immich_fetcher_card_harness.js"
CARD = TESTS.parent / "apps" / "immich_fetcher" / "immich-fetcher-card.js"
SCENARIOS = [
    "required_query", "required_album", "inverted_absolute_dates",
    "invalid_absolute_day", "incomplete_recurring_start", "incomplete_recurring_end",
    "missing_recurring_start", "impossible_recurring_day", "unknown_person",
    "unknown_album", "duplicate_filter_names", "search_with_person_and_leap_day",
    "month_days_and_month_change", "multiple_ranges_add_remove_and_wrap",
    "roundtrip_preserves_config", "favorite_people_only_boolean_false",
    "favorite_people_only_boolean_true", "favorite_people_only_string_false",
    "favorite_people_only_string_true", "favorite_people_only_lowercase_false",
    "favorite_people_only_lowercase_true", "favorite_people_only_mixed_case_false",
    "favorite_people_only_mixed_case_true", "legacy_timestamp_limits_roundtrip",
    "mixed_timestamp_and_date_same_day", "stale_favorite_can_be_removed",
    "favorite_beyond_cutoff_stays_removable", "quoted_query_and_people_roundtrip",
    "opened_select_survives_sensor_update", "text_to_select_keeps_new_picker_edit",
    "last_filter_name_renders_as_text", "save_waits_for_matching_success",
    "rejected_save_keeps_edits", "relay_failure_keeps_edits", "save_timeout_keeps_edits",
    "pending_save_preserves_newer_edits", "focused_text_survives_sensor_update",
    "focused_number_survives_sensor_update", "focused_select_survives_sensor_update",
    "focus_without_edit_does_not_freeze", "name_to_people_preserves_add",
    "name_to_search_preserves_first_save", "save_click_after_edit_dispatches",
    "touch_then_click_dispatches_once", "gesture_touchmove_cancels_action",
    "gesture_scroll_cancels_action", "gesture_touchcancel_cancels_action",
    "native_select_touch_is_not_prevented",
]


@pytest.fixture(scope="module")
def card_report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to exercise the card JavaScript")
    proc = subprocess.run(
        [node, str(HARNESS), str(CARD)], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    report = json.loads(proc.stdout)
    assert set(report) == set(SCENARIOS), "harness scenario list differs from collected tests"
    return report


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_immich_card_interaction(card_report: dict, scenario: str) -> None:
    result = card_report[scenario]
    assert result["passed"], result.get("detail", scenario)
