"""Settings, paths, and the watchlist loader.

Secrets come from the environment (via a local `.env` in development, or repo
secrets when running in GitHub Actions). The watchlist itself is plain JSON so
it stays hand-editable.

A watch describes only what actually varies between routes - where you're
flying, how long for, and whether it's on. Everything else is either a shared
default (`defaults` in watches.json) or computed: see `plan_sampling`.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHES_PATH = ROOT / "watches.json"
STATE_PATH = ROOT / "state.json"
FIXTURES_DIR = ROOT / "fixtures"

# Airlines load schedules roughly 300 days out. Past that a search returns
# nothing AND still costs a credit. Verified live: 297 days returned flights,
# 327 returned none. Not configurable - it's a fact about the world.
MAX_HORIZON_DAYS = 300


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file. Existing vars always win."""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    user: str
    password: str
    to: str

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.to)

    @classmethod
    def from_env(cls) -> "EmailSettings":
        return cls(
            host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
            port=int(os.getenv("SMTP_PORT", "465")),
            user=os.getenv("SMTP_USER", ""),
            password=os.getenv("SMTP_PASSWORD", ""),
            to=os.getenv("ALERT_TO", "") or os.getenv("SMTP_USER", ""),
        )


@dataclass(frozen=True)
class DealRules:
    alert_on_price_level: tuple[str, ...] = ("low",)
    """The whole rule set. Absolute price thresholds were removed deliberately:
    they need retuning whenever fares structurally move, and Google's own verdict
    already recalibrates by season on its own."""


@dataclass(frozen=True)
class NotifyRules:
    email: bool = True
    cooldown_hours: float | None = None
    """Hours before the same date pair may alert again. None (the default) means
    never on time alone - a date pair you've been told about stays quiet unless
    the price drops another `renotify_on_drop_pct`."""
    renotify_on_drop_pct: float = 5.0


@dataclass(frozen=True)
class Budget:
    monthly_search_cap: int = 240
    runs_per_month: float = 4.3
    """Weekly schedule - see .github/workflows/check-flights.yml."""


@dataclass(frozen=True)
class Probe:
    """One search: one exact departure date paired with one exact return date.

    The google_flights engine takes a single YYYY-MM-DD per field - it has no
    flexible-date or date-range mode - so a run samples the window rather than
    covering it. `Watch.step_days` sets how dense that sample is.
    """

    depart: date
    ret: date

    @property
    def trip_days(self) -> int:
        return (self.ret - self.depart).days

    @property
    def key(self) -> str:
        return f"{self.depart:%Y-%m-%d}_{self.ret:%Y-%m-%d}"

    @property
    def label(self) -> str:
        return f"{self.depart:%a %d %b} -> {self.ret:%a %d %b %Y} ({self.trip_days}d)"


@dataclass
class Watch:
    """One route. Only the first five fields come from the UI; the rest are
    shared defaults or computed by `plan_sampling`."""

    id: str
    origin: str
    destination: str
    enabled: bool = True
    trip_days: int = 12

    days_from_now_min: int = 60
    days_from_now_max: int = MAX_HORIZON_DAYS
    step_days: int = 5  # overwritten by plan_sampling()

    adults: int = 1
    travel_class: int = 1
    stops: int = 0
    currency: str = "AUD"
    gl: str = "au"
    hl: str = "en"
    deal_rules: DealRules = field(default_factory=DealRules)
    notify: NotifyRules = field(default_factory=NotifyRules)

    @property
    def label(self) -> str:
        """Derived, not stored - a route names itself."""
        return f"{self.origin} -> {self.destination}"

    def probes(self, today: date | None = None) -> list[Probe]:
        """Sample the window as exact date pairs, one search each.

        Trip length is a single number, so every probe uses the same duration
        and prices are directly comparable across dates: a cheaper result means
        a cheaper date, not a shorter trip.
        """
        today = today or date.today()
        start = today + timedelta(days=self.days_from_now_min)
        end = today + timedelta(days=self.days_from_now_max)

        # Never search a departure date in the past, and never past the point
        # airlines have loaded schedules.
        #
        # The horizon binds the RETURN leg, not just the departure: a round trip
        # needs both flights on sale, so a departure inside the horizon whose
        # return falls outside it returns nothing and still costs a credit.
        start = max(start, today + timedelta(days=1))
        end = min(end, today + timedelta(days=MAX_HORIZON_DAYS - self.trip_days))
        if end < start:
            return []

        step = max(self.step_days, 1)
        out: list[Probe] = []
        cursor = start
        while cursor <= end:
            out.append(Probe(depart=cursor, ret=cursor + timedelta(days=self.trip_days)))
            cursor += timedelta(days=step)
        return out


