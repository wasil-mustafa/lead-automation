"""Backtest of the domain-rescue match — does re-asking with the email domain recover
the matches the sanity gate threw out as `country_conflict`?

The question (2026-11-05, lead 00QMz00000rhynBMAQ "Baustein GmbH"): `company_name` +
`email` returned only LF's Austrian namesake, which the post-retrieve sanity gate
rightly rejected (AT != DE), while LF also held the German company whose website IS the
lead's email domain. `enrich_leads` now re-asks such a lead once with `company_name` +
`url=<email domain>` (`enrich.build_rescue_payload`) and accepts the candidate only when
LF's own website for it is that domain (`enrich.rescue_accepts`). 50 leads all-time
(17 since 2026-10-19) lost a real match to the gate; this replays them.

Population: the newest `no_lf_match` prediction per lead, in two arms (`arm` column):
  * `gate_rejected` — carries an `lf_company_id`: LF matched, the sanity gate rejected.
    (A prediction row does not record the rejection reason, so an acronym rejection is
    in the arm too; the control tells it apart by country.)
  * `unmatched` (since 2026-11-06, lead 00QMz00000rhwTdMAI "PAU-Söhne Gesellschaft mit
    beschränkter Haftung") — no `lf_company_id`: LF returned no candidate at all. The
    rescue is asked whenever the control still returns nothing today.
The payload is rebuilt by `wide_match_backtest.build_cases`, the same production path.

Per case, in order:
  * **control**   `/companies/match` with the production payload at the primary width.
                  The match SUMMARY carries the company's country and website today, so
                  the conflict is judged on it — no retrieve, no credit.
  * **treatment** only for a lead the control still rejects on country: the rescue
                  payload, judged by `rescue_accepts` on the summary website.

Outcomes:
  * `recovered`            — control conflicts, rescue found a company whose website
                             is the lead's domain AND whose summary country agrees
                             with the lead. What production would write back.
  * `recovered_foreign`    — the website agrees but the summary country still
                             conflicts. Production refuses it BEFORE the retrieve
                             (`lf_rescue = foreign:<cc>!=<cc>`, 0 credits); the sanity
                             gate would have rejected it anyway.
  * `rescue_url_mismatch`  — rescue returned a candidate whose website is not the
                             domain; refused, no credit.
  * `rescue_no_match`      — rescue returned nothing.
  * `no_corporate_domain`  — freemail / no email: the rescue cannot run.
  * `drifted_no_conflict`  — the control no longer conflicts today (LF's index moved);
                             the rescue was never needed, so it is not a recovery.
  * `drifted_unmatched`    — (gate_rejected arm) the control matches nothing today; the
                             rescue is then asked as production would, and its outcome
                             wins over this label when it is asked.
  * `control_no_country`   — the summary carries no country; cannot judge without a
                             retrieve, which this backtest refuses to pay for.
  * `unmatched_recovered` / `unmatched_recovered_foreign` / `unmatched_url_mismatch` /
    `unmatched_no_match`   — the same four outcomes for a lead the control matched
                             NOTHING for (either arm). `drifted_matched_today` is the
                             unmatched arm's control matching at the primary width.

**Cost: `/companies/match` calls only — free, 0 credits, and `meta.credits.charged` is
summed and reported to prove it.** Salesforce is read twice (Tasks, Leads), batched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .leadfeeder.client import MATCH_RESULTS_PRIMARY
from .leadfeeder.enrich import (build_rescue_payload, lead_corporate_domain,
                                lead_country, rescue_accepts)
from .logging import get_logger
from .predictions import load_predictions
from .qa_artifact import add_sheet
from .state import REPO_ROOT
from .textnorm import country_iso2, normalize_host
from .wide_match_backtest import build_cases

DEFAULT_XLSX_PATH = REPO_ROOT / "domain_rescue_backtest.xlsx"

CASES_COLUMNS = [
    "lead_id", "task_id", "predicted_at", "arm", "sent_company_name", "sent_email",
    "lead_country", "lead_domain",
    "control_company_id", "control_company_name", "control_match_score",
    "control_country", "control_website",
    "rescue_company_id", "rescue_company_name", "rescue_match_score",
    "rescue_name_agreement", "rescue_country", "rescue_website", "outcome",
]

RECOVERED = ("recovered", "recovered_foreign", "unmatched_recovered",
             "unmatched_recovered_foreign")
ARM_GATE_REJECTED = "gate_rejected"
ARM_UNMATCHED = "unmatched"


def select_predictions(predictions) -> list:
    """Every `no_lf_match` row — LF matched and the gate rejected (`lf_company_id` set)
    OR LF returned no candidate (since 2026-11-06). `build_cases` keeps the newest per
    lead; `arm_of` tells the two apart."""
    return [pr for pr in predictions if pr.rule == "no_lf_match"]


def arm_of(case) -> str:
    return ARM_GATE_REJECTED if case.lf_company_id else ARM_UNMATCHED


def classify(control: dict | None, lead_cc: str | None, lead_domain: str | None,
             rescue: dict | None, rescue_asked: bool, arm: str = ARM_GATE_REJECTED) -> str:
    """The outcome label; see the module docstring. `rescue_asked` is False when the
    control did not conflict (no rescue call was made)."""
    if not control:
        if not rescue_asked:
            return ("no_corporate_domain" if not lead_domain
                    else "drifted_unmatched" if arm == ARM_GATE_REJECTED
                    else "rescue_not_asked")
        if rescue_accepts(rescue, lead_domain):
            rescue_cc = country_iso2(rescue.get("matched_country_code"))
            return ("unmatched_recovered" if (not rescue_cc or rescue_cc == lead_cc)
                    else "unmatched_recovered_foreign")
        return "unmatched_url_mismatch" if rescue else "unmatched_no_match"
    if arm == ARM_UNMATCHED:
        return "drifted_matched_today"
    control_cc = country_iso2(control.get("matched_country_code"))
    if not control_cc or not lead_cc:
        return "control_no_country"
    if control_cc == lead_cc:
        return "drifted_no_conflict"
    if not lead_domain:
        return "no_corporate_domain"
    if not rescue_asked:
        return "rescue_not_asked"
    if rescue_accepts(rescue, lead_domain):
        rescue_cc = country_iso2(rescue.get("matched_country_code"))
        return "recovered" if (not rescue_cc or rescue_cc == lead_cc) else "recovered_foreign"
    if rescue:
        return "rescue_url_mismatch"
    return "rescue_no_match"


def run_backtest(sf_client, lf_client, settings, *, xlsx_path: Path | None = None,
                 predictions_path=None, profiles=None, use_kununu_feed: bool = True,
                 logger=None) -> dict:
    """Replay the gate-rejected leads: control match, then the rescue for the ones that
    still conflict. Read-only, 0 credits."""
    log = logger or get_logger("domain_rescue_backtest")
    xpath = Path(xlsx_path) if xlsx_path else DEFAULT_XLSX_PATH
    now = datetime.now(timezone.utc)

    cases, counts = build_cases(sf_client, settings,
                                select_predictions(load_predictions(predictions_path)),
                                profiles=profiles, use_kununu_feed=use_kununu_feed,
                                logger=log)
    if not cases:
        log.warning("Domain-rescue backtest: nothing to replay (%s)", counts)
        return {"counts": counts, "tally": {}, "rows": [], "xlsx": None, "credits": 0}

    control = lf_client.match_companies([c.payload for c in cases],
                                        max_results=MATCH_RESULTS_PRIMARY)
    lead_cc = [lead_country(c.extracted, c.feed_country_code) for c in cases]
    lead_dom = [lead_corporate_domain(c.extracted) for c in cases]

    # Rescue only where production would: a control match whose summary country
    # conflicts with the lead's, OR (2026-11-06) no control match at all — in both
    # cases only on a lead with a corporate domain.
    rescue_idx, rescue_payloads = [], []
    n_conflict = n_none = 0
    for i, (case, ctl) in enumerate(zip(cases, control)):
        ctl_cc = country_iso2((ctl or {}).get("matched_country_code"))
        conflict = bool(ctl and ctl_cc and lead_cc[i] and ctl_cc != lead_cc[i])
        if (conflict or not ctl) and lead_dom[i]:
            rp = build_rescue_payload(case.extracted, case.payload)
            if rp is not None:
                rescue_idx.append(i)
                rescue_payloads.append(rp)
                n_conflict += conflict
                n_none += not ctl
    rescue_by_idx: dict[int, dict | None] = {}
    if rescue_payloads:
        log.info("Domain-rescue backtest: re-asking %d of %d lead(s) with the email domain "
                 "(%d control matches conflict on country, %d match nothing)",
                 len(rescue_idx), len(cases), n_conflict, n_none)
        found = lf_client.match_companies(rescue_payloads, max_results=MATCH_RESULTS_PRIMARY)
        rescue_by_idx = dict(zip(rescue_idx, found))

    rows, tally = [], {}
    for i, case in enumerate(cases):
        ctl, res = control[i], rescue_by_idx.get(i)
        label = classify(ctl, lead_cc[i], lead_dom[i], res, i in rescue_by_idx, arm_of(case))
        tally[label] = tally.get(label, 0) + 1
        ctl, res = ctl or {}, res or {}
        rows.append({
            "lead_id": case.lead_id, "task_id": case.task_id,
            "predicted_at": case.predicted_at, "arm": arm_of(case),
            "sent_company_name": case.payload.get("company_name"),
            "sent_email": case.payload.get("email") or "",
            "lead_country": lead_cc[i] or "", "lead_domain": lead_dom[i] or "",
            "control_company_id": ctl.get("company_id") or "",
            "control_company_name": ctl.get("matched_name") or "",
            "control_match_score": ctl.get("match_score", ""),
            "control_country": ctl.get("matched_country_code") or "",
            "control_website": normalize_host(ctl.get("matched_url")) or "",
            "rescue_company_id": res.get("company_id") or "",
            "rescue_company_name": res.get("matched_name") or "",
            "rescue_match_score": res.get("match_score", ""),
            "rescue_name_agreement": res.get("name_agreement", ""),
            "rescue_country": res.get("matched_country_code") or "",
            "rescue_website": normalize_host(res.get("matched_url")) or "",
            "outcome": label,
        })
        if label in RECOVERED:
            log.info("%s %r (%s, %s) -> %r (lf %s, %s, %s, score %s)", label.upper(),
                     case.payload.get("company_name"), lead_cc[i], lead_dom[i],
                     res.get("matched_name"), res.get("company_id"),
                     res.get("matched_country_code"), res.get("matched_url"),
                     res.get("match_score"))

    credits = getattr(lf_client, "credits_charged", 0)
    _write_xlsx(rows, tally, counts, now, credits, xpath)
    log.info("Domain-rescue backtest done: %s (credits charged: %s) -> %s",
             tally, credits, xpath)
    return {"counts": counts, "tally": tally, "rows": rows, "xlsx": str(xpath),
            "credits": credits}


def _write_xlsx(rows, tally, counts, now, credits, xpath: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    add_sheet(wb, "Cases", CASES_COLUMNS, [[r[c] for c in CASES_COLUMNS] for r in rows])
    recovered = [r for r in rows if r["outcome"] in RECOVERED]
    add_sheet(wb, "Recovered", CASES_COLUMNS,
              [[r[c] for c in CASES_COLUMNS] for r in recovered])

    summary = [["generated_at", now.isoformat()],
               ["match_width", MATCH_RESULTS_PRIMARY],
               ["credits_charged", credits]]
    summary += [[f"count_{k}", v] for k, v in sorted(counts.items())]
    for label in sorted(tally):
        summary.append([f"outcome_{label}", tally[label]])
    conflicting = sum(v for k, v in tally.items()
                      if k in ("recovered", "recovered_foreign", "rescue_url_mismatch",
                               "rescue_no_match", "no_corporate_domain"))
    good = tally.get("recovered", 0)
    summary.append(["recovery_rate_of_still_conflicting",
                    f"{good}/{conflicting} = {100 * good / (conflicting or 1):.1f}%"])
    still_unmatched = sum(v for k, v in tally.items()
                          if k in ("unmatched_recovered", "unmatched_recovered_foreign",
                                   "unmatched_url_mismatch", "unmatched_no_match"))
    good_u = tally.get("unmatched_recovered", 0)
    summary.append(["recovery_rate_of_still_unmatched_with_domain",
                    f"{good_u}/{still_unmatched} = {100 * good_u / (still_unmatched or 1):.1f}%"])
    summary.append(["NOTE", "A `recovered` / `unmatched_recovered` row is what production "
                            "writes back: judge each one (is that company the lead's?) "
                            "before trusting the rate. `*_foreign` is refused by "
                            "production before the retrieve (0 credits)."])
    add_sheet(wb, "Summary", ["metric", "value"], summary)

    add_sheet(wb, "README", ["note"], [[line] for line in [
        "Domain-rescue match backtest. Read-only, 0 LeadFeeder credits.",
        "Population: the newest `no_lf_match` prediction per lead. arm=gate_rejected: "
        "it carries an lf_company_id (LF matched, the post-retrieve sanity gate "
        "rejected). arm=unmatched: no lf_company_id (LF returned no candidate; since "
        "2026-11-06).",
        "control = the production payload (company_name + email + local alt_names) at "
        "width 3; the conflict is judged on the match summary's country.",
        "rescue  = company_name + url=<lead email domain>, asked when the control still "
        "conflicts OR still matches nothing; accepted only when LF's website for the "
        "hit is that domain.",
        "recovered / unmatched_recovered = rescue accepted and its country agrees: "
        "production writes it back.",
        "*_foreign = website agrees, country still conflicts: production refuses it "
        "before the retrieve (0 credits).",
        "drifted_* = the control no longer conflicts / matches today at the primary "
        "width — LF's index moved, not the feature.",
    ]])

    xpath.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xpath)
