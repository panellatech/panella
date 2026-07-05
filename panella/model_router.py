"""MemoryModelRouter — the production LLM binding for the memory Extract/Decide/Synth seams.

The memory extractor (``preference_extraction.extract_preferences(chat_fn=…)``) and same-slot judge
(``preference_decide.decide(judge_fn=…)``) take an INJECTED ``Callable[[str, str], str]`` transport.
The eval binds a codex/OpenAI transport; this module is the PRODUCTION binding for the self-host
product: a BYOK model router over the four providers Owner ratified — OpenAI, Anthropic, Gemini,
Ollama — with a provider fallback walk and an auth-mode-agnostic credential model.

Auth modes (decision #1):
  - ``api_key``       — the unconditional default for all four providers; the credential is read from
                        the ENV VAR NAME in ``credential_ref`` (BYOK; the box owner sets it).
  - ``subscription``  — the owner's own subscription / device-auth (e.g. ChatGPT via codex). ROUTED
                        but gated: the router refuses to construct with a subscription binding unless
                        ``PANELLA_SUBSCRIPTION_TOS_ACK=1``, and fails loud if the provider's API key env
                        is ALSO set (a key silently overrides an OAuth/subscription token — the
                        agent-sdk-subscription-auth-headless trap).
  - ``mcp_connector`` — the owner points the box at their own model endpoint. Accepted + routed;
                        the concrete transport is deferred (walks to fallback for now).

Reopenability (decision #7): ``role="judge"`` is reused by a future durable resolver — the seam is
not foreclosed.

Only the api_key transports ship a concrete implementation in P1 (the always-clean default that ships
regardless of the subscription ToS outcome). Subscription + mcp_connector are accepted + routed but
their concrete transports are DEFERRED (subscription needs the ToS pass + a hardened env-isolated
subprocess; mcp_connector needs the P3b SSE surface) — both walk to the api_key fallback until wired.

Testability (稳定系统): every api_key transport takes an INJECTABLE ``poster`` so request construction +
the fallback walk + auth gating are unit-proven WITHOUT a network call; the default ``poster`` is a
thin urllib POST (the proven eval pattern). Imports stay within ``panella`` + stdlib so the
module is fence-safe.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Literal

from panella.governance import Governance
from panella.preference_decide import JudgeFn
from panella.preference_extraction import ChatFn

AuthMode = Literal["api_key", "subscription", "mcp_connector"]
Provider = Literal["openai", "anthropic", "gemini", "ollama"]
Role = Literal["extract", "judge", "synth"]
TierName = Literal["T0", "T1", "T3"]

_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "gemini", "ollama"})
_AUTH_MODES: frozenset[str] = frozenset({"api_key", "subscription", "mcp_connector"})
_ROLES: frozenset[str] = frozenset({"extract", "judge", "synth"})
_TIER_NAMES: frozenset[str] = frozenset({"T0", "T1", "T3"})

SUBSCRIPTION_ACK_ENV = "PANELLA_SUBSCRIPTION_TOS_ACK"

# The env var each provider's API key is conventionally read from. A subscription binding is checked
# against this (in ADDITION to any explicit credential_ref) so a set API key can never silently
# coexist with — and override — a subscription/OAuth token, even when credential_ref is omitted.
_PROVIDER_DEFAULT_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}

# OpenAI-compatible chat-completions bases (a single client shape covers three providers; the box
# owner may override per binding via ``endpoint``). Anthropic uses its own messages API below.
_OPENAI_COMPAT_ENDPOINTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://localhost:11434/v1",
}
_ANTHROPIC_DEFAULT_ENDPOINT = "https://api.anthropic.com"


class RouterConfigError(RuntimeError):
    """Malformed router config or an auth-mode policy violation (raised at construction, fail-loud)."""


class TransportError(RuntimeError):
    """A single provider call failed — the router walks to the next binding."""


class AllProvidersFailed(RuntimeError):
    """Every binding in every tier failed; the caller must handle a router outage."""


# --------------------------------------------------------------------------- config structures


@dataclass(frozen=True)
class ProviderBinding:
    provider: Provider
    auth_mode: AuthMode
    model: str
    credential_ref: str          # the ENV VAR NAME holding the credential (BYOK); "" for keyless local
    endpoint: str | None = None  # override the provider default base URL


@dataclass(frozen=True)
class RouterTier:
    name: TierName
    primary: ProviderBinding
    fallbacks: tuple[ProviderBinding, ...] = ()


# --------------------------------------------------------------------------- HTTP + transports
# A ``Poster`` performs one HTTP POST and returns (status_code, body_bytes). Injected so tests assert
# request construction without a network round-trip; the default is a thin urllib POST.
Poster = Callable[[str, dict[str, str], bytes, float], "tuple[int, bytes]"]


def _urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except urllib.error.HTTPError as exc:  # a 4xx/5xx is a status, not a transport death
        return int(exc.code), exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise TransportError(f"http POST failed: {type(exc).__name__}") from exc


def _openai_compatible_transport(
    binding: ProviderBinding, credential: str | None, system: str, user: str,
    *, poster: Poster = _urllib_post, timeout: float = 90.0,
) -> str:
    base = (binding.endpoint or _OPENAI_COMPAT_ENDPOINTS.get(binding.provider) or "").rstrip("/")
    if not base:
        raise TransportError(f"no endpoint configured for provider {binding.provider}")
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    payload = {
        "model": binding.model,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    status, raw = poster(f"{base}/chat/completions", headers, json.dumps(payload).encode("utf-8"), timeout)
    if status != 200:
        raise TransportError(f"{binding.provider} HTTP {status}")
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # type name only — never the raw body, which could echo prompt/response content.
        raise TransportError(f"{binding.provider} malformed response: {type(exc).__name__}") from exc
    # Require ACTUAL text: a null / object / bool content must NOT coerce to a truthy "None" string
    # and be returned as success (that would block the fallback walk). Fail → walk to the next binding.
    if not isinstance(content, str) or not content.strip():
        raise TransportError(f"{binding.provider} response content missing or non-text")
    return content.strip()


def _anthropic_transport(
    binding: ProviderBinding, credential: str | None, system: str, user: str,
    *, poster: Poster = _urllib_post, timeout: float = 90.0,
) -> str:
    base = (binding.endpoint or _ANTHROPIC_DEFAULT_ENDPOINT).rstrip("/")
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if credential:
        headers["x-api-key"] = credential
    payload = {
        "model": binding.model,
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    status, raw = poster(f"{base}/v1/messages", headers, json.dumps(payload).encode("utf-8"), timeout)
    if status != 200:
        raise TransportError(f"anthropic HTTP {status}")
    try:
        blocks = json.loads(raw)["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise TransportError(f"anthropic malformed response: {type(exc).__name__}") from exc
    texts = [b.get("text") for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
    joined = "".join(t for t in texts if isinstance(t, str)).strip()
    if not joined:
        raise TransportError("anthropic response content missing or non-text")
    return joined


def default_transport(binding: ProviderBinding, credential: str | None, system: str, user: str) -> str:
    """Dispatch a resolved binding to its provider transport. Injected wholesale in tests.

    Only the api_key transports ship a concrete implementation in P1 (the unconditional default path,
    decision #1). Subscription + mcp_connector are ACCEPTED + ROUTED (the auth-mode gating is enforced
    at construction) but their concrete transports are DEFERRED: subscription-mode is gated on the ToS
    research pass AND needs a hardened subprocess (env-isolated) before it sends untrusted memory text
    to an agentic CLI; mcp_connector needs the SSE write surface (Slice-S P3b). Both raise here so the
    walk lands on the api_key fallback until they are wired with their own review."""
    if binding.auth_mode == "subscription":
        raise TransportError(
            "subscription transport is accepted + routed but deferred (ToS research + a hardened, "
            "env-isolated subprocess required); walking to fallback — API-key mode is the default"
        )
    if binding.auth_mode == "mcp_connector":
        raise TransportError(
            "mcp_connector transport is accepted + routed but not yet wired (deferred); walking to fallback"
        )
    if binding.provider == "anthropic":
        return _anthropic_transport(binding, credential, system, user)
    return _openai_compatible_transport(binding, credential, system, user)


# A ``Transport`` resolves one binding to raw model text (or raises ``TransportError``).
Transport = Callable[[ProviderBinding, "str | None", str, str], str]


def _is_local_endpoint(endpoint: str | None) -> bool:
    """True when the endpoint is unset (defaults to localhost) or points at a genuine LOOPBACK host —
    the only case where a KEYLESS (no-Authorization) request is safe (it never reaches a remote host).
    ``0.0.0.0`` is NOT loopback (it is a bind-all / unspecified address) and is rejected."""
    if not endpoint:
        return True  # the default ollama base is http://localhost:11434/v1
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(endpoint).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback  # 127.0.0.0/8, ::1 — excludes 0.0.0.0 + remote
    except ValueError:
        return False


# --------------------------------------------------------------------------- the router


class MemoryModelRouter:
    """A role-scoped provider fallback walk producing a ``chat_fn`` / ``judge_fn`` seam callable."""

    def __init__(
        self,
        tiers: Sequence[RouterTier],
        *,
        role: Role,
        transport: Transport = default_transport,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if role not in _ROLES:
            raise RouterConfigError(f"invalid role: {role!r}")
        self.tiers: tuple[RouterTier, ...] = tuple(tiers)
        self.role: Role = role
        self._transport = transport
        self._env = env if env is not None else os.environ
        # The subscription-mode ToS acknowledgment comes ONLY from the env gate — there is no
        # programmatic override, so a caller (incl. from_governance) cannot bypass it.
        self._subscription_ack = self._env.get(SUBSCRIPTION_ACK_ENV) == "1"
        self._validate()

    def _all_bindings(self) -> Iterator[ProviderBinding]:
        for tier in self.tiers:
            yield tier.primary
            yield from tier.fallbacks

    def _validate(self) -> None:
        if not self.tiers:
            raise RouterConfigError(f"router for role={self.role!r} has no tiers")
        for tier in self.tiers:
            if tier.name not in _TIER_NAMES:
                raise RouterConfigError(f"invalid tier name: {tier.name!r}")
        for binding in self._all_bindings():
            if binding.provider not in _PROVIDERS:
                raise RouterConfigError(f"invalid provider: {binding.provider!r}")
            if binding.auth_mode not in _AUTH_MODES:
                raise RouterConfigError(f"invalid auth_mode: {binding.auth_mode!r}")
            # A model id must be real — else the walk hits provider HTTP errors instead of failing
            # loud at construction (a directly-built binding; the config path also checks at parse).
            if not isinstance(binding.model, str) or not binding.model.strip():
                raise RouterConfigError(f"{binding.provider}: binding.model must be a non-empty string")
            if binding.auth_mode == "subscription":
                # Gate the CLAIM on an explicit ToS acknowledgment (decision #1) …
                if not self._subscription_ack:
                    raise RouterConfigError(
                        f"{binding.provider}: auth_mode=subscription requires {SUBSCRIPTION_ACK_ENV}=1 "
                        "(subscription-mode ToS is an owner acknowledgment; API-key mode is the "
                        "unconditional default)"
                    )
                # … and fail loud if a key is ALSO set — it silently overrides the subscription token.
                # Check the provider-default key env AND any explicit credential_ref (an overlay may
                # omit credential_ref yet still have the standard key env exported).
                candidate_envs = {_PROVIDER_DEFAULT_KEY_ENV.get(binding.provider, "")}
                if binding.credential_ref:
                    candidate_envs.add(binding.credential_ref)
                conflicting = sorted(name for name in candidate_envs if name and self._env.get(name))
                if conflicting:
                    raise RouterConfigError(
                        f"{binding.provider}: auth_mode=subscription but API key env(s) "
                        f"{', '.join(conflicting)} set — an API key silently overrides a "
                        "subscription/OAuth token; unset it (see agent-sdk-subscription-auth-headless)"
                    )

    def _resolve_credential(self, binding: ProviderBinding) -> str | None:
        """The credential to pass the transport, or raise ``TransportError`` to skip this binding.

        api_key → the env value at ``credential_ref`` (empty allowed for keyless local ollama; missing
        for any other provider makes the binding unavailable → walk). subscription / mcp_connector →
        None (device-auth / endpoint routing carry their own credential)."""
        if binding.auth_mode == "api_key":
            value = self._env.get(binding.credential_ref, "")
            if not value:
                # Keyless (no Authorization) is allowed ONLY for a LOCAL ollama endpoint — there is no
                # remote host a missing key could reach unauthenticated. Any other missing key makes
                # the binding unavailable → walk to the next.
                if binding.provider == "ollama" and _is_local_endpoint(binding.endpoint):
                    return ""
                raise TransportError(f"{binding.provider}: credential env {binding.credential_ref} not set")
            return value
        return None

    def _redact(self, text: str) -> str:
        """Strip any substantial env value (e.g. an API key) that an error message might contain —
        defense-in-depth for INJECTED transports (the default transports never embed a credential or a
        raw response body). Longest values first so a shorter secret that is a substring of a longer
        one can't leave a fragment; short values (<8 chars) are left alone so common env words aren't
        mangled."""
        out = text
        for value in sorted({v for v in self._env.values() if v and len(v) >= 8}, key=len, reverse=True):
            if value in out:
                out = out.replace(value, "***")
        return out

    def _invoke(self, system: str, user: str) -> str:
        errors: list[str] = []
        for binding in self._all_bindings():
            try:
                credential = self._resolve_credential(binding)
                text = self._transport(binding, credential, system, user)
            except Exception as exc:  # noqa: BLE001 — resilient: ANY transport/credential failure
                # walks to the next binding (a router outage must degrade, not crash). Redact so a
                # buggy INJECTED transport that raises a non-TransportError with a credential in its
                # message cannot leak it into the aggregate.
                reason = str(exc) if isinstance(exc, TransportError) else f"{type(exc).__name__}: {exc}"
                errors.append(self._redact(f"{binding.provider}/{binding.auth_mode}: {reason}"))
                continue
            if not isinstance(text, str) or not text.strip():
                errors.append(f"{binding.provider}/{binding.auth_mode}: empty response")
                continue
            return text
        raise AllProvidersFailed(
            f"all {sum(1 for _ in self._all_bindings())} model-router bindings failed for "
            f"role={self.role}: " + "; ".join(errors)
        )

    def chat_fn(self) -> ChatFn:
        """A ``(system, user) -> text`` transport for ``extract_preferences(chat_fn=…)``."""
        return lambda system, user: self._invoke(system, user)

    def judge_fn(self) -> JudgeFn:
        """A ``(system, user) -> text`` transport for ``decide(judge_fn=…)`` (reopenability #7)."""
        return lambda system, user: self._invoke(system, user)

    @classmethod
    def from_governance(
        cls, governance: Governance, *, role: Role, **kwargs: Any
    ) -> MemoryModelRouter:
        """Build a router from ``governance.model_router[role]`` — the per-role list of tier configs."""
        raw = governance.model_router.get(role)
        if raw is None:
            raise RouterConfigError(f"governance.model_router has no config for role={role!r}")
        return cls(_parse_tiers(raw, role=role), role=role, **kwargs)


# --------------------------------------------------------------------------- governance parsing


def _parse_binding(raw: Mapping[str, Any], *, where: str) -> ProviderBinding:
    if not isinstance(raw, Mapping):
        raise RouterConfigError(f"{where}: binding must be a mapping, got {type(raw).__name__}")
    try:
        provider = str(raw["provider"])
        auth_mode = str(raw.get("auth_mode", "api_key"))
        raw_model = raw["model"]
        credential_ref = str(raw.get("credential_ref", ""))
    except KeyError as exc:
        raise RouterConfigError(f"{where}: binding missing required key {exc}") from exc
    # Reject a null / non-string / blank model BEFORE coercion — str(None) → "None" would build a
    # binding that only fails later as a provider HTTP error (walking to an unintended fallback).
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise RouterConfigError(f"{where}: binding.model must be a non-empty string, got {raw_model!r}")
    model = raw_model.strip()
    endpoint = raw.get("endpoint")
    return ProviderBinding(
        provider=provider,  # type: ignore[arg-type]
        auth_mode=auth_mode,  # type: ignore[arg-type]
        model=model,
        credential_ref=credential_ref,
        endpoint=str(endpoint) if endpoint else None,
    )


def _parse_tiers(raw: Any, *, role: str) -> list[RouterTier]:
    if not isinstance(raw, list) or not raw:
        raise RouterConfigError(f"model_router[{role!r}] must be a non-empty list of tiers")
    tiers: list[RouterTier] = []
    for i, tier_raw in enumerate(raw):
        if not isinstance(tier_raw, Mapping):
            raise RouterConfigError(f"model_router[{role!r}][{i}] must be a mapping")
        where = f"model_router[{role!r}][{i}]"
        name = str(tier_raw.get("name", ""))
        if "primary" not in tier_raw:
            raise RouterConfigError(f"{where}: missing 'primary' binding")
        primary = _parse_binding(tier_raw["primary"], where=f"{where}.primary")
        fallbacks = tuple(
            _parse_binding(fb, where=f"{where}.fallbacks[{j}]")
            for j, fb in enumerate(tier_raw.get("fallbacks") or [])
        )
        tiers.append(RouterTier(name=name, primary=primary, fallbacks=fallbacks))  # type: ignore[arg-type]
    return tiers
