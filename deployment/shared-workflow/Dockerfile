FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY pta_finance ./pta_finance
RUN uv sync --locked --no-dev --extra web --no-editable --no-cache

FROM python:3.12-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /build/.venv /app/.venv
COPY source-content-manifest.json inspect_image.py ./
RUN groupadd --gid 10001 proof && useradd --uid 10001 --gid 10001 --no-create-home proof
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/python", "-m", "pta_finance.shared_workflow"]
