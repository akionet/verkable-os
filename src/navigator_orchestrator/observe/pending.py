"""Validate report bodies and replay checkpoint-relative pending diffs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from navigator_orchestrator.observe.models import (
    Gate,
    ObserveFault,
    Pending,
    PendingItem,
    ReportIndexEntry,
    Window,
)
from navigator_orchestrator.observe.sources import ReportSource

SCHEMA = json.loads(
    (Path(__file__).parent / "schema/automation-report.v1.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


async def validated_report(
    source: ReportSource, entry: ReportIndexEntry, space: str
) -> dict[str, Any]:
    report = await source.get_report(entry.path)
    if not VALIDATOR.is_valid(report):
        raise ObserveFault("invalid_report")
    if (
        report["space"] != space
        or Window.model_validate(report["window"]) != entry.window
        or report["headline"] != entry.headline
    ):
        raise ObserveFault("invalid_report")
    return report


class Checkpoint(BaseModel):
    schema_version: Literal["verkable.automation-report/1"] = Field(alias="schema")
    last_report: str | None
    to: AwareDatetime
    pending_human: dict[str, Gate]


async def pending_items(
    source: ReportSource, entries: list[ReportIndexEntry], space: str, issues_repo: str | None
) -> Pending:
    try:
        checkpoint = Checkpoint.model_validate(await source.get_state())
        current = {g.issue: g for g in checkpoint.pending_human.values()}
        if len(current) != len(checkpoint.pending_human) or any(
            str(g.issue) != key for key, g in checkpoint.pending_human.items()
        ):
            raise ObserveFault("invalid_checkpoint")
        at, previous = checkpoint.to, checkpoint.last_report
        indexed_checkpoint = next((e for e in entries if e.path == previous), None)
        if indexed_checkpoint is not None and (
            indexed_checkpoint.window.to != at
            or indexed_checkpoint.headline.get("pending_human_total") != len(current)
        ):
            raise ObserveFault("invalid_checkpoint")
        for entry in reversed(entries):
            if entry.window.to <= checkpoint.to:
                continue
            report = await validated_report(source, entry, space)
            if report["previous"] != previous or entry.window.from_ != at:
                raise ObserveFault("pending_gap")
            for gate in report["pending_human"]["resolved"]:
                current.pop(gate["issue"], None)
            for gate in report["pending_human"]["added"]:
                parsed = Gate.model_validate(gate)
                current[parsed.issue] = parsed
            if len(current) != report["pending_human"]["total"]:
                raise ObserveFault("pending_gap")
            at, previous = entry.window.to, entry.path
        now = datetime.now(UTC)
        items = [
            PendingItem(
                **g.model_dump(),
                age_hours=max(0, (now - g.waiting_since).total_seconds() / 3600),
                issue_url=f"https://github.com/{issues_repo}/issues/{g.issue}"
                if issues_repo
                else None,
            )
            for g in sorted(current.values(), key=lambda g: (g.waiting_since, g.issue))
        ]
        return Pending(as_of=at, items=items)
    except ValidationError as exc:
        raise ObserveFault("invalid_checkpoint") from exc
