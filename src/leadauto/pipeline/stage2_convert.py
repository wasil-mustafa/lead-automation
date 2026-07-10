"""Stage 2 — convert only the Leads a human approved in the QA artifact.

The reviewer commits `review/approved_<date>.csv` (the Review sheet rows with `Approve?=yes`).
This stage reads those rows, turns each into a `ConversionTarget` — linking the matched
Account when present and the matched Contact only when its match reason is an identifier
signal — and runs the match-aware, idempotent bulk conversion. `dry_run` performs no
conversion. A read-only preflight (convert/preflight.py) re-checks each approved lead's
current field state against the org's conversion rules and skips+reports failures. Pool-owned
linked Accounts are enriched and kununu profile-identity conflicts are routed to review before
any Apex runs (convert/pool_enrich.py).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..convert import ConversionTarget, convert_leads, enrich_contact_email2
from ..convert.pool_enrich import resolve_pool_links
from ..convert.preflight import run_preflight, write_failures_csv
from ..logging import get_logger

_APPROVE_YES = {"yes", "y", "true", "x", "1"}

# Real Salesforce Id shape (15 or 18 alphanumeric). The approved CSV is hand-edited and its
# values are embedded in conversion Apex, so anything else fails the whole run loudly.
_SF_ID_RE = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")

# Contact reasons that identify the same person (vs. a name/heuristic match): linking on
# these is safe. Weaker reasons (contact_email_global, linkedin) create a fresh contact
# under the linked account instead.
_CONTACT_LINK_REASONS = {"kununu_user_uuid", "contact_email_in_account"}


@dataclass
class Stage2Result:
    targets: list[ConversionTarget] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    dry_run: bool = False
    preflight_failures: list[dict] = field(default_factory=list)


def read_approved(path: str | Path) -> list[dict]:
    """Read the reviewer's approved CSV into a list of row dicts."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _is_approved(row: dict) -> bool:
    return (row.get("Approve?") or "").strip().lower() in _APPROVE_YES


def targets_from_approved(rows: list[dict], settings) -> list[ConversionTarget]:
    """Build conversion targets from approved rows.

    Account is linked when `account_match_id` is present (the reviewer validated it).
    Contact is linked only when its match reason is an identifier signal — kununu user
    UUID or an email inside the linked account; weaker matches create a fresh contact.
    Blanking `account_match_id`/`contact_match_id` forces create-new.

    Ids are validated hard (15/18 alphanumeric): a malformed value raises ValueError for
    the whole run rather than reaching the generated Apex. A contact link without an
    account link is dropped with a warning — `Database.LeadConvert.setContactId` requires
    `setAccountId` alongside it, so keeping it would guarantee a conversion error.
    """
    log = get_logger("pipeline.stage2")
    targets: list[ConversionTarget] = []
    bad_ids: list[str] = []
    for line, row in enumerate(rows, start=2):  # data starts on CSV line 2 (after header)
        if not _is_approved(row):
            continue
        if (row.get("decision") or "").strip() == "skip":
            # Terminal skip (no LeadFeeder match): never converts, even if a reviewer
            # ticked Approve? — there is nothing to link or create.
            log.warning("line %d (%s): approved row has decision=skip; not converting",
                        line, (row.get("lead_id") or "").strip())
            continue
        lead_id = (row.get("lead_id") or "").strip()
        if not lead_id:
            continue
        account_id = (row.get("account_match_id") or "").strip() or None
        contact_id = (row.get("contact_match_id") or "").strip() or None
        for col, value in (("lead_id", lead_id), ("account_match_id", account_id),
                           ("contact_match_id", contact_id)):
            if value is not None and not _SF_ID_RE.fullmatch(value):
                bad_ids.append(f"line {line}: {col}={value!r}")
        if contact_id and (row.get("contact_reason") or "").strip() not in _CONTACT_LINK_REASONS:
            contact_id = None
        if contact_id and not account_id:
            log.warning("line %d (%s): dropping contact link %s — LeadConvert requires an "
                        "account link alongside a contact link; converting create-new instead",
                        line, lead_id, contact_id)
            contact_id = None
        targets.append(ConversionTarget(
            lead_id, account_id=account_id, contact_id=contact_id,
            lf_company_id=(row.get("lf_company_id") or "").strip() or None,
            kununu_profile_uuid=(row.get("kununu_profile_uuid") or "").strip() or None,
            kununu_url=(row.get("kununu_url") or "").strip() or None,
            lf_contact_email=(row.get("lf_contact_email") or "").strip() or None,
        ))
    if bad_ids:
        raise ValueError(
            "Malformed Salesforce Id(s) in the approved CSV — fix the file and re-run: "
            + "; ".join(bad_ids)
        )
    return targets


def run_stage2(
    sf_client, settings, approved_csv: str | Path, *,
    dry_run: bool | None = None, batch_size: int = 10,
    preflight: bool = True, failures_csv: str | Path = "preflight_failures.csv",
    logger=None,
) -> Stage2Result:
    log = logger or get_logger("pipeline.stage2")
    dry_run = settings.dry_run if dry_run is None else dry_run
    rows = read_approved(approved_csv)
    targets = targets_from_approved(rows, settings)
    pf_failures: list[dict] = []
    if preflight:
        # Runs even with zero targets: run_preflight returns ([], []) without querying,
        # and the header-only CSV proves in the CI artifact that preflight ran.
        targets, pf_failures = run_preflight(sf_client, targets, logger=log)
        path = write_failures_csv(pf_failures, failures_csv)
        if pf_failures:
            log.warning("Preflight skipped %d lead(s); details -> %s",
                        len({f['lead_id'] for f in pf_failures}), path)
    # Pool-account enrichment + kununu conflict gate (link targets only). Writes only on
    # live runs; kununu conflicts are dropped from conversion and reported like preflight.
    link_targets = [t for t in targets if t.account_id]
    other_targets = [t for t in targets if not t.account_id]
    kept_links, review_failures = resolve_pool_links(
        sf_client, link_targets, settings, dry_run=dry_run, logger=log)
    targets = kept_links + other_targets
    if review_failures:
        pf_failures.extend(review_failures)
        write_failures_csv(pf_failures, failures_csv)
        log.warning("Routed %d lead(s) to review (kununu conflict)", len(review_failures))
    log.info("Stage 2: %d approved row(s) -> %d conversion target(s) (dry_run=%s)",
             len(rows), len(targets), dry_run)
    result = convert_leads(sf_client, targets, settings,
                           dry_run=dry_run, batch_size=batch_size, logger=log)
    # Post-convert: LF contact email -> KN_Email2__c on NEW Contacts when it differs from
    # the primary (task) email. No-op under dry_run (convert_leads returns no results).
    result["email2"] = enrich_contact_email2(
        sf_client, targets, result.get("results", []), logger=log)
    return Stage2Result(targets=targets, result=result, dry_run=dry_run,
                        preflight_failures=pf_failures)
