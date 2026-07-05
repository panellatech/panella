"""``panella-http`` — serve the governed memory HTTP facade.

Thin uvicorn runner around the serving factory ``panella.http.app:create_app`` — the same
factory owner's systemd unit runs (``uvicorn panella.http.app:create_app --factory``).
Everything that matters (startup coherence self-check, 503 serving gate, bearer auth, rate
limit) is the factory's own contract; this wrapper only owns the bind knobs so a container can
listen on 0.0.0.0 without editing the unit-shaped defaults.

The factory resolves governance + store config from the process environment
(``PANELLA_GOVERNANCE_OVERLAY``, ``PANELLA_STORE_PATH``, ``PANELLA_API_KEY``/``PANELLA_API_KEY_FILE``,
``PANELLA_HTTP_*``) — see docs/SELF_HOST.md for the full table. Serving requires the repo-anchored
config tree (a checkout or the Docker image); the wheel alone does not carry config/.
"""

from __future__ import annotations

import argparse
import os

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
ENV_HOST = "PANELLA_HTTP_HOST"
ENV_PORT = "PANELLA_HTTP_PORT"
ENV_LOG_LEVEL = "PANELLA_HTTP_LOG_LEVEL"


def build_parser() -> argparse.ArgumentParser:
    env_host = os.environ.get(ENV_HOST, "").strip() or DEFAULT_HOST
    env_port_raw = os.environ.get(ENV_PORT, "").strip()
    parser = argparse.ArgumentParser(
        prog="panella-http",
        description="Serve the governed memory HTTP facade (panella.http.app:create_app).",
    )
    parser.add_argument(
        "--host",
        default=env_host,
        help=f"Bind address (default: ${ENV_HOST} or {DEFAULT_HOST}; containers set 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(env_port_raw) if env_port_raw else DEFAULT_PORT,
        help=f"Bind port (default: ${ENV_PORT} or {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--log-level",
        default=(os.environ.get(ENV_LOG_LEVEL, "").strip() or "info").lower(),
        help=f"uvicorn log level (default: ${ENV_LOG_LEVEL} or info).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Imported here so `panella-http --help` stays a pure-argparse path (usable in a clean
    # `uvx` env for entry-point verification without booting the server stack).
    import uvicorn

    uvicorn.run(
        "panella.http.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
