"""Tester's attack on the shared 20 s PayPal time budget (TEST_REPORT loop 1).

Independent of the fakes in test_paypal_client.py: a scripted MockTransport and a
simulated clock. Nothing really waits, nothing reaches the network, .env is never read.
"""

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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

TOKEN = "/v1/oauth2/token"
ORDERS = "/v2/checkout/orders"
ORDER_GET = "/v2/checkout/orders/ORDER-1"
CAPTURE = "/v2/checkout/orders/ORDER-1/capture"
AGENT_TIMEOUT = 30.0  # GUARD_TIMEOUT in agent/settings.py (not imported: it reads .env)
EPS = 1e-9


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setattr(paypal_client, "load_dotenv", lambda: None)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")


class Script:
    """MockTransport handler driven by per-path lists of outcomes.

    An outcome is (kind, seconds, expire): kind is "ok", "503", "timeout" or
    "connect"; seconds is how long the answer takes (a "timeout" costs the try's
    timeout; an answer slower than the timeout becomes a timeout); expire=True
    makes the cached token run out at that moment. When a list is empty the
    answer is "ok" after 0 s.
    """

    def __init__(self, client, token=(), order=()):
        self.client = client
        self.outcomes = {TOKEN: list(token), "order": list(order)}
        self.sent = []  # (path, sent_at, timeout, request)
        self.tokens = 0

    def __call__(self, request):
        clock = self.client._clock
        path = request.url.path
        timeout = request.extensions["timeout"]["read"]
        self.sent.append((path, clock.now, timeout, request))
        queue = self.outcomes[TOKEN if path == TOKEN else "order"]
        kind, seconds, expire = queue.pop(0) if queue else ("ok", 0.0, False)
        if kind == "timeout" or seconds > timeout:
            clock.now += timeout
            raise httpx.ReadTimeout("simulated timeout")
        clock.now += seconds
        if expire:
            self.client._token_expires_at = 0.0
        if kind == "connect":
            raise httpx.ConnectError("simulated connect error")
        if kind == "503":
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "debug_id": "dbg"})
        if path == TOKEN:
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"tok-{self.tokens}", "expires_in": 32400}
            )
        if path.endswith("/capture"):
            return httpx.Response(201, json={"id": "ORDER-1", "status": "COMPLETED"})
        return httpx.Response(201, json={"id": "ORDER-1", "status": "CREATED", "links": []})


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _NoNetworkClient:
    """Stand-in for the default httpx.Client during construction (skips the slow
    SSL setup); build() swaps in a MockTransport client right after."""

    def __init__(self, **kwargs):
        pass


def build(token=(), order=(), cached_token=False):
    real_client = paypal_client.httpx.Client
    paypal_client.httpx.Client = _NoNetworkClient  # type: ignore[misc,assignment]
    try:
        client = PayPalClient()
    finally:
        paypal_client.httpx.Client = real_client  # type: ignore[misc]
    client._clock = Clock()
    client.waits = []  # (started_at, seconds)

    def sleep(seconds):
        client.waits.append((client._clock.now, seconds))
        client._clock.now += seconds

    client._sleep = sleep
    script = Script(client, token, order)
    client.http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=httpx.MockTransport(script))
    if cached_token:
        client._token = "tok-cached"
        client._token_expires_at = 1e18
    return client, script


def call(client, method):
    if method == "create":
        return client.create_order("1.00", "USD", "req-1", "shoes")
    if method == "capture":
        return client.capture_order("ORDER-1", "req-1-capture")
    return client.get_order("ORDER-1")


# --- Property: any mix of token/order failures stays inside one 20 s budget ---

