# Methodology

## Roles

The corpus is a purposive recruiting sample: 58 categories with five unique postings each, drawn from 217 Ashby company boards. Categories mix industries, functions, and company stages; they are useful recruiting slices, not exclusive sectors or a representative labor-market sample. No geographic or seniority balance is claimed.

Each posting was checked on 2026-09-30 and frozen as a hashed snapshot of the original HTML, plain text, metadata, and source URLs. The snapshot date is not a promise that the position remains open.

A model extracts each rubric from its posting, separating explicit qualifications from duties, preferences, and location. Red flags are explicit disqualifiers in the source, never invented heuristics. Every criterion carries an exact quote from one source field. A quote proves provenance, not that the interpretation is correct.

## Interpretation

The `role-interpretation-v1` policy turns extracted rubrics into gates that can be judged from a professional profile:

- **Observable criteria only.** Requirements that need candidate confirmation (work authorization, citizenship, clearance, travel, schedule, physical demands) or evidence outside the judged fields (languages, portfolios, publications, licenses, designations) are removed. A criterion with an observable core is reduced to that core.
- **Largely unstated must-haves become preferences.** Soft skills, specific practices or outcomes, quantified performance claims, and long all-of lists are demoted, or reduced to their observable core with the original kept as a preference.
- **Experience ranges keep only their lower bound.** "3–5 years" gates at 3+; overqualification is judged separately at the experience stage.
- **Role text takes precedence over metadata**, and any remaining ambiguity takes the less restrictive source-supported reading.
- **One location rule per role.** The search brief and the gate use the same rule, which is exactly one of: the primary office area; one remote eligibility region (which may name several countries or states, such as "United States or Canada"); the whole country when relocation is offered and a hire from anywhere in it is realistic; or no filter when relocation is offered to hires from any location. A role never combines an office city with a remote option. A city or metropolitan area covers about 100 miles (160 km) around it; a country, state, or region covers anywhere inside it.

Each rubric's `interpretation` record lists its location decision and every removed, reduced, or demoted criterion with the original text and reason. These are benchmark decisions, not employer statements.

The search brief is rendered deterministically from the role summary, hard conditions, labeled preferences, and the location rule, and stored as `search_query`. Briefs and rubrics are frozen into a hashed plan before any paid call.

## Candidate evidence

Both judge stages see only four professional fields from the search response: location, education history, experience history, and listed skills. This is a provider's projection of profile data, not a live scrape or credential check. Providers differ in coverage: Exa's public people search returns location, work history, and education but no listed skills or role descriptions. Every report includes per-field coverage counts. Top-level headlines and summaries are not substituted for experience, provider-generated evaluations are excluded, and a profile over 120 KB after normalization is rejected rather than truncated.

Duplicates are detected within each search by person ID and canonical LinkedIn URL. A duplicate keeps its rank and earns zero. A result with neither identifier invalidates the search.

## Two-stage judge

**Stage one (gate)** checks every must-have, every red flag, and the location rule. It receives the role summary and the effective requirement text, without the original employer quotes. It returns `pass`, `partial`, or `fail`, the IDs of failing criteria (`location` for a location failure), the IDs that are only plausibly implied, and a one-sentence description. Code rejects verdicts that are inconsistent with their IDs.

- A must-have **passes** when the profile states it or reasonably implies it: the conclusion an experienced recruiter would draw from titles, responsibilities, employers' line of business, durations, degrees, and skills, never from prestige or identity. A listed skill counts as proficiency; years thresholds require job history, count only professional work in the specified function, exclude internships unless allowed, and are never rounded up.
- It is **partial** when nothing is contradicted but only closely adjacent experience makes it plausible, including a years threshold missed by about 10% or less.
- It **fails** when contradicted or entirely unsupported. A must-have naming the role's core function requires that function, not an adjacent one in the same domain.
- A red flag fails only when the profile states or implies the disqualifying condition.
- Location passes when the candidate's current location is inside the allowed area; living there implies the ability to meet onsite or hybrid terms. A broader stated place that contains the area (for example "France" for a Paris role) is partial. Willingness to relocate is never required as evidence.

