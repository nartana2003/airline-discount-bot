"""The price journal: floors, the digest rollup, and run counting."""

import tempfile
import unittest
from pathlib import Path

from flightbot import history

from tests._factories import row, verdict


class Floors(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "prices.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, rows):
        import json
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_missing_file(self):
        self.assertEqual(history.floors(self.path), {})

    def test_minimum_per_route(self):
        self.write([row(900, "bne-nrt"), row(717, "bne-nrt"), row(1144, "bne-bom")])
        self.assertEqual(history.floors(self.path), {"bne-nrt": 717.0, "bne-bom": 1144.0})

    def test_unpriced_rows_ignored(self):
        self.write([row(None, "bne-nrt"), row(900, "bne-nrt")])
        self.assertEqual(history.floors(self.path), {"bne-nrt": 900.0})

    def test_corrupt_line_does_not_lose_the_file(self):
        self.path.write_text('{"broken\n{"at":"x","watch":"a","price":10}\n', encoding="utf-8")
        self.assertEqual(history.floors(self.path), {"a": 10.0})


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
