# RecruiterBench

A benchmark for AI people search, built from 290 real job postings.

Most people-search benchmarks score only the top 10 results, but a real recruiting search needs far more depth. RecruiterBench scores up to 200 candidates per search. Shorter lists are zero-padded, except where a provider cannot return that many results at all (see below), and every report shows how many candidates each provider actually returned. There are two suites:

- **RecruiterBench Lite v1** (`recruiterbench-lite-v1`): 58 roles, one per recruiting category.
- **RecruiterBench Hard v1** (`recruiterbench-hard-v1`): all 290 roles, five per category.

## Results

### Hard v1

| Platform                | Top-1 relevance |  p@10 |  p@50 |             p@100 |             p@200 | Candidates returned | Avg latency |
| ----------------------- | --------------- | ----- | ----- | ----------------- | ----------------- | ------------------- | ----------- |
| Wrangle (deep)          |           0.914 | 0.873 | 0.799 |             0.747 |             0.676 |               199.6 |       56.0s |
| Wrangle (turbo)         |           0.772 | 0.748 | 0.697 |             0.661 |             0.619 |               199.7 |       39.3s |
| Metaview (source)       |           0.667 | 0.680 | 0.617 |             0.517 |             0.462 |               140.1 |      117.3s |
| Exa (auto)              |           0.433 | 0.388 | 0.359 |             0.311 | Cannot return 200 |               100.0 |        3.2s |
| Treg (parallel)         |           0.259 | 0.231 | 0.160 | Cannot return 100 | Cannot return 200 |                50.2 |        4.6s |

### Lite v1

| Platform                | Top-1 relevance |  p@10 |  p@50 |             p@100 |             p@200 | Candidates returned | Avg latency |
| ----------------------- | --------------- | ----- | ----- | ----------------- | ----------------- | ------------------- | ----------- |
| Wrangle (deep)          |           0.957 | 0.892 | 0.807 |             0.756 |             0.683 |               200.0 |       61.0s |
| Wrangle (turbo)         |           0.810 | 0.762 | 0.714 |             0.672 |             0.621 |               200.0 |       43.1s |
| Metaview (source)       |           0.681 | 0.684 | 0.626 |             0.529 |             0.461 |               141.1 |      134.5s |
| Juicebox (computer use) |           0.595 | 0.575 | 0.509 |             0.454 |             0.392 |               163.9 |         N/A |
| Exa (auto)              |           0.457 | 0.437 | 0.402 |             0.344 | Cannot return 200 |               100.0 |        3.0s |
| Treg (parallel)         |           0.216 | 0.232 | 0.178 | Cannot return 100 | Cannot return 200 |                51.7 |        5.5s |

### Provider notes

A `Cannot return K` cell means the provider cannot return K results per search, either because its API caps or rejects the request (Exa at 200, Treg at 200) or because it rarely fills it (Treg at 100), so that p@K is not reported rather than zero-padded to K. Every other cell keeps the standard zero-padding: a search that returns fewer results than K still scores zero for the missing slots.

- **Wrangle** (deep, turbo) and **Exa** (auto) ran through this harness's API adapters. Each Lite score reuses the 58-role subset of that provider's Hard searches and judgments.
- **Exa** returns at most 100 results and no listed skills, so its p@200 is not reported (not zero-padded).
- **Metaview (source)** ran through its MCP endpoint on October 15, 2026, at no more than three new searches per minute. Of 290 Hard searches, 289 succeeded. The one provider failure, which persisted after a retrieval retry, is scored zero and kept in every quality denominator; candidate counts and latency average successful searches only. Lite reuses the Hard subset and has no failures. Reports: `data/runs/hard-v1-metaview-source-20261015/`.
- **Treg (parallel)** ran through its `parallel.people.search` endpoint on October 15, 2026, requesting 100 results with 10 concurrent searches. The 58 Lite queries ran first, and Lite reuses them. Treg rejected requests for 200 results, and only 6 of 290 searches filled 100 (50.2 returned on average), so p@100 and p@200 are not reported (not zero-padded). Profile descriptions are capped at 1,000 characters (12,459 of 14,556 profiles, 85.6%, hit the cap) and no enrichment was added, so thin profiles can lower scores. Work experience listed under both `Experience` and `Position` is included, verified against raw responses for ten roles. Reports: `data/runs/hard-v1-treg-parallel-20261015/`.
- **Juicebox (computer use)** was run through its Sourcing UI. LinkedIn URLs were collected from result cards and enriched through Wrangle before judging. Each search continued until 200 profiles were enriched or results ran out; 43 of 58 Lite roles reached 200. Duplicates keep their rank and score zero. Some saved searches reordered when resumed; already-captured prefixes were kept and the changes logged in the run artifacts. Latency is not reported because collection included interruptions and rate-limit cooldowns. Hard is incomplete; collection is continuing at no more than one new search every five minutes. Reports and resume checkpoints: `data/runs/juicebox-computer-use-hard-v1/`.

## Scoring

Each role has a frozen rubric derived from its posting. Every returned candidate is reduced to four profile fields (location, education, experience, listed skills) and scored by a two-stage judge. Both stages use `gpt-6-luna` at medium reasoning effort with strict JSON schemas, and neither sees names, photos, contact details, search rank, or provider scores. Because both stages are the same model and the second runs only after the first, they form one two-stage judge rather than independent reviewers.

