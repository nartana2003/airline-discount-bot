"""Builders for the objects the tests need, so each test says only what it varies."""

from __future__ import annotations

from datetime import date

from flightbot.config import Probe, Watch
from flightbot.evaluate import evaluate
from flightbot.provider import Leg, Quote

WATCH = Watch(id="bne-nrt", origin="BNE", destination="NRT")


def quote(price: float | None, level: str | None = "typical", *, day: int = 18,
          watch: Watch = WATCH, legs: bool = False) -> Quote:
    return Quote(
        watch_id=watch.id,
        probe=Probe(depart=date(2027, 5, day), ret=date(2027, 5, day + 12)),
        price=price, currency="AUD", price_level=level,
        typical_low=900, typical_high=1500, airlines=("Jetstar",),
        stops=0, duration_minutes=640,
        booking_url="https://www.google.com/travel/flights?hl=en&tfs=CBwQAh&tfu=EgIIAQ",
        legs=(Leg("Jetstar", "JQ9", "BNE", "NRT",
                  f"2027-05-{day:02d} 09:30", f"2027-05-{day:02d} 17:40"),) if legs else (),
    )


def verdict(price: float | None, level: str | None = "typical", **kw):
    return evaluate(kw.pop("watch", WATCH), quote(price, level, **kw))


def row(price, watch="bne-nrt", at="2026-08-24T00:00:00+00:00", depart="2027-05-18",
        ret="2027-05-30", **extra) -> dict:
    r = {"at": at, "watch": watch, "depart": depart, "ret": ret, "trip_days": 12,
         "price": price, "currency": "AUD", "level": "typical", "deal": False}
    r.update(extra)
    return r
