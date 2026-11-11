"""
run_dashboard.py -- Read-only portfolio dashboard (stdlib HTTP server, no new dependencies)
===========================================================================================
Shows every strategy's open positions (entry date/price, last close, gain), cash, and total
value straight from MongoDB, so nobody has to query the database by hand.

Usage:
    python scripts/run_dashboard.py                      # 127.0.0.1:8787, no auth (loopback only)
    DASHBOARD_HOST=0.0.0.0 DASHBOARD_PASSWORD=... python scripts/run_dashboard.py

Environment (also read from .env):
    DASHBOARD_HOST      bind address, default 127.0.0.1
    DASHBOARD_PORT      default 8787
    DASHBOARD_USER      HTTP basic-auth user, default "admin"
    DASHBOARD_PASSWORD  required whenever DASHBOARD_HOST is not loopback; the server refuses to start without it

Only GET is served; nothing is ever written to the database. HTTP basic auth is NOT encrypted:
over the open internet put it behind HTTPS (reverse proxy) or reach it through an SSH tunnel.
"""

import base64
import hmac
import ipaddress
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:   # python-dotenv is a declared requirement; tolerate its absence in odd envs
    pass

from nse_quant_lab.dashboard import PAGE_HTML, VALID_MODES, build_snapshot
from nse_quant_lab.db.mongo_client import MongoDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def auth_ok(header, user: str, password: str) -> bool:
    """Constant-time check of an `Authorization: Basic ...` header. No password configured -> open."""
    if not password:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        supplied = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except Exception:
        return False
    expected = f"{user}:{password}"
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def make_handler(get_db, user: str, password: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QuantLabDashboard"
        sys_version = ""

        def _send(self, code: int, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not auth_ok(self.headers.get("Authorization"), user, password):
                self._send(401, b"Authentication required", "text/plain; charset=utf-8",
                           {"WWW-Authenticate": 'Basic realm="Quant Lab"'})
                return
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/portfolio":
                mode = (parse_qs(url.query).get("mode") or ["paper"])[0]
                if mode not in VALID_MODES:
                    self._send(400, b"bad mode", "text/plain; charset=utf-8")
                    return
                try:
                    payload = json.dumps(build_snapshot(get_db(), mode), allow_nan=False)
                except Exception as exc:   # fail loud to the viewer and the log; never serve a guessed snapshot
                    logger.exception("snapshot failed")
                    self._send(503, f"Data unavailable: {type(exc).__name__}".encode(), "text/plain; charset=utf-8")
                    return
                self._send(200, payload.encode("utf-8"), "application/json")
            elif url.path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def _method_not_allowed(self):
            self._send(405, b"read-only", "text/plain; charset=utf-8", {"Allow": "GET"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed

        def log_message(self, fmt, *args):
            logger.info("%s %s", self.address_string(), fmt % args)

    return Handler


def main() -> int:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))
    user = os.environ.get("DASHBOARD_USER", "admin")
    password = os.environ.get("DASHBOARD_PASSWORD", "")

    if not is_loopback(host) and not password:
        logger.error("Refusing to bind %s without DASHBOARD_PASSWORD (portfolio data would be public).", host)
        return 2

    mongo = MongoDatabase.get_instance()
    if not mongo.ping():
        logger.error("Cannot connect to MongoDB; dashboard not started.")
        return 1

    handler = make_handler(lambda: MongoDatabase.get_instance().db, user, password)
    server = ThreadingHTTPServer((host, port), handler)
    logger.info("Dashboard listening on http://%s:%d (auth %s)", host, port, "ON" if password else "OFF (loopback only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
