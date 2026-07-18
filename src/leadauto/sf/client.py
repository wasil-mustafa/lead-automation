"""Wrapper over the `sf` CLI (SOQL queries, anonymous Apex) plus direct HTTP calls
riding the CLI's cached session: REST sObject Collections for record updates and the
SOAP Partner API `convertLead()` for lead conversion.

Uses shell=True with a single command string (portable across Windows cmd and Linux
bash: SOQL string literals use single quotes, so a double-quoted --query value is safe
on both). SOQL/Apex payloads that are large or multi-line are written to temp files.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests

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

# The sf CLI intermittently dies with a non-zero exit and NO output at all (the same
# Node-level flake both workflows retry the login for) — seen live 2026-07-10 on the
# first Stage-2 conversion dispatch: `sf apex run --json` exit 1, empty stdout.
# apex_run retries only that no-output case; a parseable failure is real and never
# re-run. Retrying conversion Apex is safe: it only converts IsConverted=false leads
# and callers reconcile alreadyConverted rows.
_APEX_RUN_ATTEMPTS = 3
_APEX_RETRY_DELAY_S = 10


class SfError(RuntimeError):
    pass


# Last line of defense for ANY Id embedded in a generated SOQL literal or a SOAP
# envelope: only a plain alphanumeric Salesforce Id (≤18 chars) is allowed through.
# Shape validation (15/18 chars) happens earlier at the CSV boundary
# (stage2_convert.targets_from_approved); this rejects anything that could inject or crash.
_SF_ID_RE = re.compile(r"[A-Za-z0-9]{1,18}")


def guard_sf_id(value: str) -> str:
    """Return `value` unchanged, or raise ValueError if it is not a bare Salesforce Id."""
    if not _SF_ID_RE.fullmatch(value or ""):
        raise ValueError(f"Refusing to embed non-alphanumeric Salesforce Id: {value!r}")
    return value


# SOAP Partner API convertLead accepts at most 100 LeadConvert elements per call.
_SOAP_CONVERT_MAX = 100

# REST sObject Collections accepts at most 200 records per request.
_COLLECTIONS_MAX = 200


class SfClient:
    def __init__(self, alias: str, api_version: str = "66.0", logger=None):
        self.alias = alias
        self.api_version = api_version
        self.log = logger
        self._auth: dict | None = None

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
    def update_records(self, sobject: str, records: list[dict]) -> dict:
        """Update existing records over REST sObject Collections (one PATCH per ≤200
        records, allOrNone=false) — replaces the Bulk API job the CLI ran per update
        (5-7 round trips + polling each; API-call reduction 2026-07-18).

        Each dict must contain 'Id' plus the fields to set. Returns
        {processed, failed, failures: [{id, error}], confirmed} — the same contract
        the retired update_bulk had; `confirmed` is always True because the response carries one
        inline per-row result. Failed rows come back with a null id, so failures are
        keyed to the request-order record Id. A 401 (stale cached CLI token) gets one
        auth re-fetch + retry, like convert_lead.
        """
        if not records:
            return {"processed": 0, "failed": 0, "failures": [], "confirmed": True}
        if any(not r.get("Id") for r in records):
            raise SfError("update_records requires 'Id' in every record")
        failures: list[dict] = []
        for start in range(0, len(records), _COLLECTIONS_MAX):
            chunk = records[start:start + _COLLECTIONS_MAX]
            payload = {"allOrNone": False,
                       "records": [{"attributes": {"type": sobject}, **r} for r in chunk]}
            rows = self._patch_collections(payload)
            for rec, row in zip(chunk, rows):
                if row.get("success"):
                    continue
                msg = "; ".join(f"{e.get('statusCode', '')}: {e.get('message', '')}"
                                for e in row.get("errors") or []) or "unknown error"
                failures.append({"id": row.get("id") or rec["Id"], "error": msg})
        return {"processed": len(records), "failed": len(failures),
                "failures": failures, "confirmed": True}

    def _patch_collections(self, payload: dict) -> list[dict]:
        """One sObject-Collections PATCH; retries once with fresh auth on HTTP 401."""
        response = self._patch_once(payload, refresh=False)
        if response.status_code == 401:
            if self.log:
                self.log.warning("Collections PATCH got 401; refreshing auth and retrying")
            response = self._patch_once(payload, refresh=True)
        if response.status_code >= 300:
            raise SfError(f"sObject Collections PATCH failed "
                          f"(HTTP {response.status_code}):\n{response.text[:2000]}")
        rows = json.loads(response.text)
        if not isinstance(rows, list):
            raise SfError(f"Unexpected sObject Collections response shape:\n"
                          f"{response.text[:2000]}")
        return rows

    def _patch_once(self, payload: dict, *, refresh: bool):
        auth = self.auth_info(refresh=refresh)
        url = (f"{auth['instance_url']}/services/data/v{self.api_version}"
               "/composite/sobjects")
        return requests.patch(url, data=json.dumps(payload), headers={
            "Authorization": f"Bearer {auth['access_token']}",
            "Content-Type": "application/json",
        }, timeout=120)

    # --- SOAP conversion ---
    def auth_info(self, refresh: bool = False) -> dict:
        """Access token + instance URL from `sf org display --json`, cached per client.

        The CLI's cached org auth is the session the SOAP call rides on; `refresh=True`
        re-fetches after an INVALID_SESSION_ID fault.
        """
        if self._auth is not None and not refresh:
            return self._auth
        cmd = f"sf org display --target-org {self.alias} --json"
        data = json.loads(self._run(cmd).stdout)
        result = data.get("result") or {}
        token, url = result.get("accessToken"), result.get("instanceUrl")
        if token and token.startswith("[REDACTED"):
            # sf CLI >= ~2.141 masks accessToken in `org display`; fetch the real one.
            cmd = f"sf org auth show-access-token --target-org {self.alias} --json"
            tok_data = json.loads(self._run(cmd).stdout)
            token = (tok_data.get("result") or {}).get("accessToken")
        if not token or not url:
            raise SfError("sf org display returned no accessToken/instanceUrl:\n"
                          f"{json.dumps(result)[:500]}")
        self._auth = {"access_token": token, "instance_url": url.rstrip("/")}
        return self._auth

    def convert_lead(self, conversions: list[dict], *, converted_status: str,
                     owner_id: str | None) -> list[dict]:
        """Convert leads via the SOAP Partner API (the org forbids anonymous Apex
        conversion for this user; SOAP honors his Convert Leads permission).

        Each conversion dict: lead_id + optional account_id/contact_id. Returns one
        result row per conversion, in request order, with a nil leadId (structural
        rejections) backfilled from that order. An INVALID_SESSION_ID fault triggers
        one auth re-fetch + retry; any other fault raises SfError.
        """
        if not conversions:
            return []
        if len(conversions) > _SOAP_CONVERT_MAX:
            raise SfError(f"convert_lead accepts at most {_SOAP_CONVERT_MAX} conversions "
                          f"per call, got {len(conversions)}")
        try:
            return self._convert_once(conversions, converted_status, owner_id, refresh=False)
        except SfError as exc:
            if "INVALID_SESSION_ID" not in str(exc):
                raise
            if self.log:
                self.log.warning("SOAP session invalid; refreshing auth and retrying")
            return self._convert_once(conversions, converted_status, owner_id, refresh=True)

    def _convert_once(self, conversions: list[dict], converted_status: str,
                      owner_id: str | None, *, refresh: bool) -> list[dict]:
        """One convertLead round trip; nil leadIds backfilled from request order."""
        from .soap import build_convert_envelope, parse_convert_response

        auth = self.auth_info(refresh=refresh)
        envelope = build_convert_envelope(
            auth["access_token"], conversions,
            converted_status=converted_status, owner_id=owner_id)
        url = f"{auth['instance_url']}/services/Soap/u/{self.api_version}"
        response = requests.post(url, data=envelope.encode("utf-8"), headers={
            "Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""',
        }, timeout=120)
        rows = parse_convert_response(response.text)
        for conversion, row in zip(conversions, rows):
            if not row.get("leadId"):
                row["leadId"] = conversion["lead_id"]
        return rows

    def apex_run(self, apex_code: str) -> str:
        """Execute anonymous Apex and return the execution debug log.

        Runs with --json and inspects the structured ExecuteAnonymous result, so a compile
        error or an uncaught runtime exception raises SfError with the real problem instead
        of returning output that merely lacks whatever the caller expected to find in it.
        """
        # Unique temp file (like _query_bulk): a fixed name would collide on overlap.
        fd, tmp_name = tempfile.mkstemp(prefix="leadauto_apex_", suffix=".apex")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(apex_code)
            cmd = f'sf apex run --file "{tmp}" --target-org {self.alias} --json'
            # --json reports failures inside the payload (with a non-zero exit code), so
            # run uncheck-ed and diagnose from the structured result, not the exit code.
            data = None
            for attempt in range(1, _APEX_RUN_ATTEMPTS + 1):
                res = self._run(cmd, check=False)
                try:
                    data = json.loads(res.stdout)
                    break
                except (json.JSONDecodeError, TypeError):
                    if self.log:
                        self.log.warning(
                            "sf apex run attempt %d/%d produced no parseable --json "
                            "output (exit %s): %s", attempt, _APEX_RUN_ATTEMPTS,
                            res.returncode, (res.stderr or res.stdout or "")[:500])
                    if attempt < _APEX_RUN_ATTEMPTS:
                        time.sleep(_APEX_RETRY_DELAY_S)
            if data is None:
                raise SfError(f"sf apex run produced no parseable --json output after "
                              f"{_APEX_RUN_ATTEMPTS} attempts "
                              f"(exit {res.returncode}):\n{res.stderr or res.stdout}")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
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
