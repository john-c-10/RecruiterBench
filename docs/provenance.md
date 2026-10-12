# Provenance

## Reference structure

Reference repository: [LessieAI/people-search-bench](https://github.com/LessieAI/people-search-bench), inspected at commit [`f1afbb928e7eb6104e8255d5a882fc1820682cb7`](https://github.com/LessieAI/people-search-bench/tree/f1afbb928e7eb6104e8255d5a882fc1820682cb7).

This project follows its broad separation of benchmark code, provider adapters, loaders/data, evaluators, metrics, tools, documentation, and CLI. No upstream implementation was copied. This benchmark uses job-derived gates and P@K/first-result relevance; it does not reproduce the reference repository's external verification, query suite, nDCG/coverage/utility scoring, or leaderboard.

## Role source material

The corpus came from the prior curated Ashby directory: 58 recruiting categories with five distinct postings each, 290 unique roles, and 217 company boards. Each selected public job was checked against its Ashby feed and posting page on 2026-09-30. Source URLs and page/deadline observations are preserved in each snapshot. URLs that require Ashby's embed query parameters retain those parameters.

The corpus includes the original employer text and normalized metadata, not just search snippets. Employer descriptions and marks remain attributed source material; this project does not assign them a new license. The code is MIT-licensed (see `LICENSE`); that license does not extend to employer job text.

## Extraction and curation

All 290 rubrics were extracted using the exact requested `gpt-6-sol` model at low reasoning. The final frozen extraction prompt is included. Each file records source, prompt, and schema hashes, the requested/returned model, response ID, token usage, and generation time. The original draft requests/responses remain in the private extraction audit and are not needed to use the public corpus.

Validation checks required fields, sequential criterion IDs, exact source quotations, and lineage/content hashes. A pilot exposed subjective qualities being treated as hard requirements; the rubrics were regenerated with an explicit objective-qualification policy. Five targeted corrections are recorded on the affected rubric files: one location-quotation formatting correction, two clarifications separating subjective prestige/quality language from objective qualifications, and two removals of evaluator commentary from requirement text. These corrections do not alter original job snapshots. The effective rubric corpus is frozen in `rubrics-manifest.json`.

This validation does not establish that every interpretation is correct. Validation counts are exposed in `review-notes.json`.

## Interpretation

`ashby-2026-09-30-v1` applies the user-directed `role-interpretation-v1` policy to the extracted rubrics, offline and without new model calls or source refreshes. Each rubric record keeps the verbatim user instructions, the policy, all 32 source-question resolutions, and every removed, reduced, or demoted criterion with its original text and reason. Source snapshots are byte-identical to the imported job records.

The interpretation accepts alternative locations, a 100-mile radius around cities and metros, geographic restrictions on remote roles, text precedence over metadata, experience lower bounds without upper caps, and less restrictive source-supported alternatives. These are benchmark policy decisions, not new employer statements. Original source quotes remain unchanged except when a different existing source quote is selected to support the effective interpretation. Facility-only labels use the frozen postal address where necessary.

## Wrangle compatibility

The REST adapter was checked against the local Wrangle source contract in `pages/api/developers/search.js`, `src/utils/searchModes.js`, and `src/utils/searchLimits.js`. The source currently allows 200 results, returns the four needed professional fields, and exposes mode/limit/timing/source metadata. The harness validates mode/limit/source behavior again on actual responses. No claim of a deployed candidate-search run is made by these source checks.

Existing Wrangle benchmark patterns informed exact evidence quoting, model/rank blindness, unknown labels, frozen input hashes, private atomic artifacts, and explicit uncertain-call recovery. The standalone Python implementation does not import application modules or change application behavior.

OpenAI's official [Structured Outputs documentation](https://platform.openai.com/docs/guides/structured-outputs) informed the strict JSON schema contract. Live rubric extraction established access to `gpt-6-sol` for the configured account at build time. New rubric extraction uses `gpt-6.1-sol` and the candidate reviewers use `gpt-6-luna` at medium reasoning; historical extraction does not verify access to this model. No fallback model is substituted.
