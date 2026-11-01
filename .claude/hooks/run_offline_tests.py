#!/usr/bin/env python
"""PostToolUse hook: run the offline pytest suite after a Python edit.

Wired into .claude/settings.json as a PostToolUse hook on ``Edit|Write``.

Why this is worth a hook here: nearly every invariant this project documents in
prose is actually enforced by a test -- the rule-engine order and the "no accepted
LF match => terminal skip" invariant, the write-back field spec (revenue in
millions, uppercase ISO2, the org's 14 real employee bands), the LF name-agreement
gate, the bulk stringly-typed coercions, the live-scope gate failing closed on a
missing kununu score. The suite mocks every I/O boundary, so it touches no
Salesforce, no LeadFeeder and no clock: **507 tests in ~4 s**. That is cheap
enough to pay on every edit, and it turns a documented invariant into one that
cannot silently regress.

The whole suite runs rather than a per-module subset: at 4 s, routing by import
graph would save nothing and would itself be a thing that rots. Non-Python edits
and edits outside the repo (scratchpad, another project) run nothing.

One failure mode this specifically catches: a ``dry_run=False`` ``run_pipeline``
test written without ``state_path=tmp_path/...`` or ``predictions_path=tmp_path/...``
— it would write a fake watermark or prediction into the repo's real ``state/``
dir, which CI commits and the conversion sweep reads as a decision.

Written in Python, not a shell pipeline, because there is no ``jq`` on PATH here.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Only these trees are covered by the suite; editing docs or hooks runs nothing.
COVERED_ROOTS = ("src", "tests")

# Keep the feedback fed back to Claude bounded -- a full failure dump can be huge.
MAX_REPORT_CHARS = 3000


def is_covered(edited):
    try:
        relative = edited.relative_to(REPO_ROOT)
    except ValueError:
        return False  # outside the repo (scratchpad, sibling project)
    return bool(relative.parts) and relative.parts[0] in COVERED_ROOTS


def run_suite():
    """Run pytest. Returns None on pass, a bounded report string on failure."""
    env = os.environ.copy()
    # pyproject sets pythonpath=src for pytest, but be explicit for safety, and drop
    # FORCE_COLOR so the report fed back to Claude is not ANSI-wrapped.
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("FORCE_COLOR", None)
    env["NO_COLOR"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    return output.strip()[-MAX_REPORT_CHARS:]


def main():
    try:
        payload = json.loads(sys.stdin.read().lstrip("﻿"))
    except Exception as exc:  # noqa: BLE001 - never let a bad payload wedge editing
        print("run_offline_tests: unreadable payload: {}".format(exc), file=sys.stderr)
        return 0

    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    raw_path = tool_response.get("filePath") or tool_input.get("file_path") or ""
    if not raw_path.endswith(".py") or not is_covered(Path(raw_path).resolve()):
        return 0

    report = run_suite()
    if report is None:
        return 0

    json.dump(
        {
            "decision": "block",
            "reason": (
                "The offline pytest suite now fails. These tests pin the documented "
                "invariants of this project (rule-engine order and the no-LF-match => "
                "skip invariant, the write-back field spec, the LF name-agreement gate, "
                "the bulk stringly-typed coercions, the live-scope gate failing closed), "
                "so a failure here usually means a real behaviour change rather than a "
                "stale test. Fix the code, or change the test deliberately in the same "
                "edit.\n\n" + report
            ),
            "systemMessage": "Offline pytest suite failed after this edit.",
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
