#!/usr/bin/env python
"""PreToolUse guard: keep Read/Edit/Write off secrets, bulk data and pipeline state.

Wired into .claude/settings.json on ``Read|Edit|Write|NotebookEdit``. Written in
Python rather than a shell one-liner because there is no ``jq`` on PATH here, and
Python is a hard prerequisite of this repo anyway.

Three tiers, because they deserve different answers:

* ``deny`` (credentials) -- ``.env`` and anything auth-shaped. All local plaintext
  copies were deliberately deleted 2026-07-05; keys live only in GitHub Secrets.
  There is never a good reason to read one into a transcript. For a local run,
  set the env var ad hoc (see the /pipeline-dry-run skill).
* ``deny`` (pipeline-owned state, WRITES ONLY) -- ``state/*.json{,l}`` are emitted
  by the pipeline and committed by CI. Hand-editing them is not a fix, it is
  fabricated evidence: ``predictions.jsonl`` and ``shadow_results.jsonl`` ARE the
  shadow backtest that gates widening the live scope (and, since 2026-11-01, the
  conversion sweep's decision source), ``watermark.json`` decides which Tasks get
  re-processed or skipped for good. Reading them is fine (subject to the ``ask``
  tier below). (The ``review/approved_*.csv`` rule went with the review gate.)
* ``ask`` (bulk data / run output) -- ``*.csv``, ``*.xlsx``, ``*.jsonl``, ``out/``.
  Legitimately useful sometimes, so this is a speed bump, not a wall. It also
  stops a stray read of ``kununu_profiles*.jsonl`` (~551 MB) from swallowing the
  entire context window.

Everything in the deny tiers is either gitignored or tracked-on-purpose; that is
about what gets *committed*, which is a different failure from what gets read or
clobbered in a session.

Fails open (allow, plus a stderr note) on an unexpected payload: this guards
against accidents, not an adversary, and a crashing hook that blocks every file
operation would be worse than the accident it prevents.
"""

import json
import sys
from fnmatch import fnmatch
from pathlib import PurePath

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Credentials -- hard deny, read or write.
SECRET_GLOBS = (
    ".env",
    ".env.*",
    "auth.txt",
    "*sfdx_auth*",
    "*_api_key.txt",
    "*.key",
    "*.pem",
)

# Pipeline-owned artifacts -- deny WRITES, allow reads.
STATE_WRITE_GLOBS = (
    "watermark.json",
    "predictions.jsonl",
    "shadow_results.jsonl",
    "shadow_state.json",
)

# Bulk data / run output -- prompt instead of blocking outright.
DATA_GLOBS = (
    "*.csv",
    "*.xlsx",
    "*.xlsm",
    "*.jsonl",
    "*.jsonl.part",
)

# Any path under one of these directories is run output or legacy customer data.
DATA_DIRS = ("out", "artifacts", "export", "import", "pq", ".sf", ".sfdx")

# The session scratchpad is temp-by-design; guarding it just gets in the way.
SCRATCHPAD_MARKERS = ("temp/claude", "temp\\claude")

STATE_REASON = (
    "{} is written by the pipeline and committed by CI -- it is not a file to edit by "
    "hand. {} Change the code that emits it (or run the stage) and let the run rewrite "
    "the file. Blocked by .claude/hooks/guard_sensitive_files.py."
)
STATE_WHY = {
    "watermark.json": "It decides which Tasks are re-processed or skipped for good.",
    "predictions.jsonl": "It IS the shadow backtest that gates widening the live scope.",
    "shadow_results.jsonl": "It IS the scored shadow evidence behind go-live decisions.",
    "shadow_state.json": (
        "The shadow-compare gate fails OPEN on an unreadable file, so a botched edit "
        "silently re-runs the compare on every dispatch."
    ),
}


def classify(raw_path, tool_name):
    """Return (decision, reason), or (None, None) when the path is fine."""
    path = PurePath(raw_path)
    name = path.name.lower()
    lowered = str(path).lower().replace("\\", "/")
    is_write = tool_name in WRITE_TOOLS

    if any(marker.replace("\\", "/") in lowered for marker in SCRATCHPAD_MARKERS):
        return None, None

    for pattern in SECRET_GLOBS:
        if fnmatch(name, pattern):
            return (
                "deny",
                "{} holds Salesforce/LeadFeeder/kununu credentials. Blocked by the "
                "project PreToolUse guard (.claude/hooks/guard_sensitive_files.py). "
                "Secrets live in GitHub Secrets only; for a local run set the env var "
                "ad hoc (see the /pipeline-dry-run skill) instead of reading the "
                "file.".format(path.name),
            )

    parts_lower = [part.lower() for part in path.parts]

    if is_write:
        for pattern in STATE_WRITE_GLOBS:
            if fnmatch(name, pattern):
                return "deny", STATE_REASON.format(path.name, STATE_WHY.get(pattern, ""))

    # scripts/ == the legacy Scripts/ on case-insensitive Windows: it holds a live API key.
    if "scripts" in parts_lower and name not in (
        "leadfeeder_probe.py",
        "control_check_alt_names.py",
    ):
        return (
            "ask",
            "{} is under scripts/, which on case-insensitive Windows IS the legacy "
            "Scripts/ directory (contains a live API key). Only leadfeeder_probe.py and "
            "control_check_alt_names.py are tracked. Confirm before "
            "opening.".format(path.name),
        )

    for directory in DATA_DIRS:
        if directory in parts_lower:
            return (
                "ask",
                "{} is under {}/, which holds run output and customer data. Confirm "
                "before opening it.".format(path.name, directory),
            )

    for pattern in DATA_GLOBS:
        if fnmatch(name, pattern):
            return (
                "ask",
                "{} is a customer-data / run-output file. Every *.csv, *.xlsx and "
                "*.jsonl here carries Lead/Account data, and kununu_profiles*.jsonl is "
                "~551 MB and will not fit in context -- summarize it with a script "
                "instead of reading it. Confirm before opening.".format(path.name),
            )

    return None, None


def main():
    try:
        # lstrip the BOM: harmless from Claude Code, present when a Windows shell
        # pipes a test payload in.
        payload = json.loads(sys.stdin.read().lstrip("﻿"))
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        print("guard_sensitive_files: unreadable payload: {}".format(exc), file=sys.stderr)
        return 0

    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw_path:
        return 0

    decision, reason = classify(raw_path, payload.get("tool_name") or "")
    if decision is None:
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
