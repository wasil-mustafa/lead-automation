# Lead Automation

Automates the processing of new Salesforce sales leads at kununu.

## The goal

When someone claims a kununu company profile or starts an EBP trial, a task lands in a lead queue or to
a contact on an account. We will only be looking at automating tasks which land in a lead queue. 
Someone then has to figure out: *Who is this company? Do we already know them? 
Which of our existing accounts do they belong to — or are they brand new?* Then they enrich the lead with
company data, fill in the required fields, and convert it into an account and contact.

This project does all of that automatically, on a schedule, converting only the leads whose
enrichment came back complete and leaving every doubtful one to a human.
[LeadFeeder](https://www.leadfeeder.com/) acts as the
external data source: given a company name or email domain, it identifies the actual company and
provides the firmographic data (industry, size, revenue, address) needed to enrich the lead and
recognize duplicates.

## How it works

One scheduled run, five times a day, does everything:

### Find, enrich, and match

It:

1. **Picks up new leads** that have an open task and are in a leadpool queue
   (currently: profile claims and EBP trials).
2. **Identifies the company** behind the lead using LeadFeeder: the lead's company name (kununu profile name) 
   and email are sent to LeadFeeder's matching API, which returns the most likely real-world company along
   with firmographic data like size, address, website, phone and VAT. Because a wrong company is worse than no company,
   matches are only accepted when the returned name actually agrees with
   the lead's, and are rejected again if the company's country contradicts the lead or a short
   acronym name has nothing else backing it up. Where possible, LeadFeeder also supplies contact
   details (position, phone) for the person behind the lead.
4. **Checks for duplicates** against what already exists in Salesforce, on two levels:
   - **kununu profile level** — the strongest signal. If an existing account already carries the
     same kununu profile (via its profile ID or profile URL), that's the same company, and it wins
     even over a convincing name-and-address match on a different account.
   - **Account level** — the enriched company data is compared against existing accounts by
     normalized name plus a corroborating signal: same address, same VAT number, or matching
     website/email domain. A name match alone, or a name that matches several accounts, isn't
     trusted blindly — those go to human review instead.

   Ordered rules turn this into one of three outcomes: link the lead to an existing account,
   create a new one, or flag it for a human to decide. (The matching contact is found the same
   way, via email or kununu user ID.)
5. **Fills in the lead** with the enriched data and everything Salesforce requires for conversion.
6. **Tags the lead with a campaign** that says how the enrichment went: complete, incomplete,
   or no company found. Sales uses those campaigns to pick up what the automation left.

### Convert

In the same run, right after the tagging:

1. **Picks the leads to convert**: those tagged *complete* whose matching said "this company is
   new" — this run's, plus any from earlier runs that are still unconverted (so a lead never
   gets stuck). Leads that match an existing account, or that could not be decided, stay with
   a human.
2. **Runs a final safety check** on each lead against Salesforce's own conversion rules and
   skips anything that fails, with a report of why.
3. **Converts each lead** into a new account and contact. Leads from the same company become
   one account with multiple contacts, not duplicates.
4. **Produces a workbook** (Excel) listing every lead, what the pipeline found, what it decided,
   why, and what it converted.

## Safety first

The pipeline is deliberately cautious:

- **Dry-run by default** — a manual run analyzes and reports, but doesn't write anything; only
  the scheduler runs live.
- **Narrow conversion scope** — only fully enriched leads whose company is new are converted;
  everything that matches an existing account or is unclear stays with a human.
- **Shadow comparison** — the pipeline's decisions on the leads it leaves to humans are
  recorded and compared against what those humans actually did. That accuracy report was the
  evidence for going live. (A backtest on 283 historical cases showed the matching picks the
  right account 87% of the time; on the "new company" decision it agreed with humans 300 of
  304 times.)
- **Skip rather than guess** — if the company can't be confidently identified, the lead is left
  alone for a human instead of being converted on shaky data.

## Status

Live since 2026-10-19 (enrichment) and 2026-10-31 (conversion); one fused run since 2026-11-01.

## For developers

The code lives under `src/leadauto/` and runs via GitHub Actions through cron-jobs. Technical details, commands,
and conventions are documented in [`CLAUDE.md`](CLAUDE.md) and
[`src/leadauto/convert/CLAUDE.md`](src/leadauto/convert/CLAUDE.md).
