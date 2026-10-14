"""Render a sprint Gantt chart with precedence links from a Jira CSV export.

The chart is project-agnostic; the only required project-specific value is the
title, passed with --title.  Optional per-project settings (which tasks to
drop, manual sprint reassignments, when work may start) come from a TOML
config -- see gantt.toml.example.

Bar placement follows the schedule defined in schedule.py:
    start    = max(project start, sprint start,
                   latest prerequisite finish + LAG_DAYS working days)
    duration = estimated_hours / HOURS_PER_DAY working days

Bar LENGTH therefore encodes duration, and each sprint is a fixed-length
column.  Every task gets its own row, so the left-hand label gutter never
collides.  Dependency arrows are drawn orthogonally: horizontal and vertical
segments only.

Usage:
    python3 gantt.py --title "My Epic - sprint plan" -o gantt.png
    python3 gantt.py --title "..." --assignment rebalance --people 3 -o capped.png
"""
import os
import sys
import argparse
import datetime as dt

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

import schedule as S

BASE = os.path.dirname(os.path.abspath(__file__))

# Every bar uses the same fill.  Which sprint a task belongs to is already
# carried by the column it sits in and by its row label, so colouring by
# sprint added a third encoding without adding information.
BAR_COLOUR = "#c3d7ee"
FONT = "DejaVu Sans"

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--title", required=True,
                help="chart title (project specific, so required)")
ap.add_argument("--csv", default=None,
                help="Jira export to read (default: newest *.csv beside this script)")
ap.add_argument("--config", default=os.path.join(BASE, "gantt.toml"),
                help="optional TOML config (default: gantt.toml beside this script)")
ap.add_argument("--assignment", choices=["csv", "rebalance"], default="csv",
                help="csv = keep the sprint each task is assigned in the export "
                     "(default); rebalance = pull tasks as early as their "
                     "dependencies allow")
ap.add_argument("--version", default=None, help="version label shown in the title")
ap.add_argument("--edges", choices=["all", "latest"], default="all",
                help="all = every link in the export (default); latest = per "
                     "task, only its latest-finishing predecessor, i.e. the "
                     "link that actually gates its start")
ap.add_argument("--people", type=int, default=None, metavar="N",
                help="limit to N developers at hours_per_day x days_per_week per "
                     "week each; weekends are non-working (default: unlimited)")
ap.add_argument("-o", "--out", default=os.path.join(BASE, "gantt.png"))
args = ap.parse_args()

CSV_PATH = S.find_csv(BASE, ["prog", args.csv] if args.csv else None)
CONFIG   = S.configure(S.load_config(args.config))

sprints, tasks, blocks, cols = S.load(CSV_PATH)

if args.assignment == "rebalance":
    assign, sched, converged = S.rebalance(sprints, tasks, blocks, cols)
    if not converged:
        print("warning: rebalance did not converge", file=sys.stderr)
else:
    assign = {k: tasks[k]["sprint"] for k in tasks}
    sched  = S.schedule(sprints, tasks, blocks, assign)

if args.people:
    sched = S.schedule_capped(sprints, tasks, blocks, assign, args.people)

win   = S.sprint_windows(sprints, cols)
loads = S.sprint_loads(sched, tasks, cols, win)
peaks = S.sprint_peaks(sched, tasks, cols, win)

# --------------------------------------------------------------- layout
FIG_W, FIG_H = 15.0, 11.0
BAR_H        = 0.30
ROW_PITCH    = 0.42
GUTTER_FRAC  = 0.30
X0F, X1F     = 0.012, 0.988
HDR_NAME, HDR_DATE, HDR_TOT = 0.86, 0.56, 0.36
HDR_TOP      = 1.90

# one row per task, ordered by (start, sprint)
seq    = sorted(tasks, key=lambda k: (sched[k][0], tasks[k]["sprint"], k))
y_top  = {k: -i * ROW_PITCH for i, k in enumerate(seq)}
lowest = min(y_top.values()) - BAR_H

# The axis opens on the first sprint's start date, not on the first task's.
# With a project_start that falls inside the opening sprint (e.g. the team
# beginning mid-sprint), the earliest task still sits a few days in, and
# starting the axis there would clip the opening sprint column.
t_start = min(a for a, _ in win.values())
t_end   = max(max(e for _, e in win.values()),
              max(sched[k][1] for k in tasks))
