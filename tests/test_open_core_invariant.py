from __future__ import annotations

from pathlib import Path


def test_public_repo_product_only():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "tools").exists()
    assert not (root / "memory_gateway").exists()
    assert not (root / "panella" / "eval").exists()
    assert not any(root.glob("*COMPLETION-REPORT*"))
    http_routes = "".join(p.read_text() for p in (root / "panella" / "http" / "routes").glob("*.py"))
    assert "/v1/approvals/" not in http_routes


def test_real_box_assets_are_not_stubs():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    compose = (root / "docker-compose.yml").read_text()
    entrypoint = (root / "docker-entrypoint.sh").read_text()
    assert "mcp-memory-service[sqlite]==10.31.2" in dockerfile
    assert "cryptography<47" in dockerfile
    assert 'ENTRYPOINT ["docker-entrypoint.sh"]' in dockerfile
    assert "PANELLA_CONFIG_DIR=/app/dist-config" in dockerfile
    assert "PANELLA_HTTP_PROFILE=serving" in dockerfile
    assert "PANELLA_HTTP_HOST=0.0.0.0" in dockerfile
    assert "PANELLA_MCP_ENABLED=1" in dockerfile
    assert "condition: service_healthy" in compose
    assert ":/data:ro" in compose
    assert "panella-render-config --out" in entrypoint
