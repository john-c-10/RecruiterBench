"""Fixed-denominator graded precision and top-1 relevance."""
from collections import Counter, defaultdict
from statistics import mean
from .profiles import PROFILE_FIELDS
from .scoring import TOP1_DEFINITION, UNKNOWN_POLICY, VERDICT_CREDIT, candidate_credit, validate_scoring

CUTOFFS = (10, 50, 100, 200)
# Top-1 relevance is the first result's credit (0, 0.5, or 1): precision at
# rank 1, not recall.
METRICS = ("top1_relevance", *(f"p@{k}" for k in CUTOFFS))


def quality_metrics(judgments: list[dict], returned_count: int, *, unknown_policy: str = UNKNOWN_POLICY, top1_definition: str = TOP1_DEFINITION) -> dict:
    validate_scoring(unknown_policy, top1_definition)
    if not 0 <= returned_count <= 200 or len(judgments) != returned_count:
        raise ValueError("Judgments must cover every returned rank")
    if [row["rank"] for row in judgments] != list(range(1, returned_count + 1)):
        raise ValueError("Judgments must retain original contiguous search ranks")
    complete = all(row["status"] == "complete" for row in judgments)
    for row in judgments:
        relevance = row.get("relevance")
        if row["status"] == "complete" and (type(relevance) not in (int, float) or relevance not in (0, 0.5, 1)):
            raise ValueError("Completed judgments require relevance 0, 0.5, or 1")
        if row["status"] == "complete":
            if row.get("experience_verdict") is not None and relevance != candidate_credit(row.get("gate_outcome", "pass"), row["experience_verdict"]):
                raise ValueError("Relevance credit does not match the gate and experience verdicts")
            if (row.get("failed_criteria") or row.get("label") in ("gate_fail", "duplicate")) and relevance != 0:
                raise ValueError("Failed hard gates and duplicates must receive zero credit")
            if (row.get("obviously_overqualified") or row.get("underqualified")) and relevance != 0:
                raise ValueError("Obviously overqualified or underqualified candidates must receive zero credit")
    completed = [row for row in judgments if row["status"] == "complete"]
    gate_failures = sum(row.get("label") == "gate_fail" for row in completed)
    scoreable = complete
    labels = [row.get("relevance") or 0 for row in judgments]
    result = {"top1_relevance": (labels[0] if labels else 0) if scoreable else None}
    result.update({f"p@{k}": sum(labels[:k]) / k if scoreable else None for k in CUTOFFS})
    return {"metrics": result, "evaluation_complete": complete, "scoreable": scoreable,
            "returned_count": returned_count, "coverage@200": returned_count / 200,
            "gate_fail_count": gate_failures, "gate_fail_rate": gate_failures / returned_count if returned_count else 0,
            "gate_partial_count": sum(row.get("gate_outcome") == "partial" for row in completed),
            "implied_criteria_counts": dict(Counter(criterion for row in completed for criterion in row.get("implied_criteria") or [])),
            "failed_criteria_counts": dict(Counter(criterion for row in completed for criterion in row.get("failed_criteria") or [])),
            "experience_verdict_counts": {verdict: sum(row.get("experience_verdict") == verdict for row in completed) for verdict in VERDICT_CREDIT},
            "overqualified_count": sum(bool(row.get("obviously_overqualified")) for row in completed),
            "underqualified_count": sum(bool(row.get("underqualified")) for row in completed),
            "labels": dict(Counter(row.get("label", "error") for row in judgments))}


def summarize(searches: list[dict], expected: int) -> dict:
    """Never disguise a failed/missing search as an empty successful search."""
    complete = len(searches) == expected and all(row.get("scoreable", False) for row in searches)
    by_role, by_category = defaultdict(list), defaultdict(list)
    for row in searches:
        by_role[row["role_id"]].append(row)
        by_category[row["category"]].append(row)

    def aggregate(rows):
        successful = [row for row in rows if row.get("search_status") == "complete"]
        # Latency is each query's fastest successful pull across repetitions;
        # failed or retried attempts never contribute.
        fastest = defaultdict(list)
        for row in successful:
            fastest[row["role_id"]].append(row["latency_ms"])
        values = [min(pulls) for pulls in fastest.values()]
        scoreable = len(successful) == len(rows) and all(row.get("scoreable", False) for row in rows)
        covered = [row for row in rows if "field_coverage_counts" in row]
        return {"search_count": len(rows), "successful_searches": len(successful),
                "avg_latency_ms": mean(values) if values else None,
                # Short lists are zero-padded in p@K; this shows how many results
                # each successful search actually returned (out of 200).
                "avg_candidates_returned": mean(row["returned_count"] for row in successful) if successful else None,
                "returned_profiles": sum(row.get("returned_count", 0) for row in covered),
                "field_coverage_counts": {field: sum(row["field_coverage_counts"][field] for row in covered)
                                          for field in PROFILE_FIELDS},
                "metrics": {key: mean(row["metrics"][key] for row in rows) if scoreable and rows else None for key in METRICS},
                "scoreable": scoreable}
    global_summary = aggregate(searches)
    if not complete:
        global_summary["metrics"] = dict.fromkeys(METRICS)
        global_summary["scoreable"] = False
    return {"status": "complete" if complete else "incomplete", "expected_searches": expected,
            "overall": global_summary,
            "by_role": {key: aggregate(rows) for key, rows in by_role.items()},
            "by_category": {key: aggregate(rows) for key, rows in by_category.items()}, "searches": searches}
