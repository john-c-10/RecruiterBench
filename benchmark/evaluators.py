"""Two-stage judge: a hard gate, then experience fit only for candidates that pass it."""
from pathlib import Path
import re
from threading import Lock
import jsonschema

from .io import atomic_json, digest
from .models import JUDGE_MODEL, JUDGE_REASONING_EFFORT, prompt, schema
from .scoring import candidate_credit
from .state import ValidationRejected, execute_unit

REPAIR_REQUEST = ("The previous draft failed local validation: {reason}. Re-evaluate from the supplied role and profile; "
                  "the draft is not evidence. Return the complete corrected judgment.")


def validate_gate(value: dict, rubric: dict) -> dict:
    jsonschema.validate(value, schema("gate"))
    allowed = [item["id"] for field in ("must_haves", "red_flags") for item in rubric[field]]
    if rubric["location"]["required"]:
        allowed.append("location")
    failed, implied = value["failed_criteria"], value["implied_criteria"]
    if len(failed) != len(set(failed)) or not set(failed) <= set(allowed):
        raise ValueError("failed_criteria must name each failing rubric ID or 'location' at most once")
    impliable = {item["id"] for item in rubric["must_haves"]} | ({"location"} if rubric["location"]["required"] else set())
    if len(implied) != len(set(implied)) or not set(implied) <= impliable:
        raise ValueError("implied_criteria must name must-have IDs or 'location' at most once")
    if (value["verdict"] == "fail") != bool(failed):
        raise ValueError("A fail verdict must list failed_criteria, and pass or partial must list none")
    # A fail may also note plausibly implied must-haves; they do not change its zero credit.
    if value["verdict"] == "partial" and not implied:
        raise ValueError("A partial verdict must list implied_criteria")
    if value["verdict"] == "pass" and implied:
        raise ValueError("A pass verdict must list no implied_criteria")
    return value


def validate_relevance(value: dict) -> dict:
    jsonschema.validate(value, schema("relevance"))
    if value["obviously_overqualified"] and value["verdict"] != "fail":
        raise ValueError("Obvious overqualification must receive a fail verdict")
    if value["underqualified"] and value["verdict"] != "fail":
        raise ValueError("Underqualification must receive a fail verdict")
    if value["underqualified"] and value["obviously_overqualified"]:
        raise ValueError("A candidate cannot be both underqualified and obviously overqualified")
    return value


ARRANGEMENT = re.compile(r"\s*\((?:remote|hybrid option|hybrid|onsite|on-site)\)\s*$|,\s*(?:preferred|primarily)\s*$", re.I)


def location_alternatives(labels: list[str]) -> list[dict]:
    """Each allowed location as an explicit OR alternative with its matching rule.

    Work-arrangement notes such as "(remote)" never narrow the geography.
    """
    alternatives = []
    for label in labels:
        place = ARRANGEMENT.sub("", label).strip()
        metro = re.search(r"\barea\b|metropolitan", place, re.I) is not None
        alternatives.append({"place": place, "rule": "within about 100 miles of this city or metro area" if metro
                             else "anywhere inside this country, state, or region",
                             **({"arrangement": label[len(place):].strip(" ,()")} if place != label else {})})
    return alternatives


def gate_rubric(rubric: dict) -> dict:
    """Effective requirements only: original quotes could reimpose superseded wording."""
    def criteria(items):
        return [{"id": item["id"], "requirement": item["requirement"], "verification": item["verification"]} for item in items]
    return {"role_summary": rubric["role_summary"], "must_haves": criteria(rubric["must_haves"]),
            "red_flags": criteria(rubric["red_flags"]),
            "location": {**{key: value for key, value in rubric["location"].items() if key != "source_quotes"},
                         "alternatives": location_alternatives(rubric["location"]["allowed_locations"])}}


