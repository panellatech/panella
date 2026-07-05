from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from panella.cli import main
from panella.http.tokens import TokenStore
from panella.principal import root_principal


def test_tokens_mint_help_is_argparse_only(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["tokens", "mint", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "panella tokens mint" in captured.out
    assert "--principal" in captured.out


def test_tokens_mint_help_works_without_site_packages():
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from panella.cli import main; raise SystemExit(main(['tokens', 'mint', '--help']))",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "panella tokens mint" in result.stdout


def test_tokens_mint_prints_once_and_resolves(tmp_path, capsys):
    token_db = tmp_path / "tokens.db"
    rc = main(["tokens", "mint", "--token-db", str(token_db), "--label", "test-owner"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert len(lines) == 1
    token = lines[0]
    assert token.startswith("m2_")
    assert token not in captured.err
    assert "not recoverable" in captured.err

    record = TokenStore(token_db).resolve(token, touch=False)
    assert record is not None
    assert record.principal_id == root_principal().id
    assert record.label == "test-owner"


def test_tokens_mint_duplicate_label_is_actionable(tmp_path, capsys):
    """A duplicate --label must fail with an actionable one-liner + exit 2, not an opaque
    sqlite3.IntegrityError traceback (the exact class WP3 exists to eliminate)."""
    token_db = tmp_path / "tokens.db"
    assert main(["tokens", "mint", "--token-db", str(token_db), "--label", "owner"]) == 0
    capsys.readouterr()  # drain the first mint's output
    rc = main(["tokens", "mint", "--token-db", str(token_db), "--label", "owner"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "already exists" in captured.err
    assert captured.out == ""  # no token printed on the failure path
