from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from fastapi.testclient import TestClient

from panella.client import MemoryClient
from panella.cli import main
from panella.cli import _http as cli_http
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.http.app import create_app
from panella.http.config import MemoryHttpConfig
from panella.http_client import MemoryHttpClient
from panella.principal import principal_default_for_profile, root_principal
from panella.profile import AgentProfile

APPROVAL_TOKEN = "operator-secret"


class RecordingAdapter:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add_memory(self, wing, room, content, metadata, conversation_id=None):
        mid = f"mem-{len(self.rows) + 1}"
        self.rows.append(
            {
                "id": mid,
                "content": content,
                "wing": wing,
                "room": room,
                "tenant_id": metadata.get("tenant_id"),
                "metadata": metadata,
                "score": 1.0,
                "tags": ["status:active"],
            }
        )
        return mid

    def search_memories(self, query, k=5, wings_hint=None, retrieval_mode=None, tenant_ids=None):
        hits = [row for row in self.rows if query.lower() in str(row["content"]).lower()]
        if tenant_ids is not None and "*" not in tenant_ids:
            hits = [row for row in hits if row.get("tenant_id") in set(tenant_ids)]
        return hits[:k]

    def get_drawer(self, drawer_id: str):
        for row in self.rows:
            if row.get("id") == drawer_id:
                return row
        return None

    def aggregate_stats(self, wing_filter=None, tenant_ids=None):
        rooms_by_wing: dict[str, defaultdict[str, int]] = {}
        for row in self.rows:
            if tenant_ids is not None and "*" not in tenant_ids and row.get("tenant_id") not in set(tenant_ids):
                continue
            wing = str(row.get("wing") or "")
            if wing_filter is not None and wing != wing_filter:
                continue
            room = str(row.get("room") or "")
            rooms_by_wing.setdefault(wing, defaultdict(int))[room] += 1
        wing_breakdown = [
            {
                "wing": wing,
                "drawer_count": sum(rooms.values()),
                "rooms": dict(rooms),
                "most_recent_write_ts": None,
            }
            for wing, rooms in sorted(rooms_by_wing.items())
        ]
        return {
            "total_drawers": sum(row["drawer_count"] for row in wing_breakdown),
            "wing_breakdown": wing_breakdown,
            "last_synced_ts": None,
        }

    def find_active_hash_by_marker(self, marker, tenant_id):
        return None


def test_operator_cli_full_governance_loop(tmp_path, monkeypatch, capsys):
    env = _build(tmp_path, monkeypatch)

    with _cli_http_from_test_client(env.app, monkeypatch) as base_url:
        assert main(["approvals", "list", "--token", env.bearer, "--base-url", base_url]) == 0
        captured = capsys.readouterr()
        assert "Panella keeps governed memories" in captured.out
        # PR2 display parity: the DEFAULT table shows who proposed (the seed client's profile).
        assert "mcp-write" in captured.out
        assert APPROVAL_TOKEN not in captured.out + captured.err

        assert (
            main([
                "approvals",
                "approve",
                str(env.approval_id),
                "--token",
                env.bearer,
                "--base-url",
                base_url,
            ])
            == 0
        )
        captured = capsys.readouterr()
        assert "approved" in captured.out
        assert APPROVAL_TOKEN not in captured.out + captured.err
        memory_id = env.adapter.rows[-1]["id"]

        assert (
            main([
                "memories",
                "search",
                "governed memories",
                "--k",
                "5",
                "--token",
                env.bearer,
                "--base-url",
                base_url,
            ])
            == 0
        )
        captured = capsys.readouterr()
        assert memory_id in captured.out
        assert "Panella keeps governed memories" in captured.out

        assert (
            main(["memories", "show", memory_id, "--json", "--token", env.bearer, "--base-url", base_url])
            == 0
        )
        captured = capsys.readouterr()
        shown = json.loads(captured.out)
        assert shown["id"] == memory_id
        assert "governed memories" in shown["content"]

        assert main(["audit", "tail", "--limit", "20", "--token", env.bearer, "--base-url", base_url]) == 0
        captured = capsys.readouterr()
        # Audit-invariant vocabulary: the fail-closed pre-decision record + the finalize outcome.
        assert "approval_decision" in captured.out
        assert "approval_finalized" in captured.out

        assert main(["stats", "--json", "--token", env.bearer, "--base-url", base_url]) == 0
        captured = capsys.readouterr()
        stats = json.loads(captured.out)
        assert stats["total_drawers"] == 1
        assert stats["wing_breakdown"]


