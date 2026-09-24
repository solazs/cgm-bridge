# Base images are pinned by tag and digest; Renovate bumps both together.
FROM ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc AS uv

# --- build: resolve the locked dependencies and install the app into /app/.venv -------------
FROM python:3.14.7-slim-trixie@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# --- test: the same environment plus the dev group; used by CI, never shipped ---------------
FROM build AS test
RUN uv sync --locked --no-editable
COPY tests ./tests
ENTRYPOINT ["/app/.venv/bin/pytest"]
CMD ["-v"]

# --- runtime ---------------------------------------------------------------------------------
FROM python:3.14.7-slim-trixie@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS runtime
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_DISABLE_CREATED_SERIES=True
# Numeric, so Kubernetes can verify runAsNonRoot without a passwd lookup.
USER 10001:10001
EXPOSE 8080 9090
ENTRYPOINT ["cgm-bridge"]
