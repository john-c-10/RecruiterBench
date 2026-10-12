# RecruiterBench

A benchmark for AI people search, built from real job postings.

Existing people-search benchmarks only score the top 10 candidates per query. We believe that is not representative of the volume a successful placement or recruiting search needs, so RecruiterBench scores up to 200 candidates per search. A provider that returns fewer than 200 is zero-padded to 200, so short lists are penalized; every result reports how many candidates each provider actually returned. It ships in two suites:

- **RecruiterBench Lite v1** (`recruiterbench-lite-v1`): 58 roles, one per recruiting category.
- **RecruiterBench Hard v1** (`recruiterbench-hard-v1`): all 290 roles, five per category.

## Results

| Platform | Suite | Top-1 relevance | p@10 | p@50 | p@100 | p@200 | Candidates returned | Avg latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Juicebox | Lite v1 | | | | | | | |
| Exa | Lite v1 | | | | | | | |
| Wrangle | Lite v1 | | | | | | | |
| Juicebox | Hard v1 | | | | | | | |
| Exa | Hard v1 | | | | | | | |
| Wrangle | Hard v1 | | | | | | | |

## Scoring

Each search returns up to 200 candidates. Every candidate is reduced to four profile fields (location, education, experience, listed skills) and scored by a two-stage LLM judge. Both stages are the same model at the same effort, and the second runs only when the first does not fail, so they are not independent reviewers:

1. **Gate.** Checks every must-have, red flag, and required location. **Pass** when every must-have is stated or clearly implied; **partial** when nothing is contradicted but some must-have is only plausibly implied by closely adjacent experience (final credit capped at **0.5**); **fail** (score **0**) when a must-have has no related support, a red flag applies, or location fails.
2. **Experience fit.** Runs only if the gate passes or is partial, without seeing the gate's verdict: does the candidate have adequate experience to do this role at its scope and level? **Pass = 1.0, partial = 0.5, fail = 0.** It fails both underqualified candidates (for example, two or more levels below the role, or only internship-level exposure to its core work) and obviously overqualified ones; reports count each separately.

| Metric | Definition |
| --- | --- |
| Top-1 relevance | Credit of the first result (0, 0.5, or 1). This is precision at rank 1, not recall. |
| p@K | Sum of credits in the top K results, divided by K (K = 10, 50, 100, 200). Short result lists are padded with zeros; duplicate people score 0. |
| Candidates returned | Average number of candidates a provider returned per successful search, out of 200. Read it next to p@200: a low p@200 can come from few results rather than poor ones. |
| Avg latency | Each query's fastest successful search, averaged across queries. Retries never count. |

Both stages use `gpt-6-luna` at medium reasoning effort with strict JSON schemas; rubric extraction is separate and keeps its own recorded model. Neither stage sees names, photos, contact details, search rank, or provider scores.

## Evaluation criteria

Each role has a frozen rubric derived from its job posting:

- **Skills**: a listed skill counts as proficiency, including requirements for strong or advanced skill, and proficiency can be inferred from job history; years thresholds still come from job history.
- **Must-haves** never fail just because they aren't mentioned explicitly. They pass when the profile states them or clearly implies them (what an experienced recruiter would infer from titles, responsibilities, employers, durations, degrees, and skills; never from prestige or identity), are partial when closely adjacent experience makes them plausible (including a years threshold missed by about 10% or less), and fail only when contradicted or entirely unsupported.
- **Red flags** fail only when the profile states or implies the disqualifying condition.
- **Location** is one rule per role, used identically in the search brief and the gate: one office area, one remote eligibility region (which may span several named countries or states, such as "United States or Canada"), the country when the role offers relocation and a hire from anywhere in it is realistic, or no filter when relocation is offered to hires from any location. A role never mixes an office city with a remote option. Cities and metros cover about 100 miles around them; countries, states, and regions cover anywhere inside them. Willingness to relocate is never required as evidence. A profile that gives only a broader place that contains an allowed area (for example "France" for a Paris role) is partial; it fails only when the location matches nothing allowed.
- **Preferences** are never gates.
- **Experience fit** judges the substance, scope, and level of prior work. Passing the gate does not establish adequacy: a candidate who meets every must-have but is two or more levels below the role, has never led people for a role that requires it, or has touched the core work only through internships or coursework is underqualified and fails. One level below, or genuinely plausible adjacent experience, is partial. Overqualification is judged leniently: only a candidate at least two levels above the role (for example a current VP, partner, or C-level executive for an individual-contributor or associate role) fails. One level above, senior titles, or extra years alone are not overqualification, and debatable cases pass on their experience.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
export WRANGLE_API_KEY=... EXA_API_KEY=... OPENAI_API_KEY=...

python main.py plan --benchmark recruiterbench-lite-v1 --api-base-url https://app.usewrangle.com \
  --mode fast --as-of 2026-09-30 --out data/private/plans/lite.json
python main.py run --plan data/private/plans/lite.json --run-id lite --parallel 10
python main.py report --run-id lite --out data/private/lite-summary.json

# Exa people search (public API origin by default; up to 100 results per search)
python main.py plan --benchmark recruiterbench-lite-v1 --provider exa --mode auto \
  --as-of 2026-09-30 --out data/private/plans/lite-exa.json
```

Search providers are adapters in `benchmark/providers/`. Exa's public `/search` caps people results at 100 and returns location, work history, and education but no listed skills or role descriptions; each report's `field_coverage_counts` shows that profile coverage per provider. Juicebox has no adapter yet.

Runs resume with the same command. Results are written to `data/runs/` and stay out of Git.

## License

Code is released under the [MIT License](LICENSE). Job postings and employer text in `data/roles/` remain the employers' material and are not relicensed.