def plan_sampling(watches: list[Watch], budget: Budget) -> None:
    """Set `step_days` on every enabled watch so the whole run fits the budget.

    This is the dial that used to be hand-tuned in watches.json, and getting it
    right meant doing arithmetic every time a route was added. The constraint is
    simple enough to solve directly: split the per-run allowance evenly between
    enabled routes, then pick the densest step that keeps each within its share.

    The trade is worth stating plainly: adding a route makes every route sample
    more coarsely, because the monthly cap is fixed. Nothing silently overspends.
    """
    active = [w for w in watches if w.enabled]
    if not active:
        return

    per_run = budget.monthly_search_cap / max(budget.runs_per_month, 0.1)
    share = per_run / len(active)

    for w in active:
        # Clip to the horizon exactly as probes() does - including the trip
        # length, since the return leg has to be on sale too. Budgeting for days
        # that will never be searched makes the step too big, so the run samples
        # more coarsely than it can afford and leaves credits unspent.
        reach = min(w.days_from_now_max, MAX_HORIZON_DAYS - w.trip_days)
        span = max(reach - w.days_from_now_min, 1)
        if share <= 1:
            # Budget too small to sample at all - one probe per run per route.
            w.step_days = span
        else:
            # probes = floor(span/step) + 1 <= share  =>  step >= span/(share-1)
            w.step_days = max(math.ceil(span / (share - 1)), 1)


def _clean(obj: dict) -> dict:
    """Drop the `_note`-style documentation keys used in watches.json."""
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def _default_id(origin: str, destination: str) -> str:
    return f"{origin}-{destination}".lower()


def load_watchlist(path: Path | None = None) -> tuple[list[Watch], Budget]:
    """Return (watches, budget) from disk, with sampling already planned."""
    raw = json.loads((path or WATCHES_PATH).read_text(encoding="utf-8"))
    watches, budget = load_watchlist_data(raw)
    plan_sampling(watches, budget)
    return watches, budget


def load_watchlist_data(raw: dict) -> tuple[list[Watch], Budget]:
    """Parse an already-decoded watchlist. Does not plan sampling.

    Split out from `load_watchlist` so the control panel can validate and cost
    an unsaved edit through exactly the same code a real run uses.
    """
    b = _clean(raw.get("budget", {}))
    budget = Budget(
        monthly_search_cap=int(b.get("monthly_search_cap", 240)),
        runs_per_month=float(b.get("runs_per_month", 4.3)),
    )

    d = _clean(raw.get("defaults", {}))
    win = _clean(d.get("window", {}))
    market = _clean(d.get("market", {}))
    notify = _clean(d.get("notify", {}))

    rules = DealRules(
        alert_on_price_level=tuple(
            s.lower() for s in d.get("alert_on_price_level", ["low"])
        ),
    )
    notify_rules = NotifyRules(
        email=bool(notify.get("email", True)),
        # Absent or null both mean "never re-alert on time alone".
        cooldown_hours=(
            None if notify.get("cooldown_hours") is None
            else float(notify["cooldown_hours"])
        ),
        renotify_on_drop_pct=float(notify.get("renotify_on_drop_pct", 5)),
    )

    watches: list[Watch] = []
    seen_ids: set[str] = set()
    for entry in raw.get("watches", []):
        e = _clean(entry)
        origin = str(e["origin"]).upper()
        destination = str(e["destination"]).upper()

        # IDs are derived from the route, so two watches on the same pair would
        # collide - and the id keys alert history in state.json, which would
        # make one route suppress the other's emails.
        wid = e.get("id") or _default_id(origin, destination)
        if wid in seen_ids:
            base, n = wid, 2
            while f"{base}-{n}" in seen_ids:
                n += 1
            wid = f"{base}-{n}"
        seen_ids.add(wid)

        watches.append(
            Watch(
                id=wid,
                origin=origin,
                destination=destination,
                enabled=bool(e.get("enabled", True)),
                trip_days=int(e.get("trip_days", 12)),
                days_from_now_min=int(win.get("days_from_now_min", 60)),
                days_from_now_max=int(win.get("days_from_now_max", MAX_HORIZON_DAYS)),
                adults=int(d.get("adults", 1)),
                travel_class=int(d.get("travel_class", 1)),
                stops=int(d.get("stops", 0)),
                currency=d.get("currency", "AUD"),
                gl=market.get("gl", "au"),
                hl=market.get("hl", "en"),
                deal_rules=rules,
                notify=notify_rules,
            )
        )

    return watches, budget
