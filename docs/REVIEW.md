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

---

# Re-review (commits 42b714c, ef0305a, b1bc728)

Scope: `git diff 6265afb..HEAD`. The only production change is in `guard/paypal_client.py`
(two comment blocks and one condition). The rest is tests and TEST_REPORT.md. Nothing in
`guard/main.py`, `agent/` or config changed.

## Verdict: APPROVE

## Blocking finding 1: resolved

- paypal_client.py:141 now reads `safe_to_repeat = method == "GET" or bool(request_id) or not authed`.
  That is the same truthiness check as paypal_client.py:119 (`if request_id:`), which decides
  whether the header is sent. A POST now gets retries exactly when PayPal-Request-Id is on the
  wire. Comments on both lines point to each other, so someone editing one later will see the
  other.
- Tests: `tests/test_paypal_client.py::test_post_with_empty_request_id_is_not_retried` (one
  try, no header, no backoff). `tests/test_paypal_request_id_retry.py` covers None, "", 0,
  False and b"" (no header, one try) and real IDs (same header on every try). It also has a
  Hypothesis property: "POST retried if and only if the header is on the wire". These tests
  check the rule itself, not just the one case I flagged.

## Money-safety checklist, rechecked against the code

- Same PayPal-Request-Id on every try: yes. `_headers(request_id, ...)` is rebuilt every try
  from the same argument (paypal_client.py:149). The capture ID is always `f"{request_id}-capture"`
  (main.py:183).
- No try starts after the agent's 30 s give-up: unchanged. A try needs `left >= MIN_TRY_SECONDS`
  (paypal_client.py:151) and a retry needs backoff + 2 s to fit (`_retry_fits`) inside the
  20 s deadline. So the last start is at 18 s.
- No retry of 4xx: unchanged. Only `>= 500` and `TransportError` loop. Everything else goes to
  `_check`.
- No retry of an ID-less POST: now true by construction (above).
- No capture without approval: capture route unchanged in this diff.
- Secrets: retry logging still prints only method, URL, status and debug_id. The test fakes use
  placeholder credentials. Nothing new reads or prints `.env`.

## Non-blocking notes (for the human; not change requests)

1. paypal_client.py:32, wording, low. "a try can take roughly 2x the time left" undercounts.
   httpx applies the timeout separately to each phase (pool, connect, write, and each read
   gap), so one try can take several times the time left before the body starts. The next
   clause ("no upper bound") is the accurate claim, and the guarantee the code relies on (no
   new try starts after 18 s) is stated correctly. It doesn't block because no behavior
   depends on it. Fix the number or drop it when you take the TASKS.md follow-up for a
   whole-try deadline.
2. main.py:71, input validation, low. `request_id=" "` passes `min_length=1`. It counts as a
   real ID: the header is sent and retries happen, so the client stays consistent with
   itself. The risk is only on create. A whitespace-only header value may get trimmed to
   empty somewhere on the way to PayPal, and then a retried create could make a second
   *order*. That is not a second charge: each order still needs the buyer's approval, and
   the capture ID `" -capture"` is never blank. Decide whether request_id should be
   restricted to a pattern (for example no leading or trailing whitespace) as its own
   small task.

## Checks (run by the reviewer on b1bc728)

- `ruff check .`: All checks passed
- `ruff format --check .`: 35 files already formatted
- `mypy .`: no issues in 24 source files
- `pytest --cov`: 166 passed, total coverage 89%, guard/paypal_client.py 98%

## Still for the human before merging

Items 1, 3 and 4 from the first review's "check personally" list still apply. Item 2 is
settled by this fix. The PR description still needs the plain-words explanation listed above,
and should now add: "a POST is retried only if it carries a PayPal-Request-Id; an empty
ID means one try."

---

# Review: cap each whole PayPal try (feat/try-cap)

Base: origin/main d9833f0. Commits: a777138 (feat), e35bb28 (tests), 1f5e448 (tester attack
tests + de-flake), d366587 (comment fix, TASKS.md tick, follow-up task). Task: TASKS.md,
Hardening, "Cap each whole PayPal try". Plan: staged-jingling-lighthouse.md (approved).

