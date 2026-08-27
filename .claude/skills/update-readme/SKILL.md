---
name: update-readme
description: Refresh README.md after changing this repo's behaviour. Use whenever flightbot/*.py, ui/index.html, check.py, watches.json defaults, or .github/workflows have changed in a way a reader would notice - new or removed alerting rules, changed sampling/budget maths, renamed CLI flags, new config keys, changed email or digest output, new UI controls. Also use when the README staleness hook asks for it, or when the user says "update the readme", "docs are stale", "document this change".
---

# Keeping README.md true

The README is the only documentation this project has, and it is unusually
specific: it quotes real numbers, real output, and real reasons. That is what
makes it useful and also what makes it rot the moment behaviour changes.

**The rule: the README describes what the code does today, and says why.**
Never let it describe an intention, a plan, or how something used to work.

## Before editing anything

1. See what actually changed:
   `git diff --stat` and `git diff -- flightbot/ ui/ check.py watches.json`
2. Read the sections that cover it (headings list: `grep -n '^#\{1,3\} ' README.md`)
3. **Verify every number you are about to write** by running the code, not by
   reasoning about it. This README's credibility rests on its numbers being
   real. Cheap ways to get them:
   - sampling / budget / window: load `watches.json` through
     `config.load_watchlist_data` + `config.plan_sampling`, print step, probe
     count, first/last departure
   - alerting behaviour and terminal output: `python check.py --demo --dry-run --no-colour`
   - digest text: build `history.digest(...)` over `prices.demo.jsonl` and render
     with `notify._digest_plain`
   - anything with `→` in it: prefix the command with `PYTHONIOENCODING=utf-8`,
     the Windows console is cp1252 and will crash on the arrow

## What lives where

| Section | Owned by | Update when |
|---|---|---|
| How the whole thing works | `cli.py` main flow | run order, new step, new guard |
| How it decides something is a deal | `evaluate.py`, `history.baselines` | any alerting rule change |
| How it searches | `config.probes`, `plan_sampling` | window, step, lattice, horizon |
| Two hard limits | `MAX_HORIZON_DAYS`, budget cap | only when re-verified live |
| Running it automatically | `.github/workflows/check-flights.yml` | schedule change |
| Not getting spammed | `state.py`, `NotifyRules` | de-dup or cooldown change |
| Making silence mean something | digest path in `notify.py` | digest content change |
| The price record | `history.py` row shape | new/removed journal field |
| Adding and removing routes | `ui/index.html`, `ui_server.py` | new UI control |
| Commands | `cli.py` argparse | flag added/renamed/removed |
| Layout | file tree | file added or removed |
| Future plans / Status | `improvements.md` | something shipped |

## Style rules this README already follows - keep them

- **Say why, not just what.** Nearly every claim carries its reason. A change
  that removes a rule should say what replaced it and why the old one failed.
- **Numbers are real and stated plainly** - "26 searches", "AUD 681", "9 days".
  If you cannot verify a number, do not write it.
- **Reversals are documented, not erased.** The README and `watches.json` both
  record things that were tried and abandoned (nonstop-only, the local spend
  tally). If this change reverses an earlier decision, say so in one line - that
  history is why nobody re-tries a dead end.
- **No marketing.** No "powerful", "seamless", "robust". Flat declaratives.
- **Prose over bullets** for explanation; tables only for genuine lookups.
- Keep heading levels and section order stable - people link to them.

## Finishing

- Move anything now shipped out of **Future plans** and into the section that
  describes it; keep `improvements.md` and the README consistent with each other.
- Re-read the diff you just wrote and check no stale sentence survives *next to*
  the one you updated - the usual failure is a correct new paragraph sitting
  above an old one that contradicts it.
- Tell the user which sections you touched and which numbers you re-verified.
- Do not commit unless asked.
