#!/usr/bin/env python
"""PreToolUse guard on Bash: live writes, wrong org, and API-call cost.

Wired into .claude/settings.json as a PreToolUse hook on ``Bash``. One script for
three checks so a command costs one process spawn, not three.

Why each check exists -- all three are failures this repo has actually seen or
documented, and none of them announce themselves:

1. **Live writes.** A dry run is free; a live run PATCHes production Leads and
   calls SOAP ``convertLead()``, which cannot be undone. The only brake today is
   "dispatch deliberately", i.e. human memory. Detection is deliberately
   *explicit-signal only* (``--no-dry-run``, ``dry_run=false``) plus the
   config-derived case, so it does not cry wolf on the normal dry command.
2. **`--batch-size` > 3.** Measured, not theoretical: batched SOAP
   ``convertLead()`` runs one transaction per call and ~5 conversions' cascaded
   triggers/flows exhaust a per-transaction governor limit. The bulk call then
   fails and the individual retries share the already-spent context, so the batch
   dies as a unit. Hard deny.
3. **Wrong org.** The alias is ``headless`` (production). ``sf data``/``sf apex``
   without an explicit ``--target-org`` uses whatever default is configured -- and
   the repo guide warns "NOT ``myOrg``", which means the mistake has been made.
4. **API budget.** The org's allocation is shared with other teams' integrations,
   so consumption is a standing constraint -- but nothing counts it during a
   session. This keeps a rough running estimate and speaks up once, past a
   threshold. Advisory only: it never blocks.

Fails open on anything unexpected: a crashing hook that blocks every Bash call
would be worse than the accident it prevents.
"""

import json
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"

MAX_LIVE_BATCH_SIZE = 3
# Rough per-run cost estimates (see CLAUDE.md "API-call budget"). Advisory only.
RUN_COST = {"stage1": 12, "stage2": 8, "backtest-skipped": 12, "shadow-compare": 9}
QUERY_COST = 2  # a bulk query job: create + one result page
ESTIMATE_WARN_AT = 25

LEADAUTO_RE = re.compile(r"leadauto\s+(stage1|stage2|backtest-skipped|shadow-compare)")
BATCH_SIZE_RE = re.compile(r"--batch-size[=\s]+(\d+)")
LIVE_MARKERS = (
    "--no-dry-run",
    "dry_run=false",
    'dry_run":"false"',
    "dry_run: false",
    "dry_run=False",
)
SF_DATA_WRITE_RE = re.compile(r"\bsf\s+data\s+(create|update|delete|upsert|import|resume)")
SF_ORG_TOUCHING_RE = re.compile(r"\bsf\s+(data|apex)\b")
SF_TARGET_RE = re.compile(r"(--target-org|\s-o\s)")
SF_QUERY_RE = re.compile(r"\bsf\s+data\s+(query|export)")


def config_dry_run_default():
    """pipeline.dry_run from config.yaml, or None when it cannot be determined.

    Parsed with pyyaml when importable, else by regex -- the hook must work even
    from an interpreter without the project's dependencies installed.
    """
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import yaml  # noqa: PLC0415 - optional dependency, see docstring

        loaded = yaml.safe_load(text) or {}
        value = (loaded.get("pipeline") or {}).get("dry_run")
        if isinstance(value, bool):
            return value
    except Exception:  # noqa: BLE001 - fall through to the regex
        pass
    match = re.search(r"^\s*dry_run:\s*(true|false)", text, re.MULTILINE | re.IGNORECASE)
    if match:
        return match.group(1).lower() == "true"
    return None


def deny(reason):
    return "deny", reason


def ask(reason):
    return "ask", reason


