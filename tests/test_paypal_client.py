"""Tests for guard/paypal_client.py with mocked HTTP. Never calls the real sandbox.

httpx.MockTransport routes every request to a Python function instead of the
network, so we can inspect the headers the client sends and choose the reply.
"""

import httpx
import pytest

from guard import paypal_client
from guard.paypal_client import SANDBOX_BASE_URL, PayPalClient, PayPalError

ORDER_REPLY = {
    "id": "ORDER-1",
    "status": "CREATED",
    "links": [{"rel": "payer-action", "href": "https://sandbox.example/approve"}],
}
CAPTURE_REPLY = {"id": "ORDER-1", "status": "COMPLETED"}


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    """Obviously fake credentials; load_dotenv is disabled so .env is never read."""
    monkeypatch.setattr(paypal_client, "load_dotenv", lambda: None)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")


class FakePayPalServer:
    """Records requests and answers like PayPal. Override replies per path to fail."""

    def __init__(self, expires_in=32400):
        self.requests = []
        self.expires_in = expires_in
        self.token_count = 0
        self.replies = {}  # path -> httpx.Response, or an exception to raise

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path in self.replies:
            reply = self.replies[path]
            if isinstance(reply, Exception):
                raise reply
            return reply
        if path == "/v1/oauth2/token":
            self.token_count += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{self.token_count}", "expires_in": self.expires_in},
            )
        if path == "/v2/checkout/orders":
            return httpx.Response(201, json=ORDER_REPLY)
        if path.endswith("/capture"):
            return httpx.Response(201, json=CAPTURE_REPLY)
        if path.startswith("/v2/checkout/orders/"):
            return httpx.Response(200, json=ORDER_REPLY)
        return httpx.Response(404, json={"name": "NOT_FOUND"})

    def api_requests(self):
        return [r for r in self.requests if r.url.path != "/v1/oauth2/token"]


def make_client(server):
    client = PayPalClient()
    client.http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(server))
    return client


# --- Token caching ---


def test_token_fetched_once_and_reused():
    server = FakePayPalServer()
    client = make_client(server)

    client.create_order("1.00", "USD", "req-1", "shoes")
    client.get_order("ORDER-1")
    client.capture_order("ORDER-1", "req-1")

    assert server.token_count == 1
    for request in server.api_requests():
        assert request.headers["Authorization"] == "Bearer token-1"


def test_token_request_uses_client_credentials_and_basic_auth():
    server = FakePayPalServer()
    client = make_client(server)

    client.get_order("ORDER-1")

    token_request = server.requests[0]
    assert token_request.method == "POST"
    assert token_request.url.path == "/v1/oauth2/token"
    assert token_request.headers["Authorization"].startswith("Basic ")
    assert token_request.content == b"grant_type=client_credentials"


def test_token_refreshed_when_inside_expiry_margin():
    # expires_in below the client's 60 s safety margin: the token counts as
    # expired right away, so every call must fetch a fresh one.
    server = FakePayPalServer(expires_in=30)
    client = make_client(server)

    client.get_order("ORDER-1")
    client.get_order("ORDER-1")

    assert server.token_count == 2
    auths = [r.headers["Authorization"] for r in server.api_requests()]
    assert auths == ["Bearer token-1", "Bearer token-2"]


def test_token_refreshed_after_expiry_time_passes():
    server = FakePayPalServer()
    client = make_client(server)
    client.get_order("ORDER-1")

    client._token_expires_at = 0.0  # simulate the cached token running out
    client.get_order("ORDER-1")

    assert server.token_count == 2


# --- PayPal-Request-Id (idempotency) ---


def test_create_order_sends_request_id_and_payload():
    server = FakePayPalServer()
    client = make_client(server)

    order = client.create_order("99.00", "USD", "req-abc", "running shoes")

    assert order == ORDER_REPLY
    (request,) = server.api_requests()
    assert request.method == "POST"
    assert request.url.path == "/v2/checkout/orders"
    assert request.headers["PayPal-Request-Id"] == "req-abc"
    assert request.headers["Prefer"] == "return=representation"
    body = httpx.Response(200, content=request.content).json()
    assert body["intent"] == "CAPTURE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "99.00"}


