"""Execute the real YAML with fake states/time/services; never connect to HA.

Run serially: uv run --with jinja2 --with pyyaml --with pytest pytest -q scripts/automation-tests
This bounded interpreter covers the constructs used by these two automations.
HA's installed validator is checked separately; this is not a full HA runtime.
"""
import copy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment

ROOT = Path(__file__).resolve().parents[2]
TV = 'media_player.lg_webos_tv_oled83g4wua'
PENDING = 'input_boolean.basement_movie_room_tv_auto_off_enabled'
SENSORS = ['binary_sensor.basement_movie_inovelli_presence_occupancy',
           'binary_sensor.basement_rumpus_room_aqara_fp2_01_presence_sensor']
LIGHTS = ['light.basement_movie_room_lights', 'light.basement_movie_room_ambient_lighting']
LED = 'number.basement_movie_inovelli_presence_defaultled1colorwhenon'
LIGHTS_AUTOMATION = 'automation.light_inovelli_movie_room_motion_cleared_lights_off'
CANCELLED = 'input_boolean.basement_movie_room_tv_auto_off_cancelled'
TV_PATH = ROOT / 'home-assistant/automations/tv-timers/media_basement_movie_room_no_occupancy_tv_off.yaml'
LIGHT_PATH = ROOT / 'home-assistant/automations/occupancy-based-lighting/light_inovelli_movie_room_motion_cleared_lights_off.yaml'


class StopRun(Exception):
    pass


class ServiceFailure(Exception):
    pass


