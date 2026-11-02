"""Tester attacks on the whole-try cap (TASKS.md, Hardening: cap each whole PayPal try).

Two kinds of test:
- Fake clock + Hypothesis: `_request_with_cap` is replaced by a fake that spends
  simulated time (the worst case: the full limit), so the budget arithmetic around
  the cap can be checked for thousands of failure mixes without real waiting.
- Real clock, short limits: the real `_request_with_cap` against slow handlers,
  for the token path, a token refetch between tries, late responses, exceptions
  from the thread, ID-less POSTs and the daemon flag.

Every slow handler waits on `release`, set at teardown, so no thread outlives its
test. Nothing reaches the network or reads .env.
"""

import threading
import time
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_paypal_client import (
    CAPTURE_REPLY,
    ORDER_REPLY,
    FakePayPalServer,
    make_client,
    unavailable,
)
from test_paypal_try_cap import DripStream, Stopwatch, dripping, slow_headers

from guard import paypal_client

LIMIT = 0.3
MARGIN = 0.2
# A call with several real tries and waits: each wake-up can be late under CPU
# load, so allow more (tester: 0.2 s failed 1 of 4 loaded runs).
BUDGET_MARGIN = 0.5
TOKEN = "/v1/oauth2/token"
ORDERS = "/v2/checkout/orders"
ORDER_GET = "/v2/checkout/orders/ORDER-1"
CAPTURE = "/v2/checkout/orders/ORDER-1/capture"


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setattr(paypal_client, "load_dotenv", lambda: None)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")


@pytest.fixture
def release():
    """Set at teardown; every thread the test started must then end."""
    event = threading.Event()
    before = set(threading.enumerate())
    yield event
    event.set()
    started = [t for t in threading.enumerate() if t not in before]
    for thread in started:
        thread.join(2)
    assert not [t for t in started if t.is_alive()], "a PayPal try thread outlived the test"


def real_time_client(server, monkeypatch, budget, min_try=0.1, backoff=(0.05, 0.05)):
    """A client on the real clock with a small real budget and real short waits."""
    monkeypatch.setattr(paypal_client, "TIMEOUT_SECONDS", LIMIT)
    monkeypatch.setattr(paypal_client, "PAYPAL_BUDGET_SECONDS", budget)
    monkeypatch.setattr(paypal_client, "MIN_TRY_SECONDS", min_try)
    monkeypatch.setattr(paypal_client, "BACKOFF_SECONDS", backoff)
    client = make_client(server)
    client._clock = time.monotonic
    client._sleep = time.sleep
    server.clock = None
    return client


def token_reply(request):
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 30})


# --- Fake clock: the budget arithmetic around the cap (Hypothesis) ---

OUTCOMES = st.sampled_from(["overrun", "connect", "5xx", "ok"])
_SHARED: dict[str, Any] = {}


