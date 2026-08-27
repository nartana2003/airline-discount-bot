"""Where alerts go: the terminal always, email when there's something to say.

Email is sent multipart - an HTML version for clients that render it, and a
plain-text fallback that stays readable on its own.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape as esc
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .config import EmailSettings, Watch
from .evaluate import Verdict

# ANSI colours, skipped when output isn't a terminal.
GREEN, DIM, BOLD, RESET = "\033[32m", "\033[2m", "\033[1m", "\033[0m"

CABINS = {1: "Economy", 2: "Premium economy", 3: "Business", 4: "First"}

ACCENT = "#2f6df6"
GOOD = "#17794a"
GOOD_BG = "#e7f5ee"
INK = "#14161a"
INK_2 = "#5c6470"
INK_3 = "#8b929c"
LINE = "#e2e5ea"
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif")


@dataclass(frozen=True)
class RouteAlert:
    """One route's worth of alertable fares, plus everything it searched.

    Alerts are batched across routes into a single email. Two routes serving
    the same city - BNE->NRT and BNE->HND - arriving as separate mails is both
    noisier and harder to compare, which defeats the point of watching both.
    """

    watch: Watch
    deals: list[Verdict]
    searched: list[Verdict]


def cheapest_of(verdicts: list[Verdict]) -> Verdict | None:
    """The single best-priced result, so a wall of DEALs still has a winner."""
    priced = [v for v in verdicts if v.quote.price is not None]
    return min(priced, key=lambda v: v.quote.price) if priced else None


def by_price(verdicts: list[Verdict]) -> list[Verdict]:
    """Cheapest first - a long list is useless in date order."""
    return sorted(verdicts, key=lambda v: (v.quote.price is None, v.quote.price or 0))


def _stops_text(stops: int | None) -> str:
    if stops is None:
        return ""
    if stops == 0:
        return "Nonstop"
    return "1 stop" if stops == 1 else f"{stops} stops"


def _trip_summary(watch: Watch, sep: str = " · ") -> str:
    """`sep` is ASCII-able for the terminal: a Windows console using cp1252
    can't encode '·' and would mangle the line."""
    cabin = CABINS.get(watch.travel_class, "Economy")
    who = "1 adult" if watch.adults == 1 else f"{watch.adults} adults"
    return sep.join([f"{watch.trip_days} day return", cabin, who])


def _scope_line(searched: list[Verdict] | None) -> str:
    """Deliberately empty.

    This used to spell out how many dates were sampled and over what range, to
    stop "cheapest" implying a whole-year sweep. It was a paragraph of caveat
    above every alert, and the caveat is in the README where it belongs.
    """
    return ""


def _google_verdict(v: Verdict) -> str:
    """Google's own price_level, shown even when it disagrees with our rules."""
    level = (v.quote.price_level or "").lower()
    return {"low": "Google rates this low",
            "typical": "Google rates this typical",
            "high": "Google rates this high"}.get(level, "")


def _saving(v: Verdict) -> str:
    """Distance below the typical midpoint, phrased as money rather than percent."""
    mid = v.quote.typical_mid
    if mid is None or v.quote.price is None or v.quote.price >= mid:
        return ""
    return f"{v.quote.currency} {mid - v.quote.price:,.0f} under typical"


def _under_pct(v: Verdict) -> int | None:
    """How far under the typical midpoint, as a whole percent."""
    mid = v.quote.typical_mid
    if mid is None or not mid or v.quote.price is None or v.quote.price >= mid:
        return None
    return round((mid - v.quote.price) / mid * 100)


def _duration(minutes: int | None) -> str:
    if not minutes:
        return ""
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _timeline(v: Verdict) -> str:
    """"09:10 BNE -> 17:55 NRT" for the outbound, with the stop count.

    Only the outbound: a round-trip search returns outbound itineraries, and
    the return leg would cost a second API call per fare.
    """
    legs = v.quote.legs
    if not legs:
        return ""
    first, last = legs[0], legs[-1]
    if not (first.depart_time and last.arrive_time):
        return ""
    line = (f"{first.depart_time} {first.from_id or ''} → "
            f"{last.arrive_time} {last.to_id or ''}").strip()
    dur = _duration(v.quote.duration_minutes)
    if dur:
        line += f"  ({dur})"
    return line


def _flight_numbers(v: Verdict) -> str:
    out = []
    for leg in v.quote.legs:
        if leg.flight_number:
            out.append(leg.flight_number.replace(" ", ""))
    return ", ".join(out)


