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

## How the whole thing works

Once a week GitHub Actions wakes the bot up and it does this:

```
watches.json      your routes, shared defaults, monthly search budget
     │
     ▼
config.py         load routes, then work out how densely to sample:
                  monthly cap ÷ runs per month ÷ number of enabled routes
     │            → "check every 5th departure date, 49 searches"
     ▼
provider.py       ask SerpApi (Google Flights) for each sampled date pair
     │            ONE SEARCH = ONE EXACT DATE PAIR. no flexible-date mode.
     ▼
evaluate.py       is this a deal?  one rule: Google itself rates it "low"
     │
     ▼
state.py          have I already emailed you about these exact dates?
     │            if yes, stay quiet - unless it dropped another 5%
     ▼
notify.py         email what survives, cheapest first, with booking links
     │
     ▼
state.json        record what was sent - committed back to the repo,
                  because that commit IS the bot's memory between runs
```

Four decisions shape everything else, and each one is a reaction to a real
constraint rather than a preference:

**The API can't search a date range.** `google_flights` takes one departure date
and one return date, so a run can't *cover* a window — it **samples** it. That
single fact is why there's a budget, why sampling density matters, and why
adding a route makes every route coarser.

**You get 250 searches a month, free.** So the bot derives its own sampling
density from that cap instead of asking you to do arithmetic, and it checks
SerpApi's real quota figure before starting a run it couldn't finish.

**Knowing a price is easy; knowing it's *good* is hard.** Rather than building
months of price history, it defers to Google's own `low`/`typical`/`high`
verdict, which is built on real history and recalibrates by season on its own.
That's the only alerting rule — absolute price thresholds were removed because
they go stale as fares move.

**Being told twice is noise.** Once you've been emailed about a date pair, you
never are again, unless the price falls another 5%. That memory lives in
`state.json`, which the workflow commits back after every run.

Everything else is detail on those four, and each has its own section below.

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

How dense that sample is used to be a number you set by hand (`step_days`), which
meant redoing arithmetic every time you added a route. **The bot now works it out
itself.** It knows the monthly cap, how often it runs, and how many routes are
enabled, so it solves for the densest sampling that fits:

```
per-run allowance = monthly cap ÷ runs per month
each route's share = that ÷ number of enabled routes
step = the smallest gap that keeps a route inside its share
```

One route: every 5th date, **49 searches per run**, ~211/month against a 240 cap.
Add a second and both drop to every 9th date — 27 each, 232/month, still inside.
**Adding a route makes every route sample more coarsely.** That's the honest
trade of a fixed budget, and it happens automatically rather than silently
overspending.

Trip length is **one number, not a range** (12 days). Every probe then uses the
same duration, so prices are directly comparable across dates: a cheaper result
means a cheaper *date*, not a shorter trip. A range would rotate durations
between probes and quietly destroy that comparison.

The bot checks **SerpApi's own quota figure** before each run rather than
trusting a local tally, which drifts whenever `state.json` is deleted or a run
dies midway. It refuses to start a run it can't finish.

### Two hard limits, both verified against the live API

