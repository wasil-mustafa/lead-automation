# LeadFeeder throttle, retry & batching — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the LeadFeeder enrichment step resilient to the API's 100 req/min limit and cut request volume by batching company-match and contact-retrieve calls — without changing which companies/contacts match.

**Architecture:** Centralize a proactive rate-throttle + `429`/`5xx` retry in the client's two HTTP methods (`api_get`/`api_post`), so every call inherits them. Add batch methods (`match_companies` ≤200, `retrieve_contacts` ≤100) mirroring the existing `retrieve_companies`, keep the singular methods as thin wrappers, and restructure `enrich.py` phases 1 and 3 to call the batch methods once instead of per-lead.

**Tech Stack:** Python 3.14, `requests`, `pytest` (configured with `pythonpath=src` via `pyproject.toml`). No new dependencies.

## Global Constraints

- **Behavior-neutral:** same matches, same `match_score`s, same `min_match_score` floor, same credit model. Only transport (request batching/pacing/retry) changes.
- **No new dependencies.** Only `requests` / `pyyaml` / `typer` are available.
- **Secrets via env only:** `LEADFEEDER_API_KEY`. Never hardcode.
- **Run tests with:** `python -m pytest -q` (pythonpath=src is configured; no `PYTHONPATH` needed for pytest).
- **API limits (from docs):** rate limit 100 req/min per account; `POST /companies/match` `companies` array max 200; `GET /companies?ids=` and `GET /contacts?ids=` max 100 IDs; match & search are free, retrieves cost 1 credit/new record (free if accessed within 12 months).
- **Branch:** `leadfeeder-throttle-batching` (already created).
- **Every commit message ends with the trailer:**
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  (Omitted from the short `-m` strings below for brevity — append it to each.)

## File Structure

- `src/leadauto/leadfeeder/client.py` — **modify.** Transport rewrite (`_request`, `_throttle`, `_retry_delay`, new constructor params, remove `_sleep`); new `match_companies`/`_best_match`/`retrieve_contacts`; `match_company`/`get_contact_details` become wrappers.
- `src/leadauto/leadfeeder/enrich.py` — **modify.** Phase 1 → batched match; phase 3 → cascade then one batched retrieve; surface list-add errors.
- `src/leadauto/config.py` — **modify.** Four new `Settings` properties.
- `config/config.yaml` — **modify.** Four new `leadfeeder:` keys.
- `src/leadauto/cli.py` — **modify.** `_build_lf_client` passes the new knobs.
- `tests/test_leadfeeder_client.py` — **modify.** Test-harness upgrade (`FakeResponse.headers`, `FakeSession` pass-through, `_client` defaults) + new transport/batch tests.
- `tests/test_enrich.py` — **modify.** `FakeClient` gains batch methods; assertions updated; new batch assertions.
- `tests/test_config.py` — **modify.** New defaults test.
- `scripts/leadfeeder_probe.py` — **create.** Read-only live probe (final validation).

---

### Task 1: Transport — throttle + retry, centralized in `api_get`/`api_post`

**Files:**
- Modify: `src/leadauto/leadfeeder/client.py` (constructor ~120-139; `api_get`/`api_post` 145-155; remove `_sleep` 157-159; remove `self._sleep()` calls in `search_contact` and `retrieve_companies`)
- Test: `tests/test_leadfeeder_client.py` (`FakeResponse`, `FakeSession._respond`, `_client`, new tests)

**Interfaces:**
- Produces: `LeadfeederClient(api_key, base_url=?, session=?, min_match_score=0.70, retrieve_batch=100, match_batch=200, max_requests_per_min=95, max_retries=4, backoff_base=1.0, sleep_fn=time.sleep, clock=time.monotonic, logger=None)`. `api_get(path, params=None) -> dict` and `api_post(path, payload, params=None) -> dict` now throttle every call and retry `{429,500,502,503,504}` (honoring `Retry-After`) up to `max_retries`, then raise `HTTPError`. The `request_delay` param and `_sleep` method are **removed**. Module constant `RETRYABLE_STATUS = {429, 500, 502, 503, 504}`.

- [ ] **Step 1: Upgrade the test harness**

In `tests/test_leadfeeder_client.py`, give `FakeResponse` a `headers` dict, let `FakeSession` return `FakeResponse` instances verbatim (so tests can inject error statuses), and change `_client` to disable the throttle and never really sleep.

```python
class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)
```

In `FakeSession._respond`, replace the tail so a route value (or a queue item) that is already a `FakeResponse` is returned as-is:

