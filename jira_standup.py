#!/usr/bin/env python3
"""
Fetch Jira tickets assigned to the current user updated in the last N days
and generate a Korean natural language standup summary via Claude.

Env vars:
  JIRA_BASE_URL      e.g. https://your-org.atlassian.net
  JIRA_EMAIL         your Jira email
  JIRA_API_TOKEN     Jira API token
  ANTHROPIC_API_KEY  Claude API key
  STANDUP_DOC_ID     Google Doc ID for --update-doc
  GOOGLE_CREDENTIALS_PATH  Path to Google OAuth credentials (default: ~/.google_credentials.json)

Optional:
  JIRA_JQL           override JQL entirely
"""

import argparse
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import anthropic
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


def generate_korean_summary(rows: List[Tuple[str, str, str]]) -> str:
    if not rows:
        return "오늘 업데이트된 Jira 티켓이 없습니다."

    ticket_lines = "\n".join(
        f"- {key}: {title} [{status}]" for key, title, status in rows
    )
    prompt = (
        "당신은 개발팀의 데일리 스탠드업 미팅을 도와주는 어시스턴트입니다. "
        "아래 Jira 티켓 목록을 바탕으로 스탠드업에서 발표할 수 있는 간결한 업무 요약을 "
        "한국어로 작성해주세요. 최대 5문장으로 작성하고, 완료된 작업과 진행 중인 작업을 "
        "중심으로 설명해주세요.\n\n"
        f"티켓 목록:\n{ticket_lines}"
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def append_summary_to_google_doc(doc_id: str, summary: str) -> None:
    """Append today's date as a heading and the summary text to a Google Doc."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/documents"]
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", os.path.expanduser("~/.google_credentials.json"))
    token_path = os.path.expanduser("~/.google_token.json")

    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Google credentials not found: {creds_path}")

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as token:
            token.write(creds.to_json())

    service = build("docs", "v1", credentials=creds)

    doc = service.documents().get(documentId=doc_id).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1

    today = datetime.now().strftime("%Y-%m-%d")
    # Insert date heading + summary text, then style the heading
    insert_text = f"\n{today}\n{summary}\n"

    requests_list = [
        {
            "insertText": {
                "location": {"index": end_index},
                "text": insert_text,
            }
        },
        {
            "updateParagraphStyle": {
                "range": {
                    "startIndex": end_index + 1,
                    "endIndex": end_index + 1 + len(today),
                },
                "paragraphStyle": {"namedStyleType": "HEADING_3"},
                "fields": "namedStyleType",
            }
        },
    ]

    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests_list}
    ).execute()

    print(f"Successfully appended standup to Google Doc: {doc_id}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Korean standup summary from recent Jira activity.")
    parser.add_argument("--project", help='Optional project key filter (e.g., "VV").')
    parser.add_argument("--jql", help="Custom JQL (overrides defaults).")
    parser.add_argument("--days", type=int, default=1, help="Fetch issues updated in the last N days (default: 1).")
    parser.add_argument("--update-doc", action="store_true", help="Append summary to Google Doc (requires STANDUP_DOC_ID env var).")
    args = parser.parse_args()

    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"

    email = os.getenv("JIRA_EMAIL", "")
    token = os.getenv("JIRA_API_TOKEN", "")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not base_url or not email or not token:
        print(
            "Missing env vars. Please set:\n"
            "  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN\n",
            file=sys.stderr,
        )
        return 2

    if not api_key:
        print("Missing env var: ANTHROPIC_API_KEY\n", file=sys.stderr)
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
    summary = generate_korean_summary(rows)
    print(summary)

    if args.update_doc:
        doc_id = os.getenv("STANDUP_DOC_ID", "").strip()
        if not doc_id:
            print("ERROR: --update-doc requires STANDUP_DOC_ID env var\n", file=sys.stderr)
            return 2
        try:
            append_summary_to_google_doc(doc_id, summary)
        except Exception as e:
            print(f"ERROR appending to Google Doc: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
