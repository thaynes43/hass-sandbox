"""Expand schedule tasks into concrete calendar event instances."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from calendar_from_schedule_app.schedule_parser import Schedule, ScheduleTask


@dataclass
class EventInstance:
    task_id: str
    title: str
    start_date: date
    end_date: date  # exclusive end for all-day events
    description: str
    severity: str
    sync_key: str


def _add_months(d: date, months: int) -> date:
    """Add calendar months, clamping to last day of target month."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar

    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, max_day))


def _build_description(
    task: ScheduleTask,
    procedures: Dict[str, Any],
    *,
    window_before: int = 0,
    window_after: int = 0,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
) -> str:
    """Combine severity, window info, checklist, and resolved procedure steps."""
    parts: List[str] = []
    parts.append(f"Severity: {task.severity}")

    # Include window info in description so it's visible on the single-day event
    if window_start and window_end:
        parts.append(f"Window: {window_start.isoformat()} to {window_end.isoformat()}")
    elif window_before or window_after:
        parts.append(f"Window: {window_before} days before, {window_after} days after due date")

    if task.checklist:
        parts.append("")
        parts.append("Checklist:")
        for item in task.checklist:
            parts.append(f"  - {item}")

    if task.procedure_ref and task.procedure_ref in procedures:
        proc = procedures[task.procedure_ref]
        parts.append("")
        parts.append(f"Procedure: {proc.get('title', task.procedure_ref)}")
        for step in proc.get("steps", []):
            parts.append(f"  - {step}")

    return "\n".join(parts)


def _generate_recurring_days(
    task: ScheduleTask,
    schedule: Schedule,
    horizon_end: date,
) -> List[EventInstance]:
    if not task.frequency_days or not task.first_due:
        return []

    events = []
    due = task.first_due
    before = task.window.before_days if task.window else 0
    after = task.window.after_days if task.window else 0
    description = _build_description(
        task, schedule.procedures,
        window_before=before, window_after=after,
    )

    while due <= horizon_end:
        if due >= schedule.plan_start:
            sync_key = f"{task.id}_{due.isoformat()}"
            events.append(EventInstance(
                task_id=task.id,
                title=task.title,
                start_date=due,
                end_date=due + timedelta(days=1),  # single-day event
                description=description,
                severity=task.severity,
                sync_key=sync_key,
            ))
        due += timedelta(days=task.frequency_days)

    return events


def _generate_recurring_months(
    task: ScheduleTask,
    schedule: Schedule,
    horizon_end: date,
) -> List[EventInstance]:
    if not task.frequency_months or not task.first_due:
        return []

    events = []
    due = task.first_due
    before = task.window.before_days if task.window else 0
    after = task.window.after_days if task.window else 0
    description = _build_description(
        task, schedule.procedures,
        window_before=before, window_after=after,
    )
    iteration = 0

    while due <= horizon_end:
        if due >= schedule.plan_start:
            sync_key = f"{task.id}_{due.isoformat()}"
            events.append(EventInstance(
                task_id=task.id,
                title=task.title,
                start_date=due,
                end_date=due + timedelta(days=1),  # single-day event
                description=description,
                severity=task.severity,
                sync_key=sync_key,
            ))
        iteration += 1
        due = _add_months(task.first_due, iteration * task.frequency_months)

    return events


def _generate_fixed_date(
    task: ScheduleTask,
    schedule: Schedule,
    horizon_end: date,
) -> List[EventInstance]:
    if not task.due_date:
        return []
    if task.due_date > horizon_end:
        return []
    if task.due_date < schedule.plan_start:
        return []

    if task.window and task.window.start and task.window.end:
        description = _build_description(
            task, schedule.procedures,
            window_start=task.window.start, window_end=task.window.end,
        )
    else:
        before = task.window.before_days if task.window else 0
        after = task.window.after_days if task.window else 0
        description = _build_description(
            task, schedule.procedures,
            window_before=before, window_after=after,
        )

    sync_key = f"{task.id}_{task.due_date.isoformat()}"
    return [EventInstance(
        task_id=task.id,
        title=task.title,
        start_date=task.due_date,
        end_date=task.due_date + timedelta(days=1),  # single-day event
        description=description,
        severity=task.severity,
        sync_key=sync_key,
    )]


def _generate_consumable_trigger(
    task: ScheduleTask,
    schedule: Schedule,
    horizon_end: date,
    all_events: List[EventInstance],
) -> List[EventInstance]:
    """Generate order events when inventory depletes below threshold.

    Counts how many replacement events for the linked item occur within
    the horizon, tracks inventory depletion, and creates order events
    when remaining inventory hits the reorder threshold.
    """
    if not task.threshold:
        return []

    item_key = task.threshold.item
    consumable_cfg = schedule.consumables.get(item_key, {})
    if not consumable_cfg:
        return []

    pack_size = consumable_cfg.get("pack_size", 1)
    starting_packs = consumable_cfg.get("starting_inventory_packs", 0)
    inventory = starting_packs * pack_size
    threshold_remaining = task.threshold.remaining_filters_lte

    # Find the replacement task that consumes this item.
    # Match by checking if the item key (or its singular) appears in the task id
    # along with "replace".
    replacement_task_id = None
    item_stem = item_key.rstrip("s")  # "filters" -> "filter"
    for t in schedule.tasks:
        if t.type != "trigger" and "replace" in t.id:
            if item_key in t.id or item_stem in t.id:
                replacement_task_id = t.id
                break

    if not replacement_task_id:
        return []

    # Collect replacement event dates (sorted)
    replacement_dates = sorted(
        e.start_date for e in all_events if e.task_id == replacement_task_id
    )

    events = []
    description = _build_description(task, schedule.procedures)
    ordered = False

    for replace_date in replacement_dates:
        inventory -= 1
        if inventory <= threshold_remaining and not ordered:
            order_date = replace_date - timedelta(days=14)
            if order_date < schedule.plan_start:
                order_date = schedule.plan_start
            sync_key = f"{task.id}_{order_date.isoformat()}"
            events.append(EventInstance(
                task_id=task.id,
                title=task.title,
                start_date=order_date,
                end_date=order_date + timedelta(days=1),  # single-day event
                description=description,
                severity=task.severity,
                sync_key=sync_key,
            ))
            ordered = True
            inventory += pack_size

    return events


def generate_events(schedule: Schedule, horizon_end: date) -> List[EventInstance]:
    """Generate all event instances from a schedule within the given horizon."""
    events: List[EventInstance] = []
    trigger_tasks: List[ScheduleTask] = []

    for task in schedule.tasks:
        if task.type == "recurring_days":
            events.extend(_generate_recurring_days(task, schedule, horizon_end))
        elif task.type == "recurring_months":
            events.extend(_generate_recurring_months(task, schedule, horizon_end))
        elif task.type == "fixed_date":
            events.extend(_generate_fixed_date(task, schedule, horizon_end))
        elif task.type == "trigger":
            trigger_tasks.append(task)

    # Process trigger tasks after all others so they can reference replacement events
    for task in trigger_tasks:
        events.extend(_generate_consumable_trigger(task, schedule, horizon_end, events))

    return events
