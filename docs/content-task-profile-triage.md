# Idea: triage content/event Inbound Sales Tasks by kununu profile score

**Status:** measured, not built. Session of 2026-08-18 (Wasil + Claude), org `headless`.
**Goal:** for `Inbound Sales Task: {Whitepaper Download | Webinaranmeldung | Webinarteilnahme}`,
resolve the lead's kununu profile, and where `profile_score < 3.0` close the task and move the
lead to the trash pool — the same "No Demand" line humans already apply by hand.

Prototype: `scripts/whitepaper_lead_profiles.py` (untracked, `scripts/*` is gitignored).
2 Salesforce queries, no LeadFeeder credits, no BigQuery.

## Why these task types need their own resolution path

`claimer` and `ebp_trial` Descriptions carry the kununu identity (`- Profil-ID:`, `- User-ID:`,
the profile URL) and `extract/parsers.py` just reads it. The content/event types carry **none**
of that — verified on live Descriptions 2026-08-18:

- Whitepaper: `Download: KNU WP 2026 Kulturecho / https://arbeitgeberportal.kununu.com/...`
  or `Job title:<x> | Whitepaper download via Facebook`
- Webinar: `Webinar-Anmeldung: <date> <title> | Position: <x> | # Employs: <band> | Profile: <type>`
  / a Sales-Ansatz paragraph plus the recording link

`Lead.KN_KununuUserUUID__c` **is** populated on most of these leads (20/24 in the first sample)
and is a **dead end**: neither the profiles feed nor the DWH `users.csv` export maps a user to a
profile, and the MCP `users__export_mcp` table has no profile column either. Do not re-derive it.

Contact-attached tasks sometimes append `account=https://kununu.lightning.force.com/001...`
to the Description; lead-attached ones never do (0 of 27 in the sample).

## Resolution cascade

1. **Contact-attached** — `Contact.AccountId` → `Account.KN_KununuProfileUuid__c` → feed record.
   Exact UUID equality, no name guessing. Fallback: `Account.KN_KununuLinkProfile__c` slug, then
   the Account name.
2. **Lead-attached** — the only key is free-text `Lead.Company`, normalized (umlauts folded,
   `gmbh|co|kg|ag|mbh|se|ohg|holding|und|and` stripped, non-alphanumerics collapsed) and matched
   against **both** `profile_name` and every `assigned_companies[].company_name` in the feed.
3. **LeadFeeder** — free `POST /companies/match` gives a canonical `matched_name`; re-run step 2
   on it. Measured yield: **1 of 12** unresolved leads (see below).

Source of truth is the feed CSV via `common.kununu_feed` (see `mpa_bridge.py`) — **not** the
kununu Data MCP, so this runs offline off the daily cache and reads `profile_score` from the
exact records the live-scope gate reads. The MCP's `profiles__export_mcp` was used once to
explore the idea; it additionally carries `account_id` (SF account mapped to the profile), which
the feed does not.

## Measured, 2026-08-18 (open = Status In Progress / in Arbeit / Not Started)

| | Tasks | Records | Profile matched | score < 3.0 |
|---|---|---|---|---|
| Contact-attached | 270 | 257 contacts | 264 tasks / 252 records (98%) | 8 tasks / 7 records |
| Lead-attached (IsConverted=false) | 47 | 44 leads | 32 tasks / 31 leads (72%) | 2 |
| **Total** | **317** | **301** | **296 (94%)** | **10** |

Subject split of the open tasks: Webinarteilnahme 227 (208 contact / 19 lead), Whitepaper
Download 89 (62 / 27), Webinaranmeldung 0 open. **`Webinarteilnahme` and `Webinaranmeldung` are
two different live subjects** — `Webinarteilnahme` is the big one.

