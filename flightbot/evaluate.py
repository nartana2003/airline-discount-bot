"""Turn a price quote into a yes/no verdict, with reasons you can read."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Watch
from .provider import Quote


@dataclass
class Verdict:
    quote: Quote
    is_deal: bool
    reasons: list[str] = field(default_factory=list)
    discount_pct: float | None = None

    @property
    def when(self) -> str:
        """Exact dates - we asked for this pair, so both are known."""
        return self.quote.probe.label

    @property
    def headline(self) -> str:
        q = self.quote
        if q.price is None:
            return f"{q.probe.label}: no fares returned"
        price = f"{q.currency} {q.price:,.0f}"
        if self.discount_pct is not None:
            # One decimal: rounding to whole percent makes a 14.6% discount
            # read as "-15%" next to a 15% threshold it doesn't actually meet.
            return f"{self.when}: {price} ({self.discount_pct:+.1f}% vs typical)"
        return f"{self.when}: {price}"


def evaluate(watch: Watch, quote: Quote) -> Verdict:
    rules = watch.deal_rules
    reasons: list[str] = []

    if quote.price is None:
        return Verdict(quote=quote, is_deal=False, reasons=["no fares returned for this window"])

    # Kept for display only - useful context on an alert, but no longer a rule.
    # Comparing to the midpoint of a wide range flatters: on a 820-1,700 range a
    # fare of 1,071 reads as "15% below typical" while being nowhere near cheap.
    discount_pct: float | None = None
    mid = quote.typical_mid
    if mid:
        discount_pct = (quote.price - mid) / mid * 100.0

    # Two rules, neither with a number in it.
    #
    # Google's verdict is built on real price history for this route and these
    # dates, and it recalibrates by season on its own. It catches fares that
    # are cheap *for when they are* - a December seat that is good for December.
    if quote.price_level and quote.price_level.lower() in rules.alert_on_price_level:
        reasons.append(f"Google rates this fare '{quote.price_level}'")

    # The second rule needs every date for the route before it can be applied
    # - see mark_record().

    return Verdict(
        quote=quote,
        is_deal=bool(reasons),
        reasons=reasons,
        discount_pct=discount_pct,
    )


def mark_record(verdicts: list[Verdict], floor: float | None) -> Verdict | None:
    """Flag the run's cheapest fare when it beats everything seen before.

    The second alerting rule, and it says nothing about seasons: a fare can be
    the lowest ever seen on a route and still be rated 'typical', which is the
    whole gap Google's verdict leaves open. This reads the price journal
    instead of asking Google.

    Applied to the route's cheapest fare only, once every date has been priced.
    Testing each date against the floor as it arrives would flag every fare
    under the old record - four dates all announcing they are "the cheapest
    ever" when only one of them is.

    Self-calibrating: the bar is whatever the market has actually shown, so it
    never needs revisiting. Self-suppressing: each record has to beat the last.
    A route with no history has no floor and so cannot set a record, which is
    what keeps the first run on a new route quiet.
    """
    if floor is None:
        return None
    priced = [v for v in verdicts if v.quote.price is not None]
    if not priced:
        return None

    best = min(priced, key=lambda v: v.quote.price)
    if best.quote.price >= floor:
        return None

    best.reasons.append(f"cheapest ever recorded here - previous best was "
                        f"{best.quote.currency} {floor:,.0f}")
    best.is_deal = True
    return best
