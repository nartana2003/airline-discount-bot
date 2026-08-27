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


# Same names the control panel's Stops dropdown uses, so a reason quotes back
# exactly the setting you picked rather than a re-derived paraphrase of it.
_STOPS_CEILING = {1: "nonstop only", 2: "1 stop or fewer", 3: "2 stops or fewer"}


def _fare_stops(n: int | None) -> str:
    """A fare's own stop count, in words - "nonstop", "1 stop", "2 stops"."""
    if n is None:
        return "an unknown number of stops"
    return "nonstop" if n == 0 else "1 stop" if n == 1 else f"{n} stops"


def _meets_stops(quote: Quote, watch: Watch) -> bool:
    """Whether this fare is within the route's stops preference.

    `watch.stops` follows SerpApi's own convention: 0 = any, 1 = nonstop only,
    2 = 1 stop or fewer, 3 = 2 stops or fewer - so it names a CEILING, and the
    ceiling on stop count is one less than the value itself (nonstop only = 0
    stops allowed). 0 means no ceiling, and always passes.

    The search itself no longer filters on this - see the comment on `stops`
    in provider._params(). Every fare gets journalled regardless, real market
    price and all; this is what keeps the ones outside the ceiling out of the
    two alerting rules below without ever hiding them from the record.
    """
    return watch.stops == 0 or quote.stops is None or quote.stops <= watch.stops - 1


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

    within_stops = _meets_stops(quote, watch)
    if not within_stops:
        # Still a real, priced fare - just not one that meets the preference.
        # Recorded and shown, not suppressed: a route with no matching service
        # at all should still show what a search there actually returns.
        reasons.append(f"{_fare_stops(quote.stops)} - outside your "
                       f"\"{_STOPS_CEILING[watch.stops]}\" setting")

    # Two rules, neither with a price in it. The second carries a percentile,
    # but a percentile is a position in the route's own history rather than a
    # figure that has to be retuned when fares structurally move.
    #
    # Google's verdict is built on real price history for this route and these
    # dates, and it recalibrates by season on its own. It catches fares that
    # are cheap *for when they are* - a December seat that is good for December.
    if within_stops and quote.price_level and quote.price_level.lower() in rules.alert_on_price_level:
        reasons.append(f"Google rates this fare '{quote.price_level}'")

    # The second rule needs every date for the route before it can be applied
    # - see mark_drop().

    return Verdict(
        quote=quote,
        # bool(reasons) alone would count the stops-preference note itself as
        # a deal - only the Google-verdict reason above should ever set this.
        is_deal=within_stops and bool(reasons),
        reasons=reasons,
        discount_pct=discount_pct,
    )


def mark_drop(verdicts: list[Verdict], baseline, watch: Watch | None = None) -> Verdict | None:
    """Flag the date that has fallen furthest below its own usual price.

    The second alerting rule, and the only one that can see a price *move*.
    Google grades a date against its own time of year, so it answers "is this
    good for December". This asks the question nothing else does: has this exact
    departure date got cheaper than this exact departure date normally is?

    It compares like with like, which is what makes a change visible. The
    previous version pooled every date into one list and took the cheapest
    tenth - and on a seasonal route that is simply the April dates, run after
    run. It fired on "it is April" while claiming to mean "this fare is cheap",
    it could not distinguish an April fare drifting UP from one falling, and a
    December collapse from 1,400 to 1,000 never came close to qualifying. See
    history.baselines().

    Ranked by how far each date is below its own normal, NOT by price. The
    biggest drop is often not the cheapest seat - a December fare down a third
    is news, while the April trough being cheap again is not - and reporting
    the cheapest would just re-announce the season every week.

    Applied once every date for the route has been priced, and to one fare
    only: several dates can sit under the bar at once, but they are one piece
    of news, and the steepest drop is the one worth reading.

    A route with too little history has no baseline, and a date that has only
    just entered the window has nothing to be compared against - both stay
    quiet rather than guess.

    `watch` gates candidates by stops preference the same way `evaluate()`
    does - see `_meets_stops()`. A route whose only real service is two stops
    should not have its cheapest 2-stop fare crowned "the drop of the week"
    just because nothing nonstop exists to compare it against. Optional and
    unfiltered when absent, matching evaluate() before this gate existed.
    """
    if baseline is None:
        return None

    scored = [
        (score, v) for v, score in
        ((v, baseline.score(v.quote.price, f"{v.quote.depart_date:%Y-%m-%d}"))
         for v in verdicts
         if v.quote.price is not None and (watch is None or _meets_stops(v.quote, watch)))
        if score is not None
    ]
    if not scored:
        return None

    score, best = min(scored, key=lambda pair: pair[0])
    if score >= baseline.ratio:
        return None

    was = baseline.typical[f"{best.quote.depart_date:%Y-%m-%d}"]
    best.reasons.append(
        f"down {(1 - score) * 100:.0f}% on what this date usually costs here "
        f"({best.quote.currency} {was:,.0f})")
    best.is_deal = True
    return best
