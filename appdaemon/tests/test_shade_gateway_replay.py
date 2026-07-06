"""Real-incident replay regression tests.

Drives the ACTUAL 2026-07-06 first-floor-bathroom-shade disconnect timeline
(recorded from HA history: the shade flapped 100%<->0% ~38 times over ~6h,
with several genuinely-healthy multi-minute gaps, then self-healed) through
the real ShadeGatewayChecker and the disconnect-aware BatteryChecker, under a
controlled clock.

Guards the two behaviours the whole feature exists to guarantee:
  1. BatteryChecker with disconnect_aware=True never pages "low battery" on
     the disconnect artifacts (old behaviour paged on every 0% reading).
  2. ShadeGatewayChecker stays silent through self-healing flaps and only
     escalates (critical page + one port-32 power-cycle) once a disconnect
     genuinely persists past the auto-restart grace deadline.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Mock hassapi before importing the apps (matches the other test modules).
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules.setdefault("hassapi", mock_hass)

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

import health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker as sgc  # noqa: E402
from health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker import (  # noqa: E402
    ShadeGatewayChecker,
    REPAIR_IN_PROGRESS,
)
from health_checks.checker_apps.battery_checker.battery_checker import (  # noqa: E402
    BatteryChecker,
)

SHADE = "sensor.test_shade_a_battery"  # stands in for first_floor_bathroom (name is irrelevant to the logic)

# Real recorded timeline (2026-07-06 UTC): (H, M, S, battery %).
_RAW = """
03:37:30 0
03:38:31 100
04:59:10 0
05:00:10 100
05:12:16 0
05:13:16 100
06:20:40 0
06:21:40 100
06:36:45 0
06:39:46 100
06:43:47 0
06:44:47 100
06:51:50 0
06:53:51 100
06:55:51 0
06:57:51 100
06:58:51 0
06:59:51 100
07:04:51 0
07:05:51 100
07:06:52 0
07:07:52 100
07:09:52 0
07:10:53 100
07:11:53 0
07:12:53 100
07:14:53 0
07:15:53 100
07:23:57 0
07:25:57 100
07:34:02 0
07:35:02 100
07:39:04 0
07:40:05 100
07:51:12 0
07:53:15 100
07:55:16 0
07:56:16 100
08:05:24 0
08:06:25 100
08:10:25 0
08:11:25 100
08:12:26 0
08:13:27 100
08:14:27 0
08:15:27 100
08:16:28 0
08:17:29 100
08:20:29 0
08:21:29 100
08:22:29 0
08:23:30 100
08:25:30 0
08:26:30 100
08:32:33 0
08:33:34 100
08:34:34 0
08:41:36 100
08:43:36 0
08:44:37 100
08:46:37 0
08:47:38 100
08:48:38 0
08:49:38 100
08:50:38 0
08:51:39 100
08:57:42 0
08:58:42 100
08:59:43 0
09:00:43 100
09:03:44 0
09:04:45 100
09:09:46 0
09:10:46 100
09:47:02 0
09:48:02 100
"""
_DAY = datetime.datetime(2026, 7, 6)
EVENTS = []
for _ln in _RAW.strip().splitlines():
    _hms, _v = _ln.split()
    _h, _m, _s = map(int, _hms.split(":"))
    EVENTS.append((_DAY.replace(hour=_h, minute=_m, second=_s), int(_v)))

ENTS = {
    SHADE: {"state": "100", "attributes": {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Shade A Battery"}},
    "sensor.test_shade_b_battery": {"state": "100", "attributes": {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Shade B Battery"}},
}

_ARGS = {
    "ha_url": "http://ha", "ha_token_env": "TOKEN",
    "checker_id": "shade_gateway", "checker_name": "Shade Gateway",
    "check_interval_s": 300,
    "entity_patterns": [{"include": r"sensor\.test_shade_.*_battery$"}],
    "disconnect_low_threshold": 5, "healthy_floor": 40,
    "recovery_settle_s": 900, "repair_button": "button.test_port32",
    "repair_settle_s": 180, "repair_recovery_wait_s": 900,
    "auto_repair_enabled_default": True, "auto_repair_delay_min_default": 120,
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_sgc():
    app = ShadeGatewayChecker(MagicMock(), MagicMock())
    app.args = dict(_ARGS)
    app.get_state = AsyncMock(
        side_effect=lambda entity_id=None, **k: (
            ENTS if entity_id is None
            else (ENTS[entity_id]["state"] if entity_id in ENTS else None)
        )
    )
    for meth in ("set_state", "call_service", "listen_state", "listen_event",
                 "fire_event", "run_in", "run_every", "cancel_timer", "log",
                 "create_task"):
        setattr(app, meth, MagicMock())
    return app


def _simulate_gateway():
    """Replay the timeline through ShadeGatewayChecker; return observed facts."""
    app = _make_sgc()
    clock = {"now": EVENTS[0][0] - datetime.timedelta(minutes=5)}
    fake_dt = types.SimpleNamespace(
        datetime=types.SimpleNamespace(
            now=lambda tz=None: clock["now"],
            fromisoformat=datetime.datetime.fromisoformat,
        ),
        timedelta=datetime.timedelta,
        timezone=datetime.timezone,
    )
    with patch.object(sgc, "datetime", fake_dt):
        app.initialize()
        _run(app._discover_entities())
        _run(app._seed_baselines())
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 120

        ev_i = 0
        self_healed_episodes = 0
        cur_start = None
        first_critical = None
        restart_at = None
        t = clock["now"]
        end = EVENTS[-1][0] + datetime.timedelta(hours=1)
        step = datetime.timedelta(seconds=30)
        while t <= end:
            clock["now"] = t
            while ev_i < len(EVENTS) and EVENTS[ev_i][0] <= t:
                clock["now"] = EVENTS[ev_i][0]
                app._process_reading(SHADE, str(EVENTS[ev_i][1]))
                clock["now"] = t
                ev_i += 1

            app._recompute_recovery()
            if app._repair_status != REPAIR_IN_PROGRESS:
                app._evaluate_auto_repair()
            res = app._build_results()[0]

            if app._disconnect_since is not None and cur_start is None:
                cur_start = app._disconnect_since
            if app._disconnect_since is None and cur_start is not None:
                self_healed_episodes += 1
                cur_start = None
            if res["status"] == "critical" and first_critical is None:
                first_critical = t
            if app._repair_status == REPAIR_IN_PROGRESS:
                restart_at = t
                break
            t += step

    return {
        "self_healed_episodes": self_healed_episodes,
        "first_critical": first_critical,
        "restart_at": restart_at,
        "repair_status": app._repair_status,
        "repair_attempted": app._repair_attempted_this_episode,
    }


def _battery_criticals(disconnect_aware: bool) -> int:
    """Count 0%-reading evaluations that return critical over the timeline."""
    app = BatteryChecker(MagicMock(), MagicMock())
    app.args = {
        "checker_id": "shade_batteries", "checker_name": "Shade Batteries",
        "warning_threshold": 25, "critical_threshold": 5,
        "disconnect_aware": disconnect_aware, "disconnect_healthy_floor": 40,
        "entity_patterns": [{"include": r"sensor\.test_shade_.*_battery$"}],
    }
    app.get_state = MagicMock(
        side_effect=lambda entity_id=None, **k: (
            ENTS if entity_id is None
            else (ENTS[entity_id]["state"] if entity_id in ENTS else None)
        )
    )
    for meth in ("set_state", "call_service", "fire_event", "run_in", "run_every", "log"):
        setattr(app, meth, MagicMock())
    app.initialize()
    app._entities = {SHADE: "Shade A"}
    if disconnect_aware:
        app._last_good_value = {SHADE: 100.0}  # healthy baseline seeded at discovery

    crit = 0
    try:
        for _when, val in EVENTS:
            ENTS[SHADE]["state"] = str(val)
            if app._evaluate_entity(SHADE, "Shade A")["status"] == "critical":
                crit += 1
    finally:
        ENTS[SHADE]["state"] = "100"
    return crit


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRealIncidentReplayBatteryChecker:
    def test_old_behaviour_would_have_paged_many_times(self):
        # Regression anchor: without the guard, every 0% reading is a critical
        # low-battery page — this is the storm the user actually received.
        assert _battery_criticals(disconnect_aware=False) > 10

    def test_disconnect_aware_never_pages_low_battery(self):
        # The fix: not a single 0% disconnect artifact escalates to critical.
        assert _battery_criticals(disconnect_aware=True) == 0


class TestRealIncidentReplayGateway:
    def test_stays_silent_during_self_healing_then_restarts_once(self):
        r = _simulate_gateway()

        # The early intermittent flaps self-heal (genuine multi-minute healthy
        # gaps) and must NOT trigger a restart or a page on their own.
        assert r["self_healed_episodes"] >= 2, (
            f"expected the early transient flaps to self-heal; "
            f"got {r['self_healed_episodes']} self-healed episodes"
        )

        # The sustained disconnect eventually crosses the 2h grace deadline
        # and triggers exactly one auto power-cycle of the PoE port.
        assert r["restart_at"] is not None, "expected an auto-restart once the disconnect persisted past grace"

        # No critical page is emitted before that restart — silence during grace.
        assert r["first_critical"] == r["restart_at"], (
            "checker paged critical before the auto-restart deadline"
        )

        # The escalation ran through the real decision path: _start_repair
        # moved the state machine to in_progress and armed the one-restart
        # -per-episode guard. (The button/press mechanics of _execute_repair
        # are covered directly in test_shade_gateway_checker.py.)
        assert r["repair_status"] == REPAIR_IN_PROGRESS
        assert r["repair_attempted"] is True

    def test_restart_lands_after_two_hours_of_sustained_disconnect(self):
        r = _simulate_gateway()
        # The sustained episode began ~06:20-06:36 UTC; a 120-min grace puts the
        # restart in the ~08:20-08:40 UTC window. Assert a generous band so the
        # test is robust to minor timing/step changes but still proves the grace
        # was honoured (not an immediate page, not a missed escalation).
        assert r["restart_at"] is not None
        lo = datetime.datetime(2026, 7, 6, 8, 0, 0)
        hi = datetime.datetime(2026, 7, 6, 9, 0, 0)
        assert lo <= r["restart_at"] <= hi, f"restart at {r['restart_at']} outside expected grace window"
