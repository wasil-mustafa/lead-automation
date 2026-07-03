#!/usr/bin/env python3
"""
Analyse der Kununu profiles.jsonl
- Streamt die Datei zeilenweise (RAM-schonend bei ~530MB)
- Erfasst pro Feld: Anzahl Nicht-Null, Null-Rate, Typ
- Kategoriale Felder: Top-Werte + Anzahl unique
- Numerische Felder: min/max/avg/median + Verteilung
- Schreibt Ergebnis als JSON + lesbares TXT in den Folder
"""
import json
import sys
from collections import Counter, defaultdict
import statistics

PATH = "/Users/nicolaus.pichorner/Desktop/kununu api /Profile matchen/profiles.jsonl"
OUT_JSON = "/Users/nicolaus.pichorner/Desktop/kununu api /Profile matchen/profiles_analysis.json"
OUT_TXT = "/Users/nicolaus.pichorner/Desktop/kununu api /Profile matchen/profiles_analysis.txt"

# Felder, die wir als kategorial behandeln (begrenzte Wertemenge)
CATEGORICAL = [
    "profile_contract_type", "is_managed_profile", "profile_country_code",
    "profile_state", "profile_industry_name_de",
]
# Numerische Felder
NUMERIC = [
    "profile_score", "profile_reviews_online_count", "profile_salaries_online_count",
    "profile_cultures_completed_count", "profile_views_total",
    "profile_views_last_6_months", "profile_views_last_6_months_trend",
    "number_of_assigned_companies",
]
# Datumsfelder (claimed / badges) -> nur Null vs gesetzt
DATE_FLAGS = [
    "profile_first_claimed_on", "top_company_qualified_on",
    "top_company_last_assigned_on", "top_rated_salary_last_assigned_on",
    "top_rated_family_last_assigned_on",
]
BADGE_FIELDS = ["top_company_badges", "top_rated_salary_badges", "top_rated_family_badges"]

total = 0
null_counts = Counter()
cat_counters = {f: Counter() for f in CATEGORICAL}
num_values = {f: [] for f in NUMERIC}
date_set_counts = Counter()
badge_nonempty = Counter()
badge_values = {f: Counter() for f in BADGE_FIELDS}
uuid_seen = set()
dup_uuid = 0
url_country = Counter()  # /de/ /at/ /ch/ aus URL
multi_company = 0
all_keys = None

with open(PATH, "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        total += 1
        if all_keys is None:
            all_keys = set(d.keys())

        for k, v in d.items():
            if v is None or v == "" or v == []:
                null_counts[k] += 1

        u = d.get("profile_uuid")
        if u in uuid_seen:
            dup_uuid += 1
        else:
            uuid_seen.add(u)

        for f in CATEGORICAL:
            cat_counters[f][d.get(f)] += 1

        for f in NUMERIC:
            v = d.get(f)
            if v is not None:
                try:
                    num_values[f].append(float(v))
                except (ValueError, TypeError):
                    pass

        for f in DATE_FLAGS:
            if d.get(f):
                date_set_counts[f] += 1

        for f in BADGE_FIELDS:
            b = d.get(f) or []
            if b:
                badge_nonempty[f] += 1
                for item in b:
                    key = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                    badge_values[f][key] += 1

        url = (d.get("profile_url") or "")
        parts = url.split("/")
        if len(parts) > 3 and parts[3]:
            url_country[parts[3]] += 1

        if (d.get("number_of_assigned_companies") or 0) > 1:
            multi_company += 1

# ---- Auswertung aufbereiten ----
def num_stats(vals):
    if not vals:
        return None
    vals_sorted = sorted(vals)
    return {
        "count": len(vals),
        "min": min(vals),
        "max": max(vals),
        "avg": round(statistics.mean(vals), 3),
        "median": statistics.median(vals),
        "zeros": sum(1 for x in vals if x == 0),
    }

result = {
    "total_records": total,
    "duplicate_uuids": dup_uuid,
    "unique_uuids": len(uuid_seen),
    "fields": sorted(all_keys),
    "null_or_empty_rate": {
        k: {"count": null_counts[k], "pct": round(100 * null_counts[k] / total, 2)}
        for k in sorted(all_keys)
    },
    "categorical": {
        f: {"unique": len(c), "top": c.most_common(25)} for f, c in cat_counters.items()
    },
    "numeric": {f: num_stats(v) for f, v in num_values.items()},
    "date_flags_set": dict(date_set_counts),
    "badges_nonempty": dict(badge_nonempty),
    "badge_values": {f: c.most_common(20) for f, c in badge_values.items()},
    "url_country_prefix": url_country.most_common(),
    "multi_company_profiles": multi_company,
}

with open(OUT_JSON, "w", encoding="utf-8") as fh:
    json.dump(result, fh, ensure_ascii=False, indent=2)

# ---- Lesbarer Textreport ----
lines = []
def p(s=""):
    lines.append(s)

p("=" * 70)
p("KUNUNU PROFILES.JSONL - ANALYSE")
p("=" * 70)
p(f"Datensaetze gesamt:      {total:,}")
p(f"Unique profile_uuid:     {len(uuid_seen):,}")
p(f"Doppelte UUIDs:          {dup_uuid:,}")
p(f"Profile mit >1 Firma:    {multi_company:,}")
p(f"Anzahl Felder:           {len(all_keys)}")
p("")
p("--- NULL / LEER RATE PRO FELD ---")
for k in sorted(all_keys, key=lambda x: -null_counts[x]):
    pct = 100 * null_counts[k] / total
    p(f"  {k:<40} {null_counts[k]:>9,}  ({pct:5.1f}%)")
p("")
p("--- KATEGORIALE FELDER (Top-Werte) ---")
for f, c in cat_counters.items():
    p(f"\n  [{f}]  unique={len(c)}")
    for val, cnt in c.most_common(25):
        p(f"      {str(val):<45} {cnt:>9,}  ({100*cnt/total:4.1f}%)")
p("")
p("--- NUMERISCHE FELDER ---")
for f, v in num_values.items():
    s = num_stats(v)
    if s:
        p(f"\n  [{f}]")
        p(f"      count={s['count']:,}  min={s['min']}  max={s['max']}  "
          f"avg={s['avg']}  median={s['median']}  zeros={s['zeros']:,}")
p("")
p("--- DATUMS-/STATUS-FLAGS (gesetzt) ---")
for f in DATE_FLAGS:
    cnt = date_set_counts.get(f, 0)
    p(f"  {f:<40} {cnt:>9,}  ({100*cnt/total:4.1f}%)")
p("")
p("--- BADGES (Profile mit Badge) ---")
for f in BADGE_FIELDS:
    cnt = badge_nonempty.get(f, 0)
    p(f"  {f:<40} {cnt:>9,}  ({100*cnt/total:4.1f}%)")
    for val, c in badge_values[f].most_common(10):
        p(f"        {val:<40} {c:>8,}")
p("")
p("--- LAND (aus profile_url) ---")
for val, cnt in url_country.most_common():
    p(f"  {val:<10} {cnt:>9,}  ({100*cnt/total:4.1f}%)")

with open(OUT_TXT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

print("\n".join(lines))
print(f"\n\nGespeichert:\n  {OUT_JSON}\n  {OUT_TXT}")
