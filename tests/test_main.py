"""Tests for the guard stub. A fake PayPal client stands in for the sandbox."""

import httpx
import pytest
from fastapi.testclient import TestClient

from guard.main import PURCHASES, app, get_paypal
from guard.paypal_client import PayPalError

# Not used as a context manager, so the lifespan (real PayPalClient) never runs.
client = TestClient(app)


class FakePayPal:
    """Records every call; set create_error or capture_error to make the next calls fail."""

    def __init__(self):
        self.create_calls = []
        self.capture_calls = []
        self.create_error = None
        self.capture_error = None

    def create_order(self, amount, currency, request_id, description):
        self.create_calls.append(
            {"amount": amount, "currency": currency, "request_id": request_id}
        )
        if self.create_error:
            raise self.create_error
        return {
            "id": f"ORDER-{request_id}",
            "status": "CREATED",
            "links": [{"rel": "approve", "href": "https://sandbox.example/approve"}],
        }

    def capture_order(self, order_id, request_id):
        self.capture_calls.append({"order_id": order_id, "request_id": request_id})
        if self.capture_error:
            raise self.capture_error
        return {
            "id": order_id,
            "status": "COMPLETED",
            "payer": {"email_address": "buyer@example.test"},
            "purchase_units": [
                {"payments": {"captures": [{"id": "CAPTURE-1", "status": "COMPLETED"}]}}
            ],
        }


@pytest.fixture
def fake():
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    PURCHASES.clear()
    yield fake
    app.dependency_overrides.clear()
    PURCHASES.clear()


def purchase(request_id="req-1", price="99.00", size="10"):
    return {
        "request_id": request_id,
        "user_request": "running shoes under $100, size 10",
        "item": {
            "product_id": "shoe-001",
            "name": "Road Runner 2",
            "size": size,
            "price": price,
            "merchant": "RunShop",
        },
    }


def test_health(fake):
    response = client.get("/health")
    assert response.status_code == 200


def test_price_with_three_decimals_is_rejected(fake):
    response = client.post("/purchase-requests", json=purchase(price="99.999"))
    assert response.status_code == 422
    assert fake.create_calls == []


def test_price_sent_to_paypal_as_two_decimal_string(fake):
    response = client.post("/purchase-requests", json=purchase(price="99"))
    assert response.status_code == 200
    assert fake.create_calls[0]["amount"] == "99.00"


def test_capture_unknown_request_is_404(fake):
    response = client.post("/purchase-requests/nope/capture")
    assert response.status_code == 404
    assert fake.capture_calls == []


def test_capture_twice_calls_paypal_once(fake):
    client.post("/purchase-requests", json=purchase())
    first = client.post("/purchase-requests/req-1/capture")
    second = client.post("/purchase-requests/req-1/capture")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["status"] == "COMPLETED"
    assert len(fake.capture_calls) == 1


def test_same_request_id_different_payload_is_409(fake):
    client.post("/purchase-requests", json=purchase(size="10"))
    response = client.post("/purchase-requests", json=purchase(size="11"))
    assert response.status_code == 409
    assert len(fake.create_calls) == 1


def test_capture_before_approval_is_409(fake):
    client.post("/purchase-requests", json=purchase())
    fake.capture_error = PayPalError(
        422, "UNPROCESSABLE_ENTITY", "not approved", "debug-1", "ORDER_NOT_APPROVED"
    )
    response = client.post("/purchase-requests/req-1/capture")
    assert response.status_code == 409
    assert response.json()["paypal_issue"] == "ORDER_NOT_APPROVED"


def test_paypal_timeout_is_504(fake):
    fake.create_error = httpx.ReadTimeout("timed out")
    response = client.post("/purchase-requests", json=purchase())
    assert response.status_code == 504


def test_capture_retry_after_failure_sends_same_key(fake):
    client.post("/purchase-requests", json=purchase())
    fake.capture_error = httpx.ReadTimeout("timed out")
    assert client.post("/purchase-requests/req-1/capture").status_code == 504
    fake.capture_error = None
    assert client.post("/purchase-requests/req-1/capture").status_code == 200
    keys = [call["request_id"] for call in fake.capture_calls]
    assert keys == ["req-1-capture", "req-1-capture"]
