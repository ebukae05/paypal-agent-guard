"""PayPal Orders v2 client for the guard service (sandbox only).

Only the guard holds PayPal credentials (DESIGN.md, Agent permissions).
This module never prints or logs the client secret or the access token.
"""

import os
import time

import httpx
from dotenv import load_dotenv

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """A PayPal API call failed. Holds only safe details, never headers or tokens."""

    def __init__(self, status_code, name, message, debug_id):
        self.status_code = status_code
        self.name = name
        self.debug_id = debug_id
        super().__init__(f"PayPal {status_code} {name}: {message} (debug_id={debug_id})")


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
        self.http = httpx.Client(base_url=SANDBOX_BASE_URL, timeout=30)
        self._token = None
        self._token_expires_at = 0.0

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
        response = self.http.post(
            "/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
        )
        self._check(response)
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

    @staticmethod
    def _check(response):
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise PayPalError(
            response.status_code,
            body.get("name") or body.get("error", "UNKNOWN"),
            body.get("message") or body.get("error_description", ""),
            body.get("debug_id"),
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
        response = self.http.post(
            "/v2/checkout/orders", json=payload, headers=self._headers(request_id)
        )
        self._check(response)
        return response.json()

    def get_order(self, order_id):
        response = self.http.get(f"/v2/checkout/orders/{order_id}", headers=self._headers())
        self._check(response)
        return response.json()

    def capture_order(self, order_id, request_id):
        """Capture an approved order. Same request_id on retry = no double charge."""
        response = self.http.post(
            f"/v2/checkout/orders/{order_id}/capture", headers=self._headers(request_id)
        )
        self._check(response)
        return response.json()
