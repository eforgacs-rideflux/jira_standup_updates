#!/usr/bin/env python3
"""
Collect everything needed for a VVSW or VVP standup update into one bundle.

Does the deterministic parts of the workflow described in CLAUDE.md:
  - computes the cutoff (previous meeting start, skipping KR holidays / days off)
  - fetches Jira tickets updated since the cutoff, plus tickets whose only recent
    activity is on GitHub (new commits, review comments, merges)
  - reconciles Jira status with PR state (merged PR -> DONE)
  - drops tickets whose only change since the cutoff is the release automation
    moving them to "Release", and Epic-type tickets
  - classifies each ticket as VVP / VVSW / ambiguous
  - collects other people's PR feedback (reviews, comments) since the cutoff

Env vars: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN (same as jira_standup.py).
Requires an authenticated `gh` CLI.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import holidays
import requests

from jira_standup import jira_search_jql

KST = ZoneInfo("Asia/Seoul")
GH_OWNER = "RideFlux"

VVSW_TIME = time(13, 50)
VVP_TIME_MONDAY = time(13, 30)
VVP_TIME = time(11, 0)

# Mon/Tue/Thu dailies chain to each other; the Wednesday weekly chains to the
# previous Wednesday.
VVSW_DAILY_DAYS = {0, 1, 3}
VVSW_WEEKLY_DAYS = {2}
VVP_DAYS = {0, 1, 2, 3, 4}

VVP_REPOS = {"RFVVPlatform"}
VVP_PARENT_KEYS = {"VV-2982", "VV-3453"}
VVP_TEXT_RE = re.compile(r"\[RF-VVP\]|VVP")
JIRA_KEY_RE = re.compile(r"\b(VV-\d+)\b")

RELEASE_STATUS = "Release"
# This many tickets moved to a done-category status by the same person within
# BULK_WINDOW of each other is a bulk cleanup (e.g. closing out stale tickets), not
# progress to report.
BULK_MIN_TICKETS = 5
BULK_WINDOW = timedelta(seconds=60)
SNAPSHOT_DIR = Path(__file__).resolve().parent / "weekly_snapshots"
FEEDBACK_MAX_CHARS = 300
# Codex posts a summary comment and a review "receipt" around its line comments;
# only the line comments carry findings.
CODEX_BOILERPLATE_RE = re.compile(r"<!-- codex-(pull-request-review-summary|review-receipt)")


# --------------------------------------------------------------------------- cutoff

def is_day_off(d: date, days_off: Set[date]) -> bool:
    return d in holidays.KR() or d in days_off


def previous_meeting_day(today: date, meeting_days: Set[int], days_off: Set[date]) -> date:
    d = today - timedelta(days=1)
    for _ in range(60):
        if d.weekday() in meeting_days and not is_day_off(d, days_off):
            return d
        d -= timedelta(days=1)
    raise RuntimeError(f"No previous meeting day found within 60 days of {today}")


def vvsw_format_for(today: date) -> Optional[str]:
    return {0: "daily", 1: "daily", 2: "weekly", 3: "daily"}.get(today.weekday())


def compute_cutoff(
    meeting: str, today: date, fmt: Optional[str], days_off: Set[date]
) -> Tuple[datetime, Optional[str]]:
    """Return (cutoff datetime in KST, VVSW format or None for VVP)."""
    if meeting == "vvp":
        prev = previous_meeting_day(today, VVP_DAYS, days_off)
        t = VVP_TIME_MONDAY if prev.weekday() == 0 else VVP_TIME
        return datetime.combine(prev, t, KST), None

    fmt = fmt or vvsw_format_for(today)
    if fmt is None:
        raise ValueError(
            f"{today:%A} is not a VVSW standup day; pass --format daily|weekly to choose one."
        )
    days = VVSW_WEEKLY_DAYS if fmt == "weekly" else VVSW_DAILY_DAYS
    prev = previous_meeting_day(today, days, days_off)
    return datetime.combine(prev, VVSW_TIME, KST), fmt


# --------------------------------------------------------------------------- model

@dataclass
class PR:
    repo: str
    number: int
    title: str
    state: str  # open / merged / closed
    url: str
    updated_at: datetime
    closed_at: Optional[datetime]
    keys: List[str]
    commits_since: List[str] = field(default_factory=list)
    feedback: List[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass
class Ticket:
    key: str
    summary: str
    jira_status: str
    jira_done: bool
    issue_type: str
    parent_key: Optional[str]
    parent_summary: Optional[str]
    histories: List[Dict]
    prs: List[PR] = field(default_factory=list)
    source: str = "jira"  # "jira" or "github" (pulled in by PR activity only)
    status: str = ""
    category: str = ""
    category_reason: str = ""
    flags: List[str] = field(default_factory=list)
    since_weekly: Optional[List[str]] = None  # changes since the Wednesday weekly reported it


# --------------------------------------------------------------------------- rules

def classify(
    summary: str, parent_key: Optional[str], parent_summary: Optional[str], pr_repos: Iterable[str]
) -> Tuple[str, str]:
    repos = set(pr_repos)
    if repos:
        vvp = repos & VVP_REPOS
        other = repos - VVP_REPOS
        if vvp and other:
            return "ambiguous", f"PRs in both {sorted(vvp)} and {sorted(other)}"
        if vvp:
            return "vvp", f"PR repo {sorted(vvp)[0]}"
        return "vvsw", f"PR repo {', '.join(sorted(other))}"
    if VVP_TEXT_RE.search(summary or ""):
        return "vvp", "summary mentions VVP"
    if parent_key in VVP_PARENT_KEYS:
        return "vvp", f"parent {parent_key}"
    if parent_summary and VVP_TEXT_RE.search(parent_summary):
        return "vvp", f"parent {parent_key} mentions VVP"
    return "vvsw", "no PR, no VVP markers"


def effective_status(jira_status: str, jira_done: bool, prs: List[PR]) -> Tuple[str, List[str]]:
    states = {p.state for p in prs}
    if "open" in states:
        return jira_status, []
    if "merged" in states:
        return (jira_status if jira_done else "DONE"), []
    if "closed" in states and not jira_done:
        return jira_status, ["PR closed without merging: ask the user what happened"]
    return jira_status, []


def is_noise_change(item: Dict) -> bool:
    """A changelog item that isn't real progress: a move to Release, or a no-op (DONE -> DONE)."""
    if item.get("field") == "status" and item.get("toString") == RELEASE_STATUS:
        return True
    return item.get("fromString") == item.get("toString")


