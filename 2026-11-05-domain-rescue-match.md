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
