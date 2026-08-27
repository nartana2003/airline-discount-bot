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
                  searches SerpApi says are left ÷ runs a month ÷ routes
     │            → "check every 8th departure date, 48 searches"
     ▼
provider.py       ask SerpApi (Google Flights) for each sampled date pair
     │            ONE SEARCH = ONE EXACT DATE PAIR. no flexible-date mode.
     ▼
evaluate.py       is this a deal?  two rules: Google rates it "low", or
     │            this date fell well below what this date normally costs
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
density instead of asking you to do arithmetic — and it derives it from the
quota SerpApi says is actually left, asked before every run, not from a number
typed in a config file.

**Knowing a price is easy; knowing it's *good* is hard.** Rather than building
months of price history first, it defers to Google's own `low`/`typical`/`high`
verdict, which is built on real history and recalibrates by season on its own.
Its own journal supplies the second rule — whether a departure date has fallen
below what that same date normally costs. Absolute price thresholds were
removed because they go stale as fares move.

**Being told twice is noise.** Once you've been emailed about a date pair, you
never are again, unless the price falls another 5%. That memory lives in
`state.json`, which the workflow commits back after every run.

Everything else is detail on those four, and each has its own section below.

## How it decides something is a deal

The hard part of a price bot isn't fetching a price — it's knowing whether that
price is *good*. Rather than building months of price history first, this leans
on Google Flights' own `price_insights`, which returns a typical price range and
a `low` / `typical` / `high` verdict for the route.

There are exactly two rules, and neither of them contains a price:

```
Alert if Google rates the fare "low",
   or if this date has fallen well below what this date normally costs.
```

**The first asks the market's opinion.** `alert_on_price_level: ["low"]` — it
catches fares that are cheap *for when they are*, a December seat that is good
for December.

**The second is the only thing that can see a price move.** Google grades a date
against its own time of year, which answers "is this good for December" but
never "did December get cheaper". That second question needs the bot's own
journal, and it needs to compare a date against *itself*.

Both rules are self-calibrating, which is the point: the bar is whatever the
market has actually shown you, so neither ever needs revisiting.

### Why the second rule compares each date against itself

The obvious version — "alert if this is in the cheapest 10% of everything I have
recorded for this route" — was built first, and it does not work on a seasonal
route. Pooling every date into one list makes the bar measure the **calendar**
rather than the market. April is always cheaper than December, so the cheapest
tenth of the pool is simply the April dates, run after run, forever.

Measured on the bundled demo journal: prices span 611–1,448, the pooled bar
lands at 681, and only three dates ever clear it. The rule fired on *"it is
April"* while claiming to mean *"this fare is cheap"* — and it was blind in both
directions. An April fare drifting **up** from 640 to 660 still cleared the bar.
December collapsing from 1,400 to 1,000 never came close to it.

So each price is now divided by the median of everything previously recorded
**for that same departure date**. That removes the season, because every date is
measured against itself, and what is left on the common scale is movement. The
ratios are then pooled across the route, because a single date rarely has enough
readings of its own for a percentile to mean anything.

The same five fares, judged the new way:

| | today | normal for that date | verdict |
|---|---|---|---|
| April, sitting where it always sits | 650 | 655 | silent |
| April, drifted **up** | 660 | 655 | silent — *used to alert* |
| April genuinely dropped | 540 | 655 | **alert** |
| December, normal | 1,400 | 1,405 | silent |
| December **crashed** | 1,000 | 1,405 | **alert** — *used to be silent* |

This is only possible because the probe dates are now anchored. While the grid
drifted, no date was ever priced twice, so a date had no history of its own to
be compared against.

Four details that make it behave:

- **It ranks by drop, not by price.** The cheapest seat on a seasonal route is
  just the cheap season; the fare that *moved* is the news, even when it costs
  more. Reporting the cheapest would re-announce April every week.
- **The baseline is keyed on trip length as well as route.** A 12-day and a
  7-day fare are different goods, so changing trip length starts a fresh
  baseline rather than making every fare look like a bargain.
- **A route with under 30 usable readings has no baseline**, and a date that has
  only just entered the window has nothing to compare against. Both stay quiet
  rather than guess — which is what keeps a newly added route silent.
- **Only dates seen more than once count** towards the bar. A date with one
  reading is its own median, so it would score exactly 1.0 and drag the bar
  towards "any drop at all qualifies".

