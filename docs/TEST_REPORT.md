# Test report: tests for guard/paypal_client.py

Task (TASKS.md): "[AI] Tests for guard/paypal_client.py (idempotency, declines, timeouts).
Done when: mocked HTTP tests cover token caching, PayPal-Request-Id on create and capture,
error -> PayPalError with debug_id, timeout; coverage of the file well above 38%"

Commit under test: 3019eaf (branch feat/guard-core). Production code unchanged.

## Done-when results

| Item | Result | Evidence |
|---|---|---|
| Token caching | PASS | `test_token_fetched_once_and_reused` fails when the cache is disabled; `test_token_refreshed_when_inside_expiry_margin` fails when the 60 s margin is removed |
| PayPal-Request-Id on create | PASS | `test_create_order_sends_request_id_and_payload` fails when the header is dropped, renamed, or its value changed |
| PayPal-Request-Id on capture | PASS | capture tests fail when capture sends no ID or a fresh UUID |
| Error -> PayPalError with debug_id | PASS | fails when debug_id is set to None, issue set to None, debug_id removed from the message, or `_check` made a no-op |
| Timeout | PASS | fails when the client timeout is removed (`timeout=None`) or a capture timeout is swallowed as success |
| Coverage well above 38% | PASS | 99% in the full suite; 94% from tests/test_paypal_client.py alone (the 4 missed lines are `approval_link`, covered by tests/test_main.py) |

## Mutation testing

Each mutation was applied to guard/paypal_client.py, tests/test_paypal_client.py run, then
the file restored (`git diff --quiet guard/` confirmed clean).

| Mutation | Caught |
|---|---|
| Disable token cache | yes |
| Remove 60 s expiry margin | yes |
| Drop PayPal-Request-Id header | yes |
| Rename header to Request-Id | yes |
| Change create request ID value | yes |
| Capture sends no request ID | yes |
| Capture sends a new UUID | yes |
| debug_id -> None | yes |
| issue -> None | yes |
| debug_id removed from error message | yes |
| `_check` never raises | yes |
| `timeout=None` | yes |
| Capture timeout swallowed as COMPLETED | yes |
| Sandbox env check disabled | yes |
| Client secret put in error message | yes |
| Amount sent as float | yes |
| Base URL switched to the live host | **no** (tests built the client from the `SANDBOX_BASE_URL` constant). Fixed by a new test, now caught |
| Header name in upper case | no. Equivalent mutant: HTTP header names are case-insensitive |

## Gaps found and tests added

1. **Live host not detected.** Input: `SANDBOX_BASE_URL = "https://api-m.paypal.com"`.
   What happened: all 19 tests passed. Expected: CLAUDE.md requires sandbox only and never
   live keys. Added `test_client_targets_the_paypal_sandbox_host` (passes on current code,
   fails on the mutation).
2. **Capture retry after a timeout.** DESIGN.md Risks: "Agent retries after a timeout ->
   same request ID". The original tests covered create retries and capture timeouts
   separately. Added `test_capture_retry_after_timeout_reuses_the_same_request_id`
   (passes). Note: the client does not retry by itself; the retry policy in DESIGN.md
   ("Retry with the same request ID, then mark failed") belongs to the caller and is
   outside this task.

No application bugs found.

## Isolation

- Network: ran tests/test_paypal_client.py with `socket.connect`, `create_connection` and
  `getaddrinfo` patched to raise: 21 passed. No test reaches the network.
- .env: a .env file exists in the repo root. Ran the file with `open` patched to raise on
  any `.env` path (patch confirmed to catch `dotenv.load_dotenv`): 21 passed. The autouse
  fixture disables `load_dotenv`, so .env is never read.

## Checks

- `ruff check .`: All checks passed
- `ruff format --check tests/`: already formatted
- `mypy .`: Success, no issues in 21 source files
- `pytest --cov`: 74 passed; guard/paypal_client.py 99%, total 86%

## Eval

`npx promptfoo@0.125.1 eval`: 5/5 passed (100%). The config still uses the mock
provider, so this score says nothing about the real intent check yet.

## Verdict

PASS

---

# Test report: PayPal 5xx/timeout retry

Task (TASKS.md): "[AI] PayPal 5xx/timeout: retry with the same request ID, then mark the
request failed. Done when: tests for retry-then-success and retry-then-fail"

Commits under test: a82b6fb (client retry in `PayPalClient._send`) and 9d0252f (record
`status` / `last_error`, `mark_failed_if_retryable`). Judged against DESIGN.md Risks
("Retry with the same request ID, then mark failed in the log") and the approved retry plan.
No application code changed.

## Done-when results

