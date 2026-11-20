"""
harmonise_timepoints.py — code every listed timepoint under one identical configuration.

A multi-timepoint comparison is only valid if an observed difference can be attributed to
disclosure practice rather than to instrument behaviour. This script therefore re-codes
every company-report at every timepoint in TIMEPOINTS with a single frozen configuration
(retrieval v1.3 multilingual multi-query incl. community/potable-water terms, cap 36
passages; prompt water-target-coder/v1.1, redefined category 3, baseline_year field),
and mechanically verifies every quotation against the full report text.

TIMEPOINTS below is still [2022, 2025], matching the paper's settled two-timepoint design.
Adding 2024 (or any other year) is a one-line change -- deliberately not done here without
an explicit decision, since the two-timepoint design was chosen and reaffirmed specifically
because 2022 and 2025 are the only years with matched verification rigor; extending scope
is a scope decision, not a default.

Run:  PYTHONPATH=esg-rag  python -m research.harmonise_timepoints
"""
from __future__ import annotations

import os
import sys
import json
import uuid
import datetime as _dt
from pathlib import Path
from collections import Counter

import anthropic
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

import config
from src import db
from research.water_target_coder import Coding, SYSTEM_PROMPT, ORIENTATIONS, MODEL
from research.scispace_matrix import verify_quote, usage_cost

