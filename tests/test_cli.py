"""CLI tests.

The command bodies are thin glue over the already-tested stage functions and construct live
clients, so we don't invoke them here. We test the pure helper (`find_latest_approved`) and
that the Typer app exposes the stage1/stage2 commands (via --help, which never touches an org).
"""
import pytest
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


def _stub_shadow_compare(monkeypatch, cli):
    """Stub the org call + the run-state stamp (which would touch the repo's state/)."""
    stamped: list[bool] = []
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, log: object())
    monkeypatch.setattr(cli, "record_compare_run", lambda: stamped.append(True))
    monkeypatch.setattr(cli, "run_shadow_compare", lambda sf, excluded_domains, logger: {
        "checked": 3, "resolved": 2, "hits": 1, "misses": 1, "observed": 0,
        "expired": 0, "excluded": 1, "pending": 1, "xlsx": "shadow_comparison.xlsx"})
    return stamped


def test_shadow_compare_command(monkeypatch):
    import leadauto.cli as cli
    stamped = _stub_shadow_compare(monkeypatch, cli)
    monkeypatch.setattr(cli, "compare_is_due", lambda min_interval_hours: True)
    result = CliRunner().invoke(cli.app, ["shadow-compare"])
    assert result.exit_code == 0
    assert "resolved=2" in result.output and "hits=1" in result.output
    assert "excluded=1" in result.output
    assert stamped == [True]           # run-state stamped for the next dispatch's gate


def test_shadow_compare_skips_when_not_due(monkeypatch):
    # Stage 1 is dispatched 4x/day and a compare costs ~7-9 Salesforce requests, so a
    # too-recent compare must skip WITHOUT touching the org (and still exit green).
    import leadauto.cli as cli
    from datetime import datetime, timezone
    stamped = _stub_shadow_compare(monkeypatch, cli)
    monkeypatch.setattr(cli, "_build_sf_client",
                        lambda s, log: pytest.fail("must not reach the org"))
    monkeypatch.setattr(cli, "compare_is_due", lambda min_interval_hours: False)
    monkeypatch.setattr(cli, "last_compare_at",
                        lambda: datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc))
    result = CliRunner().invoke(cli.app, ["shadow-compare"])
    assert result.exit_code == 0
    assert "skipped" in result.output and stamped == []


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


def test_stage2_cli_exits_nonzero_on_failed_or_missing(monkeypatch, tmp_path):
    # A failed or unaccounted-for conversion must fail the job, not just log a count.
    from leadauto import cli
    from leadauto.pipeline.stage2_convert import Stage2Result

    def fake_run_stage2(sf, settings, approved, **kwargs):
        return Stage2Result(
            targets=[object()],
            result={"converted": 0, "failed": 1, "missing": 1, "missing_ids": ["00Q000000000009"]},
            dry_run=False, preflight_failures=[])

    monkeypatch.setattr(cli, "run_stage2", fake_run_stage2)
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, l: object())
    approved = tmp_path / "approved_2.csv"
    approved.write_text("lead_id,Approve?\n00Q000000000009,yes\n", encoding="utf-8")

    from typer.testing import CliRunner
    result = CliRunner().invoke(cli.app, ["stage2", "--approved", str(approved),
                                          "--no-preflight", "--no-dry-run"])
    assert result.exit_code == 1
    assert "failed=1 missing=1" in result.output


def test_stage2_cli_zero_exit_on_already_converted_only(monkeypatch, tmp_path):
    # A re-run where every lead was already converted is an idempotent no-op: it must exit
    # 0 (green), not fail the job the way `failed`/`missing` do.
    from leadauto import cli
    from leadauto.pipeline.stage2_convert import Stage2Result

    def fake_run_stage2(sf, settings, approved, **kwargs):
        return Stage2Result(
            targets=[object(), object()],
            result={"converted": 0, "already_converted": 2, "failed": 0, "missing": 0,
                    "missing_ids": []},
            dry_run=False, preflight_failures=[])

    monkeypatch.setattr(cli, "run_stage2", fake_run_stage2)
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, l: object())
    approved = tmp_path / "approved_4.csv"
    approved.write_text("lead_id,Approve?\n00Q000000000009,yes\n", encoding="utf-8")

    from typer.testing import CliRunner
    result = CliRunner().invoke(cli.app, ["stage2", "--approved", str(approved),
                                          "--no-preflight", "--no-dry-run"])
    assert result.exit_code == 0
    assert "already=2" in result.output


def test_stage2_cli_exits_nonzero_on_malformed_csv(monkeypatch, tmp_path):
    # A bad Id in the approved CSV raises ValueError inside run_stage2; the CLI turns it
    # into a readable non-zero exit rather than a traceback.
    from leadauto import cli

    def fake_run_stage2(sf, settings, approved, **kwargs):
        raise ValueError("Malformed Salesforce Id(s) in the approved CSV — fix the file")

    monkeypatch.setattr(cli, "run_stage2", fake_run_stage2)
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, l: object())
    approved = tmp_path / "approved_3.csv"
    approved.write_text("lead_id,Approve?\nbad,yes\n", encoding="utf-8")

    from typer.testing import CliRunner
    result = CliRunner().invoke(cli.app, ["stage2", "--approved", str(approved)])
    assert result.exit_code == 1
    assert "Malformed Salesforce Id" in result.output
