"""The two alerting rules."""

import unittest

from dataclasses import replace

from flightbot.evaluate import _meets_stops, mark_drop
from flightbot.history import Baseline

from tests._factories import WATCH, quote, verdict


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


class DropRule(unittest.TestCase):
    """The second rule: has THIS date got cheaper than THIS date usually is?

    Not "is this the cheapest fare" - that answer is the same every week on a
    seasonal route and says nothing about whether anything moved.
    """

    # The factories date every quote 2027-05-<day>.
    def base(self, ratio=0.90):
        return Baseline(ratio=ratio,
                        typical={"2027-05-18": 1000.0, "2027-05-01": 700.0})

    def test_no_baseline_stays_quiet(self):
        """A route with too little history cannot judge anything yet."""
        vs = [verdict(500.0)]
        self.assertIsNone(mark_drop(vs, None))
        self.assertFalse(vs[0].is_deal)

    def test_a_fare_below_its_own_normal_is_a_deal(self):
        vs = [verdict(850.0)]                      # 850/1000 = 0.85, under 0.90
        got = mark_drop(vs, self.base())
        self.assertIs(got, vs[0])
        self.assertTrue(vs[0].is_deal)
        self.assertIn("down 15%", reasons(vs[0]))
        self.assertIn("AUD 1,000", reasons(vs[0]))

    def test_a_fare_at_its_normal_is_not(self):
        self.assertIsNone(mark_drop([verdict(1000.0)], self.base()))

    def test_matching_the_bar_exactly_is_not_beating_it(self):
        self.assertIsNone(mark_drop([verdict(900.0)], self.base()))

    def test_ranked_by_drop_not_by_price(self):
        """The cheapest seat is usually just the cheap season. The fare that
        MOVED is the news, even when it costs more."""
        cheap_but_flat = verdict(690.0, day=1)     # 690/700  = 0.986
        dearer_but_down = verdict(800.0, day=18)   # 800/1000 = 0.80
        got = mark_drop([cheap_but_flat, dearer_but_down], self.base())
        self.assertIs(got, dearer_but_down)
        self.assertEqual(got.quote.price, 800.0)
        self.assertFalse(cheap_but_flat.is_deal)

    def test_a_date_with_no_history_is_skipped(self):
        """A date that just entered the window has nothing to be judged
        against; guessing would make its first sighting look like a crash."""
        self.assertIsNone(mark_drop([verdict(1.0, day=9)], self.base()))

    def test_unpriced_rows_are_skipped(self):
        vs = [verdict(None), verdict(800.0)]
        self.assertIs(mark_drop(vs, self.base()), vs[1])

    def test_empty_run(self):
        self.assertIsNone(mark_drop([], self.base()))

    def test_both_rules_can_fire_on_one_fare(self):
        vs = [verdict(800.0, "low")]
        mark_drop(vs, self.base())
        self.assertTrue(vs[0].is_deal)
        self.assertIn("Google rates", reasons(vs[0]))
        self.assertIn("down 20%", reasons(vs[0]))


class MeetsStops(unittest.TestCase):
    """`watch.stops` follows SerpApi's convention - 0 any, 1 nonstop only,
    2 one-stop-or-fewer, 3 two-stops-or-fewer - so the ceiling on the fare's
    OWN stop count is one less than the watch value itself."""

    def test_any_accepts_every_stop_count(self):
        w = replace(WATCH, stops=0)
        for n in (0, 1, 2, 5):
            with self.subTest(stops=n):
                self.assertTrue(_meets_stops(quote(1.0, stops=n), w))

    def test_nonstop_only_rejects_anything_with_a_stop(self):
        w = replace(WATCH, stops=1)
        self.assertTrue(_meets_stops(quote(1.0, stops=0), w))
        self.assertFalse(_meets_stops(quote(1.0, stops=1), w))
        self.assertFalse(_meets_stops(quote(1.0, stops=2), w))

    def test_one_stop_or_fewer(self):
        w = replace(WATCH, stops=2)
        self.assertTrue(_meets_stops(quote(1.0, stops=1), w))
        self.assertFalse(_meets_stops(quote(1.0, stops=2), w))

    def test_unknown_stop_count_is_never_blocked(self):
        """A quote with no legs parsed (stops=None) must not be silently
        dropped just because the ceiling can't be checked against it."""
        w = replace(WATCH, stops=1)
        self.assertTrue(_meets_stops(quote(1.0, stops=None), w))


class StopsGateOnEvaluate(unittest.TestCase):
    """The search itself no longer filters by stops (see provider._params) -
    every fare is priced and journalled regardless. The preference now only
    decides whether it can trigger an alert."""

    def test_a_fare_outside_the_preference_is_not_a_deal(self):
        w = replace(WATCH, stops=1)   # nonstop only
        v = verdict(800.0, "low", watch=w, stops=1)   # the fare has a stop
        self.assertFalse(v.is_deal)

    def test_but_it_still_carries_a_reason_saying_why(self):
        w = replace(WATCH, stops=1)
        v = verdict(800.0, "low", watch=w, stops=2)
        self.assertIn("2 stops", reasons(v))
        self.assertIn("nonstop only", reasons(v))

    def test_a_matching_fare_still_alerts_normally(self):
        """Regression: a non-default but SATISFIED ceiling must not
        accidentally suppress a fare that actually meets it."""
        w = replace(WATCH, stops=2)   # 1 stop or fewer
        v = verdict(800.0, "low", watch=w, stops=1)
        self.assertTrue(v.is_deal)
        self.assertIn("Google rates", reasons(v))

    def test_discount_context_still_shown_even_when_outside_preference(self):
        w = replace(WATCH, stops=1)
        v = verdict(600.0, "typical", watch=w, stops=1)   # 50% under typical
        self.assertIsNotNone(v.discount_pct)
        self.assertFalse(v.is_deal)


class StopsGateOnMarkDrop(unittest.TestCase):
    """Same preference, applied to the drop-detection rule."""

    def base(self, ratio=0.90):
        return Baseline(ratio=ratio, typical={"2027-05-18": 1000.0})

    def test_a_bigger_drop_outside_the_preference_is_passed_over(self):
        w = replace(WATCH, stops=1)   # nonstop only
        matches = verdict(850.0, day=18, stops=0)          # small drop, nonstop
        bigger_but_violates = verdict(700.0, day=18, stops=1)  # huge drop, 1 stop
        got = mark_drop([matches, bigger_but_violates], self.base(), w)
        self.assertIs(got, matches)

    def test_nothing_meets_the_preference_stays_silent(self):
        w = replace(WATCH, stops=1)
        vs = [verdict(700.0, day=18, stops=2)]
        self.assertIsNone(mark_drop(vs, self.base(), w))

    def test_no_watch_passed_is_unfiltered_as_before(self):
        """Backward compatible: existing callers that never pass a watch keep
        seeing every candidate, matching behaviour before this gate existed."""
        vs = [verdict(700.0, day=18, stops=3)]
        got = mark_drop(vs, self.base())
        self.assertIs(got, vs[0])


if __name__ == "__main__":
    unittest.main()
