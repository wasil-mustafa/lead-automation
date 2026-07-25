#!/usr/bin/env python
"""PostToolUse hook: validate config/config.yaml and echo the safety-relevant keys.

Wired into .claude/settings.json as a PostToolUse hook on ``Edit|Write``; it exits
immediately for any other file.

Why: ``config/config.yaml`` is this project's control plane. ``dry_run``,
``review_gate``, ``live_scope.decisions``, ``live_scope.max_kununu_score``,
``enabled_task_types``, ``owner_id`` and ``converted_status`` decide whether a run
writes to production, how much it converts, and to whom. A typo'd *key* does not
error -- ``Settings`` falls back to a default, and for some of these the default is
"do more than you meant to". So:

* **Block** on a parse error, a missing safety key, or a value the pipeline cannot
  honour (a decision the rule engine never emits, ``skip``/``review`` in the live
  scope, an enabled task type with no Subject mapping, an empty
  ``fresh_task_statuses``).
* Otherwise **echo** the safety keys plus a before/after diff against
  ``git show HEAD:config/config.yaml``, so a widened live scope or a flipped
  ``dry_run`` is visible at the moment it changes rather than at the next run.

Written in Python, not a shell pipeline, because there is no ``jq`` on PATH here.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_REL = "config/config.yaml"

# Decisions the rule engine can actually emit (match.py).
VALID_DECISIONS = {"link", "review", "create_new", "skip"}
# ...and the two it must never auto-convert: a candidate account needs a human,
# and a skip means no accepted LeadFeeder match at all.
FORBIDDEN_IN_LIVE_SCOPE = {"review", "skip"}


def load_yaml(text):
    import yaml  # noqa: PLC0415 - a project dependency; absence is itself a finding

    return yaml.safe_load(text) or {}


def safety_view(cfg):
    """The handful of keys worth echoing after every config edit."""
    pipeline = cfg.get("pipeline") or {}
    scope = pipeline.get("live_scope") or {}
    sf = cfg.get("salesforce") or {}
    return {
        "dry_run": pipeline.get("dry_run"),
        "review_gate": pipeline.get("review_gate"),
        "batch_limit": pipeline.get("batch_limit"),
        "task_lookback_days": pipeline.get("task_lookback_days"),
        "live_scope.decisions": scope.get("decisions"),
        "live_scope.max_kununu_score": scope.get("max_kununu_score"),
        "enabled_task_types": cfg.get("enabled_task_types"),
        "org_alias": sf.get("org_alias"),
        "owner_id": sf.get("owner_id"),
        "converted_status": sf.get("converted_status"),
    }


def validate(cfg):
    """Return a list of blocking problems (empty when the config is usable)."""
    problems = []
    pipeline = cfg.get("pipeline") or {}
    scope = pipeline.get("live_scope") or {}

    for key in ("dry_run", "review_gate"):
        if not isinstance(pipeline.get(key), bool):
            problems.append(
                "pipeline.{} must be an explicit true/false (got {!r}). A missing key "
                "falls back to a default instead of erroring.".format(key, pipeline.get(key))
            )

    decisions = scope.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        problems.append(
            "pipeline.live_scope.decisions must be a non-empty list (got {!r}). Empty or "
            "missing means the live-scope gate has nothing to admit -- or, worse, is "
            "read as absent.".format(decisions)
        )
    else:
        unknown = sorted(set(decisions) - VALID_DECISIONS)
        if unknown:
            problems.append(
                "pipeline.live_scope.decisions contains {} which the rule engine never "
                "emits. Valid decisions: {}.".format(unknown, sorted(VALID_DECISIONS))
            )
        forbidden = sorted(set(decisions) & FORBIDDEN_IN_LIVE_SCOPE)
        if forbidden:
            problems.append(
                "pipeline.live_scope.decisions must never contain {}: 'review' means a "
                "candidate account was surfaced and a human has to adjudicate, and "
                "'skip' means there was no accepted LeadFeeder match at all. Neither may "
                "auto-convert.".format(forbidden)
            )

    score = scope.get("max_kununu_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        problems.append(
            "pipeline.live_scope.max_kununu_score must be a number (got {!r}). It is the "
            "human 'No Demand' line and the gate's only quality bound.".format(score)
        )

    enabled = cfg.get("enabled_task_types") or []
    mapped = set((cfg.get("task_types") or {}).values())
    unmapped = sorted(set(enabled) - mapped)
    if unmapped:
        problems.append(
            "enabled_task_types {} have no Subject mapped to them in task_types, so they "
            "silently process nothing.".format(unmapped)
        )

    if not (cfg.get("fresh_task_statuses") or []):
        problems.append(
            "fresh_task_statuses is empty, which DISABLES the freshness filter entirely "
            "-- the run would then pick up tasks humans already handled (the org flags "
            "'Abgeschlossen' as IsClosed=false, so there is no safety net behind this)."
        )

    return problems


def head_view():
    """safety_view of the committed config, or None if it cannot be read."""
    try:
        completed = subprocess.run(
            ["git", "show", "HEAD:" + CONFIG_REL],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return None
        return safety_view(load_yaml(completed.stdout))
    except Exception:  # noqa: BLE001 - best-effort context, never a blocker
        return None


def main():
    try:
        payload = json.loads(sys.stdin.read().lstrip("﻿"))
    except Exception as exc:  # noqa: BLE001 - never let a bad payload wedge editing
        print("check_config: unreadable payload: {}".format(exc), file=sys.stderr)
        return 0

    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    raw_path = tool_response.get("filePath") or tool_input.get("file_path") or ""
    normalized = raw_path.replace("\\", "/").lower()
    if not normalized.endswith(CONFIG_REL):
        return 0

    path = Path(raw_path)
    try:
        cfg = load_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a broken config IS the finding
        json.dump(
            {
                "decision": "block",
                "reason": (
                    "config/config.yaml no longer parses as YAML, so every run would "
                    "fail at Settings.load: {}".format(exc)
                ),
                "systemMessage": "config.yaml does not parse.",
            },
            sys.stdout,
        )
        return 0

    problems = validate(cfg)
    if problems:
        json.dump(
            {
                "decision": "block",
                "reason": (
                    "config/config.yaml has problems that would not surface as an error "
                    "at runtime -- the pipeline would fall back to a default or process "
                    "nothing:\n\n- " + "\n- ".join(problems)
                ),
                "systemMessage": "config.yaml failed validation ({} problem(s)).".format(
                    len(problems)
                ),
            },
            sys.stdout,
        )
        return 0

    now = safety_view(cfg)
    before = head_view()
    lines = []
    for key, value in now.items():
        if before is not None and before.get(key) != value:
            lines.append("  {}: {!r}  <-- CHANGED from {!r}".format(key, value, before.get(key)))
        else:
            lines.append("  {}: {!r}".format(key, value))
    note = "" if before is not None else "\n  (committed config unavailable; no diff shown)"

    json.dump(
        {
            "systemMessage": "config.yaml validated. Live-behaviour keys:\n"
            + "\n".join(lines)
            + note,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