```python
    def _respond(self, method, url, params, body):
        self.calls.append((method, url, params, body))
        candidates = sorted(
            (r for r in self._routes if r[0] == method and r[1] in url),
            key=lambda r: len(r[1]), reverse=True,
        )
        if not candidates:
            return FakeResponse({"data": []})
        value = self._routes[candidates[0]]
        if callable(value):
            value = value(params, body)
        elif isinstance(value, list):
            value = value.pop(0) if value else {"data": []}
        return value if isinstance(value, FakeResponse) else FakeResponse(value)
```

Replace the `_client` helper:

```python
def _client(routes, seed_account=True, **kw):
    kw.setdefault("max_requests_per_min", 0)    # throttle off unless a test opts in
    kw.setdefault("sleep_fn", lambda *_: None)   # never actually sleep in tests
    client = LeadfeederClient(api_key="k", session=FakeSession(routes), **kw)
    if seed_account:
        client._account_id = "acc1"  # skip the /accounts round-trip for focused tests
    return client
```

- [ ] **Step 2: Write the failing transport tests**

Add to `tests/test_leadfeeder_client.py`:

```python
# --- transport: throttle + retry ---------------------------------------------

def test_throttle_spaces_consecutive_requests():
    sleeps = []
    client = LeadfeederClient(
        api_key="k", session=FakeSession({}),
        max_requests_per_min=120, sleep_fn=sleeps.append, clock=lambda: 1000.0,
    )
    client._account_id = "acc1"
    client.api_get("/x")
    client.api_get("/x")
    assert sleeps == [0.5]  # 60/120s between back-to-back calls; first call un-delayed


def test_retries_on_429_then_succeeds():
    sleeps = []
    routes = {("GET", "/companies"): [
        FakeResponse({}, status=429, headers={"Retry-After": "2"}),
        FakeResponse({"data": [{"id": "c1", "attributes": {"url": "x.de"}}]}),
    ]}
    client = _client(routes, sleep_fn=sleeps.append)
    out = client.api_get("/companies", params={"ids": "c1"})
    assert out["data"][0]["id"] == "c1"
    assert sleeps == [2.0]  # honored Retry-After


def test_429_exhausts_retries_and_raises():
    import requests
    routes = {("GET", "/companies"): [FakeResponse({}, status=429) for _ in range(10)]}
    client = _client(routes, max_retries=2)
    with pytest.raises(requests.exceptions.HTTPError):
        client.api_get("/companies")


def test_retries_on_500_without_retry_after_header():
    routes = {("GET", "/x"): [FakeResponse({}, status=500), FakeResponse({"data": []})]}
    client = _client(routes)
    assert client.api_get("/x") == {"data": []}
```

- [ ] **Step 3: Run the new tests to confirm they fail**

Run: `python -m pytest tests/test_leadfeeder_client.py -q -k "throttle or retr or 429 or 500"`
Expected: FAIL (constructor rejects `max_requests_per_min`/`sleep_fn`/`clock`; `request_delay` no longer relevant).

- [ ] **Step 4: Rewrite the transport in `client.py`**

At the top of `client.py`, the `import time` already exists; add the module constant under it:

```python
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
```

Replace the constructor (lines ~120-139) with:

```python
    def __init__(
        self,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        session=None,
        min_match_score: float = 0.70,
        retrieve_batch: int = 100,
        match_batch: int = 200,
        max_requests_per_min: int = 95,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        sleep_fn=time.sleep,
        clock=time.monotonic,
        logger=None,
    ):
        if not api_key:
            raise LeadfeederError("LEADFEEDER_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.min_match_score = min_match_score
        self.retrieve_batch = retrieve_batch
        self.match_batch = match_batch
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._min_interval = 60.0 / max_requests_per_min if max_requests_per_min else 0.0
        self._last_request_at = 0.0
        self.log = logger or get_logger("leadfeeder")
        self._account_id: str | None = None
```

Replace `api_get`/`api_post` and the `_sleep` method (lines ~145-159) with:

```python
    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._last_request_at + self._min_interval - self._clock()
        if wait > 0:
            self._sleep_fn(wait)
        self._last_request_at = self._clock()

    def _retry_delay(self, resp, attempt: int) -> float:
        retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        return self.backoff_base * (2 ** attempt)

    def _request(self, kind: str, path: str, *, params=None, json=None) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            if kind == "GET":
                resp = self.session.get(url, headers=self._headers(), params=params)
            else:
                resp = self.session.post(url, headers=self._headers(), json=json, params=params)
            status = getattr(resp, "status_code", 200)
            if status in RETRYABLE_STATUS and attempt < self.max_retries:
                delay = self._retry_delay(resp, attempt)
                self.log.warning("LeadFeeder %s %s -> %s; retry %d/%d in %.1fs",
                                 kind, path, status, attempt + 1, self.max_retries, delay)
                self._sleep_fn(delay)
                continue
            resp.raise_for_status()
            return resp.json()

    def api_get(self, path: str, params=None) -> dict:
        return self._request("GET", path, params=params)

    def api_post(self, path: str, payload, params=None) -> dict:
        return self._request("POST", path, params=params, json=payload)
```