def shared_client(server):
    """One PayPalClient for every Hypothesis example (building one takes ~0.3 s).

    Its MockTransport sends to the current example's server; all state is reset.
    """
    if "client" not in _SHARED:
        client = make_client(FakePayPalServer())
        client.http = httpx.Client(
            base_url=paypal_client.SANDBOX_BASE_URL,
            transport=httpx.MockTransport(lambda request: _SHARED["server"](request)),
        )
        _SHARED["client"] = client
    client = _SHARED["client"]
    _SHARED["server"] = server
    client._token = None
    client._token_expires_at = 0.0
    client.clock.now = 0.0
    client.waits.clear()
    server.clock = client.clock
    client.__dict__.pop("_request_with_cap", None)
    return client


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    method=st.sampled_from(["create", "capture", "get"]),
    outcomes=st.lists(OUTCOMES, min_size=1, max_size=12),
    spend=st.floats(min_value=0.0, max_value=1.0),
)
def test_every_try_ends_by_the_deadline_for_any_failure_mix(method, outcomes, spend):
    # The token expires on every call (expires_in < 60 s margin), so a token
    # refetch happens before every order try, and each one can fail too.
    server = FakePayPalServer(expires_in=30)
    client = shared_client(server)
    tries = []  # (path, start, limit)
    queue = list(outcomes)
    real_send = client.http.request

    def fake_cap(limit, verb, url, **kwargs):
        start = client.clock.now
        tries.append((url, start, limit))
        outcome = queue.pop(0) if queue else "ok"
        if outcome == "overrun":  # worst case: the whole limit is used, then cut off
            client.clock.now += limit
            raise httpx.TimeoutException("cut off")
        client.clock.now += spend * limit
        if outcome == "connect":
            raise httpx.ConnectError("refused")
        if outcome == "5xx":
            return unavailable()
        return real_send(verb, url, timeout=limit, **kwargs)

    client._request_with_cap = fake_cap
    begin = client.clock.now
    deadline = begin + paypal_client.PAYPAL_BUDGET_SECONDS
    call = {
        "create": lambda: client.create_order("1.00", "USD", "req-h", "shoes"),
        "capture": lambda: client.capture_order("ORDER-1", "req-h-capture"),
        "get": lambda: client.get_order("ORDER-1"),
    }[method]

    try:
        result = call()
    except httpx.TransportError, paypal_client.PayPalError:
        result = None
    else:
        assert isinstance(result, dict)  # never None on success

    assert tries, "at least one try"
    for url, start, limit in tries:
        assert 0 < limit <= paypal_client.TIMEOUT_SECONDS
        assert start + limit <= deadline + 1e-9, f"{url} try may run past the deadline"
        assert start <= deadline - paypal_client.MIN_TRY_SECONDS + 1e-9
    assert client.clock.now - begin <= paypal_client.PAYPAL_BUDGET_SECONDS + 1e-9
    # Every order/capture POST on the wire carries the same PayPal-Request-Id.
    posts = [r for r in server.api_requests() if r.method == "POST"]
    assert {r.headers.get("PayPal-Request-Id") for r in posts} <= {"req-h", "req-h-capture"}


# --- Real clock: the token path is capped too ---


def test_dripping_token_fetch_is_cut_off_and_the_call_stays_in_budget(release, monkeypatch):
    budget = 1.0
    server = FakePayPalServer()
    server.replies[TOKEN] = dripping(release)
    client = real_time_client(server, monkeypatch, budget)

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException):
        client.get_order("ORDER-1")

    assert watch.elapsed < budget + BUDGET_MARGIN
    assert len(server.calls_to(TOKEN)) >= 2  # the token call is retried
    assert server.calls_to(ORDER_GET) == []  # no order try without a token


def test_dripping_token_refetch_between_tries_stays_in_budget(release, monkeypatch):
    # Order try 1 drips; before try 2 the (expired) token is refetched and that drips too.
    budget = 1.2
    server = FakePayPalServer()
    token_calls = []

    def token(request):
        token_calls.append(request)
        if len(token_calls) == 1:
            return token_reply(request)
        return httpx.Response(200, stream=DripStream(release))

    server.replies[TOKEN] = token
    server.replies[ORDERS] = dripping(release)
    client = real_time_client(server, monkeypatch, budget)

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException):
        client.create_order("1.00", "USD", "req-t", "shoes")

    assert watch.elapsed < budget + BUDGET_MARGIN
    assert len(token_calls) >= 2
    assert len(server.calls_to(ORDERS)) >= 1


def test_slow_token_then_order_share_one_budget(release, monkeypatch):
    # Token answers just inside its limit three times over (5xx after 0.25 s, twice),
    # then the order drips. Nothing gives the order a fresh budget.
    budget = 1.0
    server = FakePayPalServer()
    token_calls = []

    def token(request):
        token_calls.append(request)
        release.wait(0.25)
        if len(token_calls) < 3:
            return unavailable()
        return token_reply(request)

    server.replies[TOKEN] = token
    server.replies[ORDER_GET] = dripping(release)
    client = real_time_client(server, monkeypatch, budget)

    with Stopwatch() as watch, pytest.raises((httpx.TimeoutException, paypal_client.PayPalError)):
        client.get_order("ORDER-1")

    assert watch.elapsed < budget + BUDGET_MARGIN


# --- Real clock: a late response is never returned ---


