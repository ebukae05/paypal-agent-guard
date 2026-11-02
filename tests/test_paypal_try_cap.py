"""Each PayPal try is cut off by wall clock at its time limit (TASKS.md, Hardening).

httpx's timeouts apply per phase, so a response that drips in slowly never times
out by itself. These tests use real short limits (TIMEOUT_SECONDS patched to 0.3 s)
because the cap waits in real time; the fake clock can't drive it. MockTransport
ignores httpx timeouts, so only the cap can stop a slow try here.

Every slow handler waits on the `release` Event, which is set at teardown so no
background thread outlives its test. Nothing reaches the network or reads .env.
"""

import threading
import time

import httpx
import pytest
from test_paypal_client import ORDER_REPLY, FakePayPalServer, make_client, unavailable

from guard import paypal_client
from guard.paypal_client import PayPalError

LIMIT = 0.3  # TIMEOUT_SECONDS during these tests
MARGIN = 0.2  # how much later than the limit a cut-off try may return
# A call with several real tries and waits: each wake-up can be late under CPU
# load, so allow more (tester: 0.2 s failed 1 of 4 loaded runs).
BUDGET_MARGIN = 0.5
ORDERS = "/v2/checkout/orders"
CAPTURE = "/v2/checkout/orders/ORDER-1/capture"


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setattr(paypal_client, "load_dotenv", lambda: None)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")


@pytest.fixture(autouse=True)
def short_limit(monkeypatch):
    monkeypatch.setattr(paypal_client, "TIMEOUT_SECONDS", LIMIT)


@pytest.fixture
def release():
    """Set at teardown: slow handlers stop, then every thread a test started must end."""
    event = threading.Event()
    before = set(threading.enumerate())
    yield event
    event.set()
    started = [t for t in threading.enumerate() if t not in before]
    for thread in started:
        thread.join(2)
    assert not [t for t in started if t.is_alive()], "a PayPal try thread outlived the test"


class DripStream(httpx.SyncByteStream):
    """A response body that sends one byte every 0.1 s for 5 s (or until released).

    Each gap is far below any read timeout, so httpx alone would keep reading.
    """

    def __init__(self, release, seconds=5.0, gap=0.1):
        self.release = release
        self.chunks = int(seconds / gap)
        self.gap = gap
        self.sent = 0

    def __iter__(self):
        for _ in range(self.chunks):
            yield b"x"
            self.sent += 1
            if self.release.wait(self.gap):
                return


def dripping(release, streams=None):
    """A server reply: 200 at once, then a body that drips in slowly."""

    def reply(request):
        stream = DripStream(release)
        if streams is not None:
            streams.append(stream)
        return httpx.Response(200, stream=stream)

    return reply


def slow_headers(release):
    """A server reply that sends nothing (not even headers) for 5 s or until released."""

    def reply(request):
        release.wait(5.0)
        return httpx.Response(200, json=ORDER_REPLY)

    return reply


class Stopwatch:
    """Measures real time spent inside a `with` block, even if it raises."""

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc_info):
        self.elapsed = time.monotonic() - self.start
        return False  # never swallow the exception


# --- One try never exceeds its limit ---


def test_slow_drip_body_is_cut_off_at_the_limit(release):
    # The "Done when" test. A POST without PayPal-Request-Id gets exactly one try.
    server = FakePayPalServer()
    streams = []
    server.replies[ORDERS] = dripping(release, streams)
    client = make_client(server)

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException, match="longer than 0.3 s"):
        client._send("POST", ORDERS, json={})

    assert LIMIT - 0.01 <= watch.elapsed < LIMIT + MARGIN
    assert len(server.calls_to(ORDERS)) == 1
    (stream,) = streams
    assert 1 <= stream.sent < stream.chunks  # the body was really dripping, not finished


def test_slow_headers_are_cut_off_at_the_limit(release):
    server = FakePayPalServer()
    server.replies[ORDERS] = slow_headers(release)
    client = make_client(server)

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException, match="longer than 0.3 s"):
        client._send("POST", ORDERS, json={})

    assert LIMIT - 0.01 <= watch.elapsed < LIMIT + MARGIN
    assert len(server.calls_to(ORDERS)) == 1


def test_the_limit_given_to_httpx_matches_the_cap(release):
    server = FakePayPalServer()
    client = make_client(server)

    client.get_order("ORDER-1")

    assert [t for _, _, t in server.timeouts] == [LIMIT, LIMIT]  # token, order


# --- Fast answers and errors are unchanged ---


def test_fast_response_comes_through_normally(release):
    server = FakePayPalServer()
    client = make_client(server)

    with Stopwatch() as watch:
        order = client.create_order("1.00", "USD", "req-fast", "shoes")

    assert order == ORDER_REPLY
    assert watch.elapsed < LIMIT


