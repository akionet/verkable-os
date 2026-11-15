"""OBS-001 B1 acceptance over real report fixtures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pytest_bdd import given, scenarios, then, when
from tests.test_observe import BASE, FIXTURES, PREFIX, entries, make_client

from navigator_orchestrator.store import Principal

scenarios("observe-reports.feature")


@given("real automation reports are configured for an authorized reader", target_fixture="observed")
def configured(tmp_path: Path) -> dict[str, Any]:
    shutil.copytree(FIXTURES, tmp_path, dirs_exist_ok=True)
    return {"root": tmp_path, "client": make_client(tmp_path)}


@when("the reader requests the report index")
def index(observed: dict[str, Any]) -> None:
    observed["response"] = observed["client"].get(BASE + "/reports")


@then("the four reports are returned newest first")
def newest_first(observed: dict[str, Any]) -> None:
    response = observed["response"]
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()["items"]]
    assert len(ids) == 4 and ids == sorted(ids, reverse=True)


@when("a report fails schema validation")
def invalid(observed: dict[str, Any]) -> None:
    root, client = observed["root"], observed["client"]
    (root / entries(root)[-1]["path"]).write_text(
        (FIXTURES / "invalid-report.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    report_id = client.get(BASE + "/reports").json()["items"][0]["id"]
    observed["response"] = client.get(BASE + "/reports/" + report_id)


@then("Observe returns a typed invalid-report error")
def invalid_response(observed: dict[str, Any]) -> None:
    assert observed["response"].status_code == 502
    assert observed["response"].json()["error"]["code"] == "invalid_report"


@when("a reader without organization access requests every Observe path")
def forbidden(observed: dict[str, Any]) -> None:
    client = make_client(observed["root"], Principal(subject="stranger"))
    suffixes = [
        "/spaces",
        "/spaces/cuisinesavvy/reports",
        "/spaces/cuisinesavvy/reports/missing",
        "/spaces/cuisinesavvy/pending",
        "/spaces/cuisinesavvy/trends",
    ]
    observed["statuses"] = [
        (
            client.get("/v1/orgs/akionet" + p).status_code,
            client.get("/v1/orgs/missing" + p).status_code,
        )
        for p in suffixes
    ]


@then("each organization path is indistinguishable from an unknown organization")
def hidden(observed: dict[str, Any]) -> None:
    assert all(actual == unknown == 404 for actual, unknown in observed["statuses"])


@when("the reader requests the current pending-human queue")
def pending(observed: dict[str, Any]) -> None:
    observed["response"] = observed["client"].get(BASE + "/pending")


@then("the queue contains the checkpoint gates with age and issue links")
def gates(observed: dict[str, Any]) -> None:
    expected = json.loads((observed["root"] / PREFIX / "state.json").read_text())["pending_human"]
    response = observed["response"]
    assert response.status_code == 200
    items = response.json()["items"]
    assert {row["issue"] for row in items} == {int(key) for key in expected}
    assert all(row["age_hours"] >= 0 and row["issue_url"] for row in items)


@when("the reader inspects the Observe routes")
def inspect_routes(observed: dict[str, Any]) -> None:
    observed["methods"] = [
        set(methods)
        for path, methods in observed["client"].app.openapi()["paths"].items()
        if path.startswith("/v1/orgs")
    ]


@then("every Observe route is GET or HEAD only")
def read_only(observed: dict[str, Any]) -> None:
    assert len(observed["methods"]) == 6
    assert all(methods <= {"get", "head"} for methods in observed["methods"])