# ---------------------------------------------------------------- terminal
def print_results(watch: Watch, verdicts: list[Verdict], colour: bool = True) -> None:
    g, d, b, r = (GREEN, DIM, BOLD, RESET) if colour else ("", "", "", "")
    print(f"\n{b}{watch.label}{r}  {d}{_trip_summary(watch, sep=' - ')}{r}")

    if not verdicts:
        print(f"  {d}no dates to search{r}")
        return

    best = cheapest_of(verdicts)
    for v in verdicts:
        marker = f"{g}DEAL{r}" if v.is_deal else f"{d}  - {r}"
        star = f" {g}<= cheapest{r}" if v is best and v.is_deal else ""
        print(f"  {marker} {v.headline}{star}")
        for reason in v.reasons:
            print(f"       {d}{reason}{r}")

    if best and best.quote.price is not None:
        print(f"\n  {b}Best of {len(verdicts)} dates:{r} {best.when} at "
              f"{best.quote.currency} {best.quote.price:,.0f}")


# ---------------------------------------------------------------- plain text
def _plain_body(groups: list[RouteAlert]) -> str:
    total = sum(len(g.deals) for g in groups)
    lines: list[str] = []
    if len(groups) > 1:
        lines += [f"{total} fare{'' if total == 1 else 's'} worth a look "
                  f"across {len(groups)} routes.", ""]
    for gi, g in enumerate(groups):
        if gi:
            lines += ["", "-" * 56, ""]
        lines += _plain_route(g)
    return "\n".join(lines).rstrip() + "\n"


def _plain_route(g: RouteAlert) -> list[str]:
    watch, ordered = g.watch, by_price(g.deals)
    lines = [
        f"{watch.origin} -> {watch.destination}   {_trip_summary(watch)}",
    ]
    scope = _scope_line(g.searched)
    if scope:
        lines.append(scope)
    lines.append("")
    for i, v in enumerate(ordered):
        q = v.quote
        tag = "CHEAPEST" if i == 0 and len(ordered) > 1 else "DEAL"
        saving = _saving(v)
        lines.append(f"[{tag}]  {q.currency} {q.price:,.0f}"
                     + (f"   {saving}" if saving else ""))
        lines.append(f"  {q.depart_date:%a %d %b %Y}  ->  {q.return_date:%a %d %b %Y}"
                     f"   ({q.probe.trip_days} days)")
        times = _timeline(v)
        if times:
            lines.append(f"  {times}  outbound")
        detail = " · ".join(x for x in [", ".join(q.airlines), _flight_numbers(v),
                                        _stops_text(q.stops)] if x)
        if detail:
            lines.append(f"  {detail}")
        if q.typical_low and q.typical_high:
            verdict = _google_verdict(v)
            usual = f"  Usually {q.currency} {q.typical_low:,.0f}-{q.typical_high:,.0f}"
            lines.append(usual + (f"   ({verdict})" if verdict else ""))
        for reason in v.reasons:
            lines.append(f"  - {reason}")
        if q.booking_url:
            lines.append(f"  Book: {_by_price(q.booking_url)}")
        lines.append("")

    return lines


