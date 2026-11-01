"""Offline tests for the PreToolUse shell guard (.claude/hooks/guard_bash.py).

The hook is not a package module, so it is loaded by path. Pins the 2026-11-01 fix:
`stage1`/`stage2` were fused into `leadauto run`, and the guard's pattern, cost table
and config-live check had only known the old names -- a local
`leadauto run --no-dry-run` got no prompt at all. Also pins that a PowerShell-tool
payload (same `tool_input.command` shape) is classified exactly like a Bash one.
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard_bash.py"


@pytest.fixture()
def guard(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("guard_bash_under_test", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Keep the session API-estimate counter out of the real temp dir.
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    # Default: config says dry run (the committed value); tests override as needed.
    monkeypatch.setattr(module, "config_dry_run_default", lambda: True)
    return module


def _decision(guard, command):
    return guard.classify(command)[0]


def test_run_no_dry_run_asks(guard):
    decision, reason = guard.classify("python -m leadauto run --no-dry-run")
    assert decision == "ask"
    assert "leadauto run" in reason
    assert "IRREVERSIBLE" in reason
    assert "Stage 2" not in reason


def test_run_dry_run_is_silent(guard):
    assert guard.classify("python -m leadauto run --dry-run") == (None, None)
    assert guard.classify("leadauto run --dry-run --batch-size 5") == (None, None)


def test_run_big_batch_without_dry_run_is_denied(guard):
    decision, reason = guard.classify("python -m leadauto run --batch-size 5")
    assert decision == "deny"
    assert "--batch-size 5" in reason
    assert _decision(guard, "leadauto run --batch-size=4 --no-dry-run") == "deny"
    # The ceiling itself is allowed (then asks, because it is live).
    assert _decision(guard, "leadauto run --batch-size 3 --no-dry-run") == "ask"


def test_bare_run_with_config_live_asks(guard, monkeypatch):
    monkeypatch.setattr(guard, "config_dry_run_default", lambda: False)
    decision, reason = guard.classify("python -m leadauto run")
    assert decision == "ask"
    assert "pipeline.dry_run: false" in reason
    assert "`leadauto run`" in reason
    # Explicit --dry-run still wins over the config.
    assert guard.classify("python -m leadauto run --dry-run") == (None, None)


def test_bare_run_with_config_dry_is_silent(guard):
    assert guard.classify("python -m leadauto run") == (None, None)


def test_bare_run_with_unreadable_config_is_silent(guard, monkeypatch):
    monkeypatch.setattr(guard, "config_dry_run_default", lambda: None)
    assert guard.classify("python -m leadauto run --limit 5") == (None, None)


def test_powershell_shaped_command_is_classified(guard):
    command = '$env:PYTHONPATH = "src"; python -m leadauto run --no-dry-run --no-leadfeeder'
    assert _decision(guard, command) == "ask"


@pytest.mark.parametrize("command", [
    "python -m leadauto runner --no-dry-run",
    "python -m leadauto run-foo --no-dry-run",
    "python -m leadauto running",
])
def test_run_does_not_false_match_longer_words(guard, command):
    assert guard.LEADAUTO_RE.search(command) is None


def test_legacy_stage_names_still_classify(guard, monkeypatch):
    assert _decision(guard, "python -m leadauto stage2 --no-dry-run") == "ask"
    monkeypatch.setattr(guard, "config_dry_run_default", lambda: False)
    assert _decision(guard, "python -m leadauto stage1") == "ask"
    # shadow-compare has no dry/live mode; the config does not make it "live".
    assert guard.classify("python -m leadauto shadow-compare") == (None, None)


def test_workflow_dispatch_live_still_asks(guard):
    assert _decision(guard, "gh workflow run pipeline.yml -f dry_run=false") == "ask"


def test_run_has_a_cost_entry(guard):
    assert guard.RUN_COST["run"] == 22
    assert guard.RUN_COST["run"] >= guard.RUN_COST["stage1"] + guard.RUN_COST["stage2"]


def _run_main(guard, monkeypatch, capsys, payload):
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else {}


@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_main_reads_command_for_both_shell_tools(guard, monkeypatch, capsys, tool_name):
    output = _run_main(guard, monkeypatch, capsys, {
        "session_id": "test-{}".format(tool_name),
        "tool_name": tool_name,
        "tool_input": {"command": "python -m leadauto run --no-dry-run"},
    })
    hook = output["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert hook["permissionDecision"] == "ask"


def test_main_counts_run_cost_and_warns(guard, monkeypatch, capsys):
    payload = {
        "session_id": "cost-test",
        "tool_name": "PowerShell",
        "tool_input": {"command": "python -m leadauto run --dry-run"},
    }
    # 22 < 25: no warning on the first run; the second crosses the threshold once.
    assert _run_main(guard, monkeypatch, capsys, payload) == {}
    second = _run_main(guard, monkeypatch, capsys, payload)
    assert "systemMessage" in second and "API budget" in second["systemMessage"]
    assert _run_main(guard, monkeypatch, capsys, payload) == {}


@pytest.mark.parametrize("payload", [
    [],
    {"tool_input": "not a dict"},
    {"tool_input": {"command": None}},
    {"tool_input": {}},
])
def test_main_fails_open_on_odd_payloads(guard, monkeypatch, capsys, payload):
    assert _run_main(guard, monkeypatch, capsys, payload) == {}
