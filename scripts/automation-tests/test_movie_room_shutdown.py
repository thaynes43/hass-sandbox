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
TV_PATH = ROOT / 'home-assistant/automations/tv-timers/media_basement_movie_room_no_occupancy_tv_off.yaml'
LIGHT_PATH = ROOT / 'home-assistant/automations/occupancy-based-lighting/light_inovelli_movie_room_motion_cleared_lights_off.yaml'


class StopRun(Exception):
    pass


class ServiceFailure(Exception):
    pass


class Room:
    """Restart-mode automation executor, driven by state edges and fake deadlines."""
    def __init__(self, tv='on', announcement_seconds=0):
        self.configs = {'tv': yaml.safe_load(TV_PATH.read_text()),
                        'lights': yaml.safe_load(LIGHT_PATH.read_text())}
        self.states = {TV: tv, PENDING: 'off', SENSORS[0]: 'on', SENSORS[1]: 'on',
                       LED: '255', 'input_text.inovelli_manual_hold': '90',
                       'input_text.inovelli_auto_hold': '130',
                       'input_select.basement_movie_room_tv_auto_off_delay': '00:20:00',
                       'input_select.basement_movie_room_occupancy_off_delay': '00:05:00'}
        self.now = 0
        self.calls = []
        self.running = {}
        self.queue = []
        self.latency = announcement_seconds
        self.fail_announce = False
        self.env = NativeEnvironment()
        self.env.globals.update(states=lambda e: self.states.get(e, 'unknown'),
                                is_state=lambda e, s: self.states.get(e) == s,
                                as_timedelta=lambda s: timedelta(seconds=self.seconds(s)))
        self.edges = {(name, i): bool(self.render(t['value_template'], {}))
                      for name, c in self.configs.items() for i, t in enumerate(c['triggers'])
                      if t['trigger'] == 'template'}

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

    def update(self, changes):
        previous = self.states.copy()
        self.states.update(changes)
        for name, c in self.configs.items():
            for i, t in enumerate(c['triggers']):
                fire = False
                if t['trigger'] == 'template':
                    new = bool(self.render(t['value_template'], {}))
                    fire = new and not self.edges[name, i]
                    self.edges[name, i] = new
                elif t['trigger'] == 'state':
                    e = t['entity_id']
                    fire = previous.get(e) != self.states.get(e) and self.states.get(e) == t['to']
                if fire:
                    self.queue.append((name, t['id']))

    def set(self, **changes):
        self.update(changes)
        self.drain()

    def drain(self):
        for _ in range(100):  # safety bound on immediate actions, no CPU/load loops
            while self.queue:
                name, trigger = self.queue.pop(0)
                assert self.configs[name]['mode'] == 'restart'
                self.running[name] = [self.sequence(self.configs[name]['actions'], {'trigger': SimpleNamespace(id=trigger)}), None]
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
            self.now = min([end] + deadlines)
            self.drain()

    def empty(self):
        self.set(**{s: 'off' for s in SENSORS})

    def device_offs(self):
        return [c for c in self.calls if c[1] in ['light.turn_off', 'media_player.turn_off']]


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
    r.set(**{PENDING: 'off'})
    r.advance(2000)
    assert not r.device_offs()
    assert r.states[PENDING] == 'off'


@pytest.mark.parametrize('at', [100, 1100, 1400, 1700, 1799])
@pytest.mark.parametrize('state', ['off', 'unavailable', 'unknown'])
def test_tv_inactive_cancels_and_only_confirmed_off_starts_fresh_cleanup(at, state):
    r = Room()
    r.empty()
    r.advance(at)
    r.set(**{TV: state})
    assert r.states[PENDING] == 'off'
    r.advance(299)
    assert not r.device_offs()
    r.advance(1)
    assert [c[1] for c in r.device_offs()] == (['light.turn_off'] if state == 'off' else [])


def test_off_to_unavailable_after_confirmed_off_still_cleans_up():
    r = Room()
    r.empty()
    r.advance(1100)
    r.set(**{TV: 'off'})
    r.advance(120)
    r.set(**{TV: 'unavailable'})
    r.advance(180)
    assert [(c[0], c[1]) for c in r.device_offs()] == [(1400, 'light.turn_off')]


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


@pytest.mark.parametrize('sensor', SENSORS)
@pytest.mark.parametrize('value', ['on', 'unknown', 'unavailable'])
def test_lights_only_cleanup_cancelled_by_either_sensor(sensor, value):
    r = Room(tv='off')
    r.empty()
    r.advance(299)
    r.set(**{sensor: value})
    r.advance(500)
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


@pytest.mark.parametrize('tv', ['on', 'playing', 'paused', 'unavailable', 'unknown'])
def test_lights_worker_does_not_start_without_confirmed_tv_off(tv):
    r = Room(tv=tv)
    r.empty()
    r.advance(301)
    assert not r.device_offs()


@pytest.mark.parametrize('hold', ['90', '130'])
@pytest.mark.parametrize('when', [0, 299])
def test_lights_only_holds_before_and_after_delay(hold, when):
    r = Room(tv='off')
    if when == 0:
        r.set(**{LED: hold})
    r.empty()
    r.advance(when)
    r.set(**{LED: hold})
    r.advance(400)
    assert not r.device_offs()


@pytest.mark.parametrize('hold', ['90', '130'])
def test_coordinated_shutdown_preserves_light_hold_but_tv_policy_unchanged(hold):
    r = Room()
    r.empty()
    r.advance(1799)
    r.set(**{LED: hold})
    r.advance(1)
    assert [c[1] for c in r.device_offs()] == ['media_player.turn_off']


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


def test_both_mirrors_and_incident_rollback_remain_consistent():
    mirror = ROOT / 'home-assistant/automations/media_movie_room_no_occupancy_tv_off.yaml'
    assert yaml.safe_load(mirror.read_text()) == yaml.safe_load(TV_PATH.read_text())
    import json
    evidence = ROOT / '.agents/plans/movie-room-off-2026-10-17-evidence'
    original = json.loads((evidence / 'automation_media_basement_movie_room_no_occupancy_tv_off.json').read_text())
    assert original['config_hash'] == 'd78c34ad1a2a661c'
