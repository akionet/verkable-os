"""Observe envelopes. Report bodies remain validated JSON, not workflow models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt


class ObserveFault(Exception):
    def __init__(self, code: str, status: int = 502) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ObserveError(BaseModel):
    error: ErrorDetail


class OrgRef(BaseModel):
    id: str


class Orgs(BaseModel):
    items: list[OrgRef]


class SpaceRef(BaseModel):
    id: str
    source: str
    latest_report_at: datetime | None


class Spaces(BaseModel):
    items: list[SpaceRef]


class Window(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime


class ReportIndexEntry(BaseModel):
    id: str
    path: str
    window: Window
    headline: dict[str, StrictInt]


class Reports(BaseModel):
    items: list[ReportIndexEntry]
    next_cursor: str | None


class Gate(BaseModel):
    issue: int = Field(gt=0)
    run: str
    workflow: str
    title: str
    reason: str
    waiting_since: AwareDatetime


class PendingItem(Gate):
    age_hours: float
    issue_url: str | None


class Pending(BaseModel):
    as_of: datetime
    items: list[PendingItem]


class TrendPoint(BaseModel):
    report_id: str
    at: datetime
    value: int | None


class Trends(BaseModel):
    field: str
    items: list[TrendPoint]


JsonObject = dict[str, Any]
