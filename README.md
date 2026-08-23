# Airline price watch bot

Watches flight routes and emails you when a fare is genuinely cheap — not just
when it moves. Currently watching **Brisbane → Tokyo Narita, return**.

## Try it right now

No API key, no signup, no credits spent:

```bash
pip install -r requirements.txt
python check.py --demo
```

That runs the whole pipeline against a bundled fixture so you can see the
decision logic work before committing to anything.

## How it decides something is a deal

The hard part of a price bot isn't fetching a price — it's knowing whether that
price is *good*. Rather than building months of price history first, this leans
on Google Flights' own `price_insights`, which returns a typical price range and
a `low` / `typical` / `high` verdict for the route.

There is exactly one rule:

```
Alert if Google rates the fare "low".
```

That's it — `alert_on_price_level: ["low"]`, the only knob, and the only thing
that decides anything.

Earlier versions also had an absolute `hard_ceiling` (alert at/below AUD 900) and
a `never_alert_above` veto. Both are **gone**. An absolute number is a claim
about what fares *should* cost, and it goes stale the moment they structurally
move — you'd be re-tuning it forever, and a stale ceiling either spams you or
silently stops firing. Google's verdict is built on real price history for this
route and these dates and **recalibrates by season on its own**, so it needs no
maintenance. One rule that stays correct beats three that drift.

**Expect quiet weeks.** "low" appears to mean roughly *below the floor of the
typical range*, which is a high bar. In one live run of six real dates, none
qualified. That is the filter working, not a fault.

An earlier version also alerted on "15% below the typical midpoint". It was
dropped: on a wide range like 820-1,700 a fare of 1,071 reads as "15% below
typical" while being nowhere near cheap, and it fired on a fare Google itself
called ordinary. The percentage is still *shown* on alerts as context - it just
no longer decides anything.

## How it searches (the constraint everything follows from)

**One search = one exact date pair.** The `google_flights` engine takes a single
`YYYY-MM-DD` for departure and return - it has no flexible-date or date-range
mode. So a run cannot *cover* a window; it **samples** it.

`step_days` sets how dense that sample is. The default watch looks 90-290 days
ahead and probes every 5th departure date: **41 searches per run**, covering
about 20% of departure dates.

Trip length is **pinned at 12 days** (`min` == `max` in `trip_length_days`), so
every probe uses the same duration and prices are directly comparable across
dates - a cheaper result means a cheaper date, not a shorter trip. Widening the
range makes consecutive probes rotate through lengths instead, sampling duration
at the cost of that like-for-like comparison. Checking *every* length against
*every* date would cost 5x and doesn't fit the free tier.

At weekly runs that's ~176 searches/month against SerpApi's free 250.
Anything more frequent overruns it - every-5-days would be 246.

The bot checks **SerpApi's own quota figure** before each run rather than
trusting a local tally, which drifts whenever `state.json` is deleted or a run
dies midway. It refuses to start a run it can't finish.

### Two hard limits, both verified against the live API

**The booking horizon is about 300 days.** Airlines haven't loaded schedules
beyond that, so those searches return nothing *and still cost a credit*. A live
probe at 297 days returned flights; 327 days returned none.
`max_horizon_days` clips the window automatically.

This is why `rolling` mode is the sane default. A `fixed` window silently empties
as it ages past the horizon.

**Use a real airport code, not a metro code.** `TYO` is accepted by the API and
returns **zero flights**. `NRT` and `HND` work. The default watch uses `NRT`
(where the budget carriers fly); `HND` is closer to central Tokyo and is worth a
second watch if you have quota spare.

## Going live

