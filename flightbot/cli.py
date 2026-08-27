"""Entry point: load watches, search each sampled date pair, decide, notify."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import config, history, notify
from . import provider as provider_mod
from .evaluate import Verdict, evaluate, mark_drop
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
    p.add_argument("--digest", action="store_true",
                   help="send the 'still watching' summary now, without waiting "
                        "for the next scheduled one")
    p.add_argument("--no-colour", action="store_true", help="plain output, no ANSI codes")
    p.add_argument("--limit", type=int, metavar="N",
                   help="only search the first N dates per watch - a cheap live "
                        "smoke test that doesn't spend a full run's quota")
    return p.parse_args(argv)


def _fit(plan: dict[str, list], available: int) -> int:
    """Drop probes until the run fits `available`. Returns how many went.

    Takes from whichever route currently has the most dates, so routes stay
    balanced rather than the last one in the file losing everything. Probes come
    off the end, which is the far side of the window - the dates airlines are
    least likely to have loaded anyway.
    """
    dropped = 0
    total = sum(len(p) for p in plan.values())
    while total > max(available, 0):
        biggest = max(plan.values(), key=len)
        if not biggest:
            break            # nothing left to give back
        biggest.pop()
        total -= 1
        dropped += 1
    return dropped


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config.load_dotenv()

    if args.ui:
        from .ui_server import serve
        return serve(port=args.port)

    # Asked BEFORE the watchlist is planned, because it is what the plan is
    # built from. SerpApi's own count is the only budget authority. A local
    # tally used to shadow it, reconciled with min(), but the two count
    # different windows - the tally by calendar month, SerpApi from whenever
    # the plan renews - and the tally also counted searches SerpApi served from
    # cache and never charged for. It drifted high and vetoed runs the real
    # quota allowed. A second number that can only be wrong is worse than no
    # second number, so it is gone.
    available = None
    if not args.demo:
        available = provider_mod.searches_left(os.getenv("SERPAPI_KEY", ""))
        if available is None:
            print("stopping: could not reach SerpApi to check the remaining "
                  "quota. Rather than guess, this run does not start.",
                  file=sys.stderr)
            return 2

    try:
        raw = json.loads(config.WATCHES_PATH.read_text(encoding="utf-8"))
        watches, budget = config.load_watchlist_data(raw)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
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

    # Hand the figure to the control panel, which has no way to ask for itself.
    # Recorded BEFORE it is read back below, so this run's own reading is part
    # of the history runs_remaining() reasons over - including a reset that
    # happened just before this very run, which only this reading can reveal.
    state.record_quota(available)

    # How many runs are left before that quota resets - see
    # State.runs_remaining(). Planned across the routes this run will ACTUALLY
    # search: dividing the whole watchlist's allowance and only then selecting
    # one with `--watch` left its unspent share on the table, sampling that
    # route at less than half the density it could have afforded. Selection
    # decides the divisor, so this has to come after it.
    runs_remaining = None if args.demo else state.runs_remaining(budget.runs_per_month)
    config.plan_sampling(selected, budget, available, runs_remaining)

    # Each date pair costs one search, so a run's cost is simply how many dates
    # it samples - and the plan was already built from the quota above, so this
    # comes out well inside it.
    def dates_for(w):
        probes = w.probes()
        return probes[: args.limit] if args.limit else probes

    plan = {w.id: dates_for(w) for w in selected}
    planned = sum(len(p) for p in plan.values())

    if available is not None:
        # Only reachable in the corner the planner cannot solve: even one probe
        # per route costs more than is left. Trimming beats the abort this
        # replaced, which refused the whole run when the plan came to 51 and 50
        # remained - fifty dates' worth of results is strictly better than none.
        if planned > available:
            dropped = _fit(plan, available)
            planned -= dropped
            print(f"quota: only {available} searches left, so {dropped} of the "
                  f"planned dates were dropped from this run.", file=sys.stderr)
        print(f"quota: {available} left, this run plans {planned}")

    try:
        provider = build_provider(os.getenv("SERPAPI_KEY", ""), args.demo)
    except SearchError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.demo:
        print("running in demo mode - fixture data, no API calls, no credits used")

    # Prove the mailbox before spending a run's worth of credits on it. A bad
    # SMTP password used to surface only after every search had been paid for
    # and every fare decided - the quota gone, the alert still undelivered.
    # Logging in costs nothing and takes a second.
    if not args.demo and not args.dry_run:
        mail = config.EmailSettings.from_env()
        if mail.configured:
            try:
                notify.check_login(mail)
            except OSError as exc:
                print(f"stopping: the mail server rejected the login, so no "
                      f"alert could be delivered - {exc}", file=sys.stderr)
                return 1

    exit_code = 0
    searches_used = 0
    logged = 0
    history_path = history.DEMO_HISTORY_PATH if args.demo else history.HISTORY_PATH
    pending: list[notify.RouteAlert] = []

    # Read once, up front. Every fare this run is compared against the same
    # snapshot of what came before, so a record set earlier in the run cannot
    # raise the bar for the dates checked after it.
    baselines = history.baselines(history_path)

    # One timestamp for the whole run - see history.run_stamp().
    run_at = history.run_stamp()

    for watch in selected:
        verdicts: list[Verdict] = []
        for probe in plan[watch.id]:
            try:
                quote = provider.search(watch, probe)
            except SearchError as exc:
                print(f"  {watch.id} {probe.label}: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            searches_used += 0 if args.demo else 1
            verdicts.append(evaluate(watch, quote))

        # Second alerting rule, applied once every date for this route is in:
        # whichever date has fallen furthest below what that same date normally
        # costs. Keyed on trip length too, because a 7-day fare compared against
        # 12-day history is comparing different goods - see history.baselines().
        # `watch` is passed so a fare outside the route's stops preference
        # can't be crowned the week's drop - see mark_drop()'s docstring.
        mark_drop(verdicts, baselines.get((watch.id, watch.trip_days)), watch)

        notify.print_results(watch, verdicts, colour=colour)

        # Journal every quote before any alerting decision, so the record is
        # what the run SAW - independent of what it chose to email, and still
        # written if the email later fails.
        if not args.dry_run:
            try:
                logged += history.record(verdicts, history_path, run_at)
            except OSError as exc:
                print(f"  could not write price history: {exc}", file=sys.stderr)

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
            # Held back and sent as one email once every route has been
            # checked - see the batching note on notify.RouteAlert.
            pending.append(notify.RouteAlert(watch, deals, verdicts))
        else:
            # Not emailing this route, but it was still decided on - record it
            # so the same fare doesn't queue up again next run.
            for v in deals:
                state.record_alert(v)

    if pending:
        settings = config.EmailSettings.from_env()
        found = sum(len(g.deals) for g in pending)
        if not settings.configured:
            print("email not configured (SMTP_USER/SMTP_PASSWORD/ALERT_TO) - "
                  "showing on screen only", file=sys.stderr)
            for g in pending:
                for v in g.deals:
                    state.record_alert(v)
        else:
            try:
                notify.send_email(settings, pending)
                where = (f"{found} deal(s) across {len(pending)} route(s)"
                         if len(pending) > 1 else f"{found} deal(s)")
                print(f"\nemailed {where} to {settings.to}")
                for g in pending:
                    for v in g.deals:
                        state.record_alert(v)
            except OSError as exc:
                # Nothing is recorded: an alert that never arrived must stay
                # eligible to be sent again next run.
                print(f"email failed: {exc}", file=sys.stderr)
                exit_code = 1

    if logged:
        total = history.summary(history_path)
        print(f"\nlogged {logged} price(s) to {history_path.name} "
              f"({total['rows']} rows over {total['runs']} run(s))")

    # The "still watching" note. Sent every DIGEST_INTERVAL_DAYS, covering
    # everything seen since the last one - so a missed run widens the window
    # rather than losing the period.
    #
    # This has to run BEFORE state.save(): record_digest() only sets a field in
    # memory, so sending after the save left the timestamp unwritten and the
    # digest fired again on every single run.
    if not args.dry_run and not args.demo and (args.digest or state.digest_due()):
        d = history.digest(history.since(state.last_digest_at(), history_path))
        if d["searches"] == 0 and not args.digest:
            pass  # nothing has been recorded yet; wait for a run with data
        else:
            settings = config.EmailSettings.from_env()
            if not settings.configured:
                print("digest not sent - email is not configured", file=sys.stderr)
            else:
                try:
                    notify.send_digest(settings, d, watches=selected)
                    state.record_digest()
                    print(f"sent the digest to {settings.to} "
                          f"({d['runs']} run(s), {d['searches']} searches)")
                except OSError as exc:
                    print(f"digest failed to send: {exc}", file=sys.stderr)
                    exit_code = 1

    if not args.dry_run:
        state.save()

    if searches_used:
        left = provider_mod.searches_left(os.getenv("SERPAPI_KEY", ""))
        print(f"\n{searches_used} search(es) used" + (f", {left} left this month" if left is not None else ""))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