def test_operator_cli_negative_matrix(tmp_path, monkeypatch, capsys):
    env = _build(tmp_path, monkeypatch)
    wrong_token_file = tmp_path / ".panella" / "wrong-approval-token"
    wrong_token_file.write_text("wrong-token", encoding="utf-8")
    wrong_token_file.chmod(0o600)
    missing_token_file = tmp_path / ".panella" / "missing-approval-token"
    env.adapter.rows.append(
        {
            "id": "foreign-1",
            "content": "foreign tenant content must not leak",
            "wing": "owner",
            "room": "preferences",
            "tenant_id": "t_foreign_personal",
            "metadata": {"tenant_id": "t_foreign_personal"},
            "score": 1.0,
            "tags": ["status:active"],
        }
    )

    with _cli_http_from_test_client(env.app, monkeypatch) as base_url:
        assert (
            main([
                "approvals",
                "list",
                "--approval-token-file",
                str(missing_token_file),
                "--token",
                env.bearer,
                "--base-url",
                base_url,
            ])
            == 2
        )
        captured = capsys.readouterr()
        assert "approval token file not found" in captured.err
        assert "Traceback" not in captured.err
        assert APPROVAL_TOKEN not in captured.out + captured.err

        assert (
            main([
                "approvals",
                "list",
                "--approval-token-file",
                str(wrong_token_file),
                "--token",
                env.bearer,
                "--base-url",
                base_url,
            ])
            == 1
        )
        captured = capsys.readouterr()
        assert "approval refused" in captured.err
        assert "wrong-token" not in captured.out + captured.err
        assert APPROVAL_TOKEN not in captured.out + captured.err

        monkeypatch.delenv("PANELLA_BEARER", raising=False)
        assert main(["memories", "search", "governed", "--base-url", base_url]) == 1
        captured = capsys.readouterr()
        assert "missing bearer token" in captured.err
        assert APPROVAL_TOKEN not in captured.out + captured.err

        assert main(["memories", "show", "foreign-1", "--token", env.bearer, "--base-url", base_url]) == 1
        captured = capsys.readouterr()
        assert "memory not found" in captured.err
        assert "foreign tenant content" not in captured.out + captured.err
        assert APPROVAL_TOKEN not in captured.out + captured.err


def test_get_memory_route_bearer_required_audited_and_tenant_isolated(tmp_path, monkeypatch):
    env = _build(tmp_path, monkeypatch, seed_candidate=False)
    env.adapter.rows.extend(
        [
            {
                "id": "mem-owner",
                "content": "owner visible content",
                "wing": "owner",
                "room": "preferences",
                "tenant_id": "t_owner_personal",
                "metadata": {"tenant_id": "t_owner_personal"},
                "score": 1.0,
                "tags": ["status:active"],
            },
            {
                "id": "mem-foreign",
                "content": "foreign invisible content",
                "wing": "owner",
                "room": "preferences",
                "tenant_id": "t_foreign_personal",
                "metadata": {"tenant_id": "t_foreign_personal"},
                "score": 1.0,
                "tags": ["status:active"],
            },
        ]
    )
    headers = {"Authorization": f"Bearer {env.bearer}"}

    with TestClient(env.app) as client:
        no_bearer = client.get("/v1/memory/mem-owner")
        ok = client.get("/v1/memory/mem-owner", headers=headers)
        foreign = client.get("/v1/memory/mem-foreign", headers=headers)

    assert no_bearer.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["content"] == "owner visible content"
    assert foreign.status_code == 404
    assert "foreign invisible content" not in foreign.text
    with sqlite3.connect(env.config.audit_db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op='memory_get' AND target_id='mem-owner'"
        ).fetchone()
    assert count >= 1


