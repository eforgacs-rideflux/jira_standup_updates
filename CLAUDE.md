# Standup Update Workflow

This repo generates content for a recurring team standup. There are two formats
depending on the day of the week — always check which one applies before generating anything.

## Schedule

- Meetings are always at **1:50 PM KST**, every weekday (Mon-Fri).
- **Monday, Tuesday, Thursday**: short-form daily update.
- **Wednesday**: weekly recap (covers the full week since last Wednesday's meeting).
- **Friday**: not a standup day (no meeting-format entry above covers it, per the
  Mon/Tue/Thu vs. Wed split) — if asked to generate an update on a Friday, ask the user
  which format applies.
- Every update — daily or weekly — must cover **everything since the last meeting**, not
  just a fixed calendar window. Since meetings don't happen daily, the cutoff is the
  *previous meeting day's* 1:50 PM KST, not "yesterday":
  - Tuesday's update covers since Monday 1:50 PM KST.
  - Thursday's update covers since Tuesday 1:50 PM KST.
  - **Monday's update covers since last Thursday 1:50 PM KST** (skipping the weekend).
  - Wednesday's weekly update covers since last Wednesday 1:50 PM KST.
- Since the cutoff time is now fixed (1:50 PM KST) and the cutoff day is derivable from
  today's weekday, don't ask the user for the meeting time — compute the previous
  meeting date and use `YYYY-MM-DD 13:50` directly in the JQL.

## Cross-reference with GitHub PRs (source of truth for completion)

Jira status can lag behind reality (a ticket left "Selected for Development" or "IN
PROGRESS" after its PR already merged). Always cross-check ticket status against actual
merged PRs before finalizing the status used in the bullet list:

```bash
gh search prs --author=eforgacs-rideflux --owner=RideFlux --sort=updated \
  --json number,title,repository,state,closedAt,url --limit 100
```

- PR titles in this org are consistently prefixed with the Jira key (e.g. `[VV-3018]
  ...`), so match on that to pair each PR with its ticket.
- `state` is one of `open`, `merged`, or `closed` (closed without merging — check
  `mergedAt`/use `gh pr view <n> --json state,mergedAt` if ambiguous from search
  results).
- If a PR for a ticket is `merged` but Jira still shows it as open/in-progress, treat the
  ticket as **done** in the bullet list and note the discrepancy so the user can update
  Jira.
- If a PR is still `open`, keep the ticket's Jira status (e.g. `IN PROGRESS`) as-is —
  don't downgrade based on GitHub alone.
- If a PR was **closed without merging**, don't assume it's a blocker or that work
  stalled — it may simply mean the team decided not to do the work. **Ask the user**
  what happened rather than guessing; don't report it as a blocker/problem unless they
  confirm it actually is one. If they say it was intentionally dropped, note it plainly
  (e.g. "결정: 진행하지 않기로 함") rather than framing it as something blocking them.
- Not every ticket will have a matching PR (e.g. Notion/investigation-only tickets) —
  that's fine, just fall back to the Jira status for those.

## How to get the Jira data

Use `jira_standup.py` with a custom JQL cutoff so the window matches the actual last
meeting time, not just a calendar day count:

```bash
if [ -f ~/.jira_env ]; then source ~/.jira_env; fi
export JIRA_EMAIL="${JIRA_ID}"
export JIRA_API_TOKEN="${JIRA_TOKEN}"
export JIRA_BASE_URL="https://rideflux.atlassian.net"
export JIRA_JQL='assignee = currentUser() AND updated >= "YYYY-MM-DD HH:MM" ORDER BY updated DESC'
python3 jira_standup.py
```

- The cutoff is always **1:50 PM KST** on the previous meeting day (see Schedule above
  for how to derive that day from today's weekday) — no need to ask the user.
- `--days N` also works for a rough day-granularity window, but prefer the explicit
  timestamp JQL above for accuracy, especially around weekends (Monday's cutoff is the
  prior Thursday, not "1 day ago").
- `jira_table.py --update-doc` pushes a markdown table to the configured Google Doc
  (Saturday cron job) — this is a separate, less relevant flow. Don't use it unless
  asked.

## Output format (every update, daily or weekly)

Every update has two parts, always printed in this order:

### 1. Bullet list

One line per Jira ticket, **grouped by topic/theme**, in this format:

```
- KEY: summary [STATUS]
```

Group by theme/project area (e.g. Jenkins infra, SAD CR automation, GitHub/Slack
notification work, RTV bugs, docs, etc.) rather than raw `updated` order or status. This
applies to every day's bullet list, daily or weekly — not just the Wednesday recap. Within
each topic group, list tickets in whatever order reads naturally (e.g. status or recency);
there's no required sub-sort. This is the same raw-ish list `jira_standup.py` prints —
just grouped by topic instead of by updated date.

### 2. Spoken update (Korean, then English translation)

After the bullet list, print the natural-language spoken version:

- **Korean version first** — this is what actually gets read aloud / posted to Notion.
- **English translation second** — for the user's own review only, not what gets
  posted/spoken.
- **Do not mention Jira ticket numbers** (e.g. VV-3114) anywhere in the spoken update —
  describe the work in plain language only.

## Daily format (Mon/Tue/Thu)

The spoken portion is structured in Korean around three sections, based on tickets
updated since the last meeting:

1. 지난 미팅 이후 한 일 (What I did since the last meeting)
2. 오늘/다음 할 일 (What I plan to do today / next)
3. 블로커 (Any blockers)

Keep it short — this is a spoken daily standup update, not a full report.

## Weekly format (Wed)

Triggered by this recurring Slack reminder:

> Reminder: 30분 후 스탠딩입니다. 스탠딩 전에 노션에 아래 내용을 간단히 작성해주세요!
> 1. 지난 일주일 동안 한 일
> 2. 현재 하고 있는 일 혹은 다음 일주일 동안 할 일
> 3. 겪고 있는 문제나 막힌 일

The spoken portion is structured in Korean around these three sections:

1. 지난 일주일 동안 한 일 (What I did in the past week) — pull from Jira tickets
   updated since last Wednesday's meeting, grouped by theme/project area (e.g. Jenkins
   infra, GitHub/Slack notification work, SAD CR automation, RTV bugs, etc.) rather than
   as a flat list.
2. 현재 하고 있는 일 혹은 다음 일주일 동안 할 일 (What I'm currently doing / plan to do
   next week) — based on tickets still `IN PROGRESS` / `진행중` or `Selected for
   Development` / `개발예정`.
3. 겪고 있는 문제나 막힌 일 (Problems or blockers) — ask the user directly if not
   evident from ticket status/comments; don't fabricate blockers.

## Publishing

Don't push anything to Notion, Slack, or Google Docs automatically — this is a content
generation aid only, unless the user explicitly asks to publish somewhere.
