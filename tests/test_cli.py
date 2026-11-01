"""CLI tests.

The command bodies are thin glue over the already-tested pipeline functions and construct
live clients, so we don't invoke them against anything real. We test that the Typer app
exposes the `run` command (via --help, which never touches an org), the exit-code rule of
`run` with a faked pipeline, and the shadow-compare gate.
"""
import pytest
from typer.testing import CliRunner

from leadauto.cli import app
from leadauto.convert import ConversionTarget
from leadauto.pipeline.run import PipelineResult

runner = CliRunner()


def test_app_exposes_run_and_no_stage_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "stage1" not in result.stdout and "stage2" not in result.stdout


def test_run_help_lists_flags():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.stdout
    assert "--limit" in result.stdout
    assert "--batch-size" in result.stdout


def _stub_shadow_compare(monkeypatch, cli):
    """Stub the org call + the run-state stamp (which would touch the repo's state/)."""
    stamped: list[bool] = []
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, log: object())
    monkeypatch.setattr(cli, "record_compare_run", lambda: stamped.append(True))
    # The skipped-run report writer would otherwise write the repo's real xlsx.
    monkeypatch.setattr(cli, "write_skipped_report",
                        lambda **kw: stamped.append(False) or {"xlsx": "shadow_comparison.xlsx"})
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
    # The pipeline is dispatched 5x/day and a compare costs ~7-9 Salesforce requests, so a
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
    assert "skipped" in result.output
    # Not stamped (False = the offline report writer ran, True would be a real compare):
    # the artifact is uploaded on every dispatch, so the workbook must still be written.
    assert stamped == [False]
    assert "xlsx=shadow_comparison.xlsx" in result.output


# --- `run` exit codes: fail the job on failed/missing/aborted conversions ------------

def _fake_run(monkeypatch, conversion, targets=()):
    from leadauto import cli
    captured = {}

    def fake_run_pipeline(sf, lf, settings, **kwargs):
        captured.update(kwargs)
        return PipelineResult(results=[], skipped=[], writeback={"processed": 0},
                              dry_run=bool(kwargs.get("dry_run")),
                              targets=list(targets), conversion=conversion)

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, l: object())
    return captured


def _conversion(**result):
    base = {"converted": 0, "already_converted": 0, "failed": 0, "missing": 0,
            "missing_ids": [], "results": []}
    return {"targets": 1, "preflight_failures": [], "result": {**base, **result},
            "error": None, "targets_run": 1, "targets_sweep": 0}


def test_run_cli_passes_batch_size_and_reports_counts(monkeypatch):
    captured = _fake_run(monkeypatch, _conversion(converted=2),
                         targets=[ConversionTarget("00Q000000000001"),
                                  ConversionTarget("00Q000000000002")])
    result = runner.invoke(app, ["run", "--dry-run", "--batch-size", "1"])
    assert result.exit_code == 0, result.output
    assert captured["batch_size"] == 1 and captured["dry_run"] is True
    assert "targets=2" in result.output and "converted=2" in result.output


def test_run_cli_exits_nonzero_on_failed_or_missing(monkeypatch):
    # A failed or unaccounted-for conversion must fail the job, not just log a count.
    _fake_run(monkeypatch, _conversion(failed=1, missing=1, missing_ids=["00Q000000000009"]))
    result = runner.invoke(app, ["run", "--no-dry-run"])
    assert result.exit_code == 1
    assert "failed=1 missing=1" in result.output


def test_run_cli_exits_nonzero_when_conversion_phase_aborted(monkeypatch):
    conv = _conversion()
    conv["error"] = "ConnectionError: socket dropped"
    _fake_run(monkeypatch, conv)
    result = runner.invoke(app, ["run", "--no-dry-run"])
    assert result.exit_code == 1
    assert "Conversion phase aborted" in result.output


def test_run_cli_zero_exit_on_already_converted_only(monkeypatch):
    # A re-run where every lead was already converted is an idempotent no-op: it must exit
    # 0 (green), not fail the job the way `failed`/`missing` do.
    _fake_run(monkeypatch, _conversion(already_converted=2))
    result = runner.invoke(app, ["run", "--no-dry-run"])
    assert result.exit_code == 0
    assert "already=2" in result.output


def test_run_cli_zero_exit_without_conversion(monkeypatch):
    # convert=False style result (no conversion dict at all) is still green
    _fake_run(monkeypatch, {})
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0
    assert "converted=0" in result.output
