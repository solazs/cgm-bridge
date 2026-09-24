"""PostgreSQL storage. Postgres is the source of truth; VictoriaMetrics is a derived copy."""

import logging
from collections.abc import Sequence
from datetime import datetime

from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .nightscout import GlucoseReading, Treatment

log = logging.getLogger(__name__)

# Append-only: never edit a migration that has shipped, add a new one.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE glucose (
            device       text        NOT NULL,
            time         timestamptz NOT NULL,
            mg_dl        integer     NOT NULL,
            delta        real,
            direction    text,
            received_at  timestamptz NOT NULL DEFAULT now(),
            -- Outbox flag for the VictoriaMetrics export (see exporter.py).
            exported     boolean     NOT NULL DEFAULT false,
            PRIMARY KEY (device, time)
        );
        CREATE INDEX glucose_time_idx ON glucose (time);
        CREATE INDEX glucose_unexported_idx ON glucose (time) WHERE NOT exported;

        CREATE TABLE treatments (
            id            text        PRIMARY KEY,
            time          timestamptz NOT NULL,
            event_type    text,
            insulin       real,
            insulin_type  text,
            carbs         real,
            notes         text,
            -- "Blood 7.2" -> label 'Blood', amount 7.2; insulin/carbs fill these too.
            label         text,
            amount        real,
            entered_by    text,
            raw           jsonb       NOT NULL,
            received_at   timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX treatments_time_idx ON treatments (time);
        """,
    ),
]


def migrate(pool: ConnectionPool, readonly_role: str = "") -> None:
    with pool.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version integer PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # Serialise concurrent starts (e.g. during a rolling update).
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('cgm-bridge-migrate'))")
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, statement in MIGRATIONS:
            if version in applied:
                continue
            log.info("applying migration %d", version)
            conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        if readonly_role:
            _grant_readonly(conn, readonly_role)


def _grant_readonly(conn: Connection, role: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if not exists:
        log.warning("read-only role %r does not exist yet; skipping grants", role)
        return
    ident = sql.Identifier(role)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
    conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident))
    conn.execute(
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(
            ident
        )
    )


def insert_glucose(conn: Connection, readings: Sequence[GlucoseReading]) -> int:
    """Inserts readings, ignoring ones already stored. Returns how many were new."""
    if not readings:
        return 0
    cur = conn.execute(
        """
        INSERT INTO glucose (device, time, mg_dl, delta, direction)
        SELECT * FROM unnest(%s::text[], %s::timestamptz[], %s::integer[], %s::real[], %s::text[])
        ON CONFLICT (device, time) DO NOTHING
        """,
        (
            [r.device for r in readings],
            [r.time for r in readings],
            [r.mg_dl for r in readings],
            [r.delta for r in readings],
            [r.direction for r in readings],
        ),
    )
    return cur.rowcount


def upsert_treatments(conn: Connection, treatments: Sequence[Treatment]) -> int:
    if not treatments:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO treatments (id, time, event_type, insulin, insulin_type, carbs, notes,
                                    label, amount, entered_by, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                time = excluded.time,
                event_type = excluded.event_type,
                insulin = excluded.insulin,
                insulin_type = excluded.insulin_type,
                carbs = excluded.carbs,
                notes = excluded.notes,
                label = excluded.label,
                amount = excluded.amount,
                entered_by = excluded.entered_by,
                raw = excluded.raw,
                updated_at = now()
            """,
            [
                (
                    t.id,
                    t.time,
                    t.event_type,
                    t.insulin,
                    t.insulin_type,
                    t.carbs,
                    t.notes,
                    t.label,
                    t.amount,
                    t.entered_by,
                    Jsonb(t.raw),
                )
                for t in treatments
            ],
        )
    return len(treatments)


def delete_treatment(conn: Connection, ident: str) -> bool:
    return conn.execute("DELETE FROM treatments WHERE id = %s", (ident,)).rowcount > 0


def latest_reading_time(conn: Connection) -> datetime | None:
    row = conn.execute("SELECT max(time) FROM glucose").fetchone()
    return row[0] if row else None
