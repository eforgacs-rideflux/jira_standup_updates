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

SCRIPT_DIR="${HOME}/PyCharmProjects/jira_standup_updates"
LOG_FILE="${SCRIPT_DIR}/standup.log"

PYTHON="${VENV_PYTHON:-python3}"

${PYTHON} "${SCRIPT_DIR}/jira_standup.py" | tee -a "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
echo "Standup generated at $(date)" >> "${LOG_FILE}"
