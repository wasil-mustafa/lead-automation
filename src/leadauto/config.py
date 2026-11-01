"""Configuration: merges non-secret config.yaml with secrets from env / GitHub Secrets.

Precedence: environment/secrets > config.yaml. Secrets are never stored in the repo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .textnorm import domain_sld

try:  # optional locally; absent in CI is fine
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

# repo root is two levels up from src/leadauto/config.py
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.yaml"


@dataclass
class Settings:
    raw: dict
    leadfeeder_api_key: str | None
    sf_auth_url: str | None

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        if load_dotenv:
            load_dotenv(REPO_ROOT / ".env")
        path = Path(config_path) if config_path else DEFAULT_CONFIG
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            raw=raw,
            leadfeeder_api_key=os.environ.get("LEADFEEDER_API_KEY"),
            # Accept the existing repo secret name (SFDX_AUTH_URL) as a fallback so the
            # ensure_org sfdx-url login path works without a second secret.
            sf_auth_url=os.environ.get("SF_AUTH_URL") or os.environ.get("SFDX_AUTH_URL"),
        )

    # --- Salesforce ---
    @property
    def org_alias(self) -> str:
        return self.raw["salesforce"]["org_alias"]

    @property
    def api_version(self) -> str:
        return str(self.raw["salesforce"].get("api_version", "66.0"))

    @property
    def converted_status(self) -> str:
        return self.raw["salesforce"].get("converted_status", "Qualified")

    @property
    def owner_id(self) -> str | None:
        return self.raw["salesforce"].get("owner_id")

    @property
    def rohdaten_pool_owner_id(self) -> str | None:
        """OwnerId of the 'Rohdaten Pool' user; linked Accounts with this owner are
        the only ones the Stage 2 pool-enrichment step may update."""
        return self.raw["salesforce"].get("rohdaten_pool_owner_id")

    # --- LeadFeeder ---
    @property
    def lf_base_url(self) -> str:
        return self.raw["leadfeeder"]["base_url"]

    @property
    def lf_company_list(self) -> str:
        return self.raw["leadfeeder"].get("company_list_name", "Lead_Automation")

    @property
    def lf_contact_list(self) -> str:
        return self.raw["leadfeeder"].get("contact_list_name", "Lead_Automation")

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

    @property
    def lf_match_wide_results(self) -> int:
        """`max_results_per_company` for the second-chance match on leads the primary
        pass left unmatched (see leadfeeder/enrich.py). 0 disables the pass; the API
        ceiling is 20 (client.MATCH_RESULTS_MAX). The primary pass is NOT affected.
        An ABSENT key means 0: the pass was measured at 0 of 103 recovered (2026-08-25)
        and disabled, so a config that forgets the key must not silently re-enable it."""
        return int(self.raw["leadfeeder"].get("match_wide_results", 0))

    @property
    def live_allow_wide_match(self) -> bool:
        """False (default) keeps second-chance matches OUT of the live scope: they are
        predicted, written to the QA artifact and shadow-scored, but never written back
        or converted. Flip it once the shadow evidence says the pass is accurate."""
        return bool((self.raw["pipeline"].get("live_scope") or {})
                    .get("allow_wide_match", False))

    @property
    def lf_campaigns(self) -> dict[str, str]:
        """Bucket -> CampaignId for the live-run LeadFeeder-outcome tagging
        (campaigns.py). Blank ids are dropped; an absent block means the feature is
        off — no tagging and no campaign exclusion on the lead read."""
        block = (self.raw.get("pipeline") or {}).get("lf_campaigns") or {}
        return {k: str(v).strip() for k, v in block.items() if v and str(v).strip()}

    @property
    def lf_campaign_task_types(self) -> list[str] | None:
        """Parser keys whose leads get an LF-outcome campaign; None (key absent) =
        every enabled type. `claimer` since 2026-10-19, `ebp_trial` too since
        2026-10-20 (Wasil)."""
        types = (self.raw.get("pipeline") or {}).get("lf_campaign_task_types")
        return None if types is None else [str(t) for t in types]

    @property
    def lf_contact_lookup_only_when_needed(self) -> bool:
        """True runs the LF contact cascade (search + 1-credit retrieve + find-contact-data)
        ONLY for leads whose Lead record still lacks an email or a phone — the two fields
        the cascade exists to fill (Wasil, 2026-09-18, for the ZPE event leads: 116/116
        had an email and 109/116 a phone, so 94 lookups would have served 7 leads).
        A lead that already carries both keeps its values and costs nothing. False
        (default, the scheduled pipeline) looks every matched lead up as before, which
        also yields Title/LinkedIn/FirstName for leads that have email and phone."""
        return bool(self.raw["leadfeeder"].get("contact_lookup_only_when_needed", False))

    @property
    def lf_find_contact_data(self) -> bool:
        return bool(self.raw["leadfeeder"].get("find_contact_data", True))

    @property
    def lf_find_details_timeout(self) -> float:
        return float(self.raw["leadfeeder"].get("find_details_timeout", 180))

    @property
    def lf_find_details_poll(self) -> float:
        return float(self.raw["leadfeeder"].get("find_details_poll", 10))

    # --- kununu profile feed ---
    @property
    def kununu_feed_enabled(self) -> bool:
        return bool(self.raw.get("kununu_feed", {}).get("enabled", False))

    @property
    def kununu_feed_cache_path(self) -> Path | None:
        """Explicit cache-path override, or None to let `common.kununu_feed` decide.

        There is deliberately NO default any more. The old one, `kununu_profiles.jsonl`
        at the repo root, was wrong twice over: it named JSONL after kununu switched the
        feed to CSV on 2026-08-06, and it put a ~200 MB file inside the OneDrive-synced
        checkout. `common` derives the filename from the feed URL and honours KUNUNU_CACHE.
        """
        rel = self.raw.get("kununu_feed", {}).get("cache_path")
        return REPO_ROOT / rel if rel else None

    @property
    def kununu_feed_max_age_hours(self) -> int:
        return int(self.raw.get("kununu_feed", {}).get("max_age_hours", 24))

    # --- Task routing ---
    @property
    def task_types(self) -> dict[str, str]:
        """Subject -> parser key."""
        return self.raw.get("task_types", {})

    @property
    def enabled_task_types(self) -> list[str]:
        return self.raw.get("enabled_task_types", [])

    @property
    def in_scope_subjects(self) -> list[str]:
        """Subjects whose parser key is currently enabled."""
        enabled = set(self.enabled_task_types)
        return [subj for subj, key in self.task_types.items() if key in enabled]

    @property
    def excluded_email_domains(self) -> set[str]:
        """Own-company email domain roots (second-level labels). A lead whose Lead.Email
        or parsed contact email matches one is internal and excluded from Stage 1
        extraction and shadow scoring. Empty set when the key is unset."""
        raw = self.raw.get("excluded_email_domains", []) or []
        return {r for r in (domain_sld(str(e)) for e in raw) if r}

    @property
    def fresh_task_statuses(self) -> list[str]:
        """Task statuses that count as fresh/unhandled and get processed. Explicit
        allowlist (the org picklist flags 'Abgeschlossen'=Completed as not-closed, so
        IsClosed is unusable). Empty => no status filter."""
        return self.raw.get("fresh_task_statuses", [])

    def parser_key_for(self, subject: str) -> str | None:
        return self.task_types.get(subject)

    # --- Matching thresholds ---
    @property
    def lf_min_match_score(self) -> float:
        return float(self.raw["matching"].get("lf_min_match_score", 0.70))

    @property
    def lf_name_agree_min(self) -> int:
        return int(self.raw["matching"].get("lf_name_agree_min", 85))

    @property
    def lf_name_agree_rescue(self) -> int:
        return int(self.raw["matching"].get("lf_name_agree_rescue", 90))

    @property
    def dedupe_name_agree_min(self) -> int:
        return int(self.raw["matching"].get("dedupe_name_agree_min", 85))

    # --- Pipeline toggles ---
    @property
    def review_gate(self) -> bool:
        return bool(self.raw["pipeline"].get("review_gate", True))

    @property
    def dry_run(self) -> bool:
        return bool(self.raw["pipeline"].get("dry_run", False))

    @property
    def batch_limit(self) -> int:
        return int(self.raw["pipeline"].get("batch_limit", 0))

    @property
    def task_lookback_days(self) -> int:
        return int(self.raw["pipeline"].get("task_lookback_days", 35))

    @property
    def record_predictions(self) -> bool:
        """False disables the dry-run shadow-prediction log. One-off backlog runs set
        this: their leads are converted by the pipeline itself, so shadow-compare would
        later score our own conversions as if a human had made them."""
        return bool(self.raw["pipeline"].get("record_predictions", True))

    # --- Live scope (phased go-live gate; see live_scope.py) ---
    @property
    def live_max_kununu_score(self) -> float | None:
        """Strict upper bound on the kununu profile score for live processing
        (score < max qualifies; a missing score never does). An explicit `null` in the
        config returns None = no score axis at all (see live_scope.py); an ABSENT key
        keeps the 3.0 default, so the scheduled config cannot lose the cap by omission."""
        scope = self.raw["pipeline"].get("live_scope") or {}
        if "max_kununu_score" in scope and scope["max_kununu_score"] is None:
            return None
        return float(scope.get("max_kununu_score", 3.0))

    @property
    def live_decisions(self) -> list[str]:
        """Match decisions eligible for live CONVERSION (a converted lead is always
        written back first). Widen the rollout here (e.g. add 'link') — `skip`/`review`
        must never be listed. `[]` = enrich only: nothing is staged for Stage 2."""
        return list((self.raw["pipeline"].get("live_scope") or {})
                    .get("decisions", ["create_new"]))

    @property
    def live_writeback_decisions(self) -> list[str]:
        """Match decisions whose Lead gets the live write-back. An ABSENT key falls
        back to `decisions` (write-back scope == conversion scope, the go-live
        behaviour). `review` may be listed here — enriching a lead converts nothing —
        but `skip` never: a skip has no accepted LeadFeeder match."""
        scope = self.raw["pipeline"].get("live_scope") or {}
        if "writeback_decisions" not in scope:
            return self.live_decisions
        return list(scope["writeback_decisions"] or [])

    @property
    def live_task_types(self) -> list[str] | None:
        """Parser keys (claimer | ebp_trial | ...) a non-dry run may write back. None
        (key absent) = every enabled type. An enabled type not listed here stays dry:
        extracted, matched and shadow-predicted, never written (Wasil, 2026-10-19:
        claimer live; 2026-10-20: ebp_trial live too)."""
        scope = self.raw["pipeline"].get("live_scope") or {}
        if scope.get("task_types") is None:
            return None
        return [str(t) for t in scope["task_types"]]

    @property
    def live_convert_buckets(self) -> list[str] | None:
        """LF-outcome campaign buckets (campaigns.py BUCKETS) a lead must have been
        tagged with THIS run before it is staged for conversion (Wasil, 2026-10-31:
        `matched_complete` = "EnrichLeadfedup" only). None (key absent) = no campaign
        axis, the 2026-07-18 behaviour. An unknown bucket name raises: a typo here
        would silently convert nothing."""
        scope = self.raw["pipeline"].get("live_scope") or {}
        buckets = scope.get("convert_campaigns")
        if buckets is None:
            return None
        from .campaigns import BUCKETS
        out = [str(b) for b in buckets]
        unknown = sorted(set(out) - set(BUCKETS))
        if unknown:
            raise ValueError(f"live_scope.convert_campaigns: unknown bucket(s) {unknown}; "
                             f"valid: {list(BUCKETS)}")
        return out

    @property
    def repair_national_phone(self) -> bool:
        """True lets the write-back OVERWRITE a Lead.Phone of the shape `+0<digits>` — a
        national number with a '+' glued onto its trunk zero, which the org's validation
        rule accepts (it only checks '+' and 6-15 digits) — with the international form
        for the lead's country (DE +49 / AT +43 / CH +41). Found on 46 of 109 ZPE 2026
        badge-scan leads (Wasil, 2026-09-18). Default False: Phone stays strictly
        fill-only-blank in the scheduled pipeline; the one-off ZPE config sets it."""
        return bool(self.raw["pipeline"].get("repair_national_phone", False))

    @property
    def writeback_allow_duplicates(self) -> bool:
        """True sends `Sforce-Duplicate-Rule-Header: allowSave=true` on the Lead
        write-back, so an Alert-mode duplicate rule no longer fails the row with
        DUPLICATES_DETECTED (Wasil, 2026-10-21: 31 of 56 backlog write-backs died there;
        a Block-mode rule still rejects). Default False: the org's duplicate rules apply
        as before and such a lead goes to the `unmatched` campaign (campaigns.py)."""
        return bool(self.raw["pipeline"].get("writeback_allow_duplicates", False))

    @property
    def lead_statuses(self) -> list[str]:
        """Lead `Status` allowlist for extraction (e.g. ['New']). Applied in the reader
        AFTER the batched lead query — `Status` is already selected, so the filter costs
        no extra API call. A lead in another status is skipped (reason names the
        status) and its task is accounted for like any other skip. Empty => no
        filter (the scheduled pipeline). One-off review runs set it (Wasil, 2026-09-23:
        'where the lead has Status = New')."""
        raw = self.raw["pipeline"].get("lead_statuses") or []
        return [str(s) for s in raw]

    @property
    def include_enriched_leads(self) -> bool:
        """True bypasses the already-enriched skip (kununu marker field set). One-off
        backlog runs set this to re-process leads the legacy manual pipeline enriched
        but never converted."""
        return bool(self.raw["pipeline"].get("include_enriched_leads", False))
