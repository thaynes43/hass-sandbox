"""Workflow profile registry for the ComfyUI image provider.

A *workflow profile* is the complete, named description of one ComfyUI graph:
the workflow JSON to POST, the node/input bindings the provider writes into it
(prompt, negative prompt, seed, output prefix, image slots), the literal node
input overrides to apply, and the render timeout.

The registry is declared in ``workflow_profiles.yaml`` next to this module and
validated strictly at load time, so a typo in a node id is a startup
``ValueError`` and never a silent no-op against a live ComfyUI.

This module is transport-adjacent config only: it knows nothing about Home
Assistant, apps, or prompts. The app layer chooses a profile *name*; the
provider resolves it here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

_PROFILES_DIR = Path(__file__).resolve().parent
_DEFAULT_PROFILES_FILE = _PROFILES_DIR / "workflow_profiles.yaml"

# Workflow inputs that name a model file on the ComfyUI server. Surfaced as
# ``required_models`` for docs/logging/meta — never used to gate a request.
_MODEL_INPUT_NAMES = ("unet_name", "clip_name", "vae_name", "lora_name", "ckpt_name")


class WorkflowProfileError(ValueError):
    """Raised when the workflow profile registry is malformed or inconsistent."""


class UnknownWorkflowProfileError(WorkflowProfileError):
    """Raised when a profile name is not registered."""


@dataclass(frozen=True)
class NodeInputBinding:
    """A single ``<node>.<input>`` slot the provider writes a value into."""

    node: str
    input: str


@dataclass(frozen=True)
class OutputBinding:
    """The SaveImage node whose ``filename_prefix`` the provider sets."""

    node: str
    input: str = "filename_prefix"


@dataclass(frozen=True)
class ImageSlot:
    """One ordered reference-image slot.

    ``unlink`` names ``<node>.<input>`` pairs that must be deleted from the
    graph along with this slot's LoadImage node when the caller supplies fewer
    images than the profile has slots. They are optional inputs on
    ``TextEncodeQwenImageEditPlus``; leaving them pointing at a deleted node is
    a ComfyUI validation error.
    """

    node: str
    input: str
    unlink: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowProfile:
    """One named, validated ComfyUI graph the provider can send."""

    name: str
    description: str
    model: str
    workflow: str
    workflow_path: Path
    timeout_s: float
    prompt: NodeInputBinding
    output: OutputBinding
    images: Tuple[ImageSlot, ...]
    negative_prompt: Optional[NodeInputBinding] = None
    seed: Optional[NodeInputBinding] = None
    overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: MappingProxyType({}))
    required_models: Tuple[str, ...] = ()

    @property
    def max_images(self) -> int:
        return len(self.images)

    def load_template(self) -> Dict[str, Any]:
        """Read and parse the workflow JSON. Raises WorkflowProfileError on failure."""
        try:
            return json.loads(self.workflow_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - validated at load time
            raise WorkflowProfileError(
                f"workflow profile {self.name!r}: failed to read workflow {self.workflow_path}: {exc!r}"
            ) from exc


@dataclass(frozen=True)
class WorkflowProfileRegistry:
    """All registered profiles plus the YAML-declared default."""

    default_profile: str
    profiles: Mapping[str, WorkflowProfile]
    source_path: Path

    @property
    def names(self) -> Tuple[str, ...]:
        """Profile names in YAML declaration order."""
        return tuple(self.profiles.keys())

    def has(self, name: Optional[str]) -> bool:
        return bool(name) and str(name) in self.profiles

    def get(self, name: Optional[str]) -> WorkflowProfile:
        key = str(name or "").strip()
        if not key:
            raise UnknownWorkflowProfileError(
                f"workflow profile name is empty; known profiles: {list(self.names)}"
            )
        try:
            return self.profiles[key]
        except KeyError:
            raise UnknownWorkflowProfileError(
                f"unknown ComfyUI workflow profile {key!r}; known profiles: {list(self.names)}"
            ) from None

    def get_or_default(self, name: Optional[str]) -> WorkflowProfile:
        if self.has(name):
            return self.profiles[str(name)]
        return self.profiles[self.default_profile]

    @property
    def default(self) -> WorkflowProfile:
        return self.profiles[self.default_profile]

    def model_labels(self) -> Tuple[str, ...]:
        """Distinct ``model`` labels across all profiles, sorted."""
        return tuple(sorted({p.model for p in self.profiles.values()}))


# --- loading ---------------------------------------------------------------

_registry_cache: Dict[str, WorkflowProfileRegistry] = {}


def clear_cache() -> None:
    """Drop the cached registry (tests)."""
    _registry_cache.clear()


def load_workflow_profiles(path: Optional[Path] = None) -> WorkflowProfileRegistry:
    """Load and validate the workflow profile registry. Cached per path."""
    profiles_path = Path(path) if path is not None else _DEFAULT_PROFILES_FILE
    key = str(profiles_path.resolve()) if profiles_path.exists() else str(profiles_path)
    cached = _registry_cache.get(key)
    if cached is not None:
        return cached
    registry = _load(profiles_path)
    _registry_cache[key] = registry
    return registry


def _load(profiles_path: Path) -> WorkflowProfileRegistry:
    if not profiles_path.exists():
        raise WorkflowProfileError(f"ComfyUI workflow profiles file not found: {profiles_path}")

    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a hard dependency
        raise ImportError(
            "PyYAML is required for ComfyUI workflow profiles. Install with: pip install pyyaml"
        ) from None

    try:
        raw = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise WorkflowProfileError(f"failed to parse {profiles_path}: {exc!r}") from exc

    if not isinstance(raw, dict):
        raise WorkflowProfileError(f"{profiles_path}: top level must be a mapping")

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise WorkflowProfileError(f"{profiles_path}: 'profiles' must be a non-empty mapping")

    profiles: Dict[str, WorkflowProfile] = {}
    for name, body in raw_profiles.items():
        pname = str(name).strip()
        if not pname:
            raise WorkflowProfileError(f"{profiles_path}: profile names must be non-empty strings")
        if not isinstance(body, dict):
            raise WorkflowProfileError(f"{profiles_path}: profile {pname!r} must be a mapping")
        # Relative workflow paths resolve against the registry file's own
        # directory, so a registry can be relocated (or synthesised in a test)
        # without rewriting every path in it.
        profiles[pname] = _parse_profile(pname, body, profiles_path.parent)

    default_profile = str(raw.get("default_profile") or "").strip()
    if not default_profile:
        raise WorkflowProfileError(f"{profiles_path}: 'default_profile' is required")
    if default_profile not in profiles:
        raise WorkflowProfileError(
            f"{profiles_path}: default_profile {default_profile!r} is not a registered profile; "
            f"known profiles: {list(profiles)}"
        )

    return WorkflowProfileRegistry(
        default_profile=default_profile,
        profiles=MappingProxyType(dict(profiles)),
        source_path=profiles_path,
    )


def _require_str(value: Any, *, what: str, profile: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowProfileError(f"profile {profile!r}: {what} is required")
    return text


def _parse_binding(raw: Any, *, what: str, profile: str) -> NodeInputBinding:
    if not isinstance(raw, dict):
        raise WorkflowProfileError(f"profile {profile!r}: bindings.{what} must be a mapping")
    return NodeInputBinding(
        node=_require_str(raw.get("node"), what=f"bindings.{what}.node", profile=profile),
        input=_require_str(raw.get("input"), what=f"bindings.{what}.input", profile=profile),
    )


def _parse_profile(name: str, body: Dict[str, Any], base_dir: Path) -> WorkflowProfile:
    description = _require_str(body.get("description"), what="description", profile=name)
    model = _require_str(body.get("model"), what="model", profile=name)
    workflow = _require_str(body.get("workflow"), what="workflow", profile=name)

    raw_timeout = body.get("timeout_s")
    try:
        timeout_s = float(raw_timeout)
    except (TypeError, ValueError):
        raise WorkflowProfileError(
            f"profile {name!r}: timeout_s must be a positive number (got {raw_timeout!r})"
        ) from None
    if timeout_s <= 0:
        raise WorkflowProfileError(f"profile {name!r}: timeout_s must be a positive number")

    workflow_path = Path(workflow)
    if not workflow_path.is_absolute():
        workflow_path = base_dir / workflow_path
    if not workflow_path.exists():
        raise WorkflowProfileError(
            f"profile {name!r}: workflow file does not exist: {workflow_path}"
        )
    try:
        graph = json.loads(workflow_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkflowProfileError(
            f"profile {name!r}: failed to parse workflow {workflow_path}: {exc!r}"
        ) from exc
    if not isinstance(graph, dict) or not graph:
        raise WorkflowProfileError(
            f"profile {name!r}: workflow {workflow_path} must be a non-empty API-format object"
        )

    bindings = body.get("bindings")
    if not isinstance(bindings, dict):
        raise WorkflowProfileError(f"profile {name!r}: 'bindings' must be a mapping")

    prompt = _parse_binding(bindings.get("prompt"), what="prompt", profile=name)
    negative_prompt = (
        _parse_binding(bindings.get("negative_prompt"), what="negative_prompt", profile=name)
        if bindings.get("negative_prompt") is not None
        else None
    )
    seed = (
        _parse_binding(bindings.get("seed"), what="seed", profile=name)
        if bindings.get("seed") is not None
        else None
    )

    raw_output = bindings.get("output")
    if not isinstance(raw_output, dict):
        raise WorkflowProfileError(f"profile {name!r}: bindings.output must be a mapping")
    output = OutputBinding(
        node=_require_str(raw_output.get("node"), what="bindings.output.node", profile=name),
        input=str(raw_output.get("input") or "filename_prefix").strip() or "filename_prefix",
    )

    raw_images = bindings.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise WorkflowProfileError(
            f"profile {name!r}: bindings.images must be a non-empty list (slot 0 is required)"
        )
    images: list[ImageSlot] = []
    for idx, raw_slot in enumerate(raw_images):
        if not isinstance(raw_slot, dict):
            raise WorkflowProfileError(
                f"profile {name!r}: bindings.images[{idx}] must be a mapping"
            )
        raw_unlink = raw_slot.get("unlink") or []
        if not isinstance(raw_unlink, list):
            raise WorkflowProfileError(
                f"profile {name!r}: bindings.images[{idx}].unlink must be a list"
            )
        images.append(
            ImageSlot(
                node=_require_str(
                    raw_slot.get("node"), what=f"bindings.images[{idx}].node", profile=name
                ),
                input=_require_str(
                    raw_slot.get("input"), what=f"bindings.images[{idx}].input", profile=name
                ),
                unlink=tuple(
                    _require_str(u, what=f"bindings.images[{idx}].unlink[]", profile=name)
                    for u in raw_unlink
                ),
            )
        )

    raw_overrides = body.get("overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise WorkflowProfileError(f"profile {name!r}: 'overrides' must be a mapping")
    overrides: Dict[str, Mapping[str, Any]] = {}
    for node_id, node_overrides in raw_overrides.items():
        if not isinstance(node_overrides, dict) or not node_overrides:
            raise WorkflowProfileError(
                f"profile {name!r}: overrides[{node_id!r}] must be a non-empty mapping of input -> value"
            )
        overrides[str(node_id)] = MappingProxyType(dict(node_overrides))

    profile = WorkflowProfile(
        name=name,
        description=description,
        model=model,
        workflow=workflow,
        workflow_path=workflow_path,
        timeout_s=timeout_s,
        prompt=prompt,
        output=output,
        images=tuple(images),
        negative_prompt=negative_prompt,
        seed=seed,
        overrides=MappingProxyType(overrides),
        required_models=_required_models(graph),
    )
    _validate_against_graph(profile, graph)
    return profile


def _node_inputs(graph: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        raise WorkflowProfileError(f"node {node_id!r} does not exist in the workflow")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise WorkflowProfileError(f"node {node_id!r} has no 'inputs' mapping in the workflow")
    return inputs


def _validate_against_graph(profile: WorkflowProfile, graph: Dict[str, Any]) -> None:
    """Every node id and input name the profile names must exist in the graph."""
    prefix = f"profile {profile.name!r} ({profile.workflow})"

    def _check_input(node_id: str, input_name: str, what: str) -> None:
        try:
            inputs = _node_inputs(graph, node_id)
        except WorkflowProfileError as exc:
            raise WorkflowProfileError(f"{prefix}: {what}: {exc}") from None
        if input_name not in inputs:
            raise WorkflowProfileError(
                f"{prefix}: {what}: node {node_id!r} has no input {input_name!r} "
                f"(has: {sorted(inputs)})"
            )

    _check_input(profile.prompt.node, profile.prompt.input, "bindings.prompt")
    if profile.negative_prompt is not None:
        _check_input(
            profile.negative_prompt.node, profile.negative_prompt.input, "bindings.negative_prompt"
        )
    if profile.seed is not None:
        _check_input(profile.seed.node, profile.seed.input, "bindings.seed")
    _check_input(profile.output.node, profile.output.input, "bindings.output")

    seen_slot_nodes: set[str] = set()
    for idx, slot in enumerate(profile.images):
        _check_input(slot.node, slot.input, f"bindings.images[{idx}]")
        if slot.node in seen_slot_nodes:
            raise WorkflowProfileError(
                f"{prefix}: bindings.images[{idx}]: node {slot.node!r} is bound to more than one slot"
            )
        seen_slot_nodes.add(slot.node)
        for target in slot.unlink:
            node_id, sep, input_name = str(target).partition(".")
            if not sep or not node_id or not input_name:
                raise WorkflowProfileError(
                    f"{prefix}: bindings.images[{idx}].unlink: {target!r} must be '<node>.<input>'"
                )
            _check_input(node_id, input_name, f"bindings.images[{idx}].unlink")
            linked = _node_inputs(graph, node_id)[input_name]
            if not (isinstance(linked, list) and linked and str(linked[0]) == slot.node):
                raise WorkflowProfileError(
                    f"{prefix}: bindings.images[{idx}].unlink: {target!r} does not link to "
                    f"slot node {slot.node!r} (links to {linked!r})"
                )

    for node_id, node_overrides in profile.overrides.items():
        for input_name in node_overrides:
            _check_input(node_id, str(input_name), f"overrides[{node_id!r}]")


def _required_models(graph: Dict[str, Any]) -> Tuple[str, ...]:
    """Model files the graph loads, derived from well-known loader input names."""
    found: set[str] = set()
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key in _MODEL_INPUT_NAMES:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                found.add(value.strip())
    return tuple(sorted(found))
