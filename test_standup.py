import unittest
from datetime import date, datetime, timezone

from standup import (
    KST, PR, classify, compute_cutoff, effective_status, has_pr_activity, is_release_noise, one_line,
)


def kst(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def pr(state="open", repo="RideFluxSW", closed_at=None, commits=(), feedback=()):
    return PR(repo=repo, number=1, title="[VV-1] x", state=state, url="", keys=["VV-1"],
              updated_at=datetime(2026, 11, 16, tzinfo=timezone.utc), closed_at=closed_at,
              commits_since=list(commits), feedback=list(feedback))


def history(created, *items):
    return {"created": created, "items": [dict(i) for i in items]}


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
    to_release = {"field": "status", "fromString": "DONE", "toString": "Release"}
    noop = {"field": "status", "fromString": "DONE", "toString": "DONE"}
    real = {"field": "status", "fromString": "IN PROGRESS", "toString": "DONE"}

    def test_release_only_is_noise(self):
        hs = [history("2026-11-13T00:28:19.174+0900", self.to_release),
              history("2026-11-12T11:13:00.038+0900", self.real)]  # before cutoff, ignored
        self.assertTrue(is_release_noise(hs, self.cutoff, False))

    def test_noop_is_noise(self):
        self.assertTrue(is_release_noise([history("2026-11-13T14:48:38+0900", self.noop)], self.cutoff, False))

    def test_real_change_is_kept(self):
        hs = [history("2026-11-13T00:28:19+0900", self.to_release), history("2026-11-13T07:29:06+0900", self.real)]
        self.assertFalse(is_release_noise(hs, self.cutoff, False))

    def test_pr_activity_keeps_ticket(self):
        self.assertFalse(is_release_noise([history("2026-11-13T00:28:19+0900", self.to_release)], self.cutoff, True))

    def test_no_recent_history_is_kept(self):
        # e.g. updated by a comment only
        self.assertFalse(is_release_noise([history("2026-11-01T00:00:00+0900", self.real)], self.cutoff, False))


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


if __name__ == "__main__":
    unittest.main()
