# Provenance

## Reference structure

Reference repository: [LessieAI/people-search-bench](https://github.com/LessieAI/people-search-bench), inspected at commit [`f1afbb928e7eb6104e8255d5a882fc1820682cb7`](https://github.com/LessieAI/people-search-bench/tree/f1afbb928e7eb6104e8255d5a882fc1820682cb7).

This project follows its broad separation of benchmark code, provider adapters, data loaders, evaluators, metrics, tools, documentation, and CLI. No upstream implementation was copied. This benchmark uses job-derived gates and P@K/top-1 relevance; it does not reproduce the reference repository's external verification, query suite, nDCG/coverage/utility scoring, or leaderboard.

## Role source material

The corpus is a curated set of public Ashby postings: 58 recruiting categories with five distinct postings each, 290 unique roles from 217 company boards. Each posting was checked against its Ashby feed and posting page on 2026-09-30, and source URLs and page/deadline observations are kept in each snapshot.

The corpus includes the original employer text and normalized metadata. Employer descriptions and marks remain attributed source material and are not relicensed; the MIT license (see `LICENSE`) covers the code only.

## Extraction

All 290 rubrics were extracted with `gpt-6-sol` at low reasoning effort using the frozen prompt in `benchmark/prompts/rubric.txt`. Each rubric file records the source, prompt, and schema hashes, the requested and returned model, response ID, token usage, and generation time. Re-running extraction uses the model configured in `benchmark/models.py`; no fallback model is substituted.

Validation checks required fields, sequential criterion IDs, exact source quotations, and content hashes, with counts in `review-notes.json`. It does not establish that every interpretation is correct. The interpretation policy (see [methodology](methodology.md)) is applied offline, without new model calls or source refreshes, and each rubric's `interpretation` record lists every criterion it changed and why. Source snapshots are byte-identical to the imported job records. The effective rubrics are frozen in `rubrics-manifest.json`.

## Search providers

The Wrangle adapter targets the public developer search API (`/api/developers/search`), which returns up to 200 results with the four judged profile fields and mode, limit, and timing metadata. The Exa adapter targets Exa's public `/search` endpoint. The harness validates mode, limit, and source behavior on every response and never imports provider application code.

OpenAI's [Structured Outputs documentation](https://platform.openai.com/docs/guides/structured-outputs) informed the strict JSON schema contract used for extraction and judging.