1. **Get a key** at [serpapi.com](https://serpapi.com/manage-api-key) (free tier, no card).
2. **Set up email.** Gmail needs an *App Password*, not your account password:
   turn on 2-Step Verification, then visit
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. `cp .env.example .env` and fill both in.
4. Check it end to end without sending anything:
   ```bash
   python check.py --dry-run
   ```
5. Then for real: `python check.py`

## Running it automatically

[.github/workflows/check-flights.yml](.github/workflows/check-flights.yml) runs
the check weekly, Monday 07:00 Brisbane time, on GitHub Actions (free). The repo
is [nartana2003/airline-discount-bot](https://github.com/nartana2003/airline-discount-bot).

**1. Authenticate git once.** This machine has no GitHub credentials yet, so
pushes fail with *"Password authentication is not supported"*. Easiest fix:

```bash
winget install --id GitHub.cli
gh auth login          # choose HTTPS, authenticate in the browser
gh auth setup-git      # makes git reuse that login
```

**2. Push.**

```bash
git push -u origin main
```

**3. Add the secrets.** On GitHub go to **Settings → Secrets and variables →
Actions → New repository secret** and add four:

| Secret | Value |
|---|---|
| `SERPAPI_KEY` | your key from [serpapi.com](https://serpapi.com/manage-api-key) |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the 16-character Gmail **App Password** (not your login password) |
| `ALERT_TO` | where alerts go (usually the same Gmail address) |

`SMTP_HOST` and `SMTP_PORT` are optional — they default to `smtp.gmail.com:465`.

**4. Test it without waiting a week.** Actions tab → *Check flight prices* →
**Run workflow**. The `workflow_dispatch` trigger exists for exactly this.

The workflow commits `state.json` back after each run — that commit is the bot's
memory, so don't add it to `.gitignore`. It needs `contents: write`, which is
already declared in the workflow file.

## Not getting spammed

**Once you've been emailed about a date pair, you are never emailed about it
again.** Not next week, not next year. A fare that stays cheap for two months
shouldn't email you eight times.

The memory lives in [state.json](state.json), keyed by watch ID and exact date
pair:

```json
"alerts": {
  "bne-tokyo:2026-11-21_2026-12-03": {
    "price": 719.0, "price_level": "low", "at": "2026-08-23T06:08:56+00:00"
  }
}
```

Every run loads that file, skips any date pair already in it, and writes it back.
Under GitHub Actions the workflow **commits `state.json` back to the repo** after
each run — that commit *is* the bot's memory. This is why `state.json` is
deliberately **not** in `.gitignore`; if it were, every run would start blank and
re-email you everything.

One exception gets through: if the price falls another `renotify_on_drop_pct`
(5%) below what you were last quoted, you hear about it again. Being told twice
about the same fare is noise; a further drop is genuinely new information. The
stored price then ratchets down, so the next alert needs another 5% off *that*.

If you'd rather be reminded periodically about a fare that's still cheap, set
`cooldown_hours` to a number instead of `null` — but keep it **longer than the
gap between runs** (weekly = 168h) or every run re-alerts the same fare.

`--force-notify` ignores all of the above and re-sends every current deal.

## Adding and editing routes

Open [ui/index.html](ui/index.html) in **Chrome or Edge** (double-click it).
Click *Open watches.json* and edit visually. Ctrl+S saves straight back.

Routes live in a sidebar you can filter by name or airport code (`/` jumps to
the search box) and narrow to just Active or Paused. Picking one opens it in the
detail pane, split across two tabs — **Route** (label, airports, on/off) and
**Dates & deal** (window, sampling step, trip length, alert rules). Arrow keys
move between routes, `1`–`2` jump between tabs, `?` lists the rest.

Everything that doesn't vary route to route — passengers, cabin, stops,
currency, market, and the notification cooldown — lives behind **⚙ Settings**
in the header instead of being repeated on every route. *Apply to all routes*
writes the values into each route, and newly added routes inherit them
automatically. This is a UI convenience only: the JSON schema is unchanged, so
each route still carries its own copy of those fields and you can still make one
route differ by hand-editing [watches.json](watches.json).

Booking horizon (`max_horizon_days`, effectively always 300) is no longer shown
in the UI — it's still read from the JSON and still clips windows automatically.

Tick the checkboxes to select any number of routes, and a bar appears to
enable, pause, duplicate or delete them together. Duplicate is the quickest way
to add a route: copy an existing one and change the destination.

### Adding a second route — and the budget it costs

Multi-route is already supported: `watches` is a list, and every run iterates all
enabled entries. Select the existing route, hit **Duplicate**, then change the
destination, ID and label. Nothing else needs touching.

The catch is quota, not code. The current BNE→NRT watch samples 41 dates per run
= **~176 of your 250 free searches/month**, leaving only ~74/month (~17 per run)
for everything else. A second route at the same density would put you at ~350 and
the bot would refuse to run. So a second route means one of:

- **Coarsen both.** Step 5 → 9 on each: ~23 probes each, ~197/month total.
- **Keep Tokyo dense, sample the new route thinly.** Leave Tokyo at step 5, set
  the new route to step 12 (~17 probes): ~249/month. Fits, but with no headroom.
- **Narrow the new route's window.** A 90–200 day window at step 5 is 23 probes
  rather than 41 — good when you only care about one season.
- **Pause Tokyo** while you watch something else.

The header's budget strip does this arithmetic live as you type, and turns red
when you're over cap — set the step by watching that number rather than guessing.

The header keeps a live **search budget** — cost per run, monthly total, and
whether it fits your cap, plus what share of departure dates you're actually
sampling. Lower the step and watch both climb. Problems (duplicate IDs, inverted windows,
rules that could never fire) show a `!` beside the route and are spelled out in
the detail pane, and saving is blocked until they're fixed.

Firefox and Safari can't write files directly, so *Save* there downloads a copy
you drop into the project folder yourself. Everything else works the same.

You can also just hand-edit [watches.json](watches.json) — every field has a
`_note` sibling explaining it, and the UI preserves those notes when it saves.
Use real airport codes (`NRT`, `HND`), **not** metro codes like `TYO`, which
return nothing. Each new route multiplies your search spend.

## Commands

```bash
python check.py                  # normal run
python check.py --demo           # fixture data, no key, no credits
python check.py --dry-run        # check + print, never email or save state
python check.py --watch bne-tokyo  # just one route
python check.py --force-notify   # re-alert current deals, ignore cooldown
python check.py --limit 6        # only the first 6 dates - cheap live smoke test
```

## Layout

```
check.py              entry point
watches.json          your routes and thresholds
state.json            memory: alert history + monthly search spend
ui/index.html         the control panel - open in Chrome/Edge
flightbot/
  config.py           settings, watchlist loading, date sampling
  provider.py         SerpApi client + the demo fixture provider
  evaluate.py         the is-this-a-deal logic
  state.py            cooldown + budget tracking
  notify.py           terminal output and email
```

## Future plans



## Status

**Live and verified end to end**, Aug 2026: real searches against SerpApi, real
prices, real email delivered.

A sample of live BNE→NRT returns: **AUD 951** for 24 Nov–5 Dec (17% below
typical), against **AUD 2,184** over New Year. Typical range runs ~900–1,390 in
November and ~1,400–2,400 in late December — the seasonal swing is large, which
is precisely why a single fixed price threshold couldn't have worked for both,
and why the bot defers to Google's seasonally-recalibrated verdict instead.

`--demo` never sends email; its prices are invented fixture data.
