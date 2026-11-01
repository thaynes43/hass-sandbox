"""Calendar and takeover acceptance checks against the deployed YAML mirrors."""
from datetime import datetime
import copy
import importlib.util
from pathlib import Path

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment


ROOT = Path(__file__).resolve().parents[2]
CONFIGS = {
    name: yaml.safe_load(
        (ROOT / 'home-assistant/automations/seasonal-lighting' / f'{name}.yaml').read_text()
    )
    for name in ('fall', 'christmas', 'visitor')
}


def render(expression, date, **values):
    env = NativeEnvironment()
    env.globals['now'] = lambda: datetime.fromisoformat(date)
    return env.from_string(expression).render(**values)


@pytest.mark.parametrize('date,fall,christmas,week', [
    ('2026-10-31T23:59:59-04:00', False, False, None),
    ('2026-11-01T17:00:00-05:00', True, False, 0),
    ('2026-11-07T23:59:59-05:00', True, False, 0),
    ('2026-11-08T00:00:00-05:00', True, False, 1),
    ('2026-11-14T23:59:59-05:00', True, False, 1),
    ('2026-11-15T00:00:00-05:00', True, False, 2),
    ('2026-11-21T23:59:59-05:00', True, False, 2),
    ('2026-11-22T00:00:00-05:00', True, False, 3),
    ('2026-11-26T23:59:59-05:00', True, False, 3),
    ('2026-11-27T00:00:00-05:00', False, True, None),
    ('2026-12-01T17:00:00-05:00', False, True, None),
    ('2027-01-01T17:00:00-05:00', False, True, None),
])
def test_calendar_boundary(date, fall, christmas, week):
    for name, expected in [('fall', fall), ('christmas', christmas)]:
        assert render(CONFIGS[name]['conditions'][0]['value_template'], date) is expected
    if fall:
        variables = CONFIGS['fall']['variables']
        index = render(variables['palette_index'], date)
        assert index == week
        palette = render(variables['palette'], date, **(variables | {'palette_index': index}))
        assert len(palette) == 4
        assert all(0 <= hue <= 360 and 0 <= sat <= 100 for hue, sat in palette)


@pytest.mark.parametrize('name', ['fall', 'christmas'])
def test_only_the_core_lights_and_lamp_post_are_selected(name):
    lights = CONFIGS[name]['variables']['seasonal_lights']
    expected = {
        f'light.front_yard_{side}_hue_{kind}_{n:02d}'
        for side in ('left', 'right')
        for kind, count in [('calla', 4), ('lily', 3)]
        for n in range(1, count + 1)
    } | {f'light.outdoor_front_porch_hue_recessed_{n:02d}' for n in range(1, 4)} | {
        'light.front_yard_lamp_post_hue_edison'
    }
    assert {light['entity_id'] for light in lights} == expected
    assert len(lights) == len(expected) == 18


@pytest.mark.parametrize('name', ['fall', 'christmas'])
def test_lamp_post_keeps_warm_colors_in_every_palette_and_shuts_off_at_midnight(name):
    config = CONFIGS[name]
    lamp = next(light for light in config['variables']['seasonal_lights']
                if light['entity_id'] == 'light.front_yard_lamp_post_hue_edison')
    palettes = config['variables'].get('palettes', [config['variables'].get('palette')])
    for palette in palettes:
        hue, saturation = palette[lamp['color']]
        assert 20 <= hue <= 60  # Copper through warm gold, never green.
    midnight = yaml.safe_load(
        (ROOT / 'home-assistant/automations/seasonal-lighting/midnight.yaml').read_text()
    )
    assert lamp['entity_id'] in midnight['actions'][0]['target']['entity_id']


@pytest.mark.parametrize('name', ['fall', 'christmas', 'visitor'])
def test_both_takeover_automations_block_new_runs(name):
    # A future setup can take over by enabling either existing sunset controller.
    guards = {c['entity_id']: c['state'] for c in CONFIGS[name]['conditions'] if c['condition'] == 'state'}
    assert guards == {
        'automation.light_front_yard_sun_sets_during_christmas_get_jolly': 'off',
        'automation.light_front_yard_at_sunset_turn_on': 'off',
    }


