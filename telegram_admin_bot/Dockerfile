# The Python the test suite runs on. Every compiled dependency (asyncpg,
# cryptography, pydantic-core, uvloop, httptools, watchfiles, websockets)
# ships ready-made wheels for it on both amd64 and arm64 (e.g. AWS Graviton,
# Hetzner CAX), so the build needs no compiler on either kind of server.
FROM python:3.13-slim

# tzdata so quiet hours and bookings honour each client's timezone
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# The panel has a login (ADMIN_PASSWORD), but no TLS of its own. Binding
# 0.0.0.0 here is safe ONLY because docker-compose.yml publishes the panel's
# port to the host's loopback, never to a public interface. Public HTTPS,
# when wanted, goes through the optional `caddy` service instead, which
# reaches the panel over the compose network.
ENV ADMIN_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# Session files and other per-worker scratch state written under DATA_DIR
# live here, mounted as a volume so they survive a rebuild. The database of
# record is now Postgres (see docker-compose.yml's `postgres` service); this
# volume is not that.
#
# The processes run as root inside the container. docker-compose.yml
# bind-mounts ./data here, which Docker creates root-owned on first start
# (and existing servers already have root-owned files in it), so a non-root
# user would first need that folder chowned on the host. Nothing is
# published but 127.0.0.1:8787 and Caddy's 80/443.
VOLUME ["/app/data"]

# One image serves every app role: the admin panel (panel.py), the fleet
# manager that leases sessions and runs workers (manager.py), the scheduler
# (scheduler.py), the public booking pages (public_app.py) and a one-shot
# schema migrator (migrate_entrypoint.py) that runs once before the others
# start. docker-compose.yml picks the entrypoint per service via `command:`.
# This CMD is just the fallback for a bare `docker run` with no command.
CMD ["python", "panel.py"]
