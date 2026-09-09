"""Regression tests for the health-check detail card's event dispatch.

The card is JavaScript, so none of the Python suites can see it.  What they
cannot see has broken twice: a single tap on a native form control produces
``touchend`` -> ``click`` -> ``change``, and a handler that dispatches from more
than one of those sends the relay command twice — the first carrying the
*pre*-toggle value, so the pair cancel out and auto-repair silently refuses to
turn on.  ``tests/cards/health_check_detail_card_harness.js`` drives the real
card file through a minimal DOM shim (node stdlib only, no npm) and prints what
each interaction actually sent; this module asserts on that.

Also pinned here: the re-render guard.  Both re-render paths rewrite
``innerHTML`` wholesale, so one landing mid-edit destroys what the operator was
typing — but the thing to protect is an *edit in progress*, not a focus.  Two
earlier rounds gated on ``activeElement`` and froze the card outright: first
from a tapped checkbox, then from a delay box someone had merely tapped into.

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

    The rendered bounds drive the spinner only.  The command guard enforces a
    floor of 1 and leaves the ceiling to the backend, which clamps it loudly.
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

    def test_a_delay_below_the_client_floor_is_refused(self, harness):
        """1 is the one bound the card enforces itself.

        A 0 or negative delay collapses the dwell gate, so it must never leave
        the card at all.
        """
        assert harness["delay_below_floor"]["calls"] == []

    def test_a_delay_above_the_published_maximum_is_sent_to_be_clamped(
        self, harness
    ):
        """The upper bound is the backend's, and the backend clamps it loudly.

        ``_clamp_delay(loud=True)`` warns and republishes the corrected value.
        Refusing the command here instead would be a silent client-side no-op —
        no relay call, no log, nothing on screen — and while a checker has not
        published its bounds the fallback max is 60, so that silent drop
        swallowed a perfectly legal shade-gateway 180.
        """
        call = _one_call(harness, "delay_above_max")

        assert call["command"] == "update_repair_config"
        assert call["payload"]["auto_repair_delay_min"] == 9000

    def test_the_toggle_carries_a_delay_the_old_guard_would_have_dropped(
        self, harness
    ):
        """The toggle sends the delay alongside it, on the same bounds.

        120 is legal for the shade gateway; a ``<= 60`` guard here would drop
        it from the payload and leave the backend guessing.
        """
        call = _one_call(harness, "toggle_carries_out_of_legacy_range_delay")

        assert call["payload"]["auto_repair_delay_min"] == 120


# ---------------------------------------------------------------------------
# Re-render focus guard
# ---------------------------------------------------------------------------


class TestRefreshDoesNotClobberTyping:
    """The 15 s refresh tick has to respect the edit guard too.

    ``_update()`` rewrites ``innerHTML`` wholesale, so a tick landing while the
    operator is part-way through typing a delay swaps the input being typed
    into for a fresh node carrying the published value — keystrokes and focus
    both gone.  ``set hass`` has always guarded; the timer had not.
    """

    def test_the_tick_leaves_the_input_being_typed_into_alone(self, harness):
        scenario = harness["refresh_during_edit"]

        assert scenario["same_node"] is True
        assert scenario["value_after"] == "18"

    def test_the_tick_dispatches_no_change_event(self, harness):
        """A replaced input must not look like the operator committed a value."""
        assert harness["refresh_during_edit"]["change_events"] == 0

    def test_the_tick_sends_no_relay_command(self, harness):
        assert harness["refresh_during_edit"]["calls"] == []

    def test_a_popup_closed_mid_edit_does_not_stay_guarded(self, harness):
        """The guard is a flag now, and a flag can be left standing.

        Closing the popup destroys the input the flag was protecting, and
        browsers do not agree on whether removing a focused node fires
        ``focusout`` — so nothing is guaranteed to lower it on that path.  Left
        up, it would freeze the card the next time the popup opens, which is
        the very failure this guard was rewritten to end.
        """
        assert harness["refresh_during_edit"]["editing_after_close"] is False


class TestAMerelyFocusedInputDoesNotFreezeTheCard:
    """Focus was the wrong signal; an edit in progress is the right one.

    Guarding on ``activeElement`` meant an operator who tapped into the delay
    box and walked away without typing a character stopped both re-render
    paths — ``set hass`` and the 15 s tick — until something blurred the box.
    On the wall display the card is built for, nothing ever does: the freeze
    lasted until somebody physically walked up to it.  Nothing had been typed,
    so there was never anything to protect.
    """

    def test_the_box_really_is_focused_and_really_is_a_text_control(
        self, harness
    ):
        """Without this the rest of the class would pass for the wrong reason.

        A guard reading ``activeElement`` could only have been exercised by a
        control that both holds the focus and would have matched its tag test.
        """
        scenario = harness["refresh_with_input_merely_focused"]

        assert scenario["focused_tag"] == "INPUT"
        assert scenario["focused_type"] == "number"

    def test_the_tick_still_redraws_under_an_untouched_box(self, harness):
        scenario = harness["refresh_with_input_merely_focused"]

        assert scenario["detail_before"] == "disconnected"
        assert scenario["detail_after_tick"] == "gateway back"
        assert scenario["node_replaced"] is True


class TestACommittedValueReleasesTheGuard:
    """A commit hands the box back, at once — no blur required.

    The spinner is the case that forces it: a click on it fires ``input`` and
    ``change`` together and leaves the focus where it was, so a release that
    waited for the box to be left would hold the card frozen after every
    nudge.  Once committed, the operator's number is the backend's business —
    the next redraw has to show whatever the sensor actually publishes, or the
    card quietly reports a delay that was never accepted.
    """

    def test_the_commit_reached_the_backend(self, harness):
        call = _one_call(harness, "refresh_after_commit")

        assert call["command"] == "update_repair_config"
        assert call["payload"]["auto_repair_delay_min"] == 180
        assert harness["refresh_after_commit"]["value_typed"] == "180"

    def test_the_box_still_held_the_focus_when_the_tick_landed(self, harness):
        """Otherwise the redraw below would prove nothing about the release."""
        assert harness["refresh_after_commit"]["focused_at_tick"] == "INPUT"

    def test_the_next_tick_redraws_the_server_value(self, harness):
        scenario = harness["refresh_after_commit"]

        assert scenario["node_replaced"] is True
        assert scenario["value_after"] == "120"


class TestATappedCheckboxDoesNotFreezeTheCard:
    """The focus guard must cover typed text and nothing else.

    Guarding on any focused ``INPUT`` also caught the auto-repair checkbox,
    which keeps the focus after a tap.  From that tap on, both re-render paths
    — ``set hass`` and the 15 s tick — returned early, so the card stopped
    showing new health data entirely.  A desktop user clears it by clicking
    somewhere else; the wall display it is built for never gets clicked
    elsewhere, so there the freeze lasts until somebody walks up to it.  A
    checkbox holds no keystrokes, so there was never anything to protect.
    """

    def test_the_tapped_checkbox_really_does_hold_the_focus(self, harness):
        """Without this the rest of the class would pass for the wrong reason.

        If the tap left nothing focused, the guard could not have blocked the
        re-render whatever it matched on.
        """
        scenario = harness["rerender_with_checkbox_focused"]

        assert scenario["focused_tag"] == "INPUT"
        assert scenario["focused_type"] == "checkbox"

    def test_new_health_data_still_reaches_the_card_after_the_tap(self, harness):
        """``set hass`` is the path that carries every state change from HA."""
        scenario = harness["rerender_with_checkbox_focused"]

        assert scenario["detail_before"] == "disconnected"
        assert scenario["detail_after_set_hass"] == "reconnecting"
        assert scenario["node_replaced"] is True

    def test_the_refresh_tick_still_runs_after_the_tap(self, harness):
        """The other half: the tick redraws the age-dependent text every 15 s.

        A frozen tick leaves stale "5s ago" staleness and countdowns on screen
        even while the underlying data is fresh.
        """
        scenario = harness["rerender_with_checkbox_focused"]

        assert scenario["detail_after_tick"] == "gateway back"

    def test_the_tap_itself_still_sent_exactly_one_command(self, harness):
        """Two re-renders in the middle must not resend or drop the toggle."""
        call = _one_call(harness, "rerender_with_checkbox_focused")

        assert call["command"] == "update_repair_config"
        assert call["payload"]["auto_repair_enabled"] is True


class TestAKeystrokeOnANonTextControlIsNotAnEdit:
    """Which is the whole reason ``HCD_NON_TEXT_INPUT_TYPES`` survives.

    The flag is raised by ``input`` and by ``keydown``.  ``input`` alone would
    need no type test: a checkbox's activation fires ``input`` and ``change``
    together, so the flag would go up and come straight back down in the same
    task.  ``keydown`` has no such partner — it arrives from whatever holds the
    focus, and a checkbox taking an arrow key never follows it with a
    ``change``, nor with a ``focusout`` on a display no one clicks.  Untyped,
    that one keystroke would freeze the card exactly as focus used to.
    """

    def test_the_keystroke_really_landed_on_the_checkbox(self, harness):
        assert (
            harness["refresh_after_keydown_on_checkbox"]["focused_type"]
            == "checkbox"
        )

    def test_the_tick_still_runs_after_the_keystroke(self, harness):
        assert (
            harness["refresh_after_keydown_on_checkbox"]["detail_after_tick"]
            == "gateway back"
        )
