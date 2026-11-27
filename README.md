# Jira Standup Scripts

Scripts that collect Jira/GitHub activity for VVSW/VVP standup updates. The spoken
update itself is written in a Claude Code session following `CLAUDE.md`. Nothing runs on
cron.

## Files

- **`standup.py`**: main entry point. Builds the data bundle for a standup update
  (cutoff, Jira + `gh` activity, VVP/VVSW split, noise filtering). See `CLAUDE.md`.
- **`test_standup.py`**: tests for `standup.py` (`.venv/bin/python -m unittest test_standup`).
- **`jira_standup.py`**: plain JQL listing (`- KEY: summary [STATUS]`) for ad-hoc
  queries. `standup.py` also imports its Jira search helper.
- **`jira_table.py`**: markdown table + CSV of recent tickets, with an optional
  `--update-doc` Google Doc append (needs the Google API client libraries, which aren't
  in `.venv`).

## Usage

```bash
if [ -f ~/.jira_env ]; then source ~/.jira_env; fi
export JIRA_EMAIL="${JIRA_ID}"
export JIRA_API_TOKEN="${JIRA_TOKEN}"
export JIRA_BASE_URL="https://rideflux.atlassian.net"

.venv/bin/python standup.py --meeting vvsw     # or --meeting vvp
.venv/bin/python jira_standup.py --days 7      # ad-hoc listing
```

Use `.venv/bin/python`. The system `python3` doesn't have `requests`/`holidays`.
