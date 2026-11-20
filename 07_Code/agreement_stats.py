# -*- coding: utf-8 -*-
"""
agreement_stats.py — chance-corrected agreement between the manual benchmark
(Kunz & Muram, 2024) and the author-verified RAG codings, 2022 (25 companies).

Reports, for (a) the strict 4-category rubric and (b) the substantive collapsed
rubric (explicit target present [1+2] / plan [3] / none [4]):
  - raw percentage agreement
  - Cohen's kappa (two raters)
  - Krippendorff's alpha (nominal)

Dual manual listing (Minera San Cristobal "1 & 4"): scored as agreement when the
RAG category falls within the manual set; for the contingency tables the manual
label is resolved to the matching member (stated in Methods).

MMG is the one company whose manual category is not drawn from the published
Kunz & Muram table -- it comes from the authors' working records (see PAIRS
below). The manuscript states this; the figures here include it, so they match
the paper's 25-company abstract, section 4.3 and Table 4.

Run:  PYTHONPATH=esg-rag  python -m research.agreement_stats
"""
from __future__ import annotations

import sys
import os
import json
from pathlib import Path
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

# (company, manual, rag_legacy)
#   manual     — Kunz & Muram (2024) Table 1, except MMG (see the entry below).
#   rag_legacy — the 2022 verification pass, kept for provenance only; it is
#                replaced by the harmonised v1.3 codings a few lines down unless
#                ESG_RAG_USE_LEGACY_PAIRS=1.
# These were transcribed from verification_2022_workbook.xlsx, which is no longer
# on disk. Its surviving derivative for the 24 K&M companies is
# 02_Analysis_evidence/verification_master_2022_all_sources.xlsx (same rows, same
# order, columns "Nadja & Shahla (K&M 2024 published)" and "RAG (raw)").
PAIRS = [
    ("African Rainbow Minerals", "1", "1"), ("Alcoa", "1", "1"), ("Anglo American", "1", "1"),
    ("AngloGold Ashanti", "2", "3"), ("Antofagasta", "1", "1"), ("Barrick", "4", "4"),
    ("BHP", "1", "1"), ("Boliden", "4", "4"), ("Codelco", "1", "1"),
    ("Freeport-McMoRan", "3", "4"), ("Glencore", "3", "1"), ("Gold Fields", "2", "2"),
    ("Norsk Hydro", "4", "4"), ("Minera San Cristobal", "1 & 4", "4"), ("Minsur", "2", "1"),
    # MMG is absent from the published K&M table. Its manual category comes from the
    # authors' working records -- 02_Analysis_evidence/"ICMM members water target
    # comparison_Manual_Shahla.xlsx", sheet "ICMM member analysis 2022": "Not
    # specifically mentioned about water target" -> 4. Restored to the study
    # 2026-07-31; v1.3 also codes it 4, so it needs no adjudication entry.
    ("MMG", "4", "4"),
    ("Newcrest", "1", "1"), ("Newmont", "1", "2"), ("Orano", "1", "2"),
    ("Rio Tinto", "1", "2"), ("Sibanye-Stillwater", "1", "2"), ("South32", "1", "2"),
    ("Sumitomo Metal Mining", "1", "4"), ("Teck", "1", "2"), ("Vale", "1", "1"),
]

# Author adjudication, revised 2026-07-24 after a full re-check, in two passes,
# against what was then verification_2022_workbook.xlsx (no longer on disk -- see
# the provenance note above PAIRS). MMG is not adjudicated: manual and v1.3 agree.
ADJUDICATED = {
    # Pass 1: the five originally flagged interpretive cases, plus a redefinition of
    # category 3 (no explicit target, but a stated intention/mandate/timeline to set one).
    "AngloGold Ashanti": "4",      # standard states an objective; no timeline/metric attached
    "Freeport-McMoRan": "3",       # dated (2023) review process toward a future target
    "Glencore": "3",               # "by 2023 ... setting local targets" -- dated stated intention
    "Minsur": "4",                 # "2030 target: to be defined" -- unattached placeholder, no process
    "Sumitomo Metal Mining": "4",  # p.39 KPI carries no metric/timeframe of its own
    # Pass 2: the six single-vs-multiple boundary cases, re-read against the primary quotes
    # and ruled for RAG's category 2.
    "Newmont": "2",                # two distinct site-group commitments (>5% / >=10%)
    "Orano": "2",                  # two separate dated reduction targets
    "Rio Tinto": "2",
    "Sibanye-Stillwater": "2",     # same source document; RAG found additional target text
    "South32": "2",                # dated efficiency target + separate community-access target
    "Teck": "2",                   # same source document; manual reading missed two dated targets
}


