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


def test_stage2_cli_reports_preflight_skips(monkeypatch, tmp_path):
    from leadauto import cli
    from leadauto.pipeline.stage2_convert import Stage2Result

    captured = {}

    def fake_run_stage2(sf, settings, approved, *, dry_run=None, batch_size=50,
                        preflight=True, failures_csv="preflight_failures.csv", logger=None):
        captured["preflight"] = preflight
        return Stage2Result(targets=[], result={"converted": 0, "failed": 0},
                            dry_run=True,
                            preflight_failures=[{"lead_id": "00Q1", "check": "language",
                                                 "value": "", "detail": "d"}])

    monkeypatch.setattr(cli, "run_stage2", fake_run_stage2)
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, l: object())
    approved = tmp_path / "approved_1.csv"
    approved.write_text("lead_id,Approve?\n00Q1,yes\n", encoding="utf-8")

    from typer.testing import CliRunner
    result = CliRunner().invoke(cli.app, ["stage2", "--approved", str(approved),
                                          "--no-preflight"])
    assert result.exit_code == 0
    assert captured["preflight"] is False
    assert "preflight_skipped=1" in result.output
