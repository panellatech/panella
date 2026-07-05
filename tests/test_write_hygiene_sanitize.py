from __future__ import annotations

from panella.sanitize import sanitize
from panella.write_hygiene import sanitize_network_write_metadata, strip_reserved_tags


def test_network_write_hygiene_strips_server_authoritative_keys():
    clean = sanitize_network_write_metadata({
        "source_system": "forged",
        "principal_id": "forged",
        "subject_id": "forged",
        "tags": ["ok", "ccsk:forged"],
        "note": "kept",
    })
    assert clean == {"tags": ["ok"], "note": "kept"}
    assert strip_reserved_tags("not-list") == "not-list"


def test_sanitize_redacts_secret_shapes():
    text = sanitize("api_key=abc123 PANELLA_API_KEY=secret token: bearer")
    assert "abc123" not in text
    assert "secret" not in text
