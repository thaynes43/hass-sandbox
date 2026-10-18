# Movie-room mmWave occupied/clear validation

Status: Planned — owner-operated runtime acceptance pending; no tests tonight.

Saga: Home Assistant reliable room control. Epic: safe occupied movie-room lighting and warned empty-room shutdown.
Related story: `.agents/plans/movie-room-coordinated-shutdown-fix.md`.

Tom plans to trigger each mmWave sensor tomorrow to verify occupied/clear transitions.
This record preserves that follow-up; it does not authorize agents to stimulate sensors,
run broadcasts, operate TV/lights, or perform shutdown tests tonight.

Entities:
- `binary_sensor.basement_movie_inovelli_presence_occupancy`
- `binary_sensor.basement_rumpus_room_aqara_fp2_01_presence_sensor`

The confirmed incident was a five-minute lights-only automation acting on both sensors
reporting OFF. The coordinated warned shutdown is implemented and already deployed;
why the sensors reported empty while the room was occupied remains unestablished.
Do not infer physical placement from the Aqara entity's rumpus-room name/registry area.

Acceptance for Tom's next validation session: record each sensor's occupied and clear
transitions with local/UTC timestamps and correlate with physical occupancy; assess
whether either sensor misses the seating area. Preserve evidence before proposing any
sensor replacement, placement change or tuning. Full audible countdown/device shutdown
validation remains separate and requires explicit authorization for a suitable room state.

Owner: Tom for physical stimulation; a future agent may inspect resulting history only
when asked. No new agent/session is launched by this story. No worktree/runtime writes
or new spend are authorized by this record. Budget and test window to be agreed then.

Evidence/rollback: `.agents/plans/movie-room-off-2026-10-17-evidence/` and
`.agents/plans/movie-room-coordinated-shutdown-runtime-readback.json`.
