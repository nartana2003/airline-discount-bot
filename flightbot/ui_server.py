"""A tiny local server so the control panel can read and write watches.json.

A double-clicked `file://` page can't read a local file without a picker, which
is why the old UI opened empty and made you hunt for watches.json every time.
Serving the page from localhost removes that entirely: it loads your routes on
open and saves straight back.

Deliberately stdlib-only (no Flask) - this exists to edit one JSON file on your
own machine, and binds to 127.0.0.1 so nothing off this machine can reach it.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, history, notify
from .state import State

UI_DIR = config.ROOT / "ui"


def _digest_payload() -> dict:
    """Render the digest through the exact same code a real send uses, so the
    panel can never show a preview that disagrees with the email.

    Covers everything since the last one actually sent, same as a real send -
    so this doubles as a live preview of what the NEXT digest will contain,
    not a frozen copy of a past one. Nothing durably records what a past
    digest said, only when it was sent, so that is the honest thing to show.
    """
    state = State.load()
    since_at = state.last_digest_at()
    rows = history.since(since_at, history.HISTORY_PATH)
    d = history.digest(rows)

    try:
        raw = json.loads(config.WATCHES_PATH.read_text(encoding="utf-8"))
        watches, _ = config.load_watchlist_data(raw)
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        watches = []
    subject, html = notify.render_digest(d, watches)

    return {
        "html": html,
        "subject": subject,
        "last_sent_at": since_at,
        "due": state.digest_due(),
    }


def _live_plan_inputs(runs_per_month: float) -> tuple[int | None, float | None, dict | None]:
    """(available, runs_remaining, quota) - the same numbers a real run would
    plan from, read from state.json rather than fetched.

    The panel has no API key, and a network round trip to render a form is the
    wrong trade. This is instead the exact reading the last real run recorded
    - so a projection built from it cannot disagree with what that run
    actually did - and `quota` carries its own date so staleness is visible
    rather than assumed.

    All three come back None together when no real run has ever recorded a
    reading: a fresh install, or one that has only ever run `--demo`. There is
    nothing to plan against yet, so every projection falls back to the
    declared cap and the average cadence, same as a `--demo` run does.
    """
    try:
        st = State.load()
    except OSError:
        return None, None, None
    q = st.quota()
    if q is None:
        return None, None, None
    return q["left"], st.runs_remaining(runs_per_month), q


def _next_route(watches: list, budget, available: int | None = None,
                runs_remaining: float | None = None) -> dict:
    """What adding one more route would do to the run, and whether to allow it.

    The budget is fixed, so an extra route never overspends - plan_sampling
    just makes every route sample more coarsely to fit. What actually breaks is
    usefulness: split far enough, each route checks so few dates that the run
    stops being a search and becomes a spot check.

    Worked out by running the real planner over a trial list rather than
    repeating its arithmetic in JavaScript, for the same reason the rest of
    this function exists - two implementations of the budget maths would
    eventually disagree. The trial copies an existing watch, so its window and
    trip length are realistic; `replace` keeps the originals unplanned.

    `available`/`runs_remaining` are the live figures from `_live_plan_inputs`,
    threaded through rather than re-read here so the whole page is built from
    one consistent snapshot. Left at their defaults - the tests do this - the
    projection falls back to the declared cap and the average cadence, exactly
    like `plan_sampling` itself does.
    """
    active = [w for w in watches if w.enabled]
    if not active:
        # Nothing to extrapolate from, and the first route is always allowed.
        return {"ok": True, "per_route": None, "per_run": None, "per_month": None}

    trial = [replace(w) for w in active] + [replace(active[-1], id="__trial__")]
    config.plan_sampling(trial, budget, available, runs_remaining)
    counts = [len(w.probes()) for w in trial]
    per_run = sum(counts)
    per_month = round(per_run * budget.runs_per_month)

    return {
        "ok": min(counts) >= config.MIN_DATES_PER_ROUTE and per_month <= budget.monthly_search_cap,
        "per_route": min(counts),
        "per_run": per_run,
        "per_month": per_month,
        "floor": config.MIN_DATES_PER_ROUTE,
    }


def _summary(raw: dict) -> dict:
    """Everything the page needs that only the bot knows how to work out.

    Sampling density is computed here rather than mirrored in JavaScript, so
    there is exactly one implementation of the budget maths and the panel can
    never disagree with what a run will actually do.
    """
    watches, budget = config.load_watchlist_data(raw)
    available, runs_remaining, quota = _live_plan_inputs(budget.runs_per_month)
    config.plan_sampling(watches, budget, available, runs_remaining)

    routes = []
    for w in watches:
        probes = w.probes()
        routes.append({
            "id": w.id,
            "step_days": w.step_days,
            "searches_per_run": len(probes),
            "first_depart": f"{probes[0].depart:%d %b %Y}" if probes else None,
            "last_depart": f"{probes[-1].depart:%d %b %Y}" if probes else None,
        })

    per_run = sum(r["searches_per_run"] for r, w in zip(routes, watches) if w.enabled)
    per_month = round(per_run * budget.runs_per_month)
    return {
        "routes": routes,
        "per_run": per_run,
        "per_month": per_month,
        "cap": budget.monthly_search_cap,
        "runs_per_month": budget.runs_per_month,
        "next_route": _next_route(watches, budget, available, runs_remaining),
        # The declared cap is what THIS plan was costed against; the quota is
        # what a real run would plan from. Sent separately rather than swapped
        # in, so the panel can show the plan and the reality side by side.
        "quota": quota,
        "active": sum(1 for w in watches if w.enabled),
        # Taken from a real watch, falling back to the dataclass defaults rather
        # than to numbers typed here - a second copy of the window would go
        # stale the moment watches.json changed, and the panel would quietly
        # report a range the bot does not search.
        "window": {
            "days_from_now_min": (watches[0] if watches else config.Watch).days_from_now_min,
            "days_from_now_max": (watches[0] if watches else config.Watch).days_from_now_max,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path in ("/", "/index.html"):
            try:
                body = (UI_DIR / "index.html").read_bytes()
            except OSError as exc:
                self._send(500, f"cannot read ui/index.html: {exc}".encode(), "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return

        if self.path == "/airports.json":
            try:
                body = (UI_DIR / "airports.json").read_bytes()
            except OSError as exc:
                self._json(500, {"error": f"cannot read ui/airports.json: {exc}"})
                return
            # Static reference data - never changes between runs.
            self._send(200, body, "application/json", cache="max-age=86400")
            return

        # Cheap liveness check. If the page can load but this can't, something
        # between the browser and here is filtering requests.
        #
        # This one endpoint allows cross-origin reads so that a copy of the
        # panel opened as a file:// page - which can't reach its own routes -
        # can still tell whether the real server is up and link you to it. It
        # returns nothing but {"ok": true}, so there's nothing to leak.
        if self.path == "/api/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            body = b'{"ok": true}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/watches":
            try:
                raw = json.loads(config.WATCHES_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw = {"watches": []}
            except (OSError, json.JSONDecodeError) as exc:
                self._json(500, {"error": f"watches.json is unreadable: {exc}"})
                return
            try:
                self._json(200, {"doc": raw, "summary": _summary(raw)})
            except (KeyError, ValueError, TypeError) as exc:
                self._json(200, {"doc": raw, "summary": None, "warning": str(exc)})
            return

        if self.path == "/api/digest":
            # Read-only preview, isolated from route editing: a broken journal
            # or watchlist here must never stop the routes screen from working.
            try:
                self._json(200, _digest_payload())
            except (OSError, KeyError, ValueError, TypeError) as exc:
                self._json(500, {"error": f"could not build the digest preview: {exc}"})
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/api/watches", "/api/preview"):
            self._send(404, b"not found", "text/plain")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            doc = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"error": f"invalid JSON: {exc}"})
            return

        # Validate by loading it the same way a run would. A file that can't be
        # loaded must never reach disk - that would break the scheduled job.
        try:
            summary = _summary(doc)
        except (KeyError, ValueError, TypeError) as exc:
            self._json(400, {"error": f"that watchlist wouldn't load: {exc}"})
            return

        # /api/preview re-costs the budget as you type without touching disk.
        if self.path == "/api/preview":
            self._json(200, {"summary": summary})
            return

        try:
            config.WATCHES_PATH.write_text(
                json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            self._json(500, {"error": f"could not write watches.json: {exc}"})
            return
        self._json(200, {"ok": True, "summary": summary})

    def log_message(self, *args) -> None:
        """Silence per-request logging - the terminal is the bot's output."""


def serve(port: int = 8765, open_browser: bool = True) -> int:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"could not start on port {port}: {exc}")
        print(f"something else is probably using it - try: python check.py --ui --port {port + 1}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    print(f"Control panel: {url}")
    print(f"Editing: {config.WATCHES_PATH}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0
