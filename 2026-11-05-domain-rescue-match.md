# Domain-rescue match (2026-11-05)

A second LeadFeeder match pass for leads whose primary match the post-retrieve sanity
gate rejected as `country_conflict`. Code: `leadfeeder/enrich.py` (`build_rescue_payload`,
`rescue_accepts`, phase 2b), config `leadfeeder.match_domain_rescue`, backtest
`python -m leadauto domain-rescue-backtest`. Read with `2026-08-25-second-chance-match.md`,
whose asymmetry argument this pass reuses.

## The miss that started it

Lead `00QMz00000rhynBMAQ`, task `00TMz00001Z1EMGMA3`, "Baustein GmbH",
`kundenservice@baustein.com`, kununu `/de/baustein`. Run 41794806657 (2026-11-04 08:48):

| step | what happened |
|---|---|
| primary match (`company_name` + `email`) | ONE candidate: 44647102 "Baustein GmbH", score 0.89, name agreement 100 |
| phase-2 retrieve | country **AT** (baustein-gmbh.at, Engerwitzdorf) |
| sanity gate | `country_conflict:AT!=DE` — correct, that is not the lead's company |
| result | `no_lf_match` → terminal `skip`, tagged "EnrichLeadmanually", never revisited |

LF also holds 253467134 "Baustein GmbH", Germany, website **baustein.com** = the lead's
email domain. The name+email match never returned it, at width 3 or 5: LF does not weigh
the email domain against a company's own website.

## What was measured before writing code (free calls, 0 credits)

| payload | LF returns |
|---|---|
| `company_name` + `email`, top 5 | only the AT company |
| `company_name` + `url: baustein.com` | only 253467134 (DE, baustein.com), 0.88 |
| `url: https://www.baustein.com` | only 253467134, 0.82 |
| `company_name` + `email` + `country_code: DE` | three DE candidates; the right one ranks **third** (0.80) behind "BauStein GmbH" Kirchlengern (0.90) and a Stuttgart holding (0.89) — `_best_match` would pick a stranger |

So the rescue sends `company_name` + `url=<email domain>` (+ local `alt_names`), never
`email`, never `country_code`. The CLAUDE.md warning about country filters was measured
again and holds.

Also observed: the match response's `company_summary` now carries `url` and
`address.country_code` (it did not when the sanity gate was written, 2026-07-09).
`_best_match` carries them as `matched_url` / `matched_country_code` when present.

## The pass

1. Runs after phase 2, only for leads with `lf_rejected` starting `country_conflict`
   (acronym rejections are never rescued — their corroboration already failed on this
   domain) and a corporate email domain (freemail cannot identify a company).
2. One free `/companies/match` per `match_batch` candidates, width 3.
3. Accepted only when `_best_match`'s name gate passes **and** LF's website for the
   candidate is the lead's domain (`rescue_accepts`, subdomain-tolerant, no website =
   refused). A candidate whose summary country already conflicts is refused **before**
   the retrieve (`foreign:<cc>!=<cc>`).
4. A rescued company then goes through the SAME retrieve (1 credit if new) and sanity
   gate as a primary match (`rejected_again:` if it fails).
5. `match_pass = "domain"`: not primary, so it follows `live_scope.allow_wide_match`
   (true since 2026-09-28) and is flagged `yes_domain_match` on the QA Review sheet.
   `lf_rescue` is a Review column; the original `lf_rejected` reason is left intact.

The primary pass is untouched. A rescued match rests on two independent axes (name +
own website), which is stronger evidence than the primary name match alone.

## Backtest 2026-11-05 (`domain_rescue_backtest_2026-11-05.xlsx`, 0 credits)

Population: every `no_lf_match` prediction that carried an `lf_company_id` (= LF matched,
the gate rejected), newest per lead: 50 leads, 49 replayed (1 task type no longer
enabled). Judged on the match summary's country, no retrieve.

| outcome | n | meaning |
|---|---|---|
| recovered | **15** | rescue hit whose website is the lead's domain AND country agrees — production writes it back |
| recovered_foreign | 7 | website agrees, country still conflicts — production refuses before the retrieve |
| rescue_url_mismatch | 7 | a candidate came back, its website is not the domain — refused |
| rescue_no_match | 12 | LF holds nothing for the domain |
| drifted_no_conflict | 5 | the control no longer conflicts today (LF index moved) |
| drifted_unmatched | 3 | the control matches nothing today |

**15 of 41 still-conflicting leads (36.6 %) recovered**, among them Baustein GmbH,
Volksbank Hohenlimburg, ZOOM Entertainment, Lang Energie, GenTwo, Intervista. Every
recovered row was eyeballed: the LF website equals the lead's domain in all 15. The 7
`url_mismatch` refusals are right to be refused (Priller GmbH → priller-heizungsbau.de
vs priller.de is a different company; Little Dot Studios' German branch on the .com
domain is plausible but unprovable from the domain, so it stays with a human).

The 7 foreign hits are why the pre-retrieve country check exists: without it each
replay would spend 7 credits on companies the gate rejects anyway.

## Cost

Per run: one free match call per ≤200 rescue candidates; 1 LF credit per rescued
company at the retrieve (free again for 12 months). Zero Salesforce calls added.

## Live follow-up on the 15 recovered leads (2026-11-05, Wasil: "remove the campaign and do enrichment")

Checked against the org the same afternoon, before touching anything:

| live state | n | leads |
|---|---|---|
| converted by a human (Qualified, user-owned) | 8 | Navera, mosaic IT, Opus Solutions, Precident, Zesavi, Intervista, Lang Energie, GenTwo |
| `No Demand`, queue-owned | 6 | Volksbank Hohenlimburg, AZS, public vision, Leasing Union, ZOOM Entertainment, Spieloase |
| `New`, queue-owned | 1 | Baustein GmbH `00QMz00000rhynBMAQ` |

