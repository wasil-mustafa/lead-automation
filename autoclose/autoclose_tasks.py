#!/usr/bin/env python
"""Propose inbound-sales Tasks that can be auto-closed on a low kununu score.

Read-only against Salesforce. Produces a CSV of Task Ids; closing them is a
separate, human step (house style: propose-only before any write path).

THE RULES (reviewed with Wasil 2026-08-18 - change them here, not in a caller):

  1. Task.Subject is one of AUTOCLOSE_SUBJECTS.
  2. Task is still open: Status IN ('In Progress', 'Not Started'). Those plus
     'Closed' are the only active values on Task in org `headless`; there is no
     German 'Abgeschlossen' on this object (there is on others - do not copy a
     status list between sObjects).
  3. The Task hangs off a Contact (WhoId) or an Account (WhatId). A Task on a
     Lead has no Account and is therefore out of scope.
  4. The owner of that Account is one of POOL_OWNERS. Note 'indirect Account
     Pool' - lowercase i, that is the org's actual string.
  5. Whitepaper Download ONLY: also require KN_ScoringNumber__c != 100. This
     gate is subject-specific and does not apply to the other subjects. It is
     not cosmetic: 292 of 22,130 whitepaper Tasks carry 100 (measured
     2026-08-18), even though none did in the trailing 7 days.
  6. The account's kununu score is ABOVE 0 and below 3.

WHY RULE 6 EXCLUDES 0.0, AND WHY IT DOES NOT READ THE FEED.
Account.KN_KununuScore__c stores 0.0 for "no score known", so a naive `< 3` test
sweeps in every account whose score is merely unknown - which on 2026-08-18 was
4,259 pool accounts, against 1 genuinely low one. `0 < score` is the whole
difference between "kununu rates this company badly" and "we have no idea".

This script used to read the score from the kununu feed instead, on the theory
that the Salesforce field lags it. Measured over the FULL pool population, not a
sample, that theory is wrong: of 171,469 pool-owned accounts with a profile uuid
and a score above 0, the Salesforce field equals the feed score EXACTLY - zero
disagreements. Of the 4,259 sitting at 0.0, not one has a real feed score (3,816
uuids are absent from the feed, 443 are present with an empty cell). The field is
a faithful mirror, and the ~200 MB feed download changed no output.

If you ever reintroduce the feed, RE-MEASURE first. The earlier claim of drift
came from a buggy ad-hoc scan that truncated every profile slug to its country
segment ('/de/anything' -> 'de'), so unrelated profiles matched each other. It
was never real disagreement.

API COST: 2 Salesforce calls. One query for the open Tasks, then ONE /composite
carrying the Contact and Account reads (they are independent of each other, both
dependent on the Task read). Do not add a third: resolving Contact -> Account in
two steps is the obvious-looking change that doubles the cost, and the Contact
query already pulls the Account fields inline.

Exit codes: 0 ok - 1 Salesforce query failed - 2 bad args.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from leadauto.sf.client import SfClient, SfError  # noqa: E402

AUTOCLOSE_SUBJECTS = (
    "Inbound Sales Task: Webinarteilnahme",
    "Inbound Sales Task: New Claimed Profile",
    "Inbound Sales Task: Whitepaper Download",
)
WHITEPAPER_SUBJECT = "Inbound Sales Task: Whitepaper Download"
WHITEPAPER_EXCLUDED_SCORING = 100.0

OPEN_STATUSES = ("In Progress", "Not Started")

POOL_OWNERS = (
    "Clean Pool",
    "Hunter Pool",
    "AT/CH Hunter Pool",
    "Rohdaten Pool",
    "indirect Account Pool",
)

MAX_KUNUNU_SCORE = 3.0

ACCOUNT_FIELDS = (
    "Name",
    "Owner.Name",
    "KN_KununuProfileUuid__c",
    "KN_KununuLinkProfile__c",
    "KN_KununuScore__c",
)

OUT_COLUMNS = (
    "TaskId", "Subject", "Status", "CreatedDate", "TaskOwner",
    "WhoId", "ContactName", "WhatId",
    "AccountId", "AccountName", "AccountOwner",
    "KN_ScoringNumber__c", "kununu_score",
    "KN_KununuProfileUuid__c", "KN_KununuLinkProfile__c",
)


def quoted(values) -> str:
    """SOQL literal list. Single quotes only - SfClient rejects double quotes."""
    return ", ".join("'" + str(v).replace("'", "\\'") + "'" for v in values)


def field(record: dict, path: str):
    """One field from a query record, whichever transport produced it.

    A REST read nests relationships ({'Account': {'Owner': {'Name': ...}}}); the
    Bulk API returns CSV, so the same field arrives as the FLAT key
    'Account.Owner.Name' and every null as ''. `SfClient.query` picks the
    transport by SOQL length, so a query that grows past the threshold silently
    changes record shape - read every relationship field through here.
    """
    if path in record:
        value = record[path]
        return None if value == "" else value
    cursor = record
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return None if cursor == "" else cursor


def as_float(value):
    """Bulk results are stringly typed; '' is null, not 0.0."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_tasks(client: SfClient) -> list:
    """Rule 1 + 2: open Tasks of the in-scope subjects."""
    soql = (
        "SELECT Id, Subject, Status, CreatedDate, WhoId, WhatId, Owner.Name, "
        "KN_ScoringNumber__c FROM Task "
        "WHERE Subject IN (" + quoted(AUTOCLOSE_SUBJECTS) + ") "
        "AND Status IN (" + quoted(OPEN_STATUSES) + ")"
    )
    return client.query(soql)


