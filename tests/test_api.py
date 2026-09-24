import hashlib
import http.client
import json
import threading
from urllib.parse import urlsplit

import pytest
from psycopg_pool import ConnectionPool

from cgm_bridge import db, metrics
from cgm_bridge.config import Config
from cgm_bridge.exporter import ExportError
from cgm_bridge.server import App, make_server

from . import fixtures
from .conftest import SECRET

HASHED = hashlib.sha1(SECRET.encode()).hexdigest()


def request(base, method, path, body=None, secret=HASHED, headers=None):
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    if secret is not None:
        all_headers["api-secret"] = secret
    conn.request(method, path, body=body, headers=all_headers)
    response = conn.getresponse()
    payload = json.loads(response.read() or b"null")
    conn.close()
    return response.status, payload


def rows(pool, query):
    with pool.connection() as conn:
        return conn.execute(query).fetchall()


# --- auth and routing ----------------------------------------------------------------------


@pytest.mark.parametrize("secret", [None, "", "wrong", hashlib.sha1(b"wrong").hexdigest()])
def test_rejects_bad_secret(api, pool, secret):
    status, _ = request(api, "POST", "/api/v1/entries", fixtures.ENTRIES, secret=secret)
    assert status == 401
    assert rows(pool, "SELECT count(*) FROM glucose") == [(0,)]


@pytest.mark.parametrize("secret", [HASHED, HASHED.upper(), SECRET])
def test_accepts_hashed_or_plain_secret(api, secret):
    status, _ = request(api, "POST", "/api/v1/entries", fixtures.ENTRIES, secret=secret)
    assert status == 200


def test_routing(api):
    assert request(api, "GET", "/healthz", secret=None)[0] == 200
    assert request(api, "GET", "/readyz", secret=None)[0] == 200
    assert request(api, "GET", "/api/v1/entries")[0] == 405
    assert request(api, "GET", "/api/v1/status.json")[0] == 404
    assert request(api, "POST", "/api/v1/treatments/", b"{}")[0] == 404


def test_bad_bodies(api):
    assert request(api, "POST", "/api/v1/entries", b"[{")[0] == 400
    assert request(api, "POST", "/api/v1/entries", b"x" * 5000)[0] == 413


# --- entries --------------------------------------------------------------------------------


def test_entries_are_stored_once(api, pool):
    status, body = request(api, "POST", "/api/v1/entries", fixtures.ENTRIES)
    assert (status, body) == (200, {"received": 2, "inserted": 2, "updated": 0, "skipped": 0})
    # "Resend data" in Juggluco, or a retry after a lost response.
    status, body = request(api, "POST", "/api/v1/entries", fixtures.ENTRIES)
    assert (status, body) == (200, {"received": 2, "inserted": 0, "updated": 0, "skipped": 0})
    assert rows(pool, "SELECT device, mg_dl, direction FROM glucose ORDER BY time") == [
        ("3MH00ABCDE", 112, "Flat"),
        ("3MH00ABCDE", 115, "FortyFiveUp"),
    ]


def test_undetermined_trend_stores_null_delta(api, pool):
    assert request(api, "POST", "/api/v1/entries", fixtures.ENTRIES_UNDETERMINED)[0] == 200
    assert rows(pool, "SELECT delta, direction FROM glucose ORDER BY time") == [
        (None, None),
        (1.0, "Flat"),
    ]


def test_corrected_resend_updates_and_reexports(api, pool, exporter, fake_vm):
    # Juggluco calibrates at upload time: after a recalibration, "Resend data" sends new values
    # for readings that are already stored.
    request(api, "POST", "/api/v1/entries", fixtures.ENTRIES)
    assert exporter.export_once() == 2
    corrected = fixtures.ENTRIES.replace(b'"sgv":112', b'"sgv":130')
    status, body = request(api, "POST", "/api/v1/entries", corrected)
    assert (status, body) == (200, {"received": 2, "inserted": 0, "updated": 1, "skipped": 0})
    assert rows(pool, "SELECT mg_dl FROM glucose ORDER BY time") == [(130,), (115,)]
    assert exporter.export_once() == 1
    assert json.loads(fake_vm.bodies[-1])["values"] == [130]


# --- VictoriaMetrics export -----------------------------------------------------------------


