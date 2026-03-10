"""Parse a generic YAML maintenance schedule into typed dataclasses."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class TaskWindow:
    before_days: int = 0
    after_days: int = 0
    start: Optional[date] = None
    end: Optional[date] = None


@dataclass
class ConsumableConfig:
    item: str
    remaining_filters_lte: int


@dataclass
class ScheduleTask:
    id: str
    title: str
    type: str  # recurring_days, recurring_months, fixed_date, trigger
    severity: str = "MED"
    checklist: List[str] = field(default_factory=list)
    procedure_ref: Optional[str] = None
    # recurring_days
    frequency_days: Optional[int] = None
    first_due: Optional[date] = None
    # recurring_months
    frequency_months: Optional[int] = None
    # fixed_date
    due_date: Optional[date] = None
    # trigger (consumable)
    trigger: Optional[str] = None
    threshold: Optional[ConsumableConfig] = None
    # window
    window: Optional[TaskWindow] = None
    escalate_after_days: Optional[int] = None
    escalate_after: Optional[date] = None


@dataclass
class Schedule:
    name: str
    plan_start: date
    plan_end: Optional[date]
    tasks: List[ScheduleTask]
    procedures: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    consumables: Dict[str, Any] = field(default_factory=dict)


def _parse_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    return date.fromisoformat(str(val))


def _parse_window(raw: Any) -> Optional[TaskWindow]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return TaskWindow(
            before_days=raw.get("before_days", 0),
            after_days=raw.get("after_days", 0),
            start=_parse_date(raw.get("start")),
            end=_parse_date(raw.get("end")),
        )
    return None


def _infer_type(raw: Dict[str, Any]) -> str:
    if raw.get("trigger"):
        return "trigger"
    if raw.get("due_date"):
        return "fixed_date"
    if raw.get("frequency_months"):
        return "recurring_months"
    return "recurring_days"


def _parse_task(raw: Dict[str, Any]) -> ScheduleTask:
    task_type = raw.get("type") or _infer_type(raw)
    threshold = None
    if raw.get("threshold"):
        t = raw["threshold"]
        threshold = ConsumableConfig(
            item=t["item"],
            remaining_filters_lte=t["remaining_filters_lte"],
        )

    return ScheduleTask(
        id=raw["id"],
        title=raw["title"],
        type=task_type,
        severity=raw.get("severity", "MED"),
        checklist=raw.get("checklist", []),
        procedure_ref=raw.get("procedure_ref"),
        frequency_days=raw.get("frequency_days"),
        first_due=_parse_date(raw.get("first_due")),
        frequency_months=raw.get("frequency_months"),
        due_date=_parse_date(raw.get("due_date")),
        trigger=raw.get("trigger"),
        threshold=threshold,
        window=_parse_window(raw.get("window")),
        escalate_after_days=raw.get("escalate_after_days"),
        escalate_after=_parse_date(raw.get("escalate_after")),
    )


def load_schedule(file_path: str | Path) -> Schedule:
    """Load and validate a schedule YAML file."""
    path = Path(file_path)
    with open(path, "r") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError(f"Empty schedule file: {path}")

    schedule_meta = data.get("schedule", {})
    if not schedule_meta.get("name"):
        raise ValueError("Schedule must have a 'schedule.name' field")

    tasks_raw = data.get("tasks", [])
    if not tasks_raw:
        raise ValueError("Schedule must have at least one task")

    tasks = [_parse_task(t) for t in tasks_raw]

    return Schedule(
        name=schedule_meta["name"],
        plan_start=_parse_date(schedule_meta.get("plan_start")) or date.today(),
        plan_end=_parse_date(schedule_meta.get("plan_end")),
        tasks=tasks,
        procedures=data.get("procedures", {}),
        context=data.get("context", {}),
        consumables=data.get("consumables", {}),
    )


def compute_file_hash(file_path: str | Path) -> str:
    """SHA256 hex digest of the file contents."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