Note: the local `main` ref is stale (4d3da4b, before PR #3), so `git diff main...HEAD` also
shows PR #3's changes. This review uses `d9833f0..HEAD`, which matches `origin/main`. Open
the PR against `origin/main`.

## Verdict: APPROVE

No change requests. The change matches the approved plan, stays in scope (one helper, one call
site, comments, tests, TASKS.md) and keeps every money-safety rule. Tester finding F1 (an
abandoned dripping thread keeps running) is handled the way the user chose: the comment at
paypal_client.py:196-199 now says so, and TASKS.md has the follow-up task. It is not requested
again here.

## Checks (run by the reviewer on d366587)

- `ruff check .`: All checks passed
- `ruff format --check .`: 37 files already formatted
- `mypy .`: no issues in 26 source files
- `pytest`: 196 passed, 1 xfailed (the strict xfail for F1), 72 s locally

## Scope, zones, CLAUDE.md

- Diff touches only guard/paypal_client.py, two new test files, docs/TEST_REPORT.md and
  TASKS.md. main.py, the agent and DESIGN.md are unchanged, as the plan said.
- Hand-written zones untouched: no intent check code, no eval cases or labels.
- Secrets: none. Tests use placeholder credentials and never read `.env`. The new
  TimeoutException message holds only the limit. Logging is unchanged.
- Sandbox only: `SANDBOX_BASE_URL` and the `PAYPAL_ENV` check are unchanged. The F1 test uses
  a local 127.0.0.1 server, not PayPal.
- DESIGN.md Risks row "PayPal API error": still retry with the same request ID, then mark
  failed. An overrun becomes `httpx.TimeoutException`, so main.py marks `*_failed` and answers
  504, as before.

## Money-safety checklist (checked against the code)

- Same PayPal-Request-Id on every try: headers are still built in `_send` (line 152) from the
  same `request_id` and passed through `**kwargs` into the thread. The capped helper never
  touches headers. Tested for create and capture overruns (engineer tests) and with a
  PayPal-like server that charges once (`test_overrun_capture_that_succeeded_at_paypal_is_charged_once`).
- Late responses: each call gets its own `outcome` dict (line 188), and the overrun path
  raises before it reads `outcome` (lines 203-204). So a late answer can't be returned or show
  up in a later try. Tested (`test_late_response_of_an_overrun_try_is_never_returned`).
- Exception passthrough: `run()` catches BaseException and the caller re-raises the same
  object (line 206), so ConnectError, ReadTimeout and 5xx take the same retry path as before.
  Tested by identity (`is`).
- No retry of an ID-less POST: unchanged. `safe_to_repeat` still decides attempts. An overrun
  is a TransportError, which `_retry_fits` only allows while `attempt < attempts`. Tested for
  None and "".
- Capture only after approval, and the log before PayPal: not changed by this diff.
- Limit at least 2 s: `left >= MIN_TRY_SECONDS` is checked (line 154) before the helper runs,
  so `join(limit)` never gets zero or a negative number.
- Guarantee comment (lines 28-37): accurate for the caller. Each try is cut off at
  `min(TIMEOUT_SECONDS, left)`, the token fetch shares the deadline and is capped the same
  way, and retries only start if `_retry_fits`. The Hypothesis property in the attack file
  checks this for 200 failure mixes. See note 2 for the small extra time it leaves out.

## Non-blocking notes (for the human; not change requests)

1. paypal_client.py:35-37 and 202-204, overlapping tries, low money risk, worth knowing.
   Before this PR a try that ran long ended (and closed its connection) before the retry
   started. Now the retry can go out while the abandoned try is still in flight with the
   same PayPal-Request-Id. This is not only the dripping case: any try whose phases add up to
   more than the limit can do it, for example a 5 s connect followed by a 5 s read wait. The
   comment says PayPal "returns the stored result", but for a request that is still in
   flight that hasn't been checked yet. PayPal might answer with a 4xx such as "previous
   request in progress". If it does: main.py:101-113 treats a 4xx as not retryable, so the
   record keeps its old status ("pending" or "order_created"), the route answers 502 (or 409),
   and the agent's next retry gets the stored result. Money is still safe. A capture uses the
   same ID and an order can only be captured once. A duplicate create at worst makes a second
   order that nobody approves, because only the recorded order's approval link goes to the
   buyer. The sandbox check is already part of the follow-up task in TASKS.md. The PR
   description should say that tries can now overlap.
2. paypal_client.py:31, wording. "returns within PAYPAL_BUDGET_SECONDS (20 s)" leaves out a
   few milliseconds: thread start, a late `join` wake-up, and parsing JSON after the response.
   That's harmless with 10 s of headroom before the agent's 30 s `GUARD_TIMEOUT`. Just don't
   quote it as an exact bound in an interview.
3. paypal_client.py:201, rare failure path. `thread.start()` can raise `RuntimeError` if the
   OS runs out of threads, for example after a long outage leaves many abandoned dripping
   threads (F1). That isn't a TransportError, so the route answers 500 and the record stays
   "pending" instead of being marked failed. A retry is still safe because it uses the same
   ID. The F1 follow-up task removes the cause.
4. Shared `httpx.Client` used from several threads (an abandoned try plus the next one). The
   httpx client's connection pool is safe for requests sent from several threads at once, and
   the sync FastAPI routes already shared it across worker threads. Nothing new here, except
   that F1 can fill the pool (100 connections). Then `PoolTimeout`, a TimeoutException, makes
   later tries fail fast and get marked failed.
5. CI reliability of real-time tests. The single-try tests allow 0.2 s past the limit
   (tests/test_paypal_try_cap.py:23). `test_fast_response_comes_through_normally` (line 162)
   wants two mock requests done in under 0.3 s. Multi-try tests allow 0.5 s. The tester's
   loaded runs passed after the de-flake. CI also runs under `--cov` tracing on a shared
   runner, which is slower. Expect these to pass. If one flakes, rerun it and widen that
   test's margin. Never loosen the cap itself. The strict xfail (attack file, line 466) is
   safe on CI: the drip server runs for 3 s and the check happens about 1.3 s in.
6. Simplicity: the helper is about 15 lines, has no new dependency and duplicates nothing.
   The ~28 s of real-time test cost is the price of testing a real wall-clock cap. The
   existing "Faster tests" task covers the other half of the suite's time.

## PR description (none exists yet)

Because this touches retries and the PayPal-Request-Id, the description must explain in plain
words:
- Why: httpx times each phase separately, so a slowly dripping response never timed out and
  could outlast the agent's 30 s wait.
- What: each try runs in a daemon thread. The caller waits at most the try's limit. An
  overrun becomes a normal timeout (retry with the same ID, or 504 and the record marked
  failed).
- Edge cases: a late answer is thrown away. A try that was cut off may still have succeeded
  at PayPal, and the retry with the same ID gets the stored result, so there is no double
  charge. The retry can overlap the abandoned try (note 1). An abandoned dripping thread keeps
  its connection until PayPal stops (F1, follow-up task). An ID-less POST is still tried once.
- How to test: `pytest tests/test_paypal_try_cap.py tests/test_paypal_try_cap_attack.py`. The
  Done-when test is `test_slow_drip_body_is_cut_off_at_the_limit`.

## For the human to check personally before merging

1. Read `_request_with_cap` (paypal_client.py:180-207) and be able to explain three things:
   why the overrun check comes before reading `outcome`, why `run()` catches BaseException,
   and why a daemon thread is used.
2. Decide whether note 1 (overlapping tries with the same ID) is fine to leave for the sandbox
   check in the follow-up task. My view: yes, since no money can move twice.
3. Make sure the PR targets `origin/main` (your local `main` is stale) and that the first CI
   run is green. Watch for the real-time tests in note 5.