def test_static_memory_routes_win_over_dynamic_id_route(tmp_path, monkeypatch):
    # LOAD-BEARING INVARIANT (code-reviewer B2b P3): GET /v1/memory/{memory_id} must be registered
    # AFTER the static /v1/memory/stats and /v1/memory/audit routers, or a real path like
    # /v1/memory/stats would be captured by the {memory_id} param and return a memory-not-found 404
    # instead of the stats/audit payload. Lock the ordering so a future include_router reorder can't
    # silently regress it.
    env = _build(tmp_path, monkeypatch, seed_candidate=False)
    headers = {"Authorization": f"Bearer {env.bearer}"}
    with TestClient(env.app) as client:
        stats = client.get("/v1/memory/stats", headers=headers)
        audit = client.get("/v1/memory/audit", headers=headers)
    # The static handlers answer (200) — NOT the dynamic {memory_id} route (which would 404 with the
    # memory_not_found code for the non-existent id "stats"/"audit").
    assert stats.status_code == 200
    assert "pending" not in stats.json() or stats.json().get("error", {}).get("code") != "memory_not_found"
    assert audit.status_code == 200
    assert audit.json().get("error", {}).get("code") != "memory_not_found"


def test_get_memory_route_serving_gated(tmp_path, monkeypatch):
    _write_overlay(tmp_path, monkeypatch)
    config = MemoryHttpConfig(
        token_db_path=tmp_path / "tokens.db",
        audit_db_path=tmp_path / "audit.db",
        outbox_db_path=tmp_path / "outbox.db",
        profile_name="serving",
        store_path=tmp_path / "missing" / "sqlite_vec.db",
    )
    app = create_app(config, memory_adapter=RecordingAdapter())
    bearer = app.state.token_store.mint(principal_id=root_principal().id, label="owner")

    with TestClient(app) as client:
        response = client.get("/v1/memory/anything", headers={"Authorization": f"Bearer {bearer}"})

    assert response.status_code == 503
    assert response.json()["code"] == "memory_not_serving"


def _build(tmp_path: Path, monkeypatch, *, seed_candidate: bool = True):
    monkeypatch.chdir(tmp_path)
    _write_overlay(tmp_path, monkeypatch)
    store_path = tmp_path / "sqlite_vec.db"
    _write_serving_store(store_path)

    adapter = RecordingAdapter()
    config = MemoryHttpConfig(
        token_db_path=tmp_path / "tokens.db",
        audit_db_path=tmp_path / "audit.db",
        outbox_db_path=tmp_path / "outbox.db",
        profile_name="serving",
        store_path=store_path,
    )
    app = create_app(config, memory_adapter=adapter)
    bearer = app.state.token_store.mint(principal_id=root_principal().id, label="test-bearer")

    approval_id = None
    if seed_candidate:
        write_profile = AgentProfile.load("mcp-write")
        seed_client = MemoryClient(
            write_profile,
            principal_default_for_profile(write_profile),
            adapter=adapter,
            outbox_db_path=config.outbox_db_path,
            audit_db_path=config.audit_db_path,
        )
        result = seed_client.write(
            "Panella keeps governed memories.",
            room="preferences",
            memory_type="owner_preference",
        )
        assert result.queued_for_approval is True
        approval_id = result.approval_id

    return SimpleNamespace(app=app, bearer=bearer, approval_id=approval_id, adapter=adapter, config=config)


def _write_overlay(tmp_path: Path, monkeypatch) -> None:
    token_dir = tmp_path / ".panella"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / "approval-token"
    token_file.write_text(APPROVAL_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    overlay = token_dir / "governance.yaml"
    overlay.write_text(
        "approval:\n"
        '  authorized_approvers: ["local_cli:owner"]\n'
        "  transport:\n"
        '    kind: "local_cli"\n'
        "    config:\n"
        f'      token_file: "{token_file}"\n'
        '      token_mode: "0600"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))
    config_dir = tmp_path / "dist-config"
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(config_dir))
    reset_governance_cache()
    render_distribution_config(current_governance(), config_dir)


def _write_serving_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, tags TEXT, metadata TEXT, deleted_at TEXT)")
    conn.execute(
        "INSERT INTO memories VALUES ('seed','seed','status:active,tenant:t_owner_personal','{}',NULL)"
    )
    conn.commit()
    conn.close()


@contextmanager
def _cli_http_from_test_client(app, monkeypatch) -> Iterator[str]:
    with TestClient(app, base_url="http://127.0.0.1") as test_client:

        def make_client(args):
            return MemoryHttpClient(
                base_url=cli_http.base_url(args),
                token=cli_http.bearer(args),
                client=test_client,
            )

        monkeypatch.setattr(cli_http, "make_client", make_client)
        yield "http://127.0.0.1"
