"""CLI tests.

The command bodies are thin glue over the already-tested stage functions and construct live
clients, so we don't invoke them here. We test the pure helper (`find_latest_approved`) and
that the Typer app exposes the stage1/stage2 commands (via --help, which never touches an org).
"""
from typer.testing import CliRunner

from leadauto.cli import app, find_latest_approved

runner = CliRunner()


def test_find_latest_approved_picks_newest(tmp_path):
    (tmp_path / "approved_2026-06-01.csv").write_text("x", encoding="utf-8")
    (tmp_path / "approved_2026-06-20.csv").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    latest = find_latest_approved(tmp_path)
    assert latest.name == "approved_2026-06-20.csv"


def test_find_latest_approved_none_when_empty(tmp_path):
    assert find_latest_approved(tmp_path) is None


def test_app_exposes_stage1_and_stage2():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "stage1" in result.stdout
    assert "stage2" in result.stdout


def test_stage1_help_lists_flags():
    result = runner.invoke(app, ["stage1", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.stdout
    assert "--limit" in result.stdout
