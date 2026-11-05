"""LeadFeeder HTTP client + pure flatteners (Module 2).

Ported from the legacy `leadfeeder_pipeline.py`, with the hardcoded API key, Excel I/O,
and CSV export removed. The key comes from `LEADFEEDER_API_KEY` (env / GitHub Secret) and
the `requests.Session` is injectable so the orchestration can be tested without the API.

Credit model (unchanged): company match / list adds / contact search are free; contact
details and company retrieve cost 1 credit each.
"""
from __future__ import annotations

import re
import time

import requests
from rapidfuzz import fuzz

from ..logging import get_logger
from ..textnorm import alnum, domain_root, normalize_name

DEFAULT_BASE_URL = "https://api.leadfeeder.com/v1"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Failures that never produce a response object, so the status check below cannot see them.
# `ConnectTimeout` subclasses both ConnectionError and Timeout; listing both is harmless.
RETRYABLE_TRANSPORT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
# (connect, read) seconds. WITHOUT this `requests` waits forever, so a stalled — as opposed
# to dropped — socket hangs the process and RETRYABLE_TRANSPORT's `Timeout` arm is dead code:
# nothing raises it. Measured 2026-08-17: a backtest sat 26 minutes on one established
# connection to api.leadfeeder.com at 0% CPU before it was killed by hand. The read budget is
# generous because `/companies/match` with 200 companies is genuinely slow.
DEFAULT_TIMEOUT = (10, 180)
_PHONE_EXCLUDE_TYPES = {"fax"}
# Keys the gate uses locally that the match endpoint must never receive.
_LOCAL_ONLY_MATCH_KEYS = {"alt_names"}

# `max_results_per_company` on POST /companies/match. The primary pass asks for 3 and
# must keep asking for 3: the calibration (6-month control set 2026-07-02 + the 283-case
# backtest) was measured at that width, and widening it can only ADD candidates that
# outrank the accepted one on `(match_score, agreement)` — i.e. it can silently move a
# working match. The second-chance pass in `enrich` asks for MATCH_RESULTS_WIDE, and only
# for leads the primary pass left unmatched, which have nothing to lose. Prior art and the
# reason it works: `Contact Presence Check`'s rescue pass (2026-08-23) — LF holds several
# company records for one company and the top 3 are often the wrong (emptier) ones.
MATCH_RESULTS_PRIMARY = 3
MATCH_RESULTS_WIDE = 20
MATCH_RESULTS_MAX = 20   # machine-readable spec (openapi.yaml 2026-08-10); the prose doc says 10
# Per-company result of `match_companies` when its CHUNK failed (transport error after
# retries). Falsy like None, so every `if match:` caller degrades exactly as before, but
# `is not None` — so the second-chance pass can tell "LF rejected it" (None) from "LF was
# never asked" and not re-ask (and mislabel as `wide`) a lead the primary pass never judged.
MATCH_CHUNK_FAILED: dict = {}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LeadfeederError(RuntimeError):
    pass


# --- match payload sanitizing -------------------------------------------------

def match_email(value: str | None) -> str | None:
    """An address safe to send as the match payload's `email`, else None (= omit the key).

    LeadFeeder answers `"email": ""` — and anything else that is not an address — with
    `400 invalid_parameter: The 'email' field must contain a valid email address.`
    Omitting the key is accepted. Verified live 2026-08-08.
    """
    v = (value or "").strip()
    return v if _EMAIL_RE.match(v) else None


def match_api_payload(payload: dict) -> dict:
    """A per-company match payload as the endpoint accepts it.

    Drops the local-only gate inputs (`alt_names` never leaves this process) and omits
    `email` unless it is a real address. This runs at the transport boundary on purpose:
    a 400 fails the WHOLE chunk, so ONE lead with a blank or garbage email would degrade
    up to 200 companies to "no match" — which reads downstream as "LeadFeeder knew none
    of them" (terminal `skip`), not as an API error. Callers must not be able to poison
    a batch by passing `email or ''`.
    """
    out = {k: v for k, v in payload.items() if k not in _LOCAL_ONLY_MATCH_KEYS}
    email = match_email(out.pop("email", None))
    if email:
        out["email"] = email
    return out


# --- pure flatteners ---------------------------------------------------------

