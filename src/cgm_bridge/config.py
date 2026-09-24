"""Configuration from environment variables.

The database connection is not configured here: psycopg/libpq read the standard PG* variables
(PGHOST, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE, ...) on their own.
"""

import hashlib
import os
from dataclasses import dataclass

# The secret (or its SHA-1) is the only credential of a public endpoint. Nightscout's floor is
# 12; this is stricter because nobody types it by hand more than once.
MIN_SECRET_LENGTH = 32


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    # Nightscout uploaders send SHA-1(secret) as a hex string in the `api-secret` header.
    api_secret_sha1: str
    # The plain secret is also accepted, as Nightscout does. Kept only as its SHA-256 so the
    # raw value is not held in memory longer than needed.
    api_secret_sha256: str
    # All interfaces: it runs in a container, and Kubernetes decides what can reach it.
    listen_host: str = "0.0.0.0"  # noqa: S104
    listen_port: int = 8080
    metrics_port: int = 9090
    # Base URL of VictoriaMetrics (e.g. http://victoria-metrics:8428). Empty disables export.
    vm_url: str = ""
    vm_metric: str = "cgm_glucose_mg_dl"
    export_batch_size: int = 5000
    # Idle re-check interval of the exporter; new uploads wake it immediately.
    export_interval_seconds: float = 60.0
    # Juggluco sends at most ~10k readings (~3.6 MB) in one request.
    max_body_bytes: int = 8 * 1024 * 1024
    # Optional existing role that is granted SELECT on all tables (e.g. for Grafana).
    readonly_role: str = ""

    @classmethod
    def from_secret(cls, secret: str, **kwargs) -> Config:
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(f"API secret must be at least {MIN_SECRET_LENGTH} characters")
        return cls(
            # SHA-1 is what the Nightscout protocol puts on the wire, not a choice made here.
            api_secret_sha1=hashlib.sha1(secret.encode()).hexdigest(),  # noqa: S324
            api_secret_sha256=hashlib.sha256(secret.encode()).hexdigest(),
            **kwargs,
        )

    @classmethod
    def from_env(cls, env: os._Environ | dict[str, str] = os.environ) -> Config:
        secret = env.get("CGM_BRIDGE_API_SECRET", "")
        if not secret:
            raise ConfigError("CGM_BRIDGE_API_SECRET is not set")
        return cls.from_secret(
            secret,
            listen_host=env.get("CGM_BRIDGE_LISTEN_HOST", "0.0.0.0"),  # noqa: S104
            listen_port=_int(env, "CGM_BRIDGE_LISTEN_PORT", 8080),
            metrics_port=_int(env, "CGM_BRIDGE_METRICS_PORT", 9090),
            vm_url=_url(env, "CGM_BRIDGE_VM_URL"),
            vm_metric=env.get("CGM_BRIDGE_VM_METRIC", "cgm_glucose_mg_dl"),
            export_batch_size=_int(env, "CGM_BRIDGE_EXPORT_BATCH_SIZE", 5000),
            readonly_role=env.get("CGM_BRIDGE_READONLY_ROLE", ""),
        )


def _int(env, name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _url(env, name: str) -> str:
    raw = env.get(name, "").strip().rstrip("/")
    if raw and not raw.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must be an http:// or https:// URL")
    return raw
