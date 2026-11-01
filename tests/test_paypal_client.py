"""Tests for guard/paypal_client.py with mocked HTTP. Never calls the real sandbox.

httpx.MockTransport routes every request to a Python function instead of the
network, so we can inspect the headers the client sends and choose the reply.
"""

import httpx
import pytest

from guard import paypal_client
from guard.paypal_client import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    SANDBOX_BASE_URL,
    TIMEOUT_SECONDS,
    PayPalClient,
    PayPalError,
)

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
    """Records requests and answers like PayPal. Override replies per path to fail.

    A reply can be one httpx.Response or exception (used for every call), or a
    list of them consumed in order; once the list is empty the normal reply is used.
    """

    def __init__(self, expires_in=32400):
        self.requests = []
        self.expires_in = expires_in
        self.token_count = 0
        self.replies = {}  # path -> httpx.Response, exception, or a list of them

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        reply = self.replies.get(path)
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else None
        if isinstance(reply, Exception):
            raise reply
        if reply is not None:
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

    def calls_to(self, path):
        return [r for r in self.requests if r.url.path == path]


def make_client(server):
    client = PayPalClient()
    client.http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(server))
    # Record retry waits instead of sleeping, so tests stay fast.
    client.waits = []
    client._sleep = client.waits.append
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
    assert len(server.api_requests()) == MAX_ATTEMPTS  # 5xx is retried, then raised


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
    assert len(server.api_requests()) == MAX_ATTEMPTS


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
    assert PayPalClient().http.timeout == httpx.Timeout(TIMEOUT_SECONDS)


def test_create_timeout_propagates_as_httpx_timeout():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders"] = httpx.ReadTimeout("timed out")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-1", "shoes")

    assert len(server.api_requests()) == MAX_ATTEMPTS


def test_capture_timeout_propagates_as_httpx_timeout():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders/ORDER-1/capture"] = httpx.ReadTimeout("timed out")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.capture_order("ORDER-1", "req-1")

    assert len(server.api_requests()) == MAX_ATTEMPTS


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


# --- Tester additions (gaps found by mutation testing) ---


def test_client_targets_the_paypal_sandbox_host():
    # Uses a literal, not SANDBOX_BASE_URL, so pointing the client at the live
    # host (https://api-m.paypal.com) fails this test.
    client = PayPalClient()

    assert str(client.http.base_url).rstrip("/") == "https://api-m.sandbox.paypal.com"


def test_capture_retry_after_timeout_reuses_the_same_request_id():
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders/ORDER-1/capture"] = httpx.ReadTimeout("timed out")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.capture_order("ORDER-1", "req-cap")
    del server.replies["/v2/checkout/orders/ORDER-1/capture"]
    result = client.capture_order("ORDER-1", "req-cap")

    assert result == CAPTURE_REPLY
    ids = [r.headers["PayPal-Request-Id"] for r in server.api_requests()]
    # The first call retries MAX_ATTEMPTS times, the second succeeds at once.
    assert ids == ["req-cap"] * (MAX_ATTEMPTS + 1)


# --- Retry on 5xx and timeouts (DESIGN.md, Risks: PayPal API error) ---

ORDERS = "/v2/checkout/orders"
CAPTURE = "/v2/checkout/orders/ORDER-1/capture"
TOKEN = "/v1/oauth2/token"


def unavailable(debug_id="dbg-503"):
    return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "debug_id": debug_id})


def test_create_503_then_success_retries_with_same_request_id():
    server = FakePayPalServer()
    server.replies[ORDERS] = [unavailable()]
    client = make_client(server)

    order = client.create_order("1.00", "USD", "req-r", "shoes")

    assert order == ORDER_REPLY
    ids = [r.headers["PayPal-Request-Id"] for r in server.calls_to(ORDERS)]
    assert ids == ["req-r", "req-r"]


def test_create_503_every_time_raises_paypal_error_after_max_attempts():
    server = FakePayPalServer()
    server.replies[ORDERS] = unavailable("dbg-x")
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.create_order("1.00", "USD", "req-r", "shoes")

    assert exc_info.value.status_code == 503
    assert exc_info.value.debug_id == "dbg-x"
    assert len(server.calls_to(ORDERS)) == 3


def test_capture_timeout_every_time_raises_timeout_after_max_attempts():
    server = FakePayPalServer()
    server.replies[CAPTURE] = httpx.ReadTimeout("timed out")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.capture_order("ORDER-1", "req-cap")

    assert len(server.calls_to(CAPTURE)) == 3


@pytest.mark.parametrize("status", [422, 409])
def test_4xx_is_never_retried(status):
    server = FakePayPalServer()
    server.replies[CAPTURE] = httpx.Response(
        status,
        json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]},
    )
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.capture_order("ORDER-1", "req-cap")

    assert exc_info.value.status_code == status
    assert len(server.calls_to(CAPTURE)) == 1
    assert client.waits == []


def test_token_503_then_success_is_retried():
    server = FakePayPalServer()
    server.replies[TOKEN] = [unavailable()]
    client = make_client(server)

    client.get_order("ORDER-1")

    assert len(server.calls_to(TOKEN)) == 2
    (request,) = server.api_requests()
    assert request.headers["Authorization"] == "Bearer token-1"


