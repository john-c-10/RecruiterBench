# Artifact formats

All hashes are SHA-256 over canonical JSON: sorted keys, compact separators, Unicode preserved, and non-finite numbers rejected. JSONL files contain one complete object per line. IDs are filesystem-safe identifiers; all paths in the role index are relative to their dataset directory.

## Public corpus

| Artifact | Important fields |
| --- | --- |
| `manifest.json` | `dataset_id`, `as_of`, role/category counts, category names, `selection_sha256`, upstream reference commit, source notice, and `interpretation_policy` |
| `roles.jsonl` | `role_id`, category, company, title, job URL, location/workplace metadata, publication date, snapshot path/hash, rubric path |
| `snapshots/<role_id>.json` | Original `job` object including `descriptionHtml` and `descriptionPlain`; source URLs, verification/import times, and original page check |
| `rubrics/<role_id>.json` | Rubric, rubric hash, source snapshot hash, extraction input hash, prompt/schema/model provenance, optional correction record, `interpretation`, and `experience_context` |
| `roles-with-rubrics.jsonl` | Joined role metadata + plain job text + source + effective rubric + extraction/correction/interpretation provenance + experience context + actual `benchmark_query` |
| `rubrics-manifest.json` | The effective hash of every role's rubric, frozen after initial curation |
| `review-notes.json` | Validation counts, ambiguities, and requirements that may need candidate confirmation |
| `interpretation-changes.json` | User instruction, policy, rubric hashes, changed fields, and per-role decisions |
| `clarification-resolutions.json` | All 32 original source questions/context and their resolved decisions, effective rubric hashes, and relative snapshot paths |

A rubric contains `role_summary`, the extractor's draft `search_query`, `must_haves`, `red_flags`, `preferences`, `location`, `soft_requirements`, and `ambiguities`. Criteria include a sequential ID (`M1`, `D1`, `P1`), requirement, verbatim source quote, and `verification=profile|candidate_confirmation`. Location contains explicit required/optional status, scope, alternatives, workplace type, relocation status, current-residence requirement, and supporting quotes. See the strict [rubric schema](../benchmark/schemas/rubric.json).

`search_query` holds the deterministic effective query. `interpretation` preserves the policy, user instructions, original location rubric, resolved ambiguities, dropped non-geographic location options with a note where applicable, and every removed, reduced, or demoted criterion with its original text, reason, and ID map (under `observability` and `implied_evidence`). `experience_context` contains criterion ID, original quote, effective requirement, and `kind=mandatory|preferred_not_a_gate` for duration-related statements. Original upper ranges may remain in quoted source evidence but have no automatic maximum effect.

## Benchmark suites

`data/benchmarks/<name>.json` freezes a named suite: `name`, `description`, `dataset_id`, `dataset_manifest_sha256`, `role_count`, `category_count`, and the ordered `role_ids`. `recruiterbench-lite-v1` has one role per category (58) and `recruiterbench-hard-v1` has all 290, both on `ashby-2026-09-30-v1`. `tools/define_benchmark_suites.py` writes them and refuses to overwrite an existing suite.

## Plans

A plan envelope has `plan_sha256` and `plan`. The plan records the benchmark suite name and hash (or null for an ad hoc selection) and freezes engine/dataset hashes, reviewer model and reasoning effort (`gpt-6-luna`/medium), as-of date, search provider, mode, API origin, provider result limit, repetitions, scoring version, interpretation policy, fixed unknown/top-1 relevance definitions, the pass/partial/fail credit map, selected role/rubric/experience-context/job-description/request bodies, and maximum workload. It has no credentials or candidate data. Plans are created offline and must match the dataset and engine at execution/resume time.

## Private execution data

Under `data/runs/<run_id>/`:

- `plan.json`: the immutable run plan.
- `.lock`: current process/host ownership, removed on orderly exit.
- `units/search/`: exact request inputs, attempt history, measured response, normalized ranked candidates, and contact/score-stripped original response.
- `units/gate/` and `units/relevance/`: model inputs and completed structured judgments with usage and hashes.
- `rejected/`: structured model results that failed local validation (initial draft and repair), retained for inspection.
- `results/`: per-search ranked evaluations and lineage hashes.
- `summary.json`: aggregate report, with no candidate-level data.

Units have `started`, `complete`, `failed`, or `uncertain` status. Non-timeout HTTP 4xx rejections and judgments rejected by local validation after one repair are recorded as failed; transport errors, 5xx, and ambiguous paid-response validation failures are uncertain. Other unexpected local errors are conservatively uncertain. Resume never repeats either status implicitly.

A `gate_fail` label is a valid model outcome that earns 0; error labels are unsuccessful evaluation operations that leave metrics incomplete. Reports distinguish these states. A completed candidate score is always 0, 0.5, or 1.0; null is reserved for an operational error. The first-stage output is `verdict` (`pass`/`partial`/`fail`), `failed_criteria` (rubric IDs or `location`), `implied_criteria` (must-have IDs or `location` that are only plausibly implied), and a one-sentence `description`; the second stage is `verdict` (`pass`/`partial`/`fail`), `underqualified`, `obviously_overqualified`, and a one-sentence `description`. `pass` earns 1.0, `partial` 0.5, and `fail` 0. Completed candidate records also expose the experience verdict, final relevance credit, and failing criterion IDs (if any). Raw professional histories and identities remain private even after contact fields are removed.

Second-stage output also requires booleans `underqualified` and `obviously_overqualified`, which cannot both be true. Either requires a `fail` verdict; the completed candidate label is `experience_underqualified` or `experience_overqualified` with zero credit. Gate failures and duplicates skip this review and have no experience verdict.

## Public summaries

Reports whitelist role metadata, provider/mode/model/scoring metadata, per-search metrics (`top1_relevance`, `p@K`) and latency, candidates returned (`returned_count` per search, `avg_candidates_returned` per aggregate), per-field profile coverage counts, label counts, gate-failure counts and failing-criterion counts, pass/partial/fail experience counts, underqualification and overqualification counts, and aggregates by role/category. No candidate IDs, identity keys, profile hashes, professional histories, model evidence, or raw responses are exported. An incomplete overall report contains null headline quality metrics and the expected/successful search counts. Null means unavailable, not zero.
