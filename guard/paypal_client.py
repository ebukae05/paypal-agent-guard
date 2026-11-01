"""PayPal Orders v2 client for the guard service (sandbox only).

Only the guard holds PayPal credentials (DESIGN.md, Agent permissions).
This module never prints or logs the client secret or the access token.
"""

import logging
import os
import time

import httpx
from dotenv import load_dotenv

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

# Retry policy for PayPal 5xx, timeouts and network errors (DESIGN.md, Risks).
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 1.0)  # wait before attempt 2 and attempt 3
TIMEOUT_SECONDS = 8.0
# Worst case per call: 3 * 8 + 0.5 + 1.0 = 25.5 s, under the agent's 30 s
# GUARD_TIMEOUT (agent/settings.py), so the guard answers before the agent gives up.

logger = logging.getLogger("guard.paypal")


class PayPalError(Exception):
    """A PayPal API call failed. Holds only safe details, never headers or tokens."""

    def __init__(self, status_code, name, message, debug_id, issue=None):
        self.status_code = status_code
        self.name = name
        self.debug_id = debug_id
        # Specific reason from details[0].issue, e.g. "ORDER_NOT_APPROVED".
        self.issue = issue
        super().__init__(f"PayPal {status_code} {name}/{issue}: {message} (debug_id={debug_id})")


def _debug_id(response):
    """PayPal's debug_id from an error body, or None if the body is not JSON."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("debug_id") if isinstance(body, dict) else None


def approval_link(order):
    """Return the URL where the buyer approves the order."""
    for link in order.get("links", []):
        if link["rel"] in ("payer-action", "approve"):
            return link["href"]
    raise ValueError("Order has no approval link")


class PayPalClient:
    def __init__(self):
        load_dotenv()
        self.client_id = self._require_env("PAYPAL_CLIENT_ID")
        self.client_secret = self._require_env("PAYPAL_CLIENT_SECRET")
        env = os.getenv("PAYPAL_ENV", "sandbox")
        if env != "sandbox":
            raise ValueError(f"PAYPAL_ENV must be 'sandbox', got {env!r}")
        self.http = httpx.Client(base_url=SANDBOX_BASE_URL, timeout=TIMEOUT_SECONDS)
        self._token = None
        self._token_expires_at = 0.0
        # Tests replace this so no test sleeps for real.
        self._sleep = time.sleep

    @staticmethod
    def _require_env(name):
        value = os.getenv(name)
        if not value:
            raise ValueError(f"Missing environment variable {name} (set it in .env)")
        return value

    def _get_access_token(self):
        # Reuse the cached token until 60 seconds before it expires.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        response = self._send(
            "POST",
            "/v1/oauth2/token",
            authed=False,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
        )
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.time() + body["expires_in"]
        return self._token

    def _headers(self, request_id=None):
        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id:
            # PayPal stores the response for this ID; a retry with the same ID
            # gets the original result back instead of doing the action twice.
            headers["PayPal-Request-Id"] = request_id
        return headers

    def _send(self, method, url, *, request_id=None, authed=True, **kwargs):
        """Send one PayPal request, retrying 5xx, timeouts and network errors.

        Only calls that are safe to repeat are retried: GETs, the OAuth token
        call, and POSTs that carry a PayPal-Request-Id (PayPal returns the
        stored result instead of acting twice). Any other POST gets one attempt.
        4xx responses are never retried. When attempts run out, the last 5xx
        becomes a PayPalError and the last network error is re-raised as is.
        """
        safe_to_repeat = method == "GET" or request_id is not None or not authed
        attempts = MAX_ATTEMPTS if safe_to_repeat else 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._sleep(BACKOFF_SECONDS[attempt - 2])
            # Built per attempt so an expired token is refreshed between attempts.
            headers = self._headers(request_id) if authed else None
            try:
                response = self.http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:  # includes httpx.TimeoutException
                if attempt == attempts:
                    raise
                self._log_retry(attempt, method, url, type(exc).__name__, None)
                continue
            if response.status_code >= 500 and attempt < attempts:
                self._log_retry(attempt, method, url, response.status_code, _debug_id(response))
                continue
            self._check(response)
            return response
        raise AssertionError("unreachable: the loop always returns or raises")

    @staticmethod
    def _log_retry(attempt, method, url, reason, debug_id):
        # Never log headers, request body or the token.
        logger.warning(
            "PayPal attempt %d/%d failed: %s %s -> %s (debug_id=%s); retrying",
            attempt,
            MAX_ATTEMPTS,
            method,
            url,
            reason,
            debug_id,
        )

    @staticmethod
    def _check(response):
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        details = body.get("details") or [{}]
        raise PayPalError(
            response.status_code,
            body.get("name") or body.get("error", "UNKNOWN"),
            body.get("message") or body.get("error_description", ""),
            body.get("debug_id"),
            details[0].get("issue"),
        )

    def create_order(self, amount, currency, request_id, description):
        """Create a CAPTURE order. `amount` is a decimal string like "1.00"."""
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "description": description,
                    "amount": {"currency_code": currency, "value": amount},
                }
            ],
        }
        response = self._send("POST", "/v2/checkout/orders", request_id=request_id, json=payload)
        return response.json()

    def get_order(self, order_id):
        response = self._send("GET", f"/v2/checkout/orders/{order_id}")
        return response.json()

    def capture_order(self, order_id, request_id):
        """Capture an approved order. Same request_id on retry = no double charge."""
        response = self._send(
            "POST", f"/v2/checkout/orders/{order_id}/capture", request_id=request_id
        )
        return response.json()
