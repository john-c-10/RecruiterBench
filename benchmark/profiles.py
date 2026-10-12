"""Normalize only the four allowed professional profile evidence surfaces."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from .io import canonical, digest

PROFILE_FIELDS = ("location", "educations", "experiences", "listed_skills")
DROP_KEYS = {"email", "emails", "phone", "phones", "phonenumber", "mobilephone", "personalemail", "workemail",
             "photo", "profilepicture", "profileimage", "matchscore", "percentmatch", "evaluation", "overallrating",
             "titlerelevance", "rank", "score", "relevancescore"}


def strip_private(value):
    if isinstance(value, dict):
        return {key: strip_private(child) for key, child in value.items()
                if key.lower().replace("_", "").replace("-", "") not in DROP_KEYS}
    if isinstance(value, list):
        return [strip_private(child) for child in value]
    return value


def array(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                value = parsed
        except ValueError:
            pass
    if value is None or value == "":
        return []
    if isinstance(value, list):
        output = []
        for item in value:
            if isinstance(item, str) and item.lstrip().startswith("{"):
                try:
                    item = json.loads(item)
                except ValueError:
                    pass
            output.append(strip_private(item))
        return output
    return [strip_private(value)]


def first(data, names):
    for name in names:
        value = data.get(name)
        if value is not None and value != "" and value != []:
            return value
    return None


def normalize_profile(candidate: dict) -> dict:
    data = candidate.get("candidateData") if isinstance(candidate.get("candidateData"), dict) else candidate
    profile = {"location": strip_private(first(data, ["location", "location_name", "locationName"])),
               "educations": array(first(data, ["educations", "education", "educationList", "educationRaw"])),
               "experiences": array(first(data, ["experiences", "experience", "employment", "experienceList"])),
               "listed_skills": array(first(data, ["listed_skills", "skills"]))}
    if len(canonical(profile).encode()) > 120000:
        raise ValueError("Profile exceeds the evidence budget; refusing silent truncation")
    return profile


def identity_keys(candidate: dict) -> list[str]:
    data = candidate.get("candidateData") if isinstance(candidate.get("candidateData"), dict) else candidate
    keys = set()
    for obj in [candidate, data]:
        for field in ["id", "personId", "person_id", "candidateId"]:
            value = obj.get(field)
            if type(value) in (str, int) and str(value).strip():
                keys.add("person:" + str(value).strip())
        for field in ["linkedinUrlStable", "linkedinUrl", "linkedin_url", "linkedin_flagship_url"]:
            value = obj.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            url = urlsplit(value if "://" in value else "https://" + value)
            host = (url.hostname or "").lower()
            if (host == "linkedin.com" or host.endswith(".linkedin.com")) and url.path.lower().startswith("/in/"):
                keys.add("linkedin:" + url.path.rstrip("/").lower())
    return sorted(keys)


def prepare_candidates(candidates: list[dict]) -> list[dict]:
    if len(candidates) > 200:
        raise ValueError("Search returned more than the requested 200 results")
    seen, rows = {}, []
    for rank, candidate in enumerate(candidates, 1):
        if not isinstance(candidate, dict):
            raise ValueError("Malformed search candidate")
        profile = normalize_profile(candidate)
        keys = identity_keys(candidate)
        if not keys:
            raise ValueError("Candidate has neither a stable person ID nor a LinkedIn profile URL")
        duplicate = min((seen[key] for key in keys if key in seen), default=None)
        for key in keys:
            seen.setdefault(key, duplicate or rank)
        rows.append({"rank": rank, "candidate_id": digest(keys)[:24], "identity_keys": keys,
                     "duplicate_of_rank": duplicate, "profile": profile, "profile_sha256": digest(profile),
                     "field_coverage": {key: value is not None and value != [] and value != "" for key, value in profile.items()}})
    return rows