Now remove the three `self._sleep()` calls in `search_contact` (the lines immediately after `if not contacts ...:` in attempts 2, 3, and 4) and the one in `retrieve_companies` (after the `try/except`). Delete only the `self._sleep()` lines; leave surrounding logic intact.

- [ ] **Step 5: Run the transport tests to confirm they pass**

Run: `python -m pytest tests/test_leadfeeder_client.py -q -k "throttle or retr or 429 or 500"`
Expected: PASS (4 passed).

- [ ] **Step 6: Run the full client test file (no regressions)**

Run: `python -m pytest tests/test_leadfeeder_client.py -q`
Expected: PASS — existing `match_company`, `search_contact`, `retrieve_companies`, `get_account_id` tests still green (cascade POST counts and explicit `retrieve_batch=2` unaffected by removing `_sleep`).

- [ ] **Step 7: Commit**

```bash
git add src/leadauto/leadfeeder/client.py tests/test_leadfeeder_client.py
git commit -m "feat(leadfeeder): centralized throttle + 429/5xx retry in HTTP methods"
```

---

### Task 2: Batch company match (`match_companies`)

**Files:**
- Modify: `src/leadauto/leadfeeder/client.py` (`match_company` ~196-215 becomes a wrapper; add `match_companies` + `_best_match`)
- Test: `tests/test_leadfeeder_client.py`

**Interfaces:**
- Consumes: `api_post`, `min_match_score`, `match_batch`, `get_account_id` (Task 1).
- Produces: `match_companies(payloads: list[dict]) -> list[dict | None]` — one entry per input payload, aligned by index; each is `{"company_id", "match_score", "matched_name"}` or `None`. `match_company(company_name, email=None, country=None, country_code=None) -> dict | None` is now a wrapper over `match_companies`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_leadfeeder_client.py`:

```python
# --- match_companies (batch) -------------------------------------------------

def test_match_companies_aligns_results_by_input_index():
    def handler(params, body):
        return {"data": [
            [{"id": "c1", "attributes": {"match_score": 0.95},
              "relationships": {"company_summary": {"attributes": {"name": "Acme"}}}}],
            [],  # second company: no match
        ]}
    client = _client({("POST", "/companies/match"): handler})
    out = client.match_companies([{"company_name": "Acme"}, {"company_name": "Nope"}])
    assert out == [{"company_id": "c1", "match_score": 0.95, "matched_name": "Acme"}, None]


def test_match_companies_chunks_over_match_batch():
    posts = []
    def handler(params, body):
        posts.append(len(body["companies"]))
        return {"data": [[] for _ in body["companies"]]}
    client = _client({("POST", "/companies/match"): handler}, match_batch=2)
    out = client.match_companies([{"company_name": f"C{i}"} for i in range(5)])
    assert posts == [2, 2, 1]
    assert out == [None] * 5


def test_match_companies_applies_score_floor():
    handler = lambda p, b: {"data": [[
        {"id": "c1", "attributes": {"match_score": 0.50},
         "relationships": {"company_summary": {"attributes": {"name": "x"}}}}]]}
    client = _client({("POST", "/companies/match"): handler}, min_match_score=0.70)
    assert client.match_companies([{"company_name": "x"}]) == [None]


def test_match_companies_chunk_failure_yields_none():
    routes = {("POST", "/companies/match"): FakeResponse({}, status=500)}
    client = _client(routes, max_retries=0)
    assert client.match_companies([{"company_name": "x"}, {"company_name": "y"}]) == [None, None]