| Item | Result | Evidence |
|---|---|---|
| Retry-then-success (client) | PASS | `test_create_503_then_success_retries_with_same_request_id`, `test_token_503_then_success_is_retried`; added GET and ConnectError cases |
| Retry-then-fail (client) | PASS | `test_create_503_every_time_raises_paypal_error_after_max_attempts` (PayPalError, debug_id kept), `test_capture_timeout_every_time_raises_timeout_after_max_attempts` |
| Mark the request failed (route) | PASS | `test_create_failure_marks_record_then_same_request_id_succeeds`, `test_capture_timeout_marks_record_then_later_capture_succeeds`; added real-client tests in tests/test_retry_integration.py |
| Same request ID on every attempt | PASS | ID drop and ID regeneration mutants caught |
| 4xx never retried, never marked failed | PASS | `test_4xx_is_never_retried`, `test_capture_not_approved_does_not_mark_failed` |
| Worst case under the agent's 30 s timeout | **FAIL** | see Gap 1 |

## Gaps found

1. **The retry budget can go over the agent's 30 s timeout (not fixed; xfail test).**
   Input: first purchase after start (or after token expiry). The OAuth token call times
   out twice then succeeds, then the order call times out 3 times.
   What happened: `_headers()` calls `_get_access_token()`, which runs its own full
   `_send` with 3 attempts, nested inside the order call. Simulated time: 5 timeouts x 8 s
   + waits 0.5 + 1.0 + 0.5 + 1.0 = 43 s.
   Expected: the approved plan says "worst case 25.5 s < agent GUARD_TIMEOUT 30 s", so the
   guard answers before the agent gives up. The existing
   `test_worst_case_retry_time_fits_the_agent_timeout` checks the constants for one call
   only, not token plus order.
   Effect: the agent times out and retries while the guard is still working. That is not a
   double charge (same PayPal-Request-Id), but the agent reports a failure for an order
   that may have been created.
   Reproducer: `test_worst_case_including_token_fetch_fits_the_agent_timeout` in
   tests/test_paypal_client.py. It is marked `xfail(strict=True)` so pre-commit and CI stay
   green; once fixed it shows up as XPASS (a failure), and the marker must be removed.
   Related note, no test: `httpx.Timeout(8.0)` caps each phase (connect, write, each gap
   between reads), not the whole attempt, so a PayPal response that drips slowly can take
   longer than 8 s per attempt.

2. **Missing tests (code was correct; tests added, all pass):**
   - GET retry: `test_get_503_then_success_is_retried`
   - Network errors other than timeouts (`httpx.ConnectError`):
     `test_connect_error_then_success_is_retried_with_same_request_id`,
     `test_connect_error_every_time_is_reraised_after_max_attempts`
   - Headers rebuilt per attempt (token refreshed between attempts):
     `test_headers_rebuilt_per_attempt_so_expired_token_is_refreshed`
   - Retry log contents: `test_retry_log_never_contains_headers_token_or_body` (5xx and
     timeout)
   - The route tests used only FakePayPal, so a ConnectError and the content of
     `last_error` for real httpx errors (which carry the request and its headers) were not
     tested. New file tests/test_retry_integration.py runs the real `PayPalClient` with
     `httpx.MockTransport` behind the routes: 503 exhausted gives 502 with debug_id and
     `create_failed`, then the same request_id succeeds; ConnectError gives 502 and
     `create_failed`; capture timeout gives 504 and `capture_failed`, then capture
     succeeds; no token, `Bearer`, `Authorization` or secret in `last_error` or the
     response.

## Mutation testing

Each mutation was applied alone; tests/test_paypal_client.py and tests/test_main.py (plus
tests/test_retry_integration.py after the additions) were run, then `git checkout -- guard/`.
`git diff --quiet guard/` confirmed clean at the end.

| Mutation | Before additions | After |
|---|---|---|
| Retry 4xx | caught | caught |
| Drop PayPal-Request-Id on retry | caught | caught |
| Regenerate PayPal-Request-Id on retry | caught | caught |
| `safe_to_repeat = True` (ID-less POST retried) | caught | caught |
| GET not retried | **survived** | caught |
| Token call not retried | caught | caught |
| Headers built once outside the loop | **survived** | caught |
| No backoff / doubled backoff / same wait twice | caught | caught |
| MAX_ATTEMPTS = 4 (and = 2) | caught | caught |
| TIMEOUT_SECONDS = 10 (budget over 30 s) | caught | caught |
| Only `TimeoutException` retried, not other `TransportError` | **survived** | caught |
| Last 5xx swallowed and returned | caught | caught |
| Last timeout turned into PayPalError (502 instead of 504) | caught | caught |
| Headers in retry log (5xx path) | **survived** | caught |
| Headers in retry log (network error path) | **survived** | caught |
| No retry log at all | **survived** | caught |
| Mark failed on 4xx | caught | caught |
| Only timeouts marked failed in the route | **survived** | caught |
| last_error not cleared on create success | caught | caught |
| last_error not cleared on capture success | caught | caught |
| create_failed made terminal | caught | caught |
| capture_failed made terminal | caught | caught |
| create/capture failure labels swapped | caught | caught |
| Request headers (token) appended to last_error | **survived** | caught |
| Status not set on failure | caught | caught |

