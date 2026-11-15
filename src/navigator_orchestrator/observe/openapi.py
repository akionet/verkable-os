"""Deterministic consumer contract, independent of engine/workflow registration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastapi import FastAPI

from navigator_orchestrator.observe.routes import create_router
from navigator_orchestrator.observe.tenancy import ConfigTenantAccess, ObserveConfig


def document() -> str:
    app = FastAPI(title="Verkable Observe", version="1.0.0")
    app.include_router(create_router(ConfigTenantAccess(ObserveConfig()), {}))
    return json.dumps(app.openapi(), sort_keys=True, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path("docs/openapi/observe.v1.json")
    generated = document()
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != generated:
            raise SystemExit("Observe OpenAPI snapshot is stale; run make openapi")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated, encoding="utf-8")


if __name__ == "__main__":
    main()
