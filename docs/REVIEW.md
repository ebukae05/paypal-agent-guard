# Review: PayPal 5xx/timeout retry (feat/guard-core)

Scope reviewed: `git diff 9fadac5..HEAD -- guard/ tests/ docs/TEST_REPORT.md`
(commits a82b6fb, 9d0252f, daf6ce8, 5566152, ec5512a, 3faf14b; 6dad5de ignored, TASKS.md only).

Task (TASKS.md:26): "[AI] PayPal 5xx/timeout: retry with the same request ID, then mark the
request failed. Done when: tests for retry-then-success and retry-then-fail."
DESIGN.md Risks (line 223): "5xx or timeout on create or capture -> retry with the same
request ID, then mark failed in the log."

## Verdict: CHANGES REQUESTED

One small item blocks this: a mismatch in the money path between "is this POST safe to
retry?" and "does this POST carry a PayPal-Request-Id?" (finding 1). Routes cannot trigger it
today, but it breaks the invariant the design and the docstring promise, and no test catches
it. Everything else is right. The retry loop, the shared deadline and the record statuses
match the approved design, and the tests really check the money properties.

## Checks (run by the reviewer)

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 33 files already formatted |
| `mypy .` | Success, no issues in 23 source files |
| `pytest --cov` | 119 passed, 0 xfail; guard/paypal_client.py 98%, guard/main.py 96%, total 89% |

The xfail reproducer from the first tester FAIL is gone (`grep xfail tests/` is empty), so
Gap 1 is closed with a normal passing test.

## Change requests

### 1. [Medium, blocking] guard/paypal_client.py:136 vs :115: an ID-less POST can be retried

`_send` decides a POST is safe to repeat when `request_id is not None` (line 136). But
`_headers` only sends the `PayPal-Request-Id` header when `request_id` is truthy (line 115).
With `request_id=""` the POST counts as safe, gets 3 attempts, and goes out each time with
**no** idempotency header. I confirmed this with a scratch script against `MockTransport`:
`capture_order("O-1", "")` with a 503 every time sent 3 capture POSTs, and every one had
`PayPal-Request-Id = None`.

Why it matters: the approved design and the `_send` docstring (lines 124-126) say a POST is
retried only if it carries a PayPal-Request-Id. Here the code checks one condition and the
header depends on a different one, so the safety rule rests on two checks staying in sync by
accident. No route can reach it today: `PurchaseRequest.request_id` has `min_length=1`
(main.py:71), and the capture key is always `f"{request_id}-capture"` (main.py:183). The next
caller of `create_order` or `capture_order` (a refund, a re-drive job, the audit-log
milestone) could reach it. An ID-less create retried 3 times can create 3 orders. No test
covers an empty or falsy request ID; the tester mutant `safe_to_repeat = True` only covers
`None`.

### 2. [Low] TASKS.md:26: task not ticked, no Progress note

CLAUDE.md, "How work flows": after each finished task, tick the box and add one line to
Progress notes. The earlier hardening task (TASKS.md:24) was ticked with evidence on the
branch. This one is not ticked and has no evidence line yet.

## Non-blocking notes (no change required for this PR)

- **guard/paypal_client.py:144: a token failure on a retry hides the order error.** The
  token fetch for attempt 2 or 3 runs outside the `try`. If it fails, its exception
  propagates instead of the last order 5xx. The tester noted this for the timeout case
  (504 instead of 502). There is one more edge case. If the refetch returns a **4xx** (for
  example 401 after a credentials rotation), `mark_failed_if_retryable` (main.py:107-109)
  sees a 4xx and leaves the status at `pending`/`order_created`, even though the
  order/capture call just got 5xx errors. That is rare and not a money risk (the same
  request ID is reused on the next call), but the record then does not say "failed" when it
  should.
- **guard/paypal_client.py:27-31: "about 20 s" is the best case, not the whole bound.**
  Here is what the code does guarantee: no try (token or order) **starts** later than
  `deadline - MIN_TRY_SECONDS`, which is 18 s after the public method was called. Lines 146
  and 172-177 check `self._clock()` right before every request and before every backoff
  sleep. Each route calls exactly one public method, so it gets one deadline. So no new
  PayPal request starts after the agent 30 s `GUARD_TIMEOUT` (agent/settings.py:19). But a
  try that is already running can last past `left`. httpx applies the timeout to each phase
  separately, so connect and read can each use up to `left`. Even without a slow drip, try 1
  can use about 8+8 s and try 2 about 3.5+3.5 s, which ends around 27 s, close to 30 s. A
  slow-dripping response has no upper bound at all. The comment mentions this caveat, and
  TASKS.md:27 tracks the fix. If the agent gives up while a capture is in flight, the agent
  retries with the same request ID and PayPal returns the stored result, so there is still
  no double charge.
