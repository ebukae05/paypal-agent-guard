# Design

## Problem
PayPal is refocusing on its core checkout after years of spreading across too many products, and agentic commerce is a central part of that bet. But AI agents run on probability, not certainty, so they will sometimes be wrong. A user asks their agent for running shoes under $100, and the agent buys $140 trail shoes in the wrong size. When that happens, there is no record of what the user actually approved, so it is unclear who is responsible: the user, the agent’s developer, or the merchant. One mistake is a refund. A small error rate across millions of agent purchases is a flood of disputes, lost trust, and a reason for users and merchants to avoid agent checkout altogether.

## Success looks like
Success means the guard catches the purchases a user never intended,
without getting in the way of the ones they did. We measure this by
running the same labeled set of agent purchases through three setups
and comparing the results.

**Eval set:** about 60 sandbox purchase cases, each labeled with the
correct outcome (approve, ask the user, or block).
- ~25 good purchases that match the request and budget
- ~20 bad purchases: over budget, wrong item or size, blocked merchant,
  duplicate request
- ~10 ambiguous purchases that a human should review

**Baselines (before and after):**
1. No guard: the agent buys whatever it chooses
2. Spending limit only: a simple budget check
3. Full guard: policy rules, AI intent check, human approval, audit log

**North Star metric: miss rate**, the share of bad purchases that go
through without a human seeing them.
- Target: 0% for rule violations (over budget, blocked merchant)
- Target: under 10% for intent mismatches (wrong item or size), since
  the AI check is probabilistic

**Guardrail metrics** (must not get worse):

| Metric | What it measures | Target |
|---|---|---|
| False-block rate | Good purchases wrongly blocked | Under 5% |
| Approval rate | How often the user is asked to approve | Under 30% |
| Duplicate charges | Same request charged twice | 0 |
| Audit coverage | Decisions logged with a reason | 100% |
| Added latency | Time the guard adds to checkout (p95) | Under 2 seconds |

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

## Scope
The PayPal access token can refund, pay out, manage disputes and more, far more than a shopping agent needs. So the agent never holds the token. Only the guardrail layer does, and the agent gets a single “request payment” tool that has to pass the guardrail’s checks first.
## Architecture

## Agent permissions

## Risks and failures

## Eval plan

## Timeline