# ---------------------------------------------------------------- html
def _card(v: Verdict, hero: bool) -> str:
    """One deal as an HTML block. Inline styles only - Gmail drops <style>."""
    q = v.quote
    border = GOOD if hero else LINE
    background = "#f2fbf6" if hero else "#ffffff"
    size = 30 if hero else 22

    label = ""
    if hero:
        label = (f'<div style="font-size:11px;font-weight:700;letter-spacing:.08em;'
                 f'color:{GOOD};text-transform:uppercase;margin-bottom:8px">Cheapest</div>')

    saving = _saving(v)
    saving_html = ""
    if saving:
        saving_html = (f'<div style="display:inline-block;margin-top:6px;padding:3px 9px;'
                       f'border-radius:99px;background:{GOOD_BG};color:{GOOD};'
                       f'font-size:12px;font-weight:600">{esc(saving)}</div>')

    detail = " &middot; ".join(
        esc(x) for x in [", ".join(q.airlines), _flight_numbers(v),
                         _stops_text(q.stops)] if x
    )
    detail_html = (f'<div style="font-size:13.5px;color:{INK_2};margin-top:5px">{detail}</div>'
                   if detail else "")

    # The outbound clock times - the thing that decides whether a fare is
    # actually usable, and which used to be thrown away in the parser.
    timeline = _timeline(v)
    timeline_html = (
        f'<div style="font-size:14px;color:{INK};margin-top:8px;font-weight:600;'
        f'font-variant-numeric:tabular-nums">{esc(timeline)}'
        f'<span style="font-weight:400;color:{INK_3}">&nbsp;&nbsp;outbound</span></div>'
        if timeline else "")

    usual_html = ""
    if q.typical_low and q.typical_high:
        verdict = _google_verdict(v)
        tail = f' &middot; {esc(verdict)}' if verdict else ""
        usual_html = (f'<div style="font-size:12.5px;color:{INK_3};margin-top:3px">'
                      f'Usually {q.currency} {q.typical_low:,.0f}&ndash;{q.typical_high:,.0f}'
                      f'{tail}</div>')

    reasons = "".join(
        f'<div style="font-size:13px;color:{INK_2};margin-top:3px">&bull; {esc(r)}</div>'
        for r in v.reasons
    )

    button = ""
    if q.booking_url:
        button = (f'<div style="margin-top:16px"><a href="{esc(_by_price(q.booking_url))}" '
                  f'target="_blank" rel="noopener" '
                  f'style="display:inline-block;padding:10px 20px;border-radius:8px;'
                  f'background:{ACCENT};color:#ffffff;font-size:14px;font-weight:600;'
                  f'text-decoration:none">View on Google Flights</a></div>')

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid {border};border-radius:10px;background:{background};'
        f'margin:0 0 12px"><tr><td style="padding:18px 20px">'
        f'{label}'
        f'<div style="font-size:{size}px;font-weight:700;color:{INK};line-height:1.15">'
        f'{q.currency} {q.price:,.0f}</div>'
        f'{saving_html}'
        f'<div style="margin-top:14px;font-size:15px;color:{INK};font-weight:600">'
        f'{q.depart_date:%a %d %b} &rarr; {q.return_date:%a %d %b %Y}'
        f'<span style="font-weight:400;color:{INK_2}">&nbsp;&nbsp;{q.probe.trip_days} days</span></div>'
        f'{timeline_html}{detail_html}{usual_html}{reasons}{button}'
        f'</td></tr></table>'
    )


def _row(v: Verdict) -> str:
    """One runner-up as a single compact line rather than a full card.

    Ten stacked cards is a wall to scroll past; the cheapest deserves the
    space, the rest just need to be scannable.
    """
    q = v.quote
    # Airports are already in the section heading, so the row only needs the
    # clock - that keeps room for the link, which is the point of the row.
    legs = q.legs
    clock = (f"{legs[0].depart_time} → {legs[-1].arrive_time}"
             if legs and legs[0].depart_time and legs[-1].arrive_time else "")
    link = (f'<a href="{esc(_by_price(q.booking_url))}" target="_blank" rel="noopener" style="color:{ACCENT};'
            f'text-decoration:none;font-weight:600;white-space:nowrap">View &rarr;</a>'
            if q.booking_url else "")
    return (
        f'<tr>'
        f'<td style="padding:9px 12px 9px 0;font-size:15px;font-weight:700;'
        f'color:{INK};white-space:nowrap;font-variant-numeric:tabular-nums">'
        f'{q.currency} {q.price:,.0f}</td>'
        f'<td style="padding:9px 12px 9px 0;font-size:13px;color:{INK_2};white-space:nowrap">'
        f'{q.depart_date:%a %d %b} &rarr; {q.return_date:%d %b}</td>'
        f'<td style="padding:9px 12px 9px 0;font-size:12.5px;color:{INK_3};'
        f'white-space:nowrap;font-variant-numeric:tabular-nums">{esc(clock)}</td>'
        f'<td style="padding:9px 12px 9px 0;font-size:12.5px;color:{INK_3};'
        f'white-space:nowrap">{esc(", ".join(q.airlines))}</td>'
        f'<td style="padding:9px 0;font-size:12.5px;text-align:right">{link}</td>'
        f'</tr>'
    )


def _html_route(g: RouteAlert, first: bool) -> str:
    """One route's block: its heading, then its fares."""
    watch, ordered = g.watch, by_price(g.deals)
    hero = _card(ordered[0], hero=True)

    # The rest collapse where the client supports <details>. Gmail strips it and
    # renders the contents open - which is why they are compact rows and not
    # cards: the fallback has to be acceptable on its own, since no email client
    # runs the JavaScript a real toggle would need.
    rest = ""
    if len(ordered) > 1:
        n = len(ordered) - 1
        rows = "".join(_row(v) for v in ordered[1:])
        rest = (
            f'<details style="margin-top:6px">'
            f'<summary style="cursor:pointer;font-size:13px;font-weight:600;'
            f'color:{ACCENT};padding:10px 0;list-style:none">'
            f'{n} other date{"" if n == 1 else "s"} that also qualified</summary>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-top:1px solid {LINE};margin-top:4px">'
            f'{rows}</table></details>'
        )

    scope = _scope_line(g.searched)
    scope_html = ""
    if scope:
        scope_html = (f'<div style="font-size:12px;color:{INK_3};margin-top:8px;'
                      f'line-height:1.45">{esc(scope)}</div>')

    # A rule above every route but the first, so stacked routes read as
    # separate sections rather than one run-on list.
    top = "0 0 18px" if first else "26px 0 18px"
    divider = ("" if first else
               f'border-top:1px solid {LINE};')

    return (
        f'<tr><td style="padding:{top};{divider}">'
        f'<div style="font-size:19px;font-weight:700;color:{INK};padding-top:'
        f'{"0" if first else "22px"}">'
        f'{esc(watch.origin)} &rarr; {esc(watch.destination)}</div>'
        f'<div style="font-size:13px;color:{INK_2};margin-top:3px">'
        f'{esc(_trip_summary(watch))}</div>{scope_html}</td></tr>'
        f'<tr><td>{hero}{rest}</td></tr>'
    )