outcome = st.tuples(
    st.sampled_from(["ok", "503", "timeout", "connect"]),
    st.floats(min_value=0.0, max_value=10.0, allow_nan=False),
    st.booleans(),
)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    token=st.lists(outcome, max_size=8),
    order=st.lists(outcome, max_size=4),
    cached=st.booleans(),
    method=st.sampled_from(["create", "capture", "get"]),
)
def test_property_every_try_starts_and_ends_inside_the_budget(token, order, cached, method):
    client, script = build(token, order, cached_token=cached)
    try:
        call(client, method)
    except PayPalError, httpx.TransportError:
        pass  # any outcome is fine; the timing and ID rules below are what we check

    # Whole call (token fetches, waits, order tries) fits the budget and the agent.
    assert client._clock.now <= PAYPAL_BUDGET_SECONDS + EPS < AGENT_TIMEOUT
    for path, sent_at, timeout, _ in script.sent:
        left = PAYPAL_BUDGET_SECONDS - sent_at
        # A try only starts with at least a real try's worth of time left ...
        assert left >= MIN_TRY_SECONDS - EPS, (path, sent_at)
        # ... and its timeout is capped at 8 s and at the time left.
        assert timeout == pytest.approx(min(TIMEOUT_SECONDS, left)), (path, sent_at, timeout)

    api = [r for p, _, _, r in script.sent if p != TOKEN]
    assert len(api) <= MAX_ATTEMPTS
    if method == "create":
        assert {r.url.path for r in api} <= {ORDERS}
        assert {r.headers["PayPal-Request-Id"] for r in api} <= {"req-1"}
        assert all("PayPal-Request-Id" in r.headers for r in api)
    elif method == "capture":
        assert {r.url.path for r in api} <= {CAPTURE}
        assert all(r.headers["PayPal-Request-Id"] == "req-1-capture" for r in api)
    else:
        assert {r.url.path for r in api} <= {ORDER_GET}
        assert all("PayPal-Request-Id" not in r.headers for r in api)
    # Token tries per fetch are capped too: never more than 3 per order attempt + 3.
    assert len(script.sent) - len(api) <= MAX_ATTEMPTS * (MAX_ATTEMPTS + 1)

    # No wasted backoff: every wait is followed by a request being sent.
    sends = [at for _, at, _, _ in script.sent]
    for started, seconds in client.waits:
        assert seconds in BACKOFF_SECONDS
        assert any(at >= started + seconds - EPS for at in sends), "waited, then sent nothing"


@settings(max_examples=200, deadline=None)
@given(order=st.lists(outcome, max_size=4), method=st.sampled_from(["create", "capture"]))
def test_property_create_never_sends_a_capture_and_capture_never_creates(order, method):
    client, script = build(order=order, cached_token=True)
    try:
        call(client, method)
    except PayPalError, httpx.TransportError:
        pass
    paths = {p for p, _, _, _ in script.sent}
    if method == "create":
        assert not any(p.endswith("/capture") for p in paths)
    else:
        assert ORDERS not in paths


# --- Budget edges (cached token, so only the order call uses time) ---


@pytest.mark.parametrize(
    ("token_try_2_takes", "retried"),
    [
        (1.0 - 0.001, True),  # 2.501 s left after order try 1
        (1.0, True),  # exactly BACKOFF[0] + MIN_TRY_SECONDS = 2.5 s left
        (1.0 + 0.001, False),  # 2.499 s left
    ],
)
def test_first_retry_boundary(token_try_2_takes, retried):
    # Token: timeout (8 s), wait 0.5 s, ok after ~1 s -> ~9.5 s. Order try 1: 503
    # after 8 s -> ~17.5 s, so ~2.5 s are left for wait 0.5 s + a real 2 s try.
    client, script = build(
        token=[("timeout", 0, False), ("ok", token_try_2_takes, False)],
        order=[("503", TIMEOUT_SECONDS, False)],
    )
    left = PAYPAL_BUDGET_SECONDS - (8.0 + 0.5 + token_try_2_takes + TIMEOUT_SECONDS)
    if retried:
        assert client.create_order("1.00", "USD", "req-1", "shoes")["id"] == "ORDER-1"
        orders = [(at, t, r) for p, at, t, r in script.sent if p == ORDERS]
        assert len(orders) == 2
        assert orders[1][1] == pytest.approx(left - BACKOFF_SECONDS[0])
        assert orders[1][2].headers["PayPal-Request-Id"] == "req-1"
    else:
        with pytest.raises(PayPalError) as exc_info:
            client.create_order("1.00", "USD", "req-1", "shoes")
        assert exc_info.value.status_code == 503
        assert exc_info.value.debug_id == "dbg"
        assert len([p for p, _, _, _ in script.sent if p == ORDERS]) == 1
        assert [s for _, s in client.waits] == [BACKOFF_SECONDS[0]]  # only the token's wait


