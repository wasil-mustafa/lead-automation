#!/usr/bin/env python3
"""
Kununu-Profil <-> Salesforce-Account Matching & Kontrolle  (READ-ONLY Quelle)

Teil A  KONTROLLE der bereits verknuepften Accounts (sf_linked.csv):
        Ist das BESTE *freie* Profil hinterlegt? Bewertung pro Slug-Gruppe (z.B. siemens, siemens1, ...):
        1) Status  EBP Pro > EBP > EBP Basic > Claimed > Not Claimed (none)
        2) kununu Score (hoeher = besser)
        3) Anzahl Bewertungen (mehr = besser)
        4) Traffic / Views total (hoeher = besser)
        -> Vorschlag NUR wenn ein besseres Profil existiert, das NICHT bereits auf einem
           anderen Account hinterlegt ist (sonst Zeile auslassen).
        -> Auffaellig auch wenn das verknuepfte UUID nicht im Dump ist.

Teil B  MATCHING der Accounts OHNE Profil (sf_unlinked.csv):
        Kandidatensuche ueber normalisierten Namen (+ Stadt + Land), inkl. Konfidenz.
        - Accounts mit Owner 'Papierkorb' werden ausgelassen.
        - Bereits anderswo hinterlegte Profile werden NICHT erneut vorgeschlagen.

Output: profile_matching_ergebnis.xlsx  (Sheets: Kontrolle, Neue_Matches, Summary)
"""
import csv, json, re, sys
from collections import defaultdict
from difflib import SequenceMatcher
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

BASE = "/Users/nicolaus.pichorner/Desktop/kununu api /Profile matchen/"
JSONL = BASE + "profiles.jsonl"
LINKED = BASE + "sf_linked.csv"
UNLINKED = BASE + "sf_unlinked.csv"
OCCUPIED = BASE + "sf_occupied.csv"
OUT = BASE + "profile_matching_ergebnis.xlsx"

csv.field_size_limit(10**7)

# ---------- Normalisierung ----------
LEGAL = {
    "gmbh","mbh","ggmbh","ag","se","kg","kgaa","ug","ohg","gbr","ev","ek","co",
    "llc","ltd","inc","plc","sa","srl","bv","nv","aps","oy","ab","kgmbh",
    "haftungsbeschraenkt","haftungsbeschrankt","haftungsbeschr","cokg","gmbhcokg",
}
def norm_name(s):
    if not s: return ""
    s = s.lower().strip()
    s = s.replace("&"," und ").replace("ä","ae").replace("ö","oe").replace("ü","ue").replace("ß","ss")
    s = re.sub(r"[^a-z0-9]+"," ",s)
    toks = [t for t in s.split() if t and t not in LEGAL]
    return " ".join(toks)

def norm_city(s):
    if not s: return ""
    s = s.lower().strip()
    s = s.replace("ä","ae").replace("ö","oe").replace("ü","ue").replace("ß","ss")
    s = re.sub(r"[^a-z0-9]+","",s)
    return s

COUNTRY_MAP = {
    "germany":"de","deutschland":"de","de":"de",
    "austria":"at","oesterreich":"at","österreich":"at","at":"at",
    "switzerland":"ch","schweiz":"ch","suisse":"ch","ch":"ch",
}
def map_country(s):
    if not s: return None
    return COUNTRY_MAP.get(s.strip().lower())

def base_slug(url):
    if not url: return None
    seg = url.rstrip("/").split("/")[-1]
    # kununu-Dedup-Suffix haengt DIREKT an einem Buchstaben (siemens411, kreatize1, solutions2).
    # Zahlen die Teil des Namens sind, stehen hinter '-' (promedis-24) -> bleiben.
    return re.sub(r"(?<=[a-z])\d+$","",seg)

# ---------- Status-Ranking (inkl. Bewertungen) ----------
CONTRACT_RANK = {"ebp_pro":5,"ebp":4,"ebp_basic":3,"claimed":2,"none":1}
def prof_rank(p):
    # Anzahl Bewertungen > Status > Score > Traffic
    return (p["reviews"], CONTRACT_RANK.get(p["contract"],0), p["score"], p["views_total"])

