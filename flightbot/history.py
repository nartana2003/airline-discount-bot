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
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT
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


def record(verdicts: Iterable[Verdict], path: Path | None = None) -> int:
    """Append one line per quote. Returns how many rows were written.

    Records quotes with no price too: "nothing on sale for these dates" is a
    fact about the route worth keeping, not an absence.
    """
    rows = list(verdicts)
    if not rows:
        return 0

    seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
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


def floors(path: Path | None = None) -> dict[str, float]:
    """watch id -> the cheapest price ever recorded for it.

    The bar a fare has to beat to count as a record. Read once at the start of
    a run, before any of this run's rows are written, so a fare is only ever
    compared against runs that came before it. That is also what makes the
    first run on a new route silent: no history means no floor, and a route
    with no floor cannot set a record.
    """
    out: dict[str, float] = {}
    for r in read(path):
        price, watch = r.get("price"), r.get("watch")
        if not isinstance(price, (int, float)) or not watch:
            continue
        if watch not in out or price < out[watch]:
            out[watch] = float(price)
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
    # Newest timestamp per route, not one global newest: record() is called
    # once per watch, so every route stamps its own `at` seconds apart. A
    # single global maximum would match only the last route of the run and
    # leave every other route without a current price.
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

    def headline(r: dict) -> float:
        """Sort on the number the reader sees first, which is today's."""
        return (current.get(r["watch"]) or r)["price"]

    return {
        "current": current,
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
