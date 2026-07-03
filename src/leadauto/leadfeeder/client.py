"""LeadFeeder HTTP client + pure flatteners (Module 2).

Ported from the legacy `leadfeeder_pipeline.py`, with the hardcoded API key, Excel I/O,
and CSV export removed. The key comes from `LEADFEEDER_API_KEY` (env / GitHub Secret) and
the `requests.Session` is injectable so the orchestration can be tested without the API.

Credit model (unchanged): company match / list adds / contact search are free; contact
details and company retrieve cost 1 credit each.
"""
from __future__ import annotations

import time

import requests
from rapidfuzz import fuzz

from ..logging import get_logger
from ..textnorm import alnum, domain_root, normalize_name

DEFAULT_BASE_URL = "https://api.leadfeeder.com/v1"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_PHONE_EXCLUDE_TYPES = {"fax"}


class LeadfeederError(RuntimeError):
    pass


# --- pure flatteners ---------------------------------------------------------

def flatten_company(attrs: dict) -> dict:
    """Flatten a LeadFeeder company `attributes` object to a flat enrichment dict."""
    attrs = attrs or {}
    address = attrs.get("address", {}) or {}
    revenue = attrs.get("revenue", {}) or {}
    industries_raw = attrs.get("industries", {}) or {}
    register = attrs.get("register", {}) or {}

    industry_names = [ind.get("name", "") for ind in industries_raw.get("industry", []) or []]
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
        "departments": "; ".join(attrs.get("departments", []) or []),
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
        self.name_agree_min = name_agree_min
        self.name_agree_rescue = name_agree_rescue
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
        raise LeadfeederError(f"LeadFeeder {kind} {path} exhausted retries without a response")

    def api_get(self, path: str, params=None) -> dict:
        return self._request("GET", path, params=params)

    def api_post(self, path: str, payload, params=None) -> dict:
        return self._request("POST", path, params=params, json=payload)

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
        # alt_names can override company_name on a tie (more specific signal); among
        # alt_names themselves the first one that reaches the best score wins.
        alt_threshold = best - 1
        for alt in payload.get("alt_names") or []:
            alt_norm = normalize_name(alt)
            if not alt_norm:
                continue
            s = int(fuzz.token_set_ratio(cand, alt_norm))
            if s > alt_threshold:
                alt_threshold = s
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
        return best

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
            api_chunk = [{k: v for k, v in p.items() if k != "alt_names"} for p in chunk]
            try:
                data = self.api_post(
                    "/companies/match", payload={"companies": api_chunk},
                    params={"account_id": account_id, "max_results_per_company": 3},
                )
            except requests.exceptions.RequestException as e:
                self.log.warning("Company match chunk failed (%d companies): %s", len(chunk), e)
                results.extend([None] * len(chunk))
                continue
            per_company = data.get("data", []) or []
            for j in range(len(chunk)):
                matches = per_company[j] if j < len(per_company) else []
                results.append(self._best_match(matches, chunk[j]))
        return results

    def match_company(self, company_name: str, email: str | None = None,
                      country: str | None = None, country_code: str | None = None) -> dict | None:
        payload = {"company_name": company_name, "email": email or ""}
        if country:
            payload["country"] = country
        if country_code:
            payload["country_code"] = country_code
        return self.match_companies([payload])[0]

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

        # Attempt 1: full name as a single term
        contacts = _post([name])

        # Attempt 2: split into separate terms
        if not contacts and len(parts) > 1:
            contacts = _post(parts)

        # Attempt 3: last name only, then verify first name locally
        if not contacts and len(parts) > 1:
            candidates = _post([parts[-1]])
            filtered = [c for c in candidates
                        if parts[0].lower() in (c.get("attributes", {}).get("full_name", "") or "").lower()]
            contacts = filtered

        # Attempt 4: browse the company's contacts, match all parts locally
        if not contacts:
            try:
                resp = self.api_post("/contacts/search", payload={"company_ids": [company_id]},
                                     params={"account_id": account_id, "page[size]": 50})
                for c in resp.get("data", []):
                    full = (c.get("attributes", {}).get("full_name", "") or "").lower()
                    if all(p.lower() in full for p in parts):
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

    def get_contact_details(self, contact_id: str) -> dict:
        attrs = self.retrieve_contacts([contact_id]).get(contact_id, {})
        return flatten_contact_details(attrs)

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
