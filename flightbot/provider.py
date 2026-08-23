"""Flight price lookups.

`SerpApiProvider` hits the real Google Flights endpoint. `DemoProvider` replays
a bundled fixture with deterministic per-probe variation, so the whole pipeline
can be exercised before you own an API key.

One search = one exact date pair. The engine has no flexible-date mode: both
`outbound_date` and `return_date` take a single YYYY-MM-DD.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import requests

from .config import FIXTURES_DIR, Probe, Watch

SERPAPI_ENDPOINT = "https://serpapi.com/search"
TIMEOUT_SECONDS = 45

# Returned for dates airlines haven't loaded yet. Normal, not a failure.
NO_RESULTS = "hasn't returned any results"


class SearchError(RuntimeError):
    """A lookup failed in a way the caller should surface, not crash on."""


@dataclass(frozen=True)
class Leg:
    """One flight within an itinerary.

    The times come back as "YYYY-MM-DD HH:MM" local to each airport. They were
    always in the response and used to be discarded - "Jetstar, nonstop" tells
    you nothing about whether you're leaving at 06:00 or 21:00.
    """

    airline: str | None
    flight_number: str | None
    from_id: str | None
    to_id: str | None
    depart: str | None
    arrive: str | None

    @staticmethod
    def _clock(stamp: str | None) -> str:
        return stamp.split()[1] if stamp and " " in stamp else ""

    @property
    def depart_time(self) -> str:
        return self._clock(self.depart)

    @property
    def arrive_time(self) -> str:
        return self._clock(self.arrive)


@dataclass
class Quote:
    """The cheapest itinerary for one exact date pair, plus Google's verdict."""

    watch_id: str
    probe: Probe
    price: float | None
    currency: str
    price_level: str | None
    typical_low: float | None
    typical_high: float | None
    airlines: tuple[str, ...]
    stops: int | None
    duration_minutes: int | None
    booking_url: str | None
    legs: tuple[Leg, ...] = ()
    """Outbound only. A round-trip search returns outbound itineraries; the
    return leg needs a second call against a departure token, which would cost
    another credit per fare."""

    @property
    def depart_date(self) -> date:
        return self.probe.depart

    @property
    def return_date(self) -> date:
        """Known for free - we asked for this exact date, so it's not a guess."""
        return self.probe.ret

    @property
    def typical_mid(self) -> float | None:
        if self.typical_low is None or self.typical_high is None:
            return None
        return (self.typical_low + self.typical_high) / 2


class Provider(Protocol):
    def search(self, watch: Watch, probe: Probe) -> Quote: ...