def _html_body(groups: list[RouteAlert]) -> str:
    total = sum(len(g.deals) for g in groups)
    lead = ""
    if len(groups) > 1:
        lead = (
            f'<tr><td style="padding:0 0 20px">'
            f'<div style="font-size:20px;font-weight:700;color:{INK}">'
            f'{total} fare{"" if total == 1 else "s"} worth a look</div>'
            f'<div style="font-size:13px;color:{INK_2};margin-top:3px">'
            f'across {len(groups)} routes you are watching</div></td></tr>'
        )
    sections = "".join(_html_route(g, first=(i == 0 and not lead))
                       for i, g in enumerate(groups))

    return (
        f'<!doctype html><html><body style="margin:0;padding:0;background:#f6f7f9">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f6f7f9"><tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="width:100%;max-width:600px;font-family:{FONT}">'
        f'{lead}{sections}'
        f'</table></td></tr></table></body></html>'
    )


def _subject(groups: list[RouteAlert]) -> str:
    """Front-load the price, then say why it's worth opening.

    A subject is read at a glance in a list, and phone clients cut it around
    35 characters - so the best price and its route come first, and the reason
    it qualified comes next. Across several routes the headline is the single
    cheapest fare found anywhere, not a list of them.
    """
    every = [(g.watch, v) for g in groups for v in g.deals]
    watch, best = min(every, key=lambda pair: pair[1].quote.price)
    q = best.quote

    parts = [f"{q.currency} {q.price:,.0f} {watch.origin}→{watch.destination}",
             f"{q.depart_date:%a %d %b}"]
    pct = _under_pct(best)
    if pct:
        parts.append(f"{pct}% under typical")
    elif q.price_level:
        parts.append(f"Google: {q.price_level}")

    line = " · ".join(parts)
    if len(every) > 1:
        line += f" (+{len(every) - 1})"
    return line


# ---------------------------------------------------------------- digest
# Deliberately plain, and deliberately different from an alert: this message
# is not asking you to do anything. Its whole job is to make the difference
# between "nothing was cheap" and "the bot has been broken for six weeks"
# something you can see instead of assume.

