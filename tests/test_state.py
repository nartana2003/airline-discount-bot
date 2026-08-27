"""Run-to-run memory: when the digest is due, and the recorded quota."""

import unittest
from datetime import datetime, timedelta, timezone

from flightbot import config
from flightbot.state import State


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def state(**data):
    return State(path=None, data=data)


class DigestSchedule(unittest.TestCase):
    """Every DIGEST_INTERVAL_DAYS, not on the turn of the calendar month."""

    # digest_due() allows half a day of slack, so the real boundary sits here.
    THRESHOLD = config.DIGEST_INTERVAL_DAYS - 0.5

    def test_never_sent_is_due(self):
        self.assertTrue(state().digest_due())

    def test_just_sent_is_not_due(self):
        self.assertFalse(state(digest={"at": _ago(0)}).digest_due())

    def test_one_week_in_is_not_due(self):
        self.assertFalse(state(digest={"at": _ago(7)}).digest_due())

    def test_a_full_interval_in_is_due(self):
        self.assertTrue(state(digest={"at": _ago(config.DIGEST_INTERVAL_DAYS)}).digest_due())

    def test_a_weekly_cron_landing_just_short_still_fires(self):
        """Runs land almost exactly an interval apart, so a few seconds under
        would otherwise push the digest out by a whole extra week."""
        self.assertTrue(state(digest={"at": _ago(self.THRESHOLD + 0.1)}).digest_due())

    def test_well_short_does_not_fire(self):
        self.assertFalse(state(digest={"at": _ago(self.THRESHOLD - 0.1)}).digest_due())

    def test_an_unreadable_stamp_sends_rather_than_never(self):
        self.assertTrue(state(digest={"at": "not a date"}).digest_due())

    def test_recording_makes_it_not_due(self):
        s = state(digest={"at": _ago(30)})
        self.assertTrue(s.digest_due())
        s.record_digest()
        self.assertFalse(s.digest_due())


class RecordedQuota(unittest.TestCase):
    def test_absent_until_a_run_asks(self):
        self.assertIsNone(state().quota())

    def test_none_is_not_recorded(self):
        """--demo never asks SerpApi, so it must not overwrite a real figure."""
        s = state(quota={"left": 200, "at": _ago(1)})
        s.record_quota(None)
        self.assertEqual(s.quota()["left"], 200)

    def test_round_trips(self):
        s = state()
        s.record_quota(178)
        self.assertEqual(s.quota()["left"], 178)
        self.assertTrue(s.quota()["at"])

    def test_a_malformed_entry_reads_as_absent(self):
        self.assertIsNone(state(quota={"left": "lots"}).quota())
        self.assertIsNone(state(quota="200").quota())


if __name__ == "__main__":
    unittest.main()


class RunsRemaining(unittest.TestCase):
    """How many runs are left before SerpApi's quota resets.

    `runs_per_month` alone is an average across a full billing period and says
    nothing about where the CURRENT period stands, so dividing remaining quota
    by it made every run plan against a shrinking numerator over a constant
    denominator - density thinned every run, all period long, snapping back
    only at reset. These pin the fix: it needs a WITNESSED reset (quota going
    up between two readings) before it will deviate from the honest average.
    """

    def log(self, *readings):
        """readings: [(days_ago, left), ...], oldest first."""
        return [{"at": _ago(d), "left": left} for d, left in readings]

    def test_no_history_falls_back_to_the_average(self):
        self.assertEqual(state().runs_remaining(4.3), 4.3)

    def test_history_with_no_reset_ever_seen_still_falls_back(self):
        """This is the deliberately conservative case: without a witnessed
        reset there is no evidence of where a period boundary sits, and
        guessing one reproduces the exact shrink this method exists to fix."""
        s = state(quota_log=self.log((28, 240), (21, 191), (14, 151), (7, 118)))
        self.assertEqual(s.runs_remaining(4.3), 4.3)

    def test_a_witnessed_reset_is_used_as_the_period_boundary(self):
        # 240 -> 191 -> reset (jumps back to 240) -> 174
        s = state(quota_log=self.log((21, 240), (14, 191), (7, 240), (0, 174)))
        # Two runs recorded since the reset (the 240 and the 174 readings).
        self.assertAlmostEqual(s.runs_remaining(4.3), 4.3 - 2, places=2)

    def test_period_length_is_learned_after_two_resets(self):
        """A billing cycle measurably longer than the 30.44-day prior should
        scale the expected total runs up to match, not stay pinned to it."""
        # spend, RESET (35 days after the previous reset), spend, RESET
        s = state(quota_log=self.log(
            (70, 174), (35, 240), (28, 174), (0, 240)))
        remaining = s.runs_remaining(4.3, period_days=30.44)
        expected_total = 4.3 * (35 / 30.44)
        # One run recorded since the latest reset (the final 240 reading).
        self.assertAlmostEqual(remaining, expected_total - 1, places=2)

    def test_never_drops_below_one_runs_worth(self):
        """A burst of hand-triggered runs must not starve the next
        scheduled one down to nothing."""
        s = state(quota_log=self.log(
            (10, 240), *[(10 - i, 200 - i * 10) for i in range(9)]))
        self.assertGreaterEqual(s.runs_remaining(4.3), 1.0)

    def test_malformed_entries_are_ignored_not_fatal(self):
        s = state(quota_log=[{"at": "not a date", "left": 5}, "garbage", {}])
        self.assertEqual(s.runs_remaining(4.3), 4.3)


class RunsRemainingSettles(unittest.TestCase):
    """End-to-end: after one witnessed reset, per-run density stops shrinking
    and repeats the same shape every period - the actual bug fix, not just
    the unit's internals."""

    def test_density_is_flat_within_a_settled_period(self):
        budget = config.Budget(monthly_search_cap=240, runs_per_month=4.3)
        w = config.Watch(id="r", origin="BNE", destination="NRT",
                         days_from_now_min=90, days_from_now_max=300, trip_days=12)
        s = state()

        def plan(available, days_ago):
            """`days_ago` must keep DECREASING across the whole test - time
            has to move forward across every call, or _quota_log()'s sort
            scrambles which reading is actually the most recent."""
            s.data.setdefault("quota_log", []).append(
                {"at": _ago(days_ago), "left": int(available)})
            trial = [config.Watch(**{**w.__dict__})]
            config.plan_sampling(trial, budget, int(available),
                                 s.runs_remaining(budget.runs_per_month))
            return len(trial[0].probes())

        # First period: no reset witnessed yet - this WILL shrink, honestly.
        first = [plan(240 - i * 60, 56 - i * 7) for i in range(4)]   # days 56..35
        self.assertGreater(first[0], first[-1])

        # A reset lands (quota jumps back up), starting period two.
        second = [plan(240, 21), plan(174, 14), plan(108, 7)]        # days 21..7
        # Unlike period one, these should NOT keep shrinking run to run.
        self.assertAlmostEqual(second[0], second[-1], delta=2)
