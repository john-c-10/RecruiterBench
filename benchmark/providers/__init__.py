"""Explicit network boundaries for model and search providers."""
from . import exa, wrangle

# Each search provider module exposes NAME, API_KEY_ENV, DEFAULT_BASE_URL,
# MODES, MAX_RESULTS, search_body(query, mode), and SearchClient(base_url).
SEARCH_PROVIDERS = {wrangle.NAME: wrangle, exa.NAME: exa}


def search_provider(name: str):
    if name not in SEARCH_PROVIDERS:
        raise ValueError("Unknown search provider; choose one of: " + ", ".join(SEARCH_PROVIDERS))
    return SEARCH_PROVIDERS[name]


def search_client(name: str, base_url: str):
    return search_provider(name).SearchClient(base_url)
