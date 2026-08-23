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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

UI_DIR = config.ROOT / "ui"


def _summary(raw: dict) -> dict:
    """Everything the page needs that only the bot knows how to work out.

    Sampling density is computed here rather than mirrored in JavaScript, so
    there is exactly one implementation of the budget maths and the panel can
    never disagree with what a run will actually do.
    """
    watches, budget = config.load_watchlist_data(raw)
    config.plan_sampling(watches, budget)

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
        "active": sum(1 for w in watches if w.enabled),
        "window": {
            "days_from_now_min": watches[0].days_from_now_min if watches else 60,
            "days_from_now_max": watches[0].days_from_now_max if watches else 300,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
