"""HTTP API: the subset of Nightscout v1 that uploaders write to.

    POST /api/v1/entries             glucose readings (type "sgv")
    PUT /api/v1/treatments           treatments, upserted by _id (Juggluco's v1 uploader uses
                                     PUT; POST is accepted too)
    DELETE /api/v1/treatments/<id>   remove one treatment (Juggluco deletes, then re-posts, on edit)
    GET /healthz                     process is up
    GET /readyz                      database is reachable

Uploads need the `api-secret` header. Juggluco treats anything but HTTP 200 as a failure and
retries later, which is how outages are backfilled, so every successful write answers 200.
"""

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg_pool import ConnectionPool

from . import db, metrics
from .config import Config
from .exporter import Exporter
from .nightscout import PayloadError, load_json, parse_entries, parse_treatments

log = logging.getLogger(__name__)

ENTRIES_PATHS = {"/api/v1/entries", "/api/v1/entries.json"}
TREATMENTS_PATHS = {"/api/v1/treatments", "/api/v1/treatments.json"}
TREATMENT_PREFIX = "/api/v1/treatments/"
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,8}")
# Uploads parsed at once. A request body costs several times its size in memory while being
# parsed, so bound the total instead of trusting every client to send small ones.
MAX_CONCURRENT_UPLOADS = 2


class App:
    """Everything a request handler needs, independent of the HTTP plumbing."""

    def __init__(self, config: Config, pool: ConnectionPool, exporter: Exporter | None):
        self.config = config
        self.pool = pool
        self.exporter = exporter
        self._latest_lock = threading.Lock()
        self._latest = 0.0
        self.upload_slots = threading.BoundedSemaphore(MAX_CONCURRENT_UPLOADS)

    def note_latest(self, timestamp: float) -> None:
        with self._latest_lock:
            if timestamp > self._latest:
                self._latest = timestamp
                metrics.LATEST_READING.set(timestamp)

    def authorized(self, header: str | None) -> bool:
        if not header:
            return False
        offered = header.strip().lower()
        # Uploaders send SHA-1(secret); a few send the secret itself. Compare digests of equal
        # length in constant time either way.
        as_hash = hmac.compare_digest(offered.encode(), self.config.api_secret_sha1.encode())
        as_plain = hmac.compare_digest(
            hashlib.sha256(header.strip().encode()).hexdigest().encode(),
            self.config.api_secret_sha256.encode(),
        )
        return as_hash or as_plain

    def store_entries(self, body: bytes) -> dict[str, int]:
        readings, skipped = parse_entries(load_json(body))
        with self.pool.connection() as conn:
            inserted, updated = db.upsert_glucose(conn, readings)
        unchanged = len(readings) - inserted - updated
        metrics.DOCUMENTS.labels("entry", "inserted").inc(inserted)
        metrics.DOCUMENTS.labels("entry", "updated").inc(updated)
        metrics.DOCUMENTS.labels("entry", "duplicate").inc(unchanged)
        metrics.DOCUMENTS.labels("entry", "skipped").inc(skipped)
        if readings:
            self.note_latest(max(r.time for r in readings).timestamp())
        if (inserted or updated) and self.exporter:
            self.exporter.wake()
        return {
            "received": len(readings),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
        }

    def store_treatments(self, body: bytes) -> dict[str, int]:
        treatments, skipped = parse_treatments(load_json(body))
        with self.pool.connection() as conn:
            upserted = db.upsert_treatments(conn, treatments)
        metrics.DOCUMENTS.labels("treatment", "upserted").inc(upserted)
        metrics.DOCUMENTS.labels("treatment", "skipped").inc(skipped)
        return {"received": len(treatments), "upserted": upserted, "skipped": skipped}

    def delete_treatment(self, ident: str) -> dict[str, int]:
        with self.pool.connection() as conn:
            deleted = db.delete_treatment(conn, ident)
        metrics.DOCUMENTS.labels("treatment", "deleted").inc(int(deleted))
        # Deleting something that is not there still succeeds: Juggluco would otherwise retry
        # the delete forever and never upload anything after it.
        return {"deleted": int(deleted)}

    def ready(self) -> bool:
        try:
            with self.pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception as exc:
            log.warning("readiness check failed: %s", exc)
            return False