## Suite slowdown

No test sleeps or waits on a real timeout: the suite passes with `time.sleep` patched to
raise, and every client test replaces `_sleep`. The time goes into `PayPalClient.__init__`,
which builds a real `httpx.Client`. That loads the CA certificate bundle
(`ssl.create_default_context` -> `load_verify_locations`, about 0.3 s in cProfile) even
though the tests swap in a `MockTransport` client right after. Every test that builds a
`PayPalClient` pays about 0.3 to 0.5 s. At 9fadac5 the 21 client tests took 9.7 s; at
9d0252f the 30 took 13.6 s (about 0.45 s per test both times), so the slowdown grows with
the number of new tests and is not caused by the retry code. Wall time also varies with
machine load (14 to 30 s for the full suite during this run). A possible fix, your call:
let tests build `PayPalClient` without the default SSL client (for example inject the
`httpx.Client`, or patch it in the `fake_env` fixture).

## Isolation

- Ran tests/test_paypal_client.py, tests/test_main.py, tests/test_retry_integration.py,
  tests/test_progress.py and tests/test_search.py with `time.sleep`, non-local DNS and
  `socket.connect`, and `open()` of any `.env` patched to raise: all passed. No real sleep,
  no network, no .env read.
- Not part of this task, found while checking: tests/test_agent.py imports
  `agent.settings`, which calls `load_dotenv()` at import time, so collecting the full
  suite reads the real .env into the process environment (nothing is printed; with the
  network blocked the 10 agent tests still pass). Worth fixing in a later task.

## Checks

- `ruff check .`: All checks passed
- `ruff format --check tests/`: formatted
- `mypy .`: Success, no issues in 22 source files
- `pytest --cov`: 95 passed, 1 xfailed; guard/paypal_client.py 98%, guard/main.py 96%,
  total 89%

## Eval

`npx promptfoo@0.125.1 eval`: 5/5 passed (100%). Still the mock provider, so this score
says nothing about the intent check.

## Verdict

