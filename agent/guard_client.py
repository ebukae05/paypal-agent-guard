"""HTTP client for the guard: the agent's only path to a purchase.

Uses plain httpx. The anthropic SDK uses httpx2 internally; objects from the
two libraries are never mixed.
"""

import httpx

from agent.settings import GUARD_TIMEOUT, GUARD_URL


class GuardError(Exception):
    """The guard could not give a decision (timeout, unreachable, or HTTP error)."""


def post_purchase(payload):
    """POST /purchase-requests and return the decision JSON, or raise GuardError."""
    url = f"{GUARD_URL}/purchase-requests"
    try:
        response = httpx.post(url, json=payload, timeout=GUARD_TIMEOUT)
    except httpx.TimeoutException:
        raise GuardError("The guard timed out. Retrying the same product is safe.")
    except httpx.TransportError:
        raise GuardError(f"Could not reach the guard at {GUARD_URL}.")

    if response.status_code != 200:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:200]
        raise GuardError(f"The guard returned HTTP {response.status_code}: {detail}")
    return response.json()
