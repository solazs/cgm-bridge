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


def migrate(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        # Serialise concurrent starts before touching anything, including the bookkeeping table.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('cgm-bridge-migrate'))")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version integer PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, statement in MIGRATIONS:
            if version in applied:
                continue
            log.info("applying migration %d", version)
            conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))


def ensure_readonly(pool: ConnectionPool, role: str) -> bool:
    """Grants `role` read access to this database. Idempotent; returns False if the role is
    missing. Called at startup and periodically, so a role created later (or recreated) gets
    its grants without restarting the service."""
    with pool.connection() as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if not exists:
            return False
        ident = sql.Identifier(role)
        database = sql.Identifier(conn.info.dbname)
        # Only the owner and the read-only role may connect, not every role in the cluster.
        conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(database))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, ident))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident))
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(ident)
        )
    return True


def upsert_glucose(conn: Connection, readings: Sequence[GlucoseReading]) -> tuple[int, int]:
    """Stores readings. A resend with different values (Juggluco applies calibration at upload
    time, so a recalibrated "Resend data" changes old readings) updates the row and queues it
    for export again; an identical resend changes nothing. Returns (inserted, updated)."""
    if not readings:
        return 0, 0
    rows = conn.execute(
        """
        INSERT INTO glucose AS g (device, time, mg_dl, delta, direction)
        SELECT * FROM unnest(%s::text[], %s::timestamptz[], %s::integer[], %s::real[], %s::text[])
        ON CONFLICT (device, time) DO UPDATE SET
            mg_dl = excluded.mg_dl,
            delta = excluded.delta,
            direction = excluded.direction,
            received_at = now(),
            exported = false
        WHERE (g.mg_dl, g.delta, g.direction)
            IS DISTINCT FROM (excluded.mg_dl, excluded.delta, excluded.direction)
        RETURNING (xmax = 0) AS inserted
        """,
        (
            [r.device for r in readings],
            [r.time for r in readings],
            [r.mg_dl for r in readings],
            [r.delta for r in readings],
            [r.direction for r in readings],
        ),
    ).fetchall()
    inserted = sum(1 for (was_insert,) in rows if was_insert)
    return inserted, len(rows) - inserted


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
