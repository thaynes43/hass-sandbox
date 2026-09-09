"""Regression tests for the health-check detail card's event dispatch.

The card is JavaScript, so none of the Python suites can see it.  What they
cannot see has broken twice: a single tap on a native form control produces
``touchend`` -> ``click`` -> ``change``, and a handler that dispatches from more
than one of those sends the relay command twice — the first carrying the
*pre*-toggle value, so the pair cancel out and auto-repair silently refuses to
turn on.  ``tests/cards/health_check_detail_card_harness.js`` drives the real
card file through a minimal DOM shim (node stdlib only, no npm) and prints what
each interaction actually sent; this module asserts on that.

Also pinned here: the delay input's ``min``/``max``/``step``.  Those bounds are
per checker — the shade gateway is 15/360/15 while the other six are 1/60/1 —
so the card renders whatever the checker published in
``repair_state.auto_repair_delay_bounds`` and falls back to 1/60/1 only when
the field is absent.  The backend side of that contract lives in
``test_auto_repair_config_contract.py::TestPublishedRepairState``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "cards" / "health_check_detail_card_harness.js"
CARD = (
    Path(__file__).parent.parent
    / "apps"
    / "health_checks"
    / "cards"
    / "health-check-detail-card.js"
)

RELAY_SCRIPT = "health_check_relay"


@pytest.fixture(scope="module")
def harness() -> dict:
    """Run the card through the DOM shim once and return its JSON report."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH — cannot exercise the card's JS")

    assert HARNESS.is_file(), f"missing harness: {HARNESS}"
    assert CARD.is_file(), f"missing card: {CARD}"

    proc = subprocess.run(
        [node, str(HARNESS), str(CARD)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"harness exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"harness did not print JSON ({exc})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        ) from exc
    # Anything the card logged is a finding in its own right.
    assert report.get("notes") == [], report.get("notes")
    return report


def _one_call(report: dict, scenario: str) -> dict:
    calls = report[scenario]["calls"]
    assert len(calls) == 1, f"{scenario} sent {len(calls)} relay calls: {calls}"
    call = calls[0]
    assert call["domain"] == "script"
    assert call["service"] == RELAY_SCRIPT
    return call


# ---------------------------------------------------------------------------
# One interaction, one command
# ---------------------------------------------------------------------------


class TestSingleDispatch:
    """A tap or a click is one command — with the value the user just chose.

    Before the fix a touch tap on the checkbox sent two: ``touchend`` fired
    first with the pre-toggle value and ``change`` second with the real one, so
    the backend saw enable-then-disable (or the reverse) for a single tap.
    """

    def test_touch_tap_on_the_checkbox_sends_one_command(self, harness):
        call = _one_call(harness, "touch_toggle")

        assert call["command"] == "update_repair_config"

    def test_touch_tap_carries_the_post_toggle_value(self, harness):
        """The stale-value half of the bug: the checkbox ended up checked."""
        call = _one_call(harness, "touch_toggle")

        assert harness["touch_toggle"]["checked_after"] is True
        assert call["payload"]["auto_repair_enabled"] is True

    def test_mouse_click_on_the_checkbox_sends_one_command(self, harness):
        call = _one_call(harness, "mouse_toggle")

        assert call["command"] == "update_repair_config"
        assert call["payload"]["auto_repair_enabled"] is True

    def test_a_delay_change_sends_one_command_carrying_the_delay(self, harness):
        call = _one_call(harness, "delay_change")

        assert call["command"] == "update_repair_config"
        assert call["payload"]["auto_repair_delay_min"] == 180

    def test_touch_tap_on_a_non_form_button_sends_one_command(self, harness):
        """The other half of the guard: non-form controls still fire on touch.

        These dispatch from ``touchend``/``click`` behind the 400 ms
        ``_touchActive`` flag, so the fix must not have made them inert.
        """
        call = _one_call(harness, "touch_button")

        assert call["command"] == "start_repair"
        assert call["payload"] == {"checker_id": "shade_gateway"}


# ---------------------------------------------------------------------------
# Per-checker delay bounds
# ---------------------------------------------------------------------------


class TestDelayBounds:
    """The input offers exactly the range the checker accepts.

    Hard-coded ``min=1 max=60 step=1`` was right for six checkers and wrong for
    the shade gateway (15/360/15, default 120): a spinner nudge on that row
    clamped 120 down to 60, and nothing above 60 could be entered at all.
    """

    def test_the_published_bounds_are_rendered(self, harness):
        assert harness["bounds_published"] == {
            "min": "15",
            "max": "360",
            "step": "15",
            "value": "120",
        }

    def test_an_absent_bounds_field_falls_back(self, harness):
        """A checker (or a cached sensor payload) from before the field existed.

        The live card runs this path until the AppDaemon image carrying the
        backend half deploys, so it has to render something sane.
        """
        assert harness["bounds_absent"] == {
            "min": "1",
            "max": "60",
            "step": "1",
            "value": "5",
        }

    def test_a_delay_below_the_published_minimum_is_refused(self, harness):
        assert harness["delay_below_min"]["calls"] == []

    def test_a_delay_above_the_published_maximum_is_refused(self, harness):
        """The old guard was ``>= 1`` only, so this went through unchecked."""
        assert harness["delay_above_max"]["calls"] == []

    def test_the_toggle_carries_a_delay_the_old_guard_would_have_dropped(
        self, harness
    ):
        """The toggle sends the delay alongside it, on the same bounds.

        120 is legal for the shade gateway; a ``<= 60`` guard here would drop
        it from the payload and leave the backend guessing.
        """
        call = _one_call(harness, "toggle_carries_out_of_legacy_range_delay")

        assert call["payload"]["auto_repair_delay_min"] == 120
