"""Progress dashboard: reads TASKS.md and git history, writes progress.html.

    python scripts/progress.py && start progress.html

Stdlib only, so it runs without the venv. progress.html is gitignored; rerun
the script whenever TASKS.md changes.
"""

import html
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_FILE = ROOT / "TASKS.md"
OUTPUT = ROOT / "progress.html"

# Submission deadline: Nov 12, 12:00 PM ET. DST ends Nov 1, so ET is EST
# (UTC-5) on that date. Hardcoded because zoneinfo needs the tzdata package
# on Windows, and this script is stdlib only.
DEADLINE = datetime(2026, 11, 12, 12, 0, tzinfo=timezone(timedelta(hours=-5)))

TAGS = ("AI", "YOU", "CHECK")
NOTES_SECTION = "Progress notes"
TASK_RE = re.compile(r"^- \[( |x)\] \[(AI|YOU|CHECK)\] (.+)$")
# Where a task's title ends and its details start.
DETAIL_RE = re.compile(r"\s(?=(Done when|Evidence):)")


@dataclass
class Task:
    section: str
    tag: str
    done: bool
    text: str

    @property
    def title(self) -> str:
        return DETAIL_RE.split(self.text, maxsplit=1)[0]

    @property
    def details(self) -> str:
        parts = DETAIL_RE.split(self.text, maxsplit=1)
        return parts[2] if len(parts) == 3 else ""


# ---------------------------------------------------------------- parsing


def parse_tasks(text: str) -> list[Task]:
    """Every `- [ ] [TAG] ...` line, in file order, with the `## ` section it sits under."""
    tasks = []
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        match = TASK_RE.match(line.strip())
        if match and section != NOTES_SECTION:
            box, tag, body = match.groups()
            tasks.append(Task(section, tag, box == "x", body.strip()))
    return tasks


def parse_notes(text: str) -> list[str]:
    """Bullet lines under `## Progress notes`, oldest first (as written)."""
    notes = []
    in_notes = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_notes = line[3:].strip() == NOTES_SECTION
        elif in_notes and line.startswith("- "):
            notes.append(line[2:].strip())
    return notes


def section_progress(tasks: list[Task]) -> list[tuple[str, int, int]]:
    """(section, done, total) per section, in TASKS.md order."""
    totals: dict[str, list[int]] = {}
    for task in tasks:
        counts = totals.setdefault(task.section, [0, 0])
        counts[0] += task.done
        counts[1] += 1
    return [(name, done, total) for name, (done, total) in totals.items()]


def waiting_on_you(tasks: list[Task]) -> Task | None:
    """The first open task, if it's yours. This is where the TASKS.md loop stops."""
    for task in tasks:
        if not task.done:
            return task if task.tag in ("YOU", "CHECK") else None
    return None


def days_left(now: datetime) -> float:
    return max(0.0, (DEADLINE - now).total_seconds() / 86400)


def tasks_per_day(open_count: int, days: float) -> float | None:
    """None once the deadline has passed, instead of dividing by zero."""
    return open_count / days if days > 0 else None


# ---------------------------------------------------------- git and gh


def run(*cmd: str) -> str | None:
    """stdout of a command, or None if it's missing, fails or times out."""
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout if result.returncode == 0 else None


def burndown_history() -> list[tuple[datetime, int]]:
    """(commit time, open tasks) for every commit that changed TASKS.md, oldest first."""
    log = run("git", "log", "--format=%H %cI", "--", "TASKS.md")
    if not log:
        return []
    points = []
    for line in reversed(log.splitlines()):
        sha, _, stamp = line.partition(" ")
        content = run("git", "show", f"{sha}:TASKS.md")
        if content is not None:
            open_count = sum(not t.done for t in parse_tasks(content))
            points.append((datetime.fromisoformat(stamp), open_count))
    return points


def ci_status() -> dict | None:
    """Latest GitHub Actions run for this branch via the gh CLI; git itself has no CI data."""
    branch = (run("git", "branch", "--show-current") or "").strip()
    if not branch:
        return None
    fields = "status,conclusion,displayTitle,createdAt,url,headBranch"
    out = run("gh", "run", "list", "--branch", branch, "--limit", "1", "--json", fields)
    if not out:
        return None
    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        return None
    return runs[0] if runs else {"headBranch": branch, "status": "none"}


# ------------------------------------------------------------- rendering

