# Observe API (OBS-001 B1)

Observe reads automation reports; it does not run workflows or decide gates.
Routes are absent unless `build_app(observe=...)` receives an `ObserveConfig`,
or `NAVIGATOR_OBSERVE_MANIFEST` names a manifest containing `[observe]`.

```toml
[observe.orgs.example]
allow = [{issuer = "header", subject = "operator@example.test"}]

[observe.orgs.example.spaces.editorial]
path = "../vault"                         # relative to this manifest
source = "tenants/editorial/automation-reports"
issues_repo = "example/vault"
```

For GitHub, replace `path` with `github_repo = "example/vault"` and optionally
`token_env = "OBSERVE_VAULT_TOKEN"`, `ref = "main"`, `ttl_seconds = 30`.
The token value stays on the server. GitHub content requests use the raw media
type, conditional ETags and a bounded cache; no redirect is followed.
Local reads are uncached so a refreshed clone is immediately visible.

The existing `PrincipalProvider` seam supplies identity. Both `observe:read`
scope and an exact issuer/subject match in the org's `allow` list are required.
Unknown and forbidden orgs/spaces return the same 404, before source access or
query parsing. `/v1/orgs` lists only visible orgs.

For the existing trusted-header provider, configure
`NAVIGATOR_REQUIRE_PRINCIPAL=true` and
`NAVIGATOR_PRINCIPAL_SCOPES='["observe:read"]'` on the server. The header name
remains `NAVIGATOR_PRINCIPAL_HEADER`; its issuer is `header`. This provider
requires the existing trusted proxy that strips client-supplied identity
headers. Observe does not add signature verification or browser credentials.
The default anonymous/stub principal has no Observe access. Hosting and full
RBAC remain outside P0.

## Routes

All are GET under `/v1/orgs`; none accept writes.

| Path | Response |
| --- | --- |
| `/v1/orgs` | `{items: [{id}]}` |
| `/{org}/spaces` | `{items: [{id, source, latest_report_at}]}` |
| `/{org}/spaces/{space}/reports` | `{items, next_cursor}` |
| `/{org}/spaces/{space}/reports/{report_id}` | Validated v1 JSON report body |
| `/{org}/spaces/{space}/pending` | `{as_of, items}` with gate age and issue URL |
| `/{org}/spaces/{space}/trends` | `{field, items: [{report_id, at, value}]}` |

Report ids are `YYYY-MM-DDTHHMMZ`. Reports come from `index.jsonl`, newest
first. `limit` defaults to 25 (maximum 100); return `next_cursor` unchanged
to request the next page. Cursors are keyset-based so new reports do not shift
older pages. `from` and `to` must include a timezone and select overlapping
activity windows (`report.to > from`, `report.from <= to`). An inverted window
or invalid cursor returns 422.

Trends default to `field=headline.published`, return oldest first, and support
integer headline counters, including additive producer fields. A counter
missing from an older report becomes `null`, never an invented zero.

Every report body is validated with the vendored JSON Schema and its date-time
formats, space, window and headline are checked against the selected index row.
Invalid report bodies are errors, never partial data. JSON reads are bounded
to 16 MiB, reject non-finite numbers, and cannot escape the configured source
through index paths or symlinks. Source errors are redacted.

Pending starts with `state.json` and replays indexed reports after its `to`,
oldest first. The `previous` chain, window continuity and resulting counts must
agree. Gaps and corrupt checkpoints return an error rather than an incomplete
queue. If producer files change during a read, retry after the next refresh.
Pending includes all unchanged gates, not only the latest report's additions.

Responses carry an ETag and private cache policy. Conditional reads authorize
before returning 304. Pending is revalidated because gate ages change.
Errors use `{error: {code, message}}` (404, 422, 502), without upstream payloads,
credentials or server filesystem paths.

## Contract and validation

`make openapi` generates `docs/openapi/observe.v1.json` directly from the router.
`make check-openapi` and CI reject drift. The F1 frontend must generate its
client from that snapshot after B1 merges. The report body remains JSON,
validated at the source edge; its schema provenance is in
`src/navigator_orchestrator/observe/schema/SOURCE.md`.

`features/observe-reports.feature` covers B1-01 through B1-05. Unit tests also
cover pagination, conditional reads, invalid sources/checkpoints, GitHub
ETag/TTL behavior, source boundaries and opt-in composition. The runtime's
existing isolation tests include Observe and forbid workflow-definition
imports. Run `make check` for lint, strict typing, tests and snapshot drift.
