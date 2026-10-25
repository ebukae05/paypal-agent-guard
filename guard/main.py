"""Guard service: the only path from the shopping agent to PayPal.

M1 stub: every purchase request is confirmed. Later milestones slot the rules
engine, intent check, risk score and audit log in before the PayPal call
(DESIGN.md, Request lifecycle steps 2-4).

Run from the repo root:  uvicorn guard.main:app
"""

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from guard.paypal_client import PayPalClient, PayPalError, approval_link

logger = logging.getLogger("guard")

CURRENCY = "USD"
# PayPal rejects purchase unit descriptions longer than 127 characters.
MAX_DESCRIPTION = 127
# PayPal errors that mean "wrong order state", not "PayPal is broken".
CONFLICT_ISSUES = {
    "ORDER_NOT_APPROVED": "The buyer has not approved this order yet",
    "ORDER_ALREADY_CAPTURED": "This order was already captured",
}

# request_id -> {"fingerprint", "order_id", "approval_url", "capture"}.
# In memory for M1; SQLite and the audit log replace this later.
PURCHASES = {}


@asynccontextmanager
async def lifespan(app):
    # One shared client: it caches the OAuth token across requests.
    app.state.paypal = PayPalClient()
    yield
    app.state.paypal.http.close()


app = FastAPI(title="paypal-agent-guard", lifespan=lifespan)


def get_paypal(request: Request) -> PayPalClient:
    """Dependency that hands routes the shared client; tests override it with a fake."""
    return request.app.state.paypal


class Item(BaseModel):
    # The rules milestone will look up price and merchant from the catalog by
    # product_id instead of trusting what the agent sends.
    product_id: str
    name: str
    size: str | None = None
    price: Decimal = Field(gt=0, decimal_places=2)
    merchant: str


class PurchaseRequest(BaseModel):
    # Max 100 so "{request_id}-capture" fits PayPal-Request-Id's 108-char limit.
    request_id: str = Field(min_length=1, max_length=100)
    user_request: str
    item: Item


class PurchaseResponse(BaseModel):
    request_id: str
    decision: Literal["confirm", "warn", "block"]
    order_id: str | None = None
    approval_url: str | None = None
    reason: str


class CaptureResponse(BaseModel):
    # Only these fields leave the guard; the raw capture JSON has buyer PII.
    request_id: str
    order_id: str
    status: str
    capture_id: str | None = None


def fingerprint(purchase):
    """Hash of the whole payload, so a reused request_id with different content is caught."""
    data = purchase.model_dump(mode="json")
    # "99.0" and "99.00" are the same price and must hash the same.
    data["item"]["price"] = f"{purchase.item.price:.2f}"
    canonical = json.dumps(data, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def describe(item):
    size = f" (size {item.size})" if item.size else ""
    return f"{item.name}{size} from {item.merchant}"[:MAX_DESCRIPTION]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/purchase-requests", response_model=PurchaseResponse)
def create_purchase(purchase: PurchaseRequest, paypal: PayPalClient = Depends(get_paypal)):
    # Record the request before calling PayPal (DESIGN.md: log before acting),
    # so a retry after a timeout is still checked against the original payload.
    fp = fingerprint(purchase)
    record = PURCHASES.setdefault(
        purchase.request_id,
        {"fingerprint": fp, "order_id": None, "approval_url": None, "capture": None},
    )
    if record["fingerprint"] != fp:
        raise HTTPException(409, "request_id was already used for a different purchase")

    # Later milestones: rules engine (block?), intent check and risk score
    # (warn?), then the audit log, all before PayPal is called.
    decision, reason = "confirm", "M1 stub: no checks yet"

    if record["order_id"] is None:
        item = purchase.item
        order = paypal.create_order(
            f"{item.price:.2f}", CURRENCY, purchase.request_id, describe(item)
        )
        record["order_id"] = order["id"]
        record["approval_url"] = approval_link(order)

    return PurchaseResponse(
        request_id=purchase.request_id,
        decision=decision,
        order_id=record["order_id"],
        approval_url=record["approval_url"],
        reason=reason,
    )


@app.post("/purchase-requests/{request_id}/capture", response_model=CaptureResponse)
def capture_purchase(request_id: str, paypal: PayPalClient = Depends(get_paypal)):
    record = PURCHASES.get(request_id)
    if record is None or record["order_id"] is None:
        raise HTTPException(404, "Unknown purchase request")

    if record["capture"] is None:
        # Deterministic key: retries, concurrent calls and calls after a restart
        # all send the same PayPal-Request-Id, so PayPal charges at most once.
        result = paypal.capture_order(record["order_id"], f"{request_id}-capture")
        captures = result["purchase_units"][0].get("payments", {}).get("captures", [])
        record["capture"] = CaptureResponse(
            request_id=request_id,
            order_id=record["order_id"],
            status=result["status"],
            capture_id=captures[0]["id"] if captures else None,
        )
    return record["capture"]


@app.exception_handler(PayPalError)
async def handle_paypal_error(request, exc):
    # str(exc) holds status, error name, message and debug_id; never tokens.
    logger.warning("%s", exc)
    if exc.issue in CONFLICT_ISSUES:
        return JSONResponse(
            status_code=409,
            content={"detail": CONFLICT_ISSUES[exc.issue], "paypal_issue": exc.issue},
        )
    return JSONResponse(
        status_code=502,
        content={"detail": "PayPal request failed", "debug_id": exc.debug_id},
    )


@app.exception_handler(httpx.TimeoutException)
async def handle_timeout(request, exc):
    logger.warning("PayPal timeout: %r", exc)
    return JSONResponse(status_code=504, content={"detail": "PayPal timed out; retry safely"})


@app.exception_handler(httpx.TransportError)
async def handle_transport_error(request, exc):
    # TimeoutException is a TransportError too; FastAPI uses the more specific handler above.
    logger.warning("PayPal unreachable: %r", exc)
    return JSONResponse(status_code=502, content={"detail": "Could not reach PayPal"})
