"""Where alerts go: the terminal always, email when there's something to say.

Email is sent multipart - an HTML version for clients that render it, and a
plain-text fallback that stays readable on its own.
"""

from __future__ import annotations

import smtplib
import ssl
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
        detail = " · ".join(x for x in [", ".join(q.airlines), _stops_text(q.stops)] if x)
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
        esc(x) for x in [", ".join(q.airlines), _stops_text(q.stops)] if x
    )
    detail_html = (f'<div style="font-size:13.5px;color:{INK_2};margin-top:5px">{detail}</div>'
                   if detail else "")

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
        f'{detail_html}{usual_html}{reasons}{button}'
        f'</td></tr></table>'
    )


def _html_body(watch: Watch, verdicts: list[Verdict],
               searched: list[Verdict] | None = None) -> str:
    ordered = by_price(verdicts)
    cards = "".join(_card(v, hero=(i == 0)) for i, v in enumerate(ordered))

    scope = _scope_line(searched)
    scope_html = ""
    if scope:
        scope_html = (f'<div style="font-size:12px;color:{INK_3};margin-top:8px;'
                      f'line-height:1.45">{esc(scope)}</div>')

    more = ""
    if len(ordered) > 1:
        n = len(ordered) - 1
        more = (f'<div style="font-size:12px;color:{INK_3};margin:0 0 12px">'
                f'{n} other date{"" if n == 1 else "s"} below</div>')

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
    """Front-load price and date. "Flight deal:" wasted the visible prefix."""
    best = cheapest_of(verdicts)
    q = best.quote
    head = (f"{q.currency} {q.price:,.0f} · {watch.origin}→{watch.destination} · "
            f"{q.depart_date:%d %b}")
    if len(verdicts) > 1:
        return f"{head} +{len(verdicts) - 1} more"
    return f"{head} ({q.probe.trip_days}d)"


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
