"""Tests for the progress dashboard's TASKS.md parsing and pace math."""

import subprocess
from datetime import timedelta

from scripts import progress
from scripts.progress import (
    DEADLINE,
    days_left,
    parse_notes,
    parse_tasks,
    section_progress,
    tasks_per_day,
    waiting_on_you,
)

SAMPLE = """# TASKS.md

Tags: [AI] Claude may implement. Keep the `- [ ]` format.

## Setup

- [x] [AI] Client. Evidence: tests pass
- [ ] [CHECK] Live run

## Rules

- [ ] [AI] Rules engine. Done when: each rule has a test
- [ ] [YOU] Eval cases
- plain bullet, not a task
- [ ] untagged box, not a task

## Progress notes

(Claude appends one line per finished task.)
- 2026-11-01: First note.
- 2026-11-02: Second note.
"""


def test_counts_done_and_total_per_section():
    tasks = parse_tasks(SAMPLE)
    assert len(tasks) == 4
    assert sum(t.done for t in tasks) == 1
    assert section_progress(tasks) == [("Setup", 1, 2), ("Rules", 0, 2)]


def test_tags_and_non_task_lines():
    tasks = parse_tasks(SAMPLE)
    assert [t.tag for t in tasks] == ["AI", "CHECK", "AI", "YOU"]
    assert tasks[0].title == "Client."
    assert tasks[0].details == "Evidence: tests pass"
    assert tasks[2].details == "Done when: each rule has a test"


def test_progress_notes_are_not_tasks():
    assert parse_notes(SAMPLE) == ["2026-11-01: First note.", "2026-11-02: Second note."]
    assert all(t.section != "Progress notes" for t in parse_tasks(SAMPLE))


def test_waiting_on_you_is_first_open_task_only_if_yours():
    tasks = parse_tasks(SAMPLE)
    waiting = waiting_on_you(tasks)
    assert waiting is not None and waiting.text == "Live run"

    # Once the [CHECK] is done, the next open task is [AI], so nothing waits on you,
    # even though a [YOU] task comes later.
    tasks[1].done = True
    assert waiting_on_you(tasks) is None


def test_pace_math_and_passed_deadline():
    assert days_left(DEADLINE - timedelta(days=4)) == 4
    assert tasks_per_day(10, 4) == 2.5
    assert days_left(DEADLINE + timedelta(hours=1)) == 0
    assert tasks_per_day(10, 0) is None


def test_git_and_gh_helpers_fail_safe(monkeypatch):
    def broken(*args, **kwargs):
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr(subprocess, "run", broken)
    assert progress.run("gh", "run", "list") is None
    assert progress.ci_status() is None
    assert progress.burndown_history() == []
