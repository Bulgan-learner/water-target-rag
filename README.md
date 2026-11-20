# Water target disclosure: retrieval-augmented coding of mining company reports

Materials for the paper on reading corporate water-target disclosures with a
retrieval-augmented language-model pipeline, validated against an expert benchmark
(Kunz & Muram, 2024). This repository holds the paper's pipeline output and the code and
settings needed to repeat the experiments.

## Headline results

| Measure | Value |
|---|---|
| Corpus | 47 reports; 25 companies in 2022, 22 in 2025, 22 paired |
| Agreement with expert coding as recorded | 12 of 25 (48.0%), kappa 0.334 |
| Agreement after adjudication | 21 of 25 (84.0%), kappa 0.781, 95% CI 0.58 to 0.94 |
| Collapsed to target / plan / neither | 22 of 25 (88.0%), kappa 0.795 |
| Quotations found word for word in the cited report | 53 of 56 (3 partial, 0 not found) |
| Category agreement between passes | 80.9% identical in all three; 83.0 to 89.4% pairwise |

Adjudication was not blind and was done by the team that produced the benchmark.

## What is here

| Path | Contents |
|---|---|
| `03_Machine_coding_v1.3_AUTHORITATIVE/harmonised_codings_2022_2025.jsonl` | Main pass: category, verbatim target, page, baseline year and quote check for all 47 company-years. Every reported figure derives from this file |
| `..._run_A.jsonl`, `..._run_B.jsonl` | The two repeat passes under identical settings |
| `orientations_settled_2026-08-05.jsonl` | Orientation labels after hand-coding and adjudication |
| `04_Reliability/noise_floor_3runs_2026-07-29.txt` | Main pass against the two repeats |
| `07_Code/water_target_coder.py` | Retrieval query, system prompt, output schema, model and prompt version |
| `07_Code/harmonise_timepoints.py` | Codes every company-year under one frozen configuration and checks each quotation |
| `07_Code/scispace_matrix.py` | Quotation verification (`verify_quote`) and cost accounting used by the above |
| `07_Code/agreement_stats.py` | Percentage agreement, Cohen's kappa and Krippendorff's alpha against the benchmark, with the adjudication rulings |

## How to repeat the experiments

Frozen settings: retrieval config v1.3 (multilingual, multi-query, includes community and
potable-water terms, 36-passage cap), prompt `water-target-coder/v1.1`, model
`claude-opus-4-8`, two timepoints (2022 and 2025).

1. **Collect the reports.** Download the 47 flagship reports from the web addresses in the
   paper's Supplementary Table S4. The reports are the companies' copyright and are not
   redistributed here.
2. **Index them by page.** Extract text from each PDF keeping the page as the retrieval
   unit, and load it into a PostgreSQL full-text index. There is no vector store or
   embedding service.
3. **Set up the environment.** The scripts import `config` and `src.db` from the author's
   `esg-rag` project, which holds the database access and the API settings. Those modules
   are not part of this repository, so supply your own equivalents. Set an Anthropic API
   key, and set `ESG_RAG_OUTPUT_DIR` to a folder for results. Several scripts also contain
   original Windows paths that need editing.
4. **Run the coding.** From the project root, with the `esg-rag` folder on `PYTHONPATH`:
   `python -m research.harmonise_timepoints`. Run it three times to get the main pass and
   two repeats. Each company-year gets one model call, followed by an automated check that
   every returned quotation appears in the full report text.
5. **Compare with the benchmark.** `python -m research.agreement_stats` reads the main-pass
   file and reports agreement and kappa for the 25 companies with an expert reading
   (Kunz & Muram, 2024; MMG from the authors' working records).
6. **Measure reproducibility.** Compare category by company-year across the three passes.
   `04_Reliability/noise_floor_3runs_2026-07-29.txt` shows the expected result.

Model output varies a little between passes even at identical settings, so repeats will
not match the stored files exactly. That variation is itself a result of the paper, which
reports ranges rather than single numbers.

## Citation

Add the paper reference and DOI here once published.
