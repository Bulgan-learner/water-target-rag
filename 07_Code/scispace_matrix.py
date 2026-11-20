"""
scispace_matrix.py — produce a SciSpace-style water-target matrix, but GROUNDED:
every verbatim target quote is re-verified against the primary report by a
brute-force full-text scan, and stamped with the real PDF page (the one thing the
original SciSpace matrix lacked). Reproducible: versioned prompt + logged provenance.

Run:  PYTHONPATH=esg-rag  python -m research.scispace_matrix  [YEAR]   (default 2025)
"""
from __future__ import annotations

import sys
import re
import unicodedata
import datetime as _dt
import uuid
from difflib import SequenceMatcher
from typing import List, Optional

import anthropic
from pydantic import BaseModel, Field
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

import config
from src import db

PROMPT_VERSION = "scispace-matrix/v1.0"
MODEL = config.AGENT_MODEL
RETRIEVAL_K = 20
RETRIEVAL_QUERY = (
    # English
    '"water target" OR "water goal" OR "reduce freshwater" OR "water withdrawal" '
    'OR "water recycling" OR reuse OR "water efficiency" OR "water stewardship" '
    'OR "water intensity" OR "reduce water" OR "water consumption" OR seawater OR desalination '
    'OR "by 2030" OR "by 2025" OR "by 2035" OR "by 2040" OR target OR commitment OR baseline OR ICMM '
    # Spanish (Codelco, Minsur)
    'OR agua OR "hídrico" OR hidrico OR objetivo OR meta OR compromiso OR reducir OR "reducción" '
    'OR reduccion OR consumo OR "eficiencia hídrica" OR "reutilización" OR reutilizacion OR reciclaje '
    'OR "desalinización" OR desalinizacion OR "agua de mar" '
    # French (Orano)
    'OR eau OR hydrique OR objectif OR cible OR engagement OR "réduire" OR reduire OR "réduction" '
    'OR consommation OR "prélèvement" OR prelevement OR dessalement OR recyclage OR "eau de mer"'
)

SYSTEM_PROMPT = """You extract a structured water-target profile for one mining company's reporting year, \
working ONLY from the passages provided (each labelled "[page N]"). Do not use outside knowledge.

Return these fields:
- category: EXACTLY one of '1','2','3','4' per Kunz & Muram (2024):
  1 = a SINGLE explicit water target; 2 = MORE THAN ONE explicit water target;
  3 = no explicit target yet but the company explicitly PLANS to set one (or only site-specific/undisclosed targets);
  4 = no explicit water target, but broader water performance/management reporting.
  An explicit target has a metric and/or timeframe; a bare performance figure ("83% recycled") is NOT a target.
- number: "Single", "Multiple", or "None".
- explicit_targets: a list; for EACH target give the VERBATIM quote (copied exactly from a passage, no paraphrase) \
and the page number from its "[page N]" label. Empty list if category 4.
- approach: methodology / baseline / geographic scope, in one sentence.
- icmm_referenced: "Yes - <which ICMM framework>" or "No", based only on the passages.
- alternative_terms: words the company uses instead of "target" (goal, commitment, objective, ambition, aim...), or "".
- implied_targets: any multi-year trend or commitment that implies an internal target, else "".
- key_metrics: the salient reported water figures (withdrawal, consumption, recycling, intensity), or "".

Quote verbatim, cite pages from the labels, never invent."""


class Target(BaseModel):
    verbatim: str = Field(description="exact quote copied from a passage")
    page: Optional[int] = Field(default=None, description="page from the [page N] label")


class MatrixRow(BaseModel):
    category: str = Field(description="exactly one of '1','2','3','4'")
    number: str = "None"
    explicit_targets: List[Target] = Field(default_factory=list)
    approach: str = ""
    icmm_referenced: str = ""
    alternative_terms: str = ""
    implied_targets: str = ""
    key_metrics: str = ""


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = s.replace("³", "3").replace("²", "2")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9%. ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def verify_quote(cur, company: str, year: int, quote: str):
    """Brute-force scan the whole report for the quote. Returns (status, page, snippet)."""
    q = norm(quote)
    if len(q) < 12:
        return "TOO SHORT", None, ""
    cur.execute("select page, content from documents where company = %s and year = %s", (company, year))
    best = (0, None, "")
    for page, content in cur.fetchall():
        cn = norm(content)
        m = SequenceMatcher(None, q, cn, autojunk=False).find_longest_match(0, len(q), 0, len(cn))
        if m.size > best[0]:
            best = (m.size, page, content[max(0, m.b - 12): m.b + m.size + 12].replace("\n", " "))
    run = best[0]
    status = "CONFIRMED" if run >= 30 else ("PARTIAL" if run >= 18 else "NOT FOUND")
    return status, best[1], best[2]


