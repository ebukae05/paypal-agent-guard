"""Tester's loop 2 attack on the retry rule after review (docs/REVIEW.md, 6265afb).

Rule under test: a POST is retried if and only if the PayPal-Request-Id header is
actually on the wire. GETs and the OAuth token call are always retried. Every
retry of a POST carries the same PayPal-Request-Id as the first try.

Self-contained MockTransport fake; nothing reaches the network or reads .env.
"""

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from guard import paypal_client
from guard.paypal_client import MAX_ATTEMPTS, SANDBOX_BASE_URL, PayPalClient, PayPalError

TOKEN = "/v1/oauth2/token"
ORDERS = "/v2/checkout/orders"
ORDER_GET = "/v2/checkout/orders/ORDER-1"
CAPTURE = "/v2/checkout/orders/ORDER-1/capture"

FALSY_IDS = [None, "", 0, False, b""]
TRUTHY_IDS = ["req-1", "a", "x" * 108, "0", "False", "None", "req-1-capture"]


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setattr(paypal_client, "load_dotenv", lambda: None)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")


class Server:
    """Every path in `failing` answers 503; the token call succeeds unless listed."""

    def __init__(self, failing):
        self.failing = set(failing)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path in self.failing:
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "debug_id": "d"})
        if path == TOKEN:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 32400})
        if path == ORDERS:
            return httpx.Response(201, json={"id": "ORDER-1", "status": "CREATED"})
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "ORDER-1", "status": "COMPLETED"})
        return httpx.Response(200, json={"id": "ORDER-1", "status": "APPROVED"})

    def calls_to(self, path):
        return [r for r in self.requests if r.url.path == path]


class _NoNetworkClient:
    """Stand-in for the default httpx.Client during construction (skips the slow
    SSL setup); make_client swaps in a MockTransport client right after."""

    def __init__(self, **kwargs):
        pass


def make_client(server):
    real_client = paypal_client.httpx.Client
    paypal_client.httpx.Client = _NoNetworkClient  # type: ignore[misc,assignment]
    try:
        client = PayPalClient()
    finally:
        paypal_client.httpx.Client = real_client  # type: ignore[misc]
    client.http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(server))
    now = [0.0]
    client._clock = lambda: now[0]

    def sleep(seconds):
        now[0] += seconds

    client._sleep = sleep
    return client


def call(client, how, request_id):
    if how == "create":
        return client.create_order("1.00", "USD", request_id, "shoes")
    if how == "capture":
        return client.capture_order("ORDER-1", request_id)
    return client._send("POST", ORDERS, request_id=request_id, json={})


PATH_FOR = {"create": ORDERS, "capture": CAPTURE, "send": ORDERS}


def assert_retry_iff_header(how, request_id):
    path = PATH_FOR[how]
    server = Server(failing=[path])
    client = make_client(server)

    with pytest.raises(PayPalError):
        call(client, how, request_id)

    tries = server.calls_to(path)
    with_header = ["paypal-request-id" in r.headers for r in tries]
    # All tries agree on whether the header is there.
    assert len(set(with_header)) == 1
    if with_header[0]:
        assert len(tries) == MAX_ATTEMPTS
        assert {r.headers["PayPal-Request-Id"] for r in tries} == {str(request_id)}
    else:
        assert len(tries) == 1


@pytest.mark.parametrize("how", ["create", "capture", "send"])
@pytest.mark.parametrize("request_id", FALSY_IDS, ids=repr)
def test_falsy_request_id_sends_no_header_and_gets_one_try(how, request_id):
    path = PATH_FOR[how]
    server = Server(failing=[path])
    client = make_client(server)

    with pytest.raises(PayPalError):
        call(client, how, request_id)

    (only,) = server.calls_to(path)
    assert "paypal-request-id" not in only.headers


@pytest.mark.parametrize("how", ["create", "capture", "send"])
@pytest.mark.parametrize("request_id", TRUTHY_IDS, ids=repr)
def test_real_request_id_sends_header_on_every_try(how, request_id):
    path = PATH_FOR[how]
    server = Server(failing=[path])
    client = make_client(server)

    with pytest.raises(PayPalError):
        call(client, how, request_id)

    tries = server.calls_to(path)
    assert len(tries) == MAX_ATTEMPTS
    assert [r.headers.get("PayPal-Request-Id") for r in tries] == [request_id] * MAX_ATTEMPTS


ID_ALPHABET = st.characters(min_codepoint=0x21, max_codepoint=0x7E)


@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    how=st.sampled_from(["create", "capture", "send"]),
    request_id=st.one_of(
        st.sampled_from(FALSY_IDS), st.text(alphabet=ID_ALPHABET, min_size=0, max_size=108)
    ),
)
def test_property_post_retried_iff_header_on_the_wire(how, request_id):
    assert_retry_iff_header(how, request_id)


@pytest.mark.parametrize("request_id", FALSY_IDS + ["req-1"], ids=repr)
def test_get_is_always_retried(request_id):
    server = Server(failing=[ORDER_GET])
    client = make_client(server)

    with pytest.raises(PayPalError):
        client._send("GET", ORDER_GET, request_id=request_id)

    assert len(server.calls_to(ORDER_GET)) == MAX_ATTEMPTS


def test_get_order_is_retried_and_sends_no_request_id():
    server = Server(failing=[ORDER_GET])
    client = make_client(server)

    with pytest.raises(PayPalError):
        client.get_order("ORDER-1")

    tries = server.calls_to(ORDER_GET)
    assert len(tries) == MAX_ATTEMPTS
    assert all("paypal-request-id" not in r.headers for r in tries)


@pytest.mark.parametrize("how", ["create", "capture"])
def test_token_call_is_retried_even_when_the_order_post_has_no_id(how):
    # The token POST has no PayPal-Request-Id but is safe to repeat (no side effect).
    server = Server(failing=[TOKEN])
    client = make_client(server)

    with pytest.raises(PayPalError):
        call(client, how, "")

    tries = server.calls_to(TOKEN)
    assert len(tries) == MAX_ATTEMPTS
    assert all("paypal-request-id" not in r.headers for r in tries)
    assert server.calls_to(PATH_FOR[how]) == []