OUT_DIR = Path(os.getenv("ESG_RAG_OUTPUT_DIR",
                         r"C:\Users\bulga\Downloads\Water Research X article\02_Analysis_evidence"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_ID = "harmonised/v1.3-multilingual-multiquery-community"
TIMEPOINTS = [2022, 2025]
CAP = 36

SUBQUERIES = [
    '"reduce" OR "reduction" OR "withdrawal" OR "consumption" OR "freshwater" OR "fresh water" '
    'OR "by 2030" OR "by 2035" OR "by 2040" OR "% by"',
    '"seawater" OR "sea water" OR "desalination" OR "desalinated" OR "brackish" OR "alternative '
    'water" OR "recycled water" OR "source substitution" OR "transition"',
    '"water efficiency" OR "water intensity" OR "water use efficiency" OR "recycling" OR "reuse" '
    'OR "kL" OR "m3 per" OR "per tonne" OR "intensity target"',
    '"water target" OR "water goal" OR target OR commitment OR objective OR ambition '
    'OR "context-based" OR "catchment" OR "site-specific" OR stewardship OR baseline OR ICMM',
    'agua OR "hídrico" OR hidrico OR objetivo OR meta OR compromiso OR reducir OR "reducción" '
    'OR consumo OR "reutilización" OR reciclaje OR "agua de mar" OR "desalinización" '
    'OR eau OR hydrique OR objectif OR cible OR engagement OR "réduire" OR "réduction" '
    'OR consommation OR dessalement OR "eau de mer"',
    # Community / potable water access (added 2026-07-25 -- closes the retrieval gap found
    # 2026-07-24; see community_water_access_keyword_scan_2022.xlsx, 207 hits / 20 companies)
    '"potable" OR "drinking water" OR "community water" OR "water access" OR "clean water" '
    'OR "sanitation and hygiene" OR "safe water" OR "agua potable" OR "acceso al agua" '
    'OR "eau potable" OR "accès à l\'eau"',
]


def retrieve(company: str, year: int):
    seen = {}
    for q in SUBQUERIES:
        for h in db.search_documents(q, company=company, year=year, limit=10):
            key = (h.get("page"), (h.get("content") or "")[:80])
            prev = seen.get(key)
            if prev is None or (h.get("rank") or 0) > (prev.get("rank") or 0):
                seen[key] = h
    return sorted(seen.values(), key=lambda p: p.get("rank") or 0, reverse=True)[:CAP]


def code_one(client, vcur, company: str, year: int, run_id: str):
    passages = retrieve(company, year)
    if not passages:
        return None
    files = [p.get("source_file") for p in passages if p.get("source_file")]
    src = max(set(files), key=files.count) if files else ""
    blocks = [f"[page {p.get('page')}] {(p.get('content') or '').strip().replace(chr(10),' ')[:650]}"
              for p in passages]
    user = (f"Company: {company} — {year} report (file: {src}).\n"
            f"Passages retrieved from the report:\n\n" + "\n\n".join(blocks) +
            f"\n\nClassify {company}'s {year} water target(s) per the rubric.")
    resp = client.messages.parse(
        model=MODEL, max_tokens=2000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=Coding,
    )
    it, ot, cost = usage_cost(resp)
    c: Coding = resp.parsed_output or Coding(has_explicit_target=False, category="", rationale="parse failed")
    orient = [o for o in c.orientations if o in ORIENTATIONS] or (["none"] if c.category == "4" else [])

    checks, pages = [], []
    for q in c.verbatim_targets:
        status, vpage, _ = verify_quote(vcur, company, year, q)
        checks.append(status)
        if vpage is not None:
            pages.append(vpage)
    return {
        "company": company, "year": year, "category": c.category, "orientations": orient,
        "verbatim_targets": c.verbatim_targets, "verified_pages": pages,
        "baseline_years": c.baseline_years, "baseline_stated": c.baseline_stated,
        "quote_checks": checks, "confidence": c.confidence, "rationale": c.rationale,
        "source_file": src, "n_passages": len(passages), "config": CONFIG_ID, "model": MODEL,
        "input_tokens": it, "output_tokens": ot, "cost_usd": round(cost, 5), "run_id": run_id,
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    run_id = f"harmonised-{uuid.uuid4().hex[:8]}"
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    vconn = db.connect(); vconn.autocommit = True
    vcur = vconn.cursor(); vcur.execute("set statement_timeout='120s'")

    # companies actually holding a report at each timepoint
    todo = []
    with db.connect() as c0:
        c0.autocommit = True
        with c0.cursor() as cur0:
            cur0.execute("set statement_timeout='60s'")
            for yr in TIMEPOINTS:
                cur0.execute("select distinct company from esg_metrics where year=%s order by company", (yr,))
                for (comp,) in cur0.fetchall():
                    todo.append((comp, yr))
    print(f"{CONFIG_ID}\ncoding {len(todo)} company-reports across {TIMEPOINTS}\n")

    records, total = [], 0.0
    print(f"{'company':<24}{'yr':<6}{'cat':<5}{'targets':<9}checks")
    print("-" * 74)
    for comp, yr in todo:
        rec = code_one(client, vcur, comp, yr, run_id)
        if not rec:
            print(f"{comp:<24}{yr:<6}(no passages retrieved)"); continue
        records.append(rec); total += rec["cost_usd"]
        chk = ",".join(rec["quote_checks"]) or "n/a"
        print(f"{comp:<24}{yr:<6}{rec['category']:<5}{len(rec['verbatim_targets']):<9}{chk[:34]}")

    # ---- provenance record (jsonl) ----
    jl = OUT_DIR / "harmonised_codings_2022_2025.jsonl"
    with open(jl, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- workbook ----
    wb = Workbook(); ws = wb.active; ws.title = "Harmonised codings"
    ws.append(["Company", "Timepoint", "Category", "Orientations", "Verbatim target(s)",
               "Baseline year(s)", "Baseline stated?", "Verified page(s)", "Quote check",
               "Confidence", "Rationale", "Source file"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="2E5A88")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in sorted(records, key=lambda x: (x["company"], x["year"])):
        ws.append([r["company"], r["year"], r["category"], "; ".join(r["orientations"]),
                   "\n".join(r["verbatim_targets"]),
                   ", ".join(str(y) for y in r.get("baseline_years", [])),
                   "yes" if r.get("baseline_stated") else "no",
                   ", ".join(str(p) for p in r["verified_pages"]),
                   ", ".join(r["quote_checks"]), r["confidence"], r["rationale"], r["source_file"]])
    for i, w in enumerate([22, 10, 9, 30, 60, 14, 13, 14, 20, 11, 50, 34]):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row: c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "C2"

    # ---- comparison sheet: paired panel ----
    by = {(r["company"], r["year"]): r for r in records}
    comps22 = {c for (c, y) in by if y == 2022}
    comps25 = {c for (c, y) in by if y == 2025}
    paired = sorted(comps22 & comps25)
    s2 = wb.create_sheet("2022 vs 2025")
    s2.append(["Company", "2022 category", "2025 category", "Change",
               "2022 orientations", "2025 orientations"])
    for c in s2[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="2E5A88")
    trans = Counter(); stable = 0
    for comp in paired:
        a, b = by[(comp, 2022)]["category"], by[(comp, 2025)]["category"]
        trans[(a, b)] += 1; stable += (a == b)
        s2.append([comp, a, b, "same" if a == b else f"{a}->{b}",
                   "; ".join(by[(comp, 2022)]["orientations"]),
                   "; ".join(by[(comp, 2025)]["orientations"])])
    for i, w in enumerate([22, 12, 12, 10, 30, 30]):
        s2.column_dimensions[chr(65 + i)].width = w
    s2.freeze_panes = "B2"

    d22 = Counter(by[(c, 2022)]["category"] for c in paired)
    d25 = Counter(by[(c, 2025)]["category"] for c in paired)
    o22, o25 = Counter(), Counter()
    for c in paired:
        for o in by[(c, 2022)]["orientations"]: o22[o] += 1
        for o in by[(c, 2025)]["orientations"]: o25[o] += 1

    s3 = wb.create_sheet("Distributions")
    s3.append(["Measure", "2022", "2025"])
    for cat in ["1", "2", "3", "4"]:
        s3.append([f"Category {cat}", d22.get(cat, 0), d25.get(cat, 0)])
    s3.append([])
    for o in ORIENTATIONS:
        s3.append([f"Orientation: {o}", o22.get(o, 0), o25.get(o, 0)])
    s3.append([]); s3.append([f"Paired companies", len(paired), len(paired)])
    s3.append([f"Stable categories", stable, ""])
    s3.column_dimensions["A"].width = 40

    p = wb.create_sheet("Provenance")
    p.append(["Config", CONFIG_ID]); p.append(["Model", MODEL])
    p.append(["Retrieval", f"{len(SUBQUERIES)} sub-queries, union, cap {CAP}"])
    p.append(["Timepoints", ", ".join(str(t) for t in TIMEPOINTS)])
    p.append(["Company-reports coded", len(records)])
    p.append(["Total cost USD", round(total, 4)]); p.append(["Run id", run_id])
    p.column_dimensions["A"].width = 26; p.column_dimensions["B"].width = 60

    out = OUT_DIR / "harmonised_2022_2025.xlsx"
    try:
        wb.save(out); dest = out
    except PermissionError:
        dest = OUT_DIR / "harmonised_2022_2025_new.xlsx"; wb.save(dest)
    print(f"\nSaved: {dest}\nProvenance: {jl}")
    print(f"paired companies: {len(paired)} | stable: {stable}/{len(paired)}")
    print("2022 dist:", dict(sorted(d22.items())), " 2025 dist:", dict(sorted(d25.items())))
    print(f"Total cost: ${round(total,4)}  run_id={run_id}")


if __name__ == "__main__":
    main()
