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
