"""Contract lock for the mcp-memory-service 10.67.1 memory_type ontology coercion.

10.67.1 validates ``MemoryCreateRequest.memory_type`` against a built-in ontology and silently
coerces anything outside it (e.g. Panella's governed ``{owner_slug}_preference`` / ``_feedback``) to
``observation`` in the store's top-level field. This was verified BENIGN — the facade never relies on
that field: the write-allowlist finalizer reads memory_type from METADATA, and the real semantic type
survives uncoerced in both the metadata and the ``mtype:`` tag. These tests lock that survival so a
future upstream/refactor that ALSO strips those reliable carriers is caught, not shipped silently.
"""

from __future__ import annotations

from panella.panella_adapter import PanellaAdapter


def _coerced_owner_preference_hit() -> dict:
    """A hit exactly as a 10.67.1 store returns it for a governed ``owner_preference`` write: the
    top-level ``memory_type`` is coerced to ``observation``, but the metadata and the ``mtype:`` tag
    still carry the real type (metadata/tags are not ontology-validated)."""
    return {
        "content_hash": "hash-abc",
        "content": "team shared memory",
        "memory_type": "observation",  # <- coerced by 10.67.1's ontology validation
        "metadata": {
            "memory_type": "owner_preference",  # <- survives (metadata is not validated)
            "wing": "owner",
            "room": "preferences",
            "tenant_id": "t_owner_personal",
        },
        "tags": [
            "mtype:owner_preference",  # <- survives (tags are not validated)
            "wing:owner",
            "room:preferences",
            "tenant:t_owner_personal",
            "status:active",
        ],
    }


def test_memtype_coercion_survives_in_reliable_channels():
    """After 10.67.1 coerces the store's top-level memory_type to 'observation', the real semantic
    type MUST still be recoverable from the metadata and the mtype: tag in the normalized hit."""
    adapter = object.__new__(PanellaAdapter)
    hit = adapter._normalize_hit(_coerced_owner_preference_hit())

    # The two reliable, un-coerced carriers still hold the real type:
    assert hit["metadata"]["memory_type"] == "owner_preference"
    assert "mtype:owner_preference" in hit["tags"]

    # Documented current behavior: the top-level field reflects the store's coercion. This is the
    # long-standing contract (10.31.2 coerced identically); the governed write path never trusts it.
    assert hit["memory_type"] == "observation"


def test_finalizer_reads_memory_type_from_metadata_not_the_coerced_field():
    """The governance gate (write-allowlist) reads memory_type from metadata, so a custom governed
    type is allowlist-checked against its REAL value — the store's top-level coercion can neither
    smuggle a denied type through nor spuriously deny an allowed one."""
    import inspect

    from panella import approval_finalizer

    src = inspect.getsource(approval_finalizer)
    # The allowlist check reads from metadata, never from a top-level (coercible) memory_type field.
    assert 'built["metadata"].get("memory_type")' in src
    assert "profile.memory_type_allowlist" in src