**Stage two (experience fit)** runs only after a `pass` or `partial` gate. It is a separate call to the same model and does not see the gate verdict, rank, or provider score, so the two stages form one two-stage judge, not independent reviewers. It receives the title, summary, effective must-haves, experience-duration context, and the original job description for scope and level, and judges one factor: adequate experience to do the role at its scope and level.

- **Pass (1.0):** adequate, appropriately scoped experience, stated or reasonably implied.
- **Partial (0.5):** relevant experience at roughly the role's level whose adequacy is only partly shown, including one level below the role.
- **Fail (0):** underqualified or obviously overqualified.

Underqualified means the candidate's current or most recent sustained role is at least two levels below the role, the role requires leading people and the profile shows none, the core work appears only in internships, coursework, or a brief stint, or the relevant experience is far narrower than the role. Obviously overqualified is judged leniently: only a candidate at least two levels above the role (for example a current VP, partner, or C-level executive for an individual-contributor role). Missing preferences or evidence outside the four fields never make a candidate underqualified. Age, salary expectations, motivation, and protected traits are never inferred.

Both stages use `gpt-6-luna` at medium reasoning effort with strict JSON schemas and treat all role and profile text as untrusted data. The prompts' worked examples come only from occupations absent from the corpus, so the judge is not calibrated on the roles it scores. Judges return short descriptions rather than evidence quotes, so verdicts are auditable but not mechanically grounded. No browsing or enrichment happens during judging.

## Metrics

For the candidate at rank i:

- `y_i = 0` when the gate fails.
- A `partial` gate caps `y_i` at 0.5.
- Otherwise the experience verdict sets `y_i`: pass 1.0, partial 0.5, fail 0.
- Duplicates and unfilled slots earn 0. Search and judge errors are incomplete observations, never negative labels.

`P@K = sum(y_i for i = 1..K) / K` for K = 10, 50, 100, 200. Because credit can be fractional, this is graded precision. The denominator is always K. Top-1 relevance is `y_1`: precision at rank 1, not recall.

Zero-padding to 200 is deliberate: the benchmark asks for 200 candidates, and missing slots earn nothing. Exa's public search returns at most 100 results, which caps its P@200 at 0.5. Reports show `returned_count` per search and `avg_candidates_returned` per aggregate so a low P@200 can be read as few results or poor ones.

Headline quality means are published only when every planned role is scoreable. Overall quality is the mean across role-searches, which equals the macro mean over roles when repetitions are equal. Per-role and per-category results remain visible in incomplete reports.

## Latency

Latency is a monotonic client timer around each search request, from send to response receipt. It includes network time and excludes normalization, pacing, judging, and file I/O. API- and backend-reported durations are kept separately.

Per-role latency is the fastest successful pull across repetitions; failed attempts and retries never count. Aggregates average the per-role fastest pulls over successful searches and show successful/expected counts. The harness controls pacing but cannot guarantee an idle service or a cold server cache; comparisons should use the same roles, three or more repetitions, the same account permissions, and documented execution windows.

## Reproducibility and failures

Plans hash the selected snapshots, effective rubrics, exact request bodies, model and reasoning settings, prompts, schemas, code, scoring settings, and evaluation date; changed inputs require a new plan. Completed judgments keep the provider model identifier and token usage. Model outputs are nondeterministic, so stored judgments make a run auditable, not exactly repeatable.

State is marked started before every paid call. Completed records are hashed and reused on resume. Failed or uncertain attempts require an explicit retry flag per stage, so retrying judgments never re-sends a search. A search or judge error stops new roles from starting and cancels queued judgments, producing an incomplete report. A judgment that fails local validation gets one repair request; a second rejection is a failure. A search whose profiles all lack experience data is rejected as a probable field mismatch. Schema failures, model fallback, silent mode or limit changes, and invalid identities are errors, not low-relevance candidates.

## Limitations

The 290 rubrics are model-generated reference specifications with mechanical validation and targeted review, not human gold labels or employer-confirmed requirements. Gate failures can be driven by thin profiles rather than poor matches. Publish candidates returned, per-field coverage, gate-failure rates, underqualification and overqualification counts, and the pass/partial/fail distribution alongside graded precision. A category-balanced human calibration set would improve validity, particularly for scope and level judgments.
