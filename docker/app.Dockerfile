# One image for the web server and the worker; the DemoProject picks the command.

# The console UI.
FROM node:22-slim AS ui
WORKDIR /ui
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# The Python app. git and Node are for the worker: agents work in git checkouts and run the
# storefront's node:test suite.
FROM python:3.13-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends git nodejs ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH WORKDIR_ROOT=/tmp/workdirs PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY agentic_sdlc/ agentic_sdlc/
RUN uv sync --frozen --no-dev
COPY --from=ui /ui/dist web/dist
RUN useradd --create-home --uid 10001 app && mkdir -p /tmp/workdirs && chown app /tmp/workdirs
USER app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "agentic_sdlc.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