# ---------------------------------------------------------------------------
# The RAG column in PAIRS above is retained for provenance only: it records the
# 2022 verification pass, which ran under the configuration preceding v1.3.
# The authoritative RAG categories are those of the harmonised v1.3 run, and we
# load them here so that this script and the manuscript cannot drift apart.
# Reconciled 2026-07-31 after the two sources were found to differ on 8 of the 25
# companies. Set ESG_RAG_USE_LEGACY_PAIRS=1 to reproduce the older figures.
# ---------------------------------------------------------------------------
_V13 = Path(os.getenv("ESG_RAG_OUTPUT_DIR",
                      r"C:\Users\bulga\Downloads\Water Research X article"
                      r"\02_Analysis_evidence")) / "harmonised_codings_2022_2025.jsonl"

if not os.getenv("ESG_RAG_USE_LEGACY_PAIRS"):
    _rag2022 = {}
    with open(_V13, encoding="utf-8") as _fh:
        for _line in _fh:
            _r = json.loads(_line)
            if _r["year"] == 2022:
                _rag2022[_r["company"]] = _r["category"]
    _missing = [c for c, _, _ in PAIRS if c not in _rag2022]
    if _missing:
        raise SystemExit(f"no v1.3 coding for: {_missing}")
    PAIRS = [(c, m, _rag2022[c]) for c, m, _ in PAIRS]


def resolve(man: str, rag: str) -> str:
    """Resolve a dual manual listing to the member matching RAG (else first listed)."""
    s = [x.strip() for x in man.split("&")]
    return rag if rag in s else s[0]


def cohen_kappa(pairs: list[tuple[str, str]]) -> tuple[float, float, float]:
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    ma, mb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    cats = set(ma) | set(mb)
    pe = sum(ma[c] * mb[c] for c in cats) / (n * n)
    return po, pe, (po - pe) / (1 - pe)


def kripp_alpha_nominal(pairs: list[tuple[str, str]]) -> float:
    values = [v for p in pairs for v in p]
    n = len(values)
    nc = Counter(values)
    disagree_pairs = sum(2 for a, b in pairs if a != b)  # o(c,k)+o(k,c)
    do = disagree_pairs / n
    de = (n * n - sum(v * v for v in nc.values())) / (n * (n - 1))
    return 1 - do / de


def landis_koch(k: float) -> str:
    for hi, lbl in [(0.0, "poor"), (0.20, "slight"), (0.40, "fair"),
                    (0.60, "moderate"), (0.80, "substantial"), (1.01, "almost perfect")]:
        if k <= hi:
            return lbl
    return "almost perfect"


def report(title: str, pairs: list[tuple[str, str]]) -> dict:
    po, pe, k = cohen_kappa(pairs)
    a = kripp_alpha_nominal(pairs)
    print(f"\n== {title} ==")
    print(f"  raw agreement : {sum(x == y for x, y in pairs)}/{len(pairs)} = {po:.3f}")
    print(f"  Cohen's kappa : {k:.3f}  (expected-by-chance {pe:.3f}; {landis_koch(k)})")
    print(f"  Kripp. alpha  : {a:.3f}  (nominal)")
    return {"po": po, "kappa": k, "alpha": a}


def main():
    collapse = {"1": "T", "2": "T", "3": "3", "4": "4"}

    # Layer 1: original published benchmark (Kunz & Muram 2024 Table 1) vs verified RAG
    strict = [(resolve(m, r), r) for _, m, r in PAIRS]
    report("A. Original K&M benchmark — strict 4-category", strict)
    report("A. Original K&M benchmark — collapsed target(1+2)/plan(3)/none(4)",
           [(collapse[a], collapse[b]) for a, b in strict])

    # Layer 2: author-adjudicated benchmark (11 rulings applied) vs verified RAG
    adj_pairs = [(c, ADJUDICATED.get(c, m), r) for c, m, r in PAIRS]
    strict_adj = [(resolve(m, r), r) for _, m, r in adj_pairs]
    report("B. ADJUDICATED benchmark — strict 4-category", strict_adj)
    report("B. ADJUDICATED benchmark — collapsed target(1+2)/plan(3)/none(4)",
           [(collapse[a], collapse[b]) for a, b in strict_adj])

    print("\nAdjudications applied:", ", ".join(f"{k} {dict(( (c,m) for c,m,_ in PAIRS ))[k]}→{v}"
                                                for k, v in ADJUDICATED.items()))
    print(f"All {len(ADJUDICATED)} adjudicated cases resolved; no open flags.")


if __name__ == "__main__":
    main()
