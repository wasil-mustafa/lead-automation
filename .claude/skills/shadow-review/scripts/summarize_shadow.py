#!/usr/bin/env python
"""Summarize the shadow backtest from the committed state files. Read-only, offline.

Reads ``state/shadow_results.jsonl`` (scored outcomes) and
``state/predictions.jsonl`` (what is still pending) and prints the numbers the
go-live decision actually turns on:

* per-rule decision accuracy, which is the evidence for widening
  ``pipeline.live_scope.decisions``;
* per-field enrichment agreement, which is the evidence for trusting the
  write-back spec;
* what is still pending and how old it is, so a thin sample is not mistaken for a
  verdict.

Touches **no** Salesforce and **no** LeadFeeder: every number here comes off disk.
Running the compare itself (which does cost ~7-9 API calls) is a separate,
deliberate step -- see the skill.

Usage:
    python .claude/skills/shadow-review/scripts/summarize_shadow.py [--rule NAME]
                                                                   [--misses N]
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
STATE = REPO_ROOT / "state"

def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def pct(part, whole):
    return "n/a" if not whole else "{:.0%}".format(part / whole)


def age_days(iso):
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).days


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rule", help="Only report this rule, and list its misses.")
    parser.add_argument("--misses", type=int, default=10,
                        help="How many miss rows to list (default 10).")
    args = parser.parse_args()

    results = read_jsonl(STATE / "shadow_results.jsonl")
    predictions = read_jsonl(STATE / "predictions.jsonl")

    if not results:
        print("No scored results yet (state/shadow_results.jsonl is empty or absent).")
        return 0

    if args.rule:
        results = [r for r in results if r.get("rule") == args.rule]
        if not results:
            print("No scored results for rule {!r}.".format(args.rule))
            return 0

    # --- Decisions, per rule.
    by_rule = defaultdict(Counter)
    for row in results:
        by_rule[row.get("rule") or "?"][row.get("result") or "?"] += 1

    print("DECISIONS -- per rule (hit = same account the human chose)")
    print("  {:<22} {:>5} {:>5} {:>9} {:>9}".format("rule", "hit", "miss", "observed", "accuracy"))
    total_hit = total_miss = 0
    for rule in sorted(by_rule):
        counts = by_rule[rule]
        hit, miss, observed = counts["hit"], counts["miss"], counts["observed"]
        total_hit += hit
        total_miss += miss
        print("  {:<22} {:>5} {:>5} {:>9} {:>9}".format(
            rule, hit, miss, observed, pct(hit, hit + miss)))
    print("  {:<22} {:>5} {:>5} {:>9} {:>9}".format(
        "ALL", total_hit, total_miss, "", pct(total_hit, total_hit + total_miss)))

    # create_new is scored differently in kind: there is no account to agree with,
    # so a "miss" there means the human found an account the engine did not.
    create_new = Counter()
    for row in results:
        if row.get("decision") == "create_new":
            create_new[row.get("result") or "?"] += 1
    if create_new:
        scored = create_new["hit"] + create_new["miss"]
        print("\n  create_new only (the live-scope population): hit={} miss={} "
              "observed={} accuracy={}".format(
                  create_new["hit"], create_new["miss"], create_new["observed"],
                  pct(create_new["hit"], scored)))
        print("  A miss here means the human linked to an account the engine did not find,")
        print("  i.e. the pipeline would have created a duplicate. Weigh these individually.")

    # --- Fields.
    field_agg = defaultdict(Counter)
    for row in results:
        for label, detail in (row.get("fields") or {}).items():
            field_agg[label][detail.get("status") or "?"] += 1
    if field_agg:
        print("\nFIELDS -- enrichment agreement with what the human entered")
        print("  {:<24} {:>6} {:>6} {:>11} {:>8} {:>9} {:>6}".format(
            "field", "agree", "differ", "human_empty", "lf_empty", "prefilled", "rate"))
        for label in sorted(field_agg):
            counts = field_agg[label]
            comparable = counts["agree"] + counts["differ"]
            print("  {:<24} {:>6} {:>6} {:>11} {:>8} {:>9} {:>6}".format(
                label, counts["agree"], counts["differ"], counts["human_empty"],
                counts["lf_empty"], counts["prefilled"], pct(counts["agree"], comparable)))

    # --- Pending / sample health.
    pending = [p for p in predictions if p.get("status") == "pending"]
    ages = [d for d in (age_days(p.get("predicted_at")) for p in pending) if d is not None]
    print("\nSAMPLE HEALTH")
    print("  scored ever={}  pending={}  oldest pending={} days".format(
        len(results), len(pending), max(ages) if ages else "n/a"))
    stale = [d for d in ages if d > 30]
    if stale:
        print("  {} pending prediction(s) older than 30 days -- those humans may simply "
              "never convert the lead, so they will expire rather than score.".format(len(stale)))

    # --- Misses, when a rule was named.
    if args.rule:
        misses = [r for r in results if r.get("result") == "miss"][: args.misses]
        if misses:
            print("\nMISSES for rule {} (first {})".format(args.rule, len(misses)))
            for row in misses:
                print("  lead={} predicted={} human_chose={} ({}) detail={}".format(
                    row.get("lead_id"), row.get("predicted_account_id"),
                    row.get("converted_account_id"), row.get("converted_account_name"),
                    row.get("detail")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