Median matched score is **4.0**; only 33 more records sit in 3.0-3.5. **The <3.0 rule is a thin
filter** — it moves ~3% of the backlog. The bigger junk cluster on the lead side is
*name unresolvable* (freemail + garbage company: "Hbhmgk", "Bauleiter / Projektleiter",
"[nicht vorhanden]", "Li hamburg"), which a score rule never touches. Open question for Wasil:
does *no profile found* also mean trash?

### LeadFeeder overlap (free match, 0 credits)

Of 47 open lead-attached tasks: 30 match **both** kununu and LF, 2 kununu-only, 12 LF-only,
3 neither. LF matched 42 of 47 tasks — 27 of 29 corporate-email leads and 15 of 18 freemail ones,
so a private address costs less than expected (LF matches on `company_name` too, using the domain
only as a corroborating signal).

LF is a weak *rescue* for the unresolved ones, though: it returns a cleaner **name**, and the name
was never the bottleneck. **`profiles.csv` has no website/domain column**, so LF's real strength —
the email domain — has nothing to join against. A domain→profile join would need the DWH company
table or the Main-Profile-Account tagger, not this feed.

Plain substring search in the feed beats LF here, for free: `vdek` → "Verband der Ersatzkassen e.V.
(vdek)" 3.8; `Fa. Blum CNC Technik` → "Blum CNC Technik GmbH & Co. KG" 4.0; `HAW Hamburg` →
"Hochschule für Angewandte Wissenschaften Hamburg (HAW Hamburg)" 4.6. LF's own answer for Blum was
the *wrong* "Blum GmbH".

## Confidence — read before acting on a row

Lead-side matches by evidence strength (30 classified): **20 strong** (exact full normalized name,
single profile) · **5 medium** (several candidates, most-reviewed picked; or matched via an
assigned-company name) · **5 weak** (name ≤4-5 chars: "Ab", "Fab", "MSA", "ARAG SE", "BUWOG").

Known-bad or unresolved cases, by name:

- **"Ab" → AB GmbH (1.0)** — two letters, private address. Probably false, and it is one of the
  two trash candidates.
- **"Selbständig" → a profile literally named "Selbständig" (3.8)** — exact string, meaningless
  company. String tiering says strong; it is not. Same class as LF matching "[nicht vorhanden]"
  to a company called "Nicht vorhanden".
- **BUWOG** — matched the AT group profile (3.9) via an assigned-company entry while the lead is
  German; BUWOG Bauträger (3.8) is the likelier employer. Verdict unchanged, identity uncertain.
- **Krüger** — 5 profiles spanning 2.98-4.46. Sits exactly on the threshold; human call.

Contact-side correctness inherits the Account↔profile tagging (Main-Profile-Account's job); the
join itself is exact. Not audited here.

**The systematic gap:** the prototype has *no country check and no acronym guard*, unlike the
production pipeline's `enrich._match_sanity_reason`, which degrades acronym-like names (single
token ≤3 chars, or 4-char uppercase) unless a domain or VAT corroborates, and rejects a country
contradiction. The 5 weak matches are exactly what those guards exist for.

## Next steps, cheapest first

1. **Port the sanity guards** into the prototype (country from `profile_country_code` vs
   `Lead.Country`; acronym rule) so weak matches demote to `needs_review` instead of silently
   scoring. ~15 min.
2. **Token-subset name fallback** for the unresolved leads — recovers 3-4 of 12 where exact
   normalized equality misses (`vdek`, `Blum CNC Technik`, `HAW Hamburg`). Free.
3. Decide the *no profile found* policy (12 lead-side tasks today).
4. Only then consider LF retrieves (1 credit/company) — and note the feed cannot consume a
   website anyway, so this is likely not worth it.

## Cost

Salesforce: 2 queries per run (tasks, then Leads/Contacts by Id; the contact list routes to a
Bulk query job — **Bulk returns flat dotted keys** like `Account.Name`, not nested dicts, which
silently produced a 0% contact match rate on the first run). LeadFeeder: match is free, retrieves
are not. Feed: one streaming pass over the ~200 MB cached CSV, no network.