def test_match_companies_empty_input():
    assert _client({}).match_companies([]) == []
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_leadfeeder_client.py -q -k match_companies`
Expected: FAIL with `AttributeError: ... has no attribute 'match_companies'`.

- [ ] **Step 3: Implement `match_companies` + `_best_match`, make `match_company` a wrapper**

In `client.py`, replace the existing `match_company` method (lines ~196-215) with:

```python
    # -- company match (batched) --
    def _best_match(self, matches: list) -> dict | None:
        if not matches:
            return None
        best = max(matches, key=lambda m: m.get("attributes", {}).get("match_score", 0))
        score = best.get("attributes", {}).get("match_score", 0)
        if score < self.min_match_score:
            return None
        summary = best.get("relationships", {}).get("company_summary", {}).get("attributes", {})
        return {"company_id": best["id"], "match_score": score,
                "matched_name": summary.get("name", "")}

    def match_companies(self, payloads: list[dict]) -> list[dict | None]:
        """Batch company match (free). `payloads` is a list of per-company dicts
        (company_name, email, country, country_code, ...). Returns a list aligned to
        `payloads`: {company_id, match_score, matched_name} or None per company. A chunk
        that errors after retries degrades its entries to None (logged), never raising."""
        if not payloads:
            return []
        account_id = self.get_account_id()
        results: list[dict | None] = []
        for i in range(0, len(payloads), self.match_batch):
            chunk = payloads[i:i + self.match_batch]
            try:
                data = self.api_post(
                    "/companies/match", payload={"companies": chunk},
                    params={"account_id": account_id, "max_results_per_company": 3},
                )
            except requests.exceptions.HTTPError as e:
                self.log.warning("Company match chunk failed (%d companies): %s", len(chunk), e)
                results.extend([None] * len(chunk))
                continue
            per_company = data.get("data", []) or []
            for j in range(len(chunk)):
                matches = per_company[j] if j < len(per_company) else []
                results.append(self._best_match(matches))
        return results

    def match_company(self, company_name: str, email: str | None = None,
                      country: str | None = None, country_code: str | None = None) -> dict | None:
        payload = {"company_name": company_name, "email": email or ""}
        if country:
            payload["country"] = country
        if country_code:
            payload["country_code"] = country_code
        return self.match_companies([payload])[0]
```

- [ ] **Step 4: Run match tests (new + existing)**

Run: `python -m pytest tests/test_leadfeeder_client.py -q -k "match"`
Expected: PASS — new `match_companies` tests and the three existing `match_company` tests (which post `{"companies":[payload]}` and read `data[0]`) both pass.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/leadfeeder/client.py tests/test_leadfeeder_client.py
git commit -m "feat(leadfeeder): batch match_companies (<=200), match_company delegates"
```

---

### Task 3: Batch contact retrieve (`retrieve_contacts`)

**Files:**
- Modify: `src/leadauto/leadfeeder/client.py` (`get_contact_details` ~269-271 becomes a wrapper; add `retrieve_contacts`)
- Test: `tests/test_leadfeeder_client.py`

**Interfaces:**
- Consumes: `api_get`, `retrieve_batch`, `get_account_id`, `flatten_contact_details` (Task 1 + existing).
- Produces: `retrieve_contacts(contact_ids: list[str]) -> dict[str, dict]` (id → raw `attributes`, chunked ≤`retrieve_batch`, per-chunk error isolation). `get_contact_details(contact_id) -> dict` now wraps it and flattens.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_leadfeeder_client.py`:

```python
# --- retrieve_contacts (batch) -----------------------------------------------

def test_retrieve_contacts_maps_ids_to_attributes():
    handler = lambda p, b: {"data": [
        {"id": "k1", "attributes": {"first_name": "Jane"}},
        {"id": "k2", "attributes": {"first_name": "John"}},
    ]}
    client = _client({("GET", "/contacts"): handler})
    assert client.retrieve_contacts(["k1", "k2"]) == {
        "k1": {"first_name": "Jane"}, "k2": {"first_name": "John"}}


def test_retrieve_contacts_chunks_over_retrieve_batch():
    seen = []
    def handler(params, body):
        seen.append(params["ids"])
        return {"data": []}
    client = _client({("GET", "/contacts"): handler}, retrieve_batch=2)
    client.retrieve_contacts(["a", "b", "c"])
    assert seen == ["a,b", "c"]


def test_retrieve_contacts_chunk_failure_isolated():
    routes = {("GET", "/contacts"): FakeResponse({}, status=500)}
    client = _client(routes, max_retries=0)
    assert client.retrieve_contacts(["k1"]) == {}  # logged + skipped, not raised


