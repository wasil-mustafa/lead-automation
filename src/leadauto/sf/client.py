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

# FORCE_COLOR=0: an inherited FORCE_COLOR (e.g. from a harness/CI terminal) makes the
# sf CLI ANSI-colorize its --json output, which breaks json.loads (seen 2026-07-10).
_ENV = {**os.environ, "SF_AUTOUPDATE_DISABLE": "true",
        "FORCE_COLOR": "0", "NO_COLOR": "1"}

# A plain `sf data query` sends the SOQL in the GET request URL, so a long IN(...) list
# overflows Salesforce's request-header limit -> HTTP 431 "Request Header Fields Too
# Large". Queries whose raw SOQL exceeds this length are routed through Bulk API 2.0
# (`sf data export bulk`), which carries the query in the POST body. The threshold is on
# raw length (URL-encoding inflates it) and stays well under the ~8 KB header budget, so
# small everyday queries keep using the faster synchronous REST path.
_BULK_QUERY_THRESHOLD = 2000


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

    def _run(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        res = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, env=_ENV
        )
        if check and res.returncode != 0:
            raise SfError(f"sf command failed ({res.returncode}):\n{cmd}\n{res.stderr}")
        return res

    # --- reads ---
    def query(self, soql: str, bulk: bool = False) -> list[dict]:
        """Run SOQL and return records as a list of dicts (attributes stripped).

        Large queries (or `bulk=True`) go through Bulk API 2.0 to dodge the GET-URL/
        header limit that makes long IN(...) lists fail with HTTP 431; smaller queries
        use the faster synchronous REST query.
        """
        soql = " ".join(soql.split())  # collapse whitespace/newlines
        if '"' in soql:
            raise SfError("SOQL must not contain double quotes (use single quotes for literals)")
        if bulk or len(soql) > _BULK_QUERY_THRESHOLD:
            return self._query_bulk(soql)
        cmd = (
            f'sf data query --query "{soql}" --target-org {self.alias} '
            "--result-format json"
        )
        data = json.loads(self._run(cmd).stdout)
        records = data.get("result", {}).get("records", [])
        for r in records:
            r.pop("attributes", None)
        return records

    def _query_bulk(self, soql: str) -> list[dict]:
        """Run SOQL via Bulk API 2.0 (`sf data export bulk`), reading records from a file.

        The query is written to a temp file (`--query-file`) so it never rides in the OS
        command line or an HTTP header. `--result-format json` writes a bare JSON array of
        record objects (no `attributes`, no `{result:{records}}` wrapper).
        """
        fd, q_name = tempfile.mkstemp(prefix="leadauto_query_", suffix=".soql")
        q_path = Path(q_name)
        out_path = Path(q_name + ".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(soql)
            cmd = (
                f'sf data export bulk --query-file "{q_path}" --output-file "{out_path}" '
                f"--result-format json --wait 10 --target-org {self.alias}"
            )
            self._run(cmd)
            text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            records = json.loads(text) if text.strip() else []
        finally:
            for p in (q_path, out_path):
                try:
                    p.unlink()
                except OSError:
                    pass
        if not isinstance(records, list):
            raise SfError(f"Unexpected bulk export output shape: {type(records).__name__}")
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
        """Execute anonymous Apex and return the execution debug log.

        Runs with --json and inspects the structured ExecuteAnonymous result, so a compile
        error or an uncaught runtime exception raises SfError with the real problem instead
        of returning output that merely lacks whatever the caller expected to find in it.
        """
        # Unique temp file (like update_bulk): a fixed name would collide on overlap.
        fd, tmp_name = tempfile.mkstemp(prefix="leadauto_apex_", suffix=".apex")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(apex_code)
            cmd = f'sf apex run --file "{tmp}" --target-org {self.alias} --json'
            # --json reports failures inside the payload (with a non-zero exit code), so
            # run uncheck-ed and diagnose from the structured result, not the exit code.
            res = self._run(cmd, check=False)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        try:
            data = json.loads(res.stdout)
        except (json.JSONDecodeError, TypeError):
            raise SfError(f"sf apex run produced no parseable --json output "
                          f"(exit {res.returncode}):\n{res.stderr or res.stdout}")
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise SfError(f"Unexpected sf apex run --json shape (exit {res.returncode}):\n"
                          f"{res.stdout[:2000]}")
        if not result.get("compiled"):
            raise SfError(f"Apex did not compile: {result.get('compileProblem')}")
        if not result.get("success"):
            raise SfError("Apex run failed: "
                          f"{result.get('exceptionMessage')}\n"
                          f"{result.get('exceptionStackTrace') or ''}")
        return result.get("logs") or ""