def test_fast_streamed_body_comes_through_whole(release):
    server = FakePayPalServer()
    server.replies[ORDERS] = httpx.Response(
        201, stream=httpx.ByteStream(b'{"id": "ORDER-9", "status": "CREATED"}')
    )
    client = make_client(server)

    response = client._send("POST", ORDERS, request_id="req-9", json={})

    assert response.status_code == 201
    assert response.json() == {"id": "ORDER-9", "status": "CREATED"}


def test_503_is_still_retried(release):
    server = FakePayPalServer()
    server.replies[ORDERS] = [unavailable()]
    client = make_client(server)

    assert client.create_order("1.00", "USD", "req-r", "shoes") == ORDER_REPLY
    assert len(server.calls_to(ORDERS)) == 2


def test_422_is_still_not_retried(release):
    server = FakePayPalServer()
    server.replies[CAPTURE] = httpx.Response(
        422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]}
    )
    client = make_client(server)

    with pytest.raises(PayPalError) as exc_info:
        client.capture_order("ORDER-1", "req-cap")

    assert exc_info.value.status_code == 422
    assert exc_info.value.issue == "ORDER_NOT_APPROVED"
    assert len(server.calls_to(CAPTURE)) == 1


def test_connect_error_from_the_thread_is_reraised_unchanged(release):
    error = httpx.ConnectError("connection refused")
    server = FakePayPalServer()
    server.replies[ORDERS] = error
    client = make_client(server)

    with pytest.raises(httpx.ConnectError) as exc_info:
        client._send("POST", ORDERS, json={})  # one try: no PayPal-Request-Id

    assert exc_info.value is error  # the very same exception, not a copy or a wrapper


# --- An overrun try is retried safely ---


def test_overrun_try_is_retried_with_the_same_request_id(release):
    # Try 1 drips past the limit, try 2 answers 201 at once.
    server = FakePayPalServer()
    server.replies[ORDERS] = [dripping(release)]
    client = make_client(server)

    with Stopwatch() as watch:
        order = client.create_order("1.00", "USD", "req-slow", "shoes")

    assert order == ORDER_REPLY

    ids = [r.headers["PayPal-Request-Id"] for r in server.calls_to(ORDERS)]
    assert ids == ["req-slow", "req-slow"]
    assert client.waits == [paypal_client.BACKOFF_SECONDS[0]]  # fake sleep, no real wait
    assert watch.elapsed < LIMIT + MARGIN


def test_overrun_capture_is_retried_with_the_same_request_id(release):
    server = FakePayPalServer()
    server.replies[CAPTURE] = [slow_headers(release)]
    client = make_client(server)

    assert client.capture_order("ORDER-1", "req-cap")["status"] == "COMPLETED"

    ids = [r.headers["PayPal-Request-Id"] for r in server.calls_to(CAPTURE)]
    assert ids == ["req-cap", "req-cap"]


# --- The whole call is bounded ---


def dripping_real_time_client(release, monkeypatch, budget):
    # A real clock and real (short) waits, so the budget is real time too.
    monkeypatch.setattr(paypal_client, "PAYPAL_BUDGET_SECONDS", budget)
    monkeypatch.setattr(paypal_client, "MIN_TRY_SECONDS", 0.1)
    monkeypatch.setattr(paypal_client, "BACKOFF_SECONDS", (0.05, 0.05))
    server = FakePayPalServer()
    client = make_client(server)
    client.get_order("ORDER-1")  # caches the token, so only order tries use the budget
    client._clock = time.monotonic
    client._sleep = time.sleep
    server.clock = None  # stop the fake server from moving the old fake clock
    server.replies[ORDERS] = dripping(release)
    return server, client


def test_three_dripping_tries_finish_within_the_budget(release, monkeypatch):
    # Tries: 0.3 s, wait 0.05, 0.3 s, wait 0.05, then whatever is left of 0.9 s.
    # How many tries fit depends on scheduling (under CPU load the third may not
    # start), so only the budget is asserted here; the try count is checked below
    # with plenty of slack. (Tester: the old `== 3` failed 5 of 10 runs under load.)
    budget = 0.9
    server, client = dripping_real_time_client(release, monkeypatch, budget)

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-b", "shoes")

    assert 2 <= len(server.calls_to(ORDERS)) <= 3
    assert watch.elapsed < budget + BUDGET_MARGIN


def test_three_dripping_tries_are_all_made_when_they_fit(release, monkeypatch):
    # About 1 s of tries and waits inside a 3 s budget: all three tries happen,
    # each cut off, with the same PayPal-Request-Id, and the call ends well in budget.
    budget = 3.0
    server, client = dripping_real_time_client(release, monkeypatch, budget)

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-b", "shoes")

    ids = [r.headers["PayPal-Request-Id"] for r in server.calls_to(ORDERS)]
    assert ids == ["req-b"] * 3
    assert watch.elapsed < budget
