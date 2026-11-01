#!/usr/bin/env bash
# loop.sh: runs the next [AI] task in TASKS.md, one fresh Claude session per task.
# Stops at a [YOU] or [CHECK] task, when nothing is left, after a failure, or after MAX_RUNS.
# Usage (from the repo root, in Git Bash): bash loop.sh        (default 5 runs)
#                                          MAX_RUNS=10 bash loop.sh

MAX_RUNS="${MAX_RUNS:-5}"

if [ ! -f TASKS.md ]; then
  echo "TASKS.md not found in this folder. Run this from the repo root."
  exit 1
fi

rm -f STOP
runs=0
while [ ! -f STOP ] && [ "$runs" -lt "$MAX_RUNS" ]; do
  if ! grep -q '^- \[ \]' TASKS.md; then
    echo "ALL DONE: no unchecked tasks left" > STOP
    break
  fi
  runs=$((runs + 1))
  echo "=== Run $runs of $MAX_RUNS ==="
  claude -p "Read CLAUDE.md and TASKS.md. Find the first unchecked task.
If it is tagged [YOU] or [CHECK], write its name into a file called STOP and exit.
Otherwise: do that one task, run ruff, mypy and pytest until green, tick the box,
add a line to Progress notes, and commit. If tests fail 3 times, write the reason
into STOP and exit. Do only one task." \
    --permission-mode acceptEdits --max-turns 60 || { echo "claude exited with an error" > STOP; break; }
done

if [ -f STOP ]; then
  echo "Stopped: $(cat STOP)"
else
  echo "Stopped: reached MAX_RUNS ($MAX_RUNS). Check TASKS.md progress, then run again."
fi