def is_noise_history(h: Dict) -> bool:
    return all(is_noise_change(item) for item in h.get("items", []))


def done_transition(h: Dict, done_status_ids: Set[str]) -> Optional[Tuple[str, str]]:
    """(author, created) if this history moves the ticket to a done-category status."""
    for item in h.get("items", []):
        if item.get("field") == "status" and item.get("to") in done_status_ids:
            return (h.get("author") or {}).get("accountId", ""), h["created"]
    return None


def find_bulk_transitions(
    histories_per_ticket: Iterable[List[Dict]], cutoff: datetime, done_status_ids: Set[str],
    min_tickets: int = BULK_MIN_TICKETS, window: timedelta = BULK_WINDOW,
) -> Set[Tuple[str, str]]:
    """Done-transitions made as part of a bulk cleanup, as (author, created) pairs."""
    events = []  # (author, time, ticket index, (author, created))
    for i, histories in enumerate(histories_per_ticket):
        for h in histories:
            when = parse_dt(h["created"])
            ref = done_transition(h, done_status_ids)
            if ref and when >= cutoff:
                events.append((ref[0], when, i, ref))
    bulk = set()
    for author, when, _, ref in events:
        nearby = {i for a, w, i, _ in events if a == author and abs(w - when) <= window}
        if len(nearby) >= min_tickets:
            bulk.add(ref)
    return bulk