class Room:
    """Restart-mode automation executor, driven by state edges and fake deadlines.

    Starts occupied, with the recessed group on, the ambient group off and no hold.
    """
    def __init__(self, tv='on', announcement_seconds=0, lights='on'):
        self.configs = {'tv': yaml.safe_load(TV_PATH.read_text()),
                        'lights': yaml.safe_load(LIGHT_PATH.read_text())}
        self.states = {TV: tv, PENDING: 'off', SENSORS[0]: 'on', SENSORS[1]: 'on',
                       LIGHTS[0]: lights, LIGHTS[1]: 'off', LIGHTS_AUTOMATION: 'on', CANCELLED: 'off',
                       LED: '255', 'input_text.inovelli_manual_hold': '90',
                       'input_text.inovelli_auto_hold': '130',
                       'input_select.basement_movie_room_tv_auto_off_delay': '00:20:00',
                       'input_select.basement_movie_room_occupancy_off_delay': '00:05:00'}
        self.now = 0
        self.calls = []
        self.running = {}
        self.queue = []
        self.timers = []  # state triggers with `for:` waiting to fire: [due, name, id, entity, to_states]
        self.latency = announcement_seconds
        self.fail_announce = False
        self.env = NativeEnvironment()
        self.env.globals.update(states=lambda e: self.states.get(e, 'unknown'),
                                is_state=lambda e, s: self.states.get(e) == s,
                                as_timedelta=lambda s: timedelta(seconds=self.seconds(s)))
        self.edges = {}
        self.attach(*self.configs)

    def attach(self, *names):
        """(Re)attach template triggers. HA never fires one that is already true on attach."""
        for name in names:
            for i, t in enumerate(self.configs[name]['triggers']):
                if t['trigger'] == 'template':
                    self.edges[name, i] = bool(self.render(t['value_template'], {}))

    def render(self, value, variables):
        if isinstance(value, str) and ('{{' in value or '{%' in value):
            return self.env.from_string(value).render(**variables)
        if isinstance(value, dict):
            return {k: self.render(v, variables) for k, v in value.items()}
        return value

    @staticmethod
    def seconds(value):
        if isinstance(value, dict):
            return sum(float(value.get(k, 0)) * scale for k, scale in
                       [('hours', 3600), ('minutes', 60), ('seconds', 1)])
        if isinstance(value, (int, float)):
            return value
        h, m, s = map(float, value.strip().split(':'))
        return h * 3600 + m * 60 + s

    def condition(self, c, v):
        if isinstance(c, list):
            return all(self.condition(x, v) for x in c)
        kind = c['condition']
        if kind == 'state':
            expected = c['state'] if isinstance(c['state'], list) else [c['state']]
            return self.states.get(c['entity_id']) in expected
        if kind == 'template':
            return bool(self.render(c['value_template'], v))
        if kind == 'trigger':
            return v['trigger'].id in (c['id'] if isinstance(c['id'], list) else [c['id']])
        if kind == 'and':
            return all(self.condition(x, v) for x in c['conditions'])
        if kind == 'not':
            return not any(self.condition(x, v) for x in c['conditions'])
        if kind == 'or':
            return any(self.condition(x, v) for x in c['conditions'])
        raise AssertionError(kind)

    def sequence(self, steps, v):
        for a in steps:
            if 'condition' in a:
                if not self.condition(a, v):
                    raise StopRun
            elif 'stop' in a:
                raise StopRun
            elif 'choose' in a:
                for branch in a['choose']:
                    if self.condition(branch['conditions'], v):
                        yield from self.sequence(branch['sequence'], v)
                        break
                else:
                    yield from self.sequence(a.get('default', []), v)
            elif 'if' in a:
                yield from self.sequence(a.get('then' if self.condition(a['if'], v) else 'else', []), v)
            elif 'variables' in a:
                for k, value in a['variables'].items():
                    v[k] = self.render(value, v)
            elif 'repeat' in a:
                for item in a['repeat']['for_each']:
                    v['repeat'] = SimpleNamespace(item=SimpleNamespace(**item))
                    yield from self.sequence(a['repeat']['sequence'], v)
            elif 'sequence' in a:
                try:
                    yield from self.sequence(a['sequence'], v)
                except ServiceFailure:
                    if not a.get('continue_on_error'):
                        raise
            elif 'wait_template' in a:
                yield ('wait', self.seconds(self.render(a['timeout'], v)), a['wait_template'], v.copy())
            elif 'delay' in a:
                yield ('wait', self.seconds(self.render(a['delay'], v)), None, v.copy())
            elif 'action' in a:
                yield ('service', a['action'], a['target']['entity_id'], self.render(a.get('data', {}), v))
                if a['action'] == 'assist_satellite.announce' and self.fail_announce:
                    raise ServiceFailure
            else:
                raise AssertionError(a)

    def update(self, changes, user=None):
        """Apply state changes; `user` is the context user_id (None = an automation or device)."""
        previous = self.states.copy()
        self.states.update(changes)
        for name, c in self.configs.items():
            for i, t in enumerate(c['triggers']):
                fire = False
                to_state = None
                if t['trigger'] == 'template':
                    new = bool(self.render(t['value_template'], {}))
                    fire = new and not self.edges[name, i]
                    self.edges[name, i] = new
                elif t['trigger'] == 'state':
                    to = t.get('to')
                    to_states = to if isinstance(to, list) else None if to is None else [to]
                    for e in (t['entity_id'] if isinstance(t['entity_id'], list) else [t['entity_id']]):
                        old, new = previous.get(e), self.states.get(e)
                        if (old != new and (to_states is None or new in to_states)
                                and old == t.get('from', old) and old not in t.get('not_from', [])
                                and new not in t.get('not_to', [])):
                            if 'for' in t:
                                self.timers.append([self.now + self.seconds(t['for']), name, t['id'], e, to_states])
                            else:
                                fire = True
                                to_state = SimpleNamespace(state=new, context=SimpleNamespace(user_id=user))
                if fire:
                    self.queue.append((name, t['id'], to_state))

    def set(self, **changes):
        self.update(changes)
        self.drain()

    def fire_due_timers(self):
        # A `for:` timer is cancelled as soon as its entity leaves the matched states.
        self.timers = [x for x in self.timers if self.states.get(x[3]) in x[4]]
        for x in [x for x in self.timers if x[0] <= self.now]:
            self.timers.remove(x)
            self.queue.append((x[1], x[2], None))

    def cancel(self):
        """A person turns the pending toggle off (dashboard/app call: context.user_id set)."""
        self.update({PENDING: 'off'}, user='tom')
        self.drain()

    def fire(self, kind, value):
        for name, c in self.configs.items():
            for t in c['triggers']:
                if t['trigger'] == kind and value in (t.get('event'), t.get('event_type')):
                    self.queue.append((name, t['id'], None))
        self.drain()

    def ha_start(self):
        """HA restart: every run dies, triggers re-attach, then the start event fires."""
        self.running.clear()
        self.timers.clear()
        self.attach(*self.configs)
        self.fire('homeassistant', 'start')

    def reload(self, name=None):
        """Reload one automation (None = an unrelated one), then HA fires automation_reloaded."""
        if name:
            self.running.pop(name, None)
            self.attach(name)
        self.fire('event', 'automation_reloaded')

    def drain(self):
        for _ in range(100):  # safety bound on immediate actions, no CPU/load loops
            while self.queue:
                name, trigger, to_state = self.queue.pop(0)
                assert self.configs[name]['mode'] == 'restart'
                v = {'trigger': SimpleNamespace(id=trigger, to_state=to_state),
                     'this': {'attributes': {'current': int(name in self.running)}}}
                if not self.condition(self.configs[name].get('conditions', []), v):
                    continue  # conditions fail before restart mode stops a run
                self.running[name] = [self.sequence(self.configs[name]['actions'], v), None]
            progress = False
            for name, run in list(self.running.items()):
                wait = run[1]
                if wait and self.now < wait[0] and not (wait[1] and self.render(wait[1], wait[2])):
                    continue
                progress = True
                try:
                    op = next(run[0])
                except (StopIteration, StopRun):
                    self.running.pop(name, None)
                    continue
                if op[0] == 'wait':
                    run[1] = (self.now + op[1], op[2], op[3])
                else:
                    _, service, target, data = op
                    run[1] = None
                    self.calls.append((self.now, service, target, data))
                    if service.startswith('input_boolean.'):
                        self.update({target: 'on' if service.endswith('turn_on') else 'off'})
                    elif service == 'media_player.turn_off':
                        self.update({TV: 'off'})
                    elif service == 'light.turn_off':
                        self.update({light: 'off' for light in target})
                    elif service == 'assist_satellite.announce':
                        if self.latency and not self.fail_announce:
                            run[1] = (self.now + self.latency, None, {})
                if self.queue:
                    break  # process restart triggers before the next service action
            if not progress and not self.queue:
                return
        raise AssertionError('Immediate execution did not settle')

    def advance(self, seconds):
        end = self.now + seconds
        while self.now < end:
            deadlines = [r[1][0] for r in self.running.values() if r[1] and r[1][0] > self.now]
            deadlines += [x[0] for x in self.timers if x[0] > self.now]
            self.now = min([end] + deadlines)
            self.fire_due_timers()
            self.drain()

    def empty(self):
        self.set(**{s: 'off' for s in SENSORS})

    def device_offs(self):
        return [c for c in self.calls if c[1] in ['light.turn_off', 'media_player.turn_off']]

    def announcements(self):
        return [c for c in self.calls if c[1] == 'assist_satellite.announce']