def searches_left(api_key: str) -> int | None:
    """Ask SerpApi how much quota is actually left. Free - not a search.

    Authoritative, unlike a local tally, which drifts the moment state.json is
    deleted or a run dies midway. Returns None if the endpoint can't be reached,
    so the caller can fall back rather than refuse to run.
    """
    try:
        resp = requests.get("https://serpapi.com/account",
                            params={"api_key": api_key}, timeout=20)
        if resp.status_code != 200:
            return None
        return int(resp.json()["total_searches_left"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def _params(watch: Watch, probe: Probe) -> dict:
    return {
        "engine": "google_flights",
        "departure_id": watch.origin,
        "arrival_id": watch.destination,
        "outbound_date": f"{probe.depart:%Y-%m-%d}",
        "return_date": f"{probe.ret:%Y-%m-%d}",
        "type": 1,  # round trip
        "adults": watch.adults,
        "travel_class": watch.travel_class,
        "stops": watch.stops,
        "currency": watch.currency,
        "gl": watch.gl,
        "hl": watch.hl,
    }


def _parse(payload: dict, watch: Watch, probe: Probe) -> Quote:
    insights = payload.get("price_insights") or {}
    typical = insights.get("typical_price_range") or [None, None]

    itineraries = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    cheapest = min(
        (it for it in itineraries if isinstance(it.get("price"), (int, float))),
        key=lambda it: it["price"],
        default=None,
    )

    # price_insights.lowest_price is Google's own figure and is the more
    # reliable of the two; fall back to scanning itineraries if it's absent.
    price = insights.get("lowest_price")
    if not isinstance(price, (int, float)):
        price = cheapest["price"] if cheapest else None

    airlines: tuple[str, ...] = ()
    stops = duration = None
    parsed_legs: tuple[Leg, ...] = ()
    # Only describe the itinerary when it's the one we're quoting a price for.
    # If Google's lowest_price disagrees with the cheapest listed flight,
    # naming the airline would attach someone else's flight to this fare.
    if cheapest and price == cheapest.get("price"):
        legs = cheapest.get("flights") or []
        airlines = tuple(dict.fromkeys(leg.get("airline") for leg in legs if leg.get("airline")))
        stops = max(len(legs) - 1, 0)
        duration = cheapest.get("total_duration")
        parsed_legs = tuple(
            Leg(
                airline=leg.get("airline"),
                flight_number=leg.get("flight_number"),
                from_id=(leg.get("departure_airport") or {}).get("id"),
                to_id=(leg.get("arrival_airport") or {}).get("id"),
                depart=(leg.get("departure_airport") or {}).get("time"),
                arrive=(leg.get("arrival_airport") or {}).get("time"),
            )
            for leg in legs
        )

    return Quote(
        watch_id=watch.id,
        probe=probe,
        price=float(price) if isinstance(price, (int, float)) else None,
        currency=watch.currency,
        price_level=(insights.get("price_level") or None),
        typical_low=typical[0] if typical and typical[0] is not None else None,
        typical_high=typical[1] if len(typical) > 1 and typical[1] is not None else None,
        airlines=airlines,
        stops=stops,
        duration_minutes=duration,
        booking_url=payload.get("search_metadata", {}).get("google_flights_url"),
        legs=parsed_legs,
    )


def _empty(watch: Watch, probe: Probe) -> Quote:
    return Quote(watch_id=watch.id, probe=probe, price=None, currency=watch.currency,
                 price_level=None, typical_low=None, typical_high=None,
                 airlines=(), stops=None, duration_minutes=None, booking_url=None)


class SerpApiProvider:
    def __init__(self, api_key: str, session: requests.Session | None = None) -> None:
        if not api_key:
            raise SearchError(
                "SERPAPI_KEY is not set. Add it to .env, or run with --demo to "
                "exercise the pipeline against the bundled fixture."
            )
        self.api_key = api_key
        self.session = session or requests.Session()

    def search(self, watch: Watch, probe: Probe) -> Quote:
        params = _params(watch, probe) | {"api_key": self.api_key}
        try:
            resp = self.session.get(SERPAPI_ENDPOINT, params=params, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise SearchError(f"network error contacting SerpApi: {exc}") from exc

        if resp.status_code == 401:
            raise SearchError("SerpApi rejected the key (401). Check SERPAPI_KEY.")
        if resp.status_code == 429:
            raise SearchError("SerpApi rate limit or monthly quota exhausted (429).")
        if resp.status_code >= 400:
            raise SearchError(f"SerpApi returned HTTP {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        error = payload.get("error") or ""
        if NO_RESULTS in error:
            # Nothing on sale for these dates - a fact about the route, not a
            # fault. Report it as a priced-nothing quote so the run continues.
            return _empty(watch, probe)
        if error:
            raise SearchError(f"SerpApi error: {error}")
        return _parse(payload, watch, probe)


class DemoProvider:
    """Replays a fixture, varying price per probe so the logic is visible."""

    def __init__(self, fixture: str = "bne_tyo_sample.json") -> None:
        self.payload = json.loads((FIXTURES_DIR / fixture).read_text(encoding="utf-8"))

    def search(self, watch: Watch, probe: Probe) -> Quote:
        payload = json.loads(json.dumps(self.payload))  # deep copy
        insights = payload["price_insights"]
        base = insights["lowest_price"]

        # Deterministic pseudo-variation: same probe always yields same price,
        # different probes spread across the cheap/typical/expensive range.
        seed = int(hashlib.md5(f"{watch.id}{probe.key}".encode()).hexdigest()[:8], 16)
        factor = 0.62 + (seed % 90) / 100.0  # 0.62x .. 1.51x
        price = round(base * factor)

        low, high = insights["typical_price_range"]
        insights["lowest_price"] = price
        insights["price_level"] = (
            "low" if price < low else "high" if price > high else "typical"
        )

        # Scale every itinerary by the same factor so the cheapest stays the
        # cheapest - otherwise the fixture contradicts its own lowest_price.
        for key in ("best_flights", "other_flights"):
            for itinerary in payload.get(key, []):
                itinerary["price"] = max(round(itinerary["price"] * factor), price)
                for leg in itinerary.get("flights", []):
                    stamp = (leg.get("departure_airport") or {}).get("time", "")
                    clock = stamp.split()[1] if " " in stamp else "09:10"
                    leg.setdefault("departure_airport", {})["time"] = (
                        f"{probe.depart:%Y-%m-%d} {clock}")
        payload["best_flights"][0]["price"] = price

        return _parse(payload, watch, probe)


def build_provider(api_key: str, demo: bool) -> Provider:
    return DemoProvider() if demo else SerpApiProvider(api_key)
