"""Append-only price journal.

`state.json` remembers what you were *told about*; this remembers what was
*seen*. Every quote a run fetches lands here, alerted or not.

The data is already paid for - a search costs its credit whether or not the
fare turns out to be interesting - and discarded it can never answer the
questions that matter later: was that actually cheap, when is this route
reliably cheapest, and is Google's "low" verdict any good on my own numbers.
None of that is answerable retrospectively, which is why this starts now.

One JSON object per line: appends without rewriting the file, stays readable
in a diff, and reads back a line at a time rather than all at once.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import FLOOR_PERCENTILE, MIN_FLOOR_OBSERVATIONS, ROOT
from .evaluate import Verdict

HISTORY_PATH = ROOT / "prices.jsonl"
# Demo prices are invented. They must never mix into the real record.
DEMO_HISTORY_PATH = ROOT / "prices.demo.jsonl"


def _row(verdict: Verdict, seen_at: str) -> dict:
    """One quote, flattened. Keys stay short and stable - this file is meant
    to still be readable by something written years from now."""
    q = verdict.quote
    first = q.legs[0] if q.legs else None
    last = q.legs[-1] if q.legs else None
    return {
        "at": seen_at,
        "watch": q.watch_id,
        "depart": f"{q.depart_date:%Y-%m-%d}",
        "ret": f"{q.return_date:%Y-%m-%d}",
        "trip_days": q.probe.trip_days,
        "price": q.price,
        "currency": q.currency,
        "level": q.price_level,
        "typical_low": q.typical_low,
        "typical_high": q.typical_high,
        "airlines": list(q.airlines),
        "stops": q.stops,
        "deal": verdict.is_deal,
        # Enough to rebuild a useful line in the monthly digest without
        # re-querying: the outbound clock times, the flight numbers, and the
        # Google Flights link for this exact date pair.
        "dep_time": first.depart_time if first else None,
        "arr_time": last.arrive_time if last else None,
        "from_id": first.from_id if first else None,
        "to_id": last.to_id if last else None,
        "duration": q.duration_minutes,
        "flights": [lg.flight_number for lg in q.legs if lg.flight_number],
        "url": q.booking_url,
    }


def run_stamp() -> str:
    """One timestamp for a whole run, taken once and passed to every record().

    `at` is what identifies a run in this file, and record() is called once per
    route - so letting each call stamp its own clock gave every route its own
    `at` and made a single run look like several. The digest counts distinct
    timestamps as runs, and reported three runs across two routes as six.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(verdicts: Iterable[Verdict], path: Path | None = None,
           seen_at: str | None = None) -> int:
    """Append one line per quote. Returns how many rows were written.

    Records quotes with no price too: "nothing on sale for these dates" is a
    fact about the route worth keeping, not an absence.
    """
    rows = list(verdicts)
    if not rows:
        return 0

    seen_at = seen_at or run_stamp()
    target = path or HISTORY_PATH
    with open(target, "a", encoding="utf-8", newline="\n") as fh:
        for v in rows:
            fh.write(json.dumps(_row(v, seen_at), separators=(",", ":")) + "\n")
    return len(rows)


