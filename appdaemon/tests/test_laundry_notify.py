"""Unit tests for laundry_notify: the pure coordinator and the AppDaemon wrapper."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _MockHass:
    def __init__(self, *args, **kwargs):
        pass


_mock_hassapi = MagicMock()
_mock_hassapi.Hass = _MockHass
sys.modules.setdefault("hassapi", _mock_hassapi)

_APPS_DIR = Path(__file__).resolve().parents[1] / "apps"
if str(_APPS_DIR) not in sys.path:
    sys.path.insert(0, str(_APPS_DIR))

from laundry_notify import coordinator as c  # noqa: E402
from laundry_notify.coordinator import Action, LaundryCoordinator  # noqa: E402
from laundry_notify.laundry_notify import (  # noqa: E402
    LaundryNotify,
    build_message,
    format_duration,
)


def notes(actions):
    return [a.notification for a in actions if a.kind == "notify"]


def run(coord, *steps):
    """Apply (machine, state) steps and collect every action."""
    out = []
    for machine, state in steps:
        out += coord.update(machine, state)
    return out


W, D = c.WASHER, c.DRYER


# --------------------------------------------------------------- coordinator
class TestSingleMachine:
    def test_washer_alone_notifies_and_arms_reminder(self):
        co = LaundryCoordinator()
        acts = run(co, (W, "initial"), (W, "running"), (W, "spinning"), (W, "end"))
        assert acts == [Action("notify", c.WASHER_COMPLETE), Action("arm_reminder")]

    def test_dryer_alone_notifies_without_reminder(self):
        co = LaundryCoordinator()
        acts = run(co, (D, "running"), (D, "cooling"), (D, "end"))
        assert acts == [Action("notify", c.DRYER_COMPLETE)]

    def test_dryer_wrinkle_care_is_done_once(self):
        co = LaundryCoordinator()
        acts = run(co, (D, "running"), (D, "wrinkle_care"), (D, "end"), (D, "wrinkle_care"))
        assert notes(acts) == [c.DRYER_COMPLETE]

    def test_power_off_without_end_is_silent(self):
        co = LaundryCoordinator()
        assert run(co, (W, "running"), (W, "pause"), (W, "power_off")) == []

    def test_end_from_idle_is_not_a_finish(self):
        co = LaundryCoordinator()
        co.seed("power_off", "power_off")
        assert run(co, (W, "end")) == []

    def test_unavailable_blips_are_ignored(self):
        co = LaundryCoordinator()
        acts = run(co, (W, "running"), (W, "unavailable"), (W, "end"))
        assert notes(acts) == [c.WASHER_COMPLETE]

    def test_restart_mid_unavailable_does_not_fire(self):
        co = LaundryCoordinator()
        co.seed("unavailable", "power_off")
        assert run(co, (W, "end")) == []


class TestWasherFinishesWhileDryerRuns:
    def test_holds_then_sends_one_combined(self):
        co = LaundryCoordinator()
        co.seed("running", "running")
        assert run(co, (W, "end")) == []
        assert co.holding_washer
        acts = run(co, (D, "end"))
        assert acts == [Action("notify", c.BOTH_COMPLETE), Action("arm_reminder")]
        assert not co.holding_washer

    def test_dryer_cancelled_releases_washer_push(self):
        co = LaundryCoordinator()
        co.seed("running", "running")
        run(co, (W, "end"))
        acts = run(co, (D, "pause"), (D, "power_off"))
        assert acts == [Action("notify", c.WASHER_COMPLETE), Action("arm_reminder")]

    def test_washer_emptied_while_held_then_dryer_is_plain(self):
        co = LaundryCoordinator()
        co.seed("running", "running")
        run(co, (W, "end"))
        assert run(co, (W, "power_off")) == []
        assert notes(run(co, (D, "end"))) == [c.DRYER_COMPLETE]

    def test_dryer_paused_still_counts_as_running(self):
        co = LaundryCoordinator()
        co.seed("running", "pause")
        assert run(co, (W, "end")) == []
        assert co.holding_washer


class TestDryerFinishesWhileWasherRuns:
    def test_notify_now_default(self):
        co = LaundryCoordinator()
        co.seed("running", "running")
        assert notes(run(co, (D, "end"))) == [c.DRYER_COMPLETE]
        # Washer later finishes with the dryer idle: its own push + reminder.
        assert run(co, (W, "end")) == [Action("notify", c.WASHER_COMPLETE), Action("arm_reminder")]

    def test_wait_for_washer_policy_combines(self):
        co = LaundryCoordinator(dryer_first_policy=c.POLICY_WAIT_FOR_WASHER)
        co.seed("running", "running")
        assert run(co, (D, "end")) == []
        assert co.holding_dryer
        assert run(co, (W, "end")) == [Action("notify", c.BOTH_COMPLETE), Action("arm_reminder")]

    def test_wait_policy_dryer_emptied_then_washer_is_plain(self):
        co = LaundryCoordinator(dryer_first_policy=c.POLICY_WAIT_FOR_WASHER)
        co.seed("running", "running")
        run(co, (D, "end"), (D, "power_off"))
        assert notes(run(co, (W, "end"))) == [c.WASHER_COMPLETE]

    def test_wait_policy_washer_cancelled_releases_dryer(self):
        co = LaundryCoordinator(dryer_first_policy=c.POLICY_WAIT_FOR_WASHER)
        co.seed("running", "running")
        run(co, (D, "end"))
        assert notes(run(co, (W, "power_off"))) == [c.DRYER_COMPLETE]

    def test_bad_policy_rejected(self):
        with pytest.raises(ValueError):
            LaundryCoordinator(dryer_first_policy="sometimes")


class TestReminder:
    def test_fires_when_washer_still_in_end(self):
        co = LaundryCoordinator()
        run(co, (W, "running"), (W, "end"))
        assert co.reminder_due() == [Action("notify", c.WASHER_REMINDER)]
        assert co.reminder_due() == []  # one-shot

    def test_washer_opened_cancels(self):
        co = LaundryCoordinator()
        run(co, (W, "running"), (W, "end"))
        assert run(co, (W, "power_off")) == [Action("cancel_reminder")]
        assert co.reminder_due() == []

    def test_new_cycle_cancels(self):
        co = LaundryCoordinator()
        run(co, (W, "running"), (W, "end"))
        acts = run(co, (W, "initial"))
        assert acts == [Action("cancel_reminder")]

    def test_not_armed_while_held(self):
        co = LaundryCoordinator()
        co.seed("running", "running")
        assert run(co, (W, "end")) == []
        assert not co.reminder_armed

    def test_seed_rebuilds_washer_hold_after_restart(self):
        co = LaundryCoordinator()
        co.seed("end", "running")
        assert co.holding_washer
        assert not co.restore_reminder()
        assert run(co, (D, "end")) == [Action("notify", c.BOTH_COMPLETE), Action("arm_reminder")]

    def test_seed_rebuilds_dryer_hold_only_under_wait_policy(self):
        co = LaundryCoordinator(dryer_first_policy=c.POLICY_WAIT_FOR_WASHER)
        co.seed("running", "end")
        assert co.holding_dryer
        assert notes(run(co, (W, "end"))) == [c.BOTH_COMPLETE]
        co2 = LaundryCoordinator()
        co2.seed("running", "end")
        assert not co2.holding_dryer

    def test_restore_reminder_only_in_end(self):
        co = LaundryCoordinator()
        co.seed("end", "power_off")
        assert co.restore_reminder()
        co2 = LaundryCoordinator()
        co2.seed("power_off", "power_off")
        assert not co2.restore_reminder()


# --------------------------------------------------------------- messages
def test_format_duration():
    assert format_duration(120) == "2 h"
    assert format_duration(135) == "2 h 15 min"
    assert format_duration(45) == "45 min"


def test_messages():
    assert build_message(c.BOTH_COMPLETE)[0] == "Washer & dryer complete"
    title, msg = build_message(c.WASHER_REMINDER, washer_waiting_minutes=125)
    assert title == "Washer still needs switching"
    assert "2 h 5 min" in msg
    with pytest.raises(ValueError):
        build_message("bogus")


# --------------------------------------------------------------- app wrapper
class FakeApp(LaundryNotify):
    def __init__(self, args, states, now):
        self.args = args
        self._states = states
        self._clock = now
        self.calls = []
        self.timers = {}
        self._next = 0
        self.logs = []

    def _now(self):
        return self._clock

    def get_state(self, entity, attribute=None):
        st = self._states[entity]
        if attribute == "last_changed":
            return st.get("last_changed")
        return st["state"]

    def listen_state(self, cb, entity, **kwargs):
        pass

    def run_in(self, cb, seconds):
        self._next += 1
        self.timers[self._next] = (cb, seconds)
        return self._next

    def cancel_timer(self, handle):
        self.timers.pop(handle, None)

    def call_service(self, service, **kwargs):
        self.calls.append((service, kwargs))

    def log(self, msg, level="INFO"):
        self.logs.append(msg)

    def change(self, machine, entity, old, new):
        self._on_status(entity, "state", old, new, {"machine": machine})

    def fire_only_timer(self):
        (handle, (cb, _)), = self.timers.items()
        self.timers.pop(handle)
        cb({})


NOW = dt.datetime(2026, 11, 27, 20, 0, tzinfo=dt.timezone.utc)
WE, DE = "sensor.washer_current_status", "sensor.dryer_current_status"


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    path = tmp_path / "laundry_state.json"
    monkeypatch.setattr(sys.modules[__name__], "STATE_FILE", path, raising=False)
    yield path


STATE_FILE = None


def make_app(washer="power_off", dryer="power_off", last_changed=None, now=NOW, **args):
    states = {WE: {"state": washer, "last_changed": last_changed}, DE: {"state": dryer}}
    args.setdefault("state_file", str(STATE_FILE))
    app = FakeApp({"notify_services": ["notify.mobile_app_a", "notify.mobile_app_b"], **args}, states, now)
    app.initialize()
    return app


def titles(app):
    return [kw["title"] for _, kw in app.calls]


class TestApp:
    def test_washer_complete_sends_to_every_service_then_reminds(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        assert [s for s, _ in app.calls] == ["notify/mobile_app_a", "notify/mobile_app_b"]
        assert titles(app) == ["Washer complete", "Washer complete"]
        assert app.calls[0][1]["data"]["tag"] == "laundry_washer"
        (_, (cb, secs)), = app.timers.items()
        assert secs == 7200
        app._clock = NOW + dt.timedelta(hours=2)
        app.fire_only_timer()
        assert titles(app)[-1] == "Washer still needs switching"
        assert "2 h" in app.calls[-1][1]["message"]

    def test_opening_washer_cancels_timer(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        assert app.timers
        app.change(W, WE, "end", "power_off")
        assert not app.timers

    def test_combined_when_dryer_running(self):
        app = make_app(washer="running", dryer="running")
        app.change(W, WE, "spinning", "end")
        assert app.calls == []
        app.change(D, DE, "cooling", "end")
        assert titles(app) == ["Washer & dryer complete"] * 2
        assert app.timers

    def test_custom_reminder_minutes(self):
        app = make_app(washer="running", washer_reminder_minutes=90)
        app.change(W, WE, "spinning", "end")
        (_, (_, secs)), = app.timers.items()
        assert secs == 5400

    def test_restart_with_washer_in_end_rearms_remaining_time(self):
        app = make_app(washer="end", last_changed=(NOW - dt.timedelta(minutes=30)).isoformat())
        (_, (_, secs)), = app.timers.items()
        assert secs == 90 * 60
        assert app.calls == []

    def test_restart_after_reminder_window_skips_stale_reminder(self):
        app = make_app(washer="end", last_changed=(NOW - dt.timedelta(hours=5)).isoformat())
        assert app.timers == {}
        assert not app._coord.reminder_armed

    def test_notify_failure_does_not_stop_other_services(self):
        app = make_app(washer="running")
        sent = []

        def flaky(service, **kw):
            if service.endswith("_a"):
                raise RuntimeError("boom")
            sent.append(service)

        app.call_service = flaky
        app.change(W, WE, "spinning", "end")
        assert sent == ["notify/mobile_app_b"]


class TestRestart:
    def test_restart_during_hold_still_sends_combined(self):
        app = make_app(washer="end", dryer="running", last_changed=NOW.isoformat())
        assert app.timers == {}
        app.change(D, DE, "cooling", "end")
        assert titles(app) == ["Washer & dryer complete"] * 2
        assert app.timers

    def test_reminder_not_resent_after_ha_restart(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        app._clock = NOW + dt.timedelta(hours=2)
        app.fire_only_timer()
        assert titles(app)[-1] == "Washer still needs switching"
        # HA restart: last_changed reset to boot time, AppDaemon re-initialises.
        later = NOW + dt.timedelta(hours=3)
        app2 = make_app(washer="end", last_changed=later.isoformat(), now=later)
        assert app2.timers == {}
        assert app2.calls == []

    def test_pending_reminder_keeps_original_due_time(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        later = NOW + dt.timedelta(minutes=30)
        app2 = make_app(washer="end", last_changed=later.isoformat(), now=later)
        (_, (_, secs)), = app2.timers.items()
        assert secs == 90 * 60
        app2._clock = NOW + dt.timedelta(hours=2)
        app2.fire_only_timer()
        assert "2 h" in app2.calls[-1][1]["message"]

    def test_reminder_due_during_downtime_sends_on_start(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        later = NOW + dt.timedelta(hours=4)
        app2 = make_app(washer="end", last_changed=later.isoformat(), now=later)
        (_, (_, secs)), = app2.timers.items()
        assert secs == 1
        app2.fire_only_timer()
        assert "4 h" in app2.calls[-1][1]["message"]

    def test_washer_unavailable_at_start_restores_when_back(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        later = NOW + dt.timedelta(minutes=30)
        app2 = make_app(washer="unavailable", now=later)
        assert app2.timers == {}
        app2.change(W, WE, "unavailable", "end")
        assert app2.calls == []
        (_, (_, secs)), = app2.timers.items()
        assert secs == 90 * 60

    def test_opened_washer_clears_episode(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        app.change(W, WE, "end", "power_off")
        assert json.loads(STATE_FILE.read_text()) == {}

    def test_unavailable_blip_does_not_reset_finish_time(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")
        app._clock = NOW + dt.timedelta(minutes=60)
        app.change(W, WE, "end", "unavailable")
        app.change(W, WE, "unavailable", "end")
        app._clock = NOW + dt.timedelta(hours=2)
        app.fire_only_timer()
        assert "2 h" in app.calls[-1][1]["message"]

    def test_restart_after_washer_push_with_dryer_on_other_load_is_not_a_hold(self):
        app = make_app(washer="running")
        app.change(W, WE, "spinning", "end")  # Washer complete + reminder armed
        later = NOW + dt.timedelta(minutes=30)
        app2 = make_app(washer="end", dryer="running", last_changed=later.isoformat(), now=later)
        assert not app2._coord.holding_washer
        (_, (_, secs)), = app2.timers.items()
        assert secs == 90 * 60  # original reminder kept
        app2.change(D, DE, "cooling", "end")
        assert titles(app2) == ["Dryer complete"] * 2
