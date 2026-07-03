"""Configuration: merges non-secret config.yaml with secrets from env / GitHub Secrets.

Precedence: environment/secrets > config.yaml. Secrets are never stored in the repo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

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
    kununu_api_token: str | None = None
    kununu_api_user: str | None = None
    kununu_api_password: str | None = None

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
            kununu_api_token=os.environ.get("KUNUNU_API_TOKEN"),
            kununu_api_user=os.environ.get("KUNUNU_API_USER"),
            kununu_api_password=os.environ.get("KUNUNU_API_PASSWORD"),
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
    def campaign_id(self) -> str | None:
        return self.raw["salesforce"].get("campaign_id")

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

    # --- kununu profile feed ---
    @property
    def kununu_feed_enabled(self) -> bool:
        return bool(self.raw.get("kununu_feed", {}).get("enabled", False))

    @property
    def kununu_feed_cache_path(self) -> Path:
        rel = self.raw.get("kununu_feed", {}).get("cache_path", "kununu_profiles.jsonl")
        return REPO_ROOT / rel

    @property
    def kununu_feed_max_age_hours(self) -> int:
        return int(self.raw.get("kununu_feed", {}).get("max_age_hours", 24))

    @property
    def kununu_feed_url(self) -> str:
        return self.raw.get("kununu_feed", {}).get(
            "url", "https://api.data.kununu.com/v1/feeds/data-management/profiles.jsonl")

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
    def auto_link_threshold(self) -> float:
        return float(self.raw["matching"].get("auto_link_threshold", 0.90))

    @property
    def review_threshold(self) -> float:
        return float(self.raw["matching"].get("review_threshold", 0.60))

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
