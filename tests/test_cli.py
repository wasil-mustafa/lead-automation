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


def test_shadow_compare_command(monkeypatch):
    import leadauto.cli as cli
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, log: object())
    monkeypatch.setattr(cli, "run_shadow_compare", lambda sf, logger: {
        "checked": 3, "resolved": 2, "hits": 1, "misses": 1, "observed": 0,
        "expired": 0, "pending": 1, "xlsx": "shadow_comparison.xlsx"})
    result = CliRunner().invoke(cli.app, ["shadow-compare"])
    assert result.exit_code == 0
    assert "resolved=2" in result.output and "hits=1" in result.output
