"""Thin wrapper over the `sf` CLI: SOQL queries, bulk field updates, anonymous Apex.

Uses shell=True with a single command string (portable across Windows cmd and Linux
bash: SOQL string literals use single quotes, so a double-quoted --query value is safe
on both). SOQL/Apex payloads that are large or multi-line are written to temp files.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path

_ENV = {**os.environ, "SF_AUTOUPDATE_DISABLE": "true"}


class SfError(RuntimeError):
    pass


def parse_bulk_update_result(stdout: str) -> dict:
    """Parse `sf data update bulk --json` output into a failure summary.

    Returns {failed: int, failures: [{id, error}], confirmed: bool}. `confirmed` is False
    when the JSON shape is not recognized — callers must then NOT assume a clean success.

    Shape below is the documented best-understanding of Bulk API 2.0 output; re-confirm it
    against a live run before relying on row-level failure detail (see writeback/CLAUDE.md).
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {"failed": 0, "failures": [], "confirmed": False}
    if not isinstance(data, dict):
        return {"failed": 0, "failures": [], "confirmed": False}
    result = data.get("result", data)
    if not isinstance(result, dict):
        return {"failed": 0, "failures": [], "confirmed": False}

    job = result.get("jobInfo") or {}
    records = result.get("records") or {}
    failed_results = records.get("failedResults") or result.get("failedResults") or []
    failures = []
    for r in failed_results:
        if isinstance(r, dict):
            failures.append({
                "id": r.get("sf__Id") or r.get("id") or r.get("Id"),
                "error": r.get("sf__Error") or r.get("error") or "",
            })
    confirmed = bool(job) or bool(records)
    failed_count = job.get("numberRecordsFailed")
    failed = int(failed_count) if failed_count is not None else len(failures)
    return {"failed": failed, "failures": failures, "confirmed": confirmed}


class SfClient:
    def __init__(self, alias: str, api_version: str = "66.0", logger=None):
        self.alias = alias
        self.api_version = api_version
        self.log = logger

    def _run(self, cmd: str) -> subprocess.CompletedProcess:
        res = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, env=_ENV
        )
        if res.returncode != 0:
            raise SfError(f"sf command failed ({res.returncode}):\n{cmd}\n{res.stderr}")
        return res

    # --- reads ---
    def query(self, soql: str, bulk: bool = False) -> list[dict]:
        """Run SOQL and return records as a list of dicts (attributes stripped)."""
        soql = " ".join(soql.split())  # collapse whitespace/newlines
        if '"' in soql:
            raise SfError("SOQL must not contain double quotes (use single quotes for literals)")
        flags = "--bulk --wait 10" if bulk else ""
        cmd = (
            f'sf data query --query "{soql}" --target-org {self.alias} '
            f"{flags} --result-format json"
        )
        data = json.loads(self._run(cmd).stdout)
        records = data.get("result", {}).get("records", [])
        for r in records:
            r.pop("attributes", None)
        return records

    # --- writes ---
    def update_bulk(self, sobject: str, records: list[dict]) -> dict:
        """Bulk-update existing records. Each dict must contain 'Id' plus fields to set.

        Returns {processed, failed, failures, confirmed}. A non-zero CLI exit (whole-job
        failure) raises; row-level failures are parsed from the --json payload so a partial
        failure is never reported as a clean success.
        """
        if not records:
            return {"processed": 0, "failed": 0, "failures": [], "confirmed": True}
        cols: list[str] = []
        for r in records:
            for k in r:
                if k not in cols:
                    cols.append(k)
        if "Id" not in cols:
            raise SfError("update_bulk requires 'Id' in every record")
        # Unique temp file: a fixed name would collide if two runs overlap.
        fd, tmp_name = tempfile.mkstemp(prefix=f"leadauto_update_{sobject}_", suffix=".csv")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
                writer.writeheader()
                writer.writerows(records)
            cmd = (
                f'sf data update bulk --sobject {sobject} --file "{tmp}" '
                f"--target-org {self.alias} --line-ending LF --wait 10 --json"
            )
            res = self._run(cmd)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        parsed = parse_bulk_update_result(res.stdout)
        return {
            "processed": len(records), "failed": parsed["failed"],
            "failures": parsed["failures"], "confirmed": parsed["confirmed"],
        }

    def apex_run(self, apex_code: str) -> str:
        """Execute anonymous Apex and return combined stdout."""
        tmp = Path(tempfile.gettempdir()) / "leadauto_run.apex"
        tmp.write_text(apex_code, encoding="utf-8")
        cmd = f'sf apex run --file "{tmp}" --target-org {self.alias}'
        return self._run(cmd).stdout
