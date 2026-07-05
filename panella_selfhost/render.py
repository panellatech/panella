"""``panella-render-config`` — render the per-distribution config artifact (plan v7 §1.6 part 2).

The finalizer profile + wings allowlists must match the deployment's owner identity, but
``AgentProfile.load`` resolves hardcoded in-repo paths and a coord FF-deploy would overwrite any
in-repo templating — so origin/main keeps ``config/agents/panella-finalizer.yaml`` +
``config/wings.yaml`` deployment-pinned, and the PACKAGE build renders generic versions from the
generic governance instead (the ONLY place the finalizer/wings de-Owner happens). This CLI is
that render step: the Docker image build invokes it into ``/app/dist-config``, and a bare-metal
box can invoke it against its own governance/overlay.

Overlay semantics mirror runtime governance exactly: ``--overlay`` wins, else the
``PANELLA_GOVERNANCE_OVERLAY`` env pointer, else the pure generic base — so a deployment that
renders its own distribution config gets the same identity its services resolve.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from panella.config_render import render_distribution_config
from panella.governance import DEFAULT_GOVERNANCE_PATH, GovernanceConfigError, load_governance


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panella-render-config",
        description=(
            "Render the per-distribution finalizer profile + wings config from governance "
            "identity into --out (the package artifact's config dir)."
        ),
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Directory to render into (created if missing): writes agents/ + wings.yaml.",
    )
    parser.add_argument(
        "--governance",
        type=Path,
        default=DEFAULT_GOVERNANCE_PATH,
        help=f"Governance base config to render from (default: {DEFAULT_GOVERNANCE_PATH}).",
    )
    parser.add_argument(
        "--overlay",
        type=Path,
        default=None,
        help=(
            "Governance overlay to deep-merge over the base (default: the "
            "PANELLA_GOVERNANCE_OVERLAY env pointer, unset = pure generic base)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        governance = load_governance(base_path=args.governance, overlay_path=args.overlay)
    except GovernanceConfigError as exc:
        print(f"panella-render-config: {exc}", file=sys.stderr)
        return 2
    written = render_distribution_config(governance, args.out)
    for logical_name in sorted(written):
        print(f"{logical_name}: {written[logical_name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