def test_capture_order_sends_request_id():
    server = FakePayPalServer()
    client = make_client(server)

    result = client.capture_order("ORDER-1", "req-abc")

    assert result == CAPTURE_REPLY
    (request,) = server.api_requests()
    assert request.method == "POST"
    assert request.url.path == "/v2/checkout/orders/ORDER-1/capture"
    assert request.headers["PayPal-Request-Id"] == "req-abc"


def test_retry_reuses_same_request_id():
    server = FakePayPalServer()
    client = make_client(server)

    client.create_order("1.00", "USD", "req-same", "shoes")
    client.create_order("1.00", "USD", "req-same", "shoes")

    ids = [r.headers["PayPal-Request-Id"] for r in server.api_requests()]
    assert ids == ["req-same", "req-same"]


def test_get_order_sends_no_request_id():
    server = FakePayPalServer()
    client = make_client(server)

    client.get_order("ORDER-1")

    (request,) = server.api_requests()
    assert "PayPal-Request-Id" not in request.headers


# --- Errors become PayPalError ---


def test_capture_decline_becomes_paypal_error_with_issue_and_debug_id():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders/ORDER-1/capture"] = httpx.Response(
        422,
        json={
            "name": "UNPROCESSABLE_ENTITY",
            "message": "The requested action could not be performed.",
            "debug_id": "dbg-123",
            "details": [{"issue": "ORDER_NOT_APPROVED"}],
        },
    )
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.capture_order("ORDER-1", "req-1")

    error = exc_info.value
    assert error.status_code == 422
    assert error.name == "UNPROCESSABLE_ENTITY"
    assert error.issue == "ORDER_NOT_APPROVED"
    assert error.debug_id == "dbg-123"
    assert "dbg-123" in str(error)


def test_create_error_without_details_has_no_issue():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders"] = httpx.Response(
        500, json={"name": "INTERNAL_SERVER_ERROR", "message": "oops", "debug_id": "dbg-500"}
    )
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.create_order("1.00", "USD", "req-1", "shoes")

    assert exc_info.value.status_code == 500
    assert exc_info.value.name == "INTERNAL_SERVER_ERROR"
    assert exc_info.value.issue is None
    assert exc_info.value.debug_id == "dbg-500"


def test_oauth_error_becomes_paypal_error():
    # The OAuth endpoint uses a different error shape: error / error_description.
    server = FakePayPalServer()
    server.replies["/v1/oauth2/token"] = httpx.Response(
        401, json={"error": "invalid_client", "error_description": "Client Authentication failed"}
    )
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.create_order("1.00", "USD", "req-1", "shoes")

    assert exc_info.value.status_code == 401
    assert exc_info.value.name == "invalid_client"
    assert exc_info.value.debug_id is None
    assert server.api_requests() == []  # no order call without a token


def test_non_json_error_body_becomes_unknown_paypal_error():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders"] = httpx.Response(502, text="<html>Bad Gateway</html>")
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.create_order("1.00", "USD", "req-1", "shoes")

    assert exc_info.value.status_code == 502
    assert exc_info.value.name == "UNKNOWN"
    assert exc_info.value.debug_id is None


def test_error_message_never_contains_secret_or_token():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders/ORDER-1/capture"] = httpx.Response(
        422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-1"}
    )
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.capture_order("ORDER-1", "req-1")

    assert "fake-client-secret" not in str(exc_info.value)
    assert "token-1" not in str(exc_info.value)


# --- Timeouts ---


def test_client_configures_a_timeout():
    assert PayPalClient().http.timeout == httpx.Timeout(30)


def test_create_timeout_propagates_as_httpx_timeout():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders"] = httpx.ReadTimeout("timed out")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-1", "shoes")


def test_capture_timeout_propagates_as_httpx_timeout():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders/ORDER-1/capture"] = httpx.ReadTimeout("timed out")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.capture_order("ORDER-1", "req-1")


# --- Configuration guards ---


def test_non_sandbox_env_is_refused(monkeypatch):
    monkeypatch.setenv("PAYPAL_ENV", "live")

    with pytest.raises(ValueError, match="sandbox"):
        PayPalClient()


@pytest.mark.parametrize("name", ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"])
def test_missing_credential_is_refused(monkeypatch, name):
    monkeypatch.delenv(name)

    with pytest.raises(ValueError, match=name):
        PayPalClient()