def test_warning_schedule_and_coordinated_shutdown():
    r = Room()
    r.empty()
    r.advance(1799)
    assert not r.device_offs()
    r.advance(1)
    warnings = [c for c in r.calls if c[1] == 'assist_satellite.announce']
    assert [c[0] for c in warnings] == [900, 1200, 1500, 1740]
    assert [c[3]['message'] for c in warnings] == [
        f"Movie Room appears empty. Turning off the TV and lights in {m} minute{'s' if m != 1 else ''}."
        for m in [15, 10, 5, 1]]
    assert [(c[0], c[1], c[2]) for c in r.device_offs()] == [
        (1800, 'light.turn_off', LIGHTS), (1800, 'media_player.turn_off', TV)]
    assert r.states[PENDING] == 'off'


@pytest.mark.parametrize('at', [100, 900, 1100, 1400, 1700, 1799])
@pytest.mark.parametrize('sensor', SENSORS)
@pytest.mark.parametrize('state', ['on', 'unavailable', 'unknown'])
def test_either_sensor_cancels_every_wait(at, sensor, state):
    r = Room()
    r.empty()
    r.advance(at)
    warnings_before = len(r.calls)
    r.set(**{sensor: state})
    assert r.states[PENDING] == 'off'
    r.advance(2000)
    assert not r.device_offs()
    assert not any(c[1] == 'assist_satellite.announce' for c in r.calls[warnings_before:])


@pytest.mark.parametrize('at', [100, 1100, 1400, 1700, 1799])
def test_pending_toggle_cancels(at):
    r = Room()
    r.empty()
    r.advance(at)
    r.cancel()
    r.advance(2000)
    assert not r.device_offs()
    assert r.states[PENDING] == 'off'


