"""Mirrors stored glucose readings into VictoriaMetrics.

Postgres is the outbox: readings are inserted with `exported = false`, and this thread pushes
them through VictoriaMetrics' JSON-lines import API (which keeps the original timestamps, so
backfilled readings land where they belong) and flips the flag once VictoriaMetrics accepted
them. A VictoriaMetrics outage never fails an upload; the export just catches up later.

A crash between the push and the flag update re-sends that batch once. VictoriaMetrics then
holds a duplicate sample with an identical timestamp and value, which is harmless for graphs.
A reading corrected by a later resend is exported again under the same timestamp; without
deduplication VictoriaMetrics keeps both values. Postgres holds the corrected one.

No database connection is held during the HTTP push: select and commit, push, then flag the
rows in a new transaction (idempotent, so a lost race only means a harmless re-send).
"""

import json
import logging
import threading
import urllib.request
from collections import defaultdict

from psycopg_pool import ConnectionPool

from . import metrics

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 300.0


class ExportError(RuntimeError):
    pass


def render_import(rows: list[tuple[str, object, int]], metric: str) -> bytes:
    """One JSON line per device, in the format of VictoriaMetrics' /api/v1/import."""
    series: dict[str, tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
    for device, time, mg_dl in rows:
        values, timestamps = series[device]
        values.append(mg_dl)
        timestamps.append(int(time.timestamp() * 1000))
    lines = [
        json.dumps(
            {
                "metric": {"__name__": metric, "device": device},
                "values": values,
                "timestamps": timestamps,
            },
            separators=(",", ":"),
        )
        for device, (values, timestamps) in series.items()
    ]
    return ("\n".join(lines) + "\n").encode()


class Exporter(threading.Thread):
    def __init__(
        self,
        pool: ConnectionPool,
        vm_url: str,
        metric: str,
        batch_size: int = 5000,
        interval: float = 60.0,
        timeout: float = 30.0,
    ):
        super().__init__(name="vm-exporter", daemon=True)
        self._pool = pool
        self._import_url = f"{vm_url}/api/v1/import"
        self._metric = metric
        self._batch_size = batch_size
        self._interval = interval
        self._timeout = timeout
        self._wake = threading.Event()
        self._stopping = threading.Event()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def run(self) -> None:
        backoff = 0.0
        while not self._stopping.is_set():
            try:
                while self.export_once() == self._batch_size and not self._stopping.is_set():
                    pass
                backoff = 0.0
            except Exception as exc:  # keep the thread alive whatever happens
                metrics.EXPORT_FAILURES.inc()
                backoff = min(max(backoff * 2, 5.0), MAX_BACKOFF_SECONDS)
                log.warning("export to VictoriaMetrics failed, retrying in %.0fs: %s", backoff, exc)
            self._wake.wait(backoff or self._interval)
            self._wake.clear()

    def export_once(self) -> int:
        """Exports one batch of pending readings. Returns how many were exported."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT device, time, mg_dl FROM glucose WHERE NOT exported ORDER BY time LIMIT %s",
                (self._batch_size,),
            ).fetchall()
        try:
            if rows:
                self._push(render_import(rows, self._metric))
                with self._pool.connection() as conn:
                    conn.execute(
                        "UPDATE glucose SET exported = true"
                        " WHERE (device, time) IN"
                        " (SELECT * FROM unnest(%s::text[], %s::timestamptz[]))",
                        ([r[0] for r in rows], [r[1] for r in rows]),
                    )
                metrics.EXPORT_SAMPLES.inc(len(rows))
        finally:
            # Also on failure: the "export stuck" alert watches this gauge.
            self.update_pending()
        return len(rows)

    def update_pending(self) -> None:
        with self._pool.connection() as conn:
            pending = conn.execute("SELECT count(*) FROM glucose WHERE NOT exported").fetchone()
        metrics.EXPORT_PENDING.set(pending[0] if pending else 0)

    def _push(self, body: bytes) -> None:
        # The URL is config-validated to be http(s) (config._url), never user input.
        request = urllib.request.Request(  # noqa: S310
            self._import_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-ndjson"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                status = response.status
        except OSError as exc:  # URLError and HTTPError are OSErrors
            raise ExportError(str(exc)) from exc
        if not 200 <= status < 300:
            raise ExportError(f"VictoriaMetrics answered HTTP {status}")
