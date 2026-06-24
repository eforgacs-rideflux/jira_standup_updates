#!/bin/bash
# Daily standup summary script
# Generates a Korean natural language summary of yesterday's Jira activity

if [ -f ~/.jira_env ]; then
    source ~/.jira_env
fi

export JIRA_EMAIL="${JIRA_ID}"
export JIRA_API_TOKEN="${JIRA_TOKEN}"
export JIRA_BASE_URL="${JIRA_BASE_URL:-https://your-org.atlassian.net}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"
export STANDUP_DOC_ID="${STANDUP_DOC_ID}"

SCRIPT_DIR="${HOME}/PyCharmProjects/jira_standup_updates"
LOG_FILE="${SCRIPT_DIR}/standup.log"

PYTHON="${VENV_PYTHON:-python3}"

# Skip Korean public holidays
if ${PYTHON} -c "import holidays, datetime; exit(0 if datetime.date.today() in holidays.KR() else 1)"; then
    echo "Skipping standup: today is a Korean public holiday." | tee -a "${LOG_FILE}"
    exit 0
fi

# On Wednesdays, recap the past week
if [ "$(date +%u)" -eq 3 ]; then
    DAYS_ARG="--days 7"
else
    DAYS_ARG=""
fi

${PYTHON} "${SCRIPT_DIR}/jira_standup.py" --update-doc ${DAYS_ARG} | tee -a "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
echo "Standup generated at $(date)" >> "${LOG_FILE}"
