# Airline price watch bot

Watches flight routes and emails you when a fare is genuinely cheap — not just
when it moves. Which routes it watches is up to you — add and remove them in
the control panel.

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
     │            → "check every 9th departure date, 52 searches"
     ▼
provider.py       ask SerpApi (Google Flights) for each sampled date pair
     │            ONE SEARCH = ONE EXACT DATE PAIR. no flexible-date mode.
     ▼
evaluate.py       is this a deal?  two rules: Google rates it "low", or
     │            it beats the cheapest ever recorded for that route
     ▼
state.py          have I already emailed you about these exact dates?
     │            if yes, stay quiet - unless it dropped another 5%
     ▼
notify.py         email what survives, cheapest first, with booking links
     │            (pointed at a price-sorted Google Flights page)
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

There are exactly two rules, and neither of them contains a number:

```
Alert if Google rates the fare "low",
   or if it is the cheapest ever recorded for that route.
```

**The first asks the market's opinion.** `alert_on_price_level: ["low"]` — it
catches fares that are cheap *for when they are*, a December seat that is good
for December.

**The second reads the bot's own price journal instead.** Google's verdict is
seasonal, so it says nothing about cheap in plain dollars: a fare can be the
lowest ever seen on a route and still be rated `typical`. That gap is real —
a BNE→BOM run flagged AUD 1,408 in December while staying silent about AUD 995
in February. The record rule closes it.

Both rules are self-calibrating, which is the point: the bar is whatever the
market has actually shown you, so neither ever needs revisiting. The record rule
is also **self-suppressing** — each record has to beat the last, so alerts thin
out on their own rather than repeating.

Two details that make it behave:

- **A route with no history has no floor, and so cannot set a record.** That's
  what keeps the first run on a newly added route quiet instead of flagging all
  26 dates as all-time lows.
- **It applies to the run's cheapest fare only**, once every date for the route
  has been priced. Testing each date against the floor as it arrives flags every
  fare under the old record — four dates all announcing they are "the cheapest
  ever" when only one of them is.

Earlier versions also had an absolute `hard_ceiling` (alert at/below AUD 900) and
a `never_alert_above` veto. Both are **gone**. An absolute number is a claim
about what fares *should* cost, and it goes stale the moment they structurally
move — you'd be re-tuning it forever, and a stale ceiling either spams you or
silently stops firing. Google's verdict is built on real price history for this
route and these dates and **recalibrates by season on its own**, so it needs no
maintenance. The record rule is the same bargain from the other direction: it
compares against your own observed history rather than a number you chose. Rules
that stay correct beat rules that drift.

**What neither rule catches:** a fare that is cheap in dollars but neither
rated `low` nor an all-time record — say AUD 950 on a route where you have seen
AUD 717. Covering that case needs an absolute threshold, with all the staleness
that implies. It was a deliberate omission, not an oversight.

**Expect quiet weeks.** "low" appears to mean roughly *below the floor of the
typical range*, which is a high bar. In one live run of six real dates, none
qualified. That is the filter working, not a fault.

An earlier version also alerted on "15% below the typical midpoint". It was
dropped: on a wide range like 820-1,700 a fare of 1,071 reads as "15% below
typical" while being nowhere near cheap, and it fired on a fare Google itself
called ordinary. The percentage is still *shown* on alerts as context - it just
no longer decides anything.

### Where the booking link lands

Google Flights has no stable public URL for a single itinerary, so every link in
an email points at the *search* for that route and date pair, not at the fare
itself. The results page ranks by "Top flights" — a blend of price and
convenience — which means the fare you were emailed about is often several rows
down and has to be hunted for.

The sort order turns out to live in the `tfu` query parameter, as a two-field
protobuf:

```
tfu=EgIIAQ   →  bytes 12 02 08 01  →  sort = 1  (Top flights, the default)
tfu=EgIIAg   →  bytes 12 02 08 02  →  sort = 2  (Price)
```

Those values match SerpApi's documented `sort_by` options exactly. Rewriting
that one parameter lands you on a price-sorted page, so the quoted fare is at or
near the top. The search itself is encoded separately in `tfs`, which is left
untouched — the page shows the same flights, just ordered differently.

`tfu` is undocumented, so `_by_price()` degrades rather than breaks: a non-Google
URL, a link with no `tfs`, or anything unparseable is returned unchanged. Worst
case you land on an unsorted results page, which is what you had before.

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

One route: every 5th date, **46 searches per run**, ~198/month against a 240 cap.
Add a second and both drop to every 9th date — 26 each, 224/month, still inside.
**Adding a route makes every route sample more coarsely.** That's the honest
trade of a fixed budget, and it happens automatically rather than silently
overspending.

