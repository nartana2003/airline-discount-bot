"""Settings loaded from the environment."""

import os
import unittest
from contextlib import contextmanager
from datetime import date, timedelta

from flightbot import cli, config


@contextmanager
def env(**values):
    """Set/clear vars for the duration of a test, then put them all back."""
    before = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


BASE = dict(SMTP_USER="me@example.com", SMTP_PASSWORD="pw", ALERT_TO="me@example.com")


class EmailSettingsFromEnv(unittest.TestCase):
    def test_unset_host_and_port_use_defaults(self):
        with env(SMTP_HOST=None, SMTP_PORT=None, **BASE):
            s = config.EmailSettings.from_env()
        self.assertEqual((s.host, s.port), ("smtp.gmail.com", 465))

    def test_empty_host_and_port_use_defaults_too(self):
        """An unset GitHub secret arrives as "", not as absent - int("") used to crash."""
        with env(SMTP_HOST="", SMTP_PORT="", **BASE):
            s = config.EmailSettings.from_env()
        self.assertEqual((s.host, s.port), ("smtp.gmail.com", 465))

    def test_real_values_win(self):
        with env(SMTP_HOST="mail.example.com", SMTP_PORT="587", **BASE):
            s = config.EmailSettings.from_env()
        self.assertEqual((s.host, s.port), ("mail.example.com", 587))

    def test_configured_needs_user_password_and_recipient(self):
        with env(SMTP_HOST=None, SMTP_PORT=None, SMTP_USER="u",
                 SMTP_PASSWORD="", ALERT_TO="t"):
            self.assertFalse(config.EmailSettings.from_env().configured)
        with env(SMTP_HOST=None, SMTP_PORT=None, **BASE):
            self.assertTrue(config.EmailSettings.from_env().configured)

    def test_alert_to_defaults_to_the_sender(self):
        with env(SMTP_HOST=None, SMTP_PORT=None, SMTP_USER="me@example.com",
                 SMTP_PASSWORD="pw", ALERT_TO=None):
            self.assertEqual(config.EmailSettings.from_env().to, "me@example.com")


if __name__ == "__main__":
    unittest.main()


class AnchoredProbes(unittest.TestCase):
    """Departure dates come from a fixed lattice, not from counting off today.

    Before this, weekly runs sampled 26 dates and did not revisit one of them
    for nine consecutive runs - which left de-duplication with nothing to match
    and the price journal with no repeated observations to compare.
    """

    STEP = 9

    def watch(self, **kw):
        return config.Watch(id="x", origin="BNE", destination="NRT",
                            step_days=self.STEP, days_from_now_min=60,
                            days_from_now_max=300, trip_days=12, **kw)

    def departs(self, today):
        return {p.depart for p in self.watch().probes(today)}

    def test_weekly_runs_reprice_the_same_dates(self):
        a = self.departs(date(2026, 8, 27))
        b = self.departs(date(2026, 9, 3))
        # The window slides 7 days, so a couple of dates leave one end and a
        # couple join the other. Everything in between must be the SAME date.
        self.assertGreaterEqual(len(a & b), len(a) - 2)

    def test_every_departure_sits_on_the_lattice(self):
        for offset in range(14):
            probes = self.watch().probes(date(2026, 8, 27) + timedelta(days=offset))
            self.assertTrue(probes)
            for p in probes:
                self.assertEqual(p.depart.toordinal() % self.STEP, 0)

    def test_spacing_is_still_the_step(self):
        departs = sorted(self.departs(date(2026, 8, 27)))
        gaps = {(b - a).days for a, b in zip(departs, departs[1:])}
        self.assertEqual(gaps, {self.STEP})

    def test_stays_inside_the_window(self):
        today = date(2026, 8, 27)
        for p in self.watch().probes(today):
            self.assertGreaterEqual((p.depart - today).days, 60)
            # The return leg has to be on sale too, so the horizon binds it.
            self.assertLessEqual((p.ret - today).days, config.MAX_HORIZON_DAYS)

    def test_cost_per_run_stays_stable(self):
        """Density is budgeted, so an anchored grid must not change the bill."""
        counts = {len(self.watch().probes(date(2026, 8, 27) + timedelta(days=n)))
                  for n in range(30)}
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_a_window_with_no_lattice_point_is_empty_not_broken(self):
        w = config.Watch(id="x", origin="BNE", destination="NRT",
                         step_days=400, days_from_now_min=60,
                         days_from_now_max=61, trip_days=12)
        self.assertIsInstance(w.probes(date(2026, 8, 27)), list)