class Evaluator:
    def __init__(self, judge, root: Path, as_of: str, *, retry_failed=False, retry_uncertain=False):
        self.judge, self.root, self.as_of = judge, root, as_of
        self.retries = {"retry_failed": retry_failed, "retry_uncertain": retry_uncertain}
        self._locks, self._locks_guard = {}, Lock()
        self._warm, self._warmed = {}, set()

    def _review(self, stage: str, role: dict, rubric: dict, profile: dict):
        policy, output_schema = prompt(stage), schema(stage)
        # Neither reviewer receives names, identities, provider scores, rank,
        # previous judgments, or non-professional contact information.
        role_context = ({"title": role["title"], "role_summary": rubric["role_summary"],
                         "must_haves": gate_rubric(rubric)["must_haves"],
                         "experience_context": role.get("experience_context", []),
                         "job_description": role["job_description"]}
                        if stage == "relevance" else {"title": role["title"], "rubric": gate_rubric(rubric)})
        payload = {"as_of": self.as_of, "role": role_context, "profile": profile}
        inputs = {"payload": payload, "model": JUDGE_MODEL, "reasoning_effort": JUDGE_REASONING_EFFORT,
                  "prompt_sha256": digest(policy), "schema_sha256": digest(output_schema)}
        key = digest(inputs)
        # Every call for one role and stage shares the prefix before the profile.
        cache_key = f"{stage}-{digest({'policy': policy, 'schema': output_schema, 'as_of': self.as_of, 'role': role_context})[:40]}"
        def action(attempt):
            request = payload
            # One in-unit repair; a second rejection is a definite, paid failure.
            for repair in range(2):
                result = self.judge.complete(system=policy, payload=request, schema=output_schema, name=stage + "_judgment",
                                             cache_key=cache_key)
                try:
                    if stage == "gate":
                        validate_gate(result["value"], rubric)
                    else:
                        validate_relevance(result["value"])
                    return {**result, "validation_repairs": repair}
                except (ValueError, jsonschema.ValidationError) as error:
                    atomic_json(self.root / "rejected" / stage / f"{key}_{attempt}_{repair}.json", result)
                    if repair:
                        raise ValidationRejected("Judgment failed local validation after one repair") from None
                    reason = error.message if isinstance(error, jsonschema.ValidationError) else str(error)
                    request = {**payload, "previous_draft": result["value"], "repair_request": REPAIR_REQUEST.format(reason=reason)}
        with self._locks_guard:
            unit_lock = self._locks.setdefault((stage, key), Lock())
            warm_lock = self._warm.setdefault(cache_key, Lock())
        if cache_key not in self._warmed:
            # The first call per prefix runs alone so concurrent calls hit a warm cache.
            with warm_lock:
                if cache_key not in self._warmed:
                    with unit_lock:
                        result = execute_unit(self.root, stage, key, inputs, action, **self.retries)
                    self._warmed.add(cache_key)
                    return result
        with unit_lock:
            return execute_unit(self.root, stage, key, inputs, action, **self.retries)

    def evaluate(self, role: dict, rubric: dict, candidate: dict) -> dict:
        if candidate["duplicate_of_rank"] is not None:
            return {"status": "complete", "label": "duplicate", "relevance": 0.0,
                    "experience_verdict": None, "failed_criteria": [], "gate": None, "review": None}
        profile = candidate["profile"]
        gate = self._review("gate", role, rubric, profile)
        gate_verdict = gate["value"]["verdict"]
        if gate_verdict == "fail":
            return {"status": "complete", "label": "gate_fail", "relevance": 0.0, "gate_outcome": "fail",
                    "experience_verdict": None, "failed_criteria": gate["value"]["failed_criteria"],
                    "implied_criteria": gate["value"]["implied_criteria"], "gate": gate, "review": None}
        review = self._review("relevance", role, rubric, profile)
        verdict = review["value"]["verdict"]
        overqualified = review["value"]["obviously_overqualified"]
        underqualified = review["value"]["underqualified"]
        label = ("experience_overqualified" if overqualified else "experience_underqualified" if underqualified
                 else f"experience_{verdict}")
        return {"status": "complete", "label": label,
                "relevance": candidate_credit(gate_verdict, verdict), "gate_outcome": gate_verdict, "experience_verdict": verdict,
                "obviously_overqualified": overqualified, "underqualified": underqualified, "failed_criteria": [],
                "implied_criteria": gate["value"]["implied_criteria"], "gate": gate, "review": review}