def passes_scoring_gate(task: dict) -> bool:
    """Rule 5. Applies to Whitepaper Download and nothing else."""
    if field(task, "Subject") != WHITEPAPER_SUBJECT:
        return True
    return as_float(field(task, "KN_ScoringNumber__c")) != WHITEPAPER_EXCLUDED_SCORING


def fetch_accounts(client: SfClient, contact_ids, account_ids):
    """Rules 3 + 4, in ONE /composite call. Returns (contacts, accounts).

    Both reads filter on the pool owner server-side, so an out-of-scope account
    never crosses the wire. The Contact read pulls the Account columns inline -
    that is what keeps this at one call instead of a Contact read followed by an
    Account read.
    """
    queries = {}
    if contact_ids:
        cols = ", ".join("Account." + f for f in ACCOUNT_FIELDS)
        queries["contacts"] = (
            "SELECT Id, Name, AccountId, " + cols + " FROM Contact "
            "WHERE Id IN (" + quoted(sorted(contact_ids)) + ") "
            "AND Account.Owner.Name IN (" + quoted(POOL_OWNERS) + ")"
        )
    if account_ids:
        queries["accounts"] = (
            "SELECT Id, " + ", ".join(ACCOUNT_FIELDS) + " FROM Account "
            "WHERE Id IN (" + quoted(sorted(account_ids)) + ") "
            "AND Owner.Name IN (" + quoted(POOL_OWNERS) + ")"
        )
    if not queries:
        return {}, {}
    results = client.query_many(queries)
    contacts = {field(r, "Id"): r for r in results.get("contacts", [])}
    accounts = {field(r, "Id"): r for r in results.get("accounts", [])}
    return contacts, accounts


def build_rows(tasks, contacts, accounts, counters) -> list:
    """Rule 6, and the join that gets us there. `counters` collects the funnel."""
    rows = []
    for task in tasks:
        who, what = field(task, "WhoId"), field(task, "WhatId")
        contact = contacts.get(who) if who else None
        if contact is not None:
            record, prefix = contact, "Account."
            account_id = field(contact, "AccountId")
            contact_name = field(contact, "Name")
        else:
            account = accounts.get(what) if what else None
            if account is None:
                counters["not on a pool account"] += 1
                continue
            record, prefix = account, ""
            account_id = field(account, "Id")
            contact_name = None

        def account_field(name, _r=record, _p=prefix):
            return field(_r, _p + name)

        score = as_float(account_field("KN_KununuScore__c"))
        if score is None or score == 0:
            counters["no kununu score"] += 1
            continue                      # unknown - never close on a guess
        if score >= MAX_KUNUNU_SCORE:
            counters["score >= 3"] += 1
            continue
        rows.append({
            "TaskId": field(task, "Id"),
            "Subject": field(task, "Subject"),
            "Status": field(task, "Status"),
            "CreatedDate": field(task, "CreatedDate"),
            "TaskOwner": field(task, "Owner.Name"),
            "WhoId": who or "",
            "ContactName": contact_name or "",
            "WhatId": what or "",
            "AccountId": account_id,
            "AccountName": account_field("Name"),
            "AccountOwner": account_field("Owner.Name"),
            "KN_ScoringNumber__c": as_float(field(task, "KN_ScoringNumber__c")),
            "kununu_score": score,
            "KN_KununuProfileUuid__c": account_field("KN_KununuProfileUuid__c") or "",
            "KN_KununuLinkProfile__c": account_field("KN_KununuLinkProfile__c") or "",
        })
    return rows


def write_csv(rows, out_path: Path) -> None:
    """utf-8-sig so Excel shows the umlauts; via a .part file so a half-written
    CSV never appears at the destination (which is usually OneDrive-synced)."""
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["AccountOwner"] or "",
                                                     r["AccountName"] or "")))
    os.replace(tmp, out_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", default="headless", help="sf target org alias")
    parser.add_argument("--out", default="autoclose_tasks.csv", help="output CSV")
    args = parser.parse_args(argv)

    client = SfClient(args.org)
    try:
        tasks = fetch_tasks(client)
    except SfError as err:
        print("Salesforce query failed: " + str(err), file=sys.stderr)
        return 1
    print("open tasks in scope (rules 1-2): " + str(len(tasks)))

    tasks = [t for t in tasks if passes_scoring_gate(t)]
    print("after the whitepaper scoring gate (rule 5): " + str(len(tasks)))

    contact_ids = {field(t, "WhoId") for t in tasks}
    contact_ids = {i for i in contact_ids if i and i.startswith("003")}
    account_ids = {field(t, "WhatId") for t in tasks}
    account_ids = {i for i in account_ids if i and i.startswith("001")}
    try:
        contacts, accounts = fetch_accounts(client, contact_ids, account_ids)
    except SfError as err:
        print("Salesforce query failed: " + str(err), file=sys.stderr)
        return 1
    print("pool-owned accounts reached (rules 3-4): "
          + str(len(contacts)) + " via contact, " + str(len(accounts)) + " direct")

    counters = {"not on a pool account": 0, "no kununu score": 0, "score >= 3": 0}
    rows = build_rows(tasks, contacts, accounts, counters)
    for label, count in counters.items():
        print("  dropped, " + label + ": " + str(count))

    out_path = Path(args.out).resolve()
    write_csv(rows, out_path)
    print("auto-closable tasks (rule 6, 0 < score < " + str(MAX_KUNUNU_SCORE)
          + "): " + str(len(rows)))
    print("wrote " + str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