@pytest.mark.parametrize('name', ['fall', 'christmas'])
def test_recovery_cannot_relight_after_midnight(name):
    conditions = CONFIGS[name]['conditions']
    assert {'condition': 'sun.is_set'} in conditions
    assert {'condition': 'time', 'after': '12:00:00'} in conditions


# Reuse the repo's small configuration executor rather than a second scheduler.
# These fake-time scenarios never connect to HA or sleep in real time.
spec = importlib.util.spec_from_file_location(
    'room_executor', ROOT / 'scripts/automation-tests/test_movie_room_shutdown.py'
)
executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(executor)
PERSON = 'binary_sensor.g4_doorbell_pro_poe_person_detected'
BELL = 'binary_sensor.g4_doorbell_pro_poe_doorbell'
JOLLY = 'automation.light_front_yard_sun_sets_during_christmas_get_jolly'


class Visitors(executor.Room):
    def __init__(self):
        super().__init__()
        self.configs = {'visitor': copy.deepcopy(CONFIGS['visitor'])}
        # The shared executor expects a timeout; infinite is HA's omitted timeout.
        for step in self.configs['visitor']['actions'][0]['default']:
            if 'wait_template' in step:
                step.setdefault('timeout', float('inf'))
        self.states.update({PERSON: 'off', BELL: 'off', JOLLY: 'off',
                            'automation.light_front_yard_at_sunset_turn_on': 'off',
                            'automation.light_front_yard_sun_sets_colors_of_fall': 'on',
                            'automation.light_front_yard_sun_sets_christmas_colors': 'on'})
        self.env.globals['now'] = lambda: datetime(2026, 11, 1, 20)

    def condition(self, condition, variables):
        if isinstance(condition, dict) and condition['condition'] == 'sun.is_set':
            return True  # All visitor scenarios take place after sunset.
        return super().condition(condition, variables)

    def update(self, changes, user=None):
        before = self.states.copy()
        super().update(changes, user=user)
        # Map the validated purpose-specific occupancy trigger to a sensor edge.
        for trigger in self.configs['visitor']['triggers']:
            if trigger['trigger'] == 'occupancy.detected':
                entity = trigger['target']['entity_id']
                if self.states.get(entity) == 'on' and before.get(entity) == 'off':
                    self.queue.append(('visitor', trigger['id'], None))


def test_doorbell_near_end_of_prior_clear_period_gets_full_three_minutes():
    r = Visitors()
    r.set(**{PERSON: 'on'})
    r.advance(60)
    r.set(**{PERSON: 'off'})
    r.advance(179)
    r.set(**{BELL: 'on'})
    r.advance(1)
    assert not r.device_offs()  # Old independent clear timer fired here.
    r.advance(178)
    assert not r.device_offs()
    r.advance(1)
    assert [call[0] for call in r.device_offs()] == [419]


def test_another_person_restarts_the_clear_period():
    r = Visitors()
    r.set(**{PERSON: 'on'})
    r.advance(20)
    r.set(**{PERSON: 'off'})
    r.advance(179)
    r.set(**{PERSON: 'on'})
    r.advance(60)
    r.set(**{PERSON: 'off'})
    r.advance(179)
    assert not r.device_offs()
    r.advance(1)
    assert [call[0] for call in r.device_offs()] == [439]


def test_person_presence_is_not_mistaken_for_a_clear_period():
    r = Visitors()
    r.set(**{PERSON: 'on'})
    r.advance(3600)
    assert not r.device_offs()
    r.set(**{PERSON: 'off'})
    r.advance(180)
    assert [call[0] for call in r.device_offs()] == [3780]


def test_get_jolly_takeover_prevents_delayed_lantern_off():
    r = Visitors()
    r.set(**{BELL: 'on'})
    r.advance(60)
    r.set(**{JOLLY: 'on'})
    r.advance(120)
    assert not r.device_offs()


def test_midnight_cancels_a_pending_visitor_wait():
    r = Visitors()
    r.set(**{PERSON: 'on'})
    r.advance(60)
    r.queue.append(('visitor', 'clear', None))  # Configured midnight time trigger.
    r.drain()
    assert [call[0] for call in r.device_offs()] == [60]
    assert not r.running