def rank_ctrl(p, acc_city):
    # Kontrolle: PASSENDER STANDORT zuerst, dann Bewertungen > Status > Score > Traffic.
    # Ein Profil am richtigen Standort (= Account-Stadt) ist das richtige, auch wenn ein
    # anderes Profil mehr Bewertungen/hoeheren Score hat.
    cm = 1 if (acc_city and norm_city(p["city"])==acc_city) else 0
    return (cm,) + prof_rank(p)

# ---------- 0) Occupied-Set: alle bereits (irgendwo) hinterlegten UUIDs ----------
print("Lade belegte UUIDs (sf_occupied.csv) ...", file=sys.stderr)
occupied=set()
with open(OCCUPIED, encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        u=(row.get("KN_KununuProfileUuid__c") or "").strip()
        if u: occupied.add(u)
print(f"  {len(occupied):,} belegte (distinct) UUIDs", file=sys.stderr)

# ---------- 1) Profile laden + Indizes ----------
print("Lade profiles.jsonl ...", file=sys.stderr)
by_uuid = {}
slug_groups = defaultdict(list)
name_idx = defaultdict(list)
name_idx_anycc = defaultdict(list)
token_block = defaultdict(list)
n=0
with open(JSONL, encoding="utf-8") as fh:
    for line in fh:
        line=line.strip()
        if not line: continue
        d=json.loads(line); n+=1
        try: score=float(d.get("profile_score") or 0)
        except: score=0.0
        p={
            "uuid":d["profile_uuid"],"name":(d.get("profile_name") or "").strip(),
            "url":d.get("profile_url") or "","cc":d.get("profile_country_code"),
            "city":d.get("profile_city"),"state":d.get("profile_state"),
            "contract":d.get("profile_contract_type") or "none","score":score,
            "views_total":d.get("profile_views_total") or 0,
            "views_6m":d.get("profile_views_last_6_months") or 0,
            "reviews":d.get("profile_reviews_online_count") or 0,
        }
        by_uuid[p["uuid"]]=p
        bs=base_slug(p["url"])
        if bs: slug_groups[(p["cc"],bs)].append(p)
        nn=norm_name(p["name"])
        if nn:
            name_idx[(p["cc"],nn)].append(p)
            name_idx_anycc[nn].append(p)
            token_block[(p["cc"],nn.split()[0])].append(p)
print(f"  {n:,} Profile geladen, {len(slug_groups):,} Slug-Gruppen", file=sys.stderr)

def owner_of(row): return (row.get("Owner.Name") or "").strip()

# ---------- 2) Teil A: Kontrolle ----------
print("Kontrolle der verknuepften Accounts ...", file=sys.stderr)
kontrolle=[]
stat_ok=stat_better=stat_missing=stat_better_but_taken=0
with open(LINKED, encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        uuid=(row.get("KN_KununuProfileUuid__c") or "").strip()
        cur=by_uuid.get(uuid)
        acc_name=row.get("Name",""); owner=owner_of(row)
        if not cur:
            stat_missing+=1
            kontrolle.append({
                "Status":"PROFIL_NICHT_IM_DUMP","Account":acc_name,"Account_Owner":owner,"Account_Id":row.get("Id"),
                "Land":row.get("BillingCountry"),"Stadt":row.get("BillingCity"),
                "Verknuepfte_UUID":uuid,"Verknuepfter_Link":row.get("KN_KununuLinkProfile__c"),
                "SF_Status":row.get("KN_StatusKununuProfile__c"),"SF_Score":row.get("KN_KununuScore__c"),
                "Aktuell_Reviews":"","Aktuell_Traffic":"",
                "Empfohlenes_Profil":"","Empf_UUID":"","Empf_Status":"","Empf_Score":"","Empf_Reviews":"","Empf_Traffic":"",
                "Gruppe_Groesse":"","Grund":"Verknuepftes kununu-UUID existiert nicht (mehr) im aktuellen Dump - pruefen/aktualisieren",
            })
            continue
        bs=base_slug(cur["url"])
        group=slug_groups.get((cur["cc"],bs),[cur])
        acc_city=norm_city(row.get("BillingCity"))
        # nur FREIE Alternativen (nicht anderswo belegt) + aktuelles Profil als Baseline
        pool=[cur]+[p for p in group if p["uuid"]!=cur["uuid"] and p["uuid"] not in occupied]
        best=max(pool, key=lambda p: rank_ctrl(p, acc_city))
        if best["uuid"]==cur["uuid"]:
            # pruefen ob es theoretisch ein besseres gaebe, das aber belegt ist (nur Statistik)
            theo=max(group, key=lambda p: rank_ctrl(p, acc_city))
            if theo["uuid"]!=cur["uuid"]: stat_better_but_taken+=1
            stat_ok+=1
            continue
        stat_better+=1
        r_cur,r_best=rank_ctrl(cur,acc_city),rank_ctrl(best,acc_city)
        reasons=[]
        if r_best[0]!=r_cur[0]: reasons.append(f"Standort passt besser (Profil-Stadt '{best['city']}' = Account-Stadt)")
        if r_best[1]!=r_cur[1] and r_best[0]==r_cur[0]: reasons.append(f"Mehr Bewertungen ({best['reviews']} > {cur['reviews']})")
        if r_best[2]!=r_cur[2] and r_best[:2]==r_cur[:2]: reasons.append(f"Status besser ({best['contract']} > {cur['contract']})")
        if r_best[3]!=r_cur[3] and r_best[:3]==r_cur[:3]: reasons.append(f"Score besser ({best['score']} > {cur['score']})")
        if r_best[4]!=r_cur[4] and r_best[:4]==r_cur[:4]: reasons.append(f"Traffic besser ({best['views_total']} > {cur['views_total']})")
        if not reasons: reasons.append("Besseres freies Profil in Slug-Gruppe")
        kontrolle.append({
            "Status":"BESSERES_PROFIL_VERFUEGBAR","Account":acc_name,"Account_Owner":owner,"Account_Id":row.get("Id"),
            "Land":row.get("BillingCountry"),"Stadt":row.get("BillingCity"),
            "Verknuepfte_UUID":uuid,"Verknuepfter_Link":cur["url"],
            "SF_Status":cur["contract"],"SF_Score":cur["score"],
            "Aktuell_Reviews":cur["reviews"],"Aktuell_Traffic":cur["views_total"],
            "Empfohlenes_Profil":best["url"],"Empf_UUID":best["uuid"],
            "Empf_Status":best["contract"],"Empf_Score":best["score"],
            "Empf_Reviews":best["reviews"],"Empf_Traffic":best["views_total"],
            "Gruppe_Groesse":len(group),"Grund":"; ".join(reasons),
        })
print(f"  OK={stat_ok:,}  besser(frei)={stat_better:,}  nicht_im_dump={stat_missing:,}  "
      f"(besser-aber-belegt-uebersprungen={stat_better_but_taken:,})", file=sys.stderr)

# ---------- 3) Teil B: Matching unverknuepfter ----------
print("Matching der Accounts OHNE Profil ...", file=sys.stderr)
matches=[]
m_high=m_med=m_low=m_none=0; m_papierkorb=0
def free(cands): return [p for p in cands if p["uuid"] not in occupied]
def pick_best(c): return max(c, key=prof_rank)

with open(UNLINKED, encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        owner=owner_of(row)
        if "papierkorb" in owner.lower():
            m_papierkorb+=1; continue
        acc_name=row.get("Name","")
        nn=norm_name(acc_name)
        cc=map_country(row.get("BillingCountry"))
        acc_city=norm_city(row.get("BillingCity"))
        if not nn:
            m_none+=1; continue

        cc_note=""
        cands = list(name_idx.get((cc,nn),[])) if cc else []
        if not cands:
            cands=list(name_idx_anycc.get(nn,[]))
            if cands: cc_note=" (Land abweichend/unbekannt)"
        cands=free(cands)   # bereits belegte Profile raus

        conf=""; reason=""; best=None; n_c=0
        if cands:
            n_c=len(cands)
            city_hits=[p for p in cands if acc_city and norm_city(p["city"])==acc_city]
            if city_hits:
                best=pick_best(city_hits)
                conf="HOCH"; reason=(f"Name+Stadt+Land exakt{cc_note}" if len(city_hits)==1
                                     else f"Name+Stadt exakt, {len(city_hits)} freie Profile -> bestes gewaehlt{cc_note}")
            else:
                best=pick_best(cands)
                conf="MITTEL"; reason=(f"Name exakt, Stadt nicht bestaetigt{cc_note}" if len(cands)==1
                                       else f"Name exakt aber {len(cands)} freie Kandidaten, Stadt unklar -> bestes gewaehlt{cc_note}")
        else:
            tok=nn.split()[0]
            block=free(token_block.get((cc,tok),[])) if cc else []
            bestratio=0
            for p in block[:400]:
                r=SequenceMatcher(None,nn,norm_name(p["name"])).ratio()
                if r>bestratio: bestratio=r; best=p
            if best and bestratio>=0.88:
                n_c=1
                if acc_city and norm_city(best["city"])==acc_city:
                    conf="MITTEL"; reason=f"Fuzzy {bestratio:.2f} + Stadt exakt"
                else:
                    conf="NIEDRIG"; reason=f"Fuzzy-Namensaehnlichkeit {bestratio:.2f}"
            else:
                m_none+=1; continue

        if conf=="HOCH": m_high+=1
        elif conf=="MITTEL": m_med+=1
        else: m_low+=1
        matches.append({
            "Konfidenz":conf,"Account":acc_name,"Account_Owner":owner,"Account_Id":row.get("Id"),
            "Land":row.get("BillingCountry"),"Stadt":row.get("BillingCity"),
            "Kandidat_Profil":best["url"],"Kandidat_Name":best["name"],"Kandidat_UUID":best["uuid"],
            "Kandidat_Stadt":best["city"],"Kandidat_Status":best["contract"],"Kandidat_Score":best["score"],
            "Kandidat_Reviews":best["reviews"],"Kandidat_Traffic":best["views_total"],
            "Anzahl_freie_Kandidaten":n_c,"Grund":reason,
        })
print(f"  HOCH={m_high:,} MITTEL={m_med:,} NIEDRIG={m_low:,} kein_Treffer={m_none:,} "
      f"Papierkorb_ausgelassen={m_papierkorb:,}", file=sys.stderr)

# ---------- 4) Excel ----------
print("Schreibe Excel ...", file=sys.stderr)
wb=openpyxl.Workbook()
hdr_fill=PatternFill("solid",fgColor="1F4E78"); hdr_font=Font(bold=True,color="FFFFFF")
warn_fill=PatternFill("solid",fgColor="FCE4D6"); miss_fill=PatternFill("solid",fgColor="FFF2CC")
green=PatternFill("solid",fgColor="E2EFDA"); yellow=PatternFill("solid",fgColor="FFF2CC"); red=PatternFill("solid",fgColor="FCE4D6")

def write_sheet(ws, rows, cols):
    ws.append(cols)
    for c in range(1,len(cols)+1):
        cell=ws.cell(1,c); cell.fill=hdr_fill; cell.font=hdr_font; cell.alignment=Alignment(vertical="center")
    for r in rows: ws.append([r.get(c,"") for c in cols])
    ws.freeze_panes="A2"
    for i,c in enumerate(cols,1):
        w=max([len(c)]+[len(str(r.get(c,""))) for r in rows[:500]]) if rows else len(c)
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width=min(max(w+2,10),55)

ws1=wb.active; ws1.title="Kontrolle"
cols1=["Status","Account","Account_Owner","Account_Id","Land","Stadt","Verknuepfte_UUID","Verknuepfter_Link",
       "SF_Status","SF_Score","Aktuell_Reviews","Aktuell_Traffic","Empfohlenes_Profil","Empf_UUID","Empf_Status",
       "Empf_Score","Empf_Reviews","Empf_Traffic","Gruppe_Groesse","Grund"]
write_sheet(ws1,kontrolle,cols1)
for i in range(2,len(kontrolle)+2):
    ws1.cell(i,1).fill=miss_fill if ws1.cell(i,1).value=="PROFIL_NICHT_IM_DUMP" else warn_fill

ws2=wb.create_sheet("Neue_Matches")
cols2=["Konfidenz","Account","Account_Owner","Account_Id","Land","Stadt","Kandidat_Profil","Kandidat_Name",
       "Kandidat_UUID","Kandidat_Stadt","Kandidat_Status","Kandidat_Score","Kandidat_Reviews","Kandidat_Traffic",
       "Anzahl_freie_Kandidaten","Grund"]
write_sheet(ws2,matches,cols2)
cmap={"HOCH":green,"MITTEL":yellow,"NIEDRIG":red}
for i in range(2,len(matches)+2):
    ws2.cell(i,1).fill=cmap.get(ws2.cell(i,1).value, yellow)

ws3=wb.create_sheet("Summary")
summary=[
    ["KUNUNU PROFIL-MATCHING - ZUSAMMENFASSUNG",""],["",""],
    ["Ausgeschlossen","Accounts mit Tag 'Enterprise Account'/'DCA Account' + inaktive Accounts"],
    ["Doppelvergabe","Profile die bereits (irgendwo) auf einem Account liegen, werden NICHT erneut vorgeschlagen"],
    ["",""],
    ["TEIL A - KONTROLLE verknuepfter Accounts",""],
    ["  Kontrolliert (aktiv, verknuepft)", stat_ok+stat_better+stat_missing],
    ["  OK (bestes freies Profil bereits hinterlegt)", stat_ok],
    ["  BESSERES freies Profil verfuegbar (Handlungsbedarf)", stat_better],
    ["  Verknuepftes Profil nicht im Dump", stat_missing],
    ["  (Info) besseres Profil existiert, aber bereits belegt -> uebersprungen", stat_better_but_taken],
    ["",""],
    ["TEIL B - MATCHING unverknuepfter Accounts",""],
    ["  Kandidat HOCH (Name+Stadt+Land)", m_high],
    ["  Kandidat MITTEL (Name, Stadt unklar)", m_med],
    ["  Kandidat NIEDRIG (Fuzzy)", m_low],
    ["  Kein (freier) Treffer", m_none],
    ["  Owner 'Papierkorb' ausgelassen", m_papierkorb],
    ["",""],
    ["RANKING-LOGIK (bestes Profil)",""],
    ["  0. Passender Standort (Kontrolle)", "Profil-Stadt = Account-Stadt hat Vorrang (richtiger Standort bleibt)"],
    ["  1. Anzahl Bewertungen", "mehr = besser"],
    ["  2. Status", "EBP Pro > EBP > EBP Basic > Claimed > Not Claimed"],
    ["  3. kununu Score", "hoeher = besser"],
    ["  4. Traffic (Views total)", "hoeher = besser"],
    ["  Gruppierung Duplikate", "URL-Slug-Basis je Land (siemens, siemens1, siemens411 -> 'siemens')"],
]
for r in summary: ws3.append(r)
ws3.cell(1,1).font=Font(bold=True,size=14)
for rr in (6,13,20):
    ws3.cell(rr,1).font=Font(bold=True,color="1F4E78")
ws3.column_dimensions["A"].width=52; ws3.column_dimensions["B"].width=72

wb.save(OUT)
print(f"\nFERTIG -> {OUT}")
print(f"Kontrolle: {len(kontrolle):,} Auffaellige | Neue_Matches: {len(matches):,} Kandidaten")