def test_late_response_of_an_overrun_try_is_never_returned(release, monkeypatch):
    # Try 1 answers "LATE" after 0.45 s (past the 0.3 s limit), while try 2 is
    # still waiting; try 2 answers 0.3 s in (just inside its limit) with the real order.
    server = FakePayPalServer()
    calls = []
    late_done = threading.Event()

    def orders(request):
        calls.append(request)
        if len(calls) == 1:
            release.wait(0.45)
            late_done.set()
            return httpx.Response(201, json={"id": "LATE", "status": "CREATED"})
        release.wait(0.25)
        return httpx.Response(201, json=ORDER_REPLY)

    server.replies[ORDERS] = orders
    client = real_time_client(server, monkeypatch, budget=3.0)

    order = client.create_order("1.00", "USD", "req-late", "shoes")

    assert order == ORDER_REPLY
    assert late_done.wait(2)  # the late answer really did arrive during try 2
    # And it doesn't leak into the next call either.
    assert client.get_order("ORDER-1") == ORDER_REPLY
    ids = [r.headers["PayPal-Request-Id"] for r in calls]
    assert ids == ["req-late", "req-late"]


def test_overrun_capture_that_succeeded_at_paypal_is_charged_once(release, monkeypatch):
    # A PayPal-like server: the capture happens when the request arrives, the reply
    # is slow. A repeat with the same PayPal-Request-Id gets the stored result.
    server = FakePayPalServer()
    stored = {}
    charges = []

    def capture(request):
        key = request.headers.get("PayPal-Request-Id")
        if key in stored:
            return httpx.Response(201, json=stored[key])
        charges.append(key)
        stored[key] = CAPTURE_REPLY
        release.wait(5.0)  # the reply never makes it back in time
        return httpx.Response(201, json=CAPTURE_REPLY)

    server.replies[CAPTURE] = capture
    client = real_time_client(server, monkeypatch, budget=3.0)

    result = client.capture_order("ORDER-1", "req-c-capture")

    assert result == CAPTURE_REPLY
    assert charges == ["req-c-capture"]
    assert len(server.calls_to(CAPTURE)) == 2


def test_overrun_post_without_request_id_is_tried_once(release, monkeypatch):
    server = FakePayPalServer()
    server.replies[ORDERS] = slow_headers(release)
    client = real_time_client(server, monkeypatch, budget=5.0)

    for request_id in (None, ""):
        with pytest.raises(httpx.TimeoutException):
            client._send("POST", ORDERS, request_id=request_id, json={})

    time.sleep(LIMIT)  # time for any hidden extra try to show up
    calls = server.calls_to(ORDERS)
    assert len(calls) == 2  # one per call, never retried
    assert all("PayPal-Request-Id" not in r.headers for r in calls)


# --- Exceptions from the thread are re-raised unchanged ---


class Boom(Exception):
    pass


class BaseBoom(BaseException):
    pass


@pytest.mark.parametrize("error_type", [Boom, ValueError, KeyError, BaseBoom])
def test_non_httpx_exception_from_the_thread_is_the_same_object_and_not_retried(error_type):
    error = error_type("from the thread")
    server = FakePayPalServer()

    def explode(request):
        raise error

    server.replies[ORDERS] = explode
    client = make_client(server)

    with pytest.raises(error_type) as exc_info:
        client.create_order("1.00", "USD", "req-x", "shoes")

    assert exc_info.value is error
    assert len(server.calls_to(ORDERS)) == 1


@pytest.mark.parametrize(
    "make_error",
    [
        lambda n: httpx.ReadTimeout(f"t{n}"),
        lambda n: httpx.ConnectError(f"c{n}"),
        lambda n: httpx.RemoteProtocolError(f"p{n}"),
    ],
)
def test_last_transport_error_is_reraised_as_the_same_object(make_error):
    errors = [make_error(n) for n in range(3)]
    server = FakePayPalServer()
    server.replies[ORDERS] = list(errors)
    client = make_client(server)

    with pytest.raises(type(errors[0])) as exc_info:
        client.create_order("1.00", "USD", "req-e", "shoes")

    assert exc_info.value is errors[-1]
    assert len(server.calls_to(ORDERS)) == 3


