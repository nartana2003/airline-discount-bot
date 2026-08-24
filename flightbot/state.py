"""Run-to-run memory: what we last saw, what we last said, what we've spent.

Two jobs. First, suppress repeat alerts so a fare that stays cheap for a week
doesn't email you seven times. Second, track SerpApi search credits against the
monthly cap so a runaway loop can't burn the free tier in one go.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_PATH
from .evaluate import Verdict


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")

    # --- periodic "still alive" digest -----------------------------------
    # A run that finds nothing looks exactly like a broken key, a dead SMTP
    # password, or a workflow that stopped firing. Once a month the bot says
    # so out loud, which is what turns silence into a fact rather than a
    # guess. Keyed by calendar month so it lands on the first run of each.

    def digest_due(self) -> bool:
        return self.data.setdefault("digest", {}).get("month") != _now().strftime("%Y-%m")

    def last_digest_at(self) -> str | None:
        return self.data.setdefault("digest", {}).get("at")

    def record_digest(self) -> None:
        self.data.setdefault("digest", {}).update(
            {"month": _now().strftime("%Y-%m"), "at": _now().isoformat()}
        )

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
