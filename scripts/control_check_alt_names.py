"""Validate the assigned-companies (alt-names) LF gate on the 6-month control set.

Re-runs the 1,101 converted-lead control set (Export/control_leads_6m.xlsx) through
LeadFeeder company match (FREE — no credits) and applies the production acceptance
gate (LeadfeederClient._best_match) twice on the SAME raw responses:
  - OLD gate: payload without alt_names (pre-feed behavior, 89.4%/1.1% baseline)
  - NEW gate: payload with alt_names from the kununu DWH profile feed
Ground truth per accepted match: agreement of the LF company name with the account
the lead actually converted to (name + website-domain root, gate-style scoring).

Raw LF responses are cached to a JSON file so re-analysis is offline.

Usage:  PYTHONPATH=src python scripts/control_check_alt_names.py
Needs:  LEADFEEDER_API_KEY.txt (or env), KUNUNU_API_TOKEN in .env, feed cache.
Output: Export/lf_match_check_6m_altnames.xlsx (Results / Suspects / Summary).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from leadauto.config import Settings
from leadauto.kununu_feed import base_slug, ensure_feed, lookup_profiles
from leadauto.leadfeeder.client import LeadfeederClient
from leadauto.textnorm import alnum, domain_root, normalize_host, normalize_name

CONTROL = REPO / "Export" / "control_leads_6m.xlsx"
OUT = REPO / "Export" / "lf_match_check_6m_nocountry.xlsx"
RAW_CACHE = REPO / "Export" / "lf_raw_match_nocountry.json"


def s(v) -> str:
    return "" if pd.isna(v) else str(v).strip()


def make_client(settings) -> LeadfeederClient:
    key = os.environ.get("LEADFEEDER_API_KEY")
    key_file = REPO / "LEADFEEDER_API_KEY.txt"
    if not key and key_file.exists():
        key = key_file.read_text().strip()
    return LeadfeederClient(
        api_key=key,
        min_match_score=settings.lf_min_match_score,
        name_agree_min=settings.lf_name_agree_min,
        name_agree_rescue=settings.lf_name_agree_rescue,
        match_batch=settings.lf_match_batch,
        max_requests_per_min=settings.lf_max_requests_per_min,
        max_retries=settings.lf_max_retries,
    )


def build_payloads(df: pd.DataFrame, profiles: dict) -> tuple[list[dict], list[dict]]:
    """(payloads mirroring enrich.py, per-lead feed info rows)."""
    payloads, feed_rows = [], []
    for _, row in df.iterrows():
        company = s(row["Company"])
        # No country/country_code — mirrors enrich.py (LF's country filter suppresses
        # good matches; 2026-07-03 probe: 59/59 regained candidates were correct).
        payload = {"company_name": company, "email": s(row["Email"])}
        uuid = s(row["Acc_KununuProfileUuid"])
        slug = base_slug(s(row["KN_KununuLinkProfile"]) or None)
        info = profiles.get(uuid) or profiles.get(slug or "")
        alts: list[str] = []
        if info:
            own = normalize_name(company)
            for n in info.companies:
                nn = normalize_name(n)
                if nn and nn != own and n not in alts:
                    alts.append(n)
            if alts:
                payload["alt_names"] = alts
        payloads.append(payload)
        feed_rows.append({
            "ProfileUuid": uuid, "Slug": slug or "",
            "FeedResolved": info is not None,
            "FeedProfileName": info.name if info else "",
            "AltNames": " | ".join(alts), "AltCount": len(alts),
        })
    return payloads, feed_rows


def raw_match(client: LeadfeederClient, payloads: list[dict]) -> list[list]:
    """Per-lead raw candidate arrays (cached). Same API call as match_companies."""
    if RAW_CACHE.exists():
        print(f"Using cached raw responses: {RAW_CACHE}")
        return json.loads(RAW_CACHE.read_text(encoding="utf-8"))
    account_id = client.get_account_id()
    out: list[list] = []
    for i in range(0, len(payloads), client.match_batch):
        chunk = payloads[i:i + client.match_batch]
        api_chunk = [{k: v for k, v in p.items() if k != "alt_names"} for p in chunk]
        data = client.api_post(
            "/companies/match", payload={"companies": api_chunk},
            params={"account_id": account_id, "max_results_per_company": 3},
        )
        per_company = data.get("data", []) or []
        out.extend(per_company[j] if j < len(per_company) else [] for j in range(len(chunk)))
        print(f"matched {min(i + client.match_batch, len(payloads))}/{len(payloads)}")
    RAW_CACHE.write_text(json.dumps(out), encoding="utf-8")
    return out


def truth_agreement(lf_name: str, account_name: str, account_website: str) -> tuple[int, int, int]:
    """(sim vs account name, sim vs account site root, max) — gate-style scoring."""
    sim_name = sim_site = 0
    cand = normalize_name(lf_name)
    ours = normalize_name(account_name)
    if cand and ours:
        sim_name = int(fuzz.token_set_ratio(cand, ours))
    root = domain_root(normalize_host(account_website))
    if root and lf_name:
        sim_site = int(fuzz.partial_ratio(alnum(lf_name), root))
    return sim_name, sim_site, max(sim_name, sim_site)


def main() -> None:
    settings = Settings.load()
    df = pd.read_excel(CONTROL)
    print(f"Control leads: {len(df)}")

    feed = ensure_feed(settings)
    if feed is None:
        sys.exit("No kununu feed available — set KUNUNU_API_TOKEN")
    uuids = {s(v) for v in df["Acc_KununuProfileUuid"] if s(v)}
    slugs = {base_slug(s(v)) for v in df["KN_KununuLinkProfile"] if s(v)}
    slugs.discard(None)
    print(f"Streaming feed for {len(uuids)} uuids / {len(slugs)} slugs ...")
    profiles = lookup_profiles(feed, uuids, slugs)
    print(f"Feed keys resolved: {len(profiles)}")

    payloads, feed_rows = build_payloads(df, profiles)
    client = make_client(settings)
    raw = raw_match(client, payloads)

    floor = settings.lf_min_match_score
    rows = []
    for i, (_, lead) in enumerate(df.iterrows()):
        p, matches = payloads[i], raw[i]
        old = client._best_match(matches, {k: v for k, v in p.items() if k != "alt_names"})
        new = client._best_match(matches, p)
        top_score = max((m.get("attributes", {}).get("match_score", 0) for m in matches), default=None)
        row = {
            "LeadId": lead["LeadId"], "Company": s(lead["Company"]), "Email": s(lead["Email"]),
            "Country": s(lead["Country"]), "AccountName": s(lead["AccountName"]),
            "AccountWebsite": s(lead["AccountWebsite"]), **feed_rows[i],
            "Candidates": len(matches), "TopScore": top_score,
        }
        for tag, m in (("Old", old), ("New", new)):
            row[f"{tag}_Accepted"] = m is not None
            row[f"{tag}_CompanyId"] = m["company_id"] if m else ""
            row[f"{tag}_Name"] = m["matched_name"] if m else ""
            row[f"{tag}_Score"] = m["match_score"] if m else None
            row[f"{tag}_Agree"] = m["name_agreement"] if m else None
            row[f"{tag}_Via"] = (m.get("agreed_via") or "") if m else ""
            row[f"{tag}_Rescued"] = bool(m) and m["match_score"] < floor
            if m:
                sim_n, sim_s, best = truth_agreement(
                    m["matched_name"], s(lead["AccountName"]), s(lead["AccountWebsite"]))
                row[f"{tag}_TruthSimName"] = sim_n
                row[f"{tag}_TruthSimSite"] = sim_s
                row[f"{tag}_Truth"] = best
                row[f"{tag}_Wrong"] = best < 85
            else:
                row[f"{tag}_TruthSimName"] = row[f"{tag}_TruthSimSite"] = row[f"{tag}_Truth"] = None
                row[f"{tag}_Wrong"] = None
        row["ChangedByAlts"] = (row["Old_Accepted"] != row["New_Accepted"]
                                or row["Old_CompanyId"] != row["New_CompanyId"])
        rows.append(row)

    res = pd.DataFrame(rows)
    n = len(res)

    def pct(x, base=n):
        return f"{100 * x / base:.1f}%" if base else ""

    old_acc = res["Old_Accepted"].sum()
    new_acc = res["New_Accepted"].sum()
    old_wrong = (res["Old_Wrong"] == True).sum()  # noqa: E712
    new_wrong = (res["New_Wrong"] == True).sum()  # noqa: E712
    gained = res[~res["Old_Accepted"] & res["New_Accepted"]]
    summary = pd.DataFrame([
        ("Control leads", n, "", ""),
        ("Feed profile resolved", int(res["FeedResolved"].sum()), pct(res["FeedResolved"].sum()), ""),
        ("Leads with >=1 alt name", int((res["AltCount"] > 0).sum()), pct((res["AltCount"] > 0).sum()), ""),
        ("LF returned a candidate", int((res["Candidates"] > 0).sum()), pct((res["Candidates"] > 0).sum()), "93.9%"),
        ("Score >= floor (score-only)", int((res["TopScore"] >= floor).sum()), pct((res["TopScore"] >= floor).sum()), "82.0%"),
        ("OLD gate accepted (no alt names)", int(old_acc), pct(old_acc), "89.4%"),
        ("NEW gate accepted (with alt names)", int(new_acc), pct(new_acc), ""),
        ("  newly accepted thanks to alts", len(gained), "", ""),
        ("  candidate changed by alts", int(res["ChangedByAlts"].sum()), "", ""),
        ("OLD wrong-company (truth < 85) of accepted", int(old_wrong), pct(old_wrong, old_acc), "1.1%"),
        ("NEW wrong-company (truth < 85) of accepted", int(new_wrong), pct(new_wrong, new_acc), ""),
        ("OLD rescues (accepted below floor)", int(res["Old_Rescued"].sum()), "", "115"),
        ("NEW rescues (accepted below floor)", int(res["New_Rescued"].sum()), "", ""),
        ("NEW accepted via alt_name", int(res["New_Via"].str.startswith("alt_name").sum()), "", ""),
    ], columns=["Metric", "Count", "Pct", "Baseline_2026-07-02"])

    suspects = res[(res["New_Wrong"] == True)].copy()  # noqa: E712
    with pd.ExcelWriter(OUT, engine="openpyxl") as xw:
        res.to_excel(xw, sheet_name="Results", index=False)
        suspects.to_excel(xw, sheet_name="Suspects", index=False)
        summary.to_excel(xw, sheet_name="Summary", index=False)
    print(f"\nWrote {OUT}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
