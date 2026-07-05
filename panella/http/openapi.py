"""OpenAPI export helper for the memory HTTP facade."""

from __future__ import annotations

import json
from pathlib import Path

from panella.http.app import create_app

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = ROOT / "docs" / "panella-http-openapi.json"


def build_openapi() -> dict:
    return create_app().openapi()


def write_openapi(path: str | Path = OPENAPI_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


if __name__ == "__main__":
    print(write_openapi())
