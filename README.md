# Jira Weekly Report Automation

Automated scripts for updating 업무일지 (weekly work log) with Jira tickets.

## Files

- **`jira_last_week.py`** - Main Python script that fetches Jira tickets and updates Google Docs
- **`weekly_jira_update.sh`** - Wrapper script for cron job
- **`weekly_update.log`** - Log file for automated runs

## Schedule

Runs automatically every **Saturday at 9:00 AM** via cron.

## Manual Run

To run manually:
```bash
~/scripts/jira/weekly_jira_update.sh
```

Or with the Python script directly:
```bash
python3 ~/scripts/jira/jira_last_week.py --update-doc
```

## Check Status

View logs:
```bash
cat ~/scripts/jira/weekly_update.log
```

View cron schedule:
```bash
crontab -l
```

## Requirements

- Python 3 with Google API packages (installed in venv at `/home/eddie/PyCharmProjects/RideFluxSW/.venv`)
- Environment variables in `~/.bashrc`:
  - `JIRA_ID` (your Jira email)
  - `JIRA_TOKEN` (Jira API token)
- Google OAuth credentials at `~/.google_credentials.json`
- Google Doc ID: `16cg_ZFGwEROpR4cSI4nvDuvXxaY4UNV53p2ygdvcDfM`

## Location

These scripts are kept in `~/scripts/jira/` instead of the RideFluxSW repo to prevent them from being accidentally deleted by git operations.