def _digest_when(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%d %b")
    except ValueError:
        return iso[:10]


def _pretty_date(ymd: str | None, year: bool = False) -> str:
    """"2027-05-18" -> "Tue 18 May". Nobody reads ISO dates for pleasure."""
    if not ymd:
        return ""
    try:
        d = datetime.strptime(ymd, "%Y-%m-%d")
    except ValueError:
        return ymd
    return d.strftime("%a %d %b %Y" if year else "%a %d %b")


def _labels(watches: list[Watch] | None) -> dict[str, str]:
    """watch id -> "BNE → NRT". The id is a slug for the machine, not a name."""
    return {w.id: f"{w.origin} → {w.destination}" for w in (watches or [])}


def _route_label(row: dict, labels: dict[str, str]) -> str:
    """Name a route, even one that no longer exists.

    Deleting a route from watches.json does not delete what it recorded, so the
    digest can outlive the watch that produced the rows. With only the current
    watchlist to look up, those fell back to the raw slug - a digest line
    reading "bne-hnd" beside a real fare. The airports are on the row itself,
    so the name can be rebuilt without the watch.
    """
    watch_id = row.get("watch", "")
    if watch_id in labels:
        return labels[watch_id]
    origin, dest = row.get("from_id"), row.get("to_id")
    if origin and dest:
        return f"{origin} → {dest}"
    # Last resort: a slug like "bne-hnd" still reads better upper-cased.
    if "-" in watch_id:
        a, _, b = watch_id.partition("-")
        return f"{a.upper()} → {b.upper()}"
    return watch_id


# Google Flights carries its result ordering in the `tfu` query parameter, as
# a two-field protobuf: EgIIAQ decodes to 12 02 08 01 - field 1 = 1, "Top
# flights" - and EgIIAg to 12 02 08 02, field 1 = 2, "Price". Those numbers are
# SerpApi's documented sort_by values, and the link SerpApi hands back always
# carries the default.
#
# Top flights blends price with convenience, so the fare in the email is often
# several rows down the page and has to be hunted for. Sorting by price puts it
# at or near the top. Rewriting the parameter is enough - the search itself is
# encoded in `tfs`, which is untouched, so this changes only what the page is
# ordered by and never which flights it shows.
PRICE_SORTED_TFU = "EgIIAg"


def _by_price(url: str) -> str:
    """Point a Google Flights link at a price-sorted page.

    Anything unexpected is handed back unchanged: a link that lands on the
    right search unsorted is worth far more than one that breaks.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        if "google.com" not in parts.netloc or "tfs=" not in parts.query:
            return url
        params = parse_qsl(parts.query, keep_blank_values=True)
        params = [(k, v) for k, v in params if k != "tfu"]
        params.append(("tfu", PRICE_SORTED_TFU))
        return urlunsplit(parts._replace(query=urlencode(params)))
    except ValueError:
        return url


def _search_url(origin: str, dest: str, depart: str, ret: str) -> str:
    """A Google Flights link built from the route and dates alone.

    The API hands back a URL per search and it is stored with each quote, but
    rows written before that field existed have none - and a digest whose
    every fare is a dead end is the thing worth avoiding. This is derivable
    from what every row has always carried, so the button is never missing.
    """
    q = f"Flights from {origin} to {dest} on {depart} through {ret}"
    return "https://www.google.com/travel/flights?q=" + quote(q)


def _row_url(r: dict, watches: list[Watch] | None) -> str:
    """The stored link if there is one, otherwise one built from the route."""
    if r.get("url"):
        return _by_price(r["url"])
    origin, dest = r.get("from_id"), r.get("to_id")
    if not (origin and dest):
        w = next((w for w in (watches or []) if w.id == r.get("watch")), None)
        if w:
            origin, dest = w.origin, w.destination
    if origin and dest and r.get("depart") and r.get("ret"):
        return _search_url(origin, dest, r["depart"], r["ret"])
    return ""


def _digest_pair(d: dict, r: dict) -> tuple[dict, dict | None]:
    """(the fare to show, the period's low when that is a different fare).

    Today's price leads because it is the only one that can still be booked.
    The low is context underneath it. When the newest run IS the low there is
    only one fare, and printing the same number twice reads as two options.
    A route missing from the newest run - paused, or added since - has no
    today, so its low is all there is to show.
    """
    now = (d.get("current") or {}).get(r["watch"])
    if now is None:
        return r, None
    # The low is a minimum taken over every row including today's, so it can
    # only be lower than today or equal to it. Equal means the cheapest fare
    # on record is the one being quoted, and there is nothing to add.
    if r["price"] >= now["price"]:
        return now, None
    return now, r


def _low_line(low: dict, since: str | None = None) -> str:
    """"Lowest since 27 Jul: AUD 717 on 12 Aug · Tue 18 May -> Sun 30 May 2027".

    Named for the window it actually covers. This used to read "Lowest seen",
    which is the same phrase the alert email uses for the all-time record - but
    the digest only looks back to the previous digest. Two numbers called the
    same thing over different periods eventually disagree, and a digest quoting
    a "lowest" higher than a record you were emailed about reads as a bug.

    "this month" would be almost right and occasionally wrong: a missed run
    widens the window rather than losing the period. The date the window really
    starts is already in the digest, so it can just say so.
    """
    when = f"Lowest since {_digest_when(since)}" if since else "Lowest seen"
    return (f"{when}: {low['currency']} {low['price']:,.0f} on "
            f"{_digest_when(low.get('at'))} · {_pretty_date(low['depart'])} "
            f"→ {_pretty_date(low['ret'], year=True)}")


def _spread_line(sp: dict | None) -> str:
    """"Best of 26 dates sampled · 31% under the median date (AUD 1,015)".

    The "when should I fly" answer, and the reason it belongs in the digest
    rather than in an alert: on a seasonal route the cheapest sampled date is
    always well below the median, every run, whether or not anything moved.
    Monthly it is the shape of the year; weekly it would be wallpaper.
    """
    if not sp or not sp.get("under_pct"):
        return ""
    return (f"Best of {sp['dates']} dates sampled · {sp['under_pct']}% under "
            f"the median date ({sp['currency']} {sp['median']:,.0f})")


def _month_label(ymd: str | None) -> str:
    """"2027-04-30" -> "Apr 2027"."""
    try:
        return datetime.strptime(str(ymd)[:7] + "-01", "%Y-%m-%d").strftime("%b %Y")
    except ValueError:
        return str(ymd or "")[:7]


def _months_plain(rows: list[dict]) -> list[str]:
    """The shape of the year: cheapest date in each departure month.

    The spread line says how good the best date is. This says WHEN the good
    months are, which is what you need to pick when to travel rather than
    whether to book today.
    """
    if not rows:
        return []
    best = min(r["price"] for r in rows)
    out = ["    Cheapest date in each month:"]
    for r in rows:
        mark = "  <- cheapest" if r["price"] == best else ""
        when = f"{_pretty_date(r['depart'])} -> {_pretty_date(r.get('ret'), year=True)}"
        out.append(f"      {_month_label(r['depart']):<9}"
                   f"{r['currency']} {r['price']:>6,.0f}   "
                   f"{when}{mark}")
    return out


def _months_html(rows: list[dict], watches: list[Watch] | None) -> str:
    """Same table, with every month's fare linked.

    These come from the newest run, so each one is a price that can still be
    booked - unlike the period low, which is deliberately left unlinked because
    a link beside a fare that has gone reads as an offer.
    """
    if not rows:
        return ""
    best = min(r["price"] for r in rows)
    cells = []
    for r in rows:
        top = r["price"] == best
        price = f'{r["currency"]} {r["price"]:,.0f}'
        url = _row_url(r, watches)
        if url:
            price = (f'<a href="{esc(url)}" target="_blank" rel="noopener" '
                     f'style="color:{ACCENT};text-decoration:none">{price}</a>')
        weight = "700" if top else "400"
        colour = GOOD if top else INK
        cells.append(
            f'<tr>'
            f'<td style="padding:3px 12px 3px 0;font-size:13px;color:{INK_2};'
            f'white-space:nowrap">{esc(_month_label(r["depart"]))}</td>'
            f'<td style="padding:3px 12px 3px 0;font-size:13px;font-weight:{weight};'
            f'color:{colour};white-space:nowrap;font-variant-numeric:tabular-nums">{price}</td>'
            f'<td style="padding:3px 0;font-size:13px;color:{INK_3};'
            f'white-space:nowrap">{esc(_pretty_date(r["depart"]))} &rarr; '
            f'{esc(_pretty_date(r.get("ret"), year=True))}</td>'
            f'</tr>')
    return (f'<div style="font-size:11px;font-weight:700;letter-spacing:.06em;'
            f'color:{INK_3};text-transform:uppercase;margin:16px 0 6px">'
            f'Cheapest date in each month</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0">'
            f'{"".join(cells)}</table>')


def _digest_subject(d: dict, labels: dict[str, str] | None = None) -> str:
    """Say the useful thing, not the internal thing.

    "N alerts sent" was jargon: the reader does not think in alerts, and their
    inbox already told them how many arrived. What they want from a monthly
    check-in is the best price seen and whether anything is wrong.
    """
    labels = labels or {}
    best = d["routes"][0] if d["routes"] else None

    if not best:
        # The shape a broken run takes - phrase it so it reads as a problem.
        return "Flight Watch · no fares found all month — worth a look"

    # routes[0] is sorted on the headline, so quote the headline fare.
    best = (d.get("current") or {}).get(best["watch"]) or best
    where = _route_label(best, labels)
    price = f"{best['currency']} {best['price']:,.0f}"
    if d["deals"]:
        n = d["deals"]
        return f"Flight Watch · {n} cheap fare{'' if n == 1 else 's'} this month · best {price} {where}"
    return f"Flight Watch · nothing cheap yet · best {price} {where}"


def _row_detail(r: dict) -> str:
    """"09:30 BNE → 17:40 NRT (9h 10m) · Jetstar JQ9" from a stored row.

    Rows written before these fields existed simply have less to say, so each
    piece is included only when present rather than assumed.
    """
    bits = []
    if r.get("dep_time") and r.get("arr_time"):
        leg = f"{r['dep_time']} {r.get('from_id') or ''} → {r['arr_time']} {r.get('to_id') or ''}"
        dur = _duration(r.get("duration"))
        bits.append(f"{' '.join(leg.split())}" + (f" ({dur})" if dur else ""))
    carrier = ", ".join(r.get("airlines") or [])
    nums = ", ".join(n.replace(" ", "") for n in (r.get("flights") or []))
    if carrier or nums:
        bits.append(" ".join(x for x in (carrier, nums) if x))
    return " · ".join(bits)


def _digest_plain(d: dict, labels: dict[str, str] | None = None,
                  watches: list[Watch] | None = None) -> str:
    labels = labels or {}
    lines = ["Still watching.", ""]
    if d["routes"]:
        for r in d["routes"]:
            now, low = _digest_pair(d, r)
            where = _route_label(now, labels)
            # Per route, not one heading for the whole digest: a route missing
            # from the latest run (paused, or just added) falls back to its
            # period low even while a heading up top said "now" for everyone.
            is_now = (d.get("current") or {}).get(now["watch"]) is not None
            lines.append("CHEAPEST FLIGHT ON THIS ROUTE, RIGHT NOW" if is_now
                        else "CHEAPEST FLIGHT SEEN ON THIS ROUTE THIS PERIOD")
            lines.append(f"  {where}   {now['currency']} {now['price']:,.0f}"
                         f"   ({now['level'] or 'unrated'})")
            lines.append(f"    {_pretty_date(now['depart'])} → {_pretty_date(now['ret'], year=True)}")
            detail = _row_detail(now)
            if detail:
                lines.append(f"    {detail}")
            # Only today's fare gets a link. The low is a fact about the past;
            # a link beside it would offer a trip at a price that has gone.
            url = _row_url(now, watches)
            if url:
                lines.append(f"    {url}")
            if low:
                lines.append(f"    {_low_line(low, d.get('first_run'))}")
            sp = _spread_line((d.get("spread") or {}).get(now["watch"]))
            if sp:
                lines.append(f"    {sp}")
            lines += _months_plain((d.get("months") or {}).get(now["watch"], []))
            lines.append("")
    else:
        lines += ["Not one search returned a fare this month.",
                  "That usually means a bad airport code or an API problem,",
                  "not an expensive route — worth checking.", ""]

    # No alert count - the inbox already said that. No quota line, no footer
    # explaining why the mail exists. Only the quiet case needs a word, or the
    # fares below look unexplained.
    if not d["deals"] and d["routes"]:
        rated = ", ".join(f"{n} {lv}" for lv, n in d["levels"].items())
        lines.append(f"Nothing hit 'low' — these are just the cheapest seen. "
                     f"Google rated them: {rated}.")
    if d["empty"]:
        lines.append(f"{d['empty']} date pair(s) came back with no fares at all.")
    return "\n".join(lines).rstrip() + "\n"


def _digest_html(d: dict, labels: dict[str, str] | None = None,
                 watches: list[Watch] | None = None) -> str:
    labels = labels or {}

    cards = []
    for r in d["routes"]:
        now, low = _digest_pair(d, r)
        where = _route_label(now, labels)
        # See the plain-text renderer: whether THIS route's price is from the
        # latest run or a fallback to its period low, not a whole-digest guess.
        is_now = (d.get("current") or {}).get(now["watch"]) is not None
        route_label = ('Cheapest flight on this route, right now' if is_now
                      else 'Cheapest flight seen on this route this period')
        detail = _row_detail(now)
        detail_html = (
            f'<div style="font-size:13.5px;color:{INK_2};margin-top:5px">{esc(detail)}</div>'
            if detail else "")
        # Plain text, no link: this fare is history, and a button beside it
        # would sell a trip at a price that is gone.
        low_html = (
            f'<div style="font-size:12.5px;color:{INK_3};margin-top:6px">'
            f'{esc(_low_line(low, d.get("first_run")))}</div>' if low else "")
        months_html = _months_html((d.get("months") or {}).get(now["watch"], []), watches)
        spread_text = _spread_line((d.get("spread") or {}).get(now["watch"]))
        spread_html = (
            f'<div style="font-size:12.5px;color:{INK_3};margin-top:6px">'
            f'{esc(spread_text)}</div>' if spread_text else "")
        url = _row_url(now, watches)
        button = (
            f'<div style="margin-top:16px"><a href="{esc(url)}" target="_blank" rel="noopener" '
            f'style="display:inline-block;padding:10px 20px;border-radius:8px;'
            f'background:{ACCENT};color:#ffffff;font-size:14px;font-weight:600;'
            f'text-decoration:none">View on Google Flights</a></div>'
            if url else "")
        level = now.get("level")
        chip = (f'<span style="display:inline-block;padding:3px 9px;border-radius:99px;'
                f'background:{GOOD_BG};color:{GOOD};font-size:12px;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:.04em">{esc(level)}</span>'
                if level == "low" else
                f'<span style="font-size:12.5px;color:{INK_3}">Google: {esc(level or "unrated")}</span>')

        cards.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid {LINE};border-radius:10px;background:#ffffff;'
            f'margin:0 0 12px"><tr><td style="padding:18px 20px">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:.06em;'
            f'color:{INK_3};text-transform:uppercase;margin-bottom:6px">'
            f'{esc(route_label)}</div>'
            f'<div style="font-size:14px;font-weight:700;color:{INK}">{esc(where)}</div>'
            f'<div style="margin-top:10px">'
            f'<span style="font-size:22px;font-weight:700;color:{INK};line-height:1.15">'
            f'{now["currency"]} {now["price"]:,.0f}</span>'
            f'&nbsp;&nbsp;{chip}</div>'
            f'<div style="font-size:15px;color:{INK};margin-top:14px;font-weight:600">'
            f'{esc(_pretty_date(now["depart"]))} &rarr; {esc(_pretty_date(now["ret"], year=True))}</div>'
            f'{detail_html}{low_html}{spread_html}{button}{months_html}</td></tr></table>'
        )

    if cards:
        body = "".join(cards)
    else:
        body = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #f0c9c4;border-radius:10px;background:#fdf4f3">'
            f'<tr><td style="padding:18px 20px">'
            f'<div style="font-size:15px;font-weight:700;color:#8c2f26">'
            f'Not one search returned a fare this month.</div>'
            f'<div style="font-size:13.5px;color:{INK_2};margin-top:6px;line-height:1.5">'
            f'That usually means a bad airport code or an API problem rather than an '
            f'expensive route &mdash; worth checking.</div>'
            f'</td></tr></table>')

    if not d["deals"] and d["routes"]:
        rated = ", ".join(f"{c} {lv}" for lv, c in d["levels"].items())
        note = (f'Nothing hit &ldquo;low&rdquo; &mdash; these are just the cheapest '
                f'seen. Google rated them: {esc(rated)}.')
    else:
        note = ""
    if d["empty"]:
        note += (f'<br>{d["empty"]} date pair(s) came back with no fares at all.')

    note_html = (f'<div style="font-size:13px;color:{INK_2};line-height:1.5;'
                 f'margin-top:4px">{note}</div>') if note else ""

    return (
        f'<!doctype html><html><body style="margin:0;padding:0;background:#f6f7f9">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f6f7f9"><tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="width:100%;max-width:600px;font-family:{FONT}">'
        f'<tr><td style="padding:0 0 20px">'
        f'<div style="font-size:20px;font-weight:700;color:{INK}">Still watching</div>'
        f'</td></tr>'
        f'<tr><td>{body}</td></tr>'
        f'<tr><td style="padding:6px 0 0">{note_html}</td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def check_login(settings: EmailSettings) -> None:
    """Connect and authenticate, sending nothing. Raises on failure.

    Called before a run spends any credits. Bad SMTP credentials are one of the
    two ways this bot has actually broken in the wild, and they used to surface
    only at the very end - after a full run's quota had been spent deciding
    which fares to send. The failure is identical either way; the difference is
    whether it costs you the month's searches to find out.
    """
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.host, settings.port, context=context) as server:
        server.login(settings.user, settings.password)


def render_digest(d: dict, watches: list[Watch] | None = None) -> tuple[str, str]:
    """(subject, html) - what send_digest would mail, without sending it.

    For the control panel's preview: same rendering code a real send uses, so
    the preview can never disagree with the email, without exposing the
    module's internal helpers to callers outside it.
    """
    labels = _labels(watches)
    return _digest_subject(d, labels), _digest_html(d, labels, watches)


def send_digest(settings: EmailSettings, d: dict,
                watches: list[Watch] | None = None) -> None:
    """Raises on failure so the caller can report it rather than fail silently."""
    labels = _labels(watches)
    msg = EmailMessage()
    msg["Subject"] = _digest_subject(d, labels)
    msg["From"] = settings.user
    msg["To"] = settings.to
    msg.set_content(_digest_plain(d, labels, watches))
    msg.add_alternative(_digest_html(d, labels, watches), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.host, settings.port, context=context) as server:
        server.login(settings.user, settings.password)
        server.send_message(msg)


def send_email(settings: EmailSettings, groups: list[RouteAlert]) -> None:
    """One email covering every route that found something.

    Raises on failure so the caller can report it rather than fail silently.
    """
    groups = [g for g in groups if g.deals and cheapest_of(g.deals) is not None]
    if not groups:
        return

    msg = EmailMessage()
    msg["Subject"] = _subject(groups)
    msg["From"] = settings.user
    msg["To"] = settings.to
    msg.set_content(_plain_body(groups))
    msg.add_alternative(_html_body(groups), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.host, settings.port, context=context) as server:
        server.login(settings.user, settings.password)
        server.send_message(msg)
