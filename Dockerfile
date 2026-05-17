FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=hardlink

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

RUN groupadd -r spendly && useradd -r -g spendly -m -s /sbin/nologin spendly

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

RUN mkdir -p /app/data && chown -R spendly:spendly /app/data

USER spendly

CMD ["/app/.venv/bin/python", "-m", "spendly"]