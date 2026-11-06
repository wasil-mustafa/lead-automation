"""Backtest of the second-chance ("wide") company match — does widening recover leads?

The question, and why it needs its own backtest: `no_lf_match` is a TERMINAL skip and it
was **101 of 523 distinct shadow predictions (19.3%)**, the pipeline's largest single loss
of coverage. `enrich_leads` now re-asks those leads once at `max_results_per_company: 20`
(shipped 2026-08-25, see `2026-08-25-second-chance-match.md`). The live dry run that
followed had exactly ONE unmatched lead in scope, which recovered nothing — a sample of
one measures nothing, and waiting for new tasks to trickle in would take weeks.

So this replays the leads that ALREADY failed to match, from
`state/predictions.jsonl`, and asks the same payload twice:

  * **control** at `MATCH_RESULTS_PRIMARY` (3) — what the pipeline saw then;
  * **treatment** at `--max-results` (20 by default) — what it sees now.

Reading the result, which is the whole point of running the control:

  * `recovered`  — control finds nothing, treatment does. This is the feature working.
  * `drifted`    — control ALSO finds a match now. LF's index changed since the
                   prediction was recorded; the width had nothing to do with it, and
                   counting these as recoveries would overstate the feature.
  * `still_none` — neither width finds anything. Expected to dominate: the unmatched
                   tail is micro-businesses genuinely absent from LeadFeeder.

**Cost: `/companies/match` calls only — free, 0 credits, and `meta.credits.charged` is
summed and reported to prove it.** No company or contact retrieve, no contact search, no
`find-contact-data` job, and nothing is written to Salesforce. Salesforce is read twice
(Tasks by Id, Leads by Id); both auto-route to Bulk over ~2000 chars.

The payload comes from `enrich.build_match_payload`, the same function the pipeline uses.
Rebuilding it locally from `Lead.Company` would ask LF a DIFFERENT question than
production asks — the pipeline sends the task-parsed name, which is often not
`Lead.Company` — and the answer would look like a fact about LeadFeeder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .extract.parsers import extract_lead
from .kununu_feed import load_profiles_for_leads
from .leadfeeder.client import MATCH_RESULTS_PRIMARY, MATCH_RESULTS_WIDE
from .leadfeeder.enrich import build_match_payload
from .logging import get_logger
from .predictions import load_predictions
from .qa_artifact import add_sheet
from .sf import soql
from .sf.soql import in_list
from .state import REPO_ROOT

DEFAULT_XLSX_PATH = REPO_ROOT / "wide_match_backtest.xlsx"

CASES_COLUMNS = [
    "lead_id", "task_id", "predicted_at", "rule", "sent_company_name", "alt_names",
    "sent_email", "control_company_id", "control_company_name", "control_match_score",
    "control_name_agreement", "control_agreed_via",
    "wide_company_id", "wide_company_name", "wide_match_score",
    "wide_name_agreement", "wide_agreed_via", "outcome",
]

# Rules whose leads never reached an LF match decision at all. Only `no_lf_match` is
# replayable: every other rule means LF DID match and the engine moved on.
REPLAYABLE_RULES = ("no_lf_match",)


@dataclass
class WideCase:
    lead_id: str
    task_id: str
    predicted_at: str
    rule: str
    payload: dict = field(default_factory=dict)
    # The ExtractedLead behind the payload and the feed's country for it: the
    # domain-rescue backtest (2026-11-05) judges the country conflict exactly as the
    # sanity gate does (`enrich.lead_country`), so it needs both.
    extracted: object = None
    feed_country_code: str | None = None
    # The prediction's LF company (set = LF matched and the sanity gate rejected; None =
    # LF returned no candidate). The domain-rescue backtest scores the two arms apart.
    lf_company_id: str | None = None


def select_cases(predictions, rules=REPLAYABLE_RULES) -> list:
    """The newest prediction per lead whose rule is replayable.

    Predictions accumulate one row per lead per dry run, so the same lead appears many
    times; replaying it once per row would bill the same question repeatedly and inflate
    every count in the report.
    """
    newest: dict[str, object] = {}
    for pr in predictions:
        if pr.rule not in rules:
            continue
        cur = newest.get(pr.lead_id)
        if cur is None or str(pr.predicted_at) > str(cur.predicted_at):
            newest[pr.lead_id] = pr
    return sorted(newest.values(), key=lambda pr: str(pr.predicted_at))


def build_cases(sf_client, settings, predictions, *, profiles=None,
                use_kununu_feed: bool = True, logger=None) -> tuple[list[WideCase], dict]:
    """Rebuild the production match payload for every replayable lead.

    Two Salesforce reads, both batched: the Tasks named by the predictions and the Leads
    behind them. A lead whose Task or Lead record is gone, or whose task type is no longer
    enabled, is DROPPED and counted — never replayed with a guessed payload.

    `profiles` is injectable and `use_kununu_feed=False` skips the feed entirely, the same
    contract `enrich_leads` offers: the feed is a ~200 MB download, so a test that does not
    pass one would reach the network.
    """
    log = logger or get_logger("wide_backtest")
    counts: dict[str, int] = {}
    selected = select_cases(predictions)
    counts["replayable_leads"] = len(selected)
    if not selected:
        return [], counts

    task_ids = sorted({pr.task_id for pr in selected if pr.task_id})
    lead_ids = sorted({pr.lead_id for pr in selected if pr.lead_id})
    tasks = sf_client.query(
        f"SELECT {soql.TASK_FIELDS} FROM Task WHERE Id IN ({in_list(task_ids)})"
    ) if task_ids else []
    leads = sf_client.query(
        f"SELECT {soql.LEAD_FIELDS} FROM Lead WHERE Id IN ({in_list(lead_ids)})"
    ) if lead_ids else []
    task_by_id = {t["Id"]: t for t in tasks}
    lead_by_id = {l["Id"]: l for l in leads}
    counts["tasks_found"] = len(task_by_id)
    counts["leads_found"] = len(lead_by_id)

    extracted_by_lead: dict[str, object] = {}
    pending: list = []
    for pr in selected:
        task, lead = task_by_id.get(pr.task_id), lead_by_id.get(pr.lead_id)
        if task is None or lead is None:
            counts["dropped_record_gone"] = counts.get("dropped_record_gone", 0) + 1
            continue
        e = extract_lead(task, lead, settings)
        if e is None:
            counts["dropped_task_type"] = counts.get("dropped_task_type", 0) + 1
            continue
        extracted_by_lead[pr.lead_id] = e
        pending.append(pr)

    # One feed load for the whole batch, exactly as the pipeline does it. Alt names are
    # a real gate input, so dropping the feed measures a WEAKER payload than production
    # sends — the run logs it rather than letting the number pass as comparable.
    if profiles is None and use_kununu_feed and settings.kununu_feed_enabled:
        profiles = load_profiles_for_leads(list(extracted_by_lead.values()), settings,
                                           logger=log) if extracted_by_lead else {}
    if profiles is None:
        log.warning("kununu feed skipped: payloads carry NO alt_names, so this run is not "
                    "comparable to a production match")
    profiles = profiles or {}
    counts["profiles_resolved"] = len(profiles)

    cases: list[WideCase] = []
    for pr in pending:
        e = extracted_by_lead[pr.lead_id]
        payload, info = build_match_payload(e, profiles)
        if payload is None:
            counts["dropped_no_company_name"] = counts.get("dropped_no_company_name", 0) + 1
            continue
        cases.append(WideCase(lead_id=pr.lead_id, task_id=pr.task_id,
                              predicted_at=str(pr.predicted_at), rule=pr.rule,
                              payload=payload, extracted=e,
                              feed_country_code=info.country_code if info else None,
                              lf_company_id=pr.lf_company_id or None))
    counts["replayed"] = len(cases)
    log.info("Wide-match backtest: replaying %d of %d replayable lead(s) (%s)",
             len(cases), len(selected),
             ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return cases, counts


def classify(control: dict | None, wide: dict | None) -> str:
    """The outcome label. `drifted` exists so LF index movement is never read as a
    recovery — without the control arm every drifted lead would look like a win."""
    if control:
        return "drifted"
    return "recovered" if wide else "still_none"


def run_backtest(sf_client, lf_client, settings, *, max_results: int = MATCH_RESULTS_WIDE,
                 xlsx_path: Path | None = None, predictions_path=None, profiles=None,
                 use_kununu_feed: bool = True, logger=None) -> dict:
    """Replay the unmatched leads at two candidate widths. Read-only, 0 credits."""
    log = logger or get_logger("wide_backtest")
    xpath = Path(xlsx_path) if xlsx_path else DEFAULT_XLSX_PATH
    now = datetime.now(timezone.utc)

    cases, counts = build_cases(sf_client, settings,
                                load_predictions(predictions_path), profiles=profiles,
                                use_kununu_feed=use_kununu_feed, logger=log)
    if not cases:
        log.warning("Wide-match backtest: nothing to replay (%s)", counts)
        return {"counts": counts, "tally": {}, "rows": [], "xlsx": None, "credits": 0}

    payloads = [c.payload for c in cases]
    control = lf_client.match_companies(payloads, max_results=MATCH_RESULTS_PRIMARY)
    # Only the leads the control leaves unmatched are re-asked — the same asymmetry the
    # production pass relies on, so the treatment arm measures what production would do.
    retry_idx = [i for i, m in enumerate(control) if not m]
    wide_by_idx: dict[int, dict] = {}
    if retry_idx:
        wide = lf_client.match_companies([payloads[i] for i in retry_idx],
                                         max_results=max_results)
        wide_by_idx = {i: m for i, m in zip(retry_idx, wide) if m}

    rows, tally = [], {}
    for i, case in enumerate(cases):
        c, w = control[i], wide_by_idx.get(i)
        label = classify(c, w)
        tally[label] = tally.get(label, 0) + 1
        rows.append({
            "lead_id": case.lead_id, "task_id": case.task_id,
            "predicted_at": case.predicted_at, "rule": case.rule,
            "sent_company_name": case.payload.get("company_name"),
            "alt_names": "; ".join(case.payload.get("alt_names") or []),
            "sent_email": case.payload.get("email") or "",
            "control_company_id": (c or {}).get("company_id") or "",
            "control_company_name": (c or {}).get("matched_name") or "",
            "control_match_score": (c or {}).get("match_score") if c else "",
            "control_name_agreement": (c or {}).get("name_agreement") if c else "",
            "control_agreed_via": (c or {}).get("agreed_via") or "",
            "wide_company_id": (w or {}).get("company_id") or "",
            "wide_company_name": (w or {}).get("matched_name") or "",
            "wide_match_score": (w or {}).get("match_score") if w else "",
            "wide_name_agreement": (w or {}).get("name_agreement") if w else "",
            "wide_agreed_via": (w or {}).get("agreed_via") or "",
            "outcome": label,
        })
        if label == "recovered":
            log.info("RECOVERED %r -> %r (lf %s, score %s, agree %s via %s)",
                     case.payload.get("company_name"), w.get("matched_name"),
                     w.get("company_id"), w.get("match_score"),
                     w.get("name_agreement"), w.get("agreed_via"))

    credits = getattr(lf_client, "credits_charged", 0)
    _write_xlsx(rows, tally, counts, max_results, now, credits, xpath)
    log.info("Wide-match backtest done: %s (credits charged: %s) -> %s",
             tally, credits, xpath)
    return {"counts": counts, "tally": tally, "rows": rows, "xlsx": str(xpath),
            "credits": credits}


def _write_xlsx(rows, tally, counts, max_results, now, credits, xpath: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    add_sheet(wb, "Cases", CASES_COLUMNS, [[r[c] for c in CASES_COLUMNS] for r in rows])

    # Recoveries first: they are the only rows a human has to judge, and the judgement is
    # "is that actually the same company?" — a wide match is the candidate LF ranked
    # 4th-20th, so a plausible name is not enough.
    recovered = [r for r in rows if r["outcome"] == "recovered"]
    add_sheet(wb, "Recovered", CASES_COLUMNS,
               [[r[c] for c in CASES_COLUMNS] for r in recovered])

    # Drifted rows need judging too, and for a sharper reason: each one is a lead the
    # pipeline TERMINALLY SKIPPED that would match today. Read `control_name_agreement`
    # with suspicion — the gate scores a TRUNCATED name as perfect agreement, so
    # 'Topgolf Wien' -> 'Topgolf' and 'Filter Queen GbmH Hallwil' -> 'Filter Queen' both
    # come back at 100 while pointing at a parent or a different site.
    drifted = [r for r in rows if r["outcome"] == "drifted"]
    add_sheet(wb, "Drifted", CASES_COLUMNS,
               [[r[c] for c in CASES_COLUMNS] for r in drifted])

    summary = [["generated_at", now.isoformat()],
               ["control_max_results", MATCH_RESULTS_PRIMARY],
               ["treatment_max_results", max_results],
               ["credits_charged", credits]]
    summary += [[f"count_{k}", v] for k, v in sorted(counts.items())]
    for label in sorted(tally):
        summary.append([f"outcome_{label}", tally[label]])
    replayed = len(rows) or 1
    summary.append(["recovery_rate_of_replayed",
                    f"{len(recovered)}/{len(rows)} = {100 * len(recovered) / replayed:.1f}%"])
    summary.append(["NOTE", "A `recovered` row is the candidate LF ranked 4th-20th. "
                            "Judge each one before flipping live_scope.allow_wide_match."])
    summary.append(["NOTE_drifted", "A `drifted` row is a TERMINALLY SKIPPED lead that "
                                    "would match today at the normal width. Check "
                                    "control_company_name for a truncated/parent match "
                                    "before treating it as recoverable coverage."])
    add_sheet(wb, "Summary", ["metric", "value"], summary)

    add_sheet(wb, "README", ["note"], [[line] for line in [
        "Second-chance ('wide') company-match backtest. Read-only, 0 LeadFeeder credits.",
        "Population: the newest prediction per lead whose rule was `no_lf_match` — the "
        "terminal-skip bucket, 19.3% of all predictions.",
        f"control  = /companies/match at max_results_per_company={MATCH_RESULTS_PRIMARY} "
        "(what the pipeline saw when the prediction was recorded).",
        f"treatment = the same payload at max_results_per_company={max_results}, asked "
        "ONLY for leads the control left unmatched.",
        "recovered  = control found nothing, treatment did. The feature working.",
        "drifted    = the control matches TODAY too, so LF's index moved and the width "
        "is irrelevant. Counting these as recoveries would overstate the feature.",
        "still_none = neither width matched. Expected to dominate; the unmatched tail is "
        "micro-businesses genuinely absent from LeadFeeder.",
        "The payload comes from enrich.build_match_payload — the same function the "
        "pipeline uses, so this asks LF the question production asks.",
    ]])

    xpath.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xpath)
