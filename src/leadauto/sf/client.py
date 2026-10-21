"""Salesforce transport: direct HTTP on the `sf` CLI's cached session token.

REST `GET /query` for short SOQL, Bulk API 2.0 query jobs for long SOQL, `/composite`
for bundled reads, REST sObject Collections for record updates and the SOAP Partner API
`convertLead()` for lead conversion. The CLI itself is invoked for ONE thing: `sf org
display` / `show-access-token`, to obtain the token (`auth_info`). Anonymous Apex is not
offered: the pipeline user lacks Author Apex, so conversion goes over SOAP (sf/soap.py).

API-call budget is the primary constraint on this module (the org's allocation is shared
with business-critical integrations), so every transport here is the cheapest one that is
still correct — see the per-method docstrings for the call cost.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import time
import urllib.parse

import requests

# FORCE_COLOR=0: an inherited FORCE_COLOR (e.g. from a harness/CI terminal) makes the
# sf CLI ANSI-colorize its --json output, which breaks json.loads (seen 2026-07-10).
_ENV = {**os.environ, "SF_AUTOUPDATE_DISABLE": "true",
        "FORCE_COLOR": "0", "NO_COLOR": "1"}

# A REST GET carries the SOQL in the request URL, so a long IN(...) list overflows
# Salesforce's request-header limit -> HTTP 431 "Request Header Fields Too Large".
# Queries whose raw SOQL exceeds this length are routed through a Bulk API 2.0 query job,
# which carries the query in the POST body. The threshold is on raw length (URL-encoding
# inflates it) and stays well under the ~8 KB header budget, so small everyday queries
# keep using the faster synchronous REST path.
_BULK_QUERY_THRESHOLD = 2000

# REST twin of the SOAP DuplicateRuleHeader (allowSave) that Salesforce Inspector sends:
# saves a record an ALERT-mode duplicate rule would reject with DUPLICATES_DETECTED. A
# BLOCK-mode rule still rejects. Live-proven on sObject Collections PATCH 2026-10-21
# (Lead 00QMz00000hXiVqMAK: DUPLICATES_DETECTED without it, success with it).
_ALLOW_DUPLICATES_HEADER = {"Sforce-Duplicate-Rule-Header": "allowSave=true"}


class SfError(RuntimeError):
    pass


class _JobNotReady(SfError):
    """A bulk query job's results were probed before it finished.

    Raised only for the FIRST page of a job read without a prior status poll. Never
    mid-pagination: retrying there would restart at page 1 and duplicate rows.
    """


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


def _collection_error(row: dict) -> str:
    """'CODE: message; …' for one failed sObject-Collections result row."""
    return "; ".join(f"{e.get('statusCode', '')}: {e.get('message', '')}"
                     for e in row.get("errors") or []) or "unknown error"


# A /composite request carries at most 25 subrequests and — unlike /composite/batch,
# which bills each subrequest — "the entire series of requests counts as a single call
# toward your API limits". That is the whole point of query_many.
_COMPOSITE_SUBREQUEST_MAX = 25

# Bulk API 2.0 read transport (replaced `sf data export bulk` 2026-07-25). The CLI polls
# job status on a hardcoded 5 s interval and every HTTP request counts as one API call,
# so a job spent ~4-7 calls where 2 suffice. Here the job is created, awaited ONCE, and
# its results are read directly — the results endpoint doubles as the completion check.
# The initial wait is a latency/robustness knob only: a job that is not ready yet just
# falls back to the polling loop, so a wrong guess never costs correctness.
_BULK_POLL_INITIAL_S = 5
_BULK_POLL_INTERVAL_S = 5
_BULK_TIMEOUT_S = 900

_HTTP_TIMEOUT_S = 120


class SfClient:
    def __init__(self, alias: str, api_version: str = "66.0", logger=None):
        self.alias = alias
        self.api_version = api_version
        self.log = logger
        self._auth: dict | None = None

    def _run(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        # encoding/errors are REQUIRED, not cosmetic: `text=True` alone decodes with the
        # locale codec (cp1252 on Windows), which dies with UnicodeDecodeError as soon as a
        # German company name appears in stdout or stderr. CI (Linux/UTF-8) never sees it;
        # every local Windows run does. Hit live 2026-08-09 by Contact Presence Check's
        # dept_pilot on a pool-account query.
        res = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_ENV
        )
        if check and res.returncode != 0:
            raise SfError(f"sf command failed ({res.returncode}):\n{cmd}\n{res.stderr}")
        return res

    # --- reads ---
    def query(self, soql: str, bulk: bool = False) -> list[dict]:
        """Run SOQL and return records as a list of dicts (attributes stripped).

        Large queries (or `bulk=True`) go through a Bulk API 2.0 job to dodge the GET-URL/
        header limit that makes long IN(...) lists fail with HTTP 431; smaller queries
        use the faster synchronous REST query. Several INDEPENDENT short reads should go
        through `query_many` instead — one /composite call for all of them.
        """
        soql = " ".join(soql.split())  # collapse whitespace/newlines
        if bulk or len(soql) > _BULK_QUERY_THRESHOLD:
            return self._query_bulk(soql)
        # Direct REST GET on the cached token (since 2026-09-19; before that this path
        # shelled out to `sf data query`, the one remaining CLI transport for data — and
        # the source of the double-quote ban, the FORCE_COLOR env dance and the cp1252
        # decode crash). Same single API call; `query_many` has run this exact request
        # inside /composite in production since 2026-07-25.
        response = self._request("GET", self._query_path(soql))
        self._raise_for_status(response, "query")
        return self._collect_query_records(self._json_body(response, "query"), "query")

    def _query_path(self, soql: str) -> str:
        return self._data_path(f"/query/?q={urllib.parse.quote(soql, safe='')}")

    def _query_bulk(self, soql: str) -> list[dict]:
        """Run SOQL as a Bulk API 2.0 query job over direct HTTP (2 calls on the happy
        path: create + one result page).

        The query rides in the POST body, so it never hits the GET-URL header limit that
        makes long `IN (...)` lists fail with HTTP 431. After one short wait the results
        endpoint is read directly — a readable page IS proof the job completed, so no
        status poll is spent. A job that is not ready yet (4xx on the first page) or that
        returns an EMPTY body falls back to polling: an empty body is indistinguishable
        from a zero-row result, and reporting "no rows" for a running job would be a wrong
        answer, not a slow one.

        Bulk results are CSV, so every value is a STRING and nulls arrive as '' — the same
        stringly-typed contract the CLI path had ('false' is truthy!); callers coerce with
        their own `_is_true`-style helpers.
        """
        job_id = self._create_query_job(soql)
        if _BULK_POLL_INITIAL_S:
            time.sleep(_BULK_POLL_INITIAL_S)
        rows: list[dict] | None
        try:
            rows = self._download_job_rows(job_id, probe=True)
        except _JobNotReady:
            rows = None
        if rows is not None and not rows and self._job_state(job_id) != "JobComplete":
            rows = None
        if rows is None:
            self._wait_for_job(job_id)
            rows = self._download_job_rows(job_id)
        return rows

    def _create_query_job(self, soql: str) -> str:
        """POST a Bulk API 2.0 query job; returns its id. One API call, any row count."""
        response = self._request("POST", self._data_path("/jobs/query"),
                                 body=json.dumps({"operation": "query", "query": soql}))
        self._raise_for_status(response, "create bulk query job")
        job_id = (self._json_body(response, "create-job").get("id") or "").strip()
        if not job_id:
            raise SfError(f"Bulk create-job response carried no job id:\n"
                          f"{response.text[:2000]}")
        return job_id

    def _job_state(self, job_id: str) -> str:
        """Current job state ('InProgress' | 'JobComplete' | 'Failed' | ...). One call."""
        response = self._request("GET", self._data_path(f"/jobs/query/{job_id}"))
        self._raise_for_status(response, f"read state of bulk job {job_id}")
        return (self._json_body(response, "job-status").get("state") or "").strip()

    def _wait_for_job(self, job_id: str) -> None:
        """Poll until the job completes; raise on Failed/Aborted or the deadline."""
        deadline = time.monotonic() + _BULK_TIMEOUT_S
        while True:
            state = self._job_state(job_id)
            if state == "JobComplete":
                return
            if state in ("Failed", "Aborted"):
                raise SfError(f"Bulk query job {job_id} ended in state {state}")
            if time.monotonic() >= deadline:
                raise SfError(f"Bulk query job {job_id} did not finish within "
                              f"{_BULK_TIMEOUT_S}s (last state: {state})")
            time.sleep(_BULK_POLL_INTERVAL_S)

    def _download_job_rows(self, job_id: str, probe: bool = False) -> list[dict]:
        """Every result row of a completed query job, following `Sforce-Locator` pages.

        `maxRecords` is deliberately never sent: it can only SHRINK the server's page,
        which would raise the page count and therefore the API-call count. With
        `probe=True` a 4xx on the FIRST page means "not finished yet" (_JobNotReady);
        every later page keeps its failure fatal, because a retry mid-pagination would
        restart at page 1 and duplicate rows.
        """
        rows: list[dict] = []
        base = self._data_path(f"/jobs/query/{job_id}/results")
        locator, first_page = "", True
        while True:
            path = f"{base}?locator={urllib.parse.quote(locator)}" if locator else base
            response = self._request("GET", path, accept="text/csv")
            if response.status_code >= 300:
                if probe and first_page and 400 <= response.status_code < 500:
                    raise _JobNotReady(f"bulk job {job_id} is not ready "
                                       f"(HTTP {response.status_code})")
                self._raise_for_status(response, f"read results of bulk job {job_id}")
            first_page = False
            text = response.content.decode("utf-8-sig", errors="replace")
            if text.strip():
                rows.extend(dict(r) for r in csv.DictReader(io.StringIO(text)))
            locator = (response.headers.get("Sforce-Locator") or "").strip()
            if not locator or locator == "null":
                return rows

    def query_many(self, queries: dict[str, str]) -> dict[str, list[dict]]:
        """Run several INDEPENDENT SOQL reads and return {label: records}.

        Short queries are bundled into one `/composite` request — up to 25 subrequests
        bill as a SINGLE API call — so N independent reads cost 1 instead of N. Use this
        only for reads that do not feed each other; a dependent read (e.g. contacts by the
        accounts a previous query returned) must stay a separate call.

        A query too long for a GET URL falls back to its own bulk job, exactly as
        `query` would route it. A paginated subrequest (>2000 records) follows
        `nextRecordsUrl`, one extra call per page — still cheaper than the split.
        """
        if not queries:
            return {}
        out: dict[str, list[dict]] = {}
        bundleable: list[tuple[str, str]] = []
        for label, soql in queries.items():
            collapsed = " ".join(soql.split())
            if len(collapsed) > _BULK_QUERY_THRESHOLD:
                out[label] = self._query_bulk(collapsed)   # own job; can't ride a GET URL
            else:
                bundleable.append((label, collapsed))
        for start in range(0, len(bundleable), _COMPOSITE_SUBREQUEST_MAX):
            out.update(self._composite_query(
                bundleable[start:start + _COMPOSITE_SUBREQUEST_MAX]))
        return out

    def _composite_query(self, items: list[tuple[str, str]]) -> dict[str, list[dict]]:
        """One /composite request carrying up to 25 SOQL GET subrequests."""
        payload = {"allOrNone": False, "compositeRequest": [
            {"method": "GET", "referenceId": f"q{i}", "url": self._query_path(soql)}
            for i, (_label, soql) in enumerate(items)]}
        response = self._request("POST", self._data_path("/composite"),
                                 body=json.dumps(payload))
        self._raise_for_status(response, "composite query")
        subresponses = self._json_body(response, "composite").get("compositeResponse")
        if not isinstance(subresponses, list) or len(subresponses) != len(items):
            raise SfError(f"Unexpected /composite response shape:\n{response.text[:2000]}")
        out: dict[str, list[dict]] = {}
        for (label, _soql), sub in zip(items, subresponses):
            status = sub.get("httpStatusCode", 0)
            body = sub.get("body")
            if status >= 300 or not isinstance(body, dict):
                raise SfError(f"Composite subrequest '{label}' failed "
                              f"(HTTP {status}):\n{json.dumps(body)[:2000]}")
            out[label] = self._collect_query_records(body, label)
        return out

    def _collect_query_records(self, body: dict, label: str) -> list[dict]:
        """Records of a REST query body, following `nextRecordsUrl` to the last page.

        `done == false` is never accepted as the whole answer — a truncated read is a
        plausible-looking wrong answer, so the remaining pages are fetched (1 call each).
        """
        records: list[dict] = list(body.get("records") or [])
        guard = 0
        while not body.get("done", True):
            next_url = body.get("nextRecordsUrl")
            if not next_url:
                raise SfError(f"Query '{label}' reported done=false with no "
                              "nextRecordsUrl — refusing to return a truncated result")
            guard += 1
            if guard > 100:
                raise SfError(f"Query '{label}' paginated more than 100 times; aborting")
            response = self._request("GET", next_url)
            self._raise_for_status(response, f"paginate query '{label}'")
            body = self._json_body(response, f"query '{label}'")
            records.extend(body.get("records") or [])
        for r in records:
            r.pop("attributes", None)
        return records

    # --- writes ---
    def update_records(self, sobject: str, records: list[dict], *,
                       allow_duplicates: bool = False) -> dict:
        """Update existing records over REST sObject Collections (one PATCH per ≤200
        records, allOrNone=false) — replaces the Bulk API job the CLI ran per update
        (5-7 round trips + polling each; API-call reduction 2026-07-18).

        Each dict must contain 'Id' plus the fields to set. Returns
        {processed, failed, failures: [{id, error}], confirmed} — the same contract
        the retired update_bulk had; `confirmed` is always True because the response carries one
        inline per-row result. Failed rows come back with a null id, so failures are
        keyed to the request-order record Id. A 401 (stale cached CLI token) gets one
        auth re-fetch + retry, like convert_lead.

        `allow_duplicates` sends `Sforce-Duplicate-Rule-Header: allowSave=true`, so an
        Alert-mode duplicate rule no longer fails the row (a Block-mode one still does).
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
            rows = self._send_collections(
                "PATCH", payload,
                headers=_ALLOW_DUPLICATES_HEADER if allow_duplicates else None)
            for rec, row in zip(chunk, rows):
                if row.get("success"):
                    continue
                failures.append({"id": row.get("id") or rec["Id"],
                                 "error": _collection_error(row)})
        return {"processed": len(records), "failed": len(failures),
                "failures": failures, "confirmed": True}

    def create_records(self, sobject: str, records: list[dict]) -> dict:
        """Insert records over REST sObject Collections (one POST per ≤200 records,
        allOrNone=false) — the insert sibling of update_records, 1 API call per chunk.

        Returns {processed, created, duplicates, failed, failures: [{record, error}]}.
        Failed rows carry no id, so each failure names its request-order record. A row
        rejected ONLY with DUPLICATE_VALUE (e.g. a lead that is already a member of the
        campaign) counts under `duplicates`, not as a failure: the record exists.
        """
        result = {"processed": len(records), "created": 0, "duplicates": 0,
                  "failed": 0, "failures": []}
        for start in range(0, len(records), _COLLECTIONS_MAX):
            chunk = records[start:start + _COLLECTIONS_MAX]
            payload = {"allOrNone": False,
                       "records": [{"attributes": {"type": sobject}, **r} for r in chunk]}
            rows = self._send_collections("POST", payload)
            for rec, row in zip(chunk, rows):
                codes = {e.get("statusCode") for e in row.get("errors") or []}
                if row.get("success"):
                    result["created"] += 1
                elif codes == {"DUPLICATE_VALUE"}:
                    result["duplicates"] += 1
                else:
                    result["failures"].append({"record": rec,
                                               "error": _collection_error(row)})
        result["failed"] = len(result["failures"])
        return result

    def _send_collections(self, method: str, payload: dict,
                          headers: dict | None = None) -> list[dict]:
        """One sObject-Collections request (1 API call for up to 200 records)."""
        response = self._request(method, self._data_path("/composite/sobjects"),
                                 body=json.dumps(payload), headers=headers)
        self._raise_for_status(response, f"sObject Collections {method}")
        rows = json.loads(response.text)
        if not isinstance(rows, list):
            raise SfError(f"Unexpected sObject Collections response shape:\n"
                          f"{response.text[:2000]}")
        return rows

    # --- direct HTTP transport (rides the CLI's cached session token) -------------
    def _data_path(self, suffix: str) -> str:
        """'/jobs/query' -> '/services/data/v66.0/jobs/query'."""
        return f"/services/data/v{self.api_version}{suffix}"

    def _request(self, method: str, path: str, *, body: str | None = None,
                 accept: str = "application/json", headers: dict | None = None):
        """One authenticated request against an org path, retried ONCE with a refreshed
        token on HTTP 401 (a run can outlive the CLI's cached access token).

        `headers` are extra request headers (e.g. the duplicate-rule header).
        Returns the response as-is — callers decide what a non-2xx means, because the
        bulk results probe treats a first-page 4xx as "job not ready", not as failure.
        """
        response = self._request_once(method, path, body, accept, headers, refresh=False)
        if response.status_code == 401:
            if self.log:
                self.log.warning("%s %s got 401; refreshing auth and retrying",
                                 method, path.split("?")[0])
            response = self._request_once(method, path, body, accept, headers,
                                          refresh=True)
        return response

    def _request_once(self, method: str, path: str, body: str | None,
                      accept: str, extra_headers: dict | None = None, *, refresh: bool):
        auth = self.auth_info(refresh=refresh)
        headers = {"Authorization": f"Bearer {auth['access_token']}", "Accept": accept,
                   **(extra_headers or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return requests.request(method, f"{auth['instance_url']}{path}", data=body,
                                headers=headers, timeout=_HTTP_TIMEOUT_S)

    @staticmethod
    def _raise_for_status(response, what: str) -> None:
        if response.status_code >= 300:
            raise SfError(f"Failed to {what} (HTTP {response.status_code}):\n"
                          f"{response.text[:2000]}")

    @staticmethod
    def _json_body(response, what: str) -> dict:
        try:
            body = json.loads(response.text)
        except ValueError as exc:
            raise SfError(f"Could not parse the {what} response: {exc}\n"
                          f"{response.text[:2000]}") from exc
        if not isinstance(body, dict):
            raise SfError(f"Unexpected {what} response shape: "
                          f"{type(body).__name__}\n{response.text[:2000]}")
        return body

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