def test_capped_try_returns_the_response_object_never_none():
    server = FakePayPalServer()
    client = make_client(server)

    response = client._request_with_cap(LIMIT, "GET", ORDER_GET)

    assert isinstance(response, httpx.Response)
    assert response.json() == ORDER_REPLY


# --- The try thread ---


def test_try_runs_in_a_daemon_thread(release, monkeypatch):
    server = FakePayPalServer()
    seen = []

    def orders(request):
        seen.append(threading.current_thread())
        return httpx.Response(201, json=ORDER_REPLY)

    server.replies[ORDERS] = orders
    client = make_client(server)

    client._send("POST", ORDERS, request_id="req-d", json={})

    (thread,) = seen
    assert thread is not threading.main_thread()
    assert thread is not threading.current_thread()
    assert thread.daemon  # a stuck PayPal call must not keep the process alive


def test_cut_off_happens_in_the_caller_not_after_the_thread(release, monkeypatch):
    # The caller gets the timeout while the try's thread is still running.
    server = FakePayPalServer()
    server.replies[ORDERS] = slow_headers(release)
    client = real_time_client(server, monkeypatch, budget=5.0)
    before = set(threading.enumerate())

    with pytest.raises(httpx.TimeoutException):
        client._send("POST", ORDERS, json={})

    alive = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    assert len(alive) == 1  # abandoned, not joined; released at teardown


def test_cut_off_is_tight_with_a_longer_limit(release, monkeypatch):
    # With a 0.3 s limit a cap 50% too loose (0.45 s) still fits the 0.2 s margin.
    # A 1 s limit shows it: the try must end within 1.2 s, not 1.5 s.
    monkeypatch.setattr(paypal_client, "TIMEOUT_SECONDS", 1.0)
    server = FakePayPalServer()
    server.replies[ORDERS] = slow_headers(release)
    client = make_client(server)

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException, match="longer than 1.0 s"):
        client._send("POST", ORDERS, json={})

    assert 1.0 - 0.01 <= watch.elapsed < 1.0 + MARGIN


# --- The abandoned try's thread (real local socket, real httpx transport) ---


@pytest.fixture
def drip_server():
    """A local HTTP server: headers at once, then one body byte every 0.1 s for 3 s."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()
    workers = []

    def serve(conn):
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n")
            for _ in range(30):
                if stop.wait(0.1):
                    return
                try:
                    conn.sendall(b"x")
                except OSError:
                    return

    def accept():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            worker = threading.Thread(target=serve, args=(conn,), daemon=True)
            workers.append(worker)
            worker.start()

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    stop.set()
    listener.close()
    acceptor.join(2)
    for worker in workers:
        worker.join(2)


class StillRunning(AssertionError):
    """Only this failure is the expected one; any other assertion fails the test."""


@pytest.mark.xfail(
    strict=True,
    raises=StillRunning,
    reason="Tester finding: the code comment and plan say an abandoned try 'ends by its "
    "httpx timeouts', but a dripping body never trips a per-phase timeout, so the "
    "abandoned thread (and its pooled connection) keeps running until the server stops.",
)
def test_abandoned_dripping_try_thread_ends_soon_after_the_cut_off(drip_server, monkeypatch):
    monkeypatch.setattr(paypal_client, "TIMEOUT_SECONDS", LIMIT)
    client = make_client(FakePayPalServer())
    client.get_order("ORDER-1")  # caches the token from the fake, so only the order hits the socket
    client._clock = time.monotonic
    client.http = httpx.Client(base_url=drip_server)  # real transport, local socket only
    before = set(threading.enumerate())

    with Stopwatch() as watch, pytest.raises(httpx.TimeoutException):
        client._send("POST", "/v2/checkout/orders", json={})  # one try, no request ID

    assert watch.elapsed < LIMIT + MARGIN  # the caller is cut off on time (passes)
    abandoned = [t for t in threading.enumerate() if t not in before and t.name.endswith("(run)")]
    assert len(abandoned) == 1  # the try thread, not the server's threads
    time.sleep(1.0)  # > 3x the try's limit after the cut-off
    still_running = [t for t in abandoned if t.is_alive()]
    if still_running:
        raise StillRunning("the abandoned try is still reading the dripping body")
