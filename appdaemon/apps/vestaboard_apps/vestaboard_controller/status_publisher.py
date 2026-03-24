"""Status publisher — builds the sensor attributes dict for the controller."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional


class StatusPublisher:
    """Builds the ``sensor.vestaboard_controller_status`` attributes.

    Pure Python — no AppDaemon dependency.
    """

    @staticmethod
    def build_attributes(
        queue_state: Any,
        registry: Any,
        board_io: Any,
        ai_art_preview: Optional[dict],
        external_board_frame: Optional[list[list[int]]],
        log_fn: Callable[[str], None],
    ) -> tuple[str, dict[str, Any]]:
        """Return ``(sensor_state, attributes)`` for ``set_state``.

        Args:
            queue_state: ``FrameQueueState`` from ``queue.get_state(now)``.
            registry: ``AutomationRegistry`` instance.
            board_io: ``BoardIO`` instance.
            ai_art_preview: Current AI art preview dict, or None.
            external_board_frame: Cached board grid read from the physical device.
            log_fn: Logging callable.

        Returns:
            Tuple of (state_string, attributes_dict).
        """
        displayed = queue_state.displayed
        displayed_frame_info: Optional[dict] = None

        if displayed is not None:
            displayed_frame_info = {
                "frame_id": displayed.frame_id,
                "source": displayed.source,
                "source_label": displayed.source_label,
                "characters": json.dumps(displayed.characters),
            }
        elif external_board_frame is not None:
            displayed_frame_info = {
                "frame_id": "external",
                "source": "external",
                "source_label": "External / Other App",
                "characters": json.dumps(external_board_frame),
            }

        all_automations: list[dict[str, Any]] = []
        for automation_id, auto in registry.all_automations():
            entry: dict[str, Any] = {
                "id": automation_id,
                "name": getattr(auto, "display_name", automation_id.replace("_", " ").title()),
                "description": getattr(auto, "display_description", "A Vestaboard automation."),
                "enabled": bool(
                    registry.get_stored_config(automation_id).get("enabled", True)
                ) if registry.get_stored_config(automation_id) else True,
            }

            # Include next scheduled fire time
            next_fire = getattr(auto, "_next_fire_time", None)
            if next_fire is not None:
                entry["next_fire_time"] = next_fire

            # Include preview frame
            if hasattr(auto, "get_preview_frame"):
                try:
                    entry["preview_frame"] = json.dumps(auto.get_preview_frame())
                except Exception as exc:
                    log_fn(
                        f"get_preview_frame failed for {automation_id!r}: {exc!r}"
                    )

            # Include config schema and current config
            if hasattr(auto, "get_config_schema"):
                try:
                    entry["config_schema"] = auto.get_config_schema()
                except Exception as exc:
                    log_fn(
                        f"get_config_schema failed for {automation_id!r}: {exc!r}"
                    )

            if hasattr(auto, "get_effective_config"):
                try:
                    entry["config"] = {
                        k: v for k, v in auto.get_effective_config().items()
                        if not k.startswith("_") and k != "type"
                    }
                except Exception:
                    entry["config"] = {}
            else:
                entry["config"] = {}

            all_automations.append(entry)

        # Build combined queue sequence (fallback first, then pending)
        # with estimated display times based on cumulative TTLs.
        queue_sequence: list[dict[str, Any]] = []
        cumulative_s = queue_state.displayed_ttl_remaining_s or 0.0
        seq_num = 1

        # Fallback frames first (FIFO — index 0 is next)
        for f in queue_state.fallback_stack:
            est_display_at = cumulative_s
            frame_ttl = (
                f.remaining_ttl_s
                if f.remaining_ttl_s is not None
                else (f.ttl_s or 0)
            )
            queue_sequence.append({
                "seq": seq_num,
                "frame_id": f.frame_id,
                "source": f.source,
                "source_label": f.source_label,
                "zone": "fallback",
                "est_display_in_s": round(est_display_at, 1),
                "ttl_s": frame_ttl,
            })
            cumulative_s += frame_ttl
            seq_num += 1

        # Pending frames next (FIFO — index 0 is next)
        for f in queue_state.pending:
            est_display_at = cumulative_s
            frame_ttl = f.ttl_s or 0
            queue_sequence.append({
                "seq": seq_num,
                "frame_id": f.frame_id,
                "source": f.source,
                "source_label": f.source_label,
                "zone": "pending",
                "est_display_in_s": round(est_display_at, 1),
                "ttl_s": frame_ttl,
            })
            cumulative_s += frame_ttl
            seq_num += 1

        attributes: dict[str, Any] = {
            "displayed_frame": displayed_frame_info,
            "displayed_source": (
                displayed.source if displayed
                else ("external" if displayed_frame_info else None)
            ),
            "displayed_source_label": (
                displayed.source_label if displayed
                else ("External / Other App" if displayed_frame_info else None)
            ),
            "displayed_ttl_remaining_s": queue_state.displayed_ttl_remaining_s,
            "pending_count": len(queue_state.pending),
            "fallback_count": len(queue_state.fallback_stack),
            "all_automations": all_automations,
            "last_write_ok": board_io.last_write_ok,
            "sleeping": board_io.is_sleeping(),
            "sleep_end": board_io.sleep_end,
            "ai_art_preview": ai_art_preview,
            "queue_state": {
                "pending": [
                    {
                        "frame_id": f.frame_id,
                        "source": f.source,
                        "source_label": f.source_label,
                        "ttl_s": f.ttl_s,
                        "max_age_s": f.max_age_s,
                        "expires_at": (
                            datetime.fromtimestamp(
                                f.created_at + f.max_age_s, tz=timezone.utc
                            ).isoformat()
                            if f.max_age_s is not None
                            else None
                        ),
                    }
                    for f in queue_state.pending
                ],
                "fallback": [
                    {
                        "frame_id": f.frame_id,
                        "source": f.source,
                        "source_label": f.source_label,
                        "remaining_ttl_s": f.remaining_ttl_s,
                    }
                    for f in queue_state.fallback_stack
                ],
            },
            "queue_sequence": queue_sequence,
        }

        sensor_state = "active" if displayed is not None else "idle"
        return sensor_state, attributes
