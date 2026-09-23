FROM python:3.12-slim

# tzdata so active hours honour the timezone in config.json
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The panel has a login (ADMIN_PASSWORD), but no TLS of its own. Binding
# 0.0.0.0 here is safe ONLY because docker-compose.yml publishes the panel's
# port to the host's loopback, never to a public interface.
ENV ADMIN_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# Session files and other per-worker scratch state written under DATA_DIR
# live here, mounted as a volume so they survive a rebuild. The database of
# record is now Postgres (see docker-compose.yml's `postgres` service); this
# volume is not that.
VOLUME ["/app/data"]

# One image now serves three roles: the admin panel (panel.py), the fleet
# manager that leases sessions and runs workers (manager.py), and a one-shot
# schema migrator (migrate_entrypoint.py) that runs once before the other
# two start. They share all the same code and dependencies, so rather than
# maintain three Dockerfiles, docker-compose.yml picks the entrypoint per
# service via `command:`. This CMD is just the fallback for a bare
# `docker run` with no explicit command.
CMD ["python", "panel.py"]
