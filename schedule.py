"""Loading and scheduling logic for Jira-derived sprint Gantt charts.

This module is project-agnostic.  Anything specific to one project -- which
tasks to drop, manual sprint reassignments, when the work may start -- comes
from an optional TOML config file (see CONFIG_EXAMPLE).  Sensible defaults
apply when no config is supplied.

Calendar model
--------------
Work happens Monday-Friday only.  A task's duration is its estimated hours
divided by HOURS_PER_DAY, measured in WORKING days, so a task spanning a
weekend is paused over it.  The gap after a prerequisite is likewise measured
in WORKING days.

    start    = max(project start, sprint start,
                   latest prerequisite finish + LAG_DAYS working days)
    duration = estimated_hours / HOURS_PER_DAY   (working days)

Capacity
--------
A running task occupies one person.  A five-day week at 7h/day is 35h, so
`people` people is 35h x people per week -- which is exactly `people`
concurrent tasks.  The capped scheduler therefore limits concurrency per
WORKING day; weekends never consume capacity.

Expected input
--------------
A Jira CSV export containing at least these columns (the export produces
duplicate "Sprint" and "Outward issue link (Blocks)" headers for multi-value
fields, which this module handles):

    Issue key, Summary, Sprint, Original Estimate,
    Outward issue link (Blocks) ...
"""
import csv
import re
import glob
import os
import sys
import collections
import datetime as dt

try:
    import tomllib
except ModuleNotFoundError:                     # Python < 3.11
    tomllib = None

CONFIG_EXAMPLE = "gantt.toml.example"

# --------------------------------------------------------------- policy
# Defaults for the scheduling model.  Override any of these in the config
# file; they describe how the team works rather than a particular project.
HOURS_PER_DAY = 7.0
DAYS_PER_WEEK = 5
LAG_DAYS      = 1
SPRINT_DAYS   = 14

# Populated by configure(); all optional.
PROJECT_START = None                 # date, or None for "no floor"
SKIP          = set()                # issue keys to omit entirely
OVERRIDE      = {}                   # issue key -> sprint id


def hours_per_week():
    return HOURS_PER_DAY * DAYS_PER_WEEK


def load_config(path):
    """Read an optional TOML config.  Returns {} when the file is absent.

    Recognised keys (all optional):
        project_start  = "YYYY-MM-DD"   nothing starts before this date
        skip           = ["KEY", ...]   tasks to leave out of the chart
        lag_days       = 1              working days between dependent tasks
        hours_per_day  = 7              working hours in a day
        days_per_week  = 5
        sprint_days    = 14             calendar length of a sprint window
        [sprint_override]
        KEY = "SPRINT-ID"               force a task into another sprint
    """
    if not path or not os.path.exists(path):
        return {}
    if tomllib is None:
        raise SystemExit("Python 3.11+ is required to read a TOML config")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def configure(cfg):
    """Apply a config dict to this module's policy defaults."""
    global HOURS_PER_DAY, DAYS_PER_WEEK, LAG_DAYS, SPRINT_DAYS
    global PROJECT_START, SKIP, OVERRIDE

    HOURS_PER_DAY = float(cfg.get("hours_per_day", HOURS_PER_DAY))
    DAYS_PER_WEEK = int(cfg.get("days_per_week", DAYS_PER_WEEK))
    LAG_DAYS      = int(cfg.get("lag_days", LAG_DAYS))
    SPRINT_DAYS   = int(cfg.get("sprint_days", SPRINT_DAYS))
    SKIP          = set(cfg.get("skip", []))
    OVERRIDE      = dict(cfg.get("sprint_override", {}))

    raw = cfg.get("project_start")
    if raw:
        PROJECT_START = (raw if isinstance(raw, dt.date)
                         else dt.date.fromisoformat(str(raw)))
    else:
        PROJECT_START = None
    return cfg


# ------------------------------------------------------------------ calendar
def is_working_day(d):
    return d.weekday() < DAYS_PER_WEEK


def roll_forward(when):
    """If `when` falls on a weekend, move to the next Monday, keeping the time."""
    while not is_working_day(when):
        when += dt.timedelta(days=1)
    return when