Trip length is **one number, not a range** (12 days). Every probe then uses the
same duration, so prices are directly comparable across dates: a cheaper result
means a cheaper *date*, not a shorter trip. A range would rotate durations
between probes and quietly destroy that comparison.

**SerpApi's own count is the only budget authority**, checked before each run.
There used to be a local tally alongside it, reconciled with `min()`. The two
counted different windows — the tally by calendar month, SerpApi from whenever
the plan renews — and the tally also counted searches SerpApi served from cache
and never charged for. It drifted high and vetoed runs the real quota allowed.
A second number that can only be wrong is worse than no second number, so it is
gone; if SerpApi can't be reached, the run doesn't start rather than guess.

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

`SMTP_HOST` and `SMTP_PORT` are optional — they default to `smtp.gmail.com:465`,
and an empty value falls back to the same defaults. That second half matters: an
unset GitHub secret does not leave the variable absent, it sets it to `""`, so
`os.getenv("SMTP_PORT", "465")` returns the empty string rather than the default
and `int("")` takes the whole run down with a traceback.

**Paste secrets carefully.** Both failures this project has actually hit were
malformed secret values, not bad code: a trailing newline on `SERPAPI_KEY` gave
`401 rejected the key`, and a bad `SMTP_PASSWORD` gave `535 BadCredentials`.
Setting them via `gh secret set NAME` and pasting at the prompt avoids both.

**4. Test it without waiting a week.** Actions tab → *Check flight prices* →
**Run workflow**. The `workflow_dispatch` trigger exists for exactly this.

The workflow commits `state.json` back after each run — that commit is the bot's
memory, so don't add it to `.gitignore`. It needs `contents: write`, which is
already declared in the workflow file.

### Failing before it spends anything

Two checks run before the first search, because both failures used to surface
only *after* a full run's quota was gone:

- **The test suite**, in CI. Stdlib `unittest`, under a second.
- **An SMTP login**, connecting and authenticating without sending. A bad app
  password used to be discovered at the very end — every fare priced, every
  credit spent, and the alert still undelivered.

Neither costs a search. Both turn "the month's quota is gone and you got
nothing" into "stopped immediately, here is what's wrong".

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

**This can silence a record.** A new all-time low on a date pair you were
already emailed about, where the further drop is under 5%, is suppressed like
any other repeat. The fare is nearly identical to one you already know about, and
exempting records would let a slow slide downward email you every week — but it
does mean "cheapest ever" occasionally goes unsaid. The monthly digest still
shows it.

`--force-notify` ignores all of the above and re-sends every current deal.

## Making silence mean something

This bot is designed to be quiet. But a run that finds nothing looks **exactly
like** a broken SMTP password, an expired API key, or a workflow that quietly
stopped firing. You could go three months assuming fares were just high.

So once a month — on the first run of each calendar month — it emails you
whether or not there's anything to report:

```
Still watching.

CHEAPEST NOW

  BNE → NRT   AUD 1,034   (typical)
    Sun 01 Nov → Fri 13 Nov 2026
    06:40 BNE → 09:00 NRT (27h 20m) · Jetstar JQ928, JQ15
    https://www.google.com/travel/flights?...
    Lowest since 27 Jul: AUD 717 on 23 Aug · Tue 18 May → Sun 30 May 2027

  BNE → BOM   AUD 995   (unrated)
    Mon 08 Feb → Mon 22 Feb 2027
    20:35 BNE → 22:35 BOM (30h 30m) · Qantas QF943, MH126

Nothing hit 'low' — these are just the cheapest seen.
```

**It reports two prices, and the difference matters.** The headline is what the
route costs *now* — the cheapest fare across every date the latest run checked,
and the only one that can still be booked, so it is the only one that gets a
link. `Lowest since …` underneath is the low-water mark for the period, plain text
and dated, shown only when it is genuinely lower than today.

**It is named for the window it covers, deliberately.** The alert email says
"cheapest ever recorded" and means all time; the digest looks back only as far
as the previous digest. Calling both of them "lowest seen" guarantees they
eventually disagree — a digest quoting a low *higher* than a record you were
emailed about, which reads as a bug and isn't one. Naming the actual start date
also survives a missed run, which widens the window rather than losing it, and
would have made "this month" quietly wrong.

Earlier versions printed only the low-water mark, next to a working booking
button. Every individual piece was true, but a month-old price beside a live
link reads as a fare you can buy, and usually you cannot. Nothing was wrong with
the data; the presentation was making a claim the data did not support.

