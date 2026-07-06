"""httpx-based client mirroring the in-process MemoryClient surface."""

from __future__ import annotations

from typing import Any, Literal

import httpx


class MemoryHttpClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8001",
        token: str | None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def search(self, query: str, k: int | None = None, wings_hint: list[str] | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query}
        if k is not None:
            payload["k"] = k
        if wings_hint is not None:
            payload["wings_hint"] = wings_hint
        response = self._client.post("/v1/memory/search", json=payload, headers=self._headers())
        response.raise_for_status()
        return list(response.json()["hits"])

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/memory/{memory_id}", headers=self._headers())
        response.raise_for_status()
        return dict(response.json())

    def search_memories(
        self, query: str, k: int = 5, wings_hint: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self.search(query, k=k, wings_hint=wings_hint)

    def approvals_pending(self, limit: int = 20, *, approval_token: str) -> dict[str, Any]:
        response = self._client.get(
            "/v1/approvals/pending",
            params={"limit": limit},
            headers=self._headers(approval_token=approval_token),
        )
        response.raise_for_status()
        return dict(response.json())

    def approvals_count(self) -> dict[str, Any]:
        response = self._client.get("/v1/approvals/count", headers=self._headers())
        response.raise_for_status()
        return dict(response.json())

    def approve(self, approval_id: int, *, approval_token: str) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/approvals/{approval_id}/approve",
            headers=self._headers(approval_token=approval_token),
        )
        response.raise_for_status()
        return dict(response.json())

    def reject(self, approval_id: int, *, approval_token: str) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/approvals/{approval_id}/reject",
            headers=self._headers(approval_token=approval_token),
        )
        response.raise_for_status()
        return dict(response.json())

    def write(self, content: str, room: str, memory_type: str, **metadata: Any) -> dict[str, Any]:
        response = self._client.post(
            "/v1/memory/write",
            json={"content": content, "room": room, "memory_type": memory_type, "metadata": metadata},
            headers=self._headers(),
        )
        response.raise_for_status()
        return dict(response.json())

    def tombstone(self, drawer_id: str, reason: str) -> bool:
        return self.delete(drawer_id, reason, mode="soft")["deleted"]

    def hard_delete(self, drawer_id: str, reason: str) -> bool:
        return self.delete(drawer_id, reason, mode="hard")["deleted"]

    def delete(self, drawer_id: str, reason: str, *, mode: Literal["soft", "hard"] = "soft") -> dict[str, Any]:
        response = self._client.post(
            f"/v1/memory/delete?mode={mode}",
            json={"drawer_id": drawer_id, "reason": reason},
            headers=self._headers(),
        )
        response.raise_for_status()
        return dict(response.json())

    def break_glass(self, reason: str, ttl_seconds: int = 600) -> dict[str, Any]:
        response = self._client.post(
            "/v1/principal/break-glass",
            json={"reason": reason, "ttl_seconds": ttl_seconds},
            headers=self._headers(),
        )
        response.raise_for_status()
        return dict(response.json())

    def audit(self, *, tenant: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if tenant is not None:
            params["tenant"] = tenant
        response = self._client.get("/v1/memory/audit", params=params, headers=self._headers())
        response.raise_for_status()
        return list(response.json()["entries"])

    def audit_tail(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.audit(limit=limit)

    def stats(self) -> dict[str, Any]:
        response = self._client.get("/v1/memory/stats", headers=self._headers())
        response.raise_for_status()
        return dict(response.json())

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self, *, approval_token: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if approval_token is not None:
            headers["X-Approval-Token"] = approval_token
        return headers
