"""The declared run cadence must match the cron that actually triggers runs.

`runs_per_month` in watches.json divides the search quota into a per-run
allowance. Nothing at runtime can check it - the bot cannot see its own
schedule - so it is checked here instead. Change the cron without changing the
number and this fails, which is the whole point: the figure used to be a guess
that nothing enforced.
"""

from __future__ import annotations

import calendar
import re
import unittest
from datetime import date
from pathlib import Path

from flightbot import config

WORKFLOW = config.ROOT / ".github" / "workflows" / "check-flights.yml"

# A non-leap year: cron has no notion of leap days, and averaging over one
# ordinary year is the same convention "runs per month" already implies.
YEAR = 2027


def _field(spec: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field to the set of values it matches.

    Supports the four forms this project could plausibly use: `*`, a list,
    a range, and a step. Anything else raises rather than silently matching
    nothing - a test that quietly passes on an unparsed cron is worse than no
    test.
    """
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, _, raw = part.partition("/")
            step = int(raw)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(f"unsupported cron field: {spec!r}")
        out.update(range(start, end + 1, step))
    return {v for v in out if lo <= v <= hi}


def runs_per_month(cron: str) -> float:
    """Average firings per month for a five-field cron expression."""
    minute, hour, dom, month, dow = cron.split()
    minutes, hours = _field(minute, 0, 59), _field(hour, 0, 23)
    doms, months = _field(dom, 1, 31), _field(month, 1, 12)
    dows = {d % 7 for d in _field(dow, 0, 7)}          # cron allows 7 for Sunday

    # Standard cron: when BOTH day fields are restricted a day matches if
    # either does; when only one is, that one decides.
    dom_any, dow_any = dom.strip() == "*", dow.strip() == "*"

    days = 0
    for m in months:
        for d in range(1, calendar.monthrange(YEAR, m)[1] + 1):
            # date.weekday() is Mon=0; cron is Sun=0.
            wd = (date(YEAR, m, d).weekday() + 1) % 7
            hit_dom, hit_dow = d in doms, wd in dows
            if (hit_dom or hit_dow) if not (dom_any or dow_any) else \
               (hit_dom if dow_any else hit_dow) if not (dom_any and dow_any) else True:
                days += 1

    return days * len(hours) * len(minutes) / 12.0


class CronExpansion(unittest.TestCase):
    """The parser first - a wrong parser would make the real check meaningless."""

    def test_weekly(self):
        self.assertAlmostEqual(runs_per_month("0 21 * * 0"), 52 / 12.0, places=2)

    def test_daily(self):
        self.assertAlmostEqual(runs_per_month("0 21 * * *"), 365 / 12.0, places=2)

    def test_twice_daily(self):
        self.assertAlmostEqual(runs_per_month("0 0,12 * * *"), 730 / 12.0, places=2)

    def test_every_other_hour(self):
        self.assertAlmostEqual(runs_per_month("0 */2 * * *"), 365 * 12 / 12.0, places=2)

    def test_monthly_by_day_of_month(self):
        self.assertAlmostEqual(runs_per_month("0 21 1 * *"), 1.0, places=2)

    def test_unparseable_field_raises(self):
        with self.assertRaises(ValueError):
            runs_per_month("0 21 * * MON")


class DeclaredCadenceMatchesTheWorkflow(unittest.TestCase):
    def crons(self) -> list[str]:
        text = WORKFLOW.read_text(encoding="utf-8")
        return re.findall(r'^\s*-\s*cron:\s*["\']([^"\']+)["\']', text, re.M)

    def test_workflow_declares_exactly_one_schedule(self):
        """Two schedules would need summing, and the check below assumes one."""
        self.assertEqual(len(self.crons()), 1, "update this test if that changed")

    def test_watches_json_matches_the_cron(self):
        _, budget = config.load_watchlist_data(
            __import__("json").loads(config.WATCHES_PATH.read_text(encoding="utf-8")))
        actual = runs_per_month(self.crons()[0])
        self.assertAlmostEqual(
            budget.runs_per_month, actual, delta=0.1,
            msg=(f"watches.json says runs_per_month={budget.runs_per_month}, but "
                 f"{WORKFLOW.name} fires {actual:.2f} times a month. The search "
                 f"budget is divided by that number, so they have to agree."))


if __name__ == "__main__":
    unittest.main()