FAIL. The retry-then-success and retry-then-fail tests exist and the retry and marking
logic is correct (every mutation is now caught), but the approved budget rule (the guard
answers within the agent's 30 s timeout) breaks whenever a token fetch is needed: up to
43 s (Gap 1). Reproduced by the xfail test above.

# Test report: PayPal 5xx/timeout retry, loop 1 (shared 20 s budget)

Branch feat/guard-core, re-test of 5566152 (fix) and ec5512a (tests) after the FAIL
above (Gap 1: a token fetch could stretch one guard request to 43 s, over the agent's
30 s GUARD_TIMEOUT). Judged against the approved design: one deadline of
PAYPAL_BUDGET_SECONDS (20 s) per create_order / capture_order / get_order, shared with
the token fetch; per-try timeout = min(8 s, time left); a retry only if backoff +
MIN_TRY_SECONDS (2 s) fits; no time for the first try raises httpx.TimeoutException
without sending; clock via `self._clock`.

New file: tests/test_paypal_budget.py (14 tests, its own scripted MockTransport and
simulated clock, independent of the engineer's fakes).

## Done-when results

| Item | Result |
| --- | --- |
| Gap 1: worst case with token fetch fits the agent's 30 s | PASS. Hypothesis (400 random mixes of token/order 503s, timeouts, connect errors, slow answers, token expiry, cached or not, create/capture/get): total simulated time is always <= 20 s |
| Token and order tries share one deadline | PASS (token fetch inside a retry gets only the time left; every try's timeout equals min(8, 20 - send time)) |
| No try starts with less than 2 s left (so none after 18 s, never after the agent gives up) | PASS (property over every request sent, token and order) |
| Retry only if backoff + 2 s fits; boundary is inclusive | PASS at 2.499 / 2.5 / 2.501 s left (first retry) and 2.999 / 3.0 / 3.001 s (second retry) |
| First try with < 2 s left: TimeoutException, nothing sent | PASS at 1.999 (not sent) / 2.0 / 2.001 s (sent with that timeout), reached through real token tries, no shortcuts |
| Last error kept: 5xx -> PayPalError with debug_id, network error re-raised | PASS |
| Token expiry during retries refetched inside the same deadline | PASS (token expires on every order try: tok-1, tok-2, tok-3, all within 20 s) |
| Same PayPal-Request-Id on every order/capture try; GET has none | PASS (property) |
| create never sends a capture; capture never creates an order | PASS (property) |
| No wasted backoff (every wait is followed by a request) | PASS (property) |
| Each guard request gets its own fresh budget | PASS |
| 4xx never retried, ID-less POST tried once, no secrets in logs/errors, main.py statuses | PASS (existing tests, unchanged and green) |
| Worst-case comment updated to the real bound | PASS (says about 20 s; keeps the httpx per-phase timeout caveat, tracked in TASKS.md) |

Engineer's interpretations (not in the design), checked:
- A token refetch before a retry that leaves < 2 s: the retry is not sent and the order's
  last error is raised. Behaves as described; consistent with "stop and raise the last
  error as before".
- `_send(deadline=None)` falls back to a fresh 20 s deadline. Every public method passes
  its own deadline, so the fallback is only reachable from tests; removing the explicit
  deadlines from the public methods gives identical behaviour (equivalent mutant M10).

Observation, not a gap: if the token refetch before a retry itself fails (all its tries
time out within the budget), the caller gets the token's error (504 timeout) instead of
the order's earlier 503 (502). The record is marked failed either way and the agent may
retry safely with the same request ID; this matches the pre-fix behaviour for token
errors.

## Mutation testing

One change at a time to guard/paypal_client.py, restored with `git checkout -- guard/`
after the run (`git diff --quiet guard/` clean). "Engineer" = test_paypal_client.py +
test_retry_integration.py; "tester" = the new test_paypal_budget.py.

| Mutation | Engineer | Tester |
| --- | --- | --- |
| M1 token fetch gets its own fresh deadline | caught | caught |
| M2 fixed 8 s per-try timeout | caught | caught |
| M3 skip the fit check (`return True`) | caught | caught |
| M4 skip the first-try check | caught | caught |
| M5 first-try check `<` -> `<=` | survived | caught |
| M6 fit check `>=` -> `>` | survived | caught |
| M7 fit check ignores the backoff | survived | caught |
| M8 sleep before checking fit | caught | caught |
| M9 deadline recomputed per try | caught | caught |
| M10 public methods pass no deadline (fallback) | survived | survived (equivalent: same behaviour) |
| M11 no 8 s cap (timeout = time left) | caught | caught |
| M12 first-try check always raises timeout (drops last 5xx) | caught | survived |
| M13 first-try check uses 0 instead of 2 s | caught | caught |
| M14 budget 30 s | caught | caught |
| M15 MIN_TRY_SECONDS 0 | caught | caught |
| M16 time left measured before the token fetch | caught | caught |
| M17 fit check always uses the first backoff | survived | caught |
| M18 token fetch with deadline=None | caught | caught |
| M19 network-error path ignores the fit check | caught | caught |
| M20 5xx path ignores the fit check (wasted wait, then give up) | survived | caught |

Combined: 19 of 20 caught; the only survivor (M10) is an equivalent mutant.

## Gaps found and tests added

No real gaps. Five mutations (M5, M6, M7, M17, M20) survived the
engineer's tests, so the new boundary and property tests in tests/test_paypal_budget.py
are committed as normal passing tests (no xfail needed).

## Isolation

tests/test_paypal_budget.py, test_paypal_client.py, test_retry_integration.py and
test_main.py run with `time.sleep`, non-loopback `socket.connect`, non-local DNS and
`open()` of any `.env` patched to raise: 75 passed. No real sleep, no network, no .env
read. The new file also stubs the default httpx.Client during construction so
Hypothesis examples skip the slow SSL setup (14 tests in about 3 s).

## Checks

- `ruff check .`: All checks passed
- `ruff format --check .`: 33 files already formatted
- `mypy .`: Success, no issues in 23 source files
- `pytest --cov`: 119 passed; guard/paypal_client.py 98%, guard/main.py 96%, total 89%

## Eval

`npx promptfoo@0.125.1 eval`: 5/5 passed (100%). Still the mock provider, so this score
says nothing about the intent check.

## Verdict

PASS. Gap 1 is fixed: in every simulated combination of token and order failures one
guard request finishes within 20 s, no try starts with less than 2 s left, and every
retry reuses the same PayPal-Request-Id. Remaining known caveat (out of scope, tracked in
TASKS.md): httpx timeouts are per phase, so a slowly dripping real response can overrun
a single try.