def test_get_contact_details_flattens_single_contact():
    handler = lambda p, b: {"data": [{"id": "k1", "attributes": {"position": {"title": "CEO"}}}]}
    client = _client({("GET", "/contacts"): handler})
    assert client.get_contact_details("k1")["position"] == "CEO"
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_leadfeeder_client.py -q -k "retrieve_contacts or get_contact_details"`
Expected: FAIL (`retrieve_contacts` missing; `get_contact_details` still hits `/contacts/{id}` route, so the new tests' `/contacts` handler shape won't match).

- [ ] **Step 3: Implement `retrieve_contacts`, make `get_contact_details` a wrapper**

In `client.py`, replace the existing `get_contact_details` method (lines ~269-271) with:

```python
    def retrieve_contacts(self, contact_ids: list[str]) -> dict[str, dict]:
        """Batch-retrieve contact `attributes` keyed by contact id (chunked, max 100/call).
        1 credit per contact not accessed in the last 12 months. Per-chunk errors isolated."""
        if not contact_ids:
            return {}
        account_id = self.get_account_id()
        out: dict[str, dict] = {}
        for i in range(0, len(contact_ids), self.retrieve_batch):
            batch = contact_ids[i:i + self.retrieve_batch]
            try:
                data = self.api_get("/contacts", params={"ids": ",".join(batch), "account_id": account_id})
                for contact in data.get("data", []) or []:
                    out[contact["id"]] = contact.get("attributes", {})
            except requests.exceptions.HTTPError as e:
                self.log.warning("Contact retrieve batch failed: %s", e)
        return out

    def get_contact_details(self, contact_id: str) -> dict:
        attrs = self.retrieve_contacts([contact_id]).get(contact_id, {})
        return flatten_contact_details(attrs)
```

- [ ] **Step 4: Run contact-retrieve tests**

Run: `python -m pytest tests/test_leadfeeder_client.py -q -k "retrieve_contacts or get_contact_details"`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/leadfeeder/client.py tests/test_leadfeeder_client.py
git commit -m "feat(leadfeeder): batch retrieve_contacts (<=100), get_contact_details delegates"
```

---

### Task 4: Enrich phase 1 — batched company match

**Files:**
- Modify: `src/leadauto/leadfeeder/enrich.py` (Phase 1 block ~50-73)
- Test: `tests/test_enrich.py` (`FakeClient.match_companies`; update assertions; add batch assertion)

**Interfaces:**
- Consumes: `client.match_companies` (Task 2), `client.add_company_to_list`, `EnrichedLead`, `ExtractedLead.best_company/contact_email/lead_email/region/country_code`.
- Produces: phase-1 sets `matched`/`lf_company_id`/`matched_name`/`match_score` on each `EnrichedLead`; emits exactly one `match_companies` call (none when no lead has a company name).

- [ ] **Step 1: Update `FakeClient` and the affected tests**

In `tests/test_enrich.py`, replace `FakeClient.match_company` (lines ~30-32) with a `match_companies` that records the per-payload tuple and returns index-aligned results:

```python
    def match_companies(self, payloads):
        self.calls.append(("match_companies", tuple(
            (p["company_name"], p.get("email"), p.get("country"), p.get("country_code"))
            for p in payloads)))
        return [self.match_by_name.get(p["company_name"]) for p in payloads]
```

Update the assertions that referenced the old per-lead `("match", ...)` records:
- In `test_enrich_matched_with_contact`, change the match assertion to:
  ```python
      assert ("match_companies", (("Acme GmbH", "jane@acme.de", "Germany", "DE"),)) in fc.calls
  ```
- In `test_enrich_no_company_name_skips_match_entirely`, change to:
  ```python
      assert not any(c[0] == "match_companies" for c in fc.calls)
  ```
- In `test_enrich_uses_lead_field_fallbacks`, change the match assertion to:
  ```python
      assert ("match_companies", (("Lead Co", "john@lead.co", "Germany", "DE"),)) in fc.calls
  ```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_enrich.py -q`
Expected: FAIL (`enrich_leads` still calls `client.match_company`, which `FakeClient` no longer defines → `AttributeError`).

- [ ] **Step 3: Rewrite enrich phase 1**

In `src/leadauto/leadfeeder/enrich.py`, replace the Phase 1 block (from `# Phase 1 — company match (free).` through the `matched = [r for r in results if r.matched]` line) with:

```python
    # Phase 1 — company match (free, batched).
    results: list[EnrichedLead] = []
    match_payloads: list[dict] = []
    match_index: list[int] = []  # results-index for each payload
    for e in extracteds:
        result = EnrichedLead(extracted=e)
        company_name = e.best_company
        if company_name:
            email = e.contact_email or e.lead_email
            payload = {"company_name": company_name, "email": email or ""}
            if e.region:
                payload["country"] = e.region
            if e.country_code:
                payload["country_code"] = e.country_code
            match_payloads.append(payload)
            match_index.append(len(results))
        results.append(result)

    matches = client.match_companies(match_payloads) if match_payloads else []
    for payload_i, match in enumerate(matches):
        if not match:
            continue
        r = results[match_index[payload_i]]
        r.matched = True
        r.lf_company_id = match["company_id"]
        r.matched_name = match.get("matched_name")
        r.match_score = match.get("match_score")
        try:
            client.add_company_to_list(r.lf_company_id, company_list_id)
        except requests.exceptions.HTTPError as err:
            log.warning("add_company_to_list failed for %s: %s", r.lf_company_id, err)

    matched = [r for r in results if r.matched]
```