def read(path: Path | None = None) -> Iterator[dict]:
    """Yield every recorded row, oldest first.

    Skips unparseable lines rather than failing: a half-written line from an
    interrupted run shouldn't cost you the whole history.
    """
    target = path or HISTORY_PATH
    if not target.exists():
        return
    with open(target, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def since(after: str | None, path: Path | None = None) -> list[dict]:
    """Rows recorded strictly after the ISO timestamp `after`.

    ISO-8601 UTC strings sort lexicographically in time order, so this is a
    string comparison rather than date parsing - and a malformed `at` simply
    fails to match instead of raising.
    """
    rows = list(read(path))
    if not after:
        return rows
    return [r for r in rows if str(r.get("at", "")) > after]


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile. Stdlib only, and explicit about ties.

    `statistics.quantiles` would do this, but only in fixed cuts (quartiles,
    deciles) and it raises below n=2 - this is called with whatever a route
    happens to have accumulated, so it needs to be total over any non-empty
    list.
    """
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


@dataclass(frozen=True)
class Baseline:
    """What a route's fares are judged against: each date's own past prices.

    `typical` maps a departure date to the median price previously recorded
    for it. `ratio` is the bar a fare has to come in under, expressed as a
    fraction of that median - 0.9 meaning "at least a tenth below what this
    date normally costs here".
    """

    ratio: float
    typical: dict[str, float]

    def score(self, price: float, depart: str) -> float | None:
        """Price as a fraction of what that exact date normally costs.

        None when the date has no history - a date that has just entered the
        window has nothing to be compared against, and guessing would make its
        first sighting look like a crash.
        """
        was = self.typical.get(depart)
        if not was:
            return None
        return price / was


def baselines(path: Path | None = None) -> dict[tuple[str, int], Baseline]:
    """(watch id, trip length) -> what its fares are measured against.

    The comparison is per departure date, and that is the whole point. Pooling
    every date into one list made the bar measure the CALENDAR rather than the
    market: April is always cheaper than December on a seasonal route, so the
    cheapest tenth of a pooled list is simply the April dates, every run,
    forever. The rule fired on "it is April" while claiming to mean "this fare
    is cheap", and it could not see a change at all - an April fare drifting UP
    from 640 to 660 still cleared the bar, while December collapsing from 1,400
    to 1,000 did not come near it.

    Dividing each price by its own date's median removes the season, because
    every date is then measured against itself. What is left on that common
    scale is movement, which is the thing worth emailing about. The ratios are
    then pooled across the route so there is enough data for a percentile to
    mean something - a single date rarely has enough readings of its own.

    Only dates seen more than once contribute. A date with one reading is its
    own median, so it would contribute a ratio of exactly 1.0 and drag the bar
    towards "any drop at all counts".

    Keyed on trip length as well as route because trip length is most of the
    price: a 12-day and a 7-day fare are different goods, and changing one
    starts a fresh baseline rather than making every fare look like a bargain.

    Read once at the start of a run, before any of this run's rows are written,
    so a fare is only ever compared against runs that came before it. A route
    with fewer than MIN_FLOOR_OBSERVATIONS usable readings gets no baseline at
    all, which is what keeps a newly added route quiet.
    """
    # (route, trip) -> departure date -> prices seen for it
    seen: dict[tuple[str, int], dict[str, list[float]]] = {}
    for r in read(path):
        price, watch, trip = r.get("price"), r.get("watch"), r.get("trip_days")
        depart = r.get("depart")
        if not isinstance(price, (int, float)) or not watch or not depart:
            continue
        # bool is an int subclass; a stray `true` here would key a real bucket.
        if not isinstance(trip, int) or isinstance(trip, bool):
            continue
        seen.setdefault((str(watch), trip), {}).setdefault(str(depart), []).append(float(price))

    out: dict[tuple[str, int], Baseline] = {}
    for key, by_date in seen.items():
        typical = {d: statistics.median(ps) for d, ps in by_date.items()}
        ratios = [p / typical[d] for d, ps in by_date.items() if len(ps) > 1 for p in ps]
        if len(ratios) < MIN_FLOOR_OBSERVATIONS:
            continue
        out[key] = Baseline(ratio=_percentile(ratios, FLOOR_PERCENTILE), typical=typical)
    return out


def digest(rows: list[dict]) -> dict:
    """Roll rows up into what a periodic 'still alive' report needs to say."""
    priced = [r for r in rows if isinstance(r.get("price"), (int, float))]
    runs = sorted({r["at"] for r in rows if r.get("at")})

    per_route: dict[str, dict] = {}
    for r in priced:
        best = per_route.get(r["watch"])
        if best is None or r["price"] < best["price"]:
            per_route[r["watch"]] = r

    # What the route costs today, per route: the cheapest row from the newest
    # run. The digest is sent at the tail of a run that has just searched every
    # date, so today's price is already on disk - reporting it costs nothing.
    #
    # Without it the digest shows a low-water mark next to a working booking
    # link, which reads as a fare you can still buy. Often it isn't.
    # Newest timestamp per route rather than one global newest. A run now
    # stamps every route with the same `at` (see run_stamp), so these usually
    # agree - but a route paused for a month, or rows written before that fix,
    # would otherwise drop out of `current` entirely and lose its price.
    latest_at: dict[str, str] = {}
    for r in priced:
        at = str(r.get("at") or "")
        if at > latest_at.get(r["watch"], ""):
            latest_at[r["watch"]] = at

    current: dict[str, dict] = {}
    for r in priced:
        if str(r.get("at") or "") != latest_at.get(r["watch"]):
            continue
        best = current.get(r["watch"])
        if best is None or r["price"] < best["price"]:
            current[r["watch"]] = r

    # How much cheaper the best date is than a typical one, within the newest
    # run. A run prices every sampled date at the same moment, so these are
    # directly comparable with no history and no extra searches - the numbers
    # were already paid for and were previously just thrown away.
    #
    # This lives in the digest rather than in an alert on purpose. It answers
    # "when should I fly", and that answer barely changes week to week: on a
    # seasonal route the cheapest date is always well under the median, every
    # single run, whether or not any price moved. As an alert it would fire
    # constantly and carry no news; as a monthly line it is exactly the shape
    # of the year, which is the thing worth knowing.
    #
    # Median, not mean: one 3,000 outlier drags a mean up far enough to make
    # every other fare look like a bargain.
    spread: dict[str, dict] = {}
    for watch, at in latest_at.items():
        sampled = [r for r in priced
                   if r["watch"] == watch and str(r.get("at") or "") == at]
        # Under four dates there is no meaningful "typical date" to be under.
        if len(sampled) < 4:
            continue
        prices = [r["price"] for r in sampled]
        mid = statistics.median(prices)
        if mid <= 0:
            continue
        best = min(prices)
        spread[watch] = {
            "dates": len(sampled),
            "median": mid,
            "cheapest": best,
            "under_pct": round((mid - best) / mid * 100),
            "currency": rows[0].get("currency") or "",
        }

    # The shape of the year: the cheapest departure date within each month.
    #
    # Taken from the newest run only, so every price here is one that can still
    # be booked and can carry a link. A low-water mark per month would read the
    # same and be a fare that is gone.
    #
    # The spread line above says how good the best date is; this says WHEN the
    # good dates are, which is the question a flexible traveller actually has
    # and the one the alerting rules deliberately do not answer.
    months: dict[str, list[dict]] = {}
    for watch, at in latest_at.items():
        best: dict[str, dict] = {}
        for r in priced:
            if r["watch"] != watch or str(r.get("at") or "") != at:
                continue
            month = str(r.get("depart") or "")[:7]        # YYYY-MM
            if len(month) != 7:
                continue
            if month not in best or r["price"] < best[month]["price"]:
                best[month] = r
        # One month is not a shape, and the headline already quoted that fare.
        if len(best) > 1:
            months[watch] = [best[m] for m in sorted(best)]

    def headline(r: dict) -> float:
        """Sort on the number the reader sees first, which is today's."""
        return (current.get(r["watch"]) or r)["price"]

    return {
        "current": current,
        "spread": spread,
        "months": months,
        "runs": len(runs),
        "first_run": runs[0] if runs else None,
        "last_run": runs[-1] if runs else None,
        "searches": len(rows),
        "priced": len(priced),
        "empty": len(rows) - len(priced),
        "deals": sum(1 for r in rows if r.get("deal")),
        "levels": {
            lv: sum(1 for r in priced if (r.get("level") or "?") == lv)
            for lv in sorted({(r.get("level") or "?") for r in priced})
        },
        # cheapest row per route, cheapest route first
        "routes": sorted(per_route.values(), key=headline),
    }


def summary(path: Path | None = None) -> dict:
    """Cheap overview of what's accumulated so far."""
    rows = list(read(path))
    priced = [r for r in rows if isinstance(r.get("price"), (int, float))]
    runs = {r.get("at") for r in rows}
    return {
        "rows": len(rows),
        "runs": len(runs),
        "priced": len(priced),
        "cheapest": min((r["price"] for r in priced), default=None),
        "first_seen": min((r["at"] for r in rows), default=None),
        "last_seen": max((r["at"] for r in rows), default=None),
    }
