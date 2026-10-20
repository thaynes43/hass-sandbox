"""Validate/execute JSON configs in an ISOLATED HomeAssistant object, not live HA.

Requires the installed HA Python environment. Reads [tv_config, lights_config]
from stdin. No network, config files, real integrations, or device service calls.
All waits are shortened in an execution-only copy; original timing is tested by
test_movie_room_shutdown.py. Used once through in-pod kubectl exec for this fix.
"""
import asyncio
import copy
import json
import logging
import sys

from homeassistant import loader
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import trigger
from homeassistant.helpers.script import Script

TV = 'media_player.lg_webos_tv_oled83g4wua'
PENDING = 'input_boolean.basement_movie_room_tv_auto_off_enabled'
SENSORS = ['binary_sensor.basement_movie_inovelli_presence_occupancy',
           'binary_sensor.basement_rumpus_room_aqara_fp2_01_presence_sensor']
LED = 'number.basement_movie_inovelli_presence_defaultled1colorwhenon'
LIGHTS = ['light.basement_movie_room_lights', 'light.basement_movie_room_ambient_lighting']
LIGHTS_AUTOMATION = 'automation.light_inovelli_movie_room_motion_cleared_lights_off'


def shorten_waits(node):
    if isinstance(node, dict):
        if 'timeout' in node:
            node['timeout'] = {'seconds': 0.001}
        if 'delay' in node:
            node['delay'] = {'seconds': 0.001}
        for value in node.values():
            shorten_waits(value)
    elif isinstance(node, list):
        for value in node:
            shorten_waits(value)


async def probe(hass, configs, fail_warning=False, hold=None, occupied=False, tv='on',
                trigger_id='room_empty', lights_automation='on'):
    calls = []
    for entity, state in {TV: tv, PENDING: 'off', SENSORS[0]: 'off', SENSORS[1]: 'off',
                          LED: hold or '255', 'input_text.inovelli_manual_hold': '90',
                          'input_text.inovelli_auto_hold': '130', LIGHTS_AUTOMATION: lights_automation,
                          LIGHTS[0]: 'on', LIGHTS[1]: 'off',
                          'input_select.basement_movie_room_tv_auto_off_delay': '00:20:00'}.items():
        hass.states.async_set(entity, state)

    async def fake_service(call):
        calls.append((call.domain, call.service, call.data))
        if call.domain == 'input_boolean':
            hass.states.async_set(PENDING, 'on' if call.service == 'turn_on' else 'off')
        elif call.domain == 'assist_satellite':
            if fail_warning:
                raise HomeAssistantError('Intentional isolated fake announcement failure')
            if occupied:
                hass.states.async_set(SENSORS[0], 'on')
        elif call.domain == 'media_player':
            hass.states.async_set(TV, 'off')
        elif call.domain == 'light':
            for light in LIGHTS:
                hass.states.async_set(light, 'off')

    for domain, service in [('input_boolean', 'turn_on'), ('input_boolean', 'turn_off'),
                            ('assist_satellite', 'announce'), ('light', 'turn_off'),
                            ('media_player', 'turn_off')]:
        hass.services.async_register(domain, service, fake_service)
    config = copy.deepcopy(configs[0])
    shorten_waits(config)
    validated = await async_validate_config_item(hass, config['id'], config)
    script = Script(hass, validated['actions'], 'isolated movie room probe', 'automation')
    await script.async_run({'trigger': {'id': trigger_id}})
    warnings = [c for c in calls if c[0] == 'assist_satellite']
    offs = [c[0] for c in calls if c[0] in ['light', 'media_player']]
    assert hass.states.get(PENDING).state == 'off', calls
    if tv != 'on' and lights_automation == 'off':
        # The lights automation turned off is a hold: no lights-only countdown at all.
        assert not warnings and not offs, calls
    elif tv != 'on':
        # Lights-only countdown: silent, lights off, never a TV command.
        assert not warnings and offs == ['light'], calls
    elif fail_warning or occupied:
        assert len(warnings) == 1 and not offs, calls
    else:
        assert len(warnings) == 4, calls
        assert offs == (['media_player'] if hold else ['light', 'media_player']), calls
    print(f'Isolated HA Script OK: fail_warning={fail_warning}, hold={hold}, occupied={occupied}, '
          f'tv={tv}, trigger={trigger_id}, lights_automation={lights_automation}')


async def main():
    # This is a new in-process HA object, with no connection to the running instance.
    hass = HomeAssistant('/tmp/offline-movie-room-schema-only')
    loader.async_setup(hass)
    await trigger.async_setup(hass)
    configs = json.load(sys.stdin)
    for config in configs:
        result = await async_validate_config_item(hass, config['id'], config)
        assert str(result.validation_status) == 'ok', result.validation_status
        print(config['id'], 'installed HA schema: ok')
    await probe(hass, configs)
    await probe(hass, configs, fail_warning=True)
    await probe(hass, configs, hold='90')
    await probe(hass, configs, hold='130')
    await probe(hass, configs, occupied=True)
    await probe(hass, configs, tv='unavailable')
    await probe(hass, configs, tv='off', trigger_id='recheck')
    await probe(hass, configs, tv='unavailable', trigger_id='recheck', lights_automation='off')


if __name__ == '__main__':
    logging.disable(logging.WARNING)
    asyncio.run(main())