def test_export_pushes_and_marks_rows(api, pool, exporter, fake_vm):
    request(api, "POST", "/api/v1/entries", fixtures.ENTRIES)
    request(api, "POST", "/api/v1/entries", fixtures.ENTRIES_UNDETERMINED)

    # batch_size=2 in the fixture: four readings take two batches, then nothing is left.
    assert [exporter.export_once() for _ in range(3)] == [2, 2, 0]
    assert rows(pool, "SELECT count(*) FROM glucose WHERE NOT exported") == [(0,)]

    first = json.loads(fake_vm.bodies[0])
    assert first == {
        "metric": {"__name__": "cgm_glucose_mg_dl", "device": "3MH00ABCDE"},
        "values": [112, 115],
        "timestamps": [fixtures.T0, fixtures.T0 + 60_000],
    }


def test_export_failure_keeps_rows_pending(api, pool, exporter, fake_vm):
    request(api, "POST", "/api/v1/entries", fixtures.ENTRIES)
    fake_vm.status = 503
    for _ in range(3):
        with pytest.raises(ExportError):
            exporter.export_once()
    assert rows(pool, "SELECT count(*) FROM glucose WHERE NOT exported") == [(2,)]
    # The "export stuck" alert watches this gauge, so it must move while exports fail.
    assert metrics.EXPORT_PENDING._value.get() == 2

    fake_vm.status = 204
    assert exporter.export_once() == 2
    assert rows(pool, "SELECT count(*) FROM glucose WHERE NOT exported") == [(0,)]
    assert metrics.EXPORT_PENDING._value.get() == 0


def test_resent_entries_are_not_exported_twice(api, exporter, fake_vm):
    request(api, "POST", "/api/v1/entries", fixtures.ENTRIES)
    assert exporter.export_once() == 2
    request(api, "POST", "/api/v1/entries", fixtures.ENTRIES)
    assert exporter.export_once() == 0
    assert len(fake_vm.bodies) == 1


# --- treatments -----------------------------------------------------------------------------


def test_treatment_lifecycle(api, pool):
    for body in (fixtures.TREATMENT_RAPID, fixtures.TREATMENT_CARBS, fixtures.TREATMENT_BLOOD):
        assert request(api, "POST", "/api/v1/treatments", body)[0] == 200
    assert rows(pool, "SELECT label, amount FROM treatments ORDER BY time") == [
        ("Fast Insulin", 4.0),
        ("carbs", 45.0),
        ("Blood", pytest.approx(7.2)),
    ]

    # Juggluco edits an amount by deleting it and posting it again under the same id.
    ident = "ba0e14bbbbbbbbbbbbbbbbbb"
    assert request(api, "DELETE", f"/api/v1/treatments/{ident}") == (200, {"deleted": 1})
    edited = fixtures.TREATMENT_BLOOD.replace(b"Blood 7.2", b"Blood 6.8")
    assert request(api, "POST", "/api/v1/treatments", edited)[0] == 200
    assert request(api, "PUT", "/api/v1/treatments", edited)[0] == 200  # upsert, no duplicate
    assert rows(pool, f"SELECT amount FROM treatments WHERE id = '{ident}'") == [
        (pytest.approx(6.8),)
    ]

    # Deleting something absent must still succeed, or Juggluco retries it forever.
    assert request(api, "DELETE", "/api/v1/treatments/does-not-exist") == (200, {"deleted": 0})
    assert request(api, "DELETE", f"/api/v1/treatments/{ident}", secret="wrong")[0] == 401


# --- database -------------------------------------------------------------------------------


def test_migrate_is_idempotent(pool):
    db.migrate(pool)
    db.migrate(pool)
    assert rows(pool, "SELECT count(*) FROM schema_migrations") == [(len(db.MIGRATIONS),)]


@pytest.fixture
def reader_role(pool):
    with pool.connection() as conn:
        conn.execute("DROP ROLE IF EXISTS cgm_test_reader")
    yield "cgm_test_reader"
    with pool.connection() as conn:
        conn.execute("DROP OWNED BY cgm_test_reader")
        conn.execute("DROP ROLE cgm_test_reader")


