"""Workflow registry for the ComfyUI image provider.

A *workflow* is the complete, named description of one ComfyUI graph: the
graph JSON to POST, the node/input bindings the provider writes into it
(prompt, negative prompt, seed, output prefix, image slots), the literal node
input overrides to apply, the render timeout, and the metadata an operator
needs to tell one entry from another months later.

The registry is declared in ``workflow_registry.yaml`` next to this module and
validated strictly at load time, so a typo in a node id or a missing date is a
startup ``ValueError`` and never a silent no-op against a live ComfyUI.

Consumers select a workflow **by name** in AppDaemon config; nothing here
reads Home Assistant or changes at runtime.
"""

from __future__ import annotations

import datetime as _datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

_REGISTRY_DIR = Path(__file__).resolve().parent
_DEFAULT_REGISTRY_FILE = _REGISTRY_DIR / "workflow_registry.yaml"

# Graph inputs that name a model file on the ComfyUI server. Surfaced as
# ``required_models`` for docs/logging/meta — never used to gate a request.
_MODEL_INPUT_NAMES = ("unet_name", "clip_name", "vae_name", "lora_name", "ckpt_name")

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class WorkflowRegistryError(ValueError):
    """Raised when the workflow registry is malformed or inconsistent."""


class UnknownWorkflowError(WorkflowRegistryError):
    """Raised when a workflow name is not registered."""


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
    images than the workflow has slots. They are optional inputs on
    ``TextEncodeQwenImageEditPlus``; leaving them pointing at a deleted node is
    a ComfyUI validation error.
    """

    node: str
    input: str
    unlink: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Workflow:
    """One named, validated ComfyUI graph the provider can send."""

    name: str
    description: str
    expect: str
    model: str
    model_released: _datetime.date
    added: _datetime.date
    graph: str
    graph_path: Path
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

    def load_graph(self) -> Dict[str, Any]:
        """Read and parse the graph JSON. Raises WorkflowRegistryError on failure."""
        try:
            return json.loads(self.graph_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - validated at load time
            raise WorkflowRegistryError(
                f"workflow {self.name!r}: failed to read graph {self.graph_path}: {exc!r}"
            ) from exc


@dataclass(frozen=True)
class WorkflowRegistry:
    """All registered workflows plus the YAML-declared default."""

    default_workflow: str
    workflows: Mapping[str, Workflow]
    source_path: Path

    @property
    def names(self) -> Tuple[str, ...]:
        """Workflow names in YAML declaration order."""
        return tuple(self.workflows.keys())

    def has(self, name: Optional[str]) -> bool:
        return bool(name) and str(name) in self.workflows

    def get(self, name: Optional[str]) -> Workflow:
        key = str(name or "").strip()
        if not key:
            raise UnknownWorkflowError(
                f"workflow name is empty; registered workflows: {list(self.names)}"
            )
        try:
            return self.workflows[key]
        except KeyError:
            raise UnknownWorkflowError(
                f"unknown ComfyUI workflow {key!r}; registered workflows: {list(self.names)}"
            ) from None

    def get_or_default(self, name: Optional[str]) -> Workflow:
        if self.has(name):
            return self.workflows[str(name)]
        return self.workflows[self.default_workflow]

    @property
    def default(self) -> Workflow:
        return self.workflows[self.default_workflow]

    def model_labels(self) -> Tuple[str, ...]:
        """Distinct ``model`` labels across all workflows, sorted."""
        return tuple(sorted({w.model for w in self.workflows.values()}))


# --- loading ---------------------------------------------------------------

_registry_cache: Dict[str, WorkflowRegistry] = {}


def clear_cache() -> None:
    """Drop the cached registry (tests)."""
    _registry_cache.clear()


def load_workflow_registry(path: Optional[Path] = None) -> WorkflowRegistry:
    """Load and validate the workflow registry. Cached per path."""
    registry_path = Path(path) if path is not None else _DEFAULT_REGISTRY_FILE
    key = str(registry_path.resolve()) if registry_path.exists() else str(registry_path)
    cached = _registry_cache.get(key)
    if cached is not None:
        return cached
    registry = _load(registry_path)
    _registry_cache[key] = registry
    return registry


def _load(registry_path: Path) -> WorkflowRegistry:
    if not registry_path.exists():
        raise WorkflowRegistryError(f"ComfyUI workflow registry file not found: {registry_path}")

    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a hard dependency
        raise ImportError(
            "PyYAML is required for the ComfyUI workflow registry. Install with: pip install pyyaml"
        ) from None

    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise WorkflowRegistryError(f"failed to parse {registry_path}: {exc!r}") from exc

    if not isinstance(raw, dict):
        raise WorkflowRegistryError(f"{registry_path}: top level must be a mapping")

    raw_workflows = raw.get("workflows")
    if not isinstance(raw_workflows, dict) or not raw_workflows:
        raise WorkflowRegistryError(f"{registry_path}: 'workflows' must be a non-empty mapping")

    workflows: Dict[str, Workflow] = {}
    for name, body in raw_workflows.items():
        wname = str(name).strip()
        if not wname:
            raise WorkflowRegistryError(f"{registry_path}: workflow names must be non-empty strings")
        if not isinstance(body, dict):
            raise WorkflowRegistryError(f"{registry_path}: workflow {wname!r} must be a mapping")
        # Relative graph paths resolve against the registry file's own
        # directory, so a registry can be relocated (or synthesised in a test)
        # without rewriting every path in it.
        workflows[wname] = _parse_workflow(wname, body, registry_path.parent)

    default_workflow = str(raw.get("default_workflow") or "").strip()
    if not default_workflow:
        raise WorkflowRegistryError(f"{registry_path}: 'default_workflow' is required")
    if default_workflow not in workflows:
        raise WorkflowRegistryError(
            f"{registry_path}: default_workflow {default_workflow!r} is not a registered "
            f"workflow; registered workflows: {list(workflows)}"
        )

    return WorkflowRegistry(
        default_workflow=default_workflow,
        workflows=MappingProxyType(dict(workflows)),
        source_path=registry_path,
    )


def _require_str(value: Any, *, what: str, workflow: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowRegistryError(f"workflow {workflow!r}: {what} is required")
    return text


def _require_date(value: Any, *, what: str, workflow: str) -> _datetime.date:
    """Parse a required ISO date.

    PyYAML already turns an unquoted ``2025-09-22`` into a ``datetime.date``,
    so accept that as well as the string a quoted value produces.
    """
    if value is None or value == "":
        raise WorkflowRegistryError(f"workflow {workflow!r}: {what} is required (YYYY-MM-DD)")
    if isinstance(value, _datetime.datetime):
        return value.date()
    if isinstance(value, _datetime.date):
        return value
    # Only the dashed form. `date.fromisoformat` also accepts the basic
    # `20250922`, which an unquoted YAML value turns into an int — and an int
    # where a date belongs is a typo worth failing on, not guessing at.
    text = str(value).strip()
    if isinstance(value, str) and _ISO_DATE_RE.fullmatch(text):
        try:
            return _datetime.date.fromisoformat(text)
        except ValueError:
            pass
    raise WorkflowRegistryError(
        f"workflow {workflow!r}: {what} must be an ISO date (YYYY-MM-DD), got {value!r}"
    )


def _parse_binding(raw: Any, *, what: str, workflow: str) -> NodeInputBinding:
    if not isinstance(raw, dict):
        raise WorkflowRegistryError(f"workflow {workflow!r}: bindings.{what} must be a mapping")
    return NodeInputBinding(
        node=_require_str(raw.get("node"), what=f"bindings.{what}.node", workflow=workflow),
        input=_require_str(raw.get("input"), what=f"bindings.{what}.input", workflow=workflow),
    )


def _parse_workflow(name: str, body: Dict[str, Any], base_dir: Path) -> Workflow:
    description = _require_str(body.get("description"), what="description", workflow=name)
    expect = _require_str(body.get("expect"), what="expect", workflow=name)
    model = _require_str(body.get("model"), what="model", workflow=name)
    graph = _require_str(body.get("graph"), what="graph", workflow=name)
    model_released = _require_date(body.get("model_released"), what="model_released", workflow=name)
    added = _require_date(body.get("added"), what="added", workflow=name)

    raw_timeout = body.get("timeout_s")
    try:
        timeout_s = float(raw_timeout)
    except (TypeError, ValueError):
        raise WorkflowRegistryError(
            f"workflow {name!r}: timeout_s must be a positive number (got {raw_timeout!r})"
        ) from None
    if timeout_s <= 0:
        raise WorkflowRegistryError(f"workflow {name!r}: timeout_s must be a positive number")

    graph_path = Path(graph)
    if not graph_path.is_absolute():
        graph_path = base_dir / graph_path
    if not graph_path.exists():
        raise WorkflowRegistryError(f"workflow {name!r}: graph file does not exist: {graph_path}")
    try:
        parsed_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkflowRegistryError(
            f"workflow {name!r}: failed to parse graph {graph_path}: {exc!r}"
        ) from exc
    if not isinstance(parsed_graph, dict) or not parsed_graph:
        raise WorkflowRegistryError(
            f"workflow {name!r}: graph {graph_path} must be a non-empty API-format object"
        )

    bindings = body.get("bindings")
    if not isinstance(bindings, dict):
        raise WorkflowRegistryError(f"workflow {name!r}: 'bindings' must be a mapping")

    prompt = _parse_binding(bindings.get("prompt"), what="prompt", workflow=name)
    negative_prompt = (
        _parse_binding(bindings.get("negative_prompt"), what="negative_prompt", workflow=name)
        if bindings.get("negative_prompt") is not None
        else None
    )
    seed = (
        _parse_binding(bindings.get("seed"), what="seed", workflow=name)
        if bindings.get("seed") is not None
        else None
    )

    raw_output = bindings.get("output")
    if not isinstance(raw_output, dict):
        raise WorkflowRegistryError(f"workflow {name!r}: bindings.output must be a mapping")
    output = OutputBinding(
        node=_require_str(raw_output.get("node"), what="bindings.output.node", workflow=name),
        input=str(raw_output.get("input") or "filename_prefix").strip() or "filename_prefix",
    )

    raw_images = bindings.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise WorkflowRegistryError(
            f"workflow {name!r}: bindings.images must be a non-empty list (slot 0 is required)"
        )
    images: list[ImageSlot] = []
    for idx, raw_slot in enumerate(raw_images):
        if not isinstance(raw_slot, dict):
            raise WorkflowRegistryError(
                f"workflow {name!r}: bindings.images[{idx}] must be a mapping"
            )
        raw_unlink = raw_slot.get("unlink") or []
        if not isinstance(raw_unlink, list):
            raise WorkflowRegistryError(
                f"workflow {name!r}: bindings.images[{idx}].unlink must be a list"
            )
        images.append(
            ImageSlot(
                node=_require_str(
                    raw_slot.get("node"), what=f"bindings.images[{idx}].node", workflow=name
                ),
                input=_require_str(
                    raw_slot.get("input"), what=f"bindings.images[{idx}].input", workflow=name
                ),
                unlink=tuple(
                    _require_str(u, what=f"bindings.images[{idx}].unlink[]", workflow=name)
                    for u in raw_unlink
                ),
            )
        )

    raw_overrides = body.get("overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise WorkflowRegistryError(f"workflow {name!r}: 'overrides' must be a mapping")
    overrides: Dict[str, Mapping[str, Any]] = {}
    for node_id, node_overrides in raw_overrides.items():
        if not isinstance(node_overrides, dict) or not node_overrides:
            raise WorkflowRegistryError(
                f"workflow {name!r}: overrides[{node_id!r}] must be a non-empty mapping "
                f"of input -> value"
            )
        overrides[str(node_id)] = MappingProxyType(dict(node_overrides))

    workflow = Workflow(
        name=name,
        description=description,
        expect=expect,
        model=model,
        model_released=model_released,
        added=added,
        graph=graph,
        graph_path=graph_path,
        timeout_s=timeout_s,
        prompt=prompt,
        output=output,
        images=tuple(images),
        negative_prompt=negative_prompt,
        seed=seed,
        overrides=MappingProxyType(overrides),
        required_models=_required_models(parsed_graph),
    )
    _validate_against_graph(workflow, parsed_graph)
    return workflow


def _node_inputs(graph: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        raise WorkflowRegistryError(f"node {node_id!r} does not exist in the graph")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise WorkflowRegistryError(f"node {node_id!r} has no 'inputs' mapping in the graph")
    return inputs


def _validate_against_graph(workflow: Workflow, graph: Dict[str, Any]) -> None:
    """Every node id and input name the workflow names must exist in the graph."""
    prefix = f"workflow {workflow.name!r} ({workflow.graph})"

    def _check_input(node_id: str, input_name: str, what: str) -> None:
        try:
            inputs = _node_inputs(graph, node_id)
        except WorkflowRegistryError as exc:
            raise WorkflowRegistryError(f"{prefix}: {what}: {exc}") from None
        if input_name not in inputs:
            raise WorkflowRegistryError(
                f"{prefix}: {what}: node {node_id!r} has no input {input_name!r} "
                f"(has: {sorted(inputs)})"
            )

    _check_input(workflow.prompt.node, workflow.prompt.input, "bindings.prompt")
    if workflow.negative_prompt is not None:
        _check_input(
            workflow.negative_prompt.node, workflow.negative_prompt.input, "bindings.negative_prompt"
        )
    if workflow.seed is not None:
        _check_input(workflow.seed.node, workflow.seed.input, "bindings.seed")
    _check_input(workflow.output.node, workflow.output.input, "bindings.output")

    seen_slot_nodes: set[str] = set()
    for idx, slot in enumerate(workflow.images):
        _check_input(slot.node, slot.input, f"bindings.images[{idx}]")
        if slot.node in seen_slot_nodes:
            raise WorkflowRegistryError(
                f"{prefix}: bindings.images[{idx}]: node {slot.node!r} is bound to more than one slot"
            )
        seen_slot_nodes.add(slot.node)
        for target in slot.unlink:
            node_id, sep, input_name = str(target).partition(".")
            if not sep or not node_id or not input_name:
                raise WorkflowRegistryError(
                    f"{prefix}: bindings.images[{idx}].unlink: {target!r} must be '<node>.<input>'"
                )
            _check_input(node_id, input_name, f"bindings.images[{idx}].unlink")
            linked = _node_inputs(graph, node_id)[input_name]
            if not (isinstance(linked, list) and linked and str(linked[0]) == slot.node):
                raise WorkflowRegistryError(
                    f"{prefix}: bindings.images[{idx}].unlink: {target!r} does not link to "
                    f"slot node {slot.node!r} (links to {linked!r})"
                )

    for node_id, node_overrides in workflow.overrides.items():
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
