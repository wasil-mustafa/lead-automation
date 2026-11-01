#!/usr/bin/env python
"""SessionStart hook: inject the pipeline's current state as context.

Wired into .claude/settings.json as a SessionStart hook.

Why: every session in this repo opens with the same three lookups -- where is the
watermark, is ``dry_run`` still true, when did shadow-compare last run -- and the
answers are exactly the things CLAUDE.md cannot keep current, because they live in
committed state files and in a cron-job.org payload no file here records. Ten
lines of context up front removes those tool calls and, more importantly, stops an
answer being reasoned from a stale prose claim.

Hard rule: **no network, no `sf`, no `gh`.** This runs on every session start, and
the API budget is shared with business-critical integrations. Everything here comes
off the local disk or from git. ``watermark.json`` is summarized (it carries ~7000
processed task ids) rather than quoted.

Fails open and silent: a session must never fail to start because a state file is
missing or malformed.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE = REPO_ROOT / "state"


def git(*args):
    try:
        completed = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - best effort
        return ""


def count_lines(path):
    try:
        with path.open("rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return None


def config_lines():
    try:
        import yaml  # noqa: PLC0415

        cfg = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text("utf-8")) or {}
    except Exception:  # noqa: BLE001
        return ["  config/config.yaml: unreadable"]
    pipeline = cfg.get("pipeline") or {}
    scope = pipeline.get("live_scope") or {}
    sf = cfg.get("salesforce") or {}
    return [
        "  dry_run={!r}  convert_batch_size={!r}  task_lookback_days={!r}  batch_limit={!r}".format(
            pipeline.get("dry_run"),
            pipeline.get("convert_batch_size"),
            pipeline.get("task_lookback_days"),
            pipeline.get("batch_limit"),
        ),
        "  live_scope: decisions={!r} max_kununu_score={!r}".format(
            scope.get("decisions"), scope.get("max_kununu_score")
        ),
        "  enabled_task_types={!r}".format(cfg.get("enabled_task_types")),
        "  org_alias={!r}  owner_id={!r}  converted_status={!r}".format(
            sf.get("org_alias"), sf.get("owner_id"), sf.get("converted_status")
        ),
    ]


def watermark_lines():
    try:
        data = json.loads((STATE / "watermark.json").read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return ["  state/watermark.json: unreadable"]
    ids = data.get("processed_task_ids") or []
    return [
        "  last_task_created={}  processed_task_ids={}".format(
            data.get("last_task_created"), len(ids)
        )
    ]


def shadow_lines():
    lines = []
    predictions = count_lines(STATE / "predictions.jsonl")
    results = count_lines(STATE / "shadow_results.jsonl")
    lines.append(
        "  predictions.jsonl={} rows  shadow_results.jsonl={} rows".format(
            "?" if predictions is None else predictions,
            "?" if results is None else results,
        )
    )
    try:
        state = json.loads((STATE / "shadow_state.json").read_text("utf-8"))
        lines.append("  shadow_state.json={!r}".format(state))
    except Exception:  # noqa: BLE001
        lines.append(
            "  shadow_state.json: absent/unreadable -- the compare gate fails OPEN, so "
            "the next `shadow-compare` will run"
        )
    return lines


def main():
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "?"
    head = git("log", "-1", "--format=%h %s")
    dirty = git("status", "--short", "--", "state", "config")

    context = "\n".join(
        [
            "Lead Automation -- current pipeline state (SessionStart hook, local files only,",
            "no Salesforce/LeadFeeder calls). This is the live truth; CLAUDE.md prose may lag.",
            "",
            "git: branch {} | HEAD {}".format(branch, head or "?"),
            "config/config.yaml (live-behaviour keys):",
            *config_lines(),
            "state/watermark.json:",
            *watermark_lines(),
            "shadow backtest:",
            *shadow_lines(),
            "uncommitted changes under state/ config/:",
            *(("  " + line for line in dirty.splitlines()) if dirty else ["  (none)"]),
            "",
            "Not knowable from disk: whether the cron-job.org job (id 8029899) still posts "
            "without inputs (=> scheduled runs stay DRY regardless of the dry_run above). "
            "Ask rather than assume.",
        ]
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - a session must always start
        print("session_state: {}".format(exc), file=sys.stderr)
        sys.exit(0)