def add_working_days(start, n):
    """Advance `n` working days, skipping weekends.

    Fractional n leaves the result part-way through a working day.
    """
    d = roll_forward(start)
    left = float(n)
    while left > 1e-9:
        step = min(1.0, left)
        d = roll_forward(d + dt.timedelta(days=step))
        left -= step
    return d


def working_days_touched(start, end):
    """Working-day dates touched by the interval [start, end)."""
    out, d = [], start.date()
    while dt.datetime.combine(d, dt.time.min) < end:
        if is_working_day(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def project_floor():
    """Earliest moment any task may start (datetime.min when unconstrained)."""
    if PROJECT_START is None:
        return dt.datetime.min
    return roll_forward(dt.datetime.combine(PROJECT_START, dt.time.min))


# ---------------------------------------------------------------------- load
def find_csv(base, argv=None):
    """Newest Jira export in `base`, unless one was passed on the CLI.

    Ranking is by the timestamp embedded in the filename, NOT by raw string
    sort: a separator between a prefix and the date ("Export 2026-..." with a
    space vs "Export-2026-..." with a hyphen) would otherwise outrank the date
    itself, because space sorts before hyphen.  Falls back to file mtime when
    a name carries no recognisable timestamp.
    """
    if argv and len(argv) > 1:
        return argv[1]
    csvs = glob.glob(os.path.join(base, "*.csv"))
    if not csvs:
        raise SystemExit(f"no CSV export found in {base}; pass one as an argument")

    stamp = re.compile(r"(\d{4}-\d{2}-\d{2})[T_ ](\d{2})[_:](\d{2})[_:](\d{2})")

    def rank(path):
        m = stamp.search(os.path.basename(path))
        if m:                                   # (date, time) as sortable string
            return (m.group(1) + m.group(2) + m.group(3) + m.group(4),
                    os.path.getmtime(path))
        return ("", os.path.getmtime(path))

    return max(csvs, key=rank)


def load(csv_path):
    """Return (sprints, tasks, blocks, cols).

    sprints : {'SPRINT-ID': date, ...}
    tasks   : {key: {summary, est, sprint}}
    blocks  : [(blocker, blocked), ...] -- sorted, deduped, both ends present
    cols    : sprint ids holding tasks, in date order
    """
    raw = list(csv.reader(open(csv_path, newline="", encoding="utf-8")))
    head, body = raw[0], raw[1:]

    def col(name, required=True):
        try:
            return head.index(name)
        except ValueError:
            if required:
                raise SystemExit(f"{csv_path}: missing required column {name!r}")
            return None

    # Columns are located by NAME, not position, so the export can be
    # reordered or have columns added without breaking.
    c_key  = col("Issue key")
    c_sum  = col("Summary")
    c_est  = col("Original Estimate")
    sp     = [i for i, c in enumerate(head) if c == "Sprint"]
    bl     = [i for i, c in enumerate(head)
              if c.startswith("Outward issue link (Blocks)")]
    if not sp:
        raise SystemExit(f"{csv_path}: missing required column 'Sprint'")

    sprints, tasks = {}, {}
    for r in body:
        sl = []
        for i in sp:
            v = r[i].strip()
            if v:
                m = re.match(r"(\S+)\s+\((\d{4}-\d{2}-\d{2})\)", v)
                if not m:
                    raise SystemExit(
                        f"cannot parse sprint {v!r}; expected 'NAME (YYYY-MM-DD)'")
                sprints[m.group(1)] = dt.date.fromisoformat(m.group(2))
                sl.append(m.group(1))
        if not sl:
            continue                       # task not assigned to any sprint
        tasks[r[c_key]] = dict(summary=r[c_sum],
                               est=int(r[c_est] or 0) / 3600.0,
                               sprints=sl)

    order = sorted(sprints, key=lambda s: sprints[s])
    tasks = {k: v for k, v in tasks.items() if k not in SKIP}
    for k in tasks:
        tasks[k]["sprint"] = OVERRIDE.get(k) or min(
            tasks[k]["sprints"], key=lambda x: order.index(x))
    cols = sorted({t["sprint"] for t in tasks.values()}, key=lambda c: order.index(c))

    # Only links whose BOTH endpoints are in this export can be scheduled.  A
    # blocker outside the exported scope (or a task dropped via skip) would
    # otherwise raise a confusing KeyError deep in the scheduler, so drop those
    # links and say so loudly rather than silently producing an early start.
    raw_blocks = {(r[c_key], r[i].strip()) for r in body for i in bl if r[i].strip()}
    unknown = sorted({x for e in raw_blocks for x in e if x not in tasks})
    if unknown:
        print(f"warning: {len(unknown)} link endpoint(s) not in this export; "
              f"those links are DROPPED (dependents may start early): "
              f"{', '.join(unknown)}", file=sys.stderr)
    blocks = sorted(e for e in raw_blocks if e[0] in tasks and e[1] in tasks)
    return sprints, tasks, blocks, cols


# ---------------------------------------------------------------- scheduling
def _graph(blocks):
    preds, succ = collections.defaultdict(list), collections.defaultdict(list)
    for a, b in blocks:                    # a blocks b  =>  a precedes b
        preds[b].append(a)
        succ[a].append(b)
    return preds, succ


def _topo(tasks, preds, succ, priority=None):
    """Kahn's algorithm; among ready tasks prefers the highest `priority`."""
    indeg = {k: len(preds[k]) for k in tasks}
    ready = [k for k in tasks if indeg[k] == 0]
    out = []
    while ready:
        if priority:
            ready.sort(key=lambda k: (-priority[k], k))
        k = ready.pop(0)
        out.append(k)
        for m in succ[k]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    if len(out) != len(tasks):
        raise SystemExit("cycle detected in blocking links; cannot schedule")
    return out


def _start_from(sprint_start, sched, preds, k):
    floor = project_floor()
    if preds[k]:
        earliest = add_working_days(max(sched[p][1] for p in preds[k]), LAG_DAYS)
        return max(floor, sprint_start, earliest)
    return max(floor, sprint_start)


def schedule(sprints, tasks, blocks, assign=None):
    """Uncapped schedule.  Returns {key: (start_dt, end_dt)}."""
    if assign is None:
        assign = {k: tasks[k]["sprint"] for k in tasks}
    preds, succ = _graph(blocks)
    sched = {}
    for k in _topo(tasks, preds, succ):
        sprint_start = roll_forward(
            dt.datetime.combine(sprints[assign[k]], dt.time.min))
        start = _start_from(sprint_start, sched, preds, k)
        sched[k] = (start, add_working_days(start, tasks[k]["est"] / HOURS_PER_DAY))
    return sched


def schedule_capped(sprints, tasks, blocks, assign, people):
    """Schedule with a headcount limit.

    `people` people at days_per_week x hours_per_day per week means at most
    `people` tasks may be in flight on any working day; weekends never consume
    capacity.
    """
    if not people:
        return schedule(sprints, tasks, blocks, assign)
    preds, succ = _graph(blocks)
    max_conc = int(people)

    # priority: longest remaining chain in working days, so gating work first
    memo = {}
    def chain(k):
        if k not in memo:
            d = tasks[k]["est"] / HOURS_PER_DAY
            memo[k] = d + LAG_DAYS + max((chain(m) for m in succ[k]), default=0.0)
        return memo[k]
    prio = {k: chain(k) for k in tasks}

    placed, sched = [], {}
    for k in _topo(tasks, preds, succ, prio):
        sprint_start = roll_forward(
            dt.datetime.combine(sprints[assign[k]], dt.time.min))
        earliest = _start_from(sprint_start, sched, preds, k)
        # try the ready moment, then every moment capacity frees up
        cands = sorted({earliest} | {p_end for _, p_end, _ in placed
                                     if p_end > earliest})
        start = None
        for t in cands:
            end = add_working_days(t, tasks[k]["est"] / HOURS_PER_DAY)
            mine = set(working_days_touched(t, end))
            # count how many already-placed tasks occupy EACH working day of
            # this span; adding this task must keep every day within the cap
            if all(sum(1 for _, _, od in placed if d in od) < max_conc
                   for d in mine):
                start = t
                break
        if start is None:                      # no slot found: queue after all
            start = cands[-1]
        end = add_working_days(start, tasks[k]["est"] / HOURS_PER_DAY)
        sched[k] = (start, end)
        placed.append((start, end, set(working_days_touched(start, end))))
    return sched


def sprint_for(sprints, sprint_ids, when):
    """Sprint whose window contains `when` (clamped to the range)."""
    chosen = sprint_ids[0]
    for s in sprint_ids:
        if dt.datetime.combine(sprints[s], dt.time.min) <= when:
            chosen = s
        else:
            break
    return chosen


def rebalance(sprints, tasks, blocks, sprint_ids, max_iter=200):
    """Pull every task as early as dependencies allow, then label each with
    the sprint it actually starts in.  Iterated to a fixed point.

    Returns (assign, schedule, converged).
    """
    assign = {k: sprint_ids[0] for k in tasks}
    for _ in range(max_iter):
        sched = schedule(sprints, tasks, blocks, assign)
        new = {k: sprint_for(sprints, sprint_ids, sched[k][0]) for k in tasks}
        if new == assign:
            return assign, sched, True
        assign = new
    return assign, schedule(sprints, tasks, blocks, assign), False


# ------------------------------------------------------------------ selection
def sprint_peaks(sched, tasks, sprint_ids, win):
    """Peak headcount needed inside each sprint window.

    A task occupying a whole working day consumes one person, so this is the
    largest number of simultaneously-active people on any working day of the
    sprint.  A task that only clips a day -- starting or finishing part way
    through it -- contributes a fraction of a person, which is why values like
    2.4 occur rather than whole numbers.  On a schedule capped at N people,
    no sprint can exceed N.
    """
    peaks = {c: 0.0 for c in sprint_ids}
    spans = list(sched.values())
    day = dt.timedelta(days=1)
    for c in sprint_ids:
        a, b = win[c]
        d = a.date()
        while dt.datetime.combine(d, dt.time.min) < b:
            if is_working_day(d):
                d0 = dt.datetime.combine(d, dt.time.min)
                d1 = d0 + day
                busy = sum((min(e, d1) - max(s, d0)).total_seconds() / 86400.0
                           for s, e in spans if min(e, d1) > max(s, d0))
                peaks[c] = max(peaks[c], busy)
            d += day
    return peaks


def select_edges(sched, blocks, mode="all"):
    """Choose which blocking links to draw.

    mode
      "all"      every link in the export
      "latest"   for each task, only the predecessor that finishes LAST --
                 the link that actually gates its start
    """
    if mode == "all":
        return list(blocks)

    latest = {}
    for a, b in blocks:
        cur = latest.get(b)
        if cur is None or sched[a][1] > sched[cur][1]:
            latest[b] = a
    return [(a, b) for a, b in blocks if latest.get(b) == a]


def sprint_windows(sprints, cols, days=None):
    """{sprint: (start_dt, end_dt)} with a fixed sprint length."""
    days = SPRINT_DAYS if days is None else days
    return {c: (dt.datetime.combine(sprints[c], dt.time.min),
                dt.datetime.combine(sprints[c], dt.time.min)
                + dt.timedelta(days=days))
            for c in cols}


def sprint_loads(sched, tasks, sprint_ids, win):
    """Hours actually spent inside each sprint window.

    Each task's estimate is split across the sprint windows it occupies,
    weighted by the number of working days it spends in each.  A task running
    across a sprint boundary therefore bills part of its estimate to each
    sprint, and the per-sprint totals sum to the total effort.
    """
    loads = {c: 0.0 for c in sprint_ids}
    for k, t in tasks.items():
        days = working_days_touched(*sched[k])
        if not days:
            continue
        for c in sprint_ids:
            a, b = win[c]
            n = sum(1 for d in days if a.date() <= d < b.date())
            if n:
                loads[c] += t["est"] * n / len(days)
    return loads


def truncate(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def wrap(s, n):
    lines, cur = [], ""
    for w in s.split():
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines
