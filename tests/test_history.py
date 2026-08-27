"""The price journal: baselines, the digest rollup, and run counting."""

import json
import tempfile
import unittest
from pathlib import Path

from flightbot import history

from tests._factories import row, verdict

# baselines() ignores any route with less usable history than this, so tests
# that want a bar at all have to clear it.
ENOUGH = history.MIN_FLOOR_OBSERVATIONS


class Baselines(unittest.TestCase):
    """Each date is measured against its own past prices, so the bar reflects
    movement rather than which month it happens to be."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "prices.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def repeated(self, dates=15, prices=(1000.0, 900.0), watch="bne-nrt", **kw):
        """Each date seen twice, at the same two prices - so every date's median
        is 950 and the ratios are a tidy 1.053 / 0.947."""
        return [row(p, watch, depart=f"2027-05-{i + 1:02d}", **kw)
                for i in range(dates) for p in prices]

    def test_missing_file(self):
        self.assertEqual(history.baselines(self.path), {})

    def test_a_route_with_little_history_has_no_baseline(self):
        self.write(self.repeated(dates=2))
        self.assertEqual(history.baselines(self.path), {})

    def test_dates_seen_only_once_contribute_nothing(self):
        """One reading IS its own median, so it would score exactly 1.0 and drag
        the bar towards 'any drop at all counts'."""
        self.write([row(900.0, "bne-nrt", depart=f"2027-05-{i + 1:02d}")
                    for i in range(ENOUGH + 10)])
        self.assertEqual(history.baselines(self.path), {})

    def test_typical_is_the_median_of_that_date(self):
        self.write(self.repeated())
        b = history.baselines(self.path)[("bne-nrt", 12)]
        self.assertEqual(b.typical["2027-05-01"], 950.0)
        self.assertEqual(len(b.typical), 15)

    def test_bar_comes_from_the_spread_of_ratios(self):
        self.write(self.repeated())
        b = history.baselines(self.path)[("bne-nrt", 12)]
        self.assertAlmostEqual(b.ratio, 900 / 950, places=3)

    def test_score_is_a_fraction_of_that_date_s_normal(self):
        self.write(self.repeated())
        b = history.baselines(self.path)[("bne-nrt", 12)]
        self.assertAlmostEqual(b.score(475.0, "2027-05-01"), 0.5)
        self.assertIsNone(b.score(475.0, "2099-01-01"))

    def test_season_no_longer_decides(self):
        """The old pooled rule made cheap dates permanently qualify and dear
        ones permanently silent. On the same data both are now judged on how
        far they have moved from their own normal."""
        cheap = [row(p, "r", depart=f"2027-04-{i + 1:02d}") for i in range(8)
                 for p in (600.0, 620.0)]                     # median 610
        dear = [row(p, "r", depart=f"2027-12-{i + 1:02d}") for i in range(8)
                for p in (1400.0, 1420.0)]                    # median 1410
        self.write(cheap + dear)
        b = history.baselines(self.path)[("r", 12)]
        # A dear December fare that has fallen scores better than a cheap
        # April fare sitting exactly where it always sits.
        self.assertLess(b.score(1000.0, "2027-12-01"), b.score(610.0, "2027-04-01"))

    def test_trip_lengths_are_kept_apart(self):
        self.write(self.repeated() + self.repeated(trip_days=7))
        got = history.baselines(self.path)
        self.assertEqual(sorted(got), [("bne-nrt", 7), ("bne-nrt", 12)])

    def test_routes_are_kept_apart(self):
        self.write(self.repeated() + self.repeated(watch="bne-bom"))
        self.assertEqual(sorted(history.baselines(self.path)),
                         [("bne-bom", 12), ("bne-nrt", 12)])

    def test_unpriced_rows_ignored(self):
        self.write(self.repeated() + [row(None, "bne-nrt", depart="2027-05-01")])
        b = history.baselines(self.path)[("bne-nrt", 12)]
        self.assertEqual(b.typical["2027-05-01"], 950.0)

    def test_rows_without_a_trip_length_are_skipped(self):
        self.write(self.repeated(trip_days=None))
        self.assertEqual(history.baselines(self.path), {})

    def test_corrupt_line_does_not_lose_the_file(self):
        good = "".join(json.dumps(r) + "\n" for r in self.repeated())
        self.path.write_text('{"broken\n' + good, encoding="utf-8")
        self.assertIn(("bne-nrt", 12), history.baselines(self.path))


class Percentile(unittest.TestCase):
    def test_single_observation(self):
        self.assertEqual(history._percentile([42.0], 10), 42.0)

    def test_interpolates_between_neighbours(self):
        self.assertAlmostEqual(history._percentile([0.0, 10.0], 50), 5.0)

    def test_endpoints(self):
        xs = [1.0, 2.0, 3.0]
        self.assertEqual(history._percentile(xs, 0), 1.0)
        self.assertEqual(history._percentile(xs, 100), 3.0)


class DigestSpread(unittest.TestCase):
    """How far under a typical sampled date the cheapest one is - the digest's
    "when should I fly" line, computed from one run with no history at all."""

    AT = "2026-09-01T00:00:00+00:00"

    def rows(self, prices):
        return [row(p, "bne-nrt", at=self.AT, depart=f"2027-05-{10 + i:02d}")
                for i, p in enumerate(prices)]

    def test_reports_distance_under_the_median_date(self):
        d = history.digest(self.rows(
            [700, 950, 980, 1000, 1050, 1100, 1000, 990, 1010, 1020]))
        sp = d["spread"]["bne-nrt"]
        self.assertEqual(sp["dates"], 10)
        self.assertEqual(sp["cheapest"], 700)
        self.assertEqual(sp["median"], 1000)      # median, so the 1100 can't skew it
        self.assertEqual(sp["under_pct"], 30)
        self.assertEqual(sp["currency"], "AUD")

    def test_too_few_dates_to_have_a_typical_one(self):
        d = history.digest(self.rows([700, 1000, 1100]))
        self.assertNotIn("bne-nrt", d["spread"])

    def test_only_the_newest_run_counts(self):
        old = [row(50, "bne-nrt", at="2026-08-01T00:00:00+00:00") for _ in range(6)]
        d = history.digest(old + self.rows([700, 950, 1000, 1050, 1100, 1000]))
        self.assertEqual(d["spread"]["bne-nrt"]["cheapest"], 700)


class RunStamp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "prices.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_one_run_is_one_timestamp_across_routes(self):
        """record() is called once per route; a run must still count as one run."""
        at = history.run_stamp()
        history.record([verdict(900.0)], self.path, at)
        history.record([verdict(800.0)], self.path, at)
        self.assertEqual(history.digest(list(history.read(self.path)))["runs"], 1)

    def test_separate_runs_still_count_separately(self):
        history.record([verdict(900.0)], self.path, "2026-08-01T00:00:00+00:00")
        history.record([verdict(800.0)], self.path, "2026-08-08T00:00:00+00:00")
        self.assertEqual(history.digest(list(history.read(self.path)))["runs"], 2)


class Digest(unittest.TestCase):
    def test_empty(self):
        d = history.digest([])
        self.assertEqual(d["runs"], 0)
        self.assertEqual(d["routes"], [])
        self.assertEqual(d["current"], {})

    def test_current_is_the_newest_run_per_route(self):
        rows = [
            row(717, "bne-nrt", at="2026-08-01T00:00:00+00:00"),
            row(1034, "bne-nrt", at="2026-08-08T00:00:00+00:00"),
            row(1144, "bne-bom", at="2026-08-08T00:00:00+00:00"),
        ]
        d = history.digest(rows)
        self.assertEqual(d["current"]["bne-nrt"]["price"], 1034)
        self.assertEqual(d["current"]["bne-bom"]["price"], 1144)

    def test_every_route_gets_a_current_price(self):
        """A single global 'latest timestamp' used to leave routes without one."""
        rows = [row(900, "bne-nrt", at="2026-08-08T00:00:00+00:00"),
                row(1144, "bne-bom", at="2026-08-08T00:00:01+00:00")]
        self.assertEqual(set(history.digest(rows)["current"]), {"bne-nrt", "bne-bom"})

    def test_route_absent_from_the_newest_run(self):
        rows = [row(965, "bne-hnd", at="2026-07-01T00:00:00+00:00"),
                row(900, "bne-nrt", at="2026-08-08T00:00:00+00:00")]
        d = history.digest(rows)
        self.assertIn("bne-hnd", d["current"])   # its own newest, not the global one
        self.assertEqual(d["current"]["bne-hnd"]["price"], 965)

    def test_routes_sorted_by_the_headline_price(self):
        rows = [row(100, "cheap-low", at="2026-08-01T00:00:00+00:00"),
                row(999, "cheap-low", at="2026-08-08T00:00:00+00:00"),
                row(500, "steady", at="2026-08-08T00:00:00+00:00")]
        d = history.digest(rows)
        self.assertEqual([r["watch"] for r in d["routes"]], ["steady", "cheap-low"])

    def test_counts(self):
        rows = [row(900), row(None), row(800, deal=True)]
        d = history.digest(rows)
        self.assertEqual((d["searches"], d["priced"], d["empty"], d["deals"]), (3, 2, 1, 1))


if __name__ == "__main__":
    unittest.main()
