"""Parallel Entity Search called explicitly through Treg, without a provider waterfall."""
from benchmark.providers.openai import ProviderError
from tools import treg_provider

NAME = "treg_parallel"
API_KEY_ENV = treg_provider.API_KEY_ENV
DEFAULT_BASE_URL = treg_provider.DEFAULT_BASE_URL
MODES = ("parallel",)
MAX_RESULTS = 100
ENDPOINT = "/call/parallel.people.search"


def search_body(query, mode):
    # Reuse the same validation; only the provider's transport shape differs.
    query = treg_provider.search_body(query, "routed")["q"]
    if mode not in MODES:
        raise ValueError("Invalid Treg Parallel mode")
    return {"entity_type": "people", "objective": query, "match_limit": MAX_RESULTS}


def normalize_receipt(receipt):
    if receipt["status"] != 200:
        return treg_provider.normalize_receipt(receipt)
    response = receipt.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("entities"), list):
        raise ProviderError("TREG_PARALLEL_RESPONSE_CONTRACT_REJECTED")
    people = response["entities"]
    if len(people) > MAX_RESULTS or not isinstance(response.get("entity_set_id"), str) or not response["entity_set_id"]:
        raise ProviderError("TREG_PARALLEL_RESPONSE_CONTRACT_REJECTED")
    # The native endpoint fixes the provider; reuse the existing evidence
    # normalization and receipt metadata without rewriting the saved response.
    adapted = {**receipt, "response": {"output": {"people": people}, "_treg": {
        "served_by": "parallel.people.search", "outcome": "hit" if people else "miss"}}}
    result = treg_provider.normalize_receipt(adapted)
    result["meta"]["entity_set_id"] = response["entity_set_id"]
    return result


class ParallelSearch(treg_provider.TregSearch):
    endpoint = ENDPOINT
    normalize = staticmethod(normalize_receipt)

    @staticmethod
    def validate_body(body):
        if body != search_body(body.get("objective"), "parallel"):
            raise ValueError("Search body differs from the frozen Treg Parallel contract")


SearchClient = ParallelSearch
