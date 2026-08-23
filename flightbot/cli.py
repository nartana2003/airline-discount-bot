"""Entry point: load watches, search each sampled date pair, decide, notify."""

from __future__ import annotations

import argparse
import os
import sys

from . import config, notify
from . import provider as provider_mod
from .evaluate import Verdict, evaluate
from .provider import SearchError, build_provider
from .state import State


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="check.py",
        description="Check watched routes for cheap fares and email you when one appears.",
    )
    p.add_argument("--ui", action="store_true",
                   help="open the control panel in your browser to add or remove routes")
    p.add_argument("--port", type=int, default=8765, metavar="N",
                   help="port for --ui (default 8765)")
    p.add_argument("--demo", action="store_true",
                   help="use the bundled fixture instead of the live API (no key needed)")
    p.add_argument("--watch", metavar="ID",
                   help="only run this watch id (default: every enabled watch)")
    p.add_argument("--dry-run", action="store_true",
                   help="print results but never send email or write state")
    p.add_argument("--force-notify", action="store_true",
                   help="ignore the cooldown and re-send alerts for any current deal")
    p.add_argument("--no-colour", action="store_true", help="plain output, no ANSI codes")
    p.add_argument("--limit", type=int, metavar="N",
                   help="only search the first N dates per watch - a cheap live "
                        "smoke test that doesn't spend a full run's quota")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config.load_dotenv()

    if args.ui:
        from .ui_server import serve
        return serve(port=args.port)

    try:
        watches, budget = config.load_watchlist()
    except (OSError, ValueError, KeyError) as exc:
        print(f"could not read watches.json: {exc}", file=sys.stderr)
        return 1

    selected = [w for w in watches if w.enabled and (not args.watch or w.id == args.watch)]
    if args.watch and not selected:
        known = ", ".join(w.id for w in watches) or "none"
        print(f"no enabled watch with id '{args.watch}'. Known ids: {known}", file=sys.stderr)
        return 1
    if not selected:
        print("no enabled watches in watches.json - nothing to do.")
        return 0

    # Demo runs get their own state file so a fixture run can never suppress a
    # real alert or spend against the real search budget.
    state = State.load(config.ROOT / "state.demo.json" if args.demo else None)
    colour = not args.no_colour and sys.stdout.isatty()

    # Budget guard: the free tier is 250 searches/month and each date pair
    # costs one, so a run's cost is simply how many dates it samples.
    def dates_for(w):
        probes = w.probes()
        return probes[: args.limit] if args.limit else probes

    planned = sum(len(dates_for(w)) for w in selected)
    if not args.demo:
        # Prefer SerpApi's own figure over the local tally, which drifts.
        real_left = provider_mod.searches_left(os.getenv("SERPAPI_KEY", ""))
        local_left = max(budget.monthly_search_cap - state.searches_this_month(), 0)
        # Take the stricter of the two: SerpApi's is the hard limit, but a
        # lower local cap is a deliberate self-restraint worth honouring.
        available = min(real_left, local_left) if real_left is not None else local_left
        source = "SerpApi" if real_left is not None else "local tally"
        if planned > available:
            print(
                f"stopping: this run needs {planned} searches but only {available} "
                f"remain (per {source}). Sampling density is derived from the cap, "
                f"so this usually means the month's quota is already spent - wait "
                f"for the reset, pause a route in --ui, or raise the cap.",
                file=sys.stderr,
            )
            return 2
        if real_left is not None and real_left != local_left:
            print(f"quota: {real_left} left per SerpApi "
                  f"(local tally said {local_left}) - using the smaller")

    try:
        provider = build_provider(os.getenv("SERPAPI_KEY", ""), args.demo)
    except SearchError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.demo:
        print("running in demo mode - fixture data, no API calls, no credits used")

    exit_code = 0
    searches_used = 0

    for watch in selected:
        verdicts: list[Verdict] = []
        for probe in dates_for(watch):
            try:
                quote = provider.search(watch, probe)
            except SearchError as exc:
                print(f"  {watch.id} {probe.label}: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            searches_used += 0 if args.demo else 1
            verdicts.append(evaluate(watch, quote))

        notify.print_results(watch, verdicts, colour=colour)

        deals = [
            v for v in verdicts
            if v.is_deal and (
                args.force_notify
                or state.should_notify(v, watch.notify.cooldown_hours,
                                       watch.notify.renotify_on_drop_pct)
            )
        ]
        suppressed = sum(1 for v in verdicts if v.is_deal) - len(deals)
        if suppressed > 0:
            print(f"  ({suppressed} deal(s) already emailed for these dates - "
                  f"suppressed; --force-notify overrides)")

        if not deals:
            continue
        if args.dry_run:
            print(f"  [dry-run] would email {len(deals)} deal(s)")
            continue
        if args.demo:
            # Demo prices are invented. Emailing them would put fabricated
            # fares in a real inbox, which is worse than useless.
            print(f"  [demo] would email {len(deals)} deal(s) - not sent, "
                  f"these prices are fixture data")
            continue

        if watch.notify.email:
            settings = config.EmailSettings.from_env()
            if not settings.configured:
                print("  email not configured (SMTP_USER/SMTP_PASSWORD/ALERT_TO) - "
                      "showing on screen only", file=sys.stderr)
            else:
                try:
                    notify.send_email(settings, watch, deals, searched=verdicts)
                    print(f"  emailed {len(deals)} deal(s) to {settings.to}")
                except OSError as exc:
                    print(f"  email failed: {exc}", file=sys.stderr)
                    exit_code = 1
                    continue  # don't record the alert if it never arrived

        for v in deals:
            state.record_alert(v)

    if not args.dry_run:
        state.record_searches(searches_used)
        state.save()

    if searches_used:
        # Report SerpApi's figure, not the local tally - they can disagree and
        # the authoritative one is the one that actually stops you searching.
        left = provider_mod.searches_left(os.getenv("SERPAPI_KEY", ""))
        if left is None:
            left = f"~{budget.monthly_search_cap - state.searches_this_month()} (local estimate)"
        print(f"\n{searches_used} search(es) used, {left} left this month")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
