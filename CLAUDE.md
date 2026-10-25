# CLAUDE.md

This is paypal-agent-guard, a guardrail layer for AI shopping agents
(PayPal AI Hackathon). Read DESIGN.md before planning any work; it is the
source of truth for architecture, permissions, risks and the eval plan.

I am a student learning AI engineering. My goal is to understand every
part of this project well enough to explain it in an interview without AI.
Follow the rules below on every task.

## AI-allowed zones: you may write this code

- FastAPI endpoints: route functions, request and response models
- Rules engine: budget, daily cap, allowed merchants, duplicate checks
- Intent check: the prompt, the LLM call, and JSON parsing and validation
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
