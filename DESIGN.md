# Design

## Problem
PayPal is refocusing on its core checkout after years of spreading across too many products, and agentic commerce is a central part of that bet. But AI agents run on probability, not certainty, so they will sometimes be wrong. A user asks their agent for running shoes under $100, and the agent buys $140 trail shoes in the wrong size. When that happens, there is no record of what the user actually approved, so it is unclear who is responsible: the user, the agent’s developer, or the merchant. One mistake is a refund. A small error rate across millions of agent purchases is a flood of disputes, lost trust, and a reason for users and merchants to avoid agent checkout altogether.

## Success looks like

## Role of AI and humans

## Scope
The PayPal access token can refund, pay out, manage disputes and more, far more than a shopping agent needs. So the agent never holds the token. Only the guardrail layer does, and the agent gets a single “request payment” tool that has to pass the guardrail’s checks first.
## Architecture

## Agent permissions

## Risks and failures

## Eval plan

## Timeline
