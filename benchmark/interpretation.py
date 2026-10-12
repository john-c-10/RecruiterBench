"""Benchmark interpretation rules applied to every rubric; employer source stays unchanged."""
import re

POLICY_VERSION = "role-interpretation-v1"
POLICY = {
    "version": POLICY_VERSION,
    "single_location": "one_primary_office_area_or_one_remote_eligibility_region",
    "relocation_offered": "widened_to_the_country_or_no_filter_when_hiring_from_anywhere_is_realistic",
    "specific_cities": "within_100_miles_of_city_or_metropolitan_area",
    "remote_with_named_geography": "candidate_must_be_in_named_geography",
    "source_precedence": "role_text_before_metadata",
    "experience_ranges": "lower_bound_only_no_hard_upper_bound",
    "remaining_ambiguity": "less_restrictive_source_supported_interpretation",
    "unknown_candidate_evidence": "stated_or_reasonably_implied_passes_otherwise_fail_hard_gate",
    "obvious_overqualification": "fail_final_experience_fit",
    "candidate_confirmation_criteria": "removed",
    "criteria_outside_judged_profile_fields": "removed_or_reduced_to_observable_part",
    "red_flags": "fail_only_when_stated_or_reasonably_implied",
    "experience_fit_evidence": "stated_or_reasonably_implied",
    "largely_unstated_must_haves": "demoted_to_preferences_or_reduced_to_observable_core",
}
METRO_SENTENCE = ("Specific cities and metropolitan areas also include any location within about 100 miles (160 km) of them; "
                  "states, countries, and regions require a location inside them.")

YEAR_RANGE = re.compile(r"\b(?:between\s+)?(\d+(?:\.\d+)?)\s*(?:[-–—]|to|and)\s*\d+(?:\.\d+)?\+?\s+(years?)\b", re.I)


def unbound_experience(text: str) -> str:
    """Only numeric year ranges change; dates, travel %, pay and hours do not."""
    return YEAR_RANGE.sub(lambda match: f"{match[1]}+ {match[2]}", text)