CSS = """
:root {
  --bg: #0f1216; --panel: #171b21; --line: #262c35; --text: #e6e9ee;
  --muted: #8b93a1; --faint: #5b6370; --accent: #4f9cf7; --good: #3fb37f;
  --warn: #e3a33b; --bad: #e5534b; --you: #f0b429;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
main { max-width: 1280px; margin: 0 auto; padding: 28px 24px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); margin: 0 0 14px; font-weight: 600; }
.sub { color: var(--muted); margin: 0 0 24px; font-size: 13px; }
.panel { background: var(--panel); border: 1px solid var(--line);
  border-radius: 12px; padding: 20px; }
.grid { display: grid; gap: 16px; margin-bottom: 16px; }
/* Let grid columns shrink below their longest word (file paths in tasks). */
.grid > * { min-width: 0; overflow-wrap: anywhere; }
.top { grid-template-columns: 220px 1fr 1fr; }
.mid { grid-template-columns: 1fr 1.4fr; }
.board { grid-template-columns: repeat(3, 1fr); }
.foot { grid-template-columns: 1.6fr 1fr; }
.ring { display: flex; align-items: center; justify-content: center; }
.big { font-size: 40px; font-weight: 700; line-height: 1.1;
  font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }
.bar-row { margin-bottom: 12px; }
.bar-label { display: flex; justify-content: space-between; font-size: 14px;
  margin-bottom: 4px; }
.bar { height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; }
.bar > span { display: block; height: 100%; background: var(--accent); }
.bar.full > span { background: var(--good); }
.col h2 span { color: var(--faint); font-weight: 400; }
.task { border: 1px solid var(--line); border-radius: 8px; padding: 9px 11px;
  margin-bottom: 8px; font-size: 13.5px; }
.task .sec { font-size: 11px; color: var(--faint); text-transform: uppercase;
  letter-spacing: .05em; }
.task .det { font-size: 12px; color: var(--muted); margin-top: 3px; }
.task.done { opacity: .38; }
.task.done .ttl { text-decoration: line-through; }
.task.waiting { border: 2px solid var(--you); background: rgba(240,180,41,.08); }
.badge { display: inline-block; font-size: 11px; font-weight: 700;
  color: #1a1300; background: var(--you); border-radius: 4px; padding: 1px 6px;
  margin-bottom: 4px; }
.notes li { margin-bottom: 8px; font-size: 14px; }
.notes b { color: var(--accent); font-weight: 600; }
.ci { font-size: 14px; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  margin-right: 6px; vertical-align: middle; }
a { color: var(--accent); }
svg text { fill: var(--muted); font-size: 11px; }
@media (max-width: 900px) {
  .top, .mid, .board, .foot { grid-template-columns: 1fr; }
  main { padding: 20px 16px 40px; }
}
"""

# Keeps the countdown live; the server-rendered text is the fallback.
COUNTDOWN_JS = """
(function () {
  var el = document.getElementById("countdown");
  var deadline = new Date(el.dataset.deadline);
  function tick() {
    var ms = deadline - new Date();
    if (ms <= 0) { el.textContent = "Deadline passed"; return; }
    var m = Math.floor(ms / 60000);
    el.textContent = Math.floor(m / 1440) + "d " + Math.floor(m % 1440 / 60) + "h "
      + (m % 60) + "m";
  }
  tick();
  setInterval(tick, 30000);
})();
"""


def esc(value: object) -> str:
    return html.escape(str(value))