def test_waits_between_attempts_follow_backoff():
    server = FakePayPalServer()
    server.replies[ORDERS] = unavailable()
    client = make_client(server)

    with pytest.raises(PayPalError):
        client.create_order("1.00", "USD", "req-r", "shoes")

    assert client.waits == [0.5, 1.0]


def test_post_without_request_id_is_not_retried():
    # A POST with no PayPal-Request-Id could act twice if repeated, so it gets one try.
    server = FakePayPalServer()
    server.replies[ORDERS] = unavailable()
    client = make_client(server)

    with pytest.raises(PayPalError):
        client._send("POST", ORDERS, json={})

    assert len(server.calls_to(ORDERS)) == 1
    assert client.waits == []


def test_worst_case_retry_time_fits_the_agent_timeout():
    # 30 = GUARD_TIMEOUT in agent/settings.py. Not imported: that module calls
    # load_dotenv() at import time and would read .env.
    agent_timeout = 30
    assert MAX_ATTEMPTS * TIMEOUT_SECONDS + sum(BACKOFF_SECONDS) < agent_timeout


# --- Tester additions for the retry task (gaps found by mutation testing) ---


def test_get_503_then_success_is_retried():
    # GETs are always safe to repeat, so a 5xx on get_order is retried.
    server = FakePayPalServer()
    server.replies["/v2/checkout/orders/ORDER-1"] = [unavailable()]
    client = make_client(server)

    assert client.get_order("ORDER-1") == ORDER_REPLY
    assert len(server.calls_to("/v2/checkout/orders/ORDER-1")) == 2


def test_connect_error_then_success_is_retried_with_same_request_id():
    # Network errors other than timeouts (any httpx.TransportError) are retried too.
    server = FakePayPalServer()
    server.replies[CAPTURE] = [httpx.ConnectError("connection refused")]
    client = make_client(server)

    assert client.capture_order("ORDER-1", "req-cap") == CAPTURE_REPLY
    ids = [r.headers["PayPal-Request-Id"] for r in server.calls_to(CAPTURE)]
    assert ids == ["req-cap", "req-cap"]


def test_connect_error_every_time_is_reraised_after_max_attempts():
    server = FakePayPalServer()
    server.replies[ORDERS] = httpx.ConnectError("connection refused")
    client = make_client(server)

    with pytest.raises(httpx.ConnectError):
        client.create_order("1.00", "USD", "req-r", "shoes")

    assert len(server.calls_to(ORDERS)) == MAX_ATTEMPTS


def test_headers_rebuilt_per_attempt_so_expired_token_is_refreshed():
    # expires_in=30 is inside the 60 s margin, so every header build needs a new
    # token. If headers were built once before the loop, the retry would reuse token-1.
    server = FakePayPalServer(expires_in=30)
    server.replies[ORDERS] = [unavailable()]
    client = make_client(server)

    client.create_order("1.00", "USD", "req-r", "shoes")

    auths = [r.headers["Authorization"] for r in server.calls_to(ORDERS)]
    assert auths == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.parametrize(
    "failure", [unavailable("dbg-log"), httpx.ReadTimeout("timed out")], ids=["5xx", "timeout"]
)
def test_retry_log_never_contains_headers_token_or_body(caplog, failure):
    server = FakePayPalServer()
    server.replies[ORDERS] = [failure]
    client = make_client(server)

    with caplog.at_level("WARNING", logger="guard.paypal"):
        client.create_order("1.00", "USD", "req-log", "unique-body-marker")

    retry_logs = [r for r in caplog.records if r.name == "guard.paypal"]
    assert len(retry_logs) == 1  # the retry is logged ...
    # ... but never with the token, the auth header, the secret or the request body.
    for leaked in (
        "token-1",
        "Bearer",
        "Authorization",
        "fake-client-secret",
        "unique-body-marker",
    ):
        assert leaked not in caplog.text


@pytest.mark.xfail(
    strict=True,
    reason="GAP: the OAuth token call has its own 3-attempt retry budget nested inside "
    "the order call, so one guard request can take ~43 s, over the agent's 30 s timeout",
)
def test_worst_case_including_token_fetch_fits_the_agent_timeout():
    # Simulated clock: each timed-out attempt costs TIMEOUT_SECONDS and each wait
    # costs what the client asks for. Token: 2 timeouts then success. Order: always
    # times out. This happens on the first purchase after start or token expiry.
    agent_timeout = 30  # GUARD_TIMEOUT in agent/settings.py (not imported: reads .env)
    server = FakePayPalServer()
    server.replies[TOKEN] = [httpx.ReadTimeout("t"), httpx.ReadTimeout("t")]
    server.replies[ORDERS] = httpx.ReadTimeout("t")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-r", "shoes")

    timed_out = 2 + len(server.calls_to(ORDERS))
    elapsed = timed_out * TIMEOUT_SECONDS + sum(client.waits)
    assert elapsed < agent_timeout, f"guard needs {elapsed} s in the worst case"
