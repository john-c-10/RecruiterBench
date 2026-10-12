"""User-selected scoring semantics shared by evaluation, plans, and metrics."""

SCORING_VERSION = "experience-fit-v1"
UNKNOWN_POLICY = "zero"
TOP1_DEFINITION = "top-result-relevance"
VERDICT_CREDIT = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
# A gate "partial" (some must-haves only plausibly implied) caps final credit.
GATE_PARTIAL_CAP = 0.5


def verdict_credit(verdict: str) -> float:
    if verdict not in VERDICT_CREDIT:
        raise ValueError("Experience verdict must be pass, partial, or fail")
    return VERDICT_CREDIT[verdict]


def candidate_credit(gate_verdict: str, experience_verdict: str) -> float:
    credit = verdict_credit(experience_verdict)
    return min(credit, GATE_PARTIAL_CAP) if gate_verdict == "partial" else credit


def validate_scoring(unknown_policy: str, top1_definition: str) -> None:
    if unknown_policy != UNKNOWN_POLICY or top1_definition != TOP1_DEFINITION:
        raise ValueError("This benchmark requires unknown hard constraints to fail and top-1 relevance to mean first-result credit")
