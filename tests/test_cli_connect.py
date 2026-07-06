from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path

import pytest

from panella.cli import connect as connect_cli
from panella.cli import main

OWNER_BEARER_PATH = Path(".panella/owner-bearer")
APPROVAL_TOKEN_PATH = Path(".panella/approval-token")

# Official Cursor MCP deeplink format:
# https://docs.cursor.com/context/model-context-protocol#installing-mcp-servers
CURSOR_DEEPLINK_PAYLOAD = {
    "url": "http://127.0.0.1:8001/mcp",
    "headers": {"Authorization": "Bearer TESTTOK"},
}

# Official VS Code MCP install format:
# https://code.visualstudio.com/docs/copilot/chat/mcp-servers
VSCODE_DEEPLINK_PAYLOAD = {
    "name": "panella",
    "type": "http",
    "url": "http://127.0.0.1:8001/mcp",
    "headers": {"Authorization": "Bearer TESTTOK"},
}


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
def test_connect_prints_default_placeholder_shapes_with_hint(tmp_path, monkeypatch, capsys, client, expected):
    monkeypatch.chdir(tmp_path)

    rc = main(["connect", "--print", client])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == expected
    assert "run panella init (or pass --token)" in captured.err
    assert "live credential" not in captured.err


def test_connect_resolution_order_explicit_file_then_placeholder(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    OWNER_BEARER_PATH.parent.mkdir()
    OWNER_BEARER_PATH.write_text("m2_file_bearer\n", encoding="utf-8")

    assert main(["connect", "--print", "claude-code"]) == 0
    captured = capsys.readouterr()
    assert "Bearer m2_file_bearer" in captured.out
    assert "live credential" in captured.err

    assert main(["connect", "--print", "claude-code", "--token", "m2_explicit_bearer"]) == 0
    captured = capsys.readouterr()
    assert "Bearer m2_explicit_bearer" in captured.out
    assert "m2_file_bearer" not in captured.out
    assert "live credential" in captured.err

    OWNER_BEARER_PATH.write_text("one\ntwo\n", encoding="utf-8")
    assert main(["connect", "--print", "claude-code"]) == 0
    captured = capsys.readouterr()
    assert "Bearer PANELLA_BEARER_HERE" in captured.out
    assert "run panella init (or pass --token)" in captured.err

    OWNER_BEARER_PATH.write_text("\n", encoding="utf-8")
    assert main(["connect", "--print", "claude-code"]) == 0
    captured = capsys.readouterr()
    assert "Bearer PANELLA_BEARER_HERE" in captured.out
    assert "run panella init (or pass --token)" in captured.err


def test_connect_token_arg_default_is_none():
    parser = _connect_parser()
    args = parser.parse_args(["connect", "--print", "claude-code"])
    assert args.token is None


@pytest.mark.parametrize("client", ["claude-code", "claude-desktop", "cursor"])
def test_connect_accepts_base_url_that_already_points_at_mcp(tmp_path, monkeypatch, client, capsys):
    monkeypatch.chdir(tmp_path)

    rc = main(["connect", "--print", client, "--base-url", "http://127.0.0.1:9000/mcp"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "http://127.0.0.1:9000/mcp" in captured.out
    assert "http://127.0.0.1:9000/mcp/mcp" not in captured.out


@pytest.mark.parametrize(
    "args",
    [
        ["--print", "claude-code"],
        ["--print", "claude-desktop"],
        ["--print", "cursor"],
        ["--deeplink", "cursor"],
        ["--deeplink", "vscode"],
    ],
)
def test_connect_never_emits_approval_token_when_both_files_present(tmp_path, monkeypatch, capsys, args):
    monkeypatch.chdir(tmp_path)
    OWNER_BEARER_PATH.parent.mkdir()
    OWNER_BEARER_PATH.write_text("m2_live_owner\n", encoding="utf-8")
    APPROVAL_TOKEN_PATH.write_text("approval-secret-never-emit\n", encoding="utf-8")

    assert main(["connect", *args]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    if args[0] == "--deeplink":
        payload = _decode_deeplink_payload(captured.out.strip(), kind=args[1])
        assert payload["headers"]["Authorization"] == "Bearer m2_live_owner"
    else:
        assert "m2_live_owner" in output
    assert "approval-secret-never-emit" not in output
    assert str(APPROVAL_TOKEN_PATH) not in output


def test_connect_open_guard_never_reads_approval_token(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    OWNER_BEARER_PATH.parent.mkdir()
    OWNER_BEARER_PATH.write_text("m2_live_owner\n", encoding="utf-8")
    APPROVAL_TOKEN_PATH.write_text("approval-secret-never-read\n", encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if str(self).endswith("approval-token"):
            raise AssertionError(f"connect opened forbidden path: {self}")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    for args in (
        ["--print", "claude-code"],
        ["--print", "claude-desktop"],
        ["--print", "cursor"],
        ["--deeplink", "cursor"],
        ["--deeplink", "vscode"],
    ):
        assert main(["connect", *args]) == 0
        capsys.readouterr()


def test_cursor_deeplink_external_contract(capsys):
    rc = main([
        "connect",
        "--deeplink",
        "cursor",
        "--token",
        "TESTTOK",
        "--base-url",
        "http://127.0.0.1:8001",
    ])
    captured = capsys.readouterr()
    url = captured.out.strip()

    assert rc == 0
    assert captured.out.count("\n") == 1
    assert url.startswith("cursor://anysphere.cursor-deeplink/mcp/install?name=panella&config=")
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    assert query["name"] == ["panella"]
    payload = json.loads(base64.b64decode(query["config"][0]).decode())
    assert payload == CURSOR_DEEPLINK_PAYLOAD
    assert captured.err == "deeplink embeds your bearer \u2014 treat the URL as a secret\n"


def test_vscode_deeplink_external_contract(capsys):
    rc = main([
        "connect",
        "--deeplink",
        "vscode",
        "--token",
        "TESTTOK",
        "--base-url",
        "http://127.0.0.1:8001",
    ])
    captured = capsys.readouterr()
    url = captured.out.strip()

    assert rc == 0
    assert captured.out.count("\n") == 1
    assert url.startswith("vscode:mcp/install?")
    payload = json.loads(urllib.parse.unquote(url.removeprefix("vscode:mcp/install?")))
    assert payload == VSCODE_DEEPLINK_PAYLOAD
    assert captured.err == "deeplink embeds your bearer \u2014 treat the URL as a secret\n"


def _connect_parser():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    connect_cli.register(subparsers)
    return parser


def _decode_deeplink_payload(url: str, *, kind: str) -> dict:
    if kind == "cursor":
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        return json.loads(base64.b64decode(query["config"][0]).decode())
    return json.loads(urllib.parse.unquote(url.removeprefix("vscode:mcp/install?")))
