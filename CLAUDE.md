# CLAUDE.md

This is paypal-agent-guard, a guardrail layer for AI shopping agents
(PayPal AI Hackathon). Read DESIGN.md before planning any work; it is the
source of truth for architecture, permissions, risks and the eval plan.

I am a student learning AI engineering. My goal is to understand every
part of this project well enough to explain it in an interview without AI.
Follow the rules below on every task.

## Hand-written zones: do NOT write this code

I write these myself. For these files and features, you may explain
concepts, give hints, answer questions, point out bugs, and review my
code, but you must not write or edit the implementation, even if I seem
stuck or the task would be faster. If a task touches one of these, stop
and tell me which part is mine to write.

1. **FastAPI endpoints**: route functions, request and response models
   (`POST /purchase-requests`, `GET /health`, approve and capture routes).
2. **Rules engine**: budget, daily cap, allowed merchants, duplicate
   request checks.
3. **Intent check**: the prompt, the LLM call, and parsing and validating
   the JSON response, including the fail-safe behavior.

When I ask for help in these zones:
- Start with a hint or a question, not the answer.
- If I ask for the answer directly, give the smallest piece that unblocks
  me and explain why it works.
- When reviewing, point to the line and explain the problem; let me fix it.

## AI-allowed zones: you may write this code

- Sandbox catalog data and seed scripts
- The shopping agent (`agent/`)
- The AG Grid dashboard (frontend)
- Boilerplate: config, settings, database setup, Render deploy files
- Test scaffolding (I decide what each test checks)
- README and docs formatting
- The eval runner script (I write the eval cases and their labels)

Rules for AI-written code:
- Plan first. Show me the plan and wait for my approval before writing code.
- Keep changes small and explain what each file does.
- After writing, list anything I should read closely and why.
- Run the tests and show the results.

## How work flows

- TASKS.md is the task list and the memory between sessions. Work top to
  bottom, one task at a time. Tags: [AI] you may implement, [YOU]
  hand-written zone (hints only), [CHECK] stop and wait for me.
- After each finished task: tick the box, add one line to Progress notes,
  commit.
- If a test fails 3 times in a row, stop and explain what's going wrong.
- Dev team agents live in .claude/agents (architect, engineer, tester,
  reviewer). Hand [AI] tasks to the engineer, then the tester; use the
  reviewer before a PR.
- I merge. Agents never push to main or merge.

## Commands

- Lint and format: `ruff check .` and `ruff format .`
- Types: `mypy .`
- Tests: `pytest --cov`
- Pre-commit (runs on every commit): `pre-commit run --all-files`
- LLM evals and red-team for the intent check: `npx promptfoo@0.125.1 eval`
  (once the intent-check promptfoo config exists)
- Install dev tools: `pip install -r requirements-dev.txt`
- Run ruff, mypy and pytest before every commit. CI (to be set up) will
  run pytest, Semgrep and pip-audit on every PR; a red CI blocks merging.

## Always

- Use plan mode for any task bigger than a one-line fix. I make the design
  decisions; you fill in the approved plan.
- Never print, log, or commit secrets. Credentials live only in `.env`
  (gitignored) or Render environment variables. Never paste tokens or the
  client secret into code, docs, or terminal output you show me.
- Follow DESIGN.md. If something in the code should differ from the
  design, tell me and ask before changing either one.
- Explain any new concept (a library, a pattern, an API behavior) in one or
  two sentences when you first use it.
- Python is the main language. Prefer simple, readable code over clever code.
- Once deployed (M3), every merge to main auto-deploys on Render, so main
  must always pass CI. Use sandbox credentials only, never live PayPal keys.
- Never capture a payment without the human approval step. Every retry
  reuses the same PayPal-Request-Id.
