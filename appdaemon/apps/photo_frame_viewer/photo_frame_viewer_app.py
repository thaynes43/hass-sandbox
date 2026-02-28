from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import Any, Optional

import hassapi as hass
from urllib.parse import quote


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _basename(path: str) -> str:
    try:
        return str(PurePosixPath(str(path))).split("/")[-1] or ""
    except Exception:
        return ""


class PhotoFrameViewerApp(hass.Hass):
    """
    Photo frame viewer for dashboards.

    - Reads available image paths from `source_sensor_entity_id` attribute `file_list` (default: `sensor.immich_album`).
    - Keeps a dropdown (`input_select`) synced to those images (friendly basenames).
    - Copies the selected image into `/config/www/...` via an HA `shell_command`.
    - Auto-advances when not paused (interval controlled by an `input_number` helper).
    """

    DEFAULTS = {
        "source_sensor_entity_id": "sensor.immich_album",
        "source_file_list_attr": "file_list",
        "picker_entity_id": "input_select.wall_display_photo_frame_image",
        "paused_entity_id": "input_boolean.wall_display_photo_frame_paused",
        "interval_entity_id": "input_number.wall_display_photo_frame_interval_seconds",
        "cache_bust_entity_id": "input_text.wall_display_photo_frame_cache_bust",
        # Stores the selected image to display (as a `/local/...` URL).
        "image_local_url_entity_id": "input_text.wall_display_photo_frame_image_local_url",
        "fallback_image_path": "/config/www/immich-album/no-image.jpg",
        "options_max": 100,
        "refresh_options_every_s": 60,
        "auto_cycle": True,
        # If true, any manual navigation resets the slideshow timer.
        "reset_timer_on_manual_nav": True,
    }

    def initialize(self) -> None:
        cfg = {**self.DEFAULTS, **(self.args or {})}

        self.source_sensor_entity_id: str = str(cfg["source_sensor_entity_id"])
        self.source_file_list_attr: str = str(cfg["source_file_list_attr"])
        self.picker_entity_id: str = str(cfg["picker_entity_id"])
        self.paused_entity_id: str = str(cfg["paused_entity_id"])
        self.interval_entity_id: str = str(cfg["interval_entity_id"])
        self.cache_bust_entity_id: str = str(cfg["cache_bust_entity_id"])
        self.image_local_url_entity_id: str = str(cfg["image_local_url_entity_id"])
        self.fallback_image_path: str = str(cfg["fallback_image_path"])
        self.options_max: int = max(1, int(cfg["options_max"]))
        self.refresh_options_every_s: float = max(10.0, float(cfg["refresh_options_every_s"]))
        self.auto_cycle: bool = _as_bool(cfg["auto_cycle"], True)
        self.reset_timer_on_manual_nav: bool = _as_bool(cfg["reset_timer_on_manual_nav"], True)

        self._timer_handle: Optional[Any] = None
        self._periodic_handle: Optional[Any] = None
        self._label_to_path: dict[str, str] = {}
        self._path_to_label: dict[str, str] = {}
        self._last_published_local_url: Optional[str] = None

        self.log(
            "PhotoFrameViewerApp init "
            f"source_sensor={self.source_sensor_entity_id} picker={self.picker_entity_id} "
            f"paused={self.paused_entity_id} interval={self.interval_entity_id} "
            f"image_local_url_entity_id={self.image_local_url_entity_id}",
            level="INFO",
        )

        # Watch the folder-index sensor for new file lists.
        self.listen_state(self._on_source_sensor_change, self.source_sensor_entity_id)

        # Watch selection (manual nav or our own auto-advance).
        self.listen_state(self._on_picker_change, self.picker_entity_id)

        # Watch pause + interval changes for timer control.
        self.listen_state(self._on_pause_change, self.paused_entity_id)
        self.listen_state(self._on_interval_change, self.interval_entity_id)

        # Periodic refresh (helps if sensor updates are missed or attributes don't trigger reliably).
        self._periodic_handle = self.run_every(
            self._periodic_refresh,
            self.datetime(),
            self.refresh_options_every_s,
        )

        # Initial sync + timer.
        self._refresh_picker_options(reason="init")
        # Ensure the dashboard has an initial image URL immediately.
        self._publish_selected_local_url(self._picker_value(), reason="init")
        self._sync_timer(reason="init")

    # --- state helpers ---

    def _is_paused(self) -> bool:
        return str(self.get_state(self.paused_entity_id) or "").strip().lower() == "on"

    def _interval_s(self) -> float:
        raw = self.get_state(self.interval_entity_id)
        seconds = _safe_float(raw, default=10.0)
        if seconds <= 0:
            seconds = 10.0
        return max(1.0, seconds)

    def _picker_state_all(self) -> dict[str, Any]:
        st = self.get_state(self.picker_entity_id, attribute="all")
        return st if isinstance(st, dict) else {}

    def _picker_options(self) -> list[str]:
        st = self._picker_state_all()
        attrs = st.get("attributes") if isinstance(st.get("attributes"), dict) else {}
        opts = attrs.get("options")
        if isinstance(opts, list):
            return [str(o) for o in opts]
        return []

    def _picker_value(self) -> str:
        return str(self.get_state(self.picker_entity_id) or "").strip()

    # --- source reading + picker sync ---

    def _read_file_list(self) -> list[str]:
        st = self.get_state(self.source_sensor_entity_id, attribute="all")
        if not isinstance(st, dict):
            return []
        attrs = st.get("attributes")
        if not isinstance(attrs, dict):
            return []
        files = attrs.get(self.source_file_list_attr)
        if not isinstance(files, list):
            return []
        out: list[str] = []
        for f in files:
            s = str(f or "").strip()
            if s:
                out.append(s)
        return out[: self.options_max]

    def _build_label_maps(self, file_paths: list[str]) -> tuple[list[str], dict[str, str], dict[str, str]]:
        counts: dict[str, int] = {}
        label_to_path: dict[str, str] = {}
        path_to_label: dict[str, str] = {}
        labels: list[str] = []

        for p in file_paths:
            base = _basename(p).strip() or "image"
            n = counts.get(base, 0) + 1
            counts[base] = n
            label = base if n == 1 else f"{base} ({n})"
            labels.append(label)
            label_to_path[label] = p
            path_to_label[p] = label

        return labels, label_to_path, path_to_label

    def _refresh_picker_options(self, *, reason: str) -> None:
        files = self._read_file_list()
        if not files:
            files = [self.fallback_image_path]

        labels, label_to_path, path_to_label = self._build_label_maps(files)

        # Attempt to preserve current selection by full path when possible.
        current_label = self._picker_value()
        current_path = self._label_to_path.get(current_label)
        if current_path and current_path in path_to_label:
            desired_label = path_to_label[current_path]
        elif current_label in labels:
            desired_label = current_label
        else:
            desired_label = labels[0] if labels else ""

        self._label_to_path = label_to_path
        self._path_to_label = path_to_label

        existing_opts = self._picker_options()
        if existing_opts != labels:
            self.log(
                f"PhotoFrameViewerApp: updating picker options count={len(labels)} reason={reason}",
                level="INFO",
            )
            self.call_service("input_select/set_options", entity_id=self.picker_entity_id, options=labels)

        if desired_label and desired_label != current_label:
            self.call_service(
                "input_select/select_option",
                entity_id=self.picker_entity_id,
                option=desired_label,
            )

    # --- selection -> publish local URL (no copying) ---

    def _to_local_url(self, ha_path: str) -> str:
        """
        Convert an HA-accessible file path under `/config/www/` to `/local/...`.

        Example:
        - `/config/www/immich-album/foo bar.jpg` -> `/local/immich-album/foo%20bar.jpg`
        """
        p = str(PurePosixPath(str(ha_path or "").strip()))
        if not p:
            return "/local/immich-album/no-image.jpg"
        if p.startswith("/local/"):
            return quote(p, safe="/")
        if p.startswith("/config/www/"):
            rel = p[len("/config/www/") :]
            return "/local/" + quote(rel, safe="/")
        # Best-effort fallback: treat as already a relative path.
        if p.startswith("/"):
            return quote(p, safe="/")
        return "/local/" + quote(p, safe="/")

    def _publish_selected_local_url(self, label: str, *, reason: str) -> None:
        label = str(label or "").strip()
        path = (self._label_to_path.get(label) or "").strip()
        if not path:
            path = self.fallback_image_path
            label = self._path_to_label.get(path, label) or label

        local_url = self._to_local_url(path)
        if self._last_published_local_url == local_url:
            return

        self.log(
            f"PhotoFrameViewerApp: publish local url label={label!r} path={path!r} url={local_url!r} reason={reason}",
            level="INFO",
        )
        self.call_service(
            "input_text/set_value",
            entity_id=self.image_local_url_entity_id,
            value=local_url,
        )
        self._last_published_local_url = local_url
        self._touch_cache_bust()

    def _touch_cache_bust(self) -> None:
        value = str(int(time.time()))
        self.call_service("input_text/set_value", entity_id=self.cache_bust_entity_id, value=value)

    # --- timer control ---

    def _cancel_timer(self) -> None:
        handle = self._timer_handle
        if handle is None:
            return
        # Clear first so we don't try to cancel the same handle twice.
        self._timer_handle = None
        try:
            # Only cancel handles that are still active. If the callback already fired,
            # AppDaemon will warn about an invalid handle.
            if self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass

    def _schedule_next(self, *, reason: str) -> None:
        if self._is_paused():
            self._cancel_timer()
            return
        interval = self._interval_s()
        self._cancel_timer()
        self._timer_handle = self.run_in(self._on_tick, interval)
        self.log(f"PhotoFrameViewerApp: scheduled next tick in {interval:.1f}s reason={reason}", level="DEBUG")

    def _sync_timer(self, *, reason: str) -> None:
        if self._is_paused():
            self._cancel_timer()
            self.log(f"PhotoFrameViewerApp: paused; timer cancelled reason={reason}", level="INFO")
            return
        self._schedule_next(reason=reason)

    # --- callbacks ---

    def _periodic_refresh(self, kwargs) -> None:
        self._refresh_picker_options(reason="periodic")

    def _on_source_sensor_change(self, entity, attribute, old, new, kwargs) -> None:
        # Refresh options when the folder-index sensor changes.
        self._refresh_picker_options(reason="sensor_change")

    def _on_picker_change(self, entity, attribute, old, new, kwargs) -> None:
        # Any selection change should copy the new file and optionally reset the timer.
        new_label = str(new or "").strip()
        if not new_label:
            return
        self._publish_selected_local_url(new_label, reason="picker_change")
        if self.reset_timer_on_manual_nav:
            self._schedule_next(reason="picker_change")

    def _on_pause_change(self, entity, attribute, old, new, kwargs) -> None:
        self._sync_timer(reason="pause_change")

    def _on_interval_change(self, entity, attribute, old, new, kwargs) -> None:
        self._sync_timer(reason="interval_change")

    def _on_tick(self, kwargs) -> None:
        # This callback corresponds to the current scheduled handle; mark it consumed
        # so subsequent reschedules don't try to cancel an already-fired timer.
        self._timer_handle = None
        if self._is_paused():
            self._cancel_timer()
            return

        opts = self._picker_options()
        if len(opts) <= 1:
            # Nothing to advance; keep ticking so we recover automatically when options appear.
            self._schedule_next(reason="tick_no_options")
            return

        before = self._picker_value()
        self.call_service(
            "input_select/select_next",
            entity_id=self.picker_entity_id,
            cycle=self.auto_cycle,
        )
        after = self._picker_value()
        if after == before:
            # If HA didn't advance for some reason, don't stall the slideshow.
            self._schedule_next(reason="tick_no_change")

