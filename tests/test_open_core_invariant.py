from __future__ import annotations

from pathlib import Path
import re

import yaml


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


def test_compose_arbitrary_uid_defaults_are_non_root_and_hardened():
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    expected_user = "${PANELLA_UID:-10001}:${PANELLA_GID:-0}"

    for service_name in ("panella", "panella-http"):
        service = compose["services"][service_name]
        assert service["user"] == expected_user
        assert service["group_add"] == ["0"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        # An arbitrary PANELLA_UID is not in the image passwd, so `~` cannot resolve — HOME must be
        # pinned or the ONNX cache/config paths fall back to an unwritable / (GH-bot P1).
        assert service["environment"]["HOME"] == "/home/panella"

    # The store owns the ONNX embedding cache; XDG_CACHE_HOME nails the mounted volume for any
    # XDG-aware library that resolves cache paths without consulting HOME.
    assert compose["services"]["panella"]["environment"]["XDG_CACHE_HOME"] == "/home/panella/.cache"

    assert expected_user.replace("${PANELLA_UID:-10001}", "10001").replace("${PANELLA_GID:-0}", "0") == "10001:0"


def _dockerfile_stages(dockerfile: str) -> dict[str, list[tuple[str, str]]]:
    """Parse logical Dockerfile instructions, grouped by their named build stage."""
    logical_lines: list[str] = []
    pending = ""
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending = f"{pending} {line[:-1].rstrip()}".strip()
            continue
        logical_lines.append(f"{pending} {line}".strip())
        pending = ""
    assert not pending, "Dockerfile must not end with a continued instruction"

    stages: dict[str, list[tuple[str, str]]] = {}
    current_stage: str | None = None
    for line in logical_lines:
        match = re.match(r"(?P<instruction>[A-Z]+)\s+(?P<argument>.*)", line, re.IGNORECASE)
        assert match, f"unparseable Dockerfile instruction: {line!r}"
        instruction = match["instruction"].upper()
        argument = match["argument"].strip()
        if instruction == "FROM":
            stage_match = re.search(r"\s+AS\s+(?P<stage>\S+)$", argument, re.IGNORECASE)
            assert stage_match, f"final image stage must be named: {line!r}"
            current_stage = stage_match["stage"]
            stages[current_stage] = []
            continue
        assert current_stage is not None, f"instruction before first FROM: {line!r}"
        stages[current_stage].append((instruction, argument))
    return stages


def test_container_images_preserve_non_root_group_zero_writable_paths_and_hardening():
    root = Path(__file__).resolve().parents[1]
    stages = _dockerfile_stages((root / "Dockerfile").read_text())
    expected_paths = {
        "store": "/data /home/panella/.cache",
        "app": "/app/data /app/dist-config",
    }
    suid_strip = "find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true"
    # CI-only derived stage (never pushed or signed): deterministic hash-fallback
    # injection for the airgap boot gates. It is exempt from the single-USER shape
    # (root sandwich for pip uninstall) but must still END as uid 10001 and must not
    # redefine any runtime configuration it inherits from the store stage.
    ci_only_stage = "store-hash-fallback-test"

    assert stages.keys() == expected_paths.keys() | {ci_only_stage}
    ci_instructions = stages[ci_only_stage]
    assert all(
        instruction in {"USER", "RUN"} for instruction, _ in ci_instructions
    ), f"{ci_only_stage} may only sandwich RUNs between USER switches"
    ci_users = [argument for instruction, argument in ci_instructions if instruction == "USER"]
    assert ci_users and ci_users[-1] == "10001", f"{ci_only_stage} must end as uid 10001"
    for stage_name, paths in expected_paths.items():
        instructions = stages[stage_name]
        user_indexes = [index for index, (instruction, _) in enumerate(instructions) if instruction == "USER"]
        assert len(user_indexes) == 1, f"{stage_name} must set exactly one runtime USER"
        user_index = user_indexes[0]
        assert instructions[user_index] == ("USER", "10001")
        assert all(
            instruction not in {"HEALTHCHECK", "ENTRYPOINT", "CMD"}
            for instruction, _ in instructions[:user_index]
        ), f"{stage_name} must set USER before runtime configuration"

        group_writable_indexes = [
            index
            for index, (instruction, argument) in enumerate(instructions)
            if instruction == "RUN"
            and f"chgrp -R 0 {paths}" in argument
            and f"chmod -R g=rwX {paths}" in argument
        ]
        assert len(group_writable_indexes) == 1, f"{stage_name} must retain group-0 g=rwX paths"
        assert group_writable_indexes[0] < user_index

        suid_strip_indexes = [
            index
            for index, (instruction, argument) in enumerate(instructions)
            if instruction == "RUN" and suid_strip in argument
        ]
        assert len(suid_strip_indexes) == 1, f"{stage_name} must strip inherited SUID/SGID bits"
        assert suid_strip_indexes[0] < user_index
