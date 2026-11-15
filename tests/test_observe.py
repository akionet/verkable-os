"""OBS-001 B1: owner boundaries, report validation and read-only contracts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from navigator_orchestrator.api.app import build_app
from navigator_orchestrator.config import Settings
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.observe.models import ObserveFault
from navigator_orchestrator.observe.openapi import document
from navigator_orchestrator.observe.routes import create_router, fault_response
from navigator_orchestrator.observe.sources import VaultReportSource
from navigator_orchestrator.observe.tenancy import ConfigTenantAccess, ObserveConfig, SourceConfig
from navigator_orchestrator.store import Principal

PREFIX = "tenants/cuisinesavvy/automation-reports"
FIXTURES = Path(__file__).parent / "fixtures/observe"
BASE = "/v1/orgs/akionet/spaces/cuisinesavvy"
READER = Principal(subject="reader", issuer="tests", scopes=frozenset({"observe:read"}))


def make_client(root: Path, principal: Principal = READER) -> TestClient:
    config = ObserveConfig.model_validate(
        {
            "orgs": {
                "akionet": {
                    "allow": [{"issuer": "tests", "subject": "reader"}],
                    "spaces": {
                        "cuisinesavvy": {
                            "path": str(root),
                            "source": PREFIX,
                            "issues_repo": "akionet/vault",
                        }
                    },
                }
            }
        }
    )
    app = FastAPI()
    app.state.context = SimpleNamespace(principal_provider=lambda _request: principal)
    app.add_exception_handler(ObserveFault, fault_response)
    source = VaultReportSource(config.orgs["akionet"].spaces["cuisinesavvy"])
    app.include_router(
        create_router(ConfigTenantAccess(config), {("akionet", "cuisinesavvy"): source})
    )
    return TestClient(app)


@pytest.fixture
def report_root(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURES, tmp_path, dirs_exist_ok=True)
    return tmp_path


def entries(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / PREFIX / "index.jsonl").read_text().splitlines()]


def test_reports_filter_page_trend_and_etag(report_root: Path) -> None:
    """@OBS-001-B1-01 newest-first index, stable cursor and conditional reads."""
    with make_client(report_root) as client:
        assert client.get("/v1/orgs").json()["items"][0]["id"] == "akionet"
        assert client.get("/v1/orgs/akionet/spaces").json()["items"][0]["latest_report_at"]
        first = client.get(BASE + "/reports", params={"limit": 2})
        assert first.status_code == 200
        page = first.json()
        assert len(page["items"]) == 2
        second = client.get(BASE + "/reports", params={"cursor": page["next_cursor"]}).json()
        assert len(second["items"]) == 2
        assert not ({e["id"] for e in page["items"]} & {e["id"] for e in second["items"]})
        assert (
            client.get(
                BASE + "/reports",
                headers={"If-None-Match": client.get(BASE + "/reports").headers["etag"]},
            ).status_code
            == 304
        )
        assert first.headers["cache-control"].startswith("private")
        latest = page["items"][0]
        report = client.get(BASE + "/reports/" + latest["id"])
        assert (
            report.status_code == 200 and report.json()["schema"] == "verkable.automation-report/1"
        )
        filtered = client.get(BASE + "/reports", params={"from": latest["window"]["from"]}).json()
        assert len(filtered["items"]) == 1
        trend = client.get(BASE + "/trends", params={"field": "headline.published"}).json()
        assert len(trend["items"]) == 4
        assert trend["items"][0]["at"] < trend["items"][-1]["at"]
        assert client.get(BASE + "/trends", params={"field": "anything.secret"}).status_code == 422
        assert client.get(BASE + "/reports", params={"cursor": "garbage"}).status_code == 422


def test_invalid_report_never_enters_response(report_root: Path) -> None:
    """@OBS-001-B1-02 a schema-invalid report becomes a typed error."""
    row = entries(report_root)[-1]
    (report_root / row["path"]).write_text('{"secret": "do not return me"}')
    with make_client(report_root) as client:
        report_id = client.get(BASE + "/reports").json()["items"][0]["id"]
        result = client.get(BASE + "/reports/" + report_id)
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "invalid_report"
        assert "secret" not in result.text


@pytest.mark.parametrize(
    "principal",
    [
        Principal(subject="stranger", issuer="tests", scopes=frozenset({"observe:read"})),
        Principal(subject="reader", issuer="tests"),
    ],
)
def test_forbidden_org_is_indistinguishable_from_unknown(
    report_root: Path, principal: Principal
) -> None:
    """@OBS-001-B1-03 no existence leak on any org path."""
    with make_client(report_root, principal) as client:
        assert client.get("/v1/orgs").json() == {"items": []}
        for suffix in [
            "/spaces",
            "/spaces/cuisinesavvy/reports",
            "/spaces/cuisinesavvy/reports/2026-11-15T0012Z",
            "/spaces/cuisinesavvy/pending",
            "/spaces/cuisinesavvy/trends",
        ]:
            assert client.get("/v1/orgs/akionet" + suffix).status_code == 404
            assert client.get("/v1/orgs/unknown" + suffix).status_code == 404


def test_pending_checkpoint_replay(report_root: Path) -> None:
    """@OBS-001-B1-04 apply following diffs without losing unchanged gates."""
    state_file = report_root / PREFIX / "state.json"
    checkpoint = json.loads(state_file.read_text())
    rows = entries(report_root)
    report = json.loads((report_root / rows[-1]["path"]).read_text())
    pending = checkpoint["pending_human"]
    resolved_ids = {gate["issue"] for gate in report["pending_human"]["resolved"]}
    for gate in report["pending_human"]["resolved"]:
        pending[str(gate["issue"])] = {
            "issue": gate["issue"],
            "run": gate["run"],
            "workflow": gate["workflow"],
            "title": gate.get("title") or "Previously waiting",
            "reason": "Previously waiting",
            "waiting_since": report["window"]["from"],
        }
    checkpoint.update(to=report["window"]["from"], last_report=report["previous"])
    state_file.write_text(json.dumps(checkpoint))
    (report_root / rows[-1]["path"]).write_text(json.dumps(report))
    with make_client(report_root) as client:
        result = client.get(BASE + "/pending")
        assert result.status_code == 200, result.text
        items = result.json()["items"]
        assert len(items) == report["pending_human"]["total"]
        assert all(item["issue"] not in resolved_ids for item in items)
        assert all(
            item["age_hours"] >= 0
            and item["issue_url"].startswith("https://github.com/akionet/vault/issues/")
            for item in items
        )


def test_observe_has_no_write_routes(report_root: Path) -> None:
    """@OBS-001-B1-05 every observe route is read-only."""
    with make_client(report_root) as client:
        paths = client.app.openapi()["paths"]
        operations = [
            set(methods) for path, methods in paths.items() if path.startswith("/v1/orgs")
        ]
        assert len(operations) == 6
        assert all(methods <= {"get", "head"} for methods in operations)
        for method in ["POST", "PATCH", "PUT", "DELETE"]:
            assert client.request(method, BASE + "/reports").status_code == 405


@pytest.mark.asyncio
async def test_github_conditional_cache_and_private_errors() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-only"
        if len(requests) == 1:
            return httpx.Response(200, content=b"{}", headers={"ETag": '"v1"'})
        if len(requests) == 2:
            assert request.headers["If-None-Match"] == '"v1"'
            return httpx.Response(304)
        return httpx.Response(403, text="fixture-only private upstream details")

    fixture_token = "fixture-only"  # noqa: S105 - inert MockTransport credential
    config = SourceConfig(source=PREFIX, github_repo="example/reports", ttl_seconds=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = VaultReportSource(config, token=fixture_token, client=client)
        assert await source.get_state() == {}
        assert await source.get_state() == {}
        with pytest.raises(ObserveFault) as exc:
            await source.get_state()
        assert str(exc.value) == "source_unavailable"
        assert "fixture-only" not in str(exc.value)


@pytest.mark.asyncio
async def test_github_ttl_and_path_boundary() -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"{}")

    config = SourceConfig(source=PREFIX, github_repo="example/reports", ttl_seconds=60)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = VaultReportSource(config, client=client)
        await source.get_state()
        await source.get_state()
        assert len(seen) == 1
        with pytest.raises(ObserveFault):
            await source.get_report(PREFIX + "/../../secret.json")
        assert len(seen) == 1


def test_bad_checkpoint_and_invalid_index_fail_closed(report_root: Path) -> None:
    state_file = report_root / PREFIX / "state.json"
    state = json.loads(state_file.read_text())
    state["pending_human"] = {}
    state_file.write_text(json.dumps(state))
    with make_client(report_root) as client:
        response = client.get(BASE + "/pending")
        assert (
            response.status_code == 502 and response.json()["error"]["code"] == "invalid_checkpoint"
        )
    row = entries(report_root)[0]
    row["path"] = PREFIX + "/../../secrets.json"
    (report_root / PREFIX / "index.jsonl").write_text(json.dumps(row))
    with make_client(report_root) as client:
        assert client.get(BASE + "/reports").status_code == 502


def test_denial_precedes_source_reads_and_query_validation(report_root: Path) -> None:
    (report_root / PREFIX / "index.jsonl").unlink()
    principal = Principal(subject="forbidden", scopes=frozenset({"observe:read"}))
    with make_client(report_root, principal) as client:
        assert client.get(BASE + "/reports?limit=0").status_code == 404
        assert client.get(BASE + "/trends?from=not-a-date").status_code == 404


def test_composition_opt_in_and_header_principal_scope(report_root: Path) -> None:

    settings = Settings(
        _env_file=None,
        model="fake:test",
        require_principal=True,
        principal_header="x-test-principal",
        principal_scopes=frozenset({"observe:read"}),
    )
    ordinary = build_app(settings, llm=FakeChatModel())
    assert "/v1/orgs" not in ordinary.openapi()["paths"]
    manifest = report_root / "navigator-orchestrator.toml"
    manifest.write_text("""[observe.orgs.akionet]
allow = [{issuer = "header", subject = "reader"}]
[observe.orgs.akionet.spaces.cuisinesavvy]
path = "."
source = "tenants/cuisinesavvy/automation-reports"
issues_repo = "akionet/vault"
""")
    settings.observe_manifest = manifest
    with TestClient(build_app(settings, llm=FakeChatModel())) as client:
        assert client.get(BASE + "/reports").status_code == 404
        response = client.get(BASE + "/reports", headers={"x-test-principal": "reader"})
        assert response.status_code == 200 and len(response.json()["items"]) == 4


def test_openapi_matches_generated_contract() -> None:

    root = Path(__file__).parents[1]
    assert (root / "docs/openapi/observe.v1.json").read_text(encoding="utf-8") == document()
