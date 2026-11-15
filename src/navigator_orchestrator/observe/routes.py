"""The six read-only Observe routes. Authorize before touching a source."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime

from navigator_orchestrator.observe.models import (
    ErrorDetail,
    ObserveError,
    ObserveFault,
    OrgRef,
    Orgs,
    Pending,
    Reports,
    SpaceRef,
    Spaces,
    TrendPoint,
    Trends,
)
from navigator_orchestrator.observe.pending import SCHEMA, pending_items, validated_report
from navigator_orchestrator.observe.sources import ReportSource
from navigator_orchestrator.observe.tenancy import SourceConfig, TenantAccess
from navigator_orchestrator.store import Principal

REPORT_ID = re.compile(r"\d{4}-\d{2}-\d{2}T\d{4}Z\Z")
ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ObserveError} for status in (404, 422, 502)
}
Start = Annotated[AwareDatetime | None, Query(alias="from")]
End = Annotated[AwareDatetime | None, Query(alias="to")]


class ObserveRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def checked(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                raise ObserveFault("invalid_query", 422) from exc

        return checked


async def fault_response(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ObserveFault)
    messages = {"not_found": "Not found.", "invalid_report": "The source report failed validation."}
    body = ObserveError(
        error=ErrorDetail(
            code=error.code, message=messages.get(error.code, "Observe data is unavailable.")
        )
    )
    return JSONResponse(
        status_code=error.status,
        content=body.model_dump(),
        headers={"Cache-Control": "private, no-store"},
    )


def response_for(request: Request, payload: Any) -> Response:
    content = (
        payload.model_dump(mode="json", by_alias=True)
        if hasattr(payload, "model_dump")
        else payload
    )
    etag = (
        '"'
        + hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        + '"'
    )
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=30",
        "Vary": "Cookie, Authorization",
    }
    candidates = [
        tag.strip().removeprefix("W/")
        for tag in request.headers.get("if-none-match", "").split(",")
    ]
    if etag in candidates or "*" in candidates:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=content, headers=headers)


def bounds(start: datetime | None, end: datetime | None) -> None:
    if start is not None and end is not None and start > end:
        raise ObserveFault("invalid_window", 422)


def observe_principal(request: Request) -> Principal:
    # Use the same injected PrincipalProvider seam as api/authz.py, without
    # importing api/__init__ (which eagerly imports workflow composition).
    context = getattr(request.app.state, "context", None)
    principal = context.principal_provider(request) if context is not None else None
    return principal or Principal(subject="anonymous")


ObservePrincipal = Annotated[Principal, Depends(observe_principal)]


def organization_guard(access: TenantAccess) -> Callable[[Request, Principal], None]:
    def authorize_path(request: Request, principal: ObservePrincipal) -> None:
        org = request.path_params.get("org")
        if org is not None:
            configs = access.spaces(principal, org)
            space = request.path_params.get("space")
            if space is not None and space not in configs:
                raise ObserveFault("not_found", 404)

    return authorize_path


def create_router(
    access: TenantAccess, sources: Mapping[tuple[str, str], ReportSource]
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/orgs",
        tags=["observe"],
        responses=ERRORS,
        dependencies=[Depends(organization_guard(access))],
        route_class=ObserveRoute,
    )

    def scoped(principal: Principal, org: str, space: str) -> tuple[SourceConfig, ReportSource]:
        configs = access.spaces(principal, org)
        if space not in configs:
            raise ObserveFault("not_found", 404)
        return configs[space], sources[(org, space)]

    @router.get("", response_model=Orgs, operation_id="observe_orgs")
    async def orgs(request: Request, principal: ObservePrincipal) -> Response:
        return response_for(request, Orgs(items=[OrgRef(id=org) for org in access.orgs(principal)]))

    @router.get("/{org}/spaces", response_model=Spaces, operation_id="observe_spaces")
    async def spaces(request: Request, org: str, principal: ObservePrincipal) -> Response:
        configs = access.spaces(principal, org)
        items = []
        for name, config in sorted(configs.items()):
            index = await sources[(org, name)].list_index()
            items.append(
                SpaceRef(
                    id=name,
                    source=config.source,
                    latest_report_at=index[0].window.to if index else None,
                )
            )
        return response_for(request, Spaces(items=items))

    @router.get(
        "/{org}/spaces/{space}/reports", response_model=Reports, operation_id="observe_reports"
    )
    async def reports(
        *,
        request: Request,
        org: str,
        space: str,
        principal: ObservePrincipal,
        from_: Start = None,
        to: End = None,
        limit: int = Query(25, ge=1, le=100),
        cursor: str | None = Query(None, max_length=128),
    ) -> Response:
        _, source = scoped(principal, org, space)
        bounds(from_, to)
        after = None
        if cursor is not None:
            try:
                after = base64.b64decode(cursor, altchars=b"-_", validate=True).decode("ascii")
                if not REPORT_ID.fullmatch(after):
                    raise ValueError
            except (ValueError, UnicodeError) as exc:
                raise ObserveFault("invalid_cursor", 422) from exc
        index = await source.list_index()
        filtered = [
            e
            for e in index
            if (after is None or e.id < after)
            and (from_ is None or e.window.to > from_)
            and (to is None or e.window.from_ <= to)
        ]
        page = filtered[:limit]
        next_cursor = (
            base64.urlsafe_b64encode(page[-1].id.encode()).decode()
            if len(filtered) > limit
            else None
        )
        return response_for(request, Reports(items=page, next_cursor=next_cursor))

    @router.get(
        "/{org}/spaces/{space}/reports/{report_id}",
        response_model=dict[str, Any],
        operation_id="observe_report",
    )
    async def report(
        request: Request,
        org: str,
        space: str,
        report_id: str,
        principal: ObservePrincipal,
    ) -> Response:
        _, source = scoped(principal, org, space)
        if not REPORT_ID.fullmatch(report_id):
            raise ObserveFault("not_found", 404)
        entry = next((e for e in await source.list_index() if e.id == report_id), None)
        if entry is None:
            raise ObserveFault("not_found", 404)
        return response_for(request, await validated_report(source, entry, space))

    @router.get(
        "/{org}/spaces/{space}/pending", response_model=Pending, operation_id="observe_pending"
    )
    async def pending(
        request: Request, org: str, space: str, principal: ObservePrincipal
    ) -> Response:
        config, source = scoped(principal, org, space)
        result = await pending_items(
            source, await source.list_index(), space, config.issues_repo or config.github_repo
        )
        # Ages change with the clock; current pending must be revalidated each time.
        response = response_for(request, result)
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    @router.get(
        "/{org}/spaces/{space}/trends", response_model=Trends, operation_id="observe_trends"
    )
    async def trends(
        *,
        request: Request,
        org: str,
        space: str,
        principal: ObservePrincipal,
        field: str = Query("headline.published", max_length=128),
        from_: Start = None,
        to: End = None,
    ) -> Response:
        _, source = scoped(principal, org, space)
        bounds(from_, to)
        index = await source.list_index()
        key = field.removeprefix("headline.")
        if not re.fullmatch(r"headline\.[A-Za-z_][A-Za-z0-9_]*", field) or (
            key not in SCHEMA["properties"]["headline"]["properties"]
            and not any(key in e.headline for e in index)
        ):
            raise ObserveFault("invalid_trend_field", 422)
        points = [
            TrendPoint(report_id=e.id, at=e.window.to, value=e.headline.get(key))
            for e in reversed(index)
            if (from_ is None or e.window.to > from_) and (to is None or e.window.from_ <= to)
        ]
        return response_for(request, Trends(field=field, items=points))

    return router