It costs **no extra searches** — it's assembled entirely from
[prices.jsonl](prices.jsonl), which the run already wrote, including that
"today" figure: the digest is sent at the tail of a run that has just searched
every date. It covers everything since the last digest, so a missed run widens
the window rather than losing the period. And it names the cheapest fare seen
even when nothing qualified, which tells you whether the route is genuinely
expensive or the bot has gone deaf.

Send one now rather than waiting for the month to turn:

```bash
python check.py --digest
```

## The price record

`state.json` remembers what you were *told about*. [prices.jsonl](prices.jsonl)
remembers what was **seen** — every fare the bot fetched, alerted or not, one
JSON object per line:

```json
{"at":"2026-08-24T21:00:04+00:00","watch":"bne-nrt","depart":"2026-10-23",
 "ret":"2026-11-04","trip_days":12,"price":1152.0,"currency":"AUD",
 "level":"typical","typical_low":910,"typical_high":1420,
 "airlines":["Jetstar"],"stops":0,"deal":false}
```

This costs **nothing extra** — the search is already paid for whether or not the
fare turns out to be interesting. Thrown away, that data can never answer the
questions that matter later:

- Was that "low" fare actually cheap, judged against my own history?
- Which weeks of the year is this route reliably cheapest?
- Is Google's verdict any good on this route, or should I stop trusting it?

The first of those is no longer hypothetical: the second alerting rule reads
this file. `history.floors()` returns the cheapest price ever recorded per
route, read once at the start of a run — before any of that run's own rows are
written, so a fare is only ever compared against runs that came before it.

None of those are answerable retrospectively, which is why it logs from the
start. At two routes it's about 220 rows a month — a few hundred KB a year.

The workflow commits it back after every run, same as `state.json`, so it
accumulates rather than resetting. Dates with no fares on sale are recorded too
(`"price": null`) — "nothing available" is a fact about the route, not a gap.

Read it back with `flightbot.history.read()`, or any JSONL tool:

```bash
python -c "from flightbot import history; \
  rows=[r for r in history.read() if r['price']]; \
  print(min(rows, key=lambda r: r['price']))"
```

`--dry-run` never writes it, and `--demo` writes to a gitignored
`prices.demo.jsonl` so invented fixture prices can't contaminate the real record.

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
python check.py --digest         # send the monthly 'still watching' summary now
python check.py --limit 6        # only the first 6 dates - cheap live smoke test

python -m unittest discover      # the test suite - stdlib only, under a second
```

## Layout

```
check.py              entry point
start-ui.bat          double-click to open the control panel
watches.json          your routes, plus shared defaults and the budget
state.json            memory: alert history + monthly search spend
prices.jsonl          every fare ever seen, one JSON object per line
ui/index.html         the control panel - served by --ui, not opened directly
ui/airports.json      4009 airports for the search box (public domain)
tests/                the suite - stdlib unittest, no extra dependency
flightbot/
  config.py           settings, watchlist loading, sampling planner
  history.py          the append-only price journal
  ui_server.py        localhost server behind --ui (stdlib only)
  provider.py         SerpApi client + the demo fixture provider
  evaluate.py         the is-this-a-deal logic
  state.py            cooldown + budget tracking
  notify.py           terminal output and email
```

## Future plans

Known gaps, each deliberately unbuilt rather than forgotten:

**An absolute price threshold.** The one case neither rule catches: a fare cheap
in dollars but neither rated `low` nor an all-time record. It is the only rule
that would reflect a budget rather than the market's mood — and the only one
that needs revisiting as fares move. Worth adding only once the two current
rules have run long enough to show what they miss.

**A rolling window for the record rule.** A freak one-off sale becomes a
permanent bar the route may never beat again, and the rule goes quiet without
saying so. Comparing against the cheapest seen in the last year instead of ever
would fix it, at the cost of one constant. Not worth adding until the record
actually goes stale — the digest will show it happening.

**Per-fare booking links.** A second SerpApi call using the response's booking
token returns real airline links, but costs a credit per fare. At six deals a
run that is a meaningful bite out of 250 a month, for convenience rather than
information.

## Status

**Live and verified end to end**, Aug 2026: real searches against SerpApi, real
prices, real email delivered — including a fully green GitHub Actions run that
searched all 52 date pairs, applied both rules, emailed the survivors and
committed its state back to the repo.

A sample of live BNE→NRT returns: **AUD 951** for 24 Nov–5 Dec (17% below
typical), against **AUD 2,184** over New Year. Typical range runs ~900–1,390 in
November and ~1,400–2,400 in late December — the seasonal swing is large, which
is precisely why a single fixed price threshold couldn't have worked for both,
and why the bot defers to Google's seasonally-recalibrated verdict instead.

`--demo` never sends email; its prices are invented fixture data.