1. **Gate** checks must-haves, red flags, and location. Any failure scores **0**; a partial gate caps the final credit at **0.5**.
   - A **must-have** passes when the profile states it or clearly implies it, as an experienced recruiter would infer from titles, responsibilities, employers, durations, degrees, and skills (never from prestige or identity). It is partial when only closely adjacent experience makes it plausible, including a years threshold missed by about 10% or less, and fails only when contradicted or unsupported. A listed skill counts as proficiency, even for "strong" or "advanced" requirements; years thresholds come from job history.
   - A **red flag** fails only when the profile states or implies it. **Preferences** are never gates.
   - **Location** is one rule per role, shared with the search brief: one office area, one remote region (such as "United States or Canada"), the whole country when relocation makes a hire from anywhere in it realistic, or no filter when relocation is open to anyone. Cities and metros cover about 100 miles; countries, states, and regions cover anywhere inside them. A broader place that contains the area ("France" for a Paris role) is partial.
2. **Experience fit** runs after a passed or partial gate, without seeing its verdict, and asks whether the candidate's experience fits the role's scope and level: **pass 1.0, partial 0.5, fail 0**. Underqualified candidates (two or more levels below the role, no people leadership when the role requires it, or only internship-level exposure to the core work) and obviously overqualified ones (at least two levels above, such as a current VP or partner for an individual-contributor role) both fail and are counted separately. One level below is partial.

| Metric              | Definition |
| ------------------- | --- |
| Top-1 relevance     | Credit of the first result (0, 0.5, or 1). This is precision at rank 1, not recall. |
| p@K                 | Sum of credits in the top K results, divided by K (K = 10, 50, 100, 200). Short result lists are padded with zeros; duplicate people score 0. |
| Candidates returned | Average number of candidates a provider returned per successful search, out of 200. Read it next to p@200: a low p@200 can come from few results rather than poor ones. |
| Avg latency         | Each query's fastest successful search, averaged across queries. Retries never count. |

Rubric extraction uses a separate, recorded model. The full rules are in [docs/methodology.md](docs/methodology.md); file formats are in [docs/data-format.md](docs/data-format.md).

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
export WRANGLE_API_KEY=... EXA_API_KEY=... OPENAI_API_KEY=...

python main.py plan --benchmark recruiterbench-lite-v1 --api-base-url https://app.usewrangle.com \
  --mode fast --as-of 2026-09-30 --out data/private/plans/lite.json
python main.py run --plan data/private/plans/lite.json --run-id lite --parallel 3
python main.py report --run-id lite --out data/private/lite-summary.json

# Exa people search (public API origin by default; up to 100 results per search)
python main.py plan --benchmark recruiterbench-lite-v1 --provider exa --mode auto \
  --as-of 2026-09-30 --out data/private/plans/lite-exa.json
```

Search providers are adapters in `benchmark/providers/`. Exa's public `/search` caps people results at 100 and returns location, work history, and education but no listed skills or role descriptions; each report's `field_coverage_counts` shows that profile coverage per provider.

Runs resume with the same command. Results are written to `data/runs/` and stay out of Git.

### Metaview MCP

Put `METAVIEW_API_KEY` and `OPENAI_API_KEY` in `.env`, then run `python3 tools/run_metaview_benchmark.py`. It checks the first Hard role and a real judge response, then runs all 290 Hard roles at no more than three new searches per minute, writing `summary-hard.json` and `summary-lite.json` under `data/runs/hard-v1-metaview-source-20261015/`. Lite uses the exact 58-role subset of the Hard results and judgments, and the first role is reused when the full run continues. `--preflight-only` runs only the first role; `--report-only` rebuilds reports offline.

Each role sends one frozen brief requesting up to 200 candidates. The adapter waits for the sourcing agent to go idle, paginates full profiles, and keeps MCP list order, which is not a documented relevance ranking. Only the four standard profile fields are judged; Metaview's match summaries are excluded. Latency includes asynchronous generation, polling, and profile retrieval. Checkpoints keep remote search IDs. No feedback, outreach, ATS posting, or recurring searches are requested.

Recovery options:

- **Startup polls.** An empty idle response in the first two minutes after creation is polled again. `--resume-startup-polls` recovers an older run that stopped on this response, after the original process exits, and only when every incomplete unit has this exact error and a saved remote search ID.
- **Approved failure exception.** For role `02db71fb-9524-435e-8f96-4053011581c1` in the October 15 run, `python3 tools/resume_metaview_benchmark.py` retries retrieval from the saved search ID and, if it still fails, records a zero for that role only, with a separate `failure-zero/` receipt; the original checkpoint and failed attempt remain. Reports label the failure, include the zero in quality scores, and exclude it from successful-search counts and latency. Use this launcher's `--report-only` to keep those annotations when rebuilding reports.
- **Judge retries.** After an inspected scoring HTTP 520, `--retry-judge` resumes unfinished judgments only when every search is complete and no other failure type is open. It keeps completed judgments and records provenance in `judge-recoveries/`. Future 520 responses retry at most twice with backoff, with the same model, prompts, and scoring.

Uncertain creation requests require reconciliation and are never resubmitted automatically.

## License

Code is released under the [MIT License](LICENSE). Job postings and employer text in `data/roles/` remain the employers' material and are not relicensed.
