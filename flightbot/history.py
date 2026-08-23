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
