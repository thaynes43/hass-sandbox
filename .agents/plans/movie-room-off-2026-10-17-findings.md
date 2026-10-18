# Findings: movie-room unexpected lights OFF, October 17, 2026

The 23:18:49 EDT lights OFF is directly attributable to automation.light_inovelli_movie_room_motion_cleared_lights_off, not the TV countdown automation. Its retained execution trace shows a 300-second delay, successful occupancy and hold checks, and light.turn_off targeting both movie-room light groups. Recorder and logbook independently show the recessed lights turning off immediately afterward.

Penelope's manual ON at 23:19:01 EDT is a timing anchor, not evidence of an automatic OFF/ON cycle. TV remained on through both the unexpected OFF and manual ON. A later TV OFF at 23:19:39 is a separate event whose initiating actor is not established here.

All local dates/times below are October 17, America/New_York, UTC−04:00. All listed UTC times are October 18. The full local-day query used October 17 04:00Z through October 18 04:00Z.

| Event | Local EDT | UTC |
|---|---|---|
| Inovelli occupancy becomes OFF | 23:06:37.568453 | 03:06:37.568453Z |
| Aqara FP2 becomes OFF; both occupancy automations start | 23:13:49.149431 | 03:13:49.149431Z |
| Lights automation issues light.turn_off | 23:18:49.150546 | 03:18:49.150546Z |
| Recessed light group records OFF | 23:18:49.717106 | 03:18:49.717106Z |
| Aqara FP2 returns ON; TV shutdown run cancels | 23:18:57.664803 | 03:18:57.664803Z |
| Inovelli occupancy returns ON | 23:18:58.287358 | 03:18:58.287358Z |
| Wall-switch MQTT up_held event | 23:19:01.611558 | 03:19:01.611558Z |
| Light group records ON (Penelope's manual restoration) | 23:19:01.676512 | 03:19:01.676512Z |
| TV records OFF, after restoration | 23:19:39.792014 | 03:19:39.792014Z |
| Both occupancy entities OFF again | 23:20:13.139199 | 03:20:13.139199Z |
| Second light-group OFF | 23:25:13.721351 | 03:25:13.721351Z |

## Evidence and context chain

1. Both-sensors trigger: binary_sensor.basement_movie_inovelli_presence_occupancy had been OFF since 23:06:37.568453. binary_sensor.basement_rumpus_room_aqara_fp2_01_presence_sensor changed ON → OFF at 23:13:49.149431. Recorder has no intervening ON or unavailable/unknown state before the lights shutdown. The sensor reports do not prove that the room was physically empty.
2. FP2 OFF context: 01M56G0MJXGNV8K7G2J36FP3BJ, parent_id null, user_id null. These raw state context fields are retained inside the TV trace's wait-trigger from_state.
3. Lights execution: run 5b4cc488bab8fbae5e882881973c95cd; context 01M56G0MJXDCWMR1HW24GK9V3F, parent 01M56G0MJXGNV8K7G2J36FP3BJ, user_id null. At 23:13:49 the hold check passes, delay resolves to 300 seconds, then at 23:18:49 both occupancy and hold checks pass again. The final action targets light.basement_movie_room_lights and light.basement_movie_room_ambient_lighting. The recessed group's logbook attributes OFF to that exact named automation and FP2 template trigger. All four Hue recessed lights and the Inovelli light report OFF within 23:18:49.447–.729.
4. TV execution: run d9ee2d708d3a30df305f93f7ef9b2d69; context 01M56G0MJYW9DXGT4CD7J7PKEG, same FP2 OFF parent, user_id null. It turns the pending boolean ON and enters a wait with pre_warning_seconds=900. On FP2 returning ON at 23:18:57.664803 (context 01M56GA1W068DE04P7K67RFNR8, null parent/user), the run cancels with approximately 591.485 seconds still remaining. No announcement or TV turn_off action is reached in this run.
5. Cancellation execution: run 49554d637499f3d7a9af254ade7676f5, context 01M56GA1W10HJ862DM3SM877PA, parent 01M56GA1W068DE04P7K67RFNR8, user_id null. It clears input_boolean.basement_movie_room_tv_auto_off_enabled at 23:18:57.681157.
6. Manual ON anchor: switch run 8cbc90874965b89605bae242f89a735d, MQTT command up_held, context 01M56GA5QBDGWB5JS5H79E7ZFQ, null parent/user. Its trace selects the hold branch but shows no light service action. Recessed group turns ON 65 ms later, and member lights follow. This is consistent with local switch/binding behavior and Tom/Penelope's explicit account. HA does not identify Penelope by user ID; a null user is not proof of absence of human input.
7. Later OFF: run 15f791ef4c24cf75218025e8447b1d50 starts 23:20:13 and issues light.turn_off five minutes later. Context 01M56GCBJKHB3QP4G77NCK8AYJ, parent 01M56GCBJKAP7QN991J0M1YBRB, user null. This later candidate does not match TV remaining ON: recorder says TV OFF already.

## Actual configuration versus expectation

Live configuration and historical settings agree on two independent paths:

- Lights: both sensors OFF → hold guard → 5-minute delay → both sensors OFF and hold guards → both light groups OFF. No broadcast, TV state guard, or dependency on the TV warning sequence.
- TV: both sensors OFF while TV active → pending boolean ON → 15 minutes silent → five one-minute announcements/waits → final announcement → TV OFF. The TV delay helper was 00:20:00. This path contains no light shutdown action.

Thus the installed behavior is not a single warned shutdown of TV and lights. The absence of a noticed broadcast is explained by an execution trace that never reached one; no failed broadcast need be assumed. If occupancy had remained OFF, the first TV warning would have been due about 23:28:49, well after the 23:18:49 lights shutdown.

Hold evidence: LED1 color 255, manual hold color 90, auto hold color 130 throughout the queried window; both checks passed. The Inovelli's mmWave wired-device control was Disabled, while HA's lights automation remained enabled. Those are separate controls.

## Entities, scenes and alternative paths

- Recessed group: light.basement_movie_room_lights, members four light.basement_movie_hue_recessed_01–04 entities plus light.basement_movie_inovelli_presence.
- Ambient group: light.basement_movie_room_ambient_lighting, members two floor lamps, two play bars, gradient_65 and gradient_75. This group was OFF throughout the queried local day; the OFF action targeted it but caused no new OFF transition.
- TV: media_player.lg_webos_tv_oled83g4wua, ON from 22:03:26.128556 until 23:19:39.792014.
- FP2: HomeKit Controller device Aqara FP2 Presence Sensor 01, model PS-S02E, firmware 1.3.3, registered area rumpus_room. The actual movie-room automations reference its rumpus-named entity explicitly. Registry naming/area does not establish physical placement or a wrong sensor selection.
- Inovelli occupancy and recessed bulbs: MQTT/Zigbee2MQTT integration.
- Live reference/config searches inspected the ZEN37 mapping, Inovelli mapping, recessed/ambient brightness helpers, voice lighting scripts, and whole-house brighten/restore/parameter scripts. No matching scene definition returned. Retained ZEN37 traces around the later period failed conditions. No scene or remote-control inference is needed to explain OFF because its service call, source context and resulting group transition are positively attributed.

## Availability, restarts and limitations

- HA recorder's current run began 2026-10-12 16:03:05Z. This argues against an HA restart during the incident. Both relevant automation entities remained ON with original last_changed dates on October 12; no unavailable/off transition is recorded during the incident window.
- Neither occupancy entity nor any recessed member became unavailable/unknown in 22:00–23:30 local history. This does not rule out an internally stale reading, missed detection, network loss that preserves state, or a physical coverage problem.
- The FP2 integration was loaded when inspected; its entry modified_at is 23:12:16.913 local, about 92 seconds before occupancy OFF. The API does not state what changed or who caused it. This timestamp alone proves neither a reload nor a fault and is not the established cause.
- TV availability: it was available and ON during the unexpected OFF. It became unavailable at 23:21:58.922282 after its later OFF. The structured WebOS error entry's latest occurrence matches that later unavailability; it does not explain the earlier lights shutdown.
- Targeted system-log search found no HomeKit entry. The MQTT search returned an unrelated gecko_iot_client connection error, not a Zigbee2MQTT broker failure.
- Targeted logbook searches for reload/homeassistant/homekit returned no entries in 22:27–23:27 local. Logbook is not a complete audit stream of reloads or service calls; absence does not prove no reload.
- Raw error-log searches inspect only a bounded newest 2,000-line window, with older lines still available. They returned no HomeKit match in that window. The error-log tool ignores end_time; no incident-wide absence claim is made from these results.
- Only five traces were retained for each inspected automation. Both incident traces and manual-ON anchor survived and were saved. History pages for the day groups/TV and incident sensors/settings were complete (has_more=false).
- MCP recorder history responses omit raw state context IDs even with minimal_response=false. Group logbook provides source attribution; trace variables provide raw parent/user contexts. Leaf-state and manual-ON raw contexts cannot be independently reconstructed through the available history response. No direct recorder database or credential access was used.
- Config-body searches reported partial results: two YAML-defined scripts returned 404, so dynamic/templated references are not exhaustively excluded. Positive trace/logbook attribution establishes this OFF despite that search limit.
- Period-start history entries are synthetic snapshots at the query boundary, not proof of a transition at midnight/22:00.
- Both-sensors OFF is established as the trigger. Why the two sensors reported no occupancy while people were watching remains unestablished. Sensor fault, physical coverage/placement, detection tuning and stale integration state remain possible explanations, not findings.

## Smallest proposed correction — not applied

For the reported expectation, prevent the independent five-minute lights-OFF path from bypassing the TV warning/shutdown sequence while the TV is active, and put both light groups' shutdown on the shared, occupancy-cancellable warned shutdown path. Preserve lights-only empty-room cleanup when the TV is off if desired. Align that shared timer with Tom's intended 15-minute behavior; currently the TV uses 20 minutes total (15 silent plus five spoken). Merely changing the lights delay to 15 minutes would still leave an independent, unannounced lights shutdown and would not implement the expected shared behavior.

This addresses the proven control-path mismatch; do not replace or retune sensors on the present evidence alone.

## Ending state and handoff

Read-only investigation complete. Only local investigation records/evidence were created. No HA actions, configuration changes, broadcasts, test events, restarts, deployments, additional agents or Claude sessions. The separate Opus hass-sandbox-1017-233330 session was untouched. Any implementation requires a new authorized bounded story. Evidence JSON files alongside this report retain the exact trace IDs, contexts, settings and response limitations.