Only 4 of the 15 carried the pipeline's "EnrichLeadmanually" membership (the other 11
were predicted by DRY runs before tagging began on 2026-10-19). Done, one-off script in
the session scratchpad (`rescue_enrich.py`, pipeline modules, no watermark / prediction /
campaign re-tag / conversion):

- deleted the 4 memberships (Baustein + the three converted ones; one composite DELETE);
- enriched Baustein through the real path: primary match rejected `country_conflict:AT!=DE`,
  rescue hit 253467134 (baustein.com, DE), `create_new` / `no_match`, written back
  `KN_Employee__c 11-50`, `KN_VATNumber__c DE268985208`, Website, Industry, Region,
  kununu link, CountryCode DE — 0 LF credits, 0 failures. LF holds no address or phone
  for it, so Street stays blank (= `matched_incomplete` had it been tagged).
- the 6 `No Demand` leads were NOT enriched: `pipeline.lead_statuses: [New]` (Wasil,
  2026-09-28 — any other status means a human already worked the lead). Enrich by hand
  or say so explicitly.

Baustein is now an enriched `New` lead in no LF campaign: the scheduled run will not
re-read it (task processed) and the sweep will not see it (not a campaign member), so it
stays with a human unless it is tagged + given a `create_new` prediction row.

## Widened to leads with NO candidate (2026-11-06, lead 00QMz00000rhwTdMAI)

**The miss.** "PAU-Söhne Gesellschaft mit beschränkter Haftung" (s.pau@pau-maschinenbau.de,
/de/, claimer task of 2026-11-03) was predicted `no_lf_match` on 2026-11-04 and tagged
EnrichLeadmanually. Wasil found it by hand in LF as "PAU-Söhne GmbH". Replayed the same
day (free calls): LF returns **zero candidates** for the spelled-out name and the right
company (1231563, pau-maschinenbau.de, DE) at 0.8 / agreement 100 for the "GmbH" form.
Not a gate problem — LF's own name search chokes on the register's long form.

**Two fixes, both shipped the same day:**

1. `textnorm.abbreviate_legal_form` collapses the spelled-out German legal forms
   ("Gesellschaft mit beschränkter Haftung" → GmbH, Aktiengesellschaft → AG,
   Kommanditgesellschaft → KG, KGaA, OHG, UG, eG, e.V., e.K.). Applied to the name
   `build_match_payload` sends to LF AND inside `normalize_name`, so the dedupe rules
   see an Account "PAU-Söhne GmbH" as the same name. Only 1 of the 201 historical
   `no_lf_match` leads carried a long form, so this is the narrow fix.
2. The domain rescue now also re-asks a lead whose primary (and wide) pass returned
   **no candidate at all** — same payload (`company_name` + `url=<email domain>`), same
   two-axis acceptance (`_best_match` name gate + `rescue_accepts` website), same
   foreign-country refusal before the retrieve. A recovery here is
   `lf_rescue = recovered_unmatched` (vs `recovered` for the conflict arm) so the two
   populations can be scored apart on the QA sheet and in shadow-compare. A lead whose
   primary chunk FAILED is never re-asked (same rule as the wide pass).

**Backtest 2026-11-06 (`domain_rescue_backtest_2026-11-06.xlsx`, 0 credits).** Population
widened to every latest `no_lf_match` prediction: 201 leads, 200 replayed (1 dropped:
task type no longer enabled), production payload with feed alt names, `arm` column.

| outcome | n |
|---|---|
| `unmatched_recovered` (control matches nothing, rescue hits the lead's domain, country agrees) | **16** |
| `unmatched_no_match` | 105 |
| `unmatched_url_mismatch` (refused, 0 credits) | 5 |
| `drifted_matched_today` (control matches at width 3 today — PAU-Söhne is one, via fix 1) | 18 |
| `no_corporate_domain` (freemail, never asked) | 10 |
| conflict arm: `recovered` / `recovered_foreign` / `rescue_url_mismatch` / `rescue_no_match` / `drifted_no_conflict` | 15 / 7 / 7 / 12 / 5 |

Recovery rate of the still-unmatched arm: **16 / 126 = 12.7 %**. Eyeballed all 16: the
hits are the lead's own company or its legal entity on the same website
(Cgs Flughafen Zürich → CGS Customer Ground Service AG on cgs-ltd.com; elektro4000 Hagen
E-Commerce GmbH → Hagen E-Commerce GmbH on elektro4000.de; AWO Bezirksverband Schwaben
e.V. → Arbeiterwohlfahrt Bezirksverband Schwaben e. V.). Two are the known
`lf_name_dropped` class — a sibling entity on the group domain (Verkehrsakademie &
Fahrschule **Hamburg** → **SH** GmbH on verkehrsakademie-group.de) — which the gate was
calibrated to accept for lead matching. Rescued matches stay `match_pass = "domain"`
and so follow `allow_wide_match`; watch `lf_rescue` on the next runs' QA sheets.

The earlier scratch replay the same day (Lead.Company + email, no alt names) gave
13 of 117 — the production payload is both wider (alt names) and asked less (control
matched 18, not 74), so the committed number is the backtest's, not the probe's.

**Cost.** One more free match call per ≤200 unmatched leads per run; 1 credit per
recovered company at the retrieve. Zero Salesforce calls added. The PAU-Söhne lead
itself is NOT revisited by the pipeline — the EnrichLeadmanually tag anti-joins it out
of every later read — so it stays a human's (Wasil found it by hand, which is how this
started).