def test_readonly_grants_follow_the_role(pool, reader_role):
    # Role missing (CNPG hasn't created it yet): nothing happens, no error.
    assert db.ensure_readonly(pool, reader_role) is False
    with pool.connection() as conn:
        conn.execute("CREATE ROLE cgm_test_reader NOLOGIN")
    # Created later: the periodic re-grant picks it up without a restart.
    assert db.ensure_readonly(pool, reader_role) is True
    assert db.ensure_readonly(pool, reader_role) is True  # idempotent
    with pool.connection() as conn:
        conn.execute("CREATE TABLE later_table (x int)")  # a future migration's table
    try:
        assert rows(
            pool,
            "SELECT has_table_privilege('cgm_test_reader', 'glucose', 'SELECT'),"
            " has_table_privilege('cgm_test_reader', 'glucose', 'INSERT'),"
            " has_table_privilege('cgm_test_reader', 'later_table', 'SELECT'),"
            " has_database_privilege('cgm_test_reader', current_database(), 'CONNECT'),"
            " has_database_privilege('public', current_database(), 'CONNECT')",
        ) == [(True, False, True, True, False)]
    finally:
        with pool.connection() as conn:
            conn.execute("DROP TABLE later_table")
            conn.execute("GRANT CONNECT ON DATABASE " + conn.info.dbname + " TO PUBLIC")


def test_database_down_is_not_a_success(pool):
    # Juggluco must see a non-200 so it keeps the data and retries.
    broken = ConnectionPool("dbname=does_not_exist", min_size=0, max_size=1, timeout=1, open=True)
    config = Config.from_secret(SECRET, listen_host="127.0.0.1", listen_port=0)
    server = make_server(App(config, broken, None))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert request(base, "POST", "/api/v1/entries", fixtures.ENTRIES)[0] == 500
        assert request(base, "GET", "/readyz", secret=None)[0] == 503
    finally:
        server.shutdown()
        server.server_close()
        broken.close()


def test_unstorable_treatment_values_do_not_block_uploads(api, pool):
    body = (
        b'{"_id":"odd","date":1790000000000,"insulin":NaN,"notes":"Blood 7\\u00002",'
        b'"extra":"\\u0000"}'
    )
    assert request(api, "PUT", "/api/v1/treatments", body)[0] == 200
    assert rows(pool, "SELECT insulin, notes FROM treatments") == [(None, "Blood 72")]


# --- HTTP plumbing --------------------------------------------------------------------------


def test_unread_body_closes_connection(api):
    # A rejected upload's body is never read; the connection must not be reused, or the body
    # would be parsed as the next request.
    parts = urlsplit(api)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("POST", "/api/v1/entries", body=fixtures.ENTRIES, headers={"api-secret": "x"})
    response = conn.getresponse()
    response.read()
    assert response.status == 401
    assert response.getheader("Connection") == "close"
    conn.close()


def test_keep_alive_after_successful_upload(api, pool):
    parts = urlsplit(api)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    for body in (fixtures.ENTRIES, fixtures.ENTRIES_UNDETERMINED):
        conn.request("POST", "/api/v1/entries", body=body, headers={"api-secret": HASHED})
        response = conn.getresponse()
        response.read()
        assert response.status == 200
        assert response.getheader("Connection") is None
    conn.close()
    assert rows(pool, "SELECT count(*) FROM glucose") == [(4,)]


def test_missing_or_short_body(api):
    parts = urlsplit(api)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.putrequest("POST", "/api/v1/entries")
    conn.putheader("api-secret", HASHED)
    conn.endheaders()  # no Content-Length, no body
    response = conn.getresponse()
    assert response.status == 411
    conn.close()

    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.putrequest("POST", "/api/v1/entries")
    conn.putheader("api-secret", HASHED)
    conn.putheader("Content-Length", "100")
    conn.endheaders()
    conn.send(b"[]")
    conn.sock.shutdown(1)  # client gives up after 2 of 100 bytes
    response = conn.getresponse()
    assert response.status == 400
    conn.close()


def test_malformed_chunk_size(api):
    parts = urlsplit(api)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.putrequest("POST", "/api/v1/entries")
    conn.putheader("api-secret", HASHED)
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"0x2\r\n[]\r\n0\r\n\r\n")
    assert conn.getresponse().status == 400
    conn.close()


def test_chunked_upload(api, pool):
    parts = urlsplit(api)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    chunks = [fixtures.ENTRIES[:100], fixtures.ENTRIES[100:]]
    conn.request(
        "POST",
        "/api/v1/entries",
        body=iter(chunks),
        headers={"api-secret": HASHED},
        encode_chunked=True,
    )
    response = conn.getresponse()
    assert (response.status, json.loads(response.read())["inserted"]) == (200, 2)
    conn.close()
