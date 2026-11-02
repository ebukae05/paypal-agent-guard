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
    MIN_TRY_SECONDS,
    PAYPAL_BUDGET_SECONDS,
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


class FakeClock:
    """Simulated time for the client's deadline. Nothing really waits."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakePayPalServer:
    """Records requests and answers like PayPal. Override replies per path to fail.

    A reply can be one httpx.Response or exception (used for every call), or a
    list of them consumed in order; once the list is empty the normal reply is used.
    A reply can also be a function of the request that returns a Response.

    Simulated time (make_client sets `clock`): a timeout exception costs the
    timeout the client passed for that try; any other answer costs delays[path]
    seconds, and if that is longer than the try's timeout it becomes a timeout.
    """

    def __init__(self, expires_in=32400):
        self.requests = []
        self.expires_in = expires_in
        self.token_count = 0
        self.replies = {}  # path -> httpx.Response, exception, function, or a list of them
        self.delays = {}  # path -> seconds each answer takes (simulated)
        self.clock = None
        self.timeouts = []  # (path, clock at send, timeout passed for that try)

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        timeout = request.extensions["timeout"]
        try_timeout = timeout["read"]
        assert set(timeout.values()) == {try_timeout}  # one number for every phase
        if self.clock is not None:
            self.timeouts.append((path, self.clock.now, try_timeout))
        reply = self.replies.get(path)
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else None
        if isinstance(reply, httpx.TimeoutException):
            self._advance(try_timeout)
            raise reply
        delay = self.delays.get(path, 0.0)
        if delay > try_timeout:
            self._advance(try_timeout)
            raise httpx.ReadTimeout("timed out (simulated slow answer)")
        self._advance(delay)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(request)
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

    def _advance(self, seconds):
        if self.clock is not None:
            self.clock.now += seconds

    def api_requests(self):
        return [r for r in self.requests if r.url.path != "/v1/oauth2/token"]

    def calls_to(self, path):
        return [r for r in self.requests if r.url.path == path]


def make_client(server):
    client = PayPalClient()
    client.http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(server))
    # Fake clock: time only moves when the client waits or the server says so.
    client.clock = FakeClock()
    client._clock = client.clock
    server.clock = client.clock
    # Record retry waits instead of sleeping, so tests stay fast.
    client.waits = []

    def fake_sleep(seconds):
        client.waits.append(seconds)
        client.clock.now += seconds

    client._sleep = fake_sleep
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


def test_post_with_empty_request_id_is_not_retried():
    # "" sends no PayPal-Request-Id header, so it must count as "no ID": one try only.
    server = FakePayPalServer()
    server.replies[CAPTURE] = unavailable()
    client = make_client(server)

    with pytest.raises(PayPalError):
        client.capture_order("ORDER-1", "")

    (request,) = server.calls_to(CAPTURE)
    assert "PayPal-Request-Id" not in request.headers
    assert client.waits == []


def test_worst_case_retry_time_fits_the_agent_timeout():
    # 30 = GUARD_TIMEOUT in agent/settings.py. Not imported: that module calls
    # load_dotenv() at import time and would read .env.
    agent_timeout = 30
    assert PAYPAL_BUDGET_SECONDS < agent_timeout
    # Simulated: token cached, every order try times out after its full timeout.
    server = FakePayPalServer()
    client = make_client(server)
    client.get_order("ORDER-1")  # caches the token
    server.replies[ORDERS] = httpx.ReadTimeout("t")
    start = client.clock.now

    with pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-r", "shoes")

    elapsed = client.clock.now - start
    assert elapsed <= PAYPAL_BUDGET_SECONDS, f"guard needs {elapsed} s in the worst case"


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


def test_worst_case_including_token_fetch_fits_the_agent_timeout():
    # Simulated clock: each timed-out try costs the timeout the client gave it and
    # each wait costs what the client asks for. Token: 2 timeouts then success.
    # Order: always times out. This happens on the first purchase after start or
    # token expiry. Before the shared deadline this took 43 s.
    agent_timeout = 30  # GUARD_TIMEOUT in agent/settings.py (not imported: reads .env)
    server = FakePayPalServer()
    server.replies[TOKEN] = [httpx.ReadTimeout("t"), httpx.ReadTimeout("t")]
    server.replies[ORDERS] = httpx.ReadTimeout("t")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-r", "shoes")

    assert len(server.calls_to(TOKEN)) == 3
    assert len(server.calls_to(ORDERS)) >= 1  # the order call was still tried
    elapsed = client.clock.now
    assert elapsed <= PAYPAL_BUDGET_SECONDS < agent_timeout, f"guard needs {elapsed} s"


# --- One time budget per guard request (token fetch and order tries share it) ---


def test_token_expiring_during_retries_is_refetched_within_the_same_deadline():
    # Token fetch takes 4 s, each order try 5 s. The first order try fails with a
    # 503 and the token runs out at that moment, so the retry must fetch token-2
    # first. That fetch uses the same deadline, so the second order try only gets
    # the time that is left: 20 - 13.5 = 6.5 s instead of 8 s.
    server = FakePayPalServer()
    client = make_client(server)

    def expire_token_and_fail(request):
        client._token_expires_at = 0.0
        return unavailable()

    server.delays[TOKEN] = 4.0
    server.delays[ORDERS] = 5.0
    server.replies[ORDERS] = [expire_token_and_fail]

    assert client.create_order("1.00", "USD", "req-exp", "shoes") == ORDER_REPLY

    assert server.token_count == 2
    orders = server.calls_to(ORDERS)
    assert [r.headers["PayPal-Request-Id"] for r in orders] == ["req-exp", "req-exp"]
    assert [r.headers["Authorization"] for r in orders] == ["Bearer token-1", "Bearer token-2"]
    assert server.timeouts == [
        (TOKEN, 0.0, TIMEOUT_SECONDS),
        (ORDERS, 4.0, TIMEOUT_SECONDS),
        (TOKEN, 9.5, TIMEOUT_SECONDS),
        (ORDERS, 13.5, 6.5),
    ]
    assert client.clock.now <= PAYPAL_BUDGET_SECONDS


def test_budget_running_out_stops_timeouts_early_with_the_last_error():
    # Token: one 8 s timeout, wait 0.5 s, then success (8.5 s used). Order: try 1
    # times out after 8 s (16.5 s), wait 0.5 s, try 2 gets the 3 s left and times
    # out at 20 s. A third try (wait 1 s + MIN_TRY_SECONDS) no longer fits.
    server = FakePayPalServer()
    server.replies[TOKEN] = [httpx.ReadTimeout("token timed out")]
    server.replies[ORDERS] = httpx.ReadTimeout("order timed out")
    client = make_client(server)

    with pytest.raises(httpx.ReadTimeout, match="order timed out"):
        client.create_order("1.00", "USD", "req-b", "shoes")

    orders = server.calls_to(ORDERS)
    assert len(orders) == 2 < MAX_ATTEMPTS
    assert [r.headers["PayPal-Request-Id"] for r in orders] == ["req-b", "req-b"]
    assert client.waits == [BACKOFF_SECONDS[0], BACKOFF_SECONDS[0]]  # token retry, order retry
    assert client.clock.now == PAYPAL_BUDGET_SECONDS


def test_budget_running_out_stops_5xx_early_with_paypal_error_and_debug_id():
    # Token 6 s, each order try 6 s and a 503. After try 2 (18.5 s) only 1.5 s is
    # left, less than the next wait plus MIN_TRY_SECONDS, so the 503 is raised.
    server = FakePayPalServer()
    server.delays[TOKEN] = 6.0
    server.delays[ORDERS] = 6.0
    server.replies[ORDERS] = unavailable("dbg-late")
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.create_order("1.00", "USD", "req-b", "shoes")

    assert exc_info.value.status_code == 503
    assert exc_info.value.debug_id == "dbg-late"
    assert len(server.calls_to(ORDERS)) == 2 < MAX_ATTEMPTS
    assert client.clock.now <= PAYPAL_BUDGET_SECONDS


def test_per_try_timeout_shrinks_to_the_time_left():
    # Token: 2 timeouts then success; order: always times out (the 43 s case).
    server = FakePayPalServer()
    server.replies[TOKEN] = [httpx.ReadTimeout("t"), httpx.ReadTimeout("t")]
    server.replies[ORDERS] = httpx.ReadTimeout("t")
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-t", "shoes")

    for path, sent_at, timeout in server.timeouts:
        left = PAYPAL_BUDGET_SECONDS - sent_at  # the call started at time 0
        assert timeout == min(TIMEOUT_SECONDS, left), (path, sent_at, timeout)
    assert server.timeouts == [
        (TOKEN, 0.0, 8.0),
        (TOKEN, 8.5, 8.0),
        (TOKEN, 17.5, 2.5),
        (ORDERS, 17.5, 2.5),
    ]


def test_cached_token_uses_no_budget_and_each_call_gets_a_fresh_deadline():
    server = FakePayPalServer()
    server.delays[TOKEN] = 7.0
    client = make_client(server)
    client.get_order("ORDER-1")  # fetches and caches the token (7 s)

    client.create_order("1.00", "USD", "req-c", "shoes")

    (path, _, timeout) = server.timeouts[-1]
    assert path == ORDERS
    assert timeout == TIMEOUT_SECONDS  # the earlier call's time does not carry over
    assert server.token_count == 1


def test_no_time_for_the_first_try_raises_timeout_without_sending():
    # Token: 2 timeouts, then success after 1 s at 18.5 s. Only 1.5 s is left,
    # less than MIN_TRY_SECONDS, so the order is never sent and the call fails
    # like a timeout (the route answers 504 and marks the record failed).
    server = FakePayPalServer()
    server.replies[TOKEN] = [httpx.ReadTimeout("t"), httpx.ReadTimeout("t")]
    server.delays[TOKEN] = 1.0
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException, match="budget used up"):
        client.create_order("1.00", "USD", "req-n", "shoes")

    assert server.token_count == 1  # the token did arrive ...
    assert server.calls_to(ORDERS) == []  # ... but the order was never sent
    assert PAYPAL_BUDGET_SECONDS - client.clock.now < MIN_TRY_SECONDS


def test_no_time_for_the_first_try_without_a_token_fetch():
    server = FakePayPalServer()
    client = make_client(server)

    with pytest.raises(httpx.TimeoutException, match="budget used up"):
        client._send("POST", TOKEN, authed=False, deadline=MIN_TRY_SECONDS - 0.1)

    assert server.requests == []


def test_token_refetch_eating_the_time_left_raises_the_last_order_error():
    # The token is cached before the call. Order try 1 (5 s) gets a 503 and the
    # token runs out. Before try 2 the token fetch times out once (8 s, at 13.5 s),
    # waits, and its second try only gets the 6 s left of the shared deadline; it
    # succeeds after 5 s: 19 s used, 1 s left. Order try 2 is not sent; the caller
    # gets the order's last 503, not a token error.
    server = FakePayPalServer()
    client = make_client(server)
    client.get_order("ORDER-1")  # caches token-1, takes no simulated time

    def expire_token_and_fail(request):
        client._token_expires_at = 0.0
        return unavailable("dbg-order")

    server.delays[ORDERS] = 5.0
    server.delays[TOKEN] = 5.0
    server.replies[ORDERS] = [expire_token_and_fail]
    server.replies[TOKEN] = [httpx.ReadTimeout("t")]

    with pytest.raises(PayPalError) as exc_info:
        client.create_order("1.00", "USD", "req-g", "shoes")

    assert exc_info.value.status_code == 503
    assert exc_info.value.debug_id == "dbg-order"
    assert len(server.calls_to(ORDERS)) == 1
    assert server.token_count == 2
    # The token fetch shares the order's deadline: its second try is cut to 6 s.
    token_tries = [(at, t) for path, at, t in server.timeouts if path == TOKEN]
    assert token_tries[-2:] == [(5.5, 8.0), (14.0, 6.0)]
    assert client.clock.now <= PAYPAL_BUDGET_SECONDS


def test_token_refetch_eating_the_time_left_reraises_the_last_network_error():
    # Same timeline as above, but order try 1 fails with a network error: that
    # error is re-raised as is (the route answers 502 and marks the record failed).
    server = FakePayPalServer()
    client = make_client(server)
    client.get_order("ORDER-1")

    def expire_token_and_drop(request):
        client._token_expires_at = 0.0
        raise httpx.ConnectError("order connection dropped")

    server.delays[ORDERS] = 5.0
    server.delays[TOKEN] = 5.0
    server.replies[ORDERS] = [expire_token_and_drop]
    server.replies[TOKEN] = [httpx.ReadTimeout("t")]

    with pytest.raises(httpx.ConnectError, match="order connection dropped"):
        client.create_order("1.00", "USD", "req-g", "shoes")

    assert len(server.calls_to(ORDERS)) == 1
    assert server.token_count == 2
    assert client.clock.now <= PAYPAL_BUDGET_SECONDS
