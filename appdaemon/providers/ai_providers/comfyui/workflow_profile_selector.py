"""Home Assistant selection of the ComfyUI workflow profile, for the app layer.

The provider never reads Home Assistant. This module is the bridge: it
provisions the helpers an operator drives, reads them at call time, and hands
the app a profile name plus where that name came from.

Three helpers make up the control surface:

* ``input_select.comfyui_active_workflow`` — the profile every zone uses.
* ``input_select.comfyui_trial_workflow``  — the profile a zone uses while its
  trial toggle is on, so a new workflow can be proved on one camera.
* ``input_boolean.<zone>_detection_summary_trial_workflow`` — per zone, the
  toggle that opts that zone into the trial select.

Promote = set Active to what Trial has been running. Roll back = set Active
back to the previous profile. Neither needs a deploy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from .workflow_profiles import WorkflowProfileRegistry, load_workflow_profiles

logger = logging.getLogger(__name__)

# User-facing helper names. The entity ids below are what HA derives from them;
# renaming a helper renames its entity, so treat both as a public contract.
ACTIVE_HELPER_NAME = "ComfyUI Active Workflow"
TRIAL_HELPER_NAME = "ComfyUI Trial Workflow"
ACTIVE_ENTITY_ID = "input_select.comfyui_active_workflow"
TRIAL_ENTITY_ID = "input_select.comfyui_trial_workflow"

SOURCE_HA_TRIAL = "ha_trial"
SOURCE_HA_ACTIVE = "ha_active"
SOURCE_YAML_DEFAULT = "yaml_default"

# HA states that mean "no usable value", not "a profile named this". Compared
# case-insensitively because these are HA keywords, unlike profile names.
_UNUSABLE_STATES = frozenset({"", "unknown", "unavailable", "none", "loading"})

# Every DetectionSummary zone provisions the same two global helpers at
# startup, concurrently, on AppDaemon's shared event loop. ensure_helper is
# check-then-create, so without this the 7 zones can race into
# comfyui_active_workflow_2. One lock + a done-set makes it exactly-once per
# process; HA's own existence check covers restarts.
_GLOBAL_PROVISION_LOCK = asyncio.Lock()
_PROVISIONED_GLOBAL_HELPERS: set[str] = set()


def reset_global_provisioning_state() -> None:
    """Forget which global helpers this process has provisioned (tests)."""
    _PROVISIONED_GLOBAL_HELPERS.clear()


@dataclass(frozen=True)
class ProfileSelection:
    """The profile to send, and where the name came from."""

    profile: str
    source: str
    # The raw HA state, when it was unusable and the YAML default was taken.
    rejected_state: Optional[str] = None


def trial_helper_name(zone_display_name: str) -> str:
    """The per-zone trial toggle's HA helper name."""
    return f"{zone_display_name} Detection Summary Trial Workflow"


