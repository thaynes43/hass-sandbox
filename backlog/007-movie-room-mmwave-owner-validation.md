# Movie-room mmWave occupied/clear validation

Status: Partly done — adult seated test passed 2026-10-18; child lying-down test pending.

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

## 2026-10-18 adult seated test (Tom)

Tom sat in his daughter's spot from 12:32 EDT (16:32 UTC) and sat still.
- Aqara FP2 (`..._aqara_fp2_01_presence_sensor`): on at 12:32:38 EDT and held without
  a drop through at least 12:45:52 EDT (13+ min). Pass for a seated adult.
- Inovelli (`basement_movie_inovelli_presence_occupancy`): flapped on/off about every
  40 s while he settled, then reported off from 12:38:21 EDT onward. It does not hold a
  still, seated person; the FP2 alone keeps the room occupied.
- Tom (with a photo of the TV wall): the seat is in the corner, outside the Inovelli
  switch's view, so only the FP2 covers it. The Inovelli reading off there is expected.

Incident correlation (2026-10-17, EDT): the FP2 held her from 22:03:18 to 23:13:49
(70 min) before dropping, while the Inovelli had already cleared at 23:06:37. She was
seen again at 23:18:57, 8 s after the lights went off. Working hypothesis, unconfirmed:
she changed posture (lying down, possibly under a blanket) shortly before 23:13.

Next step (Tom, 2026-10-18): repeat the test with his daughter lying down where she
was, for 5+ minutes, then read back the FP2 history for that window. If the FP2 drops
her, the fix is FP2 sensitivity or zone settings in the Aqara Home app. HA receives
only on/off over HomeKit, so it cannot tune the FP2.
