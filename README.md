# Jira Standup & Weekly Report Scripts

Scripts for fetching Jira tickets and generating standup summaries or weekly work log updates.

## Files

- **`jira_last_week.py`** - Main Python script that fetches Jira tickets, outputs a markdown table or standup summary
- **`weekly_jira_update.sh`** - Wrapper script for the weekly cron job
- **`weekly_update.log`** - Log file for automated runs

## Usage

### Daily Standup Summary (≤5 sentences, via Claude)

```bash
python3 jira_last_week.py --standup
# Defaults to --days 1. Override with e.g. --days 2
```

### Weekly Markdown Table

```bash
python3 jira_last_week.py --days 7
```

### Update Google Doc

```bash
~/scripts/jira/weekly_jira_update.sh
# or:
python3 jira_last_week.py --update-doc
```

## Schedule

Weekly cron runs automatically every **Saturday at 9:00 AM** via `weekly_jira_update.sh`.

## Configuration

All secrets go in `~/.jira_env` (sourced by the wrapper script and compatible with cron):

```bash
export JIRA_ID="you@your-org.com"
export JIRA_TOKEN="your-jira-api-token"
export JIRA_BASE_URL="https://your-org.atlassian.net"
export GOOGLE_DOC_ID="your-google-doc-id"          # from the doc URL: /document/d/<ID>/edit
export ANTHROPIC_API_KEY="your-anthropic-api-key"   # required for --standup
export VENV_PYTHON="/path/to/venv/bin/python"        # optional, falls back to system python3
```

### Google OAuth

Place your OAuth credentials at `~/.google_credentials.json`. A cached token will be saved to `~/.google_token.json` after first authorization.

## Check Status

```bash
cat ~/scripts/jira/weekly_update.log
crontab -l
```