- [ ] **Step 4: Run enrich tests**

Run: `python -m pytest tests/test_enrich.py -q`
Expected: PASS — note phase 3 still uses `get_contact_details` on `FakeClient` (unchanged here), so `test_enrich_matched_with_contact` still passes.

- [ ] **Step 5: Add a multi-lead batch-match assertion**

Add to `tests/test_enrich.py`:

```python
def test_enrich_matches_all_companies_in_one_call():
    fc = FakeClient()
    fc.match_by_name = {
        "Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"},
        "Beta AG": {"company_id": "c2", "match_score": 0.9, "matched_name": "Beta AG"},
    }
    fc.company_attrs = {"c1": {}, "c2": {}}
    e1 = _extracted(company="Acme GmbH", contact_name=None)
    e2 = _extracted(company="Beta AG", contact_name=None, lead_id="00Q2", task_id="T2")
    enrich_leads(fc, [e1, e2], SETTINGS)
    match_calls = [c for c in fc.calls if c[0] == "match_companies"]
    assert len(match_calls) == 1
    assert {t[0] for t in match_calls[0][1]} == {"Acme GmbH", "Beta AG"}
```

Run: `python -m pytest tests/test_enrich.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/leadauto/leadfeeder/enrich.py tests/test_enrich.py
git commit -m "feat(leadfeeder): enrich phase 1 uses batched match_companies"
```

---

### Task 5: Enrich phase 3 — batched retrieve + surfaced list-add errors

**Files:**
- Modify: `src/leadauto/leadfeeder/enrich.py` (import line ~19; Phase 3 block ~83-106)
- Test: `tests/test_enrich.py` (`FakeClient.retrieve_contacts`; raw-attr test data; add batch assertion)

**Interfaces:**
- Consumes: `client.search_contact` (unchanged), `client.retrieve_contacts` (Task 3), `client.add_contact_to_list`, `flatten_contact_details` (imported into enrich).
- Produces: phase 3 sets `contact_found`/`contact` on matched leads; emits exactly one `retrieve_contacts` call covering all found contact ids.

- [ ] **Step 1: Update `FakeClient` and the contact test data (raw attrs)**

In `tests/test_enrich.py`:
- In `FakeClient.__init__`, replace `self.contact_details: dict = {}` with `self.contact_attrs: dict = {}   # contact_id -> raw LF attributes`.
- Replace `FakeClient.get_contact_details` (lines ~45-47) with:
  ```python
      def retrieve_contacts(self, ids):
          self.calls.append(("retrieve_contacts", tuple(ids)))
          return {i: self.contact_attrs.get(i, {}) for i in ids}
  ```
- In `test_enrich_matched_with_contact`, replace the `fc.contact_details = {...}` line with raw-shaped attrs and update the contact assertions:
  ```python
      fc.contact_attrs = {"k1": {"position": {"title": "CEO"},
                                 "social_media_profiles": {"linkedin": [{"url": "https://li/x"}]}}}
  ```
  Keep `assert r.contact["position"] == "CEO"` and `assert r.contact["contact_id"] == "k1"`; change the linkedin assertion to `assert r.contact["contact_linkedin"] == "https://li/x"` (it was not asserted before — add it).
- In `test_enrich_uses_lead_field_fallbacks`, replace `fc.contact_details = {"k1": {"position": "Mgr"}}` with `fc.contact_attrs = {"k1": {"position": {"title": "Mgr"}}}` (the test only asserts `contact_found is True`).

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_enrich.py -q`
Expected: FAIL (`enrich_leads` still calls `client.get_contact_details`, now absent on `FakeClient`).

- [ ] **Step 3: Rewrite enrich phase 3 and the import**

In `src/leadauto/leadfeeder/enrich.py`, change the client import (line ~19) to bring in the contact flattener:

```python
from .client import flatten_company, flatten_contact_details
```

Replace the Phase 3 block (from `# Phase 3 — contact cascade ...` to the end of that loop, before the final `log.info(...)`) with:

