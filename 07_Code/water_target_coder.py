"""
water_target_coder.py — reproducible RAG coder for mining-company water targets.

Pipeline per (company, year):
    retrieve (Postgres FTS over report chunks)  ->  classify (Claude, structured)
    ->  provenance record (category, orientation, verbatim, page citations, model,
        prompt version, retrieval config, tokens, cost, run id, timestamp).

Everything that affects the result is versioned and recorded, so a run is fully
reproducible and each classification is auditable to a page.
"""
from __future__ import annotations

import datetime as _dt
from typing import List

import anthropic
from pydantic import BaseModel, Field

import config
from src import db

# --- Versioned, documented configuration (change bumps the version) ----------
# v1.1 (2026-07-25): added community/potable-water retrieval terms (closes a retrieval
# gap found 2026-07-24 -- 207 keyword hits across 20/25 companies were never reaching the
# classifier); added baseline_year extraction; redefined category 3 (see SYSTEM_PROMPT).
PROMPT_VERSION = "water-target-coder/v1.1"
MODEL = config.AGENT_MODEL  # e.g. claude-opus-4-8
RETRIEVAL_K = 14
RETRIEVAL_QUERY = (
    # English
    '"water target" OR "water goal" OR "reduce freshwater" OR "water withdrawal" '
    'OR "water recycling" OR reuse OR "water efficiency" OR "water stewardship" '
    'OR "water intensity" OR "reduce water" OR "water consumption" OR seawater OR desalination '
    'OR "by 2030" OR "by 2025" OR "by 2035" OR "by 2040" OR target OR commitment OR baseline OR ICMM '
    # Community / potable water access (added 2026-07-25 -- previously missing entirely)
    'OR potable OR "drinking water" OR "community water" OR "water access" OR "clean water" '
    'OR "sanitation and hygiene" OR "safe water" '
    # Spanish (Codelco, Minsur)
    'OR agua OR "hídrico" OR hidrico OR objetivo OR meta OR compromiso OR reducir OR "reducción" '
    'OR reduccion OR consumo OR "eficiencia hídrica" OR "reutilización" OR reutilizacion OR reciclaje '
    'OR "desalinización" OR desalinizacion OR "agua de mar" OR "agua potable" OR "acceso al agua" '
    # French (Orano)
    'OR eau OR hydrique OR objectif OR cible OR engagement OR "réduire" OR reduire OR "réduction" '
    'OR consommation OR "prélèvement" OR prelevement OR dessalement OR recyclage OR "eau de mer" '
    'OR "eau potable" OR "accès à l\'eau"'
)

# Controlled vocabulary for target orientation (tests the Kunz & Muram thesis:
# efficiency/withdrawal dominance vs. broader stewardship).
ORIENTATIONS = [
    "withdrawal_reduction", "efficiency", "recycling_reuse", "source_substitution",
    "water_quality", "discharge_reduction", "stewardship_catchment_context",
    "community_access", "none",
]

SYSTEM_PROMPT = f"""You apply a FIXED human coding scheme (Kunz & Muram, 2024) to a mining company's WATER \
TARGET(S) for one reporting year, working ONLY from the passages provided (each labelled with its PDF page).

CATEGORY — assign EXACTLY ONE:
1 = a SINGLE explicit water target.
2 = MORE THAN ONE explicit water target.
3 = no explicit target, but the company has stated an INTENTION, MANDATE, or TIMELINE to set one \
(e.g. a dated commitment to "finalise the assessment and set local targets by 2023", or a named future \
target slot such as "2030 target: to be defined" WHEN accompanied by a stated process or date for setting \
it). A general water-management standard or policy with no stated timeline to set a target, or an \
unattached placeholder with no process or date, is category 4, not 3.
4 = no explicit water target and no stated intention to set one; the company reports water \
performance/management more broadly.
An "explicit target" has a metric and/or timeframe (e.g. "reduce X by Y% by 20ZZ"); a bare performance \
figure (e.g. "83% recycled") is NOT a target. Milestones of one target are still category 1 unless there are \
genuinely distinct separate targets (then 2).

ORIENTATION — tag ALL that apply, using ONLY this controlled vocabulary (use ["none"] for category 4 with no \
orientation):
- withdrawal_reduction  : reduce water withdrawal/abstraction
- efficiency            : water-use efficiency / intensity per unit of product or ore
- recycling_reuse       : increase recycling / reuse
- source_substitution   : shift to seawater / brackish / other alternative sources
- water_quality         : water-quality targets (contaminants, discharge quality)
- discharge_reduction   : reduce discharge volume / hazardous substances to water
- stewardship_catchment_context : catchment / context-based / site-specific stewardship targets
- community_access      : community / Indigenous-rightsholder water access, potable/drinking water \
projects, WASH (water, sanitation and hygiene) initiatives
- none

BASELINE YEAR — for each explicit target, if the passage states the year the target is measured against \
(e.g. "10% reduction against a 2019 baseline", "from FY2017 levels"), extract that year. If a baseline is \
mentioned but no year is given in the passage, leave the year empty but set baseline_stated=true. If no \
baseline is mentioned at all, set baseline_stated=false.

Rules: quote each target VERBATIM from the passages (do not paraphrase or invent); cite the page number(s) \
from the "[page N]" labels; set confidence high/medium/low based on how clearly the passages state the target."""


