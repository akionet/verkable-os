"""Vault file/HTTP source with bounded reads and private ETag/TTL caching."""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from http import HTTPStatus
from typing import Any, NoReturn, Protocol
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from navigator_orchestrator.observe.models import JsonObject, ObserveFault, ReportIndexEntry
from navigator_orchestrator.observe.tenancy import SourceConfig

REPORT_PATH = re.compile(r"(\d{4})/(\d{2})/(\d{2})/(\d{4})Z\.json\Z")
MAX_BYTES = 16 * 1024 * 1024
MAX_CACHE = 256


def reject_non_json_number(_value: str) -> NoReturn:
    raise ValueError("Non-finite numbers are not JSON")


class ReportSource(Protocol):
    async def list_index(self) -> list[ReportIndexEntry]: ...
    async def get_report(self, path: str) -> JsonObject: ...
    async def get_state(self) -> JsonObject: ...


class VaultReportSource:
    def __init__(
        self,
        config: SourceConfig,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._root = (config.path / config.source).resolve() if config.path else None
        if (
            config.path is not None
            and self._root is not None
            and not self._root.is_relative_to(config.path.resolve())
        ):
            raise ValueError("source escapes vault root")
        self._client = client or (
            httpx.AsyncClient(timeout=15, follow_redirects=False) if config.github_repo else None
        )
        self._owned_client = client is None
        self._token = token
        self._cache: OrderedDict[str, tuple[float, bytes, str | None]] = OrderedDict()

    async def aclose(self) -> None:
        if self._client is not None and self._owned_client:
            await self._client.aclose()

    async def _read(self, relative: str) -> bytes:
        if self._root is not None:
            path = (self._root / relative).resolve()
            if not path.is_relative_to(self._root):
                raise ObserveFault("invalid_source")
            try:
                with path.open("rb") as stream:
                    body = stream.read(MAX_BYTES + 1)
            except OSError as exc:
                raise ObserveFault("source_unavailable") from exc
            if len(body) > MAX_BYTES:
                raise ObserveFault("source_too_large")
            return body
        return await self._remote_read(relative)

    async def _remote_read(self, relative: str) -> bytes:
        cached = self._cache.get(relative)
        if cached is not None and time.monotonic() < cached[0]:
            self._cache.move_to_end(relative)
            return cached[1]
        headers = {"Accept": "application/vnd.github.raw+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if cached is not None and cached[2]:
            headers["If-None-Match"] = cached[2]
        path = quote(self.config.source + "/" + relative, safe="/")
        url = f"https://api.github.com/repos/{self.config.github_repo}/contents/{path}"
        assert self._client is not None
        try:
            async with self._client.stream(
                "GET", url, headers=headers, params={"ref": self.config.ref}
            ) as response:
                if response.status_code == HTTPStatus.NOT_MODIFIED and cached is not None:
                    body, etag = cached[1], cached[2]
                else:
                    response.raise_for_status()
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise ObserveFault("source_too_large")
                    etag = response.headers.get("etag")
        except httpx.HTTPError as exc:
            raise ObserveFault("source_unavailable") from exc
        self._cache[relative] = (time.monotonic() + self.config.ttl_seconds, body, etag)
        self._cache.move_to_end(relative)
        while len(self._cache) > MAX_CACHE:
            self._cache.popitem(last=False)
        return body

    def _relative(self, path: str) -> str:
        prefix = self.config.source + "/"
        relative = path.removeprefix(prefix)
        if not path.startswith(prefix) or not REPORT_PATH.fullmatch(relative):
            raise ObserveFault("invalid_source")
        return relative

    async def _json(self, relative: str) -> JsonObject:
        try:
            result: Any = json.loads(
                await self._read(relative), parse_constant=reject_non_json_number
            )
        except (ValueError, UnicodeError) as exc:
            raise ObserveFault("invalid_source") from exc
        if not isinstance(result, dict):
            raise ObserveFault("invalid_source")
        return result

    async def get_state(self) -> JsonObject:
        return await self._json("state.json")

    async def get_report(self, path: str) -> JsonObject:
        return await self._json(self._relative(path))

    async def list_index(self) -> list[ReportIndexEntry]:
        try:
            result = []
            for line in (await self._read("index.jsonl")).splitlines():
                if not line.strip():
                    continue
                row = json.loads(line, parse_constant=reject_non_json_number)
                match = REPORT_PATH.fullmatch(self._relative(row["path"]))
                assert match is not None
                year, month, day, minute = match.groups()
                entry = ReportIndexEntry.model_validate(
                    {**row, "id": f"{year}-{month}-{day}T{minute}Z"}
                )
                if entry.window.from_ > entry.window.to:
                    raise ObserveFault("invalid_source")
                result.append(entry)
            if len({e.id for e in result}) != len(result):
                raise ObserveFault("invalid_source")
            return sorted(result, key=lambda e: e.id, reverse=True)
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            raise ObserveFault("invalid_source") from exc
