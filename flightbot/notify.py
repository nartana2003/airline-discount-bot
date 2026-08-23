"""Where alerts go: the terminal always, email when there's something to say.

Email is sent multipart - an HTML version for clients that render it, and a
plain-text fallback that stays readable on its own.
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from html import escape as esc

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
    """State how much was actually looked at.

    A run samples dates rather than covering them, so "cheapest" means cheapest
    of what was checked. Without this the email implies a whole-year sweep.
    """
    if not searched:
        return ""
    days = sorted(v.quote.depart_date for v in searched)
    n = len(searched)
    return (f"Cheapest of {n} departure date{'' if n == 1 else 's'} checked "
            f"between {days[0]:%d %b} and {days[-1]:%d %b %Y} — not every date "
            f"in that range was searched.")


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
def _plain_body(watch: Watch, verdicts: list[Verdict],
                searched: list[Verdict] | None = None) -> str:
    ordered = by_price(verdicts)
    lines = [
        f"{watch.origin} -> {watch.destination}   {_trip_summary(watch)}",
    ]
    scope = _scope_line(searched)
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
            lines.append(f"  Book: {q.booking_url}")
        lines.append("")

    lines.append("-- ")
    lines.append("Your flight watch bot. Prices move; check before booking.")
    return "\n".join(lines)


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
        button = (f'<div style="margin-top:16px"><a href="{esc(q.booking_url)}" '
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
    times = _timeline(v)
    return (
        f'<tr>'
        f'<td style="padding:9px 12px 9px 0;font-size:15px;font-weight:700;'
        f'color:{INK};white-space:nowrap;font-variant-numeric:tabular-nums">'
        f'{q.currency} {q.price:,.0f}</td>'
        f'<td style="padding:9px 12px 9px 0;font-size:13px;color:{INK_2};white-space:nowrap">'
        f'{q.depart_date:%a %d %b} &rarr; {q.return_date:%d %b}</td>'
        f'<td style="padding:9px 12px 9px 0;font-size:12.5px;color:{INK_3};'
        f'white-space:nowrap;font-variant-numeric:tabular-nums">{esc(times)}</td>'
        f'<td style="padding:9px 0;font-size:12.5px;color:{INK_3}">'
        f'{esc(", ".join(q.airlines))}</td>'
        f'</tr>'
    )


def _html_body(watch: Watch, verdicts: list[Verdict],
               searched: list[Verdict] | None = None) -> str:
    ordered = by_price(verdicts)
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

    cards = hero + rest

    scope = _scope_line(searched)
    scope_html = ""
    if scope:
        scope_html = (f'<div style="font-size:12px;color:{INK_3};margin-top:8px;'
                      f'line-height:1.45">{esc(scope)}</div>')

    more = ""

    return (
        f'<!doctype html><html><body style="margin:0;padding:0;background:#f6f7f9">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f6f7f9"><tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="width:100%;max-width:600px;font-family:{FONT}">'
        f'<tr><td style="padding:0 0 18px">'
        f'<div style="font-size:19px;font-weight:700;color:{INK}">'
        f'{esc(watch.origin)} &rarr; {esc(watch.destination)}</div>'
        f'<div style="font-size:13px;color:{INK_2};margin-top:3px">'
        f'{esc(_trip_summary(watch))}</div>{scope_html}</td></tr>'
        f'<tr><td>{more}{cards}</td></tr>'
        f'<tr><td style="padding:8px 4px 0">'
        f'<div style="font-size:12px;color:{INK_3};line-height:1.5">'
        f'Prices move &mdash; check before booking. Sent by your flight watch bot; '
        f'edit watches.json to change thresholds.</div>'
        f'</td></tr></table></td></tr></table></body></html>'
    )


def _subject(watch: Watch, verdicts: list[Verdict]) -> str:
    """Front-load the price, then say why it's worth opening.

    A subject is read at a glance in a list, and phone clients cut it around
    35 characters - so price and route come first, and the reason it qualified
    comes next. "+N more" was the old tail and said nothing about whether the
    mail was worth opening.
    """
    best = cheapest_of(verdicts)
    q = best.quote
    head = f"{q.currency} {q.price:,.0f} {watch.origin}→{watch.destination}"
    parts = [head, f"{q.depart_date:%a %d %b}"]

    pct = _under_pct(best)
    if pct:
        parts.append(f"{pct}% under typical")
    elif q.price_level:
        parts.append(f"Google: {q.price_level}")

    line = " · ".join(parts)
    if len(verdicts) > 1:
        line += f" (+{len(verdicts) - 1})"
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
        return datetime.fromisoformat(iso).strftime("%d %b %Y")
    except ValueError:
        return iso[:10]


def _digest_subject(d: dict) -> str:
    best = d["routes"][0] if d["routes"] else None
    head = "Flight Watch · still running"
    if d["deals"]:
        return f"{head} · {d['deals']} alert{'' if d['deals'] == 1 else 's'} sent"
    if best:
        return f"{head} · nothing cheap · best was {best['currency']} {best['price']:,.0f}"
    return f"{head} · no fares found"


def _digest_plain(d: dict, quota: str) -> str:
    lines = [
        "Still watching. Nothing here needs your attention.",
        "",
        f"{_digest_when(d['first_run'])} – {_digest_when(d['last_run'])}   "
        f"{d['runs']} run{'' if d['runs'] == 1 else 's'}, {d['searches']} searches",
        "",
    ]
    if d["routes"]:
        lines.append("Cheapest seen on each route:")
        for r in d["routes"]:
            lines.append(f"  {r['watch']}   {r['currency']} {r['price']:,.0f}"
                         f"   {r['depart']} -> {r['ret']}   (Google: {r['level'] or '?'})")
    else:
        lines.append("No priced fares were returned at all this period.")
    lines.append("")

    if d["deals"]:
        lines.append(f"{d['deals']} fare(s) rated 'low' and were emailed to you.")
    else:
        rated = ", ".join(f"{n} {lv}" for lv, n in d["levels"].items()) or "none"
        lines.append(f"Nothing was rated 'low', so nothing was emailed. "
                     f"Google rated what it saw: {rated}.")
    if d["empty"]:
        lines.append(f"{d['empty']} date pair(s) returned no fares at all.")
    lines += ["", quota, "",
              "-- ",
              "This message exists so that silence means 'still working'",
              "rather than 'possibly broken'. It arrives once a month."]
    return "\n".join(lines)


def _digest_html(d: dict, quota: str) -> str:
    rows = "".join(
        f'<tr>'
        f'<td style="padding:7px 14px 7px 0;font-size:14px;color:{INK};font-weight:600">'
        f'{esc(r["watch"])}</td>'
        f'<td style="padding:7px 14px 7px 0;font-size:15px;color:{INK};'
        f'font-weight:700;white-space:nowrap">{r["currency"]} {r["price"]:,.0f}</td>'
        f'<td style="padding:7px 14px 7px 0;font-size:13px;color:{INK_2};white-space:nowrap">'
        f'{esc(r["depart"])} &rarr; {esc(r["ret"])}</td>'
        f'<td style="padding:7px 0;font-size:12.5px;color:{INK_3}">'
        f'{esc(r["level"] or "?")}</td>'
        f'</tr>'
        for r in d["routes"]
    )
    table = (f'<table role="presentation" cellpadding="0" cellspacing="0" '
             f'style="width:100%;margin:6px 0 0">{rows}</table>') if rows else (
             f'<div style="font-size:14px;color:{INK_2}">'
             f'No priced fares were returned at all this period.</div>')

    verdict = (f'{d["deals"]} fare(s) rated &ldquo;low&rdquo; and were emailed to you.'
               if d["deals"] else
               'Nothing was rated &ldquo;low&rdquo;, so nothing was emailed.')

    return (
        f'<!doctype html><html><body style="margin:0;padding:0;background:#f6f7f9">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#f6f7f9"><tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="560" cellpadding="0" cellspacing="0" '
        f'style="width:100%;max-width:560px;font-family:{FONT}">'
        f'<tr><td style="padding:0 0 6px">'
        f'<div style="font-size:18px;font-weight:700;color:{INK}">Still watching</div>'
        f'<div style="font-size:13px;color:{INK_2};margin-top:4px">'
        f'{esc(_digest_when(d["first_run"]))} &ndash; {esc(_digest_when(d["last_run"]))}'
        f' &middot; {d["runs"]} run{"" if d["runs"] == 1 else "s"}'
        f' &middot; {d["searches"]} searches</div></td></tr>'
        f'<tr><td style="padding:16px 0 0">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:.08em;'
        f'color:{INK_3};text-transform:uppercase">Cheapest seen</div>{table}</td></tr>'
        f'<tr><td style="padding:18px 0 0">'
        f'<div style="font-size:13.5px;color:{INK_2};line-height:1.55">{verdict}</div>'
        f'<div style="font-size:12.5px;color:{INK_3};margin-top:6px">{esc(quota)}</div>'
        f'</td></tr>'
        f'<tr><td style="padding:20px 0 0">'
        f'<div style="font-size:12px;color:{INK_3};line-height:1.5;'
        f'border-top:1px solid {LINE};padding-top:12px">'
        f'This message exists so that silence means &ldquo;still working&rdquo; rather '
        f'than &ldquo;possibly broken&rdquo;. It arrives once a month.</div>'
        f'</td></tr></table></td></tr></table></body></html>'
    )


def send_digest(settings: EmailSettings, d: dict, quota: str) -> None:
    """Raises on failure so the caller can report it rather than fail silently."""
    msg = EmailMessage()
    msg["Subject"] = _digest_subject(d)
    msg["From"] = settings.user
    msg["To"] = settings.to
    msg.set_content(_digest_plain(d, quota))
    msg.add_alternative(_digest_html(d, quota), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.host, settings.port, context=context) as server:
        server.login(settings.user, settings.password)
        server.send_message(msg)


def send_email(settings: EmailSettings, watch: Watch, verdicts: list[Verdict],
               searched: list[Verdict] | None = None) -> None:
    """Raises on failure so the caller can report it rather than fail silently."""
    if not verdicts or cheapest_of(verdicts) is None:
        return

    msg = EmailMessage()
    msg["Subject"] = _subject(watch, verdicts)
    msg["From"] = settings.user
    msg["To"] = settings.to
    msg.set_content(_plain_body(watch, verdicts, searched))
    msg.add_alternative(_html_body(watch, verdicts, searched), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.host, settings.port, context=context) as server:
        server.login(settings.user, settings.password)
        server.send_message(msg)
