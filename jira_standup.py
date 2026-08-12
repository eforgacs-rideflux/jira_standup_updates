#!/usr/bin/env python3
"""
Fetch Jira tickets assigned to the current user updated in the last N days
and print them as a plain list for use in standup generation.

Env vars:
  JIRA_BASE_URL      e.g. https://your-org.atlassian.net
  JIRA_EMAIL         your Jira email
  JIRA_API_TOKEN     Jira API token

Optional:
  JIRA_JQL           override JQL entirely
"""

import argparse
import os
import sys
from typing import Dict, List, Optional, Tuple

import requests


def build_jql(args: argparse.Namespace) -> str:
    if args.jql:
        return args.jql

    parts = []
    if args.project:
        parts.append(f'project = "{args.project}"')

    parts.append("assignee = currentUser()")
    parts.append(f"updated >= -{args.days}d")
    return " AND ".join(parts) + " ORDER BY updated DESC"


def jira_search_jql(
    session: requests.Session,
    base_url: str,
    jql: str,
    fields: List[str],
    page_size: int = 100,
) -> List[Dict]:
    """Uses POST /rest/api/3/search/jql with nextPageToken pagination."""
    endpoint = f"{base_url}/rest/api/3/search/jql"
    issues: List[Dict] = []
    next_token: Optional[str] = None

    while True:
        payload: Dict = {
            "jql": jql,
            "fields": fields,
            "maxResults": page_size,
        }
        if next_token:
            payload["nextPageToken"] = next_token

        r = session.post(endpoint, json=payload, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Jira API error {r.status_code}: {r.text}")

        data = r.json()
        batch = data.get("issues") or []
        issues.extend(batch)

        next_token = data.get("nextPageToken")
        if not next_token or not batch:
            break

    return issues


def to_rows(issues: List[Dict]) -> List[Tuple[str, str, str]]:
    rows = []
    for it in issues:
        key = it.get("key", "")
        f = it.get("fields") or {}
        summary = (f.get("summary") or "").replace("\n", " ").strip()
        status = ((f.get("status") or {}) or {}).get("name", "")
        rows.append((key, summary, status))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="List recent Jira activity for standup preparation.")
    parser.add_argument("--project", help='Optional project key filter (e.g., "VV").')
    parser.add_argument("--jql", help="Custom JQL (overrides defaults).")
    parser.add_argument("--days", type=int, default=1, help="Fetch issues updated in the last N days (default: 1).")
    args = parser.parse_args()

    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"

    email = os.getenv("JIRA_EMAIL", "")
    token = os.getenv("JIRA_API_TOKEN", "")

    if not base_url or not email or not token:
        print(
            "Missing env vars. Please set:\n"
            "  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN\n",
            file=sys.stderr,
        )
        return 2

    jql = os.getenv("JIRA_JQL") or build_jql(args)

    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    try:
        issues = jira_search_jql(
            session=session,
            base_url=base_url,
            jql=jql,
            fields=["summary", "status"],
            page_size=100,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    rows = to_rows(issues)

    if not rows:
        print("No Jira tickets updated in the specified period.")
        return 0

    for key, summary, status in rows:
        print(f"- {key}: {summary} [{status}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