@pytest.mark.parametrize('at', [100, 1100, 1400, 1700, 1799])
@pytest.mark.parametrize('state', ['off', 'unavailable', 'unknown'])
@pytest.mark.parametrize('lights', ['on', 'off'])
def test_tv_inactive_mid_countdown_hands_over_to_fresh_silent_lights_countdown(at, state, lights):
    r = Room(lights=lights)
    r.empty()
    r.advance(at)
    spoken = len(r.announcements())
    r.set(**{TV: state})
    if lights == 'off':
        assert r.states[PENDING] == 'off'
        r.advance(4000)
        assert not r.device_offs()
    else:
        assert r.states[PENDING] == 'on'
        r.advance(1799)
        assert not r.device_offs()
        r.advance(1)
        assert [(c[0], c[1]) for c in r.device_offs()] == [(at + 1800, 'light.turn_off')]
        assert r.states[PENDING] == 'off'
    assert len(r.announcements()) == spoken


def test_off_to_unavailable_during_lights_only_countdown_does_not_restart_it():
    r = Room()
    r.empty()
    r.advance(1100)
    r.set(**{TV: 'off'})
    r.advance(120)
    r.set(**{TV: 'unavailable'})
    r.advance(1680)
    assert [(c[0], c[1]) for c in r.device_offs()] == [(2900, 'light.turn_off')]


def test_tv_turns_on_during_lights_cleanup_gets_full_warned_cycle():
    r = Room(tv='off')
    r.empty()
    r.advance(299)
    r.set(**{TV: 'on'})
    r.advance(1501)
    assert not r.device_offs()
    r.advance(299)
    assert [(c[0], c[1]) for c in r.device_offs()] == [(2099, 'light.turn_off'), (2099, 'media_player.turn_off')]


@pytest.mark.parametrize('sensor', SENSORS)
def test_one_occupied_sensor_prevents_any_countdown(sensor):
    r = Room()
    r.set(**{s: ('on' if s == sensor else 'off') for s in SENSORS})
    r.advance(2000)
    assert not r.calls
    assert r.states[PENDING] == 'off'


@pytest.mark.parametrize('at', [299, 1100, 1799])
@pytest.mark.parametrize('sensor', SENSORS)
@pytest.mark.parametrize('value', ['on', 'unknown', 'unavailable'])
def test_lights_only_countdown_cancelled_by_either_sensor(at, sensor, value):
    r = Room(tv='off')
    r.empty()
    r.advance(at)
    r.set(**{sensor: value})
    assert r.states[PENDING] == 'off'
    r.advance(4000)
    assert not r.device_offs()


def test_tv_playback_state_changes_do_not_reset_countdown():
    r = Room()
    r.empty()
    r.advance(1100)
    r.set(**{TV: 'playing'})
    r.advance(200)
    r.set(**{TV: 'paused'})
    r.advance(500)
    assert [c[0] for c in r.device_offs()] == [1800, 1800]


@pytest.mark.parametrize('tv', ['off', 'unavailable', 'unknown'])
def test_lights_only_countdown_is_silent_with_unified_timing_and_no_tv_command(tv):
    r = Room(tv=tv)
    r.empty()
    r.advance(299)
    assert not r.device_offs()  # the separate 5-minute lights cleanup is gone
    r.advance(1500)
    assert not r.device_offs()
    assert r.states[PENDING] == 'on'
    r.advance(1)
    assert [(c[0], c[1], c[2]) for c in r.device_offs()] == [(1800, 'light.turn_off', LIGHTS)]
    assert not r.announcements()
    assert r.states[PENDING] == 'off'


@pytest.mark.parametrize('tv', ['on', 'playing', 'paused'])
def test_confirmed_active_tv_gets_spoken_countdown_and_tv_off(tv):
    r = Room(tv=tv)
    r.empty()
    r.advance(1800)
    assert [c[0] for c in r.announcements()] == [900, 1200, 1500, 1740]
    assert [(c[0], c[1]) for c in r.device_offs()] == [(1800, 'light.turn_off'), (1800, 'media_player.turn_off')]


@pytest.mark.parametrize('tv', ['unavailable', 'unknown'])
def test_tv_unavailable_is_not_active_and_does_not_override_a_hold(tv):
    # Lights off: an unavailable TV starts nothing and is never sent a command.
    r = Room(tv=tv, lights='off')
    r.empty()
    r.advance(4000)
    assert not r.device_offs() and not r.announcements()
    assert r.states[PENDING] == 'off'
    # Lights on with a hold: an unavailable TV does not override the hold.
    r = Room(tv=tv)
    r.set(**{LED: '130'})
    r.empty()
    r.advance(4000)
    assert not r.device_offs() and not r.announcements()
    assert r.states[PENDING] == 'off'


