import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from standup import (
    KST, PR, Ticket, apply_weekly_dedupe, changes_since_weekly, classify, compute_cutoff,
    drop_reason, effective_status, find_bulk_transitions, has_pr_activity, load_weekly_snapshot, one_line,
    save_weekly_snapshot, weekly_between,
)


def kst(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def pr(state="open", repo="RideFluxSW", closed_at=None, commits=(), feedback=(), number=1):
    return PR(repo=repo, number=number, title="[VV-1] x", state=state, url="", keys=["VV-1"],
              updated_at=datetime(2026, 11, 16, tzinfo=timezone.utc), closed_at=closed_at,
              commits_since=list(commits), feedback=list(feedback))


def ticket(key, status="IN PROGRESS", prs=(), category="vvsw"):
    return Ticket(key=key, summary=key, jira_status=status, jira_done=False, issue_type="Task",
                  parent_key=None, parent_summary=None, histories=[], prs=list(prs),
                  status=status, category=category)


def history(created, *items, author="me"):
    return {"created": created, "author": {"accountId": author}, "items": [dict(i) for i in items]}


class CutoffTest(unittest.TestCase):
    def test_vvsw_monday_goes_back_to_thursday(self):
        self.assertEqual(compute_cutoff("vvsw", date(2026, 11, 16), None, set()),
                         (kst(2026, 11, 12, 13, 50), "daily"))

    def test_vvsw_tuesday_goes_back_to_monday(self):
        self.assertEqual(compute_cutoff("vvsw", date(2026, 11, 17), None, set())[0], kst(2026, 11, 16, 13, 50))

    def test_vvsw_thursday_skips_wednesday_weekly(self):
        self.assertEqual(compute_cutoff("vvsw", date(2026, 11, 19), None, set())[0], kst(2026, 11, 17, 13, 50))

    def test_vvsw_wednesday_goes_back_a_week(self):
        self.assertEqual(compute_cutoff("vvsw", date(2026, 11, 18), None, set()),
                         (kst(2026, 11, 11, 13, 50), "weekly"))

    def test_vvsw_friday_needs_format(self):
        with self.assertRaises(ValueError):
            compute_cutoff("vvsw", date(2026, 11, 20), None, set())
        self.assertEqual(compute_cutoff("vvsw", date(2026, 11, 20), "daily", set())[0], kst(2026, 11, 19, 13, 50))

    def test_vvsw_skips_holiday(self):
        # 2026-10-05 (Mon) is a 추석 substitute holiday, so Tuesday goes back to Thursday 10-01.
        self.assertEqual(compute_cutoff("vvsw", date(2026, 10, 6), None, set())[0], kst(2026, 10, 1, 13, 50))

    def test_vvsw_skips_day_off(self):
        self.assertEqual(compute_cutoff("vvsw", date(2026, 11, 19), None, {date(2026, 11, 17)})[0],
                         kst(2026, 11, 16, 13, 50))

    def test_vvp_monday_goes_back_to_friday(self):
        self.assertEqual(compute_cutoff("vvp", date(2026, 11, 16), None, set()), (kst(2026, 11, 13, 11, 0), None))

    def test_vvp_tuesday_uses_monday_1330(self):
        self.assertEqual(compute_cutoff("vvp", date(2026, 11, 17), None, set())[0], kst(2026, 11, 16, 13, 30))

    def test_vvp_friday_uses_thursday(self):
        self.assertEqual(compute_cutoff("vvp", date(2026, 11, 20), None, set())[0], kst(2026, 11, 19, 11, 0))


class ClassifyTest(unittest.TestCase):
    def test_vvp_repo(self):
        self.assertEqual(classify("x", None, None, {"RFVVPlatform"})[0], "vvp")

    def test_non_vvp_repo_wins_over_vvp_summary(self):
        self.assertEqual(classify("VVP sanity 결과", None, None, {"RideFluxSW"})[0], "vvsw")

    def test_mixed_repos_are_ambiguous(self):
        self.assertEqual(classify("x", None, None, {"RFVVPlatform", "RideFluxSW"})[0], "ambiguous")

    def test_no_pr_summary_marker(self):
        self.assertEqual(classify("[RF-VVP] 피드백", None, None, set())[0], "vvp")

    def test_no_pr_vvp_parent(self):
        self.assertEqual(classify("x", "VV-3453", None, set())[0], "vvp")
        self.assertEqual(classify("x", "VV-9", "[RF-VVP] epic", set())[0], "vvp")

    def test_no_pr_default_vvsw(self):
        self.assertEqual(classify("Jenkins 수정", "VV-9", "CI", set())[0], "vvsw")


class StatusTest(unittest.TestCase):
    def test_merged_pr_means_done(self):
        self.assertEqual(effective_status("IN PROGRESS", False, [pr("merged")]), ("DONE", []))

    def test_open_pr_keeps_jira_status(self):
        self.assertEqual(effective_status("IN PROGRESS", False, [pr("merged"), pr("open")]), ("IN PROGRESS", []))

    def test_closed_unmerged_is_flagged(self):
        status, flags = effective_status("IN PROGRESS", False, [pr("closed")])
        self.assertEqual(status, "IN PROGRESS")
        self.assertEqual(len(flags), 1)

    def test_closed_unmerged_on_done_ticket_not_flagged(self):
        self.assertEqual(effective_status("DONE", True, [pr("closed")]), ("DONE", []))

    def test_no_pr_keeps_jira_status(self):
        self.assertEqual(effective_status("Selected for Development", False, []), ("Selected for Development", []))


class NoiseTest(unittest.TestCase):
    cutoff = kst(2026, 11, 12, 13, 50)
    to_release = {"field": "status", "fromString": "DONE", "toString": "Release", "to": "20"}
    noop = {"field": "status", "fromString": "DONE", "toString": "DONE", "to": "10"}
    real = {"field": "status", "fromString": "IN PROGRESS", "toString": "DONE", "to": "10"}
    cancel = {"field": "status", "fromString": "Selected for Development", "toString": "Canceled", "to": "30"}
    done_ids = {"10", "20", "30"}

    def test_release_only_is_noise(self):
        hs = [history("2026-11-13T00:28:19.174+0900", self.to_release),
              history("2026-11-12T11:13:00.038+0900", self.real)]  # before cutoff, ignored
        self.assertEqual(drop_reason(hs, self.cutoff, False), "release automation / no-op changes only")

    def test_noop_is_noise(self):
        self.assertIsNotNone(drop_reason([history("2026-11-13T14:48:38+0900", self.noop)], self.cutoff, False))

    def test_real_change_is_kept(self):
        hs = [history("2026-11-13T00:28:19+0900", self.to_release), history("2026-11-13T07:29:06+0900", self.real)]
        self.assertIsNone(drop_reason(hs, self.cutoff, False))

    def test_pr_activity_keeps_ticket(self):
        self.assertIsNone(drop_reason([history("2026-11-13T00:28:19+0900", self.to_release)], self.cutoff, True))

    def test_no_recent_history_is_kept(self):
        # e.g. updated by a comment only
        self.assertIsNone(drop_reason([history("2026-11-01T00:00:00+0900", self.real)], self.cutoff, False))

    def bulk(self, n, item, created="2026-11-13T16:16:05.1+0900", author="me"):
        return [[history(created, item, author=author)] for _ in range(n)]

    def bulk_refs(self, tickets):
        return find_bulk_transitions(tickets, self.cutoff, self.done_ids)

    def test_bulk_needs_enough_tickets(self):
        self.assertEqual(self.bulk_refs(self.bulk(5, self.cancel)), {("me", "2026-11-13T16:16:05.1+0900")})
        self.assertEqual(self.bulk_refs(self.bulk(4, self.cancel)), set())

    def test_bulk_spans_minute_boundary_and_mixed_done_statuses(self):
        # The real 2026-11-11 cleanup: 16:15:55-16:16:00, some to DONE and some to Canceled.
        tickets = (self.bulk(2, self.real, created="2026-11-13T16:15:55+0900")
                   + self.bulk(3, self.cancel, created="2026-11-13T16:16:00+0900"))
        self.assertEqual(len(self.bulk_refs(tickets)), 2)

    def test_bulk_split_by_author_and_window(self):
        tickets = (self.bulk(3, self.cancel) + self.bulk(3, self.cancel, author="other")
                   + self.bulk(2, self.cancel, created="2026-11-13T16:20:00+0900"))
        self.assertEqual(self.bulk_refs(tickets), set())

    def test_bulk_counts_distinct_tickets(self):
        one_ticket = [[history(f"2026-11-13T16:16:0{i}+0900", self.cancel) for i in range(6)]]
        self.assertEqual(self.bulk_refs(one_ticket), set())

    def test_bulk_ignores_non_done_and_old_changes(self):
        backlog = {"field": "status", "fromString": "IN PROGRESS", "toString": "Backlog", "to": "1"}
        self.assertEqual(self.bulk_refs(self.bulk(6, backlog)), set())
        self.assertEqual(self.bulk_refs(self.bulk(6, self.cancel, created="2026-11-01T10:00:00+0900")), set())

    def test_bulk_cleanup_is_dropped(self):
        tickets = self.bulk(5, self.cancel)
        refs = self.bulk_refs(tickets)
        self.assertEqual(drop_reason(tickets[0], self.cutoff, False, self.done_ids, refs), "bulk status cleanup")

    def test_bulk_plus_release_noise_is_dropped(self):
        tickets = self.bulk(5, self.cancel)
        refs = self.bulk_refs(tickets)
        hs = tickets[0] + [history("2026-11-14T00:00:00+0900", self.to_release)]
        self.assertEqual(drop_reason(hs, self.cutoff, False, self.done_ids, refs), "bulk status cleanup")

    def test_single_close_is_kept(self):
        tickets = self.bulk(1, self.real)
        self.assertIsNone(drop_reason(tickets[0], self.cutoff, False, self.done_ids, self.bulk_refs(tickets)))

    def test_bulk_with_other_real_change_is_kept(self):
        tickets = self.bulk(5, self.cancel)
        assigned = {"field": "assignee", "fromString": None, "toString": "에디"}
        hs = tickets[0] + [history("2026-11-14T00:00:00+0900", assigned)]
        self.assertIsNone(drop_reason(hs, self.cutoff, False, self.done_ids, self.bulk_refs(tickets)))

    def test_bulk_with_pr_activity_is_kept(self):
        tickets = self.bulk(5, self.cancel)
        self.assertIsNone(drop_reason(tickets[0], self.cutoff, True, self.done_ids, self.bulk_refs(tickets)))


class PrActivityTest(unittest.TestCase):
    cutoff = kst(2026, 11, 13, 11, 0)

    def test_merged_since_cutoff(self):
        self.assertTrue(has_pr_activity(pr("merged", closed_at=kst(2026, 11, 16, 11, 22)), self.cutoff))

    def test_merged_before_cutoff_with_bumped_updated_at(self):
        self.assertFalse(has_pr_activity(pr("merged", closed_at=kst(2026, 11, 11, 11, 0)), self.cutoff))

    def test_open_with_commits_or_feedback(self):
        self.assertTrue(has_pr_activity(pr(commits=["fix"]), self.cutoff))
        self.assertTrue(has_pr_activity(pr(feedback=["someone: lgtm"]), self.cutoff))
        self.assertFalse(has_pr_activity(pr(), self.cutoff))


class OneLineTest(unittest.TestCase):
    def test_strips_bot_markup(self):
        text = '<!-- state {"a":1} -->\n**<sub>![P2 Badge](https://x/y.svg)</sub>** Skip   Jira\n deploy'
        self.assertEqual(one_line(text), "**** Skip Jira deploy")

    def test_truncates(self):
        self.assertEqual(len(one_line("a" * 500, limit=10)), 10)


class WeeklySnapshotTest(unittest.TestCase):
    def test_weekly_between_thursday(self):
        self.assertEqual(weekly_between(kst(2026, 11, 17, 13, 50), date(2026, 11, 19), set()), date(2026, 11, 18))

    def test_weekly_between_none_on_tuesday(self):
        self.assertIsNone(weekly_between(kst(2026, 11, 16, 13, 50), date(2026, 11, 17), set()))

    def test_weekly_between_skips_day_off(self):
        self.assertIsNone(weekly_between(kst(2026, 11, 17, 13, 50), date(2026, 11, 19), {date(2026, 11, 18)}))

    def test_weekly_between_monday_after_thursday_off(self):
        # Thursday 11-19 off, so Monday's cutoff is Tuesday 11-17 and the weekly falls inside.
        cutoff, _ = compute_cutoff("vvsw", date(2026, 11, 23), None, {date(2026, 11, 19)})
        self.assertEqual(weekly_between(cutoff, date(2026, 11, 23), {date(2026, 11, 19)}), date(2026, 11, 18))

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            save_weekly_snapshot(date(2026, 11, 18), [ticket("VV-1", prs=[pr("open")])], Path(d))
            snap = load_weekly_snapshot(date(2026, 11, 18), Path(d))
            self.assertEqual(snap["tickets"]["VV-1"], {"summary": "VV-1", "status": "IN PROGRESS",
                                                       "prs": {"RideFluxSW#1": "open"}})
            self.assertIsNone(load_weekly_snapshot(date(2026, 11, 11), Path(d)))

    def test_changes_since_weekly(self):
        entry = {"status": "IN PROGRESS", "prs": {"RideFluxSW#1": "open"}}
        self.assertEqual(changes_since_weekly(ticket("VV-1", prs=[pr("open")]), entry), [])
        self.assertEqual(changes_since_weekly(ticket("VV-1", "DONE", prs=[pr("merged")]), entry),
                         ["status IN PROGRESS → DONE", "RideFluxSW#1 open → merged"])
        self.assertEqual(changes_since_weekly(ticket("VV-1", prs=[pr("open"), pr("open", number=2)]), entry),
                         ["RideFluxSW#2 new (open)"])

    def test_apply_weekly_dedupe(self):
        unchanged = ticket("VV-1", prs=[pr("open")])
        moved = ticket("VV-2", "DONE", prs=[pr("merged", number=2)])
        new = ticket("VV-3")
        vvp = ticket("VV-4", category="vvp")
        snapshot = {"tickets": {
            "VV-1": {"status": "IN PROGRESS", "prs": {"RideFluxSW#1": "open"}},
            "VV-2": {"status": "IN PROGRESS", "prs": {"RideFluxSW#2": "open"}},
            "VV-4": {"status": "IN PROGRESS", "prs": {}},
        }}
        bundle = {"tickets": [unchanged, moved, new, vvp], "covered": []}
        apply_weekly_dedupe(bundle, snapshot)
        self.assertEqual([t.key for t in bundle["tickets"]], ["VV-2", "VV-3", "VV-4"])
        self.assertEqual([t.key for t in bundle["covered"]], ["VV-1"])
        self.assertEqual(moved.since_weekly, ["status IN PROGRESS → DONE", "RideFluxSW#2 open → merged"])
        self.assertIsNone(new.since_weekly)


if __name__ == "__main__":
    unittest.main()