Earlier versions also had an absolute `hard_ceiling` (alert at/below AUD 900) and
a `never_alert_above` veto. Both are **gone**. An absolute number is a claim
about what fares *should* cost, and it goes stale the moment they structurally
move — you'd be re-tuning it forever, and a stale ceiling either spams you or
silently stops firing. Google's verdict is built on real price history for this
route and these dates and **recalibrates by season on its own**, so it needs no
maintenance. The second rule is the same bargain from the other direction: it
compares each date against its own observed history rather than a number you
chose. Rules that stay correct beat rules that drift.

**What neither rule catches:** *which* of the sampled dates is cheapest. Both
rules judge a fare against a reference — Google's against that date's own time
of year, the percentile against the route's history — and neither compares the
sampled dates to each other. So an April fare that is unremarkable for April can
still be the cheapest month of the year and pass both rules in silence.

That comparison is deliberately **not** an alerting rule. On a seasonal route
the cheapest sampled date is always well below the median, every run, whether or
not a single price moved — as an alert it would fire constantly and carry no
news. It answers "when should I fly", which barely changes week to week, so it
is reported in the digest instead, every fortnight. See
[Making silence mean something](#making-silence-mean-something).

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
per-run allowance = searches actually left ÷ runs per month
each route's share = that ÷ number of enabled routes
step = the smallest gap that keeps a route inside its share
```

Two routes over a 90–300 day window: every 8th date, **24 searches each**, 48 a
run and 206/month against a 240 cap. Add a third and all three sample more
coarsely. **Adding a route makes every route sample more coarsely.** That's the
honest trade of a fixed budget, and it happens automatically rather than
silently overspending.

Trip length is **one number, not a range** (12 days), and it is shared by every
route. Every probe then uses the same duration, so prices are directly
comparable across dates: a cheaper result means a cheaper *date*, not a shorter
trip. A range would rotate durations between probes and quietly destroy that
comparison.

### The sampled dates sit on a fixed calendar grid

Sampling density is only half of it. *Which* dates get sampled matters just as
much, and for a long time it was wrong in a way that was easy to miss.

The window is anchored to today (90 to 300 days ahead) and today moves. Counting
the probe dates forward from the start of that window therefore dragged the
whole grid along with it: with an 8-day step and weekly runs every sampled date
shifted by 7, and **the same departure date did not come round again for nine
consecutive runs.** Measured on this watchlist, eight straight weeks shared not
one date with the first.

Two things quietly depended on dates repeating, and neither worked:

- **De-duplication.** [state.py](flightbot/state.py) keys an alert on the exact
  date pair. A pair it has already emailed about never comes back, so one cheap
  week in March gets emailed five times over, once per neighbouring date.
- **The price journal.** `prices.jsonl` filled with 24 one-off dates per run,
  none comparable to any other. It could record what a fare *was*, never that it
  had *moved* — which is the one thing worth knowing before booking.

Probe dates now come from a fixed lattice instead — every date whose ordinal
divides by the step — so the window slides *across* a stable set of dates rather
than carrying its own along. One date drops off the near end each run, one
appears at the far end, and everything between is re-priced:

```
run on 27 Aug  →  28 Oct, 06 Nov, 15 Nov, 24 Nov, 03 Dec ...
run on 03 Sep  →          06 Nov, 15 Nov, 24 Nov, 03 Dec, 12 Dec ...
run on 10 Sep  →                  15 Nov, 24 Nov, 03 Dec, 12 Dec, 21 Dec ...
```

Same cost, same density, same span — 24–26 of each run's dates are now shared
with the one before, against zero previously. Six months on, the window has
moved through the year normally and holds only six dates in common with where it
started; it explores the same range, it just stops forgetting what it saw.

**The lattice only holds while the step does**, and the step is derived from the
budget — so adding or pausing a route, or changing the window, re-cuts the grid
and the per-date series starts over. That is the cost of deriving density rather
than pinning it, and it is worth knowing before adding a route to a watchlist
that has been running a while.

**SerpApi's own count is the only budget authority**, asked before each run —
and the run *plans* from it, rather than merely checking against it.

It used to divide two hand-typed numbers, `monthly_search_cap` (240) and
`runs_per_month` (4.3), and use the live figure only to refuse: if the static
plan came to 51 searches and 50 remained, you got **zero** results instead of
fifty. The cap was also just a claim — nothing kept it in step with an account
whose runs get missed, fail halfway, or are triggered by hand.

Now the live figure sets the density, and it corrects in both directions:

| Searches left | Step | Per run | |
|---|---|---|---|
| 480 (two runs missed) | 4 days | 98 | spends the backlog instead of letting it expire |
| 240 (full month) | 8 days | 48 | |
| 120 (half spent) | 16 days | 24 | plans a run the account can pay for |
| 51 | 41 days | 10 | **used to abort with nothing** |
| 20 | 150 days | 4 | still runs |

Because the plan is built from the quota, it structurally cannot overshoot it,
so the all-or-nothing abort is gone. One corner survives it — when even a
single date per route costs more than is left — and there the run trims dates
off the far end of the window rather than refusing outright.

**Dividing by `runs_per_month` alone made density shrink every run, all period
long, even when nothing was wrong.** `runs_per_month` is an *average* cadence
across a full billing period; it says nothing about how far into the CURRENT
period things stand. Remaining quota shrinks through a period from ordinary
spending, but the average never does — so dividing the first by the second
gave a smaller number every single run, snapping back to full density only at
reset. Measured live, seven honestly-spent weekly runs: 49, 40, 33, 24, 20, 16,
12 dates. That also kept re-cutting the anchored probe lattice — the entire
point of which was for the same dates to keep coming back.

The fix needs to know how many runs are left before reset, not just the
average cadence — and nothing hands that boundary over; SerpApi's account
endpoint returns a remaining-search count, not a renewal date. So it is
inferred from what the count itself does: a reading that goes UP can only mean
a reset happened since the last one, since every run only ever spends. The
most recent such jump is taken as this period's start, and every run recorded
since is subtracted from the average to get what is actually left —
`State.runs_remaining()`, fed a short log of past readings kept in
`state.json`. Settled into a period with one reset behind it, density holds
roughly flat run to run instead of shrinking:

```
period 1 (no reset ever witnessed yet):  49, 40, 33, 24, 20  -  the old shrink
period 2 (one reset behind it):          66, 66, 66, 40      -  flat, until genuinely low
```

**The very first period a fresh install ever watches has no boundary to infer
from and behaves like the old code** — the same shrink, once, honestly. Guessing
a boundary before one has actually been witnessed was tried and measured: it
back-dates every reading on file to a period there is no evidence even began
there, so it drains the estimate faster than reality and reproduces the exact
shrink this exists to fix — for one period instead of every one, but still
wrong. The plain average is the more honest answer until a real reset proves
where a boundary sits, the same caution this codebase already applies to a
route with no price history, or a date sampled only once.

The period length itself starts as a guess — an average month — and
self-corrects once **two** resets have actually been witnessed: their gap is
real data about this account's billing cycle, and nothing about it needs to be
typed in.

There used to be a local tally alongside SerpApi, reconciled with `min()`. The
two counted different windows — the tally by calendar month, SerpApi from
whenever the plan renews — and the tally also counted searches SerpApi served
from cache and never charged for. It drifted high and vetoed runs the real
quota allowed. A second number that can only be wrong is worse than no second
number, so it is gone; if SerpApi can't be reached at all, the run doesn't
start rather than guess.

`monthly_search_cap` and the plain `runs_per_month` average both survive as
fallbacks for the cases with no account to ask, or no quota history yet —
`--demo`, the tests, a fresh install, and the control panel, which has no API
key and no business making network calls to render a form. Instead, each real
run records what it saw into `state.json`, and the panel reads that back — the
same figures a real run would plan from, not a guess made independently in
JavaScript, and not a fresh network call.

The other half of that division, `runs_per_month` (4.3), cannot be checked at
runtime — the bot has no way to see its own cron. It is checked in CI instead:
[tests/test_schedule.py](tests/test_schedule.py) expands the cron in
`check-flights.yml`, works out how often it actually fires, and fails if the
two disagree by more than 0.1. Change the schedule without changing the number
and the build breaks, which is the point — it used to be a figure nothing
enforced.

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

**This can silence a standout fare.** A new low on a date pair you were already
emailed about, where the further drop is under 5%, is suppressed like any other
repeat. The fare is nearly identical to one you already know about, and
exempting it would let a slow slide downward email you every week. The monthly
digest still shows it.

This suppression only works because probe dates repeat. While the grid drifted,
the same date pair essentially never came round again, so nothing ever matched
and one cheap week could be emailed five times over — see [The sampled dates sit
on a fixed calendar grid](#the-sampled-dates-sit-on-a-fixed-calendar-grid).

`--force-notify` ignores all of the above and re-sends every current deal.

## Making silence mean something

This bot is designed to be quiet. But a run that finds nothing looks **exactly
like** a broken SMTP password, an expired API key, or a workflow that quietly
stopped firing. You could go three months assuming fares were just high.

So every fortnight it emails you whether or not there's anything to report:

```
Still watching.

CHEAPEST NOW

  BNE → HND   AUD 611   (low)
    Fri 25 Dec → Wed 06 Jan 2027
    09:10 BNE → 17:55 NRT (9h 45m) · Jetstar JQ11
    https://www.google.com/travel/flights?...
    Best of 26 dates sampled · 36% under the median date (AUD 955)

  BNE → NRT   AUD 808   (low)
    Sun 01 Nov → Fri 13 Nov 2026
    09:10 BNE → 17:55 NRT (9h 45m) · Jetstar JQ11
    https://www.google.com/travel/flights?...
    Lowest since 23 Aug: AUD 611 on 23 Aug · Fri 30 Apr → Wed 12 May 2027
```

(That one is rendered from the bundled demo journal, so the prices are invented;
the shape is what a real digest looks like.)

**It reports two prices, and the difference matters.** The headline is what the
route costs *now* — the cheapest fare across every date the latest run checked,
and the only one that can still be booked, so it is the only one that gets a
link. `Lowest since …` underneath is the low-water mark for the period, plain text
and dated, shown only when it is genuinely lower than today.

**`Cheapest date in each month` is the shape of the year.** One row per
departure month, each naming the exact cheapest date in it and linking straight
to that search. It is built from the newest run, so every price in it is one you
can still book - which is why these carry links while `Lowest since …` above
deliberately does not. A month table is only printed when the run covered more
than one month; with a single month the headline already said it.

**`Best of N dates sampled` is the "when should I fly" answer.** A run prices
every sampled date at the same moment, so those prices are directly comparable
with no history and no extra searches — the numbers were already paid for and
used to be thrown away. It is the median, not the mean: one outlier at 3,000
drags a mean up far enough to make every other fare look like a bargain.

This is the one thing the alerting rules structurally cannot tell you (see
[How it decides something is a deal](#how-it-decides-something-is-a-deal)), and
it is here rather than in an alert on purpose. On a seasonal route the cheapest
sampled date is always well under the median, every single run, whether or not
any price moved. Weekly it would be wallpaper; monthly it is the shape of the
year. It appears only when the newest run priced at least four dates for that
route — under four there is no meaningful "typical date" to be under, which is
why the second route above has no such line.

**It goes out every fortnight**, not on the turn of the calendar month. Month
keying meant the gap between two digests could be anything from one day to
thirty-one depending on where in the month they landed, so no two reported on
comparable periods - and a quiet route could go five weeks without a word. A
fixed interval fixes both. `DIGEST_INTERVAL_DAYS` in
[config.py](flightbot/config.py) is the one number, alongside the horizon.

**It is named for the window it covers, deliberately.** The alert email's
percentile bar is drawn from all time; the digest looks back only as far as the
previous digest. Calling both of them "lowest seen" guarantees they eventually
disagree — a digest quoting a low *higher* than something you were emailed
about, which reads as a bug and isn't one. Naming the actual start date also
survives a missed run, which widens the window rather than losing it, and would
have made "this month" quietly wrong.

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
this file. `history.baselines()` works out, per departure date, the median
price previously recorded for it, and the bar a fare has to fall below —
read once at the start of a run, before any of that run's own rows are
written, so a fare is only ever compared against runs that came before it.
Routes with fewer than 30 usable readings are left out rather than guessed at.

Because probe dates now sit on a fixed grid, this file accumulates **repeated
readings of the same departure date** rather than a scatter of one-off ones —
which is what makes "is this cheaper than it was?" answerable at all, and what
the percentile is computed over.

None of those are answerable retrospectively, which is why it logs from the
start. At two routes it's about 206 rows a month — a few hundred KB a year.

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

A route is **three things**: origin, destination, on/off.

```
  Cabin [Economy ▾]   Trip length [12]   Stops [Nonstop only ▾]

  [BNE] → [NRT]     12 days     (on)   ×
  every 8th date · 90–300 days ahead · 02 Dec 2026 – 04 Jun 2027
```

Cabin, trip length and stops sit **once, above the cards**, because they apply
to every route. They were briefly per-route and it was the wrong shape: three
copies of a setting you only ever want one of, and trip length in particular
*must* be uniform or prices stop being comparable across dates.

They write into `defaults` in watches.json, and setting one also clears any copy
left on an individual route — the loader resolves these as
`route value, else default`, so a stale per-route value would shadow the shared
one and the control would look like it had done nothing.

Everything else is either derived or shared, which is why it isn't on the form:

| Was a field | Now |
|---|---|
| `label`, `id` | Derived from the airport codes (`BNE`+`NRT` → `bne-nrt`) |
| `step_days` | Computed from your budget and route count |
| `days_from_now_min/max` | Shared default, 90–300 days |
| `trip_length_days.min/max` | One shared number — a range breaks price comparability |
| passengers, currency, market | Shared `defaults` in watches.json |
| `hard_ceiling`, `never_alert_above` | Removed entirely |

**Stops is a decision-time filter, not a search-time one.** It didn't use to
be: `stops` was sent straight to the search, so a route with no matching
service got nothing back *and still spent a credit on every date* — verified
live on 24 Aug 2026, BNE→HND under nonstop-only: 24 searches a run, zero
results, forever, because Brisbane has no direct Haneda flight to filter down
to in the first place.

Every search now asks for everything regardless of the setting
([provider.py](flightbot/provider.py)'s `_params()` always sends `stops: 0`),
and the preference is applied afterwards, in [evaluate.py](flightbot/evaluate.py),
when deciding whether a fare is alert-worthy. Same one credit either way, but
now it comes back with a real price — the actual connecting fare BNE→HND would
book you onto — journalled like any other quote, just not treated as a deal.
The card still says why:

> 2 stops — outside your "nonstop only" setting

The drop-detection rule respects the same preference — a route with only
2-stop service can't have its cheapest 2-stop fare crowned "the drop of the
week" for want of anything nonstop to compare it against.

**The panel does not show a running cost, deliberately.** It used to: routes,
searches per run, a meter against the monthly cap. Every figure was accurate
and none of them was actionable — sampling density is derived, so there is no
dial to turn in response to the number, and a real run plans from the live
quota rather than the cap the panel drew its meter against. It was arithmetic
to be read and then ignored.

What that display was really guarding against now guards the **Add a route**
button instead, at the moment the decision is actually made. Adding a route
can never overspend — `plan_sampling` just samples every route more coarsely
to fit. What it *can* do is thin the sample until a route checks too few dates
to find anything, so the button disables itself at that point and says why:

> Another route would leave each one checking only 4 dates a run — too thin to
> find much. The monthly budget is fixed and split between routes, so pause or
> remove one first.

The floor is `MIN_DATES_PER_ROUTE` in [config.py](flightbot/config.py). The
projection behind it comes **from the bot itself** — the server runs the real
planner over a trial watchlist with one extra route — rather than from
JavaScript arithmetic that could drift out of step with it.

### A live preview of the digest, below the routes

The panel's **Digest** section renders the exact HTML a real send would
produce — same function, `notify.render_digest()`, not a JavaScript
reimplementation that could quietly drift from what actually gets mailed. It
sits in a sandboxed `<iframe>`, so it looks precisely as it would in an inbox.

It shows what the *next* digest will say, not a frozen copy of a past one —
nothing durably records what a previous digest actually said, only when it was
sent, so `history.since(state.last_digest_at(), ...)` is the honest thing to
compute it from. A fresh install with no history yet, or a route that has
never had a real search run against it, shows the same "not one search
returned a fare" card a real digest would send in that state — which is
exactly what this repository's own bundled panel shows until a real,
non-`--demo` run has actually searched something.

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
python check.py --digest         # send the 'still watching' summary now
python check.py --limit 6        # only the first 6 dates - cheap live smoke test

python -m unittest discover      # the test suite - stdlib only, under a second
```

## Layout

```
check.py              entry point
start-ui.bat          double-click to open the control panel
improvements.md       record of the first improvement pass, all of it shipped
watches.json          your routes, plus shared defaults and the budget
state.json            memory: alert history + monthly search spend
prices.jsonl          every fare ever seen, one JSON object per line
ui/index.html         the control panel - served by --ui, not opened directly
ui/airports.json      4009 airports for the search box (public domain)
tests/                the suite - stdlib unittest, no extra dependency
.claude/
  skills/update-readme/  how to keep this file true when behaviour changes
  hooks/readme-check.sh  flags a stale README at the end of a session
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

**An absolute price threshold.** The one case neither rule catches: a fare
cheap in dollars but rated `typical` by Google and not a big enough drop from
its own normal to trip the second rule either. It is the only rule that would
reflect a budget rather than the market's mood — and the only one that needs
revisiting as fares move. Worth adding only once the two current rules have
run long enough to show what they miss.

**Skipping dates that are reliably empty.** Some dates have no service at all
and are re-checked, and re-charged, every run. Empty results are already
journalled, so a date that came back empty on the last N runs could be
skipped. This one adds machinery rather than removing it, so it is only worth
doing if the digest shows a meaningful `empty` count.

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
