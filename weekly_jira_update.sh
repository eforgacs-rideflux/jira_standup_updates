#!/bin/bash
# Weekly Jira update script for 업무일지
# Runs automatically via cron to update Google Doc

# Load Jira credentials from dedicated env file (works with cron)
if [ -f ~/.jira_env ]; then
    source ~/.jira_env
fi

# Set environment variables for the script
# Map from user's existing env vars to what the script expects
export JIRA_EMAIL="${JIRA_ID}"
export JIRA_API_TOKEN="${JIRA_TOKEN}"
export JIRA_BASE_URL="https://rideflux.atlassian.net"

# Google Docs configuration
export GOOGLE_DOC_ID="16cg_ZFGwEROpR4cSI4nvDuvXxaY4UNV53p2ygdvcDfM"
export GOOGLE_CREDENTIALS_PATH="${HOME}/.google_credentials.json"

# Script and log location
SCRIPT_DIR="${HOME}/scripts/jira"
LOG_FILE="${SCRIPT_DIR}/weekly_update.log"

# Change to script directory
cd "${SCRIPT_DIR}"

# Check if venv exists, if not use system python3
if [ -f /home/eddie/PyCharmProjects/RideFluxSW/.venv/bin/python ]; then
    PYTHON="/home/eddie/PyCharmProjects/RideFluxSW/.venv/bin/python"
else
    PYTHON="python3"
fi

# Run the script
${PYTHON} "${SCRIPT_DIR}/jira_last_week.py" --update-doc >> "${LOG_FILE}" 2>&1

# Log completion
echo "Update completed at $(date)" >> "${LOG_FILE}"
