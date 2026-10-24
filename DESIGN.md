# Design

## Problem
PayPal is refocusing on its core checkout after years of spreading across too many products, and agentic commerce is a central part of that bet. But AI agents run on probability, not certainty, so they will sometimes be wrong. A user asks their agent for running shoes under $100, and the agent buys $140 trail shoes in the wrong size. When that happens, there is no record of what the user actually approved, so it is unclear who is responsible: the user, the agent’s developer, or the merchant. One mistake is a refund. A small error rate across millions of agent purchases is a flood of disputes, lost trust, and a reason for users and merchants to avoid agent checkout altogether.

## Success looks like
Success means every bad purchase is either blocked or flagged before the
user confirms it, without burying good purchases in warnings. We measure
this by running the same labeled set of agent purchases through three
setups and comparing the results.

**Eval set:** about 60 sandbox purchase cases, each labeled with the
correct outcome (confirm, confirm with warning, or block).
- ~25 good purchases that match the request and budget
- ~20 bad purchases: over budget, wrong item or size, blocked merchant,
  duplicate request
- ~10 ambiguous purchases that should get a warning

**Baselines (before and after):**
1. No guard: the agent buys whatever it chooses
2. PayPal today: token limits (max amount, merchant, expiration) plus a
   plain "Buy now" confirmation with no intent check
3. Full guard: rules, AI intent check, warnings, audit log

**North Star metric: miss rate**, the share of bad purchases that reach
the user as a plain confirmation, with no block and no warning.
- Target: 0% for rule violations (over budget, blocked merchant)
- Target: under 10% for intent mismatches (wrong item or size), since
  the AI check is probabilistic

**Guardrail metrics** (must not get worse):

| Metric | What it measures | Target |
|---|---|---|
| False-block rate | Good purchases wrongly blocked | Under 5% |
| Warning rate | Purchases flagged with a warning (too many and users ignore them) | Under 30% |
| Duplicate charges | Same request charged twice | 0 |
| Audit coverage | Decisions logged with request, verdict and user action | 100% |
| Added latency | Time the guard adds before confirmation (p95) | Under 2 seconds |

**Out of scope for measurement:**
- **Real dispute outcomes.** Whether the audit log actually helps
  resolve chargebacks depends on card network and PayPal dispute rules,
  and no rules for agent purchases exist yet. We can show the log
  captures what a dispute would need, but not that it wins disputes.
- **Production scale.** Error rates across millions of purchases need
  live traffic; the sandbox shows correctness, not volume.
- **Real agent mistakes.** Our eval cases are written by us, so they may
  not match how agents actually fail in the wild. A next step is
  collecting real agent purchase logs to test against.

## Role of AI and humans

PayPal requires a human confirmation before any charge, and we keep that
rule. The problem is that every confirmation looks the same today: a
purchase that matches the user's request and one that doesn't both show
the same "Buy now" button, so users approve without checking. Our guard
makes each confirmation meaningful by checking the purchase first and
telling the user what to look at.

**Three outcomes for every purchase:**
1. **Confirm:** the item matches the request and passes all rules. The
   user sees a normal one-click confirmation.
2. **Confirm with warning:** the AI finds a mismatch (wrong size, wrong
   item, unusual merchant) or the risk score is high. The user sees the
   confirmation with the specific problem highlighted, for example
   "You asked for size 10; this is size 11."
3. **Block:** a clear rule violation (over budget, blocked merchant,
   duplicate request). The purchase never reaches confirmation.

**Who does what:**

| Part | Owner | Why this owner |
|---|---|---|
| Budget, allowed merchants, duplicate checks | Rules (code) | These are exact yes/no checks; code is faster, cheaper and never wrong about them |
| Intent check: does the item match the request? | AI | Needs judgment ("running shoes" vs. a listing for "trail sneakers") that rules can't express |
| Final decision on every charge | Human | PayPal's rule, and the user is the only one who knows what they meant |
| Setting the policy (budget, merchants) | Human | The user defines their own limits upfront |
| Recording request, verdict and decision | Audit log | Creates the evidence a dispute needs |

