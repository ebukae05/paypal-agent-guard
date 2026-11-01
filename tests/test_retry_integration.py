"""Route-level retry tests: the real PayPalClient behind the guard routes, with
httpx.MockTransport instead of the network (never the real sandbox, never .env).

These check what the FakePayPal in tests/test_main.py cannot: that a failure which
survived the client's retries is marked, and leaves no token or header in the record.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from guard import paypal_client
from guard.main import PURCHASES, app, get_paypal
from guard.paypal_client import MAX_ATTEMPTS, SANDBOX_BASE_URL, PayPalClient

client = TestClient(app)  # not a context manager: the lifespan never runs

ORDERS = "/v2/checkout/orders"
CAPTURE = "/v2/checkout/orders/ORDER-1/capture"


class Server:
    """MockTransport handler. failures[path] = "timeout", "connect" or "503"."""

    def __init__(self):
        self.requests = []
        self.failures = {}

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        failure = self.failures.get(path)
        # Exceptions carry the request (and so its headers), like real httpx errors.
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if failure == "503":
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "debug_id": "dbg-9"})
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token-xyz", "expires_in": 32400})
        if path == ORDERS:
            return httpx.Response(
                201,
                json={
                    "id": "ORDER-1",
                    "status": "CREATED",
                    "links": [{"rel": "payer-action", "href": "https://sandbox.example/a"}],
                },
            )
        if path == CAPTURE:
            return httpx.Response(
                201,
                json={
                    "id": "ORDER-1",
                    "status": "COMPLETED",
                    "purchase_units": [{"payments": {"captures": [{"id": "CAP-1"}]}}],
                },
            )
        return httpx.Response(404, json={"name": "NOT_FOUND"})

    def calls_to(self, path):
        return [r for r in self.requests if r.url.path == path]


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(paypal_client, "load_dotenv", lambda: None)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")
    server = Server()
    paypal = PayPalClient()
    paypal.http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(server))
    paypal._sleep = lambda seconds: None  # no real waits
    app.dependency_overrides[get_paypal] = lambda: paypal
    PURCHASES.clear()
    yield server
    app.dependency_overrides.clear()
    PURCHASES.clear()


def purchase(request_id="req-1"):
    return {
        "request_id": request_id,
        "user_request": "running shoes under $100, size 10",
        "item": {
            "product_id": "shoe-001",
            "name": "Road Runner 2",
            "size": "10",
            "price": "99.00",
            "merchant": "RunShop",
        },
    }


def assert_no_secrets(text):
    for leaked in ("token-xyz", "Bearer", "Authorization", "fake-client-secret"):
        assert leaked not in text


def test_create_503_exhausted_is_502_then_same_request_id_succeeds(server):
    server.failures[ORDERS] = "503"
    response = client.post("/purchase-requests", json=purchase())
    assert response.status_code == 502
    assert response.json()["debug_id"] == "dbg-9"
    record = PURCHASES["req-1"]
    assert record["status"] == "create_failed"
    assert_no_secrets(record["last_error"])

    del server.failures[ORDERS]
    response = client.post("/purchase-requests", json=purchase())
    assert response.status_code == 200
    assert record["status"] == "order_created"
    assert record["last_error"] is None
    ids = [r.headers["PayPal-Request-Id"] for r in server.calls_to(ORDERS)]
    assert ids == ["req-1"] * (MAX_ATTEMPTS + 1)


def test_create_connect_error_exhausted_marks_create_failed(server):
    server.failures[ORDERS] = "connect"
    response = client.post("/purchase-requests", json=purchase())
    assert response.status_code == 502
    record = PURCHASES["req-1"]
    assert record["status"] == "create_failed"
    assert_no_secrets(record["last_error"])
    assert len(server.calls_to(ORDERS)) == MAX_ATTEMPTS


def test_capture_timeout_exhausted_is_504_and_record_has_no_secrets(server):
    client.post("/purchase-requests", json=purchase())
    server.failures[CAPTURE] = "timeout"
    response = client.post("/purchase-requests/req-1/capture")
    assert response.status_code == 504
    record = PURCHASES["req-1"]
    assert record["status"] == "capture_failed"
    assert record["last_error"]
    assert_no_secrets(record["last_error"])
    assert_no_secrets(response.text)

    del server.failures[CAPTURE]
    response = client.post("/purchase-requests/req-1/capture")
    assert response.status_code == 200
    assert record["status"] == "captured"
    ids = [r.headers["PayPal-Request-Id"] for r in server.calls_to(CAPTURE)]
    assert ids == ["req-1-capture"] * (MAX_ATTEMPTS + 1)