t0, t1  = mdates.date2num(t_start), mdates.date2num(t_end)
gutter  = (t1 - t0) * GUTTER_FRAC / (1 - GUTTER_FRAC)
XL, XR  = t0 - gutter, t1

fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=150)
ax = fig.add_axes([X0F, 0.075, X1F - X0F, 0.87])
ax.axis("off")
ax.set_xlim(XL, XR)
ax.set_ylim(lowest - 0.55, HDR_TOP)

plot_days = XR - XL
def dx(inches):                      # inches -> x data units
    return inches * plot_days / (FIG_W * (X1F - X0F))

# ---------------------------------------------- sprint columns + headers
for idx, c in enumerate(cols):
    a, b = win[c]
    x0, x1 = mdates.date2num(a), mdates.date2num(b)
    if idx % 2 == 0:
        ax.add_patch(plt.Rectangle((x0, lowest - 0.55), x1 - x0,
                                   HDR_TOP - lowest + 0.55,
                                   color="#f4f4f4", zorder=0))
    ax.plot([x1, x1], [lowest - 0.55, HDR_NAME + 0.16],
            color="#d8d8d8", lw=0.8, zorder=1)
    cx = (x0 + x1) / 2
    ax.text(cx, HDR_NAME, c, ha="center", va="top", fontsize=13,
            fontweight="bold", fontfamily=FONT, color="#222", zorder=2)
    ax.text(cx, HDR_DATE, f"{a:%b %d} → {b:%b %d}", ha="center", va="top",
            fontsize=9.5, fontfamily=FONT, color="#666", zorder=2)
    # hours spent inside the sprint, then the busiest working day.  The peak
    # is not an average: a task clipping part of a day needs part of a person,
    # which is why it can read 2.6 rather than a whole number.
    ax.text(cx, HDR_TOT, f"{loads[c]:.0f}h (peak: {peaks[c]:.1f} dev)",
            ha="center", va="top", fontsize=8.5, fontfamily=FONT,
            color="#999", zorder=2)
ax.plot([t1, t1], [lowest - 0.55, HDR_NAME + 0.16], color="#d8d8d8", lw=0.8, zorder=1)
ax.plot([t0, t0], [lowest - 0.55, HDR_NAME + 0.16], color="#b0b0b0", lw=1.1, zorder=1)
ax.text(t0 - dx(0.12), HDR_DATE, "task", ha="right", va="top", fontsize=11,
        fontweight="bold", fontfamily=FONT, color="#444", zorder=2)

# ---------------------------------------------------- bars + gutter labels
for k, t in tasks.items():
    st, en = sched[k]
    yt = y_top[k]
    xd0, xd1 = mdates.date2num(st), mdates.date2num(en)
    ax.add_patch(FancyBboxPatch(
        (xd0, yt - BAR_H), xd1 - xd0, BAR_H,
        boxstyle="round,pad=0,rounding_size=0.12",
        linewidth=0.9, edgecolor="#5a6a7a",
        facecolor=BAR_COLOUR, zorder=3))
    ax.text(t0 - dx(0.12), yt - BAR_H + 0.165, k, ha="right", va="center",
            fontsize=9, fontweight="bold", fontfamily=FONT, color="#1a1a1a", zorder=4)
    ax.text(t0 - dx(0.12), yt - BAR_H + 0.055, S.truncate(t["summary"], 52),
            ha="right", va="center", fontsize=6.4, fontfamily=FONT,
            color="#666", zorder=4)
    ax.text(xd1 + dx(0.05), yt - BAR_H / 2, f"{t['est']:g}h", ha="left",
            va="center", fontsize=8, fontfamily=FONT, color="#333", zorder=4)

# ---------------------------------------------------------------- arrows
draw_blocks = S.select_edges(sched, blocks, args.edges)

# index of each edge among those sharing a source, used to fan the vertical
# segments out so they do not all stack on one x
_fan, _seen = {}, {}
for _a, _b in draw_blocks:
    _fan[(_a, _b)] = _seen.get(_a, 0)
    _seen[_a] = _seen.get(_a, 0) + 1

