from __future__ import annotations

from pathlib import Path


def test_public_repo_product_only():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "tools").exists()
    assert not (root / "memory_gateway").exists()
    assert not (root / "panella" / "eval").exists()
    assert not any(root.glob("*COMPLETION-REPORT*"))
    # WP-B2a: the HTTP approval surface SHIPS — but ONLY through the shared approval trust chain
    # (panella.approval_service). This is a STRONGER invariant than the pre-B2a "no approvals
    # routes": the surface exists, yet the route module must never call the auth-less raw queue
    # mutators directly and must never let a caller assert their own approver identity.
    approvals = (root / "panella" / "http" / "routes" / "approvals.py").read_text()
    assert "approval_service" in approvals  # routes delegate to the shared, gated trust chain
    assert "X-Approval-Token" in approvals  # the local_cli token is header-only (never query/path)
    for raw in (
        "mcp_approve_or_redrive(",
        "update_approval_status(",
        "finalize_approved_candidate(",
        "list_pending_approvals(",
    ):
        assert raw not in approvals, f"approvals route must not call the auth-less raw helper {raw}"
    # The route must never accept/stamp an approver identity as a kwarg — it is derived only inside
    # the service from the verified transport (the docstring may NAME these fields; a `field=` assign
    # would be the real leak).
    assert "approved_by=" not in approvals
    assert "approved_via=" not in approvals


def test_real_box_assets_are_not_stubs():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    compose = (root / "docker-compose.yml").read_text()
    entrypoint = (root / "docker-entrypoint.sh").read_text()
    assert "mcp-memory-service[sqlite]==10.67.1" in dockerfile
    assert "cryptography<47" in dockerfile
    assert 'ENTRYPOINT ["docker-entrypoint.sh"]' in dockerfile
    assert "PANELLA_CONFIG_DIR=/app/dist-config" in dockerfile
    assert "PANELLA_HTTP_PROFILE=serving" in dockerfile
    assert "PANELLA_HTTP_HOST=0.0.0.0" in dockerfile
    assert "PANELLA_MCP_ENABLED=1" in dockerfile
    assert "condition: service_healthy" in compose
    assert ":/data:ro" in compose
    assert "panella-render-config --out" in entrypoint