**The booking horizon is about 300 days.** Airlines haven't loaded schedules
beyond that, so those searches return nothing *and still cost a credit*. A live
probe at 297 days returned flights; 327 days returned none. It's a fact about
the world rather than a preference, so it's the constant `MAX_HORIZON_DAYS` in
[config.py](flightbot/config.py) and the window is clipped to it automatically.

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
  "bne-nrt:2026-11-21_2026-12-03": {
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

## Adding and removing routes

**Double-click `start-ui.bat`** (or run `python check.py --ui`).

That opens the control panel in your browser. Nothing is hardcoded — add routes,
remove them, pause them, and it writes `watches.json` for you as you type.

> **Don't open `ui/index.html` directly.** Double-clicking it, or using an
> editor's preview pane, loads it as a `file://` page — which browsers forbid
> from reading local files, so it can't see your routes. The page detects this
> and says so rather than sitting there loading forever.

A route is **four things**: origin, destination, trip length, on/off.

```
  [BNE] → [NRT]     [12] days     (on)   ×
  27 searches per run · every 9 days · 22 Oct 2026 – 19 Jun 2027
```

Everything else is either derived or shared, which is why it isn't on the form:

| Was a field | Now |
|---|---|
| `label`, `id` | Derived from the airport codes (`BNE`+`NRT` → `bne-nrt`) |
| `step_days` | Computed from your budget and route count |
| `days_from_now_min/max` | Shared default, 60–300 days |
| `trip_length_days.min/max` | One number — a range breaks price comparability |
| passengers, cabin, stops, currency, market | Shared `defaults` in watches.json |
| `hard_ceiling`, `never_alert_above` | Removed entirely |

The header shows what the whole watchlist costs and how much of the monthly cap
it uses. Add a route and watch every route's sampling loosen to compensate — the
panel gets those numbers **from the bot itself**, not from JavaScript that could
drift out of step with it.

### Why a local server instead of double-clicking the HTML

A `file://` page isn't allowed to read local files without a picker, which is why
the old panel opened blank showing default values and made you hunt for
`watches.json` every session. Serving it from `127.0.0.1` removes that: your
routes are there when it opens and saves land straight on disk. It's
[stdlib-only](flightbot/ui_server.py) — no Flask, no new dependencies — and binds
to localhost, so nothing off your machine can reach it.

Edits are validated by **loading them exactly the way a run would** before
anything is written. A watchlist that wouldn't parse can't reach disk, so the
scheduled job can't be broken from the UI.

You can still hand-edit [watches.json](watches.json) — every field has a `_note`
sibling explaining it.

### Airport search

You type a city, country or nickname; you don't hunt for codes. "Tokyo" gives
Haneda and Narita, "bali" gives Denpasar, "saigon" gives Ho Chi Minh City.
Typing a code you already know still works.

**Metro codes are stripped from the list at build time.** `TYO`, `LON` and `NYC`
are accepted by the API and return **zero flights** — verified live — so the
picker must not let you choose one.

`ui/airports.json` is 4,009 airports (281 KB) built from
[OurAirports](https://ourairports.com/data/), which is **public domain** — no
attribution obligation, and safe to carry in a public repo.

```bash
curl -O https://davidmegginson.github.io/ourairports-data/airports.csv
curl -O https://davidmegginson.github.io/ourairports-data/countries.csv
# keep rows with a 3-letter IATA code, type in {large,medium,small}_airport,
# and scheduled_service == "yes"; emit [code, city, country, name, size, keywords]
```

Three columns do real work here:

- **`scheduled_service`** filters to airports with actual commercial flights.
  That's what drops the ~2,000 private strips, and it's why the file shrank
  while getting *more* useful.
- **`type`** (large/medium/small) ranks results, replacing a hand-maintained
  list of hub codes with something the upstream keeps current.
- **`keywords`** carries the alternate names — this is why "bali" finds
  Denpasar and "saigon" finds Ho Chi Minh City without a lookup table of my own.

This replaced [OpenFlights](https://openflights.org/data.html), which was tried
first and rejected on evidence: it was **missing BER** (Berlin's airport since
2020) while still listing **TXL and SXF**, both closed in 2020. Offering a
closed airport is the same failure as offering a metro code — a search that
returns nothing and still costs a credit. OpenFlights is also ODbL, so it
carried an attribution obligation this doesn't.

Search folds accents (`são paulo` = `sao paulo`) and is checked against a sweep
of 87 major destinations. A handful of cities still need an explicit alias
because their airport mentions the city nowhere at all — Taipei's is "Taiwan
Taoyuan International Airport" in the municipality of Taoyuan.

## Commands

```bash
python check.py                  # normal run
python check.py --ui             # add/remove routes in the browser
python check.py --demo           # fixture data, no key, no credits
python check.py --dry-run        # check + print, never email or save state
python check.py --watch bne-nrt  # just one route
python check.py --force-notify   # re-alert current deals, ignore cooldown
python check.py --limit 6        # only the first 6 dates - cheap live smoke test
```

## Layout

```
check.py              entry point
start-ui.bat          double-click to open the control panel
watches.json          your routes, plus shared defaults and the budget
state.json            memory: alert history + monthly search spend
ui/index.html         the control panel - served by --ui, not opened directly
ui/airports.json      4009 airports for the search box (public domain)
flightbot/
  config.py           settings, watchlist loading, sampling planner
  ui_server.py        localhost server behind --ui (stdlib only)
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