def drop_reason(
    histories: List[Dict], cutoff: datetime, has_pr_activity: bool,
    done_status_ids: Set[str] = frozenset(), bulk_transitions: Set[Tuple[str, str]] = frozenset(),
) -> Optional[str]:
    """Why a ticket's changes since the cutoff aren't worth reporting, or None to keep it."""
    if has_pr_activity:
        return None
    recent = [h for h in histories if parse_dt(h["created"]) >= cutoff]
    if not recent:
        return None
    if all(is_noise_history(h) for h in recent):
        return "release automation / no-op changes only"
    if all(is_noise_history(h) or done_transition(h, done_status_ids) in bulk_transitions for h in recent):
        return "bulk status cleanup"
    return None


def has_pr_activity(pr: PR, cutoff: datetime) -> bool:
    merged_or_closed_since = pr.closed_at is not None and pr.closed_at >= cutoff
    return merged_or_closed_since or bool(pr.commits_since) or bool(pr.feedback)


# --------------------------------------------------------------------------- helpers

def parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s or s.startswith("0001-01-01"):
        return None
    return datetime.fromisoformat(s)


def one_line(text: str, limit: int = FEEDBACK_MAX_CHARS) -> str:
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # badge images
    text = re.sub(r"</?su[bp]>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def gh_json(args: List[str]):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


# --------------------------------------------------------------------------- GitHub

def fetch_prs(author: str, limit: int = 200) -> List[PR]:
    rows = gh_json([
        "search", "prs", f"--author={author}", f"--owner={GH_OWNER}", "--sort=updated",
        "--json", "number,title,repository,state,closedAt,updatedAt,url", "--limit", str(limit),
    ]) or []
    prs = []
    for r in rows:
        keys = JIRA_KEY_RE.findall(r["title"])
        if not keys:
            continue
        prs.append(PR(
            repo=r["repository"]["name"],
            number=r["number"],
            title=r["title"],
            state=r["state"],
            url=r["url"],
            updated_at=parse_dt(r["updatedAt"]),
            closed_at=parse_dt(r["closedAt"]),
            keys=keys,
        ))
    return prs


def fill_pr_activity(pr: PR, cutoff: datetime, me: str) -> None:
    full = f"{GH_OWNER}/{pr.repo}"
    data = gh_json(["pr", "view", str(pr.number), "-R", full, "--json", "commits,reviews,comments"])
    line_comments = gh_json(["api", "--paginate", f"repos/{full}/pulls/{pr.number}/comments"]) or []

    for c in data.get("commits", []):
        if parse_dt(c["committedDate"]) >= cutoff:
            pr.commits_since.append(c["messageHeadline"])

    entries = []
    for r in data.get("reviews", []):
        if r.get("body") or r.get("state") not in ("COMMENTED", None):
            entries.append((r["author"]["login"], r.get("submittedAt"), f"review {r['state']}", r.get("body", "")))
    for c in data.get("comments", []):
        entries.append((c["author"]["login"], c["createdAt"], "comment", c["body"]))
    for c in line_comments:
        entries.append((c["user"]["login"], c["created_at"], f"line comment {c.get('path', '')}", c["body"]))

    seen = set()
    for login, created, kind, body in sorted(entries, key=lambda e: e[1] or ""):
        if login == me or not created or parse_dt(created) < cutoff:
            continue
        if CODEX_BOILERPLATE_RE.search(body or ""):
            continue
        if (login, body) in seen:
            continue
        seen.add((login, body))
        when = parse_dt(created).astimezone(KST).strftime("%m-%d %H:%M")
        pr.feedback.append(f"{login} ({kind}, {when}): {one_line(body) or '(no text)'}")


def gh_login() -> str:
    return subprocess.run(
        ["gh", "api", "user", "--jq", ".login"], check=True, capture_output=True, text=True
    ).stdout.strip()


# --------------------------------------------------------------------------- Jira

def jira_session() -> Tuple[requests.Session, str]:
    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    email = os.getenv("JIRA_EMAIL") or os.getenv("JIRA_ID", "")
    token = os.getenv("JIRA_API_TOKEN") or os.getenv("JIRA_TOKEN", "")
    if not base_url or not email or not token:
        raise RuntimeError("Missing env vars: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN")
    session = requests.Session()
    session.auth = (email, token)
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return session, base_url


def fetch_done_status_ids(session: requests.Session, base_url: str) -> Set[str]:
    r = session.get(f"{base_url}/rest/api/3/status", timeout=30)
    r.raise_for_status()
    return {s["id"] for s in r.json() if (s.get("statusCategory") or {}).get("key") == "done"}


def fetch_tickets(session: requests.Session, base_url: str, jql: str, source: str) -> List[Ticket]:
    issues = jira_search_jql(
        session, base_url, jql,
        fields=["summary", "status", "issuetype", "parent"],
        expand="changelog",
    )
    tickets = []
    for it in issues:
        f = it.get("fields") or {}
        parent = f.get("parent") or {}
        status = f.get("status") or {}
        tickets.append(Ticket(
            key=it["key"],
            summary=(f.get("summary") or "").replace("\n", " ").strip(),
            jira_status=status.get("name", ""),
            jira_done=(status.get("statusCategory") or {}).get("key") == "done",
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            parent_key=parent.get("key"),
            parent_summary=(parent.get("fields") or {}).get("summary"),
            histories=(it.get("changelog") or {}).get("histories", []),
            source=source,
        ))
    return tickets


# --------------------------------------------------------------------------- bundle

def build_bundle(meeting: str, cutoff: datetime, me: str) -> Dict:
    session, base_url = jira_session()
    cutoff_jql = cutoff.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    tickets = fetch_tickets(
        session, base_url,
        f'assignee = currentUser() AND updated >= "{cutoff_jql}" ORDER BY updated DESC',
        source="jira",
    )

    prs = fetch_prs(me)
    recent_prs = [
        p for p in prs
        if p.updated_at >= cutoff and (p.state == "open" or (p.closed_at and p.closed_at >= cutoff))
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda p: fill_pr_activity(p, cutoff, me), recent_prs))

    known = {t.key for t in tickets}
    github_only = sorted({
        k for p in recent_prs if has_pr_activity(p, cutoff) for k in p.keys if k not in known
    })
    if github_only:
        tickets += fetch_tickets(session, base_url, f"key in ({', '.join(github_only)})", source="github")

    done_status_ids = fetch_done_status_ids(session, base_url)
    bulk_transitions = find_bulk_transitions((t.histories for t in tickets), cutoff, done_status_ids)

    prs_by_key: Dict[str, List[PR]] = {}
    for p in prs:
        for k in p.keys:
            prs_by_key.setdefault(k, []).append(p)

    kept, dropped = [], []
    for t in tickets:
        t.prs = prs_by_key.get(t.key, [])
        activity = any(has_pr_activity(p, cutoff) for p in t.prs)
        if t.issue_type == "Epic":
            dropped.append((t, "epic"))
            continue
        reason = drop_reason(t.histories, cutoff, activity, done_status_ids, bulk_transitions)
        if reason:
            dropped.append((t, reason))
            continue
        t.status, t.flags = effective_status(t.jira_status, t.jira_done, t.prs)
        t.category, t.category_reason = classify(
            t.summary, t.parent_key, t.parent_summary, {p.repo for p in t.prs}
        )
        kept.append(t)

    return {"tickets": kept, "dropped": dropped, "covered": []}


