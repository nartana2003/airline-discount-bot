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

    # The only rule. Google's verdict is built on real price history for this
    # route and these dates, and it recalibrates by season on its own - so
    # there is nothing here to tune, and nothing that goes stale as fares move.
    if quote.price_level and quote.price_level.lower() in rules.alert_on_price_level:
        reasons.append(f"Google rates this fare '{quote.price_level}'")

    return Verdict(
        quote=quote,
        is_deal=bool(reasons),
        reasons=reasons,
        discount_pct=discount_pct,
    )