class PlanFromLiveQuota(unittest.TestCase):
    """Density comes from SerpApi's real remaining count when it is known, and
    from the declared cap only when there is no account to ask."""

    def watches(self, n=2):
        return [config.Watch(id=f"r{i}", origin="BNE", destination="NRT",
                             days_from_now_min=90, days_from_now_max=300,
                             trip_days=12)
                for i in range(n)]

    BUDGET = config.Budget(monthly_search_cap=240, runs_per_month=4.3)

    def probes(self, available):
        ws = self.watches()
        config.plan_sampling(ws, self.BUDGET, available)
        return sum(len(w.probes(date(2026, 8, 27))) for w in ws)

    def test_falls_back_to_the_cap_when_quota_is_unknown(self):
        ws = self.watches()
        config.plan_sampling(ws, self.BUDGET, None)
        by_cap = [w.step_days for w in ws]
        config.plan_sampling(ws, self.BUDGET, 240)
        self.assertEqual([w.step_days for w in ws], by_cap)

    def test_unspent_quota_samples_denser(self):
        """A month with runs missed leaves credits over; the runs that remain
        should use them rather than let them expire."""
        self.assertGreater(self.probes(480), self.probes(240))

    def test_spent_quota_samples_sparser(self):
        """And a half-spent month plans a run the account can actually pay for,
        instead of one it cannot."""
        self.assertLess(self.probes(60), self.probes(240))

    def test_never_plans_more_than_the_quota(self):
        for available in (0, 1, 5, 20, 100, 240, 1000):
            with self.subTest(available=available):
                self.assertLessEqual(self.probes(available), max(available, 2))

    def test_negative_quota_is_treated_as_none_left(self):
        ws = self.watches()
        config.plan_sampling(ws, self.BUDGET, -5)   # shouldn't raise
        self.assertTrue(all(w.step_days >= 1 for w in ws))


class FitToQuota(unittest.TestCase):
    """The corner the planner can't solve: even one probe per route is too many."""

    def test_trims_to_fit(self):
        plan = {"a": list(range(10)), "b": list(range(10))}
        dropped = cli._fit(plan, 6)
        self.assertEqual(dropped, 14)
        self.assertEqual(sum(len(p) for p in plan.values()), 6)

    def test_takes_from_the_longest_so_routes_stay_balanced(self):
        plan = {"a": list(range(10)), "b": list(range(2))}
        cli._fit(plan, 6)
        self.assertEqual(sorted(len(p) for p in plan.values()), [2, 4])

    def test_nothing_to_drop_terminates(self):
        plan = {"a": [], "b": []}
        self.assertEqual(cli._fit(plan, 0), 0)

    def test_already_fits_is_a_no_op(self):
        plan = {"a": [1, 2], "b": [3]}
        self.assertEqual(cli._fit(plan, 99), 0)
        self.assertEqual(sum(len(p) for p in plan.values()), 3)


class PlansForRoutesActuallyRun(unittest.TestCase):
    """`--watch` runs one route, so the others' shares must not be reserved."""

    BUDGET = config.Budget(monthly_search_cap=240, runs_per_month=4.3)

    def routes(self, n):
        return [config.Watch(id=f"r{i}", origin="BNE", destination="NRT",
                             days_from_now_min=90, days_from_now_max=300,
                             trip_days=12)
                for i in range(n)]

    def test_a_single_route_samples_denser_than_its_share_of_two(self):
        pair = self.routes(2)
        config.plan_sampling(pair, self.BUDGET)
        shared_step = pair[0].step_days

        alone = self.routes(1)
        config.plan_sampling(alone, self.BUDGET)
        self.assertLess(alone[0].step_days, shared_step)

    def test_a_single_route_uses_the_whole_run_allowance(self):
        """Roughly double what it got when it was reserving a share for the
        other route, and still inside the allowance. Not exactly the allowance:
        the step is a whole number of days, so the fit is never perfect."""
        pair = self.routes(2)
        config.plan_sampling(pair, self.BUDGET)
        shared = len(pair[0].probes(date(2026, 8, 27)))

        alone = self.routes(1)
        config.plan_sampling(alone, self.BUDGET)
        n = len(alone[0].probes(date(2026, 8, 27)))

        per_run = self.BUDGET.monthly_search_cap / self.BUDGET.runs_per_month
        self.assertGreater(n, shared * 1.8)
        self.assertLessEqual(n, per_run)

    def test_disabled_routes_never_take_a_share(self):
        ws = self.routes(2)
        ws[1].enabled = False
        config.plan_sampling(ws, self.BUDGET)
        alone = self.routes(1)
        config.plan_sampling(alone, self.BUDGET)
        self.assertEqual(ws[0].step_days, alone[0].step_days)