def flatten_company(attrs: dict) -> dict:
    """Flatten a LeadFeeder company `attributes` object to a flat enrichment dict."""
    attrs = attrs or {}
    address = attrs.get("address", {}) or {}
    revenue = attrs.get("revenue", {}) or {}
    industries_raw = attrs.get("industries", {}) or {}
    register = attrs.get("register", {}) or {}

    # LF sends explicit "name": null entries — drop them, a join over None raises.
    industry_names = [n for ind in industries_raw.get("industry", []) or []
                      if (n := ind.get("name"))]
    phones = attrs.get("phones", []) or []
    phone = phones[0].get("number", "") if phones else ""
    emails = attrs.get("emails", []) or []
    company_email = emails[0].get("address", "") if emails else ""
    socials = attrs.get("social_media_profiles", {}) or {}
    linkedin_list = socials.get("linkedin", []) or []
    linkedin = linkedin_list[0].get("url", "") if linkedin_list else ""

    return {
        "Website": attrs.get("url", ""),
        "Company_Phone": phone,
        "Company_Email": company_email,
        "Street": address.get("street_address", ""),
        "Postal_Code": address.get("postal_code", ""),
        "City": address.get("city", ""),
        "Country": address.get("country", ""),
        "Employee_Count": attrs.get("employee_count", ""),
        "Employee_Range": attrs.get("employee_range", ""),
        "Industry": "; ".join(industry_names),
        "Revenue": revenue.get("value", ""),
        "Revenue_Currency": revenue.get("currency", ""),
        "Founded_Year": attrs.get("founded_year", ""),
        "Legal_Form": attrs.get("legal_form", ""),
        "Register_Id": register.get("id", ""),
        "Register_Status": register.get("status", ""),
        "VAT_Id": attrs.get("vat_id", ""),
        "Company_LinkedIn": linkedin,
        "Description": (attrs.get("description", "") or "").replace("\n", " ").replace("\r", " "),
    }


def flatten_contact_details(attrs: dict) -> dict:
    """Flatten a LeadFeeder contact `attributes` object, prioritizing phone types."""
    attrs = attrs or {}
    position = (attrs.get("position", {}) or {}).get("title", "")
    emails = [e.get("address", "") for e in (attrs.get("emails", []) or []) if e.get("address")]

    phones_all: list[str] = []
    phone_direct = phone_mobile = phone_office = ""
    for p in attrs.get("phones", []) or []:
        number = p.get("number", "")
        ptype = (p.get("type", "") or "").lower().strip()
        if not number or p.get("blacklisted", False):
            continue
        if ptype in _PHONE_EXCLUDE_TYPES:
            continue
        phones_all.append(f"{number} ({ptype})")
        if "direct" in ptype and not phone_direct:
            phone_direct = number
        elif "mobile" in ptype and not phone_mobile:
            phone_mobile = number
        elif ("office" in ptype or "landline" in ptype) and not phone_office:
            phone_office = number

    best_contact_phone = (
        phone_direct or phone_mobile or phone_office
        or (phones_all[0].split(" (")[0] if phones_all else "")
    )

    socials = attrs.get("social_media_profiles", {}) or {}
    linkedin_list = socials.get("linkedin", []) or []
    linkedin = linkedin_list[0].get("url", "") if linkedin_list else ""
    xing_list = socials.get("xing", []) or []
    xing = xing_list[0].get("url", "") if xing_list else ""

    return {
        "position": position,
        "contact_emails": "; ".join(emails),
        "phone_direct": phone_direct,
        "phone_mobile": phone_mobile,
        "phone_office": phone_office,
        "best_contact_phone": best_contact_phone,
        "phones_all": "; ".join(phones_all),
        "contact_linkedin": linkedin,
        "contact_xing": xing,
        "departments": "; ".join(d for d in attrs.get("departments") or [] if d),
        "hierarchy_level": attrs.get("hierarchy_level", ""),
    }


# --- client ------------------------------------------------------------------

class LeadfeederClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        session=None,
        min_match_score: float = 0.70,
        name_agree_min: int = 85,
        name_agree_rescue: int = 90,
        retrieve_batch: int = 100,
        match_batch: int = 200,
        max_requests_per_min: int = 95,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        timeout=DEFAULT_TIMEOUT,
        sleep_fn=time.sleep,
        clock=time.monotonic,
        logger=None,
    ):
        if not api_key:
            raise LeadfeederError("LEADFEEDER_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.credits_charged = 0   # sum of meta.credits.charged over this client's calls
        self.min_match_score = min_match_score
        self.name_agree_min = name_agree_min
        self.name_agree_rescue = name_agree_rescue
        self.retrieve_batch = retrieve_batch
        self.match_batch = match_batch
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._min_interval = 60.0 / max_requests_per_min if max_requests_per_min else 0.0
        self._last_request_at = 0.0
        self.log = logger or get_logger("leadfeeder")
        self._account_id: str | None = None

    # -- low-level --
    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._last_request_at + self._min_interval - self._clock()
        if wait > 0:
            self._sleep_fn(wait)
        # record the post-sleep timestamp so pacing reflects when this request actually fires
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
            try:
                if kind == "GET":
                    resp = self.session.get(url, headers=self._headers(), params=params,
                                            timeout=self.timeout)
                else:
                    resp = self.session.post(url, headers=self._headers(), json=json,
                                             params=params, timeout=self.timeout)
            except RETRYABLE_TRANSPORT as exc:
                # A dropped socket raises here instead of returning a response, so before
                # 2026-08-17 it bypassed the status retry below entirely and killed the run
                # mid-sweep. Ported from `Contact Presence Check`'s `enrich_pilot.get_with_retry`
                # (2026-08-11), where a `WinError 10053` ended a 50-minute run AFTER 485
                # retrieve credits were spent. GET ONLY, deliberately: POST
                # `/contacts/find-contact-data-jobs` is not idempotent and a blind re-POST
                # would bill the credits twice.
                if kind != "GET":
                    self.log.warning("LeadFeeder POST %s transport error (%s); not retried "
                                     "(a POST may have been applied server-side)",
                                     path, type(exc).__name__)
                    raise
                if attempt >= self.max_retries:
                    self.log.warning("LeadFeeder GET %s transport error (%s); retries exhausted",
                                     path, type(exc).__name__)
                    raise
                delay = self._retry_delay(None, attempt)
                self.log.warning("LeadFeeder GET %s transport error (%s); retry %d/%d in %.1fs",
                                 path, type(exc).__name__, attempt + 1, self.max_retries, delay)
                self._sleep_fn(delay)
                continue
            status = getattr(resp, "status_code", 200)
            if status in RETRYABLE_STATUS and attempt < self.max_retries:
                delay = self._retry_delay(resp, attempt)
                self.log.warning("LeadFeeder %s %s -> %s; retry %d/%d in %.1fs",
                                 kind, path, status, attempt + 1, self.max_retries, delay)
                self._sleep_fn(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise LeadfeederError(f"LeadFeeder {kind} {path} exhausted retries without a response")

    def api_get(self, path: str, params=None) -> dict:
        return self._note_credits(self._request("GET", path, params=params))

    def api_post(self, path: str, payload, params=None) -> dict:
        return self._note_credits(self._request("POST", path, params=params, json=payload))

    def _note_credits(self, data: dict) -> dict:
        """Accumulate `meta.credits.charged` into `credits_charged`, unchanged pass-through.

        Every LF response carries what it billed. Summing it turns "match is free" from a
        claim into a number a report can print, and makes an accidentally-billing call
        visible instead of silent. Never raises on a shape it does not recognise: a
        miscounted credit must not sink a run.
        """
        try:
            charged = ((data or {}).get("meta", {}) or {}).get("credits", {}) or {}
            self.credits_charged += int(charged.get("charged", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            pass
        return data

    # -- account / lists --
    def get_account_id(self) -> str:
        if self._account_id:
            return self._account_id
        accounts = self.api_get("/accounts").get("data", [])
        if not accounts:
            raise LeadfeederError("No accounts found for this API key.")
        self._account_id = accounts[0]["id"]
        self.log.info("LeadFeeder account: %s (%s)",
                      accounts[0].get("attributes", {}).get("name", ""), self._account_id)
        return self._account_id

    def find_or_create_list(self, name: str, scope: str) -> str:
        account_id = self.get_account_id()
        page = 1
        while True:
            data = self.api_get("/lists", params={
                "account_id": account_id, "filter[scope]": scope,
                "page[num]": page, "page[size]": 50,
            })
            for lst in data.get("data", []):
                if lst.get("attributes", {}).get("name") == name:
                    return lst["id"]
            pagination = data.get("meta", {}).get("pagination", {})
            if page >= pagination.get("page_count", 1):
                break
            page += 1
        result = self.api_post("/lists", payload={
            "data": {"type": "list", "attributes": {"name": name, "scope": scope}},
        }, params={"account_id": account_id})
        list_id = result["data"]["id"]
        self.log.info("Created LeadFeeder list %r (%s)", name, list_id)
        return list_id

    # -- company match (batched) --
    def _name_agreement(self, matched_name: str, payload: dict) -> tuple[int, str | None]:
        """(0-100 agreement, winning signal) between LF's returned name and the
        request's identity signals: the sent company_name, any alt_names (assigned
        companies of the lead's kununu profile), and — for corporate addresses — the
        email-domain root. (0, None) when LF returned no name (fail closed)."""
        cand = normalize_name(matched_name)
        if not cand:
            return 0, None
        best, via = 0, None
        ours = normalize_name(payload.get("company_name"))
        if ours:
            s = int(fuzz.token_set_ratio(cand, ours))
            if s > best:
                best, via = s, "company_name"
        # alt_names must score strictly higher than best to override.
        for alt in payload.get("alt_names") or []:
            alt_norm = normalize_name(alt)
            if not alt_norm:
                continue
            s = int(fuzz.token_set_ratio(cand, alt_norm))
            if s > best:
                best, via = s, f"alt_name:{alt}"
        root = domain_root(payload.get("email"))
        if root:
            s = int(fuzz.partial_ratio(alnum(matched_name), root))
            if s > best:
                best, via = s, "email_domain"
        return best, via

    def _best_match(self, matches: list, payload: dict) -> dict | None:
        """Best candidate passing the acceptance gate, or None.

        Gate (calibrated on the 6-month converted-leads control set, 2026-07-02 —
        see Export/lf_match_check_6m.xlsx):
          - match_score >= min_match_score AND name agreement >= name_agree_min, OR
          - name agreement >= name_agree_rescue regardless of score (LF's score is
            unreliably low for small companies even on exact name hits).
        A high LF score alone is NOT trusted: 0.9+ scores were observed on entirely
        wrong companies, so score-only acceptance would write a stranger's firmographics.
        """
        best_key, best = None, None
        for m in matches or []:
            score = m.get("attributes", {}).get("match_score", 0)
            summary = m.get("relationships", {}).get("company_summary", {}).get("attributes", {})
            agree, via = self._name_agreement(summary.get("name", ""), payload)
            if not ((score >= self.min_match_score and agree >= self.name_agree_min)
                    or agree >= self.name_agree_rescue):
                continue
            key = (score, agree)
            if best_key is None or key > best_key:
                best_key = key
                best = {"company_id": m["id"], "match_score": score,
                        "matched_name": summary.get("name", ""), "name_agreement": agree,
                        "agreed_via": via}
                # The summary carries the company's website and address since at least
                # 2026-11-05 (it did not when the sanity gate was written, 2026-07-09).
                # Carried ONLY when present so a summary without them is unchanged:
                # the domain-rescue pass in `enrich` corroborates on `matched_url`
                # BEFORE spending the retrieve credit.
                url = summary.get("url")
                if url:
                    best["matched_url"] = url
                cc = (summary.get("address") or {}).get("country_code")
                if cc:
                    best["matched_country_code"] = cc
        return best

    def match_companies(self, payloads: list[dict],
                        max_results: int = MATCH_RESULTS_PRIMARY) -> list[dict | None]:
        """Batch company match (free). `payloads` is a list of per-company dicts
        (company_name, email, country, country_code, ...). Returns a list aligned to
        `payloads`: {company_id, match_score, matched_name, name_agreement, agreed_via}
        or None per company. `name_agreement` is a 0-100 int scoring how well the matched
        name agrees with the payload's company_name / alt_names / email-domain root;
        `agreed_via` names which signal vouched for the match (`company_name`,
        `alt_name:<name>`, or `email_domain`), or None if nothing agreed. A chunk that
        errors after retries degrades its entries to None (logged), never raising.

        `max_results` is LF's `max_results_per_company` (1-20 per the machine-readable
        spec; the prose doc's 1-10 is wrong). It changes only how many candidates LF
        RETURNS — `_best_match` already gates every one of them, so a wider call cannot
        accept anything the narrow call would have rejected. See `MATCH_RESULTS_PRIMARY`
        for why the primary pass keeps 3.
        """
        if not payloads:
            return []
        if not 1 <= max_results <= MATCH_RESULTS_MAX:
            raise ValueError(f"max_results must be 1..{MATCH_RESULTS_MAX}, got {max_results}")
        account_id = self.get_account_id()
        results: list[dict | None] = []
        for i in range(0, len(payloads), self.match_batch):
            chunk = payloads[i:i + self.match_batch]
            api_chunk = [match_api_payload(p) for p in chunk]
            try:
                data = self.api_post(
                    "/companies/match", payload={"companies": api_chunk},
                    params={"account_id": account_id,
                            "max_results_per_company": max_results},
                )
            except requests.exceptions.RequestException as e:
                self.log.warning("Company match chunk failed (%d companies): %s", len(chunk), e)
                # MATCH_CHUNK_FAILED (falsy, `is not None`) lets a caller tell "LF was
                # asked and rejected it" (None) from "LF was never asked" — the wide
                # pass must not re-ask the latter and then label a hit as `wide`.
                results.extend([MATCH_CHUNK_FAILED] * len(chunk))
                continue
            per_company = data.get("data", []) or []
            for j in range(len(chunk)):
                matches = per_company[j] if j < len(per_company) else []
                results.append(self._best_match(matches, chunk[j]))
        return results

    def add_company_to_list(self, company_id: str, list_id: str) -> None:
        self.api_post(f"/companies/{company_id}/lists",
                      payload={"data": [{"type": "list", "id": list_id}]},
                      params={"account_id": self.get_account_id()})

    # -- contact search (4-attempt cascade) --
    def search_contact(self, name: str, company_id: str) -> dict | None:
        account_id = self.get_account_id()
        parts = name.strip().split()

        def _post(terms):
            payload = {"company_ids": [company_id]}
            if terms is not None:
                payload["search_terms"] = terms
            return self.api_post("/contacts/search", payload=payload,
                                 params={"account_id": account_id, "page[size]": 5}).get("data", [])

        # A hyphenated name part also counts as matched via one of its segments:
        # the lead may carry "Kothe-Spies" while LF stores just "Kothe" (or vice versa).
        def _segments(part):
            if "-" not in part:
                return []
            return [s for s in part.split("-") if len(s) >= 2]

        def _part_in(part, full):
            p = part.lower()
            return p in full or any(s.lower() in full for s in _segments(part))

        def _all_parts_ok(c):
            """The searched name's FIRST and LAST parts both appear in the returned name.

            Attempts 1 and 2 originally returned `contacts[0]` UNVERIFIED, which is wrong
            for two compounding reasons (measured 2026-08-10 over a 100-contact sample:
            13 of 64 hits, 20%, were a different person):

              * `/contacts/search` ORs multiple `search_terms`, so attempt 2's
                ["Christian", "Etscher"] matches anyone sharing EITHER name — it returned
                "Christina Zepf". The API doc states the OR; this is not a quirk.
              * taking [0] ignored the other candidates on the page, and the ordering is
                not stable — replaying "Peter Schmidt" returned "Tim Schmidt" where the
                first run had returned "Roland Schmid".

            Filtering (rather than checking [0] and giving up) also repairs the cascade:
            an attempt whose candidates are all wrong now leaves `contacts` empty, so the
            `if not contacts` guards below correctly fall through to attempts 3 and 4
            instead of stopping on a stranger.

            All four attempts apply this same test. Attempt 3 used to check only the FIRST
            name, on the assumption that searching the surname server-side had already
            constrained the surname — but `search_terms` is fuzzy (and matches the title
            field too), so it does not. Measured live: searching "Hübner" returns "Markus
            Huber", and "Weidemann" returns "Paul Weimann". Under the old check a lead
            named "Paul Weidemann" would therefore have been accepted as "Paul Weimann".

            MIDDLE parts are ignored on purpose: the two systems disagree about them
            constantly, in both directions. Requiring every part cost a real match on
            2026-08-10 — Salesforce "Peter Sven Mitterhuemer" against LF "Peter
            Mitterhuemer" — while requiring only the first and last names accepts that and
            still rejects every wrong-person case above, since those differ on a first or
            last name, never on a middle one.
            """
            full = (c.get("attributes", {}).get("full_name", "") or "").lower()
            return _part_in(parts[0], full) and _part_in(parts[-1], full)

        # Attempt 1: full name as a single term
        contacts = [c for c in _post([name]) if _all_parts_ok(c)]

        # Attempt 2: split into separate terms
        if not contacts and len(parts) > 1:
            contacts = [c for c in _post(parts) if _all_parts_ok(c)]

        # Attempt 3: last name only (then its hyphen segments), verify the name locally
        if not contacts and len(parts) > 1:
            for term in [parts[-1]] + _segments(parts[-1]):
                contacts = [c for c in _post([term]) if _all_parts_ok(c)]
                if contacts:
                    break

        # Attempt 4: browse the company's contacts, match all parts locally
        if not contacts:
            try:
                resp = self.api_post("/contacts/search", payload={"company_ids": [company_id]},
                                     params={"account_id": account_id, "page[size]": 50})
                for c in resp.get("data", []):
                    if _all_parts_ok(c):
                        contacts = [c]
                        break
            except requests.exceptions.HTTPError:
                contacts = []

        if not contacts:
            return None
        c = contacts[0]
        return {"contact_id": c["id"], "full_name": c.get("attributes", {}).get("full_name", "")}

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
            except requests.exceptions.RequestException as e:
                self.log.warning("Contact retrieve batch failed: %s", e)
        return out

    def find_contact_data(self, contact_ids: list[str], timeout: float = 180.0,
                          poll_interval: float = 10.0) -> bool:
        """Trigger LF's 'Find contact details' enrichment and wait for it to finish.

        POST /contacts/find-contact-data-jobs starts an async external-provider lookup
        (≤100 ids per job; 1 credit per contact that actually gains data, free
        otherwise); the job status is then polled until completed/failed or `timeout`.
        Returns True when every job completed — the caller should re-retrieve the
        contacts to pick up new phones/emails. False on failure or timeout: callers
        degrade to the data they already have.
        """
        if not contact_ids:
            return False
        account_id = self.get_account_id()
        job_ids: list[str] = []
        for i in range(0, len(contact_ids), 100):
            resp = self.api_post("/contacts/find-contact-data-jobs", payload={
                "data": {"type": "find_contact_data_job",
                         "attributes": {"contact_ids": contact_ids[i:i + 100],
                                        "enrichment_type": "full"}},
            }, params={"account_id": account_id})
            job_ids.append(resp["data"]["id"])
        deadline = self._clock() + timeout
        pending = set(job_ids)
        failed = False
        while pending:
            for jid in sorted(pending):
                resp = self.api_get(f"/contacts/find-contact-data-jobs/{jid}",
                                    params={"account_id": account_id})
                status = ((resp.get("data") or {}).get("attributes") or {}).get("status", "")
                if status in ("completed", "failed"):
                    pending.discard(jid)
                    if status == "failed":
                        failed = True
                        self.log.warning("Find-contact-data job %s failed", jid)
            if not pending:
                break
            if self._clock() >= deadline:
                self.log.warning("Find-contact-data job(s) timed out after %.0fs: %s",
                                 timeout, sorted(pending))
                return False
            self._sleep_fn(poll_interval)
        return not failed

    def add_contact_to_list(self, contact_id: str, list_id: str) -> None:
        self.api_post(f"/contacts/{contact_id}/lists",
                      payload={"data": [{"type": "list", "id": list_id}]},
                      params={"account_id": self.get_account_id()})

    # -- company enrichment (batched retrieve) --
    def retrieve_companies(self, company_ids: list[str]) -> dict:
        account_id = self.get_account_id()
        enriched: dict[str, dict] = {}
        for i in range(0, len(company_ids), self.retrieve_batch):
            batch = company_ids[i:i + self.retrieve_batch]
            try:
                data = self.api_get("/companies", params={"ids": ",".join(batch), "account_id": account_id})
                for company in data.get("data", []):
                    enriched[company["id"]] = company.get("attributes", {})
            except requests.exceptions.RequestException as e:
                self.log.warning("Company retrieve batch failed: %s", e)
        return enriched