def classify(command):
    """Return (decision, reason), or (None, None) to allow silently."""
    lowered = command.lower()
    run_match = LEADAUTO_RE.search(lowered)
    live_marker = next((m for m in LIVE_MARKERS if m.lower() in lowered), None)

    batch = BATCH_SIZE_RE.search(command)
    if batch and int(batch.group(1)) > MAX_LIVE_BATCH_SIZE and "--dry-run" not in lowered:
        return deny(
            "--batch-size {} on a non-dry convert. Measured on this org (backlog "
            "conversion, 2026-07-17): batched SOAP convertLead() runs ONE transaction "
            "per call, and around 5 conversions' cascaded triggers/flows exhaust a "
            "per-transaction governor limit -- the bulk call fails and the individual "
            "retries share the already-spent context, so the whole batch dies. Keep "
            "--batch-size {} or lower for live runs.".format(
                batch.group(1), MAX_LIVE_BATCH_SIZE
            )
        )

    if live_marker and (run_match or "workflow run" in lowered or "gh api" in lowered):
        stage = run_match.group(1) if run_match else "the dispatched workflow"
        return ask(
            "This is a LIVE run ({} found), not a dry run: {} writes to PRODUCTION "
            "Salesforce org 'headless'. Lead write-back is a real PATCH and Stage 2 "
            "conversions are IRREVERSIBLE. Confirm this is deliberate, and check "
            "--batch-size <= {} if it converts.".format(
                live_marker, stage, MAX_LIVE_BATCH_SIZE
            )
        )

    if run_match and "--dry-run" not in lowered and not live_marker:
        stage = run_match.group(1)
        if stage in ("stage1", "stage2") and config_dry_run_default() is False:
            return ask(
                "config/config.yaml now has pipeline.dry_run: false, so this bare "
                "`{}` command is a LIVE run against production org 'headless' even "
                "without --no-dry-run. Pass --dry-run explicitly, or confirm the live "
                "run is deliberate.".format(stage)
            )

    if SF_ORG_TOUCHING_RE.search(lowered) and not SF_TARGET_RE.search(command):
        return deny(
            "`sf data`/`sf apex` without --target-org runs against whatever org is "
            "currently the CLI default. This project's org is the production alias "
            "'headless' (NOT 'myOrg'). Add --target-org headless."
        )

    if SF_DATA_WRITE_RE.search(lowered):
        return ask(
            "This is a WRITE to production Salesforce via the sf CLI. Record updates in "
            "this project go through SfClient.update_records (one "
            "PATCH /composite/sobjects per <=200 records, inline per-row errors) -- the "
            "CLI bulk path was deliberately removed 2026-07-18 because it cost 5-7 raw "
            "requests plus polling. Confirm this is a one-off manual fix, not something "
            "that belongs in the pipeline."
        )

    return None, None


def bump_estimate(command, session_id):
    """Update the session's rough API-call estimate; return a warning or None."""
    lowered = command.lower()
    cost = QUERY_COST * len(SF_QUERY_RE.findall(lowered))
    run_match = LEADAUTO_RE.search(lowered)
    if run_match:
        cost += RUN_COST.get(run_match.group(1), 0)
    if not cost:
        return None

    path = Path(tempfile.gettempdir()) / "leadauto-api-estimate-{}.json".format(
        re.sub(r"[^A-Za-z0-9_-]", "", session_id or "nosession")[:64]
    )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a fresh or corrupt counter just starts over
        state = {"estimate": 0, "warned": False}

    state["estimate"] = int(state.get("estimate", 0)) + cost
    warning = None
    if state["estimate"] >= ESTIMATE_WARN_AT and not state.get("warned"):
        state["warned"] = True
        warning = (
            "API budget: this session has spent roughly {} Salesforce API calls "
            "(estimate). The org's allocation is shared with business-critical "
            "integrations, so prefer /composite (query_many), one batched IN (...) over "
            "per-record loops, and re-reading artifacts already on disk over "
            "re-querying the org.".format(state["estimate"])
        )
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return warning


def main():
    try:
        # lstrip the BOM: harmless from Claude Code, present when a Windows shell
        # pipes a test payload in.
        payload = json.loads(sys.stdin.read().lstrip("﻿"))
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        print("guard_bash: unreadable payload: {}".format(exc), file=sys.stderr)
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return 0

    decision, reason = classify(command)
    # A denied command never runs, so it must not count against the estimate.
    warning = None if decision == "deny" else bump_estimate(
        command, payload.get("session_id") or ""
    )

    output = {}
    if decision:
        output["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    if warning:
        output["systemMessage"] = warning
    if output:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