class WorkflowProfileSelector:
    """Provisions and reads the ComfyUI workflow-selection helpers for one app."""

    def __init__(
        self,
        app: Any,
        *,
        log_prefix: str = "ComfyUI",
        registry: Optional[WorkflowProfileRegistry] = None,
    ) -> None:
        self._app = app
        self._log_prefix = log_prefix
        self._registry = registry if registry is not None else load_workflow_profiles()
        self._warned: set[str] = set()

    # --- registry passthroughs -----------------------------------------

    @property
    def profile_names(self) -> List[str]:
        return list(self._registry.names)

    @property
    def default_profile(self) -> str:
        return self._registry.default_profile

    # --- provisioning ---------------------------------------------------

    async def provision(self, provisioner: Any, *, zone_display_name: str) -> str:
        """Create the two global selects and this zone's trial toggle.

        Returns the zone's ``input_boolean`` entity id. Never raises: a
        provisioning failure degrades selection to the YAML default rather than
        taking the whole app's startup down with it.
        """
        options = self.profile_names
        async with _GLOBAL_PROVISION_LOCK:
            for name, entity_id in (
                (ACTIVE_HELPER_NAME, ACTIVE_ENTITY_ID),
                (TRIAL_HELPER_NAME, TRIAL_ENTITY_ID),
            ):
                if entity_id in _PROVISIONED_GLOBAL_HELPERS:
                    continue
                await self._ensure_helper(
                    provisioner,
                    "input_select",
                    name,
                    entity_id,
                    options=options,
                    initial=self.default_profile,
                )
                _PROVISIONED_GLOBAL_HELPERS.add(entity_id)

        boolean_name = trial_helper_name(zone_display_name)
        trial_entity_id = self._expected_entity_id(provisioner, "input_boolean", boolean_name)
        await self._ensure_helper(
            provisioner, "input_boolean", boolean_name, trial_entity_id, initial=False
        )
        return trial_entity_id

    async def _ensure_helper(
        self,
        provisioner: Any,
        helper_type: str,
        name: str,
        entity_id: str,
        **kwargs: Any,
    ) -> None:
        try:
            created = await provisioner.ensure_helper(helper_type, name, **kwargs)
        except Exception as exc:
            self._log(f"failed to provision {helper_type} {name!r}: {exc!r}", level="ERROR")
            return
        self._log(
            f"helper {entity_id} {'created' if created else 'already exists'}",
            level="INFO" if created else "DEBUG",
        )

    @staticmethod
    def _expected_entity_id(provisioner: Any, helper_type: str, name: str) -> str:
        """Ask the provisioner how HA will slug this name, with a local fallback."""
        slugger = getattr(provisioner, "_helper_slug", None)
        if callable(slugger):
            try:
                return f"{helper_type}.{slugger(helper_type, name)}"
            except Exception:
                pass
        slug = "_".join(
            part
            for part in "".join(ch if ch.isalnum() else "_" for ch in name.lower()).split("_")
            if part
        )
        return f"{helper_type}.{slug}"

    # --- option reconciliation -------------------------------------------

    def reconcile_options(self) -> None:
        """Re-point the two selects at the registered profiles.

        ``ensure_helper`` is create-only, so a release that adds or removes a
        profile leaves the live helper showing the old list forever. Every zone
        calls this on startup with identical values; it is a no-op once the
        options already match.
        """
        for entity_id in (ACTIVE_ENTITY_ID, TRIAL_ENTITY_ID):
            try:
                self._reconcile_one(entity_id)
            except Exception as exc:
                self._log(
                    f"failed to reconcile options for {entity_id}: {exc!r}", level="WARNING"
                )

    def _reconcile_one(self, entity_id: str) -> None:
        wanted = self.profile_names
        state = self._app.get_state(entity_id, attribute="all")
        if not isinstance(state, dict):
            self._log(
                f"{entity_id} not present yet — skipping option reconcile", level="DEBUG"
            )
            return

        attrs = state.get("attributes")
        raw_options = (attrs.get("options") if isinstance(attrs, dict) else None) or []
        live_options = [str(o) for o in raw_options] if isinstance(raw_options, (list, tuple)) else []
        current = str(state.get("state") or "").strip()

        if live_options != wanted:
            self._app.call_service(
                "input_select/set_options",
                target={"entity_id": entity_id},
                options=wanted,
            )
            self._log(
                f"{entity_id} options reconciled {live_options} -> {wanted}", level="INFO"
            )

        if current not in wanted:
            self._app.call_service(
                "input_select/select_option",
                target={"entity_id": entity_id},
                option=self.default_profile,
            )
            self._log(
                f"{entity_id} selection {current!r} is not a registered profile — "
                f"selected {self.default_profile!r}",
                level="INFO",
            )

    # --- selection --------------------------------------------------------

    def select(self, trial_boolean_entity_id: Optional[str]) -> ProfileSelection:
        """Resolve the profile to send for this run."""
        trial_on = self._is_on(trial_boolean_entity_id)
        entity_id = TRIAL_ENTITY_ID if trial_on else ACTIVE_ENTITY_ID
        source = SOURCE_HA_TRIAL if trial_on else SOURCE_HA_ACTIVE

        # Profile names are matched exactly as HA reports them — a registry may
        # legitimately name a profile with capitals, and silently lower-casing
        # would turn that into a permanent fall back to the default.
        raw = self._read_state(entity_id)
        if raw and raw.lower() not in _UNUSABLE_STATES and self._registry.has(raw):
            return ProfileSelection(profile=raw, source=source)

        self._warn_once(
            f"{entity_id}:{raw}",
            f"{entity_id} is {raw!r}, which is not a registered workflow profile "
            f"({self.profile_names}) — using the YAML default {self.default_profile!r}",
        )
        return ProfileSelection(
            profile=self.default_profile, source=SOURCE_YAML_DEFAULT, rejected_state=raw
        )

    def _is_on(self, entity_id: Optional[str]) -> bool:
        if not entity_id:
            return False
        # A toggle's state is an HA keyword, so case-insensitive is right here.
        return self._read_state(entity_id).lower() == "on"

    def _read_state(self, entity_id: str) -> str:
        """The raw state, stripped but NOT case-folded — it may be a profile name."""
        try:
            return str(self._app.get_state(entity_id) or "").strip()
        except Exception as exc:
            self._warn_once(
                f"{entity_id}:read_error",
                f"failed to read {entity_id}: {exc!r}",
            )
            return ""

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(message, level="WARNING")

    def _log(self, message: str, *, level: str) -> None:
        text = f"{self._log_prefix}: {message}" if self._log_prefix else message
        log = getattr(self._app, "log", None)
        if callable(log):
            log(text, level=level)
        else:  # pragma: no cover - non-AppDaemon callers
            logger.log(getattr(logging, level, logging.INFO), text)
