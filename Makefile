# Render sprint Gantt charts from a Jira CSV export.
#
#   make                 all six charts
#   make planned         sprints exactly as planned in the export
#   make capped          rebalanced, limited to $(PEOPLE) developers
#   make unlimited       rebalanced, no headcount limit
#   make planned-simple  as above, but only the link that gates each task
#   make demo            render the committed synthetic example (no export needed)
#   make setup           create/refresh the virtualenv from uv.lock
#   make clean           remove generated charts, venv and caches
#
# The three charts isolate two independent choices:
#   planned   vs unlimited   -> the effect of REBALANCING the sprints
#   unlimited vs capped      -> the effect of the HEADCOUNT limit
#
# The "-simple" variants draw only each task's latest-finishing predecessor
# (--edges latest) instead of every blocking link, which reads as a single
# chain when the full dependency web is too busy.
#
# The title is required by gantt.py and is read from gantt.toml (gitignored),
# so override it with:  make TITLE="My Epic - plan"  or edit gantt.toml.
# Override the headcount with:  make capped PEOPLE=5
#
# Environment
#   Dependencies are pinned in uv.lock and installed into .venv:
#     uv sync --frozen      (or: make setup)
#   Recipes run through `uv run --frozen`, so they use the locked environment
#   and fail loudly if uv.lock is out of step with pyproject.toml rather than
#   silently re-resolving.
#
# Inputs
#   The Jira export is auto-detected by gantt.py (newest *.csv beside it).
#   CSVs and gantt.toml are gitignored -- see .gitignore.
#
# Scheduling model (schedule.py)
#   Mon-Fri working week; weekends are non-working.  A task's duration is
#   estimated hours / hours_per_day measured in WORKING days, so it pauses
#   over weekends.
#     start    = max(project start, sprint start, latest prerequisite
#                    + lag_days working days)
#     duration = hours / hours_per_day working days
#   Defaults live in gantt.toml.example and can be overridden in gantt.toml.

PY     := uv run --frozen python
CFG    ?= gantt.toml
PEOPLE ?= 3

# Read the required title from the config.  Plain python3 is fine: tomllib is
# stdlib, so this needs no virtualenv and is fast enough for a $(shell).
TITLE ?= $(shell python3 -c "import tomllib,pathlib as p; f=p.Path('$(CFG)'); print(tomllib.loads(f.read_text()).get('title','') if f.exists() else '')" 2>/dev/null)

.PHONY: all planned planned-simple capped capped-simple unlimited unlimited-simple \
        demo setup clean force title

# `force` is declared before `all`, so pin the default goal explicitly
.DEFAULT_GOAL := all

# Fails only the targets that actually need a title.  A plain $(error ...)
# would fire at parse time and block `make demo`, `make setup` and `make clean`
# too, which is exactly what a fresh clone needs to run.
title:
	@if [ -z "$(strip $(TITLE))" ]; then \
		echo "error: no chart title." >&2; \
		echo "  set it in $(CFG) (copy gantt.toml.example)," >&2; \
		echo "  or run: make TITLE=\"My Epic - sprint plan\"" >&2; \
		exit 1; \
	fi

# phony prerequisite: guarantees the recipes below always run, which removes
# any need to track the export as a dependency (its name contains spaces,
# which make cannot use as a prerequisite).
force:

all: gantt-planned.png gantt-planned-simple.png \
     gantt-capped.png gantt-capped-simple.png \
     gantt-unlimited.png gantt-unlimited-simple.png

planned: gantt-planned.png
planned-simple: gantt-planned-simple.png
capped: gantt-capped.png
capped-simple: gantt-capped-simple.png
unlimited: gantt-unlimited.png
unlimited-simple: gantt-unlimited-simple.png

# create/refresh the virtualenv from the lockfile
setup:
	uv sync --frozen

# Render the committed synthetic example.  Useful as a smoke test: it needs
# no Jira export, so it is the one chart that works from a fresh clone.
# This is the image embedded in README.md.
demo:
	$(PY) gantt.py --csv docs/example.csv --config /nonexistent \
	               --title "Example release — sprint plan" \
	               --assignment rebalance --people 3 -o docs/example.png

# --- planned: the sprints the export assigns, no headcount limit -----------------
gantt-planned.png: gantt.py schedule.py title force
	$(PY) gantt.py --title "$(TITLE)" --assignment csv --edges all -o $@

gantt-planned-simple.png: gantt.py schedule.py title force
	$(PY) gantt.py --title "$(TITLE)" --assignment csv --edges latest -o $@

# --- capped: sprints rebalanced, limited to $(PEOPLE) developers ------------------
gantt-capped.png: gantt.py schedule.py title force
	$(PY) gantt.py --title "$(TITLE)" --assignment rebalance --people $(PEOPLE) --edges all -o $@

gantt-capped-simple.png: gantt.py schedule.py title force
	$(PY) gantt.py --title "$(TITLE)" --assignment rebalance --people $(PEOPLE) --edges latest -o $@

# --- unlimited: sprints rebalanced, no headcount limit ----------------------------
gantt-unlimited.png: gantt.py schedule.py title force
	$(PY) gantt.py --title "$(TITLE)" --assignment rebalance --edges all -o $@

gantt-unlimited-simple.png: gantt.py schedule.py title force
	$(PY) gantt.py --title "$(TITLE)" --assignment rebalance --edges latest -o $@

clean:
	rm -f gantt.png \
	      gantt-planned.png gantt-planned-simple.png \
	      gantt-capped.png gantt-capped-simple.png \
	      gantt-unlimited.png gantt-unlimited-simple.png
	rm -rf __pycache__ .venv
