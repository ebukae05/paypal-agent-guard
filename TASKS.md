# TASKS.md: paypal-agent-guard

Source of truth: DESIGN.md. One task at a time, top to bottom.
Tags: [AI] Claude may implement · [YOU] hand-written zone (CLAUDE.md): Claude
gives hints only · [CHECK] stop and wait for me.
Tick a box only when its "Done when" is true and ruff, mypy and pytest pass.
Keep the `- [ ]` format: loop.sh counts unchecked boxes by it.

Schedule (DESIGN.md timeline): rules/idempotency were due Oct 28-29, so we're
behind. If needed, cut in DESIGN.md order: stretch goal, then webhooks, then
risk score. Never cut rules, intent check, audit log or evals.

## Done: setup and Milestone 1 pieces

- [x] [AI] PayPal sandbox client: OAuth, create order with PayPal-Request-Id, capture. Evidence: `guard/paypal_client.py`, `scripts/smoke_payment.py` captured COMPLETED in sandbox (4c4574e)
- [x] [AI] Guard service stub: POST /purchase-requests, capture route, GET /health. Evidence: `guard/main.py`, tests/test_main.py (e2efc7a)
- [x] [AI] Sandbox catalog and search_catalog tool. Evidence: `catalog/`, tests/test_search.py (d547105)
- [x] [AI] Shopping agent loop with search and request_purchase tools. Evidence: `agent/`, tests/test_agent.py
- [x] [AI] Idempotency: a retry never double-charges. Evidence: test_capture_twice_calls_paypal_once, test_capture_retry_after_failure_sends_same_key, test_same_request_id_different_payload_is_409, test_guard_timeout_then_same_product_retry_reuses_request_id
- [x] [AI] Dev tooling and CI: ruff, mypy, pytest-cov, pre-commit + gitleaks, ci.yml, promptfoo scaffold (PR #1)
- [ ] [CHECK] Live M1 run: `python -m agent.run "..."` → guard → approve as sandbox buyer → capture COMPLETED

## Hardening what exists

- [ ] [AI] Tests for guard/paypal_client.py (idempotency, declines, timeouts). Done when: mocked HTTP tests cover token caching, PayPal-Request-Id on create and capture, error → PayPalError with debug_id, timeout; coverage of the file well above 38%
- [ ] [AI] PayPal 5xx/timeout: retry with the same request ID, then mark the request failed. Done when: tests for retry-then-success and retry-then-fail

## Rules

- [ ] [AI] Policy: the user's per-purchase limit, daily cap and allowed merchants, stored by the guard. Done when: tests load and validate a policy
- [ ] [AI] Rules engine: per-purchase limit, daily cap, allowed merchants, duplicate request. Price and merchant come from the catalog by product_id, not from the agent. Done when: each rule has a pytest test, plus a Hypothesis property (any price over the limit blocks)
- [ ] [AI] Wire rules into POST /purchase-requests: a violation blocks before PayPal is called. Done when: test proves a blocked request never creates an order

## Intent check, risk, decision, audit log

- [ ] [AI] Intent check plumbing: LLM client, timeout, model setting; tests use a mocked LLM. Done when: a mocked call returns raw text to the parser
- [ ] [YOU] Intent check prompt (evals/intent_prompt.txt and the code that uses it)
- [ ] [YOU] Parse and validate the JSON response (match or mismatch, reason, confidence). Done when: unit tests with mocked LLM pass
- [ ] [YOU] Fail safe: error, timeout, invalid JSON or low confidence returns a warning. Done when: a test for each case passes
- [ ] [AI] Swap the promptfoo mock provider for the real intent check. Done when: `npx promptfoo@0.125.1 eval` runs against it
- [ ] [AI] Risk score: amount vs normal, new merchant, rapid repeats. Done when: tests pass
- [ ] [AI] Decision: confirm, warn or block from rules + intent + risk. Done when: tests cover all three outcomes
- [ ] [AI] SQLite store + append-only audit log (request, item, rule results, intent verdict, risk, decision, user action, request ID, order ID, outcome), replacing the in-memory PURCHASES. Done when: every decision writes one row before PayPal is called
- [ ] [AI] Show the warning before the PayPal approval link
- [ ] [CHECK] I review the full purchase flow by hand

## Evals (Milestone 2)

- [ ] [YOU] Write ~60 labeled eval cases (confirm, warn, block), including prompt injection in listings
- [ ] [AI] Eval runner: 3 setups (no guard, PayPal today, full guard), full guard 3 runs; miss rate, false-block rate, warning rate, duplicate charges, audit coverage, p95 latency
- [ ] [AI] Fix what the evals exposed; rerun
- [ ] [CHECK] Milestone 2: I approve the numbers
- [ ] [AI] Write the results table into the README

## Dashboard, webhooks, deploy (Milestone 3)

- [ ] [AI] AG Grid dashboard: read-only view of every request, decision, reason, risk and outcome (list every fetch call for me)
- [ ] [AI] Webhooks: completed and denied payments update the audit log; reconcile on dashboard load
- [ ] [AI] Dockerfile and Render config; health check passes on the hosted URL
- [ ] [CHECK] I test from a fresh clone using only the README

## Finish

- [ ] [AI] README: problem, architecture diagram, eval table, how to run, tools used
- [ ] [AI] Stretch, only if everything above is done: guard as an MCP tool
- [ ] [CHECK] Feature freeze; final eval run; I record the demo and submit by Nov 12 noon

## Progress notes

(Claude appends one line per finished task: date, task, what changed, test result.)
- 2026-11-01: TASKS.md created and synced with the code; setup, M1 pieces and idempotency ticked with evidence.