- **guard/paypal_client.py:134-135: the `deadline=None` fallback is unused in production.**
  Every public method passes its own deadline. Only tests use the fallback. It is harmless,
  but it is one more code path to keep in mind.

## Money-safety checklist (verified from code, not from the reports)

1. **Same PayPal-Request-Id on every try.** PASS. The headers are rebuilt on each attempt
   (line 144) from the same `request_id` argument. A token refresh between tries changes
   only `Authorization`. The capture key is still the fixed string `f"{request_id}-capture"`
   (main.py:183), so a later call with the same request_id sends the same key.
   tests/test_retry_integration.py:117 and :146 check the real header on the wire across
   failed and later successful calls (`["req-1"] * 4`, `["req-1-capture"] * 4`).
   tests/test_paypal_budget.py:170 checks it as a property. Exception: finding 1.
2. **No new try after the agent gives up.** PASS for try starts (bound 18 s, see the note
   above). Backoff sleeps happen only after `_retry_fits` has checked that backoff + 2 s
   still fits. A nested token fetch uses the same deadline (line 100). The in-flight
   overrun caveat is tracked in TASKS.md:27.
3. **No unsafe retries and no unrequested captures.** PASS apart from finding 1. A 4xx goes
   straight to `_check` (line 168) and is never retried. An authed POST with
   `request_id=None` gets one attempt. `capture_order` is called in exactly one place, the
   capture route (main.py:183), and `create_order` only sends to `/v2/checkout/orders`. A
   capture before buyer approval gets the PayPal 422 ORDER_NOT_APPROVED, which is a 4xx, so
   it is not retried, the route answers 409 and the status is unchanged
   (`test_capture_not_approved_does_not_mark_failed`). Sandbox only: unchanged
   (paypal_client.py:70-73).
4. **No secrets.** PASS. `_log_retry` logs the method, the URL (holds the order id only), the
   status or exception class name, and the debug_id. `last_error` is `str(exc)`. The
   PayPalError message holds only safe fields, and httpx exception messages do not include
   the request headers. `last_error` is not part of any response model. Tested with real
   httpx exceptions that carry the request (test_retry_integration.py:97-99, :109, :138-139)
   and in the retry-log test.
5. **Decision recorded before PayPal is called.** PASS. `PURCHASES.setdefault` runs before
   `create_order` (main.py:131).

## Scope, design and style

- In scope. The only changes are the client retry, the shared deadline, and record
  `status`/`last_error`. Nothing touches the hand-written zones (intent check, evals).
- Matches the approved design: MAX_ATTEMPTS 3, backoff 0.5/1.0, 8 s cap, a 20 s budget shared
  with the token fetch, MIN_TRY_SECONDS 2 (with an inclusive boundary), TimeoutException
  with nothing sent when the first try has under 2 s left, failed is not terminal, success
  clears `last_error`, and a 4xx leaves the status unchanged.
- The style matches the surrounding code: plain functions, short comments in plain English,
  no new dependencies.
- Tests: retry-then-success and retry-then-fail are covered at the client level and at the
  route level with the real client. The budget property test (Hypothesis, 400 mixes) checks
  "no try starts with under 2 s left" and "total time at most 20 s" on a simulated clock.

## PR description (none exists yet)

CLAUDE.md requires a plain-words explanation for changes touching payments or idempotency.
The PR should explain:
- which calls are retried and why ID-less POSTs are not
- the shared 20 s deadline, and the 18 s "last start" bound against the agent 30 s timeout
- the per-phase timeout caveat and its TASKS.md follow-up
- what `create_failed`/`capture_failed` mean, and that they are not terminal
- why 4xx is never retried or marked
- how to test: `pytest tests/test_paypal_client.py tests/test_paypal_budget.py
  tests/test_retry_integration.py tests/test_main.py`

## For the human to check personally before merging

1. Read paypal_client.py:121-170 (`_send`) line by line, especially how the nested token
   fetch at line 144 uses the same deadline. This is the piece to be able to explain in an
   interview.
2. Decide on finding 1, and whether an empty request ID should be a programming error.
3. PayPal keeps `PayPal-Request-Id` results only for a limited time (check the current PayPal
   docs for Orders v2 create and capture). A `create_failed` record retried after that
   window could create a second order. That is outside this task, but it limits what "failed
   is not terminal" can promise.
4. Optional: one sandbox run where you kill the network during capture, then capture again
   with the same request_id, and confirm in the sandbox dashboard that there is only one
   capture.