class Coding(BaseModel):
    has_explicit_target: bool
    category: str = Field(description="exactly one of '1','2','3','4'")
    orientations: List[str] = Field(default_factory=list, description="from the controlled vocabulary only")
    verbatim_targets: List[str] = Field(default_factory=list)
    source_pages: List[int] = Field(default_factory=list)
    baseline_years: List[int] = Field(default_factory=list,
        description="stated baseline year per target, aligned by position with verbatim_targets; "
                    "omit an entry (leave shorter than verbatim_targets) where no year is given")
    baseline_stated: bool = Field(default=False,
        description="true if any target mentions a baseline at all, even without a year")
    confidence: str = "medium"
    rationale: str = ""


def _usage_cost(resp) -> tuple[int, int, float]:
    u = getattr(resp, "usage", None)
    it = getattr(u, "input_tokens", 0) or 0
    ot = getattr(u, "output_tokens", 0) or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    cost = config.estimate_cost_usd(MODEL, input_tokens=it, output_tokens=ot,
                                    cache_read_tokens=cr, cache_write_tokens=cw)
    return it + cw + cr, ot, cost


def code_company_year(client, company: str, year: int, run_id: str) -> dict:
    """Retrieve + classify one (company, year). Returns a full provenance record."""
    passages = db.search_documents(RETRIEVAL_QUERY, company=company, year=year, limit=RETRIEVAL_K)
    base = {
        "company": company, "year": year,
        "model": MODEL, "prompt_version": PROMPT_VERSION,
        "retrieval_query": RETRIEVAL_QUERY, "retrieval_k": RETRIEVAL_K,
        "run_id": run_id, "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }
    if not passages:
        base.update({"n_passages": 0, "category": "", "orientations": [], "verbatim_targets": [],
                     "source_file": "", "source_pages": [], "citation": "", "confidence": "",
                     "rationale": "no passages retrieved", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        return base

    files = [p.get("source_file") for p in passages if p.get("source_file")]
    src = max(set(files), key=files.count) if files else ""
    blocks = [f"[page {p.get('page')}] {(p.get('content') or '').strip().replace(chr(10), ' ')[:650]}"
              for p in passages]
    user = (f"Company: {company} — {year} report (file: {src}).\n"
            f"Passages retrieved from the report:\n\n" + "\n\n".join(blocks) +
            f"\n\nClassify {company}'s {year} water target(s) per the rubric.")

    resp = client.messages.parse(
        model=MODEL,
        max_tokens=1500,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=Coding,
    )
    it, ot, cost = _usage_cost(resp)
    c: Coding = resp.parsed_output or Coding(has_explicit_target=False, category="", confidence="",
                                             rationale="parse returned no output")
    orient = [o for o in c.orientations if o in ORIENTATIONS] or (["none"] if c.category == "4" else [])
    cite = f"{src} p.{', '.join(str(x) for x in c.source_pages)}" if c.source_pages else src
    base.update({
        "n_passages": len(passages), "category": c.category, "orientations": orient,
        "verbatim_targets": c.verbatim_targets, "source_file": src, "source_pages": c.source_pages,
        "baseline_years": c.baseline_years, "baseline_stated": c.baseline_stated,
        "citation": cite, "confidence": c.confidence, "rationale": c.rationale,
        "input_tokens": it, "output_tokens": ot, "cost_usd": round(cost, 5),
    })
    return base
