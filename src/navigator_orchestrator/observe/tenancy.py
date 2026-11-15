"""Configuration authorization seam; no workflow-project SDK import."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from navigator_orchestrator.observe.models import ObserveFault
from navigator_orchestrator.store import Principal

SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    path: Path | None = None
    github_repo: str | None = None
    ref: str = "main"
    token_env: str | None = None
    issues_repo: str | None = None
    ttl_seconds: float = Field(default=30, ge=0, le=3600)

    @model_validator(mode="after")
    def check_source(self) -> SourceConfig:
        if (self.path is None) == (self.github_repo is None):
            raise ValueError("provide exactly one of path and github_repo")
        if (
            not self.source
            or any(p in {"", ".", ".."} for p in self.source.split("/"))
            or "\\" in self.source
        ):
            raise ValueError("source must be a vault-relative POSIX path")
        for repo in (self.github_repo, self.issues_repo):
            if repo is not None and not REPO.fullmatch(repo):
                raise ValueError("repository must be owner/name")
        if self.token_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.token_env
        ):
            raise ValueError("token_env names a variable, not a token")
        return self


class AllowedPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issuer: str
    subject: str


class OrgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: list[AllowedPrincipal]
    spaces: dict[str, SourceConfig]


class ObserveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orgs: dict[str, OrgConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_names(self) -> ObserveConfig:
        for org, config in self.orgs.items():
            if not SLUG.fullmatch(org) or any(not SLUG.fullmatch(s) for s in config.spaces):
                raise ValueError("org and space names must be slugs")
        return self

    @classmethod
    def from_manifest(cls, path: Path) -> ObserveConfig | None:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("observe")
        if raw is None:
            return None
        config = cls.model_validate(raw)
        for org in config.orgs.values():
            for source in org.spaces.values():
                if source.path is not None:
                    source.path = (path.parent / source.path).resolve()
        return config


class TenantAccess(Protocol):
    def orgs(self, principal: Principal) -> list[str]: ...
    def spaces(self, principal: Principal, org: str) -> dict[str, SourceConfig]: ...


class ConfigTenantAccess:
    def __init__(self, config: ObserveConfig) -> None:
        self.config = config

    def orgs(self, principal: Principal) -> list[str]:
        if "observe:read" not in principal.scopes:
            return []
        return sorted(
            org
            for org, config in self.config.orgs.items()
            if any(
                p.issuer == principal.issuer and p.subject == principal.subject
                for p in config.allow
            )
        )

    def spaces(self, principal: Principal, org: str) -> dict[str, SourceConfig]:
        if org not in self.orgs(principal):
            raise ObserveFault("not_found", 404)
        return self.config.orgs[org].spaces