HOLDS = [{LED: '90'}, {LED: '130'}, {LIGHTS_AUTOMATION: 'off'}]


@pytest.mark.parametrize('hold', HOLDS)
@pytest.mark.parametrize('when', [0, 299, 1100, 1799])
def test_lights_only_hold_blocks_or_cancels_countdown(hold, when):
    r = Room(tv='off')
    if when == 0:
        r.set(**hold)
    r.empty()
    r.advance(when)
    r.set(**hold)
    assert r.states[PENDING] == 'off'
    r.advance(4000)
    assert not r.device_offs()


@pytest.mark.parametrize('hold', HOLDS)
def test_coordinated_shutdown_preserves_light_hold_but_tv_policy_unchanged(hold):
    r = Room()
    r.empty()
    r.advance(1799)
    r.set(**hold)
    r.advance(1)
    assert [c[1] for c in r.device_offs()] == ['media_player.turn_off']
    r.advance(4000)
    assert [c[1] for c in r.device_offs()] == ['media_player.turn_off']


def test_lights_turned_off_by_hand_cancel_lights_only_countdown():
    r = Room(tv='unavailable')
    r.empty()
    r.advance(1000)
    r.set(**{LIGHTS[0]: 'off'})
    assert r.states[PENDING] == 'off'
    r.advance(4000)
    assert not r.device_offs()


def test_occupancy_during_announcement_cancels_shutdown():
    r = Room(announcement_seconds=10)
    r.empty()
    r.advance(905)
    r.set(**{SENSORS[0]: 'on'})
    r.advance(2000)
    assert not r.device_offs()
    assert r.states[PENDING] == 'off'


def test_warning_lead_time_includes_realistic_announcement_duration():
    r = Room(announcement_seconds=10)
    r.empty()
    r.advance(1840)
    assert r.device_offs()[0][0] - 900 >= 900


def test_failed_announcement_cannot_shut_down_devices():
    r = Room()
    r.fail_announce = True
    r.empty()
    r.advance(2500)
    assert not r.device_offs()
    assert r.states[PENDING] == 'off'


def test_reoccupancy_then_empty_starts_a_fresh_full_cycle():
    r = Room()
    r.empty()
    r.advance(1000)
    r.set(**{SENSORS[1]: 'on'})
    r.advance(10)
    r.empty()
    r.advance(1799)
    assert not r.device_offs()
    r.advance(1)
    assert all(c[0] == 2810 for c in r.device_offs())


def held_vacant_lit_room(hold, tv='unavailable', hours=4):
    """The 2026-10-19 state: a hold is set, the room empties with lights on, hours pass."""
    r = Room(tv=tv)
    r.set(**hold)
    r.empty()
    r.advance(hours * 3600)
    assert not r.device_offs() and r.states[PENDING] == 'off'
    return r


@pytest.mark.parametrize('tv', ['unavailable', 'off', 'unknown'])
@pytest.mark.parametrize('hold_led', ['90', '130'])
def test_hold_release_rechecks_vacant_room_and_starts_silent_countdown_immediately(tv, hold_led):
    r = held_vacant_lit_room({LED: hold_led, LIGHTS_AUTOMATION: 'off'}, tv=tv)
    t0 = r.now
    r.set(**{LED: '255'})  # the clear-hold scripts restore LED1 first ...
    assert r.states[PENDING] == 'off'  # ... while the automation is still off
    r.set(**{LIGHTS_AUTOMATION: 'on'})  # ... then re-enable the automation
    assert r.states[PENDING] == 'on'
    r.advance(899)
    assert not r.device_offs()
    r.advance(1)
    assert [(c[0] - t0, c[1], c[2]) for c in r.device_offs()] == [(900, 'light.turn_off', LIGHTS)]
    assert not r.announcements()
    assert r.states[PENDING] == 'off'


def test_hold_release_with_late_led_confirmation_still_rechecks():
    r = held_vacant_lit_room({LED: '130', LIGHTS_AUTOMATION: 'off'})
    t0 = r.now
    r.set(**{LIGHTS_AUTOMATION: 'on'})  # re-enabled after the scripts' 5 s LED wait timed out
    assert r.states[PENDING] == 'off'  # LED1 still shows the hold colour
    r.advance(10)
    r.set(**{LED: '255'})
    r.advance(900)
    assert [(c[0] - t0, c[1]) for c in r.device_offs()] == [(910, 'light.turn_off')]
    assert not r.announcements()