for a_k, b_k in draw_blocks:
    _, ea = sched[a_k]
    sb, _  = sched[b_k]
    same   = assign[a_k] == assign[b_k]
    x1, y1 = mdates.date2num(ea), y_top[a_k] - BAR_H / 2
    x2, y2 = mdates.date2num(sb), y_top[b_k] - BAR_H / 2
    colour, aw = ("#3f74b0" if same else "#93aecb"), 0.85

    # Orthogonal routing: horizontal and vertical segments only, with a
    # staircase when a plain elbow cannot reach (target starts at or before
    # the predecessor finishes, so the link must go around).
    if abs(y1 - y2) < 1e-9:
        pts = [(x1, y1), (x2, y2)]
    elif x2 > x1:
        # drop just after the predecessor, fanning each edge out
        xm = x1 + dx(0.10 + 0.075 * _fan[(a_k, b_k)])
        xm = min(xm, x2 - dx(0.03))
        pts = [(x1, y1), (xm, y1), (xm, y2), (x2, y2)]
    else:
        xm = max(x1, x2) + dx(0.18)              # staircase: out, over, back
        pts = [(x1, y1), (xm, y1), (xm, y2), (x2, y2)]
    ax.plot([p[0] for p in pts[:-1]], [p[1] for p in pts[:-1]],
            color=colour, lw=aw, zorder=2, solid_capstyle="butt",
            solid_joinstyle="miter")
    ax.add_patch(FancyArrowPatch(
        pts[-2], pts[-1], arrowstyle="-|>", mutation_scale=7.5,
        linewidth=aw, color=colour, zorder=2,
        shrinkA=0, shrinkB=1.5))

# -------------------------------------------------------------- time axis
# The gutter is part of the main axes, so the x-axis must span only the
# timeline portion (the right 1-GUTTER_FRAC of the axes width); otherwise the
# date ticks would extend across the label gutter and lie.
AX_W = X1F - X0F
axis_ax = fig.add_axes([X0F + AX_W * GUTTER_FRAC, 0.030,
                        AX_W * (1 - GUTTER_FRAC), 0.030])
axis_ax.set_xlim(t0, t1)
axis_ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
axis_ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
axis_ax.set_yticks([])
for s in ("top", "right", "left", "bottom"):
    axis_ax.spines[s].set_visible(False)
axis_ax.tick_params(length=2, color="#c4c4c4")
for lab in axis_ax.get_xticklabels():
    lab.set_fontsize(7); lab.set_fontfamily(FONT); lab.set_color("#777")

# ----------------------------------------------------------------- title
title = args.title
if args.version:
    title += f"  (v{args.version})"
if args.people:
    title += (f"  —  {args.people} developer{'' if args.people == 1 else 's'} "
              f"({S.hours_per_week():g}h/week each)")

finish = max(sched[k][1] for k in tasks)
fig.text(X0F, 0.982, title, fontsize=16, fontweight="bold",
         fontfamily=FONT, color="#141414", va="top")
fig.text(X0F, 0.958,
         f"Mon–Fri working week  ·  start = max(project start, sprint start, "
         f"latest prerequisite + {S.LAG_DAYS} working day)  ·  "
         f"duration = hours ÷ {S.HOURS_PER_DAY:g} working days",
         fontsize=8.5, fontfamily=FONT, color="#6a6a6a", va="top")
fig.text(X0F, 0.938,
         f"finishes {finish:%b %d}"
         + (f"  ·  capped at {args.people} × {S.hours_per_week():g}h/week"
            if args.people else "")
         + "  ·  " + {"all": f"showing all {len(blocks)} links",
                      "latest": f"showing {len(draw_blocks)}/{len(blocks)} links "
                                f"(latest predecessor per task)"}[args.edges],
         fontsize=8.5, fontfamily=FONT, color="#6a6a6a", va="top")

fig.savefig(args.out, dpi=150, facecolor="white")

import collections as _c
_days = _c.defaultdict(int)
for _k in tasks:
    for _d in S.working_days_touched(*sched[_k]):
        _days[_d] += 1
_peak = max(_days.values()) if _days else 0
n_moved = sum(1 for k in tasks if tasks[k]["sprint"] != assign[k])
print(f"wrote {args.out}  ({len(tasks)} tasks, {len(draw_blocks)}/{len(blocks)} links, "
      f"finish {finish:%b %d}, {n_moved} re-sprinted, "
      f"peak {_peak} concurrent = {_peak * S.hours_per_week():g}h/week"
      + (f", limit {args.people} dev" if args.people else "") + ")")
