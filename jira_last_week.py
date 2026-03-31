#!/usr/bin/env python3
"""
Jira Cloud: Fetch issues assigned to the authenticated user, updated in last N days,
using the NEW endpoint: /rest/api/3/search/jql (legacy /search is removed).

Outputs:
- Markdown table to stdout
- CSV file (default: jira_last_week_assigned_to_me.csv)
- Optional: Append table to Google Doc with --update-doc flag
- Optional: Standup summary (<=5 sentences) with --standup flag

Env vars:
  JIRA_BASE_URL     e.g. https://rideflux.atlassian.net
  JIRA_EMAIL        e.g. eforgacs@rideflux.com
  JIRA_API_TOKEN    Jira API token
  ANTHROPIC_API_KEY Claude API key (required for --standup)
  GOOGLE_DOC_ID     Google Doc ID for --update-doc
  GOOGLE_CREDENTIALS_PATH  Path to Google OAuth credentials (default: ~/.google_credentials.json)

Optional:
  JIRA_JQL          override JQL entirely
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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
    """
    Uses POST /rest/api/3/search/jql with nextPageToken pagination.
    """
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

        # Jira Cloud pagination for this endpoint uses nextPageToken
        next_token = data.get("nextPageToken")

        if not next_token or not batch:
            break

    return issues


def to_rows(issues: List[Dict]) -> List[Tuple[str, str, str, str, str]]:
    rows = []
    for it in issues:
        key = it.get("key", "")
        f = it.get("fields") or {}
        summary = (f.get("summary") or "").replace("\n", " ").strip()
        status = ((f.get("status") or {}) or {}).get("name", "")
        # Extract date from ISO timestamp (e.g., "2026-01-08T12:34:56.000+0000" -> "2026-01-08")
        created = (f.get("created") or "")[:10]
        updated = (f.get("updated") or "")[:10]
        rows.append((key, summary, status, created, updated))
    return rows


def print_markdown_table(rows: List[Tuple[str, str, str, str, str]]) -> None:
    def esc(s: str) -> str:
        return s.replace("|", "\\|")

    print("| Ticket | Title | Status | Created | Last Updated |")
    print("|---|---|---|---|---|")
    for key, title, status, created, updated in rows:
        print(f"| {esc(key)} | {esc(title)} | {esc(status)} | {esc(created)} | {esc(updated)} |")


def write_csv(rows: List[Tuple[str, str, str, str, str]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticket", "title", "status", "created", "last_updated"])
        w.writerows(rows)


def get_google_docs_service():
    """
    Authenticate and return Google Docs API service.
    Uses OAuth 2.0 with credentials from GOOGLE_CREDENTIALS_PATH.
    Token is cached at ~/.google_token.json after first authorization.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/documents"]

    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", os.path.expanduser("~/.google_credentials.json"))
    token_path = os.path.expanduser("~/.google_token.json")

    if not os.path.exists(creds_path):
        print(
            f"ERROR: Google credentials file not found at {creds_path}\n"
            "See setup instructions in the script comments.",
            file=sys.stderr,
        )
        raise FileNotFoundError(f"Credentials not found: {creds_path}")

    creds = None
    # Load cached token if it exists
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # If no valid credentials, authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the credentials for the next run
        with open(token_path, "w") as token:
            token.write(creds.to_json())

    return build("docs", "v1", credentials=creds)


