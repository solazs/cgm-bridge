# cgm-bridge

A small receiver for [Nightscout](https://nightscout.github.io/) uploads. Point a CGM app's
Nightscout uploader at it (written and tested against [Juggluco](https://www.juggluco.nl/)) and it:

- stores glucose readings and treatments in **PostgreSQL**, the source of truth, and
- mirrors glucose readings into **VictoriaMetrics** (or anything that speaks its
  `/api/v1/import` API) for Grafana graphs and alerting.

It implements only the part of the Nightscout v1 API that uploaders write to. There is no web
UI, no read API, and no MongoDB.

## API

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/v1/entries` | Store glucose readings (`type: "sgv"`). Duplicates are ignored. |
| `POST`, `PUT` | `/api/v1/treatments` | Insert or update treatments by `_id` |
| `DELETE` | `/api/v1/treatments/<id>` | Delete one treatment. Succeeds even if it is already gone. |
| `GET` | `/healthz` | Liveness |
| `GET` | `/readyz` | Readiness (database reachable) |

Writes need the `api-secret` header: the SHA-1 hex of the secret, as Nightscout uploaders send
it, or the secret itself. Bodies may be a single JSON document or an array.

Every successful write answers **HTTP 200**. That matters for Juggluco, which treats any other
code as a failure. It keeps a per-sensor "sent up to here" cursor, retries every 15 minutes, and
then uploads everything it missed (up to 30 days back), so outages backfill on their own.

Prometheus metrics for the service itself (`cgm_bridge_*`) are on a separate port.

## Data

| Table | Key | Notes |
|---|---|---|
| `glucose` | `(device, time)` | mg/dL, delta, trend direction; `exported` marks rows already in VictoriaMetrics |
| `treatments` | `id` | insulin, carbs, notes, plus `label`/`amount` parsed from Juggluco's `"<label> <value>"` notes (e.g. `Blood 7.2`), and the raw document as `jsonb` |

Migrations run at startup. The VictoriaMetrics series is `cgm_glucose_mg_dl{device="<sensor>"}`
with the readings' own timestamps. Export is asynchronous: a VictoriaMetrics outage never fails
an upload, and the export catches up afterwards. Postgres keeps everything; VictoriaMetrics only
keeps its retention window and can be rebuilt from Postgres at any time
(`UPDATE glucose SET exported = false`).

## Configuration

| Variable | Default | |
|---|---|---|
| `CGM_BRIDGE_API_SECRET` | — | **Required**, at least 12 characters. Use a long random one. |
| `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`, … | | Standard libpq variables |
| `CGM_BRIDGE_VM_URL` | empty | VictoriaMetrics base URL, e.g. `http://victoria-metrics:8428`. Empty disables export. |
| `CGM_BRIDGE_VM_METRIC` | `cgm_glucose_mg_dl` | Metric name |
| `CGM_BRIDGE_READONLY_ROLE` | empty | Existing role to grant `SELECT` on all tables (e.g. for a Grafana datasource) |
| `CGM_BRIDGE_LISTEN_PORT` | `8080` | API |
| `CGM_BRIDGE_METRICS_PORT` | `9090` | Prometheus metrics |
| `CGM_BRIDGE_LOG_LEVEL` | `INFO` | |

The image runs as UID 10001 and works with a read-only root filesystem.

## Juggluco setup

Settings → Nightscout upload (the uploader, not the built-in web server):

- **URL:** `https://<your host>` — Juggluco appends `/api/v1/entries` itself
- **Secret:** the same value as `CGM_BRIDGE_API_SECRET`
- **Active:** on; leave "test V3" **off** (this implements v1)
- **Send amounts** (optional): uploads treatments. Each amount label also needs a Nightscout
  kind assigned in Juggluco, or it is not sent.

Juggluco cannot present TLS client certificates, so the secret is the only credential. Put the
service behind a reverse proxy that only forwards the routes above.

## Development

Only Docker is needed:

```bash
scripts/test.sh              # ruff + pytest against a throwaway PostgreSQL
scripts/test.sh -k export    # extra args go to pytest
```

With [uv](https://docs.astral.sh/uv/) and a PostgreSQL reachable through `PG*` variables,
`uv run pytest` works as well. Tests that need a database are skipped when `PGHOST` is unset.

## Releasing

1. Bump `version` in `pyproject.toml` and merge to `main`.
2. Tag it: `git tag v0.2.0 && git push origin v0.2.0`.
3. The workflow lints, tests, scans the image with Trivy, and publishes
   `ghcr.io/solazs/cgm-bridge:0.2.0`. It refuses a tag that doesn't match `pyproject.toml`.

## License

MIT