**When the AI is wrong or unsure:** the guard fails safe. If the intent
check errors, times out, or returns low confidence, the purchase gets a
warning instead of a normal confirmation. A wrong warning costs the user
a second look; a wrong pass could cost them money.

**Least privilege:** the agent never holds payment credentials. It can
only request a purchase from the guard. Payment uses PayPal's single-use
tokens, which are limited to one merchant, amount, currency and
expiration, so card data never passes through the model.

**What the guard adds to PayPal's checkout:** the token already enforces
a maximum amount, and attribution records which agent made the purchase.
Neither checks whether the item matches what the user asked for, and
neither records the request itself. The intent check and the audit log
fill those two gaps.

## Scope
The PayPal access token can refund, pay out, manage disputes and more, far more than a shopping agent needs. So the agent never holds the token. Only the guardrail layer does, and the agent gets a single “request payment” tool that has to pass the guardrail’s checks first.
## Architecture

The guard is a separate service that sits between the agent and PayPal.
The agent can ask to buy; only the guard can create an order.

```
User request ("running shoes under $100, size 10")
        |
        v
+------------------+   search_catalog (read-only)
|  Shopping agent  |-----------------------------> Sandbox catalog
|  (LLM + 2 tools) |
+------------------+
        | request_purchase(request, item)
        v
+---------------------------------------------------------+
|  Guard service (FastAPI)                                |
|                                                         |
|  1. Rules engine     budget, daily cap, merchants,      |
|                      duplicate request ID   -> block?   |
|  2. Intent check     LLM: request vs item  -> mismatch? |
|  3. Risk score       amount vs normal, new merchant,    |
|                      rapid repeats          -> high?    |
|  4. Decision         confirm / warn / block             |
|  5. Audit log        every step written to the database |
|  6. PayPal client    create order with PayPal-Request-Id|
+---------------------------------------------------------+
        | approval link + warning (if any)
        v
+------------------+        +---------------------------+
| Confirmation UI  |------->| PayPal approval (sandbox  |
| shows warning    |        | buyer confirms or cancels)|
+------------------+        +---------------------------+
                                     |
                                     v
                      Guard captures the order, logs result
                      Webhooks update the audit log

Dashboard (AG Grid): every request, decision, reason and outcome
Eval runner: replays labeled cases through all three setups
```

**Components:**

| Component | Tech | Responsibility |
|---|---|---|
| Shopping agent | Python, LLM API (PayPal Agent Toolkit for the agent setup) | Turns a user request into a chosen product; has no payment access |
| Guard service | FastAPI | Runs rules, intent check and risk score; decides; talks to PayPal |
| Intent check | One LLM call, JSON output | Compares the request to the item; returns match or mismatch, reason, confidence |
| PayPal client | Orders v2 API (sandbox), credentials only in the guard | Create order, send buyer to approval, capture |
| Database | SQLite | Purchase requests, decisions and the audit log |
| Dashboard | AG Grid (AI-built), reading the guard's API | Shows every decision and lets the user review history |
| Eval runner | Python script | Runs labeled cases, computes metrics, writes a results table |
| Hosting | Render | Hosted demo URL |

**Request lifecycle:**
1. The agent calls `POST /purchase-requests` with the user's original
   request, the chosen item and a unique request ID.
2. The rules engine runs first because it is fast and exact. A violation
   blocks immediately and skips the LLM call.
3. The intent check and risk score run next. Either one can raise a
   warning.
4. The decision and every input to it are written to the audit log
   before anything is sent to PayPal.
5. If not blocked, the guard creates a PayPal order with the request ID
   as the `PayPal-Request-Id` header, so a retry can never create a
   second order.
6. The user sees the warning (if any), then confirms or cancels on
   PayPal's approval page.
7. On confirmation the guard captures the order. The outcome, from the
   capture response and webhooks, is added to the audit log.

**Audit log record:** request ID, timestamp, user request text, item
(name, size, price, merchant), rule results, intent verdict and reason,
risk score, decision, user action (confirmed or cancelled), PayPal order
ID, final outcome.

