"""Integration fixtures: a real PostgreSQL and a fake VictoriaMetrics.

The database tests need a PostgreSQL server reachable through the usual PG* environment
variables (PGHOST, PGUSER, PGPASSWORD, ...), with a role allowed to create databases. They are
skipped when PGHOST is unset. scripts/test.sh and the GitHub workflow both provide one.
"""

import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from cgm_bridge import db
from cgm_bridge.config import Config
from cgm_bridge.exporter import Exporter
from cgm_bridge.server import App, make_server

SECRET = "correct-horse-battery-staple"


@pytest.fixture(scope="session")
def database():
    if not os.environ.get("PGHOST"):
        pytest.skip("PGHOST not set; skipping PostgreSQL integration tests")
    name = f"cgm_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield name
    with psycopg.connect(dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def pool(database):
    pool = ConnectionPool(f"dbname={database}", min_size=1, max_size=4, open=True)
    db.migrate(pool)
    yield pool
    pool.close()


@pytest.fixture(autouse=True)
def _clean(request):
    if "pool" in request.fixturenames:
        pool = request.getfixturevalue("pool")
        with pool.connection() as conn:
            conn.execute("TRUNCATE glucose, treatments")
    yield


class FakeVM:
    """Records /api/v1/import bodies; answers with `status`."""

    def __init__(self):
        self.bodies: list[bytes] = []
        self.status = 204
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/v1/import" and fake.status < 300:
                    fake.bodies.append(body)
                self.send_response(fake.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_vm():
    vm = FakeVM()
    yield vm
    vm.close()


@pytest.fixture
def exporter(pool, fake_vm):
    # Not started: tests drive export_once() directly so they stay deterministic.
    return Exporter(pool, fake_vm.url, "cgm_glucose_mg_dl", batch_size=2, timeout=5)


@pytest.fixture
def api(pool, exporter):
    """Base URL of a running server wired to the test database and fake VM."""
    config = Config.from_secret(SECRET, listen_host="127.0.0.1", listen_port=0, max_body_bytes=4096)
    server = make_server(App(config, pool, exporter))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