def append_to_google_doc(
    doc_id: str, rows: List[Tuple[str, str, str, str, str]]
) -> None:
    """
    Append a table with today's date to the specified Google Doc.
    """
    service = get_google_docs_service()

    # Get current document to find end index
    doc = service.documents().get(documentId=doc_id).execute()
    content = doc.get("body").get("content")
    end_index = content[-1].get("endIndex") - 1

    # Build requests to insert content
    requests_list: List[Dict[str, Any]] = []

    # Insert heading with today's date
    today = datetime.now().strftime("%Y-%m-%d")
    heading_text = f"\n{today}\n"

    requests_list.append(
        {
            "insertText": {
                "location": {"index": end_index},
                "text": heading_text,
            }
        }
    )

    # Update paragraph style for the date heading
    requests_list.append(
        {
            "updateParagraphStyle": {
                "range": {
                    "startIndex": end_index + 1,
                    "endIndex": end_index + len(today) + 1,
                },
                "paragraphStyle": {
                    "namedStyleType": "HEADING_3",
                },
                "fields": "namedStyleType",
            }
        }
    )

    # Calculate new end index after heading
    table_start_index = end_index + len(heading_text)

    # Insert table
    num_rows = len(rows) + 1  # +1 for header
    num_columns = 5

    requests_list.append(
        {
            "insertTable": {
                "rows": num_rows,
                "columns": num_columns,
                "location": {"index": table_start_index},
            }
        }
    )

    # Execute the batch update
    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests_list}
    ).execute()

    # Now populate the table cells
    # Re-fetch the document to get the table structure
    doc = service.documents().get(documentId=doc_id).execute()

    # Find the table we just inserted
    content = doc.get("body").get("content")
    table = None
    for element in content:
        if "table" in element:
            table_start = element.get("startIndex")
            if table_start >= table_start_index:
                table = element.get("table")
                break

    if not table:
        print("WARNING: Could not find inserted table to populate", file=sys.stderr)
        return

    # Populate table cells
    # Build list of (index, text, is_header) tuples, then sort by index descending
    # This ensures we insert from end to beginning, avoiding index shifting issues
    cell_data: List[Tuple[int, str, bool]] = []
    headers = ["Ticket", "Title", "Status", "Created", "Last Updated"]
    all_rows = [headers] + list(rows)

    table_rows = table.get("tableRows", [])
    for row_idx, table_row in enumerate(table_rows):
        if row_idx >= len(all_rows):
            break
        data_row = all_rows[row_idx]
        cells = table_row.get("tableCells", [])

        for col_idx, cell in enumerate(cells):
            if col_idx >= len(data_row):
                break

            cell_content = cell.get("content", [])
            if not cell_content:
                continue

            # Get the start index of the paragraph inside the cell
            paragraph = cell_content[0]
            if "paragraph" not in paragraph:
                continue

            para_elements = paragraph.get("paragraph", {}).get("elements", [])
            if not para_elements:
                continue

            insert_index = para_elements[0].get("startIndex")
            text = str(data_row[col_idx])
            is_header = row_idx == 0

            cell_data.append((insert_index, text, is_header))

    # Sort by index in descending order (insert from end to beginning)
    cell_data.sort(key=lambda x: x[0], reverse=True)

    # Build requests in reverse order
    populate_requests: List[Dict[str, Any]] = []
    for insert_index, text, is_header in cell_data:
        populate_requests.append(
            {
                "insertText": {
                    "location": {"index": insert_index},
                    "text": text,
                }
            }
        )

        # Make header row bold
        if is_header:
            populate_requests.append(
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": insert_index,
                            "endIndex": insert_index + len(text),
                        },
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                }
            )

    if populate_requests:
        service.documents().batchUpdate(
            documentId=doc_id, body={"requests": populate_requests}
        ).execute()

    print(f"Successfully appended table to Google Doc: {doc_id}", file=sys.stderr)


def generate_standup_summary(rows: List[Tuple[str, str, str, str, str]]) -> str:
    """
    Use Claude API to generate a standup-ready summary (<=5 sentences).
    """
    import anthropic

    if not rows:
        return "No Jira tickets were updated in the past day."

    ticket_lines = "\n".join(
        f"- {key}: {title} [{status}]" for key, title, status, _, _ in rows
    )
    prompt = (
        "You are helping an engineer prepare a daily standup update. "
        "Given the following Jira tickets they worked on, write a concise summary "
        "of at most 5 sentences suitable for a standup meeting. "
        "Focus on what was accomplished and any tickets that moved to done/review.\n\n"
        f"Tickets:\n{ticket_lines}"
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help='Optional project key filter (e.g., "VV" or "IS").')
    parser.add_argument("--jql", help="Custom JQL (overrides defaults).")
    parser.add_argument("--days", type=int, default=7, help="Fetch issues updated in the last N days (default: 7).")
    parser.add_argument("--standup", action="store_true", help="Print a <=5 sentence standup summary using Claude (implies --days 1 if not set).")
    parser.add_argument("--csv", default="jira_last_week_assigned_to_me.csv", help="CSV output path.")
    parser.add_argument("--page-size", type=int, default=100, help="Results per page (maxResults).")
    parser.add_argument("--update-doc", action="store_true", help="Append table to Google Doc (requires GOOGLE_DOC_ID env var).")
    args = parser.parse_args()

    if args.standup and args.days == 7:
        args.days = 1

    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    # Ensure base_url has a scheme
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
            fields=["summary", "status", "created", "updated"],
            page_size=args.page_size,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    rows = to_rows(issues)

    if args.standup:
        print(generate_standup_summary(rows))
        return 0

    print_markdown_table(rows)
    write_csv(rows, args.csv)
    print(f"\nJQL used: {jql}\nWrote CSV: {args.csv}", file=sys.stderr)

    if args.update_doc:
        doc_id = os.getenv("GOOGLE_DOC_ID", "").strip()
        if not doc_id:
            print(
                "ERROR: --update-doc requires GOOGLE_DOC_ID env var\n"
                "Extract from URL: https://docs.google.com/document/d/DOCUMENT_ID/edit\n",
                file=sys.stderr,
            )
            return 2

        try:
            append_to_google_doc(doc_id, rows)
        except Exception as e:
            print(f"ERROR appending to Google Doc: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
