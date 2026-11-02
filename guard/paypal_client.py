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
TIMEOUT_SECONDS = 8.0  # longest single try
# One time limit per guard request: create_order, capture_order and get_order
# each get PAYPAL_BUDGET_SECONDS in total, shared by the OAuth token fetch (if
# the cached token has run out) and every order/capture try and backoff wait.
PAYPAL_BUDGET_SECONDS = 20.0
# PayPal normally answers in under 2 s. A retry only starts if its backoff wait
# plus this much time still fits before the deadline.
MIN_TRY_SECONDS = 2.0
# What is guaranteed: no try (token or order) starts later than
# PAYPAL_BUDGET_SECONDS - MIN_TRY_SECONDS (18 s) after the public method is
# called, so no new PayPal request starts after the agent's 30 s GUARD_TIMEOUT
# (agent/settings.py). A try already in flight can run past its timeout: httpx
# timeouts apply per phase (connect, write, each gap between reads), so a try can
# take roughly 2x the time left, and a response that drips in slowly has no upper
# bound. That is tracked as its own task in TASKS.md. If the agent gives up while
# a request is in flight, that is safe: its retry reuses the same
# PayPal-Request-Id, so PayPal returns the stored result instead of acting twice.

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
        # Tests replace these so no test sleeps for real and time can be faked.
        self._sleep = time.sleep
        self._clock = time.monotonic

    @staticmethod
    def _require_env(name):
        value = os.getenv(name)
        if not value:
            raise ValueError(f"Missing environment variable {name} (set it in .env)")
        return value

    def _new_deadline(self):
        return self._clock() + PAYPAL_BUDGET_SECONDS

    def _get_access_token(self, deadline):
        # Reuse the cached token until 60 seconds before it expires (no call, no budget used).
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        # The token fetch shares the caller's deadline, so it cannot add its own
        # full retry budget on top of the order call's.
        response = self._send(
            "POST",
            "/v1/oauth2/token",
            authed=False,
            deadline=deadline,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
        )
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.time() + body["expires_in"]
        return self._token

    def _headers(self, request_id, deadline):
        headers = {
            "Authorization": f"Bearer {self._get_access_token(deadline)}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id:  # same check as safe_to_repeat in _send
            # PayPal stores the response for this ID; a retry with the same ID
            # gets the original result back instead of doing the action twice.
            headers["PayPal-Request-Id"] = request_id
        return headers

    def _send(self, method, url, *, request_id=None, authed=True, deadline=None, **kwargs):
        """Send one PayPal request, retrying 5xx, timeouts and network errors.

        Only calls that are safe to repeat are retried: GETs, the OAuth token
        call, and POSTs that carry a PayPal-Request-Id (PayPal returns the
        stored result instead of acting twice). Any other POST gets one attempt.
        4xx responses are never retried. When attempts or time run out, the last
        5xx becomes a PayPalError and the last network error is re-raised as is.

        All tries (and any token fetch) must finish by `deadline`, a value of
        self._clock(). Each try's timeout shrinks to the time left, and a retry
        only starts if its backoff wait plus MIN_TRY_SECONDS still fits.
        """
        if deadline is None:
            deadline = self._new_deadline()
        # Same truthiness as _headers: a POST is retried only if it carries the header.
        safe_to_repeat = method == "GET" or bool(request_id) or not authed
        attempts = MAX_ATTEMPTS if safe_to_repeat else 1
        last_failure = None  # the last 5xx response or network error
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._sleep(BACKOFF_SECONDS[attempt - 2])
            # Built per attempt so an expired token is refreshed between attempts.
            # A token fetch here uses up part of the same deadline.
            headers = self._headers(request_id, deadline) if authed else None
            left = deadline - self._clock()
            if left < MIN_TRY_SECONDS:
                if last_failure is None:
                    # Nothing has been tried yet but there is no time for a real try
                    # (e.g. the token fetch used up the budget). Fail like a timeout,
                    # so the route answers 504 and marks the record failed.
                    raise httpx.TimeoutException("PayPal time budget used up")
                # A token refetch before this retry ate the time left: stop here.
                self._give_up(last_failure)
            try:
                response = self.http.request(
                    method, url, headers=headers, timeout=min(TIMEOUT_SECONDS, left), **kwargs
                )
            except httpx.TransportError as exc:  # includes httpx.TimeoutException
                if not self._retry_fits(attempt, attempts, deadline):
                    raise
                last_failure = exc
                self._log_retry(attempt, method, url, type(exc).__name__, None)
                continue
            if response.status_code >= 500 and self._retry_fits(attempt, attempts, deadline):
                last_failure = response
                self._log_retry(attempt, method, url, response.status_code, _debug_id(response))
                continue
            self._check(response)
            return response
        raise AssertionError("unreachable: the loop always returns or raises")

    def _retry_fits(self, attempt, attempts, deadline):
        """True if another try is allowed and its wait plus a real try fit in the time left."""
        if attempt >= attempts:
            return False
        left = deadline - self._clock()
        return left >= BACKOFF_SECONDS[attempt - 1] + MIN_TRY_SECONDS

    def _give_up(self, last_failure):
        """Raise the last failure exactly as if attempts had run out."""
        if isinstance(last_failure, httpx.Response):
            self._check(last_failure)  # a 5xx, so this raises PayPalError with debug_id
        raise last_failure

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
        response = self._send(
            "POST",
            "/v2/checkout/orders",
            request_id=request_id,
            deadline=self._new_deadline(),
            json=payload,
        )
        return response.json()

    def get_order(self, order_id):
        response = self._send(
            "GET", f"/v2/checkout/orders/{order_id}", deadline=self._new_deadline()
        )
        return response.json()

    def capture_order(self, order_id, request_id):
        """Capture an approved order. Same request_id on retry = no double charge."""
        response = self._send(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            request_id=request_id,
            deadline=self._new_deadline(),
        )
        return response.json()