@pytest.mark.parametrize('group', LIGHTS)
@pytest.mark.parametrize('tv', ['unavailable', 'off'])
def test_lights_turned_on_in_empty_room_start_silent_countdown_immediately(group, tv):
    r = Room(tv=tv, lights='off')
    r.empty()
    r.advance(3600)
    t0 = r.now
    r.set(**{group: 'on'})
    assert r.states[PENDING] == 'on'
    r.advance(899)
    assert not r.device_offs()
    r.advance(1)
    assert [(c[0] - t0, c[1], c[2]) for c in r.device_offs()] == [(900, 'light.turn_off', LIGHTS)]
    assert not r.announcements()


@pytest.mark.parametrize('recheck', ['ha_start', 'reload_self', 'reload_other'])
def test_start_and_reload_recheck_a_vacant_lit_room(recheck):
    r = Room(tv='unavailable', lights='off')
    r.empty()
    r.advance(3600)
    r.states[LIGHTS[0]] = 'on'  # e.g. restored on during a restart, no edge seen
    t0 = r.now
    getattr(r, 'ha_start' if recheck == 'ha_start' else 'reload')(
        *(['tv'] if recheck == 'reload_self' else []))
    assert r.states[PENDING] == 'on'
    r.advance(900)
    assert [(c[0] - t0, c[1]) for c in r.device_offs()] == [(900, 'light.turn_off')]
    assert not r.announcements()


@pytest.mark.parametrize('recheck', ['ha_start', 'reload_self'])
@pytest.mark.parametrize('tv', ['on', 'unavailable'])
def test_killed_countdown_is_recovered_immediately(recheck, tv):
    r = Room(tv=tv)
    r.empty()
    r.advance(1000)
    t0 = r.now
    getattr(r, 'ha_start' if recheck == 'ha_start' else 'reload')(*(['tv'] if recheck == 'reload_self' else []))
    assert r.states[PENDING] == 'on'
    r.advance(900)
    expected = [(900, 'light.turn_off')] + ([(900, 'media_player.turn_off')] if tv == 'on' else [])
    assert [(c[0] - t0, c[1]) for c in r.device_offs()] == expected
    # A spoken (TV-active) recovery warns at once, still 15 minutes before shutdown.
    assert [c[0] - t0 for c in r.announcements() if c[0] >= t0] == ([0, 300, 600, 840] if tv == 'on' else [])


RECHECKS = {'reload_other': lambda r: r.reload(),
            'ambient_on': lambda r: r.set(**{LIGHTS[1]: 'on'}),
            'led_change': lambda r: r.set(**{LED: '200'}),
            'hold_release_edge': lambda r: (r.states.update({LIGHTS_AUTOMATION: 'off'}),
                                            r.set(**{LIGHTS_AUTOMATION: 'on'}))}


@pytest.mark.parametrize('recheck', RECHECKS)
@pytest.mark.parametrize('tv', ['on', 'unavailable'])
@pytest.mark.parametrize('at', [100, 1000, 1700])
def test_recheck_never_restarts_a_running_countdown(recheck, tv, at):
    r = Room(tv=tv)
    r.empty()
    r.advance(at)
    RECHECKS[recheck](r)
    r.advance(1800 - at)
    expected = [(1800, 'light.turn_off')] + ([(1800, 'media_player.turn_off')] if tv == 'on' else [])
    assert [(c[0], c[1]) for c in r.device_offs()] == expected
    assert [c[0] for c in r.announcements()] == ([900, 1200, 1500, 1740] if tv == 'on' else [])


@pytest.mark.parametrize('blocker', [{SENSORS[0]: 'on'}, {SENSORS[1]: 'unavailable'}, *HOLDS])
@pytest.mark.parametrize('recheck', ['ha_start', 'reload_other', 'ambient_on'])
def test_recheck_does_nothing_when_occupied_or_held(blocker, recheck):
    r = Room(tv='unavailable')
    r.set(**blocker)
    r.empty() if SENSORS[0] not in blocker and SENSORS[1] not in blocker else r.set(
        **{s: 'off' for s in SENSORS if s not in blocker})
    r.advance(3600)
    {'ha_start': r.ha_start, 'reload_other': r.reload,
     'ambient_on': lambda: r.set(**{LIGHTS[1]: 'on'})}[recheck]()
    r.advance(4000)
    assert not r.device_offs() and not r.announcements()
    assert r.states[PENDING] == 'off'


