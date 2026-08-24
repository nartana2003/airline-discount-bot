"""The two alerting rules."""

import unittest

from flightbot.evaluate import mark_record

from tests._factories import WATCH, verdict


def reasons(v):
    return " | ".join(v.reasons)


class GoogleRule(unittest.TestCase):
    def test_low_is_a_deal(self):
        v = verdict(1400.0, "low")
        self.assertTrue(v.is_deal)
        self.assertIn("low", reasons(v))

    def test_typical_is_not(self):
        self.assertFalse(verdict(600.0, "typical").is_deal)

    def test_unrated_is_not(self):
        """A very cheap fare Google has no opinion on still fails rule one."""
        self.assertFalse(verdict(1.0, None).is_deal)

    def test_no_fare_returned(self):
        v = verdict(None, "low")
        self.assertFalse(v.is_deal)
        self.assertIn("no fares returned", reasons(v))

    def test_discount_is_shown_but_does_not_decide(self):
        v = verdict(600.0, "typical")          # 50% under the 1200 midpoint
        self.assertIsNotNone(v.discount_pct)
        self.assertFalse(v.is_deal)


class RecordRule(unittest.TestCase):
    def test_no_floor_cannot_set_a_record(self):
        """First run on a new route: nothing to beat, so silence."""
        vs = [verdict(950.0), verdict(700.0, day=1)]
        self.assertIsNone(mark_record(vs, None))
        self.assertEqual([v.is_deal for v in vs], [False, False])

    def test_cheapest_beating_the_floor_is_a_deal(self):
        vs = [verdict(950.0), verdict(700.0, day=1)]
        got = mark_record(vs, 717.0)
        self.assertIs(got, vs[1])
        self.assertTrue(vs[1].is_deal)
        self.assertIn("previous best was AUD 717", reasons(vs[1]))

    def test_only_the_cheapest_claims_the_record(self):
        """The bug this was written for: four fares under the old floor, one record."""
        vs = [verdict(1310.0), verdict(827.0, day=1), verdict(1015.0, day=2)]
        mark_record(vs, 1320.0)
        claimed = [v.quote.price for v in vs if "cheapest ever" in reasons(v)]
        self.assertEqual(claimed, [827.0])

    def test_matching_the_floor_is_not_beating_it(self):
        vs = [verdict(717.0)]
        self.assertIsNone(mark_record(vs, 717.0))
        self.assertFalse(vs[0].is_deal)

    def test_nothing_under_the_floor(self):
        vs = [verdict(950.0), verdict(800.0, day=1)]
        self.assertIsNone(mark_record(vs, 717.0))

    def test_unpriced_rows_are_skipped(self):
        vs = [verdict(None), verdict(650.0, day=1)]
        got = mark_record(vs, 717.0)
        self.assertIs(got, vs[1])

    def test_all_unpriced(self):
        self.assertIsNone(mark_record([verdict(None)], 717.0))

    def test_both_rules_can_fire_on_one_fare(self):
        vs = [verdict(600.0, "low")]
        mark_record(vs, 717.0)
        self.assertTrue(vs[0].is_deal)
        self.assertIn("Google rates", reasons(vs[0]))
        self.assertIn("cheapest ever", reasons(vs[0]))

    def test_empty_run(self):
        self.assertIsNone(mark_record([], 717.0))


if __name__ == "__main__":
    unittest.main()