# --------------------------------------------------------------------------- weekly snapshot

def weekly_between(cutoff: datetime, today: date, days_off: Set[date]) -> Optional[date]:
    """The Wednesday weekly held after the cutoff and before today, if any."""
    d = today - timedelta(days=1)
    while d > cutoff.date():
        if d.weekday() == 2 and not is_day_off(d, days_off):
            return d
        d -= timedelta(days=1)
    return None


def snapshot_path(wednesday: date, directory: Path = SNAPSHOT_DIR) -> Path:
    return directory / f"{wednesday.isoformat()}.json"


def save_weekly_snapshot(wednesday: date, tickets: List[Ticket], directory: Path = SNAPSHOT_DIR) -> Path:
    path = snapshot_path(wednesday, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "date": wednesday.isoformat(),
        "saved_at": datetime.now(KST).isoformat(timespec="seconds"),
        "tickets": {
            t.key: {"summary": t.summary, "status": t.status, "prs": {p.ref: p.state for p in t.prs}}
            for t in tickets
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def load_weekly_snapshot(wednesday: date, directory: Path = SNAPSHOT_DIR) -> Optional[Dict]:
    path = snapshot_path(wednesday, directory)
    return json.loads(path.read_text()) if path.exists() else None


def changes_since_weekly(t: Ticket, entry: Dict) -> List[str]:
    """What moved since the weekly: status change, or a PR that is new or changed state."""
    changes = []
    if t.status != entry["status"]:
        changes.append(f"status {entry['status']} → {t.status}")
    for p in t.prs:
        old = entry["prs"].get(p.ref)
        if old is None:
            changes.append(f"{p.ref} new ({p.state})")
        elif old != p.state:
            changes.append(f"{p.ref} {old} → {p.state}")
    return changes


def apply_weekly_dedupe(bundle: Dict, snapshot: Dict) -> None:
    """Move VVSW tickets the weekly already reported, unchanged since, into bundle["covered"]."""
    reported = snapshot["tickets"]
    kept = []
    for t in bundle["tickets"]:
        entry = reported.get(t.key)
        if t.category != "vvsw" or entry is None:
            kept.append(t)
            continue
        t.since_weekly = changes_since_weekly(t, entry)
        (kept if t.since_weekly else bundle["covered"]).append(t)
    bundle["tickets"] = kept


# --------------------------------------------------------------------------- output

def ticket_to_dict(t: Ticket, cutoff: datetime) -> Dict:
    d = asdict(t)
    d.pop("histories")
    d["prs"] = [
        {
            "ref": p.ref, "title": p.title, "state": p.state, "url": p.url,
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            "active_since_cutoff": has_pr_activity(p, cutoff),
            "commits_since": p.commits_since, "feedback": p.feedback,
        }
        for p in t.prs
    ]
    return d


def render_ticket(t: Ticket, cutoff: datetime) -> List[str]:
    lines = [f"- {t.key}: {t.summary} [{t.status}]"]
    meta = [f"jira: {t.jira_status}"]
    if t.source == "github":
        meta.append("pulled in by PR activity (Jira not updated)")
    if t.prs:
        meta.append("PRs: " + ", ".join(f"{p.ref} {p.state}" for p in t.prs))
    lines.append("    " + " | ".join(meta))
    for p in t.prs:
        if p.closed_at and p.closed_at >= cutoff:
            lines.append(f"    {p.ref} {p.state} {p.closed_at.astimezone(KST):%m-%d %H:%M}")
        if p.commits_since:
            lines.append(f"    {p.ref} commits since cutoff: {len(p.commits_since)} (latest: {p.commits_since[-1]})")
        for fb in p.feedback:
            lines.append(f"    {p.ref} feedback: {fb}")
    for flag in t.flags:
        lines.append(f"    FLAG: {flag}")
    if t.since_weekly:
        lines.append(f"    in Wednesday weekly; since then: {'; '.join(t.since_weekly)}")
    return lines


def render_text(meeting: str, fmt: Optional[str], today: date, cutoff: datetime,
                warnings: List[str], notes: List[str], bundle: Dict) -> str:
    label = "VVP daily" if meeting == "vvp" else f"VVSW {fmt}"
    out = [
        f"Meeting: {label} ({today:%a %Y-%m-%d})",
        f"Cutoff:  {cutoff:%a %Y-%m-%d %H:%M} KST",
    ]
    out += [f"WARNING: {w}" for w in warnings]
    out += [f"Note: {n}" for n in notes]

    other = "vvsw" if meeting == "vvp" else "vvp"
    groups = {c: [t for t in bundle["tickets"] if t.category == c] for c in ("vvp", "vvsw", "ambiguous")}

    out += ["", f"== {meeting.upper()} ({len(groups[meeting])}) =="]
    for t in groups[meeting]:
        out += render_ticket(t, cutoff)
    if groups["ambiguous"]:
        out += ["", f"== Ambiguous: ask the user ({len(groups['ambiguous'])}) =="]
        for t in groups["ambiguous"]:
            out += render_ticket(t, cutoff) + [f"    reason: {t.category_reason}"]
    if bundle["covered"]:
        out += ["", f"== Covered by Wednesday weekly, unchanged: don't report ({len(bundle['covered'])}) =="]
        out += [f"- {t.key}: {t.summary} [{t.status}]" for t in bundle["covered"]]
    out += ["", f"== Excluded: {other.upper()} work ({len(groups[other])}) =="]
    out += [f"- {t.key}: {t.summary} ({t.category_reason})" for t in groups[other]]
    out += ["", f"== Dropped ({len(bundle['dropped'])}) =="]
    out += [f"- {t.key} [{t.jira_status}] {reason}" for t, reason in bundle["dropped"]]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect standup data for the VVSW or VVP meeting.")
    parser.add_argument("--meeting", choices=["vvsw", "vvp"], required=True)
    parser.add_argument("--format", choices=["daily", "weekly"],
                        help="VVSW only: override the format (required on Fridays).")
    parser.add_argument("--today", type=date.fromisoformat,
                        help="Pretend today is this date (YYYY-MM-DD). Default: today in KST.")
    parser.add_argument("--day-off", type=date.fromisoformat, action="append", default=[],
                        help="Team/company day off to skip as a meeting day (repeatable).")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args()

    today = args.today or datetime.now(KST).date()
    days_off = set(args.day_off)
    warnings = []
    if is_day_off(today, days_off):
        name = holidays.KR().get(today) or "day off"
        warnings.append(f"today is {name}: there is no meeting")

    try:
        cutoff, fmt = compute_cutoff(args.meeting, today, args.format, days_off)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        bundle = build_bundle(args.meeting, cutoff, gh_login())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    notes = []
    if args.meeting == "vvsw" and fmt == "weekly" and today.weekday() == 2 and not args.today:
        vvsw = [t for t in bundle["tickets"] if t.category == "vvsw"]
        notes.append(f"Saved weekly snapshot: {save_weekly_snapshot(today, vvsw)}")
    elif args.meeting == "vvsw" and fmt == "daily":
        wednesday = weekly_between(cutoff, today, days_off)
        if wednesday:
            snapshot = load_weekly_snapshot(wednesday)
            if snapshot:
                apply_weekly_dedupe(bundle, snapshot)
                notes.append(f"Deduped against weekly snapshot {snapshot_path(wednesday).name}")
            else:
                warnings.append(
                    f"no weekly snapshot for {wednesday}: check the Notion entry by hand (see CLAUDE.md)"
                )

    if args.json:
        print(json.dumps({
            "meeting": args.meeting,
            "format": fmt,
            "today": today.isoformat(),
            "cutoff": cutoff.isoformat(),
            "warnings": warnings,
            "notes": notes,
            "tickets": [ticket_to_dict(t, cutoff) for t in bundle["tickets"]],
            "covered_by_weekly": [{"key": t.key, "status": t.status} for t in bundle["covered"]],
            "dropped": [{"key": t.key, "jira_status": t.jira_status, "reason": r} for t, r in bundle["dropped"]],
        }, ensure_ascii=False, indent=2))
    else:
        print(render_text(args.meeting, fmt, today, cutoff, warnings, notes, bundle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