@pytest.mark.parametrize('at', [0, 100, 450, 840, 899])
@pytest.mark.parametrize('sensor', SENSORS)
@pytest.mark.parametrize('value', ['on', 'unavailable'])
def test_occupancy_cancels_a_recheck_countdown(at, sensor, value):
    r = Room(tv='unavailable', lights='off')
    r.empty()
    r.advance(3600)
    r.set(**{LIGHTS[0]: 'on'})
    r.advance(at)
    r.set(**{sensor: value})
    assert r.states[PENDING] == 'off'
    r.advance(4000)
    assert not r.device_offs()


def test_lights_automation_reload_blip_does_not_disturb_a_lights_only_countdown():
    r = Room(tv='unavailable')
    r.empty()
    r.advance(1000)
    r.set(**{LIGHTS_AUTOMATION: 'unavailable'})  # entity removed while it reloads
    r.set(**{LIGHTS_AUTOMATION: 'on'})
    r.reload('lights')
    r.advance(800)
    assert [(c[0], c[1]) for c in r.device_offs()] == [(1800, 'light.turn_off')]


def test_lights_automation_has_no_separate_shutoff_path():
    config = yaml.safe_load(LIGHT_PATH.read_text())
    assert config['triggers'] == [] and config['actions'] == []


# --- Ruling Q6: a manual cancel sticks until occupancy is detected or the TV turns off ---

STARTS = {'reload_other': lambda r: r.reload(),
          'reload_self': lambda r: r.reload('tv'),
          'ha_start': lambda r: r.ha_start(),
          'ambient_on': lambda r: r.set(**{LIGHTS[1]: 'on'}),
          'lights_off_then_on': lambda r: (r.set(**{LIGHTS[0]: 'off'}), r.set(**{LIGHTS[0]: 'on'})),
          'led_change': lambda r: r.set(**{LED: '200'}),
          'hold_release': lambda r: (r.set(**{LIGHTS_AUTOMATION: 'off'}), r.set(**{LIGHTS_AUTOMATION: 'on'})),
          'sensor_blip': lambda r: (r.set(**{SENSORS[1]: 'unavailable'}), r.set(**{SENSORS[1]: 'off'}))}


@pytest.mark.parametrize('start', STARTS)
@pytest.mark.parametrize('tv', ['on', 'unavailable'])
def test_manual_cancel_sticks_through_every_recheck(start, tv):
    r = Room(tv=tv)
    r.empty()
    r.advance(1000)
    spoken = len(r.announcements())
    r.cancel()
    assert r.states[PENDING] == 'off' and r.states[CANCELLED] == 'on'
    STARTS[start](r)
    r.advance(4000)
    assert not r.device_offs()
    assert len(r.announcements()) == spoken
    assert r.states[PENDING] == 'off' and r.states[CANCELLED] == 'on'


def test_manual_cancel_of_lights_only_countdown_sticks_when_the_tv_turns_on():
    r = Room(tv='unavailable')
    r.empty()
    r.advance(1000)
    r.cancel()
    r.set(**{TV: 'on'})
    r.advance(4000)
    assert not r.device_offs() and not r.announcements()


@pytest.mark.parametrize('sensor', SENSORS)
@pytest.mark.parametrize('tv', ['on', 'unavailable'])
def test_occupancy_then_vacancy_rearms_after_a_manual_cancel(sensor, tv):
    r = Room(tv=tv)
    r.empty()
    r.advance(1000)
    r.cancel()
    r.advance(600)
    r.set(**{sensor: 'on'})
    assert r.states[CANCELLED] == 'off'
    r.advance(60)
    t0 = r.now
    r.empty()
    r.advance(1800)
    expected = [(1800, 'light.turn_off')] + ([(1800, 'media_player.turn_off')] if tv == 'on' else [])
    assert [(c[0] - t0, c[1]) for c in r.device_offs()] == expected
    assert [c[0] - t0 for c in r.announcements() if c[0] >= t0] == ([900, 1200, 1500, 1740] if tv == 'on' else [])


