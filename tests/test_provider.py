"""What actually gets sent to SerpApi."""

import unittest
from dataclasses import replace
from datetime import date

from flightbot.config import Probe
from flightbot.provider import _params

from tests._factories import WATCH


class SearchParams(unittest.TestCase):
    """`stops` is a decision-time filter now (see evaluate._meets_stops), not
    a search-time one - so the request must always ask for everything,
    regardless of what the route's own preference is set to."""

    PROBE = Probe(depart=date(2027, 5, 18), ret=date(2027, 5, 30))

    def test_stops_sent_is_always_zero(self):
        for pref in (0, 1, 2, 3):
            with self.subTest(watch_stops=pref):
                w = replace(WATCH, stops=pref)
                self.assertEqual(_params(w, self.PROBE)["stops"], 0)

    def test_the_route_still_carries_its_own_preference(self):
        """Confirms the field isn't erased - only kept out of the request."""
        w = replace(WATCH, stops=1)
        self.assertEqual(w.stops, 1)
        self.assertEqual(_params(w, self.PROBE)["stops"], 0)


if __name__ == "__main__":
    unittest.main()
