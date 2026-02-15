from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CapturedFrame:
    idx: int
    filename: str
    image_ha_path: str
    captured_ts: float


@dataclass
class CaptureConfig:
    snapshot_interval_s: float
    off_grace_s: float
    capture_max_s: float
    # When motion is OFF but within off_grace_s, how often we re-check motion.
    off_poll_s: float = 1.0


@dataclass
class CaptureState:
    run_id: str
    started_ts: float
    frames: list[CapturedFrame]
    capture_idx: int = 0
    motion_off_since: Optional[float] = None
    timed_out: bool = False
    ended_ts: Optional[float] = None


def should_stop_capture(
    *,
    now: float,
    cfg: CaptureConfig,
    state: CaptureState,
    motion_is_on: bool,
) -> bool:
    if cfg.capture_max_s > 0 and (now - state.started_ts) >= cfg.capture_max_s:
        state.timed_out = True
        state.ended_ts = now
        return True

    if motion_is_on:
        state.motion_off_since = None
        return False

    if state.motion_off_since is None:
        state.motion_off_since = now
        return False

    if cfg.off_grace_s <= 0:
        state.ended_ts = now
        return True

    if (now - float(state.motion_off_since)) >= cfg.off_grace_s:
        state.ended_ts = now
        return True

    return False


def next_delay_s(*, cfg: CaptureConfig, state: CaptureState, motion_is_on: bool) -> float:
    # Capture while motion is ON, otherwise poll until grace expires.
    if motion_is_on:
        return max(0.0, float(cfg.snapshot_interval_s))
    return max(0.1, float(cfg.off_poll_s))