@pytest.mark.parametrize('state', ['off', 'unavailable'])
def test_tv_turning_off_rearms_after_a_manual_cancel(state):
    r = Room()
    r.empty()
    r.advance(1000)
    r.cancel()
    r.reload()
    r.advance(500)
    assert not r.device_offs() and r.states[CANCELLED] == 'on'
    spoken = len(r.announcements())
    t0 = r.now
    r.set(**{TV: state})
    r.advance(59)
    assert r.states[CANCELLED] == 'on' and r.states[PENDING] == 'off'
    r.advance(1)  # off for a minute: the cancel ends and a fresh full cycle starts
    assert r.states[CANCELLED] == 'off' and r.states[PENDING] == 'on'
    r.advance(1800)
    assert [(c[0] - t0, c[1]) for c in r.device_offs()] == [(1860, 'light.turn_off')]
    assert len(r.announcements()) == spoken


def test_tv_turning_off_with_lights_off_clears_the_cancel_and_rechecks_work_again():
    r = Room(lights='off')
    r.empty()
    r.advance(1000)
    r.cancel()
    r.set(**{TV: 'off'})
    r.advance(60)
    assert r.states[CANCELLED] == 'off' and r.states[PENDING] == 'off'
    r.advance(600)
    t0 = r.now
    r.set(**{LIGHTS[0]: 'on'})
    r.advance(900)
    assert [(c[0] - t0, c[1]) for c in r.device_offs()] == [(900, 'light.turn_off')]


def test_automation_clearing_pending_is_never_a_manual_cancel():
    r = Room(tv='unavailable')
    r.empty()
    r.advance(1800)  # a completed silent run clears pending itself
    assert [c[1] for c in r.device_offs()] == ['light.turn_off']
    assert r.states[CANCELLED] == 'off'
    r.set(**{LIGHTS[0]: 'on'})  # so a recheck still starts a countdown
    assert r.states[PENDING] == 'on'
    r.advance(100)
    r.set(**{SENSORS[0]: 'on'})  # occupancy clears pending itself too
    assert r.states[PENDING] == 'off' and r.states[CANCELLED] == 'off'
    r.set(**{SENSORS[0]: 'off'})
    t0 = r.now
    r.advance(1800)
    assert [(c[0] - t0, c[1]) for c in r.device_offs()][-1] == (1800, 'light.turn_off')


@pytest.mark.parametrize('blip', ['unavailable', 'off'])
def test_tv_connection_blip_does_not_end_a_manual_cancel(blip):
    r = Room()
    r.empty()
    r.advance(1000)
    spoken = len(r.announcements())
    r.cancel()
    r.set(**{TV: blip})
    r.advance(30)
    r.set(**{TV: 'on'})
    r.advance(4000)
    assert r.states[CANCELLED] == 'on'
    assert not r.device_offs() and len(r.announcements()) == spoken


def test_occupancy_missed_during_a_restart_still_ends_a_manual_cancel():
    r = Room(tv='unavailable')
    r.empty()
    r.advance(1000)
    r.cancel()
    r.states[SENSORS[0]] = 'on'  # someone walked in while HA was down: no edge is seen
    r.ha_start()
    assert r.states[CANCELLED] == 'off'
    t0 = r.now
    r.set(**{SENSORS[0]: 'off'})
    r.advance(1800)
    assert [(c[0] - t0, c[1]) for c in r.device_offs()] == [(1800, 'light.turn_off')]


def test_pending_turned_off_while_someone_is_detected_does_not_stick():
    r = Room()  # occupied
    r.set(**{PENDING: 'on'})
    r.cancel()
    assert r.states[CANCELLED] == 'off'


def test_manual_cancel_survives_ha_restart():
    r = Room(tv='unavailable')
    r.empty()
    r.advance(1000)
    r.cancel()
    r.ha_start()
    r.advance(4000)
    assert not r.device_offs() and r.states[CANCELLED] == 'on'


def test_both_mirrors_and_incident_rollback_remain_consistent():
    mirror = ROOT / 'home-assistant/automations/media_movie_room_no_occupancy_tv_off.yaml'
    assert yaml.safe_load(mirror.read_text()) == yaml.safe_load(TV_PATH.read_text())
    import json
    evidence = ROOT / '.agents/plans/movie-room-off-2026-10-17-evidence'
    original = json.loads((evidence / 'automation_media_basement_movie_room_no_occupancy_tv_off.json').read_text())
    assert original['config_hash'] == 'd78c34ad1a2a661c'
