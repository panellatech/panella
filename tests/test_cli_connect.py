from __future__ import annotations

import json

import pytest

from panella.cli import main


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (
            "claude-code",
            'claude mcp add --transport http panella http://127.0.0.1:8001/mcp '
            '--header "Authorization: Bearer PANELLA_BEARER_HERE"\n',
        ),
        (
            "claude-desktop",
            json.dumps(
                {
                    "mcpServers": {
                        "panella": {
                            "type": "http",
                            "url": "http://127.0.0.1:8001/mcp",
                            "headers": {"Authorization": "Bearer PANELLA_BEARER_HERE"},
                        }
                    }
                },
                indent=2,
            )
            + "\n",
        ),
        (
            "cursor",
            json.dumps(
                {
                    "mcpServers": {
                        "panella": {
                            "url": "http://127.0.0.1:8001/mcp",
                            "headers": {"Authorization": "Bearer PANELLA_BEARER_HERE"},
                        }
                    }
                },
                indent=2,
            )
            + "\n",
        ),
    ],
)
def test_connect_prints_default_placeholder_shapes(client, expected, capsys):
    rc = main(["connect", "--print", client])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == expected
    assert captured.err == ""


def test_connect_inlines_bearer_and_base_url(capsys):
    rc = main([
        "connect",
        "--print",
        "claude-code",
        "--token",
        "m2_test_bearer",
        "--base-url",
        "http://127.0.0.1:9000",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == (
        'claude mcp add --transport http panella http://127.0.0.1:9000/mcp '
        '--header "Authorization: Bearer m2_test_bearer"\n'
    )


@pytest.mark.parametrize("client", ["claude-code", "claude-desktop", "cursor"])
def test_connect_accepts_base_url_that_already_points_at_mcp(client, capsys):
    rc = main(["connect", "--print", client, "--base-url", "http://127.0.0.1:9000/mcp"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "http://127.0.0.1:9000/mcp" in captured.out
    assert "http://127.0.0.1:9000/mcp/mcp" not in captured.out
