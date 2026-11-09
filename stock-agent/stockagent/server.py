"""The simulated trading platform's local HTTP surface: a dashboard for
people and a JSON API for agents (the same broker the autonomous agent uses).

    GET    /                 dashboard
    GET    /api/state        everything: account, pace, positions, orders, trades, tips, journal
    GET    /api/<section>    one section of the state (account, positions, orders, trades, tips, ...)
    POST   /api/orders       {"symbol","side","qty","type","limit_price","stop_price","tif"}
    DELETE /api/orders/<id>  cancel

Binds to 127.0.0.1 by default. It is a paper account, but do not expose it.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import clock as mcal
from .broker import SimBroker
from .config import Config
from .data.http import DataError
from .data.provider import MarketData
from .db import Database
from .report import build_state

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web"


def make_handler(cfg: Config, db: Database, broker: SimBroker, clk: mcal.Clock, md: MarketData | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stockagent/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if path.startswith("/api/"):
                section = path[len("/api/"):].strip("/")
                state = build_state(cfg, db, broker, clk)
                if section in ("", "state"):
                    self._json(200, state)
                elif section in state:
                    self._json(200, state[section])
                else:
                    self._json(404, {"error": f"unknown section {section!r}", "sections": sorted(state)})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/orders":
                self._json(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                order = broker.submit_order(
                    str(body["symbol"]), str(body["side"]), int(body["qty"]), str(body.get("type", "limit")),
                    limit_price=body.get("limit_price"), stop_price=body.get("stop_price"),
                    tif=str(body.get("tif", "day")), strategy="manual", reason=str(body.get("reason", "manual order")),
                )
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": f"bad order: {exc}"})
                return
            if order.get("status") == "rejected":
                self._json(422, order)
                return
            if md is not None and mcal.market_phase(clk.now()) == "open":
                try:
                    q = md.quotes([order["symbol"]]).get(order["symbol"])
                    if q:
                        broker.update_mark(q)
                        broker.process_quote(q)
                except DataError as exc:
                    log.info("no quote to fill manual order now: %s", exc)
            self._json(201, broker.order(order["id"]))

        def do_DELETE(self) -> None:  # noqa: N802
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "orders"] and parts[2].isdigit():
                broker.cancel_order(int(parts[2]), "cancelled via API")
                self._json(200, broker.order(int(parts[2])))
                return
            self._json(404, {"error": "not found"})

    return Handler


def make_server(cfg: Config, db: Database, broker: SimBroker, clk: mcal.Clock, host: str, port: int,
                md: MarketData | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(cfg, db, broker, clk, md))


def serve(cfg: Config, db: Database, broker: SimBroker, clk: mcal.Clock, host: str = "127.0.0.1", port: int = 8765,
          md: MarketData | None = None) -> None:
    httpd = make_server(cfg, db, broker, clk, host, port, md)
    print(f"dashboard: http://{host}:{httpd.server_address[1]}/   (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
