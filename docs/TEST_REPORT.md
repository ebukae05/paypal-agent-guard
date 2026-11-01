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
