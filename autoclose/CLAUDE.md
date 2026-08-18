# Task Auto-Close — Project Guide

Read this **together with** the repo root `CLAUDE.md`, not instead of it — the org alias,
the API-call budget, the Bulk-typing traps and the dry-run-by-default house style all come
from there and are not repeated here.

**One script, `autoclose_tasks.py`.** It answers one question: *which open inbound-sales
Tasks can be closed without a human looking at them, because kununu rates the company too
low to be worth pursuing?* It is **read-only** and emits a propose-only CSV. Nothing here
writes to Salesforce.

This is a standalone tool, not part of the Stage 1 / Stage 2 pipeline. It shares only
`SfClient` with it.

```powershell
# from the repo root
python autoclose\autoclose_tasks.py --out autoclose_tasks.csv
```

## The rules

Wasil reviewed and locked these on 2026-08-18. **Change them in the script's module
docstring and here in the same commit** — that docstring is the spec, this file is the
*why*.

| # | Rule |
|---|---|
| 1 | `Subject` ∈ Webinarteilnahme / New Claimed Profile / Whitepaper Download |
| 2 | Task still open: `Status IN ('In Progress','Not Started')` |
| 3 | Task hangs off a Contact (`WhoId`) or an Account (`WhatId`) |
| 4 | That Account's Owner ∈ Clean Pool / Hunter Pool / AT/CH Hunter Pool / Rohdaten Pool / indirect Account Pool |
| 5 | **Whitepaper Download only:** also `KN_ScoringNumber__c != 100` |
| 6 | `0 < Account.KN_KununuScore__c < 3` |

There is deliberately **no date window**. It was tried and removed: open Tasks barely
accumulate, so "all time" and "last 7 days" returned the same single row.

## Why each rule is shaped the way it is

- **Rule 2 — the status list is Task-specific.** `Not Started`, `In Progress` and `Closed`
  are the only active values on Task in org `headless`. Do **not** copy a status list in
  from another sObject: the root guide's warning that `Abgeschlossen` (Completed) carries
  `IsClosed = false` is about *other* objects, and importing it here would invent a status
  Task does not have. Equally, do not switch this to `IsClosed = false` for the same reason.

- **Rule 4 is the binding constraint, by a wide margin.** On 2026-08-18: 667 open in-scope
  Tasks, of which 578 hang off a Contact — but only **63** of those Contacts belong to a
  pool-owned Account. The other ~515 sit on rep-owned Accounts and are somebody's named
  work. Widening this is a business decision, not a data one.

- **Rule 5 is subject-specific and is not decoration.** `KN_ScoringNumber__c = 100` appears
  on **292 of 22,130** whitepaper Tasks (1.3%, measured 2026-08-18). It happened to appear
  on none in the trailing 7 days, which is exactly the kind of small-window reading that
  makes a real rule look dead. It excluded 6 Tasks on the first full run.

- **Rule 6's `0 <` half is the entire safety margin.** `KN_KununuScore__c` stores **0.0 for
  "no score known"**, not for "rated zero". A naive `< 3` test therefore sweeps in every
  Account whose score is simply unknown — on 2026-08-18 that was **4,259** pool Accounts
  against **1** genuinely low-scored one. Getting this wrong auto-closes Tasks on companies
  kununu rates well.

## Measured, and settled — do not re-litigate without re-measuring

**The kununu feed was tried as the score source and removed (2026-08-18).** The theory was
that `KN_KununuScore__c` lags the feed. Measured over the **full** pool population, not a
sample:

- 171,469 pool Accounts with a profile UUID and a score above 0 — the Salesforce field
  equals the feed score **exactly, zero disagreements**.
- 4,259 pool Accounts sitting at 0.0 — **not one** has a real feed score (3,816 UUIDs are
  absent from the feed entirely; 443 are present with an empty score cell).

So the field is a faithful mirror of the feed, `0.0` honestly means "no score", and a
~200 MB download per run changed no output. The feed read, the `common.kununu_feed` bridge
and the `--feed` / `--no-download` flags were deleted.

**The drift that motivated the feed read was an artefact.** An ad-hoc scan appeared to show
Salesforce disagreeing with the feed on three Accounts. Its URL matcher truncated every
profile slug to its country segment (`/de/anything` → `de`), so unrelated profiles matched
each other. Two of the three "disagreements" were name-matched Accounts that carry neither
a UUID nor a profile link in Salesforce — a gap in what Salesforce stores, not a stale
score. If you reintroduce the feed, measure first, and match on the full slug.

**Name matching is out of scope on purpose.** `HR SOLVIA GmbH` matches two distinct feed
profiles by name. A wrong profile here closes a Task on a good lead, so an Account that
cannot be identified is never auto-closed.

## What the rule is actually worth — and why a run returning 0 is normal

Low-score Tasks get closed by hand within days; that manual triage is precisely what this
would replace. So the standing backlog of *open* Tasks has already been picked over, and
the rule finds almost nothing in it. Measured on the 96 pool Tasks created in the 7 days to
2026-08-18:

| Task status | n | score < 3 |
|---|---|---|
| Closed | 66 | 16 (**24%**) |
| In Progress | 30 | 1 (**3%**) |

**The value is on newly created Tasks, before a human reaches them** — where the real rate
is ~24%, not 3%. A run that returns zero rows means the humans got there first, not that
the rule is broken. The first full run flagged exactly one Task (Handwerkskammer Karlsruhe,
score 2.0); a human closed it the same afternoon, which is the rule agreeing with a human
rather than failing.

**Open follow-up:** score the rule against the last 90 days of *closed* pool Tasks to size
the saving. That is the number that justifies scheduling this, and it has not been run yet.

## Implementation notes

- **API cost is 2 calls per run, and it is meant to stay there.** One query for the open
  Tasks, then **one** `/composite` carrying the Contact and Account reads — they are
  independent of each other, so `query_many` bundles them. The Contact query pulls the
  Account columns inline (`Account.Owner.Name`, `Account.KN_*`); resolving
  Contact → Account as a second step is the obvious-looking change that doubles the cost.
  Both reads filter the pool owner **server-side**, so out-of-scope Accounts never cross
  the wire.
- **Read every relationship field through `field()`.** `SfClient.query` picks REST or Bulk
  by SOQL length, and the two disagree on record shape: REST nests
  (`{'Account': {'Owner': {'Name': …}}}`), Bulk returns CSV so the same field arrives as the
  flat key `'Account.Owner.Name'` with nulls as `''`. The Contact query grows with the Task
  count, so it crosses that threshold on its own — a direct `rec['Account']['Owner']` works
  on a small run and returns `None` on a large one.
- **`as_float` exists because Bulk results are stringly typed.** `''` is null, not `0.0` —
  and conflating them re-creates exactly the bug rule 6 guards against.
- Output is written via a `.part` file and `os.replace`, because the destination is usually
  the OneDrive-synced folder, where a live write can die mid-write.
- Exit codes: `0` ok · `1` Salesforce query failed · `2` bad args.
