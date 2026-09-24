"""Entry point: `cgm-bridge` or `python -m cgm_bridge`."""

import logging
import os
import signal
import sys
import threading

from prometheus_client import start_http_server
from psycopg_pool import ConnectionPool

from . import __version__, db
from .config import Config, ConfigError
from .exporter import Exporter
from .server import App, make_server

log = logging.getLogger("cgm_bridge")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CGM_BRIDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    # Connection settings come from the PG* environment variables.
    pool = ConnectionPool(
        conninfo="",
        min_size=1,
        max_size=4,
        check=ConnectionPool.check_connection,
        name="cgm-bridge",
        open=True,
    )
    pool.wait(timeout=60)
    db.migrate(pool, config.readonly_role)

    exporter = None
    if config.vm_url:
        exporter = Exporter(
            pool,
            config.vm_url,
            config.vm_metric,
            batch_size=config.export_batch_size,
            interval=config.export_interval_seconds,
        )
        exporter.start()
    else:
        log.info("CGM_BRIDGE_VM_URL is empty; VictoriaMetrics export disabled")

    start_http_server(config.metrics_port, addr=config.listen_host)
    app = App(config, pool, exporter)
    with pool.connection() as conn:
        if latest := db.latest_reading_time(conn):
            app.note_latest(latest.timestamp())
    server = make_server(app)

    def shutdown(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        # shutdown() blocks until serve_forever() returns, so call it off the main thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        "cgm-bridge %s listening on %s:%d (metrics on :%d)",
        __version__,
        config.listen_host,
        config.listen_port,
        config.metrics_port,
    )
    server.serve_forever()
    server.server_close()
    if exporter:
        exporter.stop()
        exporter.join(timeout=10)
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
