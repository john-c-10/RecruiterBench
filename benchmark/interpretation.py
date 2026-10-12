"""Explicit user-selected interpretation rules; employer source stays unchanged."""
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


METROS = {
    **dict.fromkeys(("san francisco", "san jose", "sunnyvale", "palo alto", "mountain view", "san mateo", "san carlos", "foster city", "burlingame", "south san francisco", "san leandro", "santa clara", "redwood city", "menlo park", "alameda", "berkeley", "emeryville", "brisbane", "silicon valley"), "San Francisco Bay Area, California"),
    **dict.fromkeys(("new york city", "new york", "nyc", "brooklyn", "williamsburg", "jersey city"), "New York metropolitan area"),
    **dict.fromkeys(("boston", "cambridge"), "Greater Boston area, Massachusetts"),
    **dict.fromkeys(("washington, dc", "arlington, va"), "Washington–Arlington metropolitan area"),
    **dict.fromkeys(("seattle", "kent", "everett"), "Greater Seattle area, Washington"),
    **dict.fromkeys(("los angeles", "hawthorne", "el segundo", "torrance", "norwalk"), "Greater Los Angeles area, California"),
}

# A metro mapping for a city name shared by other places applies only when the
# label carries this qualifier, e.g. "Norwalk, CA" but not "Norwalk, CT".
METRO_QUALIFIERS = {"cambridge": ("ma", "massachusetts"), "kent": ("wa", "washington"),
                    "everett": ("wa", "washington"), "norwalk": ("ca", "california")}
# Bare names that denote different metros; broadening one requires a qualifier.
AMBIGUOUS_CITIES = ("cambridge", "everett", "kent", "norwalk", "portland", "vancouver", "waterloo")

# These are city labels observed in the frozen corpus. Country/state/time-zone
# names are deliberately absent, so broadening cannot erase those boundaries.
CITIES = ("amsterdam", "ann arbor", "atlanta", "austin", "barcelona", "berlin", "bethesda", "bogotá", "boulder", "brighton", "bristol", "brussels", "buenos aires", "brownsville", "childress", "chicago", "dallas", "delft", "denver", "detroit", "el paso", "helsinki", "houston", "idaho falls", "irvine", "irving", "kitchener", "kitengela", "lima", "linz", "london", "mexico city", "miami", "mombasa", "montreal", "munich", "nairobi", "new haven", "paris", "philadelphia", "phoenix", "portland", "princeton", "pune", "rabat", "raleigh", "salt lake city", "seoul", "shanghai", "stockholm", "stockton", "thika", "toronto", "toulouse", "vancouver", "warsaw", "waterloo", "weirton", "whitwell", "zurich", "petaluma", "bridport", "cambridge", "everett", "kent", "norwalk")


def broaden_location(label: str) -> str:
    value = label.strip()
    if re.search(r"\b(?:area|region|metropolitan|timezone|time zone)\b", value, re.I):
        return value
    # Preserve qualified country/state scopes and geographic exclusions.
    if value.lower() == "new york state":
        return value
    value = re.sub(r"^central\s+(?=london)", "", value, flags=re.I)
    folded = value.casefold()
    if re.sub(r"\s*(?:office|\(.*?\))\s*", " ", folded).strip(" ,") in AMBIGUOUS_CITIES:
        raise ValueError(f"Ambiguous city label {label!r}; qualify it with a state/province or country first")
    for city, metro in sorted(METROS.items(), key=lambda item: -len(item[0])):
        if re.match(re.escape(city) + r"(?:\b|$)", folded):
            qualifiers = METRO_QUALIFIERS.get(city)
            if qualifiers and not re.search(r"\b(?:" + "|".join(qualifiers) + r")\b", folded[len(city):]):
                continue
            return metro
    for city in sorted(CITIES, key=len, reverse=True):
        if re.match(re.escape(city) + r"(?:\b|$)", folded):
            clean = re.sub(r"\s*(?:office|\(.*?\))\s*", " ", value, flags=re.I).strip(" ,")
            return "Greater " + clean + " area"
    return value


def qualify_city(label: str, postal_addresses) -> str:
    """Qualify a bare ambiguous city from the posting's own postal metadata."""
    if label.strip().casefold() not in AMBIGUOUS_CITIES:
        return label
    matches = {", ".join(str(postal[key]) for key in ("addressLocality", "addressRegion", "addressCountry") if postal.get(key))
               for postal in postal_addresses if str(postal.get("addressLocality", "")).casefold() == label.strip().casefold()}
    return matches.pop() if len(matches) == 1 else label


def geographic_option(label: str) -> str | None:
    """Remove remote/hybrid mode placeholders, retaining actual named scopes."""
    lower = label.casefold().strip()
    if lower.startswith("hybrid candidates") or lower.startswith("customer farms"):
        return None
    if lower.startswith("remote"):
        if re.search(r"\b(?:unspecified|exceptional|candidates|subject to|no country)\b", lower):
            return None
        stripped = re.sub(r"^remote\s*(?:within\s+the\s+|within\s+|[-,;(]\s*)?", "", label, flags=re.I).strip(" ()-;,")
        if not stripped or stripped.casefold() in ("hq", "location"):
            return None
        return stripped
    return label


def dropped_location_options(location: dict, *, extra_locations=()) -> list[str]:
    return [label for label in [*location["allowed_locations"], *extra_locations] if not geographic_option(label)]


def apply_location_policy(location: dict, *, extra_locations=()) -> dict:
    names = []
    for label in [*location["allowed_locations"], *extra_locations]:
        option = geographic_option(label)
        if option:
            expanded = broaden_location(option)
            if expanded not in names:
                names.append(expanded)
    global_scope = any(name.casefold() in ("global", "worldwide", "anywhere") for name in names)
    required = bool(names) and not global_scope
    remote = location["workplace_type"] in ("remote", "mixed")
    if not required:
        requirement = "No geographic filter: " + ("the role explicitly allows global remote work." if global_scope else "no concrete eligible location is stated; use the less restrictive interpretation.")
    else:
        requirement = "Accept any one of these geographic alternatives (OR): " + "; ".join(names) + ". Specific cities include their surrounding general metropolitan area."
        if remote:
            requirement += " Remote candidates must currently be located in one of these areas; remote work does not remove this geographic condition."
        elif location["workplace_type"] == "hybrid":
            requirement += " Hybrid work is based in one of these areas."
        elif location["workplace_type"] == "onsite":
            requirement += " Onsite work is based in one of these areas."
        if not remote and location["relocation"] == "offered" and not location["current_residence_required"]:
            requirement += " Relocation is offered; candidates outside these areas may qualify with explicit evidence of willingness to relocate. Current residence in the area is not mandatory."
        attendance = [quote for quote in location["source_quotes"] if re.search(r"(?:per|a|each) week|\d+x\s+per\s+week", quote, re.I)]
        if attendance:
            requirement += " Explicit work-attendance terms (city labels use the area interpretation above): " + " ".join(attendance)
    return {**location, "allowed_locations": names, "required": required,
            "current_residence_required": bool(required and (location["current_residence_required"] or remote)),
            "remote_scope": (" OR ".join(names) if required else "No geographic filter") if remote else location["remote_scope"],
            "requirement_text": requirement}
