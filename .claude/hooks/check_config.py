#!/usr/bin/env python
"""PostToolUse hook: validate config/config.yaml and echo the safety-relevant keys.

Wired into .claude/settings.json as a PostToolUse hook on ``Edit|Write``; it exits
immediately for any other file.

Why: ``config/config.yaml`` is this project's control plane. ``dry_run``,
``convert_batch_size``, ``live_scope.decisions``, ``live_scope.max_kununu_score``,
``enabled_task_types``, ``owner_id`` and ``converted_status`` decide whether a run
writes to production, how much it converts, and to whom. A typo'd *key* does not
error -- ``Settings`` falls back to a default, and for some of these the default is
"do more than you meant to". So:

* **Block** on a parse error, a missing safety key, or a value the pipeline cannot
  honour (a decision the rule engine never emits, ``skip``/``review`` in the
  conversion scope, ``skip`` in the write-back scope, a live task type that is not
  enabled, an enabled task type with no Subject mapping, an empty
  ``fresh_task_statuses``). ``decisions: []`` (enrich only, 2026-10-19) and
  ``max_kununu_score: null`` (no score axis, 2026-09-28) are legal; an ABSENT key
  for either is not, because ``Settings`` then defaults to converting / a 3.0 cap.
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
        "convert_batch_size": pipeline.get("convert_batch_size"),
        "batch_limit": pipeline.get("batch_limit"),
        "task_lookback_days": pipeline.get("task_lookback_days"),
        "live_scope.decisions": scope.get("decisions"),
        "live_scope.writeback_decisions": scope.get("writeback_decisions"),
        "live_scope.task_types": scope.get("task_types"),
        "live_scope.max_kununu_score": scope.get("max_kununu_score"),
        "live_scope.allow_wide_match": scope.get("allow_wide_match"),
        "enabled_task_types": cfg.get("enabled_task_types"),
        "lf_campaign_task_types": pipeline.get("lf_campaign_task_types"),
        "org_alias": sf.get("org_alias"),
        "owner_id": sf.get("owner_id"),
        "converted_status": sf.get("converted_status"),
    }


def _decision_problems(key, decisions, forbidden_set):
    problems = []
    unknown = sorted(set(decisions) - VALID_DECISIONS)
    if unknown:
        problems.append(
            "pipeline.live_scope.{} contains {} which the rule engine never emits. Valid "
            "decisions: {}.".format(key, unknown, sorted(VALID_DECISIONS))
        )
    forbidden = sorted(set(decisions) & forbidden_set)
    if forbidden:
        problems.append(
            "pipeline.live_scope.{} must never contain {}: 'review' means a candidate "
            "account was surfaced and a human has to adjudicate (it may be enriched, never "
            "auto-converted), and 'skip' means there was no accepted LeadFeeder match at "
            "all.".format(key, forbidden)
        )
    return problems


def validate(cfg):
    """Return a list of blocking problems (empty when the config is usable)."""
    problems = []
    pipeline = cfg.get("pipeline") or {}
    scope = pipeline.get("live_scope") or {}

    for key in ("dry_run",):
        if not isinstance(pipeline.get(key), bool):
            problems.append(
                "pipeline.{} must be an explicit true/false (got {!r}). A missing key "
                "falls back to a default instead of erroring.".format(key, pipeline.get(key))
            )
    batch = pipeline.get("convert_batch_size")
    if not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= 5:
        problems.append(
            "pipeline.convert_batch_size must be an integer 1..5 (got {!r}): >5 SOAP "
            "conversions per call exhausted the per-transaction governor limit on the "
            "2026-07-17 backlog run; 3 is the measured ceiling.".format(batch)
        )

    # Conversion scope. `[]` is legal = enrich only (2026-10-19); an ABSENT key is not,
    # because Settings.live_decisions then falls back to ['create_new'] and converts.
    decisions = scope.get("decisions")
    if not isinstance(decisions, list):
        problems.append(
            "pipeline.live_scope.decisions must be an explicit list (got {!r}); use [] for "
            "enrich-only. A missing key falls back to ['create_new'], i.e. live "
            "conversion.".format(decisions)
        )
    else:
        problems += _decision_problems("decisions", decisions, FORBIDDEN_IN_LIVE_SCOPE)

    # Write-back scope (2026-10-19). Absent = same as `decisions`. `review` is fine
    # here -- enriching converts nothing -- but `skip` has no accepted LF match.
    if "writeback_decisions" in scope:
        wb = scope["writeback_decisions"]
        if not isinstance(wb, list):
            problems.append(
                "pipeline.live_scope.writeback_decisions must be a list (got {!r}).".format(wb)
            )
        else:
            problems += _decision_problems("writeback_decisions", wb, {"skip"})

    # Explicit null = no score axis (Wasil, 2026-09-28). An ABSENT key silently means
    # 3.0, so the key must be present either way.
    if "max_kununu_score" not in scope:
        problems.append(
            "pipeline.live_scope.max_kununu_score is missing, which silently means 3.0. "
            "Set a number, or null for no score axis."
        )
    else:
        score = scope["max_kununu_score"]
        if score is not None and (not isinstance(score, (int, float)) or isinstance(score, bool)):
            problems.append(
                "pipeline.live_scope.max_kununu_score must be a number or null "
                "(got {!r}).".format(score)
            )

    enabled = cfg.get("enabled_task_types") or []

    # Live task types (2026-10-19). Absent = every enabled type. A listed type that is
    # not enabled is never extracted, so it would silently write nothing.
    if "task_types" in scope:
        live_types = scope["task_types"]
        if not isinstance(live_types, list):
            problems.append(
                "pipeline.live_scope.task_types must be a list (got {!r}).".format(live_types)
            )
        else:
            not_enabled = sorted(set(live_types) - set(enabled))
            if not_enabled:
                problems.append(
                    "pipeline.live_scope.task_types {} are not in enabled_task_types, so "
                    "they are never extracted and nothing is written for them.".format(
                        not_enabled
                    )
                )

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