```python
    # Phase 3 — contact search cascade (free), then ONE batched detail retrieve (1 credit each).
    search_hits: list[tuple[EnrichedLead, dict]] = []
    for r in matched:
        name = _contact_name(r.extracted)
        if not name:
            continue
        try:
            cmatch = client.search_contact(name, r.lf_company_id)
        except requests.exceptions.HTTPError as err:
            log.warning("Contact search failed for %r: %s", name, err)
            continue
        if cmatch:
            search_hits.append((r, cmatch))

    details_by_id = (client.retrieve_contacts([c["contact_id"] for _, c in search_hits])
                     if search_hits else {})

    for r, cmatch in search_hits:
        cid = cmatch["contact_id"]
        details = flatten_contact_details(details_by_id.get(cid, {}))
        r.contact_found = True
        r.contact = {**details, "contact_id": cid, "full_name": cmatch.get("full_name", "")}
        try:
            client.add_contact_to_list(cid, contact_list_id)
        except requests.exceptions.HTTPError as err:
            log.warning("add_contact_to_list failed for %s: %s", cid, err)
```

- [ ] **Step 4: Run enrich tests**

Run: `python -m pytest tests/test_enrich.py -q`
Expected: PASS.

- [ ] **Step 5: Add a single-batch contact-retrieve assertion**

Add to `tests/test_enrich.py`:

```python
def test_enrich_retrieves_all_contacts_in_one_call():
    fc = FakeClient()
    fc.match_by_name = {
        "Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"},
        "Beta AG": {"company_id": "c2", "match_score": 0.9, "matched_name": "Beta AG"},
    }
    fc.company_attrs = {"c1": {}, "c2": {}}
    fc.contact_by_name = {
        "Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"},
        "John Roe": {"contact_id": "k2", "full_name": "John Roe"},
    }
    fc.contact_attrs = {"k1": {}, "k2": {}}
    e1 = _extracted(company="Acme GmbH", contact_name="Jane Doe")
    e2 = _extracted(company="Beta AG", contact_name="John Roe", lead_id="00Q2", task_id="T2")
    enrich_leads(fc, [e1, e2], SETTINGS)
    rc = [c for c in fc.calls if c[0] == "retrieve_contacts"]
    assert len(rc) == 1
    assert set(rc[0][1]) == {"k1", "k2"}
```

Run: `python -m pytest tests/test_enrich.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/leadauto/leadfeeder/enrich.py tests/test_enrich.py
git commit -m "feat(leadfeeder): enrich phase 3 batched retrieve; surface list-add errors"
```

---

### Task 6: Config wiring (4 knobs)

**Files:**
- Modify: `config/config.yaml` (`leadfeeder:` section)
- Modify: `src/leadauto/config.py` (add four properties)
- Modify: `src/leadauto/cli.py` (`_build_lf_client` ~41-47)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `LeadfeederClient` knobs from Tasks 1–3.
- Produces: `Settings.lf_max_requests_per_min: int`, `lf_max_retries: int`, `lf_retrieve_batch: int`, `lf_match_batch: int`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_leadfeeder_throttle_and_batch_defaults():
    s = Settings.load()
    assert s.lf_max_requests_per_min == 95
    assert s.lf_max_retries == 4
    assert s.lf_retrieve_batch == 100
    assert s.lf_match_batch == 200
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/test_config.py -q -k throttle`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'lf_max_requests_per_min'`).

- [ ] **Step 3: Add config values, Settings properties, and cli wiring**

In `config/config.yaml`, extend the `leadfeeder:` section:

```yaml
leadfeeder:
  base_url: "https://api.leadfeeder.com/v1"
  company_list_name: "Lead_Automation"
  contact_list_name: "Lead_Automation"
  max_requests_per_min: 95   # stay under LeadFeeder's 100/min/account hard limit
  max_retries: 4             # retries on 429/5xx before surfacing HTTPError
  retrieve_batch: 100        # GET /companies & /contacts (API max 100)
  match_batch: 200           # POST /companies/match (API max 200)
```

In `src/leadauto/config.py`, add under the existing `# --- LeadFeeder ---` properties:

```python
    @property
    def lf_max_requests_per_min(self) -> int:
        return int(self.raw["leadfeeder"].get("max_requests_per_min", 95))

    @property
    def lf_max_retries(self) -> int:
        return int(self.raw["leadfeeder"].get("max_retries", 4))

    @property
    def lf_retrieve_batch(self) -> int:
        return int(self.raw["leadfeeder"].get("retrieve_batch", 100))

    @property
    def lf_match_batch(self) -> int:
        return int(self.raw["leadfeeder"].get("match_batch", 200))
```

In `src/leadauto/cli.py`, replace `_build_lf_client` (lines ~41-47) with:

```python
def _build_lf_client(settings: Settings, logger) -> LeadfeederClient:
    return LeadfeederClient(
        api_key=settings.leadfeeder_api_key,
        base_url=settings.lf_base_url,
        min_match_score=settings.lf_min_match_score,
        retrieve_batch=settings.lf_retrieve_batch,
        match_batch=settings.lf_match_batch,
        max_requests_per_min=settings.lf_max_requests_per_min,
        max_retries=settings.lf_max_retries,
        logger=logger,
    )
```

