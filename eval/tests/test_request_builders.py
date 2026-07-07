"""Fixture-based tests for BOTH lane request-builders against the REAL schemas — the brief's
REQUIRED fallback when a docker box is unavailable. Validates the exact request bodies
`eval/longmemeval/ingest_retrieve.py` sends:

  store lane  -> the mcp-memory-service OpenAPI fixture `tests/fixtures/panella_openapi_v10.67.1.json`
                 (MemoryCreateRequest for ingest, SemanticSearchRequest for search)
  facade lane -> `panella/http/schemas.py` (SearchRequest / SearchResponse)

Does not require a running box: these tests build the request dict a real call WOULD send, and
assert its shape/fields against the pinned contracts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from panella.http.schemas import SearchRequest, SearchResponse

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENAPI_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "panella_openapi_v10.67.1.json"


@pytest.fixture(scope="module")
def store_openapi() -> dict:
    return json.loads(_OPENAPI_FIXTURE.read_text(encoding="utf-8"))


def _memory_create_request_schema(store_openapi: dict) -> dict:
    return store_openapi["components"]["schemas"]["MemoryCreateRequest"]


def _semantic_search_request_schema(store_openapi: dict) -> dict:
    return store_openapi["components"]["schemas"]["SemanticSearchRequest"]


def _assert_conforms(body: dict, schema: dict) -> None:
    """Minimal structural conformance: every key in `body` is a declared property, and every
    required property in `schema` is present in `body`."""
    props = schema.get("properties", {})
    unknown = set(body) - set(props)
    assert not unknown, f"body has keys not in the OpenAPI schema: {unknown}"
    for req in schema.get("required", []):
        assert req in body, f"body is missing required field {req!r}"


def test_store_ingest_request_conforms_to_memory_create_request(store_openapi: dict) -> None:
    """The exact body eval/longmemeval/ingest_retrieve.py `ingest()` POSTs to /api/memories."""
    body = {
        "content": "[Session s1 | date: 2024/01/01] user: hello",
        "tags": ["panella_eval", "status:active", "wing:owner", "room:preferences"],
        "memory_type": "observation",
        "metadata": {"wing": "owner", "room": "preferences", "session_id": "s1", "date": "2024/01/01", "qid": "q1"},
        "conversation_id": "q1::s1",
    }
    schema = _memory_create_request_schema(store_openapi)
    _assert_conforms(body, schema)


def test_store_search_request_conforms_to_semantic_search_request(store_openapi: dict) -> None:
    """The exact body eval/longmemeval/ingest_retrieve.py `search_store()` POSTs to /api/search."""
    body = {"query": "where does the user work?", "n_results": 10}
    schema = _semantic_search_request_schema(store_openapi)
    _assert_conforms(body, schema)
    # n_results must respect the store's own [1, 100] cap (SemanticSearchRequest.n_results).
    n_results_schema = schema["properties"]["n_results"]
    assert n_results_schema["minimum"] <= body["n_results"] <= n_results_schema["maximum"]


def test_facade_search_request_matches_pydantic_schema() -> None:
    """The exact body eval/longmemeval/ingest_retrieve.py `search_facade()` POSTs to
    /v1/memory/search — validated against the REAL pydantic model (panella/http/schemas.py), not a
    hand-copied shape. StrictModel(extra='forbid') means an extra/renamed field raises here."""
    body = {"query": "where does the user work?", "k": 10}
    validated = SearchRequest(**body)
    assert validated.query == body["query"]
    assert validated.k == body["k"]
    assert validated.wings_hint is None


def test_facade_search_request_rejects_unknown_fields() -> None:
    """StrictModel(extra='forbid') — a request-builder typo (e.g. a stray legacy field) must be
    caught here, not silently dropped or silently accepted by a real facade box."""
    with pytest.raises(ValidationError):
        SearchRequest(query="hi", n_results=10)  # n_results is NOT a facade field name (it's "k")


def test_facade_search_response_shape_matches_pydantic_schema() -> None:
    """The exact response shape eval/longmemeval/ingest_retrieve.py `search_facade()` parses:
    SearchResponse{hits: list[dict]}. Confirms the harness reads `r.get("hits", [])`, not a
    store-shaped `r.get("results", [])` (the two lanes have DIFFERENT response envelopes)."""
    payload = {
        "hits": [
            {"drawer_id": "abc", "content": "hello", "metadata": {"session_id": "s1"}, "score": 0.9},
        ]
    }
    validated = SearchResponse(**payload)
    assert len(validated.hits) == 1
    assert validated.hits[0]["metadata"]["session_id"] == "s1"
