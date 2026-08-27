"""The control panel's server: what it lets you do, and what it refuses."""

import unittest

from flightbot import config, ui_server

BUDGET = config.Budget(monthly_search_cap=240, runs_per_month=4.3)


def routes(n):
    return [config.Watch(id=f"r{i}", origin="BNE", destination=f"X{i:02d}",
                         days_from_now_min=90, days_from_now_max=300, trip_days=12)
            for i in range(n)]


class AddingAnotherRoute(unittest.TestCase):
    """A fixed budget split between more routes never overspends - it thins the
    sample. The panel refuses at the point that stops being worth doing."""

    def test_the_first_route_is_always_allowed(self):
        self.assertTrue(ui_server._next_route([], BUDGET)["ok"])

    def test_a_handful_of_routes_is_fine(self):
        for n in (1, 2, 3, 5):
            with self.subTest(routes=n):
                self.assertTrue(ui_server._next_route(routes(n), BUDGET)["ok"])

    def test_refused_once_the_sample_would_be_too_thin(self):
        nxt = ui_server._next_route(routes(20), BUDGET)
        self.assertFalse(nxt["ok"])
        self.assertLess(nxt["per_route"], config.MIN_DATES_PER_ROUTE)

    def test_the_projection_never_exceeds_the_cap(self):
        """Because plan_sampling solves for the budget, not against it."""
        for n in range(1, 25):
            with self.subTest(routes=n):
                self.assertLessEqual(
                    ui_server._next_route(routes(n), BUDGET)["per_month"],
                    BUDGET.monthly_search_cap)

    def test_paused_routes_do_not_count(self):
        ws = routes(9)
        for w in ws[3:]:
            w.enabled = False
        self.assertTrue(ui_server._next_route(ws, BUDGET)["ok"])

    def test_the_real_watches_are_left_unplanned(self):
        """The trial runs plan_sampling; it must not touch the caller's list."""
        ws = routes(3)
        before = [w.step_days for w in ws]
        ui_server._next_route(ws, BUDGET)
        self.assertEqual([w.step_days for w in ws], before)

    def test_a_tiny_budget_is_refused_rather_than_planned(self):
        tiny = config.Budget(monthly_search_cap=20, runs_per_month=4.3)
        self.assertFalse(ui_server._next_route(routes(2), tiny)["ok"])


if __name__ == "__main__":
    unittest.main()
