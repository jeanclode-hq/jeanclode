"""Generate OpenAPI JSON from FastAPI routers.

Replicates WebServerPlugin._discover_routers() logic to build a minimal
FastAPI app with all routers, then dumps the OpenAPI schema to disk.
"""

import importlib
import json
import pkgutil
from pathlib import Path

from fastapi import FastAPI

import api.routers as routers_pkg


def build_app() -> FastAPI:
    """Build a minimal FastAPI app with all discovered routers."""
    app = FastAPI(
        title="Jeanclode API",
        description="Autonomous Sentry error triage and fix system",
        version="0.1.0",
    )

    for module_info in pkgutil.iter_modules(routers_pkg.__path__):
        if module_info.name.startswith("_") or module_info.name == "base_schema":
            continue
        try:
            module = importlib.import_module(f"api.routers.{module_info.name}")
            if hasattr(module, "router"):
                app.include_router(module.router)
        except Exception as exc:
            print(f"Warning: failed to load api.routers.{module_info.name}: {exc}")

    return app


def main() -> None:
    app = build_app()
    schema = app.openapi()
    out = Path(__file__).resolve().parent.parent / "packages" / "api-types" / "openapi.json"
    out.write_text(json.dumps(schema, indent=2))
    print(f"Wrote OpenAPI schema to {out}")


if __name__ == "__main__":
    main()