@pytest.mark.parametrize(
    ("left_after_try_2", "third_try"),
    [
        (BACKOFF_SECONDS[1] + MIN_TRY_SECONDS - 0.001, False),
        (BACKOFF_SECONDS[1] + MIN_TRY_SECONDS, True),
        (BACKOFF_SECONDS[1] + MIN_TRY_SECONDS + 0.001, True),
    ],
)
def test_second_retry_boundary_uses_the_longer_backoff(left_after_try_2, third_try):
    # Token 1 s; order try 1: 503 after 8 s (9 s); wait 0.5 s; try 2: 503 after
    # enough time that left_after_try_2 seconds remain. A third try needs 1.0 + 2.0 s.
    try_2_takes = PAYPAL_BUDGET_SECONDS - 1.0 - TIMEOUT_SECONDS - BACKOFF_SECONDS[0]
    try_2_takes -= left_after_try_2
    assert try_2_takes <= TIMEOUT_SECONDS
    client, script = build(
        token=[("ok", 1.0, False)],
        order=[("503", TIMEOUT_SECONDS, False), ("503", try_2_takes, False)],
    )
    if third_try:
        assert client.create_order("1.00", "USD", "req-1", "shoes")["id"] == "ORDER-1"
        assert len(script.sent) == 4  # token + 3 order tries
        assert script.sent[3][2] == pytest.approx(left_after_try_2 - BACKOFF_SECONDS[1])
    else:
        with pytest.raises(PayPalError):
            client.create_order("1.00", "USD", "req-1", "shoes")
        assert len(script.sent) == 3  # token + 2 order tries
        assert [s for _, s in client.waits] == [BACKOFF_SECONDS[0]]


@pytest.mark.parametrize(
    ("token_try_3_takes", "sent"),
    [
        (0.5 + 0.001, False),  # 1.999 s left for the first order try
        (0.5, True),  # exactly MIN_TRY_SECONDS left
        (0.5 - 0.001, True),  # 2.001 s left
    ],
)
def test_first_try_boundary_after_token_fetch(token_try_3_takes, sent):
    # Token: timeout 8 s, wait 0.5, timeout 8 s, wait 1.0 -> 17.5 s; try 3 answers
    # after ~0.5 s, leaving ~2 s for the first order try.
    client, script = build(
        token=[("timeout", 0, False), ("timeout", 0, False), ("ok", token_try_3_takes, False)]
    )
    left = PAYPAL_BUDGET_SECONDS - 17.5 - token_try_3_takes
    if sent:
        assert client.create_order("1.00", "USD", "req-1", "shoes")["id"] == "ORDER-1"
        path, _, timeout, _ = script.sent[-1]
        assert path == ORDERS
        assert timeout == pytest.approx(left)
    else:
        with pytest.raises(httpx.TimeoutException):
            client.create_order("1.00", "USD", "req-1", "shoes")
        assert [p for p, _, _, _ in script.sent] == [TOKEN, TOKEN, TOKEN]


def test_first_try_boundary_without_pre_aging_through_real_token_tries():
    # No helper shortcuts: token try 1 times out (8 s), wait 0.5 s, try 2 times out
    # (8 s), wait 1.0 s -> 17.5 s; try 3 gets 2.5 s and answers after 0.5 s -> 18.0 s,
    # exactly MIN_TRY_SECONDS left, so the order is still sent with a 2 s timeout.
    client, script = build(token=[("timeout", 0, False), ("timeout", 0, False), ("ok", 0.5, False)])
    assert client.create_order("1.00", "USD", "req-1", "shoes")["id"] == "ORDER-1"
    assert [(p, at, t) for p, at, t, _ in script.sent] == [
        (TOKEN, 0.0, 8.0),
        (TOKEN, 8.5, 8.0),
        (TOKEN, 17.5, 2.5),
        (ORDERS, 18.0, 2.0),
    ]


def test_token_expiry_on_every_order_try_refetches_inside_one_deadline():
    # Every order try returns 503 and expires the token, every token fetch takes 3 s.
    client, script = build(
        token=[("ok", 3.0, False)] * 4,
        order=[("503", 2.0, True), ("503", 2.0, True), ("503", 2.0, True)],
    )
    with pytest.raises(PayPalError):
        client.create_order("1.00", "USD", "req-1", "shoes")
    orders = [r for p, _, _, r in script.sent if p == ORDERS]
    assert [r.headers["Authorization"] for r in orders] == [
        "Bearer tok-1",
        "Bearer tok-2",
        "Bearer tok-3",
    ]
    assert {r.headers["PayPal-Request-Id"] for r in orders} == {"req-1"}
    assert client._clock.now <= PAYPAL_BUDGET_SECONDS


def test_two_calls_each_get_their_own_budget():
    # The first call burns its whole budget; the next guard request starts fresh.
    client, script = build(order=[("timeout", 0, False)] * 3, cached_token=True)
    with pytest.raises(httpx.TimeoutException):
        client.capture_order("ORDER-1", "req-1-capture")
    first_end = client._clock.now
    assert first_end <= PAYPAL_BUDGET_SECONDS
    assert client.capture_order("ORDER-1", "req-1-capture")["status"] == "COMPLETED"
    _, at, timeout, request = script.sent[-1]
    assert at == first_end and timeout == TIMEOUT_SECONDS
    assert request.headers["PayPal-Request-Id"] == "req-1-capture"