def usage_cost(resp):
    u = getattr(resp, "usage", None)
    it = getattr(u, "input_tokens", 0) or 0
    ot = getattr(u, "output_tokens", 0) or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    return it, ot, config.estimate_cost_usd(MODEL, input_tokens=it, output_tokens=ot,
                                            cache_read_tokens=cr, cache_write_tokens=cw)


def main(year: int, limit: int = 0):
    sys.stdout.reconfigure(encoding="utf-8")
    run_id = f"scispace-{year}-{uuid.uuid4().hex[:8]}"
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # companies that actually have a report for this year
    with db.connect() as c0:
        c0.autocommit = True
        with c0.cursor() as cur0:
            cur0.execute("set statement_timeout='60s'")
            cur0.execute("select distinct company from esg_metrics where year=%s order by company", (year,))
            companies = [r[0] for r in cur0.fetchall()]
    if limit:
        companies = companies[:limit]
    print(f"{year}: {len(companies)} companies\n")

    vconn = db.connect(); vconn.autocommit = True
    vcur = vconn.cursor(); vcur.execute("set statement_timeout='120s'")

    rows, prov, total_cost = [], [], 0.0
    for comp in companies:
        passages = db.search_documents(RETRIEVAL_QUERY, company=comp, year=year, limit=RETRIEVAL_K)
        if not passages:
            rows.append([comp, year, "(no passages retrieved)", "None", "", "", "", "", "", "4", "", ""])
            continue
        files = [p.get("source_file") for p in passages if p.get("source_file")]
        src = max(set(files), key=files.count) if files else ""
        blocks = [f"[page {p.get('page')}] {(p.get('content') or '').strip().replace(chr(10),' ')[:650]}"
                  for p in passages]
        user = (f"Company: {comp} — {year} report (file: {src}).\nPassages:\n\n"
                + "\n\n".join(blocks) + f"\n\nExtract {comp}'s {year} water-target profile.")
        resp = client.messages.parse(
            model=MODEL, max_tokens=3000,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=MatrixRow,
        )
        it, ot, cost = usage_cost(resp); total_cost += cost
        r: MatrixRow = resp.parsed_output or MatrixRow(category="4")

        # verify each verbatim target against the real report, stamp the true page
        targ_cells, vpages, checks = [], [], []
        for t in r.explicit_targets:
            status, vpage, _ = verify_quote(vcur, comp, year, t.verbatim)
            page = vpage if vpage is not None else t.page
            targ_cells.append(f'"{t.verbatim}" (p.{page})' if page else f'"{t.verbatim}"')
            if page is not None:
                vpages.append(page)
            checks.append(status)
        target_text = "\n".join(targ_cells) if targ_cells else "No explicit numerical water target stated"
        check = ", ".join(f"{c}:{p}" for c, p in zip(checks, vpages)) if checks else "n/a"

        rows.append([comp, year, target_text, r.number, r.approach, r.icmm_referenced,
                     r.alternative_terms, r.implied_targets, r.key_metrics, r.category,
                     ", ".join(str(p) for p in vpages), check])
        prov.append([comp, src, r.category, MODEL, PROMPT_VERSION, len(passages), it, ot, round(cost, 5), run_id])
        print(f"{comp:<24} cat {r.category}  targets {len(r.explicit_targets):<2} checks[{check}]")

    # ---- write workbook ----
    out = fr"C:\Users\bulga\Downloads\Water Research X article\02_Analysis_evidence\water_target_matrix_{year}_grounded.xlsx"
    wb = Workbook(); ws = wb.active; ws.title = f"Water target {year}"
    hdr = ["Company", "Report Year", "Explicit Target(s) [verbatim + page]", "Number", "Approach",
           "ICMM Referenced", "Alternative Terms", "Implied Targets", "Key Metrics", "Category",
           "Verified page(s)", "Quote check (scan)"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="2E5A88")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append(row)
    widths = [22, 8, 60, 10, 34, 26, 20, 30, 34, 9, 14, 26]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "C2"

    ps = wb.create_sheet("Provenance")
    ps.append(["Company", "Source file", "Category", "Model", "Prompt version", "Passages",
               "Input tokens", "Output tokens", "Cost USD", "Run id"])
    for p in prov:
        ps.append(p)
    ps.append([]); ps.append([f"TOTAL cost USD: {round(total_cost,4)}", "", "", "", "", "", "", "", "", run_id])
    wb.save(out)
    print(f"\nSaved: {out}\nTotal cost: ${round(total_cost,4)}  run_id={run_id}")


if __name__ == "__main__":
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    main(yr, lim)