class Handler(BaseHTTPRequestHandler):
    server_version = "cgm-bridge"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # Socket timeout: drops idle keep-alive connections and stalled uploads.
    timeout = 300
    app: App  # set on the subclass built by make_server()

    # --- dispatch ---------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        self._body_read = False
        path = urlsplit(self.path).path
        route, status = "other", HTTPStatus.NOT_FOUND
        try:
            route, status, payload = self._route(method, path)
        except PayloadError as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except _BodyTooLarge:
            status, payload = HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"}
        except _LengthRequired:
            status, payload = HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"}
        except _Busy:
            status, payload = HTTPStatus.SERVICE_UNAVAILABLE, {"error": "busy, retry later"}
        except psycopg.Error as exc:
            # Database error text can quote the offending row, i.e. health data: log only the
            # class and SQLSTATE. Juggluco gets a 500 and retries later.
            log.error(
                "database error handling %s %s: %s (SQLSTATE %s)",
                method,
                route,
                type(exc).__name__,
                exc.sqlstate,
            )
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
        except Exception as exc:
            log.error(
                "error handling %s %s: %s",
                method,
                route,
                type(exc).__name__,
                exc_info=log.isEnabledFor(logging.DEBUG),
            )
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
        # A body left unread would be parsed as the next request on this keep-alive
        # connection (and upstream proxies do reuse connections), so hang up instead.
        if self._has_body() and not self._body_read:
            self.close_connection = True
        self._send(status, payload)
        elapsed = time.monotonic() - started
        metrics.REQUESTS.labels(route, method, str(int(status))).inc()
        metrics.REQUEST_SECONDS.labels(route).observe(elapsed)
        log.info("%s %s %d %.3fs", method, route, status, elapsed)

    def _route(self, method: str, path: str) -> tuple[str, HTTPStatus, Any]:
        if path == "/healthz" and method == "GET":
            return "healthz", HTTPStatus.OK, {"status": "ok"}
        if path == "/readyz" and method == "GET":
            ok = self.app.ready()
            return (
                "readyz",
                HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE,
                {"status": "ok" if ok else "unavailable"},
            )

        if path in ENTRIES_PATHS:
            route, allowed = "entries", {"POST"}
        elif path in TREATMENTS_PATHS:
            route, allowed = "treatments", {"POST", "PUT"}
        elif path.startswith(TREATMENT_PREFIX) and len(path) > len(TREATMENT_PREFIX):
            route, allowed = "treatment", {"DELETE"}
        else:
            return "other", HTTPStatus.NOT_FOUND, {"error": "not found"}

        if method not in allowed:
            return route, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}
        if not self.app.authorized(self.headers.get("api-secret")):
            return route, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}

        if route == "treatment":
            ident = unquote(path[len(TREATMENT_PREFIX) :])
            return route, HTTPStatus.OK, self.app.delete_treatment(ident)
        if not self.app.upload_slots.acquire(timeout=30):
            raise _Busy()
        try:
            body = self._read_body()
            if route == "entries":
                return route, HTTPStatus.OK, self.app.store_entries(body)
            return route, HTTPStatus.OK, self.app.store_treatments(body)
        finally:
            self.app.upload_slots.release()

    # --- I/O ----------------------------------------------------------------

    def _read_body(self) -> bytes:
        limit = self.app.config.max_body_bytes
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = self._read_chunked(limit)
        else:
            raw = self.headers.get("Content-Length")
            if raw is None or not raw.isdigit():
                raise _LengthRequired()
            length = int(raw)
            if length > limit:
                raise _BodyTooLarge()
            body = self.rfile.read(length)
            if len(body) != length:
                raise PayloadError("body shorter than Content-Length")
        self._body_read = True
        return body

    def _read_chunked(self, limit: int) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(1024)
            # Strict hex only: int(x, 16) would also accept "0x10", "1_0" or "+a".
            size_field = line.split(b";", 1)[0].strip()
            if not _CHUNK_SIZE.fullmatch(size_field):
                raise PayloadError("malformed chunked body")
            size = int(size_field, 16)
            if size == 0:
                break
            if size < 0 or len(body) + size > limit:
                raise _BodyTooLarge()
            chunk = self.rfile.read(size + 2)  # data + CRLF
            if len(chunk) != size + 2:
                raise PayloadError("truncated chunked body")
            body += chunk[:size]
        # Trailer section, ended by an empty line.
        while (line := self.rfile.readline(1024)) not in (b"\r\n", b"\n", b""):
            pass
        return bytes(body)

    def _has_body(self) -> bool:
        return "Transfer-Encoding" in self.headers or self.headers.get(
            "Content-Length", "0"
        ).strip() not in ("", "0")

    def _send(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Replaced by the structured line in _dispatch; the default logs raw paths.
        pass


class _Busy(Exception):
    pass


class _BodyTooLarge(Exception):
    pass


class _LengthRequired(Exception):
    pass


def make_server(app: App) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((app.config.listen_host, app.config.listen_port), handler)
    server.daemon_threads = True
    return server
