"""Settings, paths, and the watchlist loader.

Secrets come from the environment (via a local `.env` in development, or repo
secrets when running in GitHub Actions). The watchlist itself is plain JSON so
it stays hand-editable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHES_PATH = ROOT / "watches.json"
STATE_PATH = ROOT / "state.json"
FIXTURES_DIR = ROOT / "fixtures"


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
class Probe:
    """One search: one exact departure date paired with one exact return date.

    The google_flights engine takes a single YYYY-MM-DD per field - it has no
    flexible-date or date-range mode - so a run samples the window rather than
    covering it. `step_days` in the watch config sets how dense that sample is.
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
    id: str
    label: str
    origin: str
    destination: str
    enabled: bool = True
    adults: int = 1
    travel_class: int = 1
    stops: int = 0
    currency: str = "AUD"
    gl: str = "au"
    hl: str = "en"
    trip_min: int = 10
    trip_max: int = 14
    window: dict = field(default_factory=dict)
    deal_rules: DealRules = field(default_factory=DealRules)
    notify: NotifyRules = field(default_factory=NotifyRules)

    def probes(self, today: date | None = None) -> list[Probe]:
        """Sample the window as exact date pairs, one search each.

        Trip length rotates through [trip_min, trip_max] across consecutive
        probes, so a run samples the duration dimension too without costing
        anything extra - each search needs one exact return date regardless.
        """
        today = today or date.today()
        win = self.window
        step = max(int(win.get("step_days", 5)), 1)

        if win.get("mode") == "fixed" and win.get("start_date"):
            start = date.fromisoformat(win["start_date"])
            end = date.fromisoformat(win["end_date"])
        else:
            start = today + timedelta(days=int(win.get("days_from_now_min", 90)))
            end = today + timedelta(days=int(win.get("days_from_now_max", 290)))

        # Never search a departure date in the past, and never past the point
        # airlines have loaded schedules - beyond that every search returns
        # nothing and still costs a credit.
        start = max(start, today + timedelta(days=1))
        horizon = today + timedelta(days=int(win.get("max_horizon_days", 300)))
        end = min(end, horizon)
        if end < start:
            return []

        spread = max(self.trip_max - self.trip_min + 1, 1)
        out: list[Probe] = []
        cursor, i = start, 0
        while cursor <= end:
            trip = self.trip_min + (i % spread)
            out.append(Probe(depart=cursor, ret=cursor + timedelta(days=trip)))
            cursor += timedelta(days=step)
            i += 1
        return out


def _strip_comments(obj: dict) -> dict:
    """Drop the `_note`-style documentation keys used in watches.json."""
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def load_watchlist(path: Path | None = None) -> tuple[list[Watch], int]:
    """Return (watches, monthly_search_cap)."""
    raw = json.loads((path or WATCHES_PATH).read_text(encoding="utf-8"))
    cap = int(_strip_comments(raw.get("_budget", {})).get("monthly_search_cap", 240))

    watches: list[Watch] = []
    for entry in raw.get("watches", []):
        e = _strip_comments(entry)
        rules = _strip_comments(e.get("deal_rules", {}))
        notify = _strip_comments(e.get("notify", {}))
        trip = _strip_comments(e.get("trip_length_days", {}))
        market = _strip_comments(e.get("market", {}))
        passengers = _strip_comments(e.get("passengers", {}))

        watches.append(
            Watch(
                id=e["id"],
                label=e.get("label", e["id"]),
                origin=e["origin"].upper(),
                destination=e["destination"].upper(),
                enabled=e.get("enabled", True),
                adults=int(passengers.get("adults", 1)),
                travel_class=int(e.get("travel_class", 1)),
                stops=int(e.get("stops", 0)),
                currency=e.get("currency", "AUD"),
                gl=market.get("gl", "au"),
                hl=market.get("hl", "en"),
                trip_min=int(trip.get("min", 10)),
                trip_max=int(trip.get("max", 14)),
                window=_strip_comments(e.get("window", {})),
                deal_rules=DealRules(
                    alert_on_price_level=tuple(
                        s.lower() for s in rules.get("alert_on_price_level", ["low"])
                    ),
                ),
                notify=NotifyRules(
                    email=bool(notify.get("email", True)),
                    # Absent or null both mean "never re-alert on time alone".
                    cooldown_hours=(
                        None if notify.get("cooldown_hours") is None
                        else float(notify["cooldown_hours"])
                    ),
                    renotify_on_drop_pct=float(notify.get("renotify_on_drop_pct", 5)),
                ),
            )
        )
    return watches, cap