- [ ] **Step 4: Run the config test**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config/config.yaml src/leadauto/config.py src/leadauto/cli.py tests/test_config.py
git commit -m "feat(leadfeeder): config knobs for throttle/retry/batch sizes"
```

---

### Task 7: Full-suite verification + live read-only probe

**Files:**
- Create: `scripts/leadfeeder_probe.py`

**Interfaces:**
- Consumes: `Settings`, `LeadfeederClient` (final, from Tasks 1–6).
- Produces: a standalone read-only probe (no SF, no writes).

- [ ] **Step 1: Run the full unit suite**

Run: `python -m pytest -q`
Expected: PASS — all tests (119 prior + the new transport/batch/config tests) green.

- [ ] **Step 2: Write the live probe script**

Create `scripts/leadfeeder_probe.py`:

```python
"""Read-only LeadFeeder probe — confirms the live API shape and that throttle/retry work.

Usage (PowerShell):
    $env:PYTHONPATH = "src"; $env:LEADFEEDER_API_KEY = "<key>"
    python scripts/leadfeeder_probe.py "kununu" "DE"

Spends 0 credits for match + search; retrieving one contact may cost 1 credit
(free if that contact was accessed in the last 12 months). No Salesforce calls.
"""
from __future__ import annotations

import sys

from leadauto.config import Settings
from leadauto.leadfeeder.client import LeadfeederClient, flatten_contact_details


def main() -> int:
    company = sys.argv[1] if len(sys.argv) > 1 else "kununu"
    country_code = sys.argv[2] if len(sys.argv) > 2 else "DE"
    settings = Settings.load()
    client = LeadfeederClient(
        api_key=settings.leadfeeder_api_key,
        base_url=settings.lf_base_url,
        min_match_score=settings.lf_min_match_score,
        retrieve_batch=settings.lf_retrieve_batch,
        match_batch=settings.lf_match_batch,
        max_requests_per_min=settings.lf_max_requests_per_min,
        max_retries=settings.lf_max_retries,
    )

    print(f"Account: {client.get_account_id()}")
    [match] = client.match_companies([{"company_name": company, "country_code": country_code}])
    print(f"Match: {match}")
    if not match:
        print("No company match; stopping (search/retrieve need a company id).")
        return 0

    hit = client.search_contact(company, match["company_id"])
    print(f"Search first hit: {hit}")
    if not hit:
        print("No contact found; search confirmed (free). Done.")
        return 0

    attrs = client.retrieve_contacts([hit["contact_id"]])
    print(f"retrieve_contacts keys: {list(attrs)}")
    print(f"Flattened: {flatten_contact_details(attrs.get(hit['contact_id'], {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Commit the probe**

```bash
git add scripts/leadfeeder_probe.py
git commit -m "chore(leadfeeder): read-only live probe script"
```

- [ ] **Step 4: Run the live probe (requires `LEADFEEDER_API_KEY`)**

Run (PowerShell):
```powershell
$env:PYTHONPATH = "src"; python scripts/leadfeeder_probe.py "kununu" "DE"
```
Expected: prints an account id, a `Match` dict (or "No company match"), a search hit, and the `retrieve_contacts` shape. Confirms the live response shapes match the design and that no `429`/throttle errors surface. **If the API key isn't available locally, defer this step to the existing end-to-end validation task and note it.**

---

## Self-Review

**Spec coverage:**
- #1 throttle + retry → Task 1. ✅
- #6 batch match (≤200) → Task 2 (client) + Task 4 (enrich). ✅
- #2 batch contact retrieve (≤100) → Task 3 (client) + Task 5 (enrich). ✅
- #4 retrieve batch 20→100 → Task 1 (constructor default) + Task 6 (config). ✅
- #5 surface list-add errors → Tasks 4 & 5 (both list-add sites). ✅
- Config knobs (95/min, 4 retries, batch sizes) → Task 6. ✅
- Validation: unit tests throughout + live probe → Task 7. ✅
- Behavior-neutral preserved: `match_company`/`get_contact_details` wrappers keep existing client tests green; cascade logic and `min_match_score` unchanged. ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every run step states the command + expected result.

**Type consistency:** `match_companies(list[dict]) -> list[dict|None]` (Task 2) consumed index-aligned in Task 4; `retrieve_contacts(list[str]) -> dict[str,dict]` (Task 3) consumed in Task 5 with `flatten_contact_details`; `Settings.lf_*` names (Task 6) match `cli._build_lf_client` and the `LeadfeederClient` constructor kwargs from Task 1. `RETRYABLE_STATUS` defined once (Task 1). Consistent.
