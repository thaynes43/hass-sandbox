from __future__ import annotations

import os
import time
from typing import Any, Optional

import hassapi as hass

from photo_frame_viewer.gen_helpers import (
    basename,
    build_label_maps,
    compute_fingerprint,
    gen_path_to_local_url,
    parse_gen_id_from_url,
    source_paths_to_gen_paths,
)


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


class PhotoFrameViewerApp(hass.Hass):
    """Photo frame viewer with generation-based atomic batch swap.

    Discovers available images by scanning ``source_dir`` directly via
    ``os.listdir()`` (AppDaemon has filesystem access to ``/media/``),
    copies them into a versioned "generation" directory under
    ``/config/www/photo-frame/live/<gen>/`` via HA ``shell_command``
    services, and keeps a dashboard ``input_select`` + ``input_text``
    URL in sync.

    The generation swap ensures the displayed image is never a broken
    link when the external service (Immich fetcher) refreshes the
    source directory.
    """

    IMAGE_EXTENSIONS: frozenset[str] = frozenset(
        {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    )

    DEFAULTS: dict[str, Any] = {
        "source_dir": "/media/immich-photos",
        "ha_local_url_base": "/local/photo-frame/live",
        "stage_shell_command": "photo_frame_stage_gen",
        "cleanup_shell_command": "photo_frame_cleanup_gen",
        "source_poll_interval_s": 30,
        "stage_settle_delay_s": 3,
        "picker_entity_id": "input_select.wall_display_photo_frame_image",
        "paused_entity_id": "input_boolean.wall_display_photo_frame_paused",
        "interval_entity_id": "input_number.wall_display_photo_frame_interval_seconds",
        "cache_bust_entity_id": "input_text.wall_display_photo_frame_cache_bust",
        "image_local_url_entity_id": "input_text.wall_display_photo_frame_image_local_url",
        "fallback_image_path": "/config/www/immich-album/no-image.jpg",
        "options_max": 100,
        "refresh_options_every_s": 60,
        "auto_cycle": True,
        "reset_timer_on_manual_nav": True,
    }

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        cfg = {**self.DEFAULTS, **(self.args or {})}

        self.source_dir: str = str(cfg["source_dir"])
        self.ha_source_dir: str = str(cfg.get("ha_source_dir") or cfg["source_dir"])
        self.ha_local_url_base: str = str(cfg["ha_local_url_base"]).rstrip("/")
        self.stage_shell_command: str = str(cfg["stage_shell_command"])
        self.cleanup_shell_command: str = str(cfg["cleanup_shell_command"])
        self.source_poll_interval_s: float = max(5.0, float(cfg["source_poll_interval_s"]))
        self.stage_settle_delay_s: float = max(1.0, float(cfg["stage_settle_delay_s"]))
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

        # Slideshow timer
        self._timer_handle: Optional[Any] = None
        self._periodic_handle: Optional[Any] = None
        self._poll_handle: Optional[Any] = None

        # Label maps (current live generation)
        self._label_to_path: dict[str, str] = {}
        self._path_to_label: dict[str, str] = {}
        self._last_published_local_url: Optional[str] = None

        # Generation state
        self._current_gen_id: Optional[str] = None
        self._current_fingerprint: Optional[str] = None
        self._next_gen_counter: int = 1
        self._staging_in_progress: bool = False

        # Pending generation (staged but not yet displayed)
        self._pending_gen_id: Optional[str] = None
        self._pending_labels: list[str] = []
        self._pending_label_to_path: dict[str, str] = {}
        self._pending_path_to_label: dict[str, str] = {}
        self._pending_fingerprint: Optional[str] = None

        # Guard: suppress picker-change handler during programmatic updates
        self._suppress_picker_handler: bool = False
        # Flag set by _on_tick before select_next so _on_picker_change
        # knows the change is tick-driven (not a manual button press).
        self._tick_advance_pending: bool = False

        self.log(
            "PhotoFrameViewerApp init "
            f"source_dir={self.source_dir} "
            f"ha_source_dir={self.ha_source_dir} "
            f"ha_local_url_base={self.ha_local_url_base} "
            f"picker={self.picker_entity_id}",
            level="INFO",
        )

        # Recover generation counter from previously published URL
        self._recover_gen_from_url()

        # Watch selection (manual nav or our own auto-advance).
        self.listen_state(self._on_picker_change, self.picker_entity_id)
        self.listen_state(self._on_pause_change, self.paused_entity_id)
        self.listen_state(self._on_interval_change, self.interval_entity_id)

        # Poll source directory for changes periodically.
        self._poll_handle = self.run_every(
            self._poll_for_changes_cb,
            self.datetime(),
            self.source_poll_interval_s,
        )

        # React to fetcher batch-ready events for faster detection.
        self.listen_event(self._on_batch_ready, "immich_fetcher_batch_ready")

        # Kick off: always stage on startup to guarantee files exist on HA.
        # If we recovered a gen, populate the picker first so the dashboard
        # has *something* while the new gen stages.
        if self._current_gen_id is not None:
            self._refresh_picker_from_current_gen(reason="init")
            self._publish_selected_local_url(self._picker_value(), reason="init")
            self._current_fingerprint = None  # force re-stage
        self._poll_for_changes(reason="init")

        self._sync_timer(reason="init")

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover_gen_from_url(self) -> None:
        """On startup, recover the generation counter from the existing URL helper."""
        raw = str(self.get_state(self.image_local_url_entity_id) or "").strip()
        if not raw:
            return
        gen = parse_gen_id_from_url(raw, self.ha_local_url_base)
        if gen is not None:
            gen_int = int(gen)
            self._current_gen_id = gen
            self._next_gen_counter = gen_int + 1
            self._last_published_local_url = raw
            self.log(
                f"PhotoFrameViewerApp: recovered gen={gen} from url={raw!r}, "
                f"next_counter={self._next_gen_counter}",
                level="INFO",
            )

    # ------------------------------------------------------------------
    # Source reading
    # ------------------------------------------------------------------

    def _read_source_file_list(self) -> list[str]:
        """Read the source file list by scanning source_dir on disk.

        AppDaemon has direct filesystem access to /media/ so we can
        use os.listdir() instead of relying on an HA folder sensor.
        """
        try:
            entries = os.listdir(self.source_dir)
        except FileNotFoundError:
            self.log(
                f"PhotoFrameViewerApp: source_dir not found: {self.source_dir}",
                level="WARNING",
            )
            return []
        except OSError as exc:
            self.log(
                f"PhotoFrameViewerApp: error reading source_dir: {exc}",
                level="WARNING",
            )
            return []

        out: list[str] = []
        for name in sorted(entries):
            ext = os.path.splitext(name)[1].lower()
            if ext in self.IMAGE_EXTENSIONS:
                out.append(os.path.join(self.source_dir, name))
        return out[: self.options_max]

    @staticmethod
    def _stat_files(paths: list[str]) -> dict[str, tuple[int, float]]:
        """Return {path: (size_bytes, mtime)} for each path that exists."""
        stats: dict[str, tuple[int, float]] = {}
        for p in paths:
            try:
                st = os.stat(p)
                stats[p] = (st.st_size, st.st_mtime)
            except OSError:
                pass
        return stats

    # ------------------------------------------------------------------
    # Change detection + staging
    # ------------------------------------------------------------------

    def _poll_for_changes(self, *, reason: str) -> None:
        """Check for source changes and trigger staging if needed."""
        source_paths = self._read_source_file_list()
        if not source_paths:
            self.log("PhotoFrameViewerApp: source file list empty, skipping poll", level="DEBUG")
            return

        fp = compute_fingerprint(source_paths, file_stats=self._stat_files(source_paths))
        if fp == self._current_fingerprint and self._current_gen_id is not None:
            return  # No change

        if self._staging_in_progress:
            self.log("PhotoFrameViewerApp: staging already in progress, skipping", level="DEBUG")
            return

        self.log(
            f"PhotoFrameViewerApp: source changed (reason={reason}) "
            f"old_fp={self._current_fingerprint!r} new_fp={fp[:12]}... "
            f"files={len(source_paths)}",
            level="INFO",
        )
        self._stage_new_generation(source_paths, fp)

    def _stage_new_generation(self, source_paths: list[str], fingerprint: str) -> None:
        """Call the staging shell_command and schedule the settle callback."""
        gen_id = str(self._next_gen_counter)
        self._next_gen_counter += 1
        self._staging_in_progress = True

        # If there's an existing pending gen that was never adopted, clean it up
        if self._pending_gen_id is not None:
            old_pending = self._pending_gen_id
            self._clear_pending()
            self.log(
                f"PhotoFrameViewerApp: replacing pending gen={old_pending} with gen={gen_id}",
                level="INFO",
            )
            self._call_cleanup(old_pending, reason="replace_pending")

        self.log(
            f"PhotoFrameViewerApp: staging gen={gen_id} from {self.source_dir} "
            f"({len(source_paths)} files)",
            level="INFO",
        )

        self.call_service(
            f"shell_command/{self.stage_shell_command}",
            source_dir=self.ha_source_dir,
            gen_id=gen_id,
        )

        # Stash staging context for the settle callback
        self._staging_gen_id = gen_id
        self._staging_source_paths = source_paths
        self._staging_fingerprint = fingerprint

        self.run_in(self._on_stage_settled, self.stage_settle_delay_s)

    def _on_stage_settled(self, kwargs: Any) -> None:
        """Called after the staging shell_command has had time to complete."""
        self._staging_in_progress = False

        gen_id = getattr(self, "_staging_gen_id", None)
        source_paths = getattr(self, "_staging_source_paths", [])
        fingerprint = getattr(self, "_staging_fingerprint", None)

        if not gen_id or not source_paths:
            self.log("PhotoFrameViewerApp: stage settled but no staging context", level="WARNING")
            return

        # Build gen paths and label maps for the new generation
        gen_paths = source_paths_to_gen_paths(source_paths, gen_id)
        labels, l2p, p2l = build_label_maps(gen_paths, self.options_max)

        self._pending_gen_id = gen_id
        self._pending_labels = labels
        self._pending_label_to_path = l2p
        self._pending_path_to_label = p2l
        self._pending_fingerprint = fingerprint

        self.log(
            f"PhotoFrameViewerApp: gen={gen_id} ready as pending "
            f"({len(labels)} labels)",
            level="INFO",
        )

        # Do NOT update picker options here. Changing options while the user
        # is viewing (especially paused) causes HA to reset the selected value,
        # which triggers an unwanted image change. Options are updated only
        # when the image would naturally change (tick advance or manual nav)
        # via _apply_pending_gen().

        # Exception: very first generation -- there's nothing displayed yet.
        if self._current_gen_id is None:
            self.log("PhotoFrameViewerApp: first gen, applying immediately", level="INFO")
            self._apply_pending_gen(reason="first_gen")

    # ------------------------------------------------------------------
    # Picker options management
    # ------------------------------------------------------------------

    def _refresh_picker_from_current_gen(self, *, reason: str) -> None:
        """Rebuild label maps from the current gen using the source file list.

        Used on startup recovery when we have a current gen but need to
        populate the in-memory maps.
        """
        source_paths = self._read_source_file_list()
        if not source_paths:
            source_paths = [self.fallback_image_path]

        gen_paths = source_paths_to_gen_paths(source_paths, self._current_gen_id or "1")
        labels, l2p, p2l = build_label_maps(gen_paths, self.options_max)

        self._label_to_path = l2p
        self._path_to_label = p2l
        self._current_fingerprint = compute_fingerprint(
            source_paths, file_stats=self._stat_files(source_paths)
        )

        existing_opts = self._picker_options()
        if existing_opts != labels:
            self.call_service(
                "input_select/set_options",
                entity_id=self.picker_entity_id,
                options=labels,
            )

    def _refresh_picker_options(self, *, reason: str) -> None:
        """Update the input_select options from the current generation's maps."""
        source_paths = self._read_source_file_list()
        if not source_paths:
            source_paths = [self.fallback_image_path]
        gen_id = self._current_gen_id or "1"
        gen_paths = source_paths_to_gen_paths(source_paths, gen_id)
        labels, label_to_path, path_to_label = build_label_maps(gen_paths, self.options_max)

        current_label = self._picker_value()
        current_path = self._label_to_path.get(current_label)
        current_basename = basename(current_path) if current_path else ""

        desired_label = ""
        if current_basename:
            for lbl, pth in label_to_path.items():
                if basename(pth) == current_basename:
                    desired_label = lbl
                    break
        if not desired_label and current_label in labels:
            desired_label = current_label
        if not desired_label:
            desired_label = labels[0] if labels else ""

        existing_opts = self._picker_options()
        if existing_opts != labels:
            self.log(
                f"PhotoFrameViewerApp: updating picker options "
                f"count={len(labels)} reason={reason}",
                level="INFO",
            )
            self.call_service(
                "input_select/set_options",
                entity_id=self.picker_entity_id,
                options=labels,
            )

        if desired_label and desired_label != current_label:
            self.call_service(
                "input_select/select_option",
                entity_id=self.picker_entity_id,
                option=desired_label,
            )

    # ------------------------------------------------------------------
    # Publish URL + generation swap
    # ------------------------------------------------------------------

    def _apply_pending_gen(self, *, reason: str) -> None:
        """Promote the pending generation to current and update the picker.

        Called at the moment the displayed image would naturally change
        (tick advance, manual nav, or first-gen init) so the user never
        sees an unexpected image swap.

        Uses ``_suppress_picker_handler`` to prevent the programmatic
        picker update from cascading back through ``_on_picker_change``.
        """
        if self._pending_gen_id is None:
            return

        pending_labels = self._pending_labels[:]
        self._finalize_pending(reason=reason)

        self._suppress_picker_handler = True
        try:
            existing_opts = self._picker_options()
            if existing_opts != pending_labels:
                self.log(
                    f"PhotoFrameViewerApp: updating picker options "
                    f"count={len(pending_labels)} reason=apply_{reason}",
                    level="INFO",
                )
                self.call_service(
                    "input_select/set_options",
                    entity_id=self.picker_entity_id,
                    options=pending_labels,
                )

            if pending_labels:
                first_label = pending_labels[0]
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.picker_entity_id,
                    option=first_label,
                )
                self._publish_selected_local_url(first_label, reason=reason)
        finally:
            self._suppress_picker_handler = False

    def _publish_selected_local_url(self, label: str, *, reason: str) -> None:
        """Publish the ``/local/...`` URL for the selected label."""
        label = str(label or "").strip()
        path = (self._label_to_path.get(label) or "").strip()

        if not path:
            path = self.fallback_image_path

        gen_id = self._current_gen_id
        local_url = gen_path_to_local_url(path, self.ha_local_url_base, gen_id or "")

        if self._last_published_local_url == local_url:
            return

        self.log(
            f"PhotoFrameViewerApp: publish url={local_url!r} "
            f"label={label!r} gen={gen_id} reason={reason}",
            level="DEBUG",
        )
        self.call_service(
            "input_text/set_value",
            entity_id=self.image_local_url_entity_id,
            value=local_url,
        )
        self._last_published_local_url = local_url
        self._touch_cache_bust()

    def _finalize_pending(self, *, reason: str) -> None:
        """Promote pending gen to current and clean up the old gen."""
        old_gen = self._current_gen_id
        new_gen = self._pending_gen_id

        self._current_gen_id = new_gen
        self._current_fingerprint = self._pending_fingerprint
        self._label_to_path = self._pending_label_to_path.copy()
        self._path_to_label = self._pending_path_to_label.copy()

        self._clear_pending()

        self.log(
            f"PhotoFrameViewerApp: finalized swap old_gen={old_gen} -> "
            f"new_gen={new_gen} reason={reason}",
            level="INFO",
        )

        if old_gen is not None and old_gen != new_gen:
            self._call_cleanup(old_gen, reason=f"finalize_{reason}")

    def _clear_pending(self) -> None:
        self._pending_gen_id = None
        self._pending_labels = []
        self._pending_label_to_path = {}
        self._pending_path_to_label = {}
        self._pending_fingerprint = None

    def _call_cleanup(self, gen_id: str, *, reason: str) -> None:
        self.log(
            f"PhotoFrameViewerApp: cleanup gen={gen_id} reason={reason}",
            level="INFO",
        )
        self.call_service(
            f"shell_command/{self.cleanup_shell_command}",
            gen_id=gen_id,
        )

    def _touch_cache_bust(self) -> None:
        value = str(int(time.time()))
        self.call_service(
            "input_text/set_value",
            entity_id=self.cache_bust_entity_id,
            value=value,
        )

    # ------------------------------------------------------------------
    # HA state helpers
    # ------------------------------------------------------------------

    def _is_paused(self) -> bool:
        return str(self.get_state(self.paused_entity_id) or "").strip().lower() == "on"

    def _interval_s(self) -> float:
        raw = self.get_state(self.interval_entity_id)
        seconds = _safe_float(raw, default=10.0)
        if seconds <= 0:
            seconds = 10.0
        return max(1.0, seconds)

    def _picker_options(self) -> list[str]:
        st = self.get_state(self.picker_entity_id, attribute="all")
        if not isinstance(st, dict):
            return []
        attrs = st.get("attributes")
        if not isinstance(attrs, dict):
            return []
        opts = attrs.get("options")
        if isinstance(opts, list):
            return [str(o) for o in opts]
        return []

    def _picker_value(self) -> str:
        return str(self.get_state(self.picker_entity_id) or "").strip()

    # ------------------------------------------------------------------
    # Timer control
    # ------------------------------------------------------------------

    def _cancel_timer(self) -> None:
        handle = self._timer_handle
        if handle is None:
            return
        self._timer_handle = None
        try:
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
        self.log(
            f"PhotoFrameViewerApp: scheduled tick in {interval:.1f}s reason={reason}",
            level="DEBUG",
        )

    def _sync_timer(self, *, reason: str) -> None:
        if self._is_paused():
            self._cancel_timer()
            self.log(
                f"PhotoFrameViewerApp: paused; timer cancelled reason={reason}",
                level="INFO",
            )
            return
        self._schedule_next(reason=reason)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _poll_for_changes_cb(self, kwargs: Any) -> None:
        self._poll_for_changes(reason="poll")

    def _on_batch_ready(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Fetcher wrote a new batch — poll for changes immediately."""
        self._poll_for_changes(reason="batch_ready")

    def _on_picker_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: Any) -> None:
        if self._suppress_picker_handler:
            return
        new_label = str(new or "").strip()
        if not new_label:
            return

        # Tick-driven advance: publish quietly and return.
        if self._tick_advance_pending:
            self._tick_advance_pending = False
            self._publish_selected_local_url(new_label, reason="tick")
            return

        # Manual navigation while a pending gen is queued.
        if self._pending_gen_id is not None:
            self.log(
                f"PhotoFrameViewerApp: manual nav to {new_label!r} (applying pending gen)",
                level="INFO",
            )
            self._apply_pending_gen(reason="manual_nav")
            if self.reset_timer_on_manual_nav:
                self._schedule_next(reason="manual_nav")
            return

        self.log(f"PhotoFrameViewerApp: manual nav to {new_label!r}", level="INFO")
        self._publish_selected_local_url(new_label, reason="manual_nav")
        if self.reset_timer_on_manual_nav:
            self._schedule_next(reason="manual_nav")

    def _on_pause_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: Any) -> None:
        self._sync_timer(reason="pause_change")

    def _on_interval_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: Any) -> None:
        self._sync_timer(reason="interval_change")

    def _on_tick(self, kwargs: Any) -> None:
        self._timer_handle = None
        if self._is_paused():
            self._cancel_timer()
            return

        if self._pending_gen_id is not None:
            self._apply_pending_gen(reason="tick")

        opts = self._picker_options()
        if len(opts) <= 1:
            self._schedule_next(reason="tick_no_options")
            return

        self._tick_advance_pending = True
        self.call_service(
            "input_select/select_next",
            entity_id=self.picker_entity_id,
            cycle=self.auto_cycle,
        )
        self._schedule_next(reason="tick")
