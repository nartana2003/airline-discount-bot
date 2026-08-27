"""Run-to-run memory: what we last saw, what we last said, what we've spent.

Two jobs. First, suppress repeat alerts so a fare that stays cheap for a week
doesn't email you seven times. Second, track SerpApi search credits against the
monthly cap so a runaway loop can't burn the free tier in one go.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_QUOTA_PERIOD_DAYS, DIGEST_INTERVAL_DAYS, STATE_PATH
from .evaluate import Verdict


def _now() -> datetime:
    return datetime.now(timezone.utc)


MAX_QUOTA_LOG = 60


@dataclass
class State:
    path: Path
    data: dict

    @classmethod
    def load(cls, path: Path | None = None) -> "State":
        p = path or STATE_PATH
        if p.exists():
            try:
                return cls(path=p, data=json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass  # corrupt state is not worth crashing over; start fresh
        return cls(path=p, data={"alerts": {}})

    def save(self) -> None:
        # `spend` was a local count of searches used per calendar month. It is
        # no longer read: SerpApi's own figure is the budget authority, and a
        # second number that could only drift out of step was worse than none.
        # Dropped on save so the file cleans itself up on the next run.
        self.data.pop("spend", None)
        # "month" keyed the old calendar-month digest schedule; the interval
        # one reads only "at". Dropped on save so the file cleans itself up.
        self.data.get("digest", {}).pop("month", None)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")

    # --- periodic "still alive" digest -----------------------------------
    # A run that finds nothing looks exactly like a broken key, a dead SMTP
    # password, or a workflow that stopped firing. Every DIGEST_INTERVAL_DAYS
    # the bot says so out loud, which turns silence into a fact rather than a
    # guess.
    #
    # This was keyed to the calendar month, which had two faults: a digest sent
    # on the 1st and the next on the 31st are 30 days apart while one sent on
    # the 31st and the next on the 1st are one, so the window it reported on
    # was never a fixed length; and a quiet route could go five weeks without a
    # word. An elapsed interval fixes both.

    def digest_due(self) -> bool:
        at = self.last_digest_at()
        if not at:
            return True          # never sent one - say hello
        try:
            last = datetime.fromisoformat(at)
        except ValueError:
            return True          # unreadable stamp: sending twice beats never
        # Half a day of slack. Runs land on a weekly cron, so the gap between
        # the second run and the last digest is almost exactly the interval -
        # a few seconds either way would otherwise push it out a whole week.
        return _now() - last >= timedelta(days=DIGEST_INTERVAL_DAYS, hours=-12)

    def last_digest_at(self) -> str | None:
        return self.data.setdefault("digest", {}).get("at")

    def record_digest(self) -> None:
        self.data.setdefault("digest", {})["at"] = _now().isoformat()

    # --- last observed search quota, and how many runs remain -------------
    # The run asks SerpApi how many searches are left and plans from it. The
    # control panel cannot: it has no API key and no business making network
    # calls to render a form. Recording what the run saw lets the panel show
    # the real figure from a file it already reads, dated so its staleness is
    # visible rather than assumed.
    #
    # A short history of these readings also answers a question a single
    # reading cannot: how many runs are left before the quota resets. Dividing
    # remaining quota by `runs_per_month` - the AVERAGE cadence across a full
    # billing period - looked like it used the live figure, but it quietly
    # made every run sample more sparsely than the one before it. Remaining
    # quota shrinks through a period from ordinary spending; the average
    # cadence never does, so per-run allowance kept shrinking with it, run
    # after run, all period long, and only snapped back at reset. Measured
    # live: 49, 40, 33, 24, 20, 16, 12 dates in seven honestly-spent runs. That
    # also kept re-cutting the anchored probe lattice - the whole point of
    # which was for the same dates to keep coming back.

    def record_quota(self, left: int | None) -> None:
        """Remember SerpApi's remaining-search count, and log it for
        runs_remaining(). None means it wasn't asked - a demo run."""
        if left is None:
            return
        at = _now().isoformat()
        self.data["quota"] = {"left": int(left), "at": at}
        log = self.data.setdefault("quota_log", [])
        log.append({"left": int(left), "at": at})
        del log[:-MAX_QUOTA_LOG]

    def quota(self) -> dict | None:
        """{"left": int, "at": iso} from the last run that asked, or None."""
        q = self.data.get("quota")
        if not isinstance(q, dict) or not isinstance(q.get("left"), int):
            return None
        return q

    def _quota_log(self) -> list[tuple[datetime, int]]:
        """The recorded (when, remaining) readings, oldest first, parsed and
        sorted - malformed entries dropped rather than raising."""
        out = []
        for e in self.data.get("quota_log", []):
            if not isinstance(e, dict) or not isinstance(e.get("left"), int):
                continue
            try:
                out.append((datetime.fromisoformat(e["at"]), e["left"]))
            except (KeyError, ValueError, TypeError):
                continue
        out.sort(key=lambda pair: pair[0])
        return out

    def runs_remaining(self, runs_per_month: float,
                       period_days: float = DEFAULT_QUOTA_PERIOD_DAYS) -> float:
        """How many runs are likely left before SerpApi's quota resets.

        `runs_per_month` alone answers "on average, how often does this run" -
        it says nothing about where THIS billing period currently stands. That
        needs a period boundary, and nothing hands one over: SerpApi's account
        endpoint is asked for a remaining-search count, not a renewal date, so
        the boundary has to be inferred from what the count itself does.

        A reset shows up as the only thing that can make it go UP: every run
        spends credits, so two consecutive readings where the second is higher
        than the first can only mean a period rolled over in between. The most
        recent such jump is taken as this period's start, and everything
        recorded since is a run already spent from it.

        Until the FIRST such jump has actually been witnessed, there is no
        boundary to reason from at all, and guessing one - "the period must
        have started whenever we happened to start watching" - was tried and
        measured: it treats every reading on file so far as spent from a
        period we have no evidence even began there, so it depletes the
        estimate faster than reality and reproduces the very shrink this
        method exists to fix, just for one period instead of every one. The
        plain average is the more honest answer until a reset actually proves
        where a boundary sits - the same caution the rest of this codebase
        applies to a route with no price history yet, or a date sampled only
        once: silence, not a guess, until there is real evidence.

        The period length itself starts as a guess (`period_days`, an average
        month) and self-corrects once TWO resets have been witnessed - their
        gap is real data about this account's billing cycle, not something
        that needs to be typed in.
        """
        log = self._quota_log()
        resets = [at for (at, left), (prev_at, prev_left)
                 in zip(log[1:], log[:-1]) if left > prev_left]
        if not resets:
            return runs_per_month
        period_start = resets[-1]

        if len(resets) >= 2:
            gaps = [(b - a).total_seconds() / 86400 for a, b in zip(resets, resets[1:])]
            period_days = sum(gaps) / len(gaps)

        # The gap between runs comes from the PRIOR, not from `period_days` -
        # deriving it from `period_days` and then dividing `period_days` by
        # that same derived figure cancels out to `runs_per_month` regardless
        # of what was actually measured, silently undoing the whole point of
        # learning a real period length.
        days_between_runs = DEFAULT_QUOTA_PERIOD_DAYS / max(runs_per_month, 0.1)
        total_this_period = max(period_days / days_between_runs, 1.0)
        runs_so_far = sum(1 for at, _ in log if at >= period_start)

        # Never below one run's worth: an extra hand-triggered run should not
        # be able to starve the next scheduled one down to nothing.
        return max(total_this_period - runs_so_far, 1.0)

    # --- alert de-duplication -------------------------------------------

    def should_notify(self, verdict: Verdict, cooldown_hours: float | None,
                      renotify_drop_pct: float) -> bool:
        """True if this deal is new, or has dropped enough to be worth repeating.

        A date pair you have already been emailed about stays quiet forever by
        default (`cooldown_hours=None`). The one thing that gets through is a
        genuinely better price - being told twice about the same fare is noise,
        but a further drop is new information.
        """
        if not verdict.is_deal or verdict.quote.price is None:
            return False

        key = f"{verdict.quote.watch_id}:{verdict.quote.probe.key}"
        previous = self.data.setdefault("alerts", {}).get(key)
        if not previous:
            return True

        last_price = previous.get("price")
        last_at = previous.get("at")
        if not isinstance(last_price, (int, float)) or not last_at:
            return True

        # A further drop of this size always re-alerts, cooldown or not.
        drop_pct = (last_price - verdict.quote.price) / last_price * 100.0
        if drop_pct >= renotify_drop_pct:
            return True

        if cooldown_hours is None:
            return False  # already alerted, and no further drop - stay quiet

        elapsed_hours = (_now() - datetime.fromisoformat(last_at)).total_seconds() / 3600.0
        return elapsed_hours >= cooldown_hours

    def record_alert(self, verdict: Verdict) -> None:
        key = f"{verdict.quote.watch_id}:{verdict.quote.probe.key}"
        self.data.setdefault("alerts", {})[key] = {
            "price": verdict.quote.price,
            "price_level": verdict.quote.price_level,
            "at": _now().isoformat(),
        }