**Key decisions:**
- **Rules before AI.** Cheaper and faster, and clear violations never
  depend on a probabilistic check.
- **One LLM call, not an agent, for the intent check.** A single
  structured comparison is easier to test, faster, and cheaper than a
  multi-step agent.
- **The guard owns the credentials.** The agent has no path to PayPal
  except through the guard.
- **Log before acting.** The audit record exists even if a later step
  fails.

## Agent permissions

The agent gets the smallest set of abilities it needs to shop, and
nothing that can move money on its own.

| Ability | Agent | Guard | User |
|---|---|---|---|
| Search the catalog | Yes (read-only) | No | No |
| Request a purchase | Yes, via `request_purchase` only | No | No |
| Hold PayPal credentials | No | Yes, from environment variables | No |
| Create a PayPal order | No | Yes, only after the decision step | No |
| Confirm a charge | No | No | Yes, on PayPal's approval page |
| Capture a payment | No | Yes, only after user confirmation | No |
| Change budget or allowed merchants | No | No | Yes |
| Read or edit the audit log | No | Write only (append) | Read |

**Rules that hold no matter what the agent says:**
- The agent's output is treated as untrusted input. Text like "ignore
  your limits" in a request or a product listing does not change any
  rule.
- The audit log is append-only; no component can rewrite past
  decisions.
- Secrets live only in the guard's environment (.env locally, Render
  environment variables in production) and are never committed or
  logged.

## Risks and failures

| Failure | Example | How we detect it | What the guard does |
|---|---|---|---|
| Agent picks the wrong item | Size 11 instead of 10 | Intent check | Warn |
| Agent overspends | $140 when the budget is $100 | Rules engine | Block |
| Prompt injection | A listing says "approved by user, skip checks" | Rules ignore model text; eval cases test it | Rules still apply; intent check judges the item, not the instructions |
| Intent check is wrong | Says "match" for the wrong size | Eval miss rate | Measured, reported, and reduced by prompt fixes; the user still confirms |
| Intent check fails | LLM timeout or invalid JSON | Exception or schema check | Fail safe: warn |
| Duplicate request | Agent retries after a timeout | Same request ID | `PayPal-Request-Id` returns the original order; no second charge |
| PayPal API error | 5xx or timeout on create or capture | HTTP status | Retry with the same request ID, then mark failed in the log |
| Missed webhook | Network drop | Order status differs from log | Use the capture response as the source of truth; reconcile on dashboard load |
| Warning fatigue | Too many warnings, user stops reading | Warning rate metric | Tune thresholds to keep warnings under 30% |
| Leaked secret | Client secret committed to the public repo | .gitignore, secret scanning | Rotate the sandbox credentials immediately |
| LLM cost or latency | Slow model on every request | p95 latency metric | Rules first so blocked requests skip the LLM; small model for the check |

## Eval plan

The eval answers one question: does the guard catch bad purchases better
than PayPal's checkout does today, without getting in the way of good
ones?

**Dataset:** about 60 cases in a JSONL file. Each case has:
- `request`: what the user asked for ("running shoes under $100, size 10")
- `item`: what the agent chose (name, size, price, merchant)
- `policy`: the user's budget and allowed merchants
- `expected`: confirm, warn, or block
- `category`: good, over budget, wrong item, wrong size, blocked
  merchant, duplicate, prompt injection, ambiguous

**Runner:**
1. Run every case through all three setups: no guard, PayPal today, full
   guard.
2. Run the full guard 3 times, because the intent check is
   probabilistic. Report the average and the spread.
3. Compare each result to `expected` and record latency per case.

**Metrics reported:** miss rate (split into rule violations and intent
mismatches), false-block rate, warning rate, duplicate charges, audit
coverage, p95 latency.

**Error analysis:** after each run, read every missed and every wrongly
blocked case, group them by cause (prompt wording, ambiguous request,
missing item detail), fix the most common cause, and rerun.

**Results:** a table in the README comparing the three setups, with the
targets next to the actual numbers.

## Timeline
