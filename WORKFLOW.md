# Dev Team Workflow

Four Claude Code subagents that take a TASKS.md task or a new feature to a reviewed PR.

```
TASKS.md task / new feature
      │
      ▼
 Architect ──► DESIGN.md edits + TASKS.md tasks ──► YOU approve (gate 1)
                                                     │
                                                     ▼
                                                 Engineer ──► branch + PR
                                                   ▲   ▲          │
                             failures (max 3 loops)│   │          ▼
                                                   │   │       Tester ──► docs/TEST_REPORT.md
                                                   │   │          │ PASS
                                     change requests   │          ▼
                                                       └────── Reviewer ──► docs/REVIEW.md
                                                                  │ APPROVE
                                                                  ▼
                                          YOU merge (gate 2) ──► auto-deploy once on Render (M3)
```

## Setup (once per repo)

1. `.claude/agents/` and `.claude/settings.json` are already in this repo.
2. `.env` is in `.gitignore`, and agents can't read or edit it (settings.json).
3. Open Claude Code in the repo and run `/agents` to confirm all four show up.

## Running it

For [AI] tasks already in TASKS.md, run them one fresh session at a time:

```
bash loop.sh
```

It stops at the first [YOU] or [CHECK] task, when nothing is left, after a failure, or after
MAX_RUNS (default 5).

For a new feature, paste this into Claude Code, replacing the feature:

```
Run our dev team workflow from WORKFLOW.md for this feature:
"<feature or change>"

1. Use the architect agent to propose DESIGN.md edits and TASKS.md tasks. Stop for my approval.
2. After I approve, use the engineer agent to build it on a new branch.
3. Use the tester agent to try to break it. If it FAILS, send the report to the engineer
   and repeat. Stop and ask me after 3 failed loops.
4. When the tester PASSES, use the reviewer agent. If it requests changes, send them to
   the engineer, then rerun the tester and reviewer.
5. When the reviewer APPROVES, stop and tell me the PR is ready for me to merge.
```

## Rules

- You own two decisions: approving the design and merging. Agents never push or merge (blocked in settings.json).
- Tester and reviewer judge the code, not the engineer's explanation.
- `.env` is unreadable to agents. Secrets live in environment variables only.
- Hand-written zones in CLAUDE.md (intent check prompt, JSON parsing and fail-safe; eval cases and labels): the engineer gives hints instead of code.

## First job

Jobs come from TASKS.md, top to bottom. [AI] tasks can run via `bash loop.sh`, which stops at
[YOU] and [CHECK] tasks. For a new feature, start with the architect.