def countdown_text(now: datetime) -> str:
    minutes = int((DEADLINE - now).total_seconds() // 60)
    if minutes <= 0:
        return "Deadline passed"
    return f"{minutes // 1440}d {minutes % 1440 // 60}h {minutes % 60}m"


def render_ring(done: int, total: int) -> str:
    pct = done / total if total else 0.0
    r = 70
    circumference = 2 * 3.14159265 * r
    return f"""
<svg width="180" height="180" viewBox="0 0 180 180" role="img"
     aria-label="{done} of {total} tasks done">
  <circle cx="90" cy="90" r="{r}" fill="none" stroke="var(--line)" stroke-width="14"/>
  <circle cx="90" cy="90" r="{r}" fill="none" stroke="var(--good)" stroke-width="14"
    stroke-linecap="round" stroke-dasharray="{circumference * pct:.1f} {circumference:.1f}"
    transform="rotate(-90 90 90)"/>
  <text x="90" y="88" text-anchor="middle" style="font-size:34px;font-weight:700;
    fill:var(--text)">{pct:.0%}</text>
  <text x="90" y="112" text-anchor="middle" style="font-size:13px">{done} / {total} tasks</text>
</svg>"""


def render_bars(sections: list[tuple[str, int, int]]) -> str:
    rows = []
    for name, done, total in sections:
        pct = 100 * done / total if total else 0
        full = " full" if done == total else ""
        rows.append(
            f'<div class="bar-row"><div class="bar-label"><span>{esc(name)}</span>'
            f'<span class="muted">{done}/{total}</span></div>'
            f'<div class="bar{full}"><span style="width:{pct:.0f}%"></span></div></div>'
        )
    return "".join(rows)


def render_burndown(history: list[tuple[datetime, int]], now: datetime) -> str:
    if not history:
        return '<p class="muted">No git history for TASKS.md yet.</p>'
    w, h, pad_l, pad_r, pad_t, pad_b = 640, 260, 36, 16, 12, 28
    start = history[0][0]
    span = max((DEADLINE - start).total_seconds(), 1)
    top = max(count for _, count in history) or 1

    def x(t: datetime) -> float:
        return pad_l + (w - pad_l - pad_r) * (t - start).total_seconds() / span

    def y(count: float) -> float:
        return pad_t + (h - pad_t - pad_b) * (1 - count / top)

    parts = []
    for tick in (0, top / 2, top):
        parts.append(
            f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(tick):.1f}" y2="{y(tick):.1f}" '
            f'stroke="var(--line)"/><text x="{pad_l - 6}" y="{y(tick) + 4:.1f}" '
            f'text-anchor="end">{tick:.0f}</text>'
        )
    # Ideal: straight from the first count to zero at the deadline.
    parts.append(
        f'<line x1="{x(start):.1f}" y1="{y(history[0][1]):.1f}" x2="{x(DEADLINE):.1f}" '
        f'y2="{y(0):.1f}" stroke="var(--faint)" stroke-dasharray="5 5" stroke-width="2"/>'
    )
    deadline_x = x(DEADLINE)
    parts.append(
        f'<line x1="{deadline_x:.1f}" x2="{deadline_x:.1f}" y1="{pad_t}" y2="{h - pad_b}" '
        f'stroke="var(--bad)" stroke-width="1.5"/>'
    )
    now_x = x(min(now, DEADLINE))
    parts.append(
        f'<line x1="{now_x:.1f}" x2="{now_x:.1f}" y1="{pad_t}" y2="{h - pad_b}" '
        f'stroke="var(--accent)" stroke-dasharray="2 4"/>'
    )
    points = " ".join(f"{x(t):.1f},{y(c):.1f}" for t, c in history)
    parts.append(
        f'<polyline points="{points}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>'
    )
    for t, c in history:
        parts.append(f'<circle cx="{x(t):.1f}" cy="{y(c):.1f}" r="3.5" fill="var(--accent)"/>')
    labels = [(start, "start", "start"), (now, "today", "middle"), (DEADLINE, "Nov 12", "end")]
    for t, label, anchor in labels:
        text = f"{label} {t.strftime('%b %d')}" if label == "start" else label
        parts.append(
            f'<text x="{x(min(t, DEADLINE)):.1f}" y="{h - 8}" text-anchor="{anchor}">'
            f"{esc(text)}</text>"
        )
    legend = (
        '<p class="muted" style="font-size:12px;margin:6px 0 0">'
        '<span style="color:var(--accent)">&#9679;</span> open tasks per TASKS.md commit '
        '&nbsp; <span style="color:var(--faint)">- - -</span> ideal pace '
        '&nbsp; <span style="color:var(--bad)">|</span> deadline</p>'
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Burndown">'
        f"{''.join(parts)}</svg>{legend}"
    )


def render_task(task: Task, waiting: Task | None) -> str:
    classes = "task"
    badge = ""
    if task.done:
        classes += " done"
    elif task is waiting:
        classes += " waiting"
        badge = '<div class="badge">WAITING ON YOU</div>'
    details = f'<div class="det">{esc(task.details)}</div>' if task.details else ""
    return (
        f'<div class="{classes}">{badge}<div class="sec">{esc(task.section)}</div>'
        f'<div class="ttl">{esc(task.title)}</div>{details}</div>'
    )


def render_board(tasks: list[Task], waiting: Task | None) -> str:
    columns = []
    for tag in TAGS:
        mine = [t for t in tasks if t.tag == tag]
        # Open tasks first (in file order), done ones greyed out underneath.
        ordered = [t for t in mine if not t.done] + [t for t in mine if t.done]
        open_count = sum(not t.done for t in mine)
        cards = "".join(render_task(t, waiting) for t in ordered)
        columns.append(
            f'<div class="panel col"><h2>[{tag}] <span>{open_count} open / {len(mine)}</span>'
            f"</h2>{cards}</div>"
        )
    return "".join(columns)


def render_notes(notes: list[str]) -> str:
    if not notes:
        return '<p class="muted">No progress notes yet.</p>'
    items = []
    for note in reversed(notes[-5:]):
        date, sep, rest = note.partition(": ")
        if sep:
            items.append(f"<li><b>{esc(date)}</b> {esc(rest)}</li>")
        else:
            items.append(f"<li>{esc(note)}</li>")
    return f'<ul class="notes" style="padding-left:18px;margin:0">{"".join(items)}</ul>'


def render_ci(ci: dict | None) -> str:
    if ci is None:
        reason = "gh CLI missing, not signed in, or offline"
        return f'<p class="ci muted">CI status unavailable ({reason}).</p>'
    branch = esc(ci.get("headBranch", ""))
    if ci.get("status") == "none":
        return f'<p class="ci muted">No CI runs for {branch} yet.</p>'
    conclusion = ci.get("conclusion") or ci.get("status") or "unknown"
    color = {"success": "var(--good)", "failure": "var(--bad)"}.get(conclusion, "var(--warn)")
    url = esc(ci.get("url", ""))
    return (
        f'<p class="ci"><span class="dot" style="background:{color}"></span>'
        f"<b>{esc(conclusion)}</b> on {branch}<br>"
        f'<a href="{url}">{esc(ci.get("displayTitle", "latest run"))}</a><br>'
        f'<span class="muted">{esc(ci.get("createdAt", ""))}</span></p>'
    )


def render(
    tasks: list[Task],
    notes: list[str],
    history: list[tuple[datetime, int]],
    ci: dict | None,
    now: datetime,
) -> str:
    done = sum(t.done for t in tasks)
    open_count = len(tasks) - done
    days = days_left(now)
    rate = tasks_per_day(open_count, days)
    pace = (
        f"{open_count} open &divide; {days:.1f} days"
        if rate is not None
        else f"{open_count} open, deadline passed"
    )
    waiting = waiting_on_you(tasks)
    waiting_line = (
        f"Waiting on you: <b>[{waiting.tag}] {esc(waiting.title)}</b>"
        if waiting
        else "Nothing waiting on you: the next open task is [AI]."
    )
    rate_text = f"{rate:.1f}" if rate is not None else "&mdash;"
    generated = now.astimezone(DEADLINE.tzinfo).strftime("%b %d, %I:%M %p ET")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Guard Progress</title>
<style>{CSS}</style>
</head>
<body>
<main>
  <h1>paypal-agent-guard progress</h1>
  <p class="sub">Generated {esc(generated)} from TASKS.md and git &middot; {waiting_line}</p>

  <div class="grid top">
    <div class="panel ring">{render_ring(done, len(tasks))}</div>
    <div class="panel">
      <h2>Submission deadline</h2>
      <div class="big" id="countdown"
           data-deadline="{DEADLINE.isoformat()}">{countdown_text(now)}</div>
      <p class="muted" style="margin:6px 0 0">Nov 12, 12:00 PM ET (Devpost by noon)</p>
    </div>
    <div class="panel">
      <h2>Pace needed</h2>
      <div class="big">{rate_text}
        <span style="font-size:18px" class="muted">tasks / day</span></div>
      <p class="muted" style="margin:6px 0 0">{pace}</p>
    </div>
  </div>

  <div class="grid mid">
    <div class="panel"><h2>Milestones</h2>{render_bars(section_progress(tasks))}</div>
    <div class="panel"><h2>Burndown</h2>{render_burndown(history, now)}</div>
  </div>

  <div class="grid board">{render_board(tasks, waiting)}</div>

  <div class="grid foot">
    <div class="panel"><h2>Latest progress notes</h2>{render_notes(notes)}</div>
    <div class="panel"><h2>Last CI run</h2>{render_ci(ci)}</div>
  </div>
</main>
<script>{COUNTDOWN_JS}</script>
</body>
</html>
"""


def main() -> int:
    text = TASKS_FILE.read_text(encoding="utf-8")
    tasks = parse_tasks(text)
    notes = parse_notes(text)
    now = datetime.now(timezone.utc)
    # Committed history plus a "now" point from the working copy.
    history = burndown_history()
    history.append((now, sum(not t.done for t in tasks)))
    page = render(tasks, notes, history, ci_status(), now)
    OUTPUT.write_text(page, encoding="utf-8")
    done = sum(t.done for t in tasks)
    print(f"Wrote {OUTPUT.name}: {done}/{len(tasks)} tasks done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
