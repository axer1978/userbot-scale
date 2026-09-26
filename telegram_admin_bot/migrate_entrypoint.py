"""One-shot entrypoint for the `migrate` compose service.

This is intentionally NOT part of manager.py or panel.py: those two are long
running processes that only ever call `pg.assert_version` (per pg.py's own
docstring, "worker/panel processes ... refuse to boot on a mismatch"). Only
`manager.py` and the test harness are supposed to call `apply_migrations`,
but manager.py doesn't exist as a fleet-wide singleton lock step yet (and
even once it does, tying "run migrations" to "also start leasing sessions"
would race in a multi-manager-replica future). So this script is the thing
that actually calls `apply_migrations`: a short-lived container that runs
once, applies whatever migrations haven't landed yet, prints the result, and
exits 0 (or non-zero on failure). docker-compose's
`condition: service_completed_successfully` makes panel/manager wait on this
exit code before they ever open a connection.

Requires DATABASE_URL, and USERBOT_MASTER_KEY for the customer_ref backfill.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pg
import tenants


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("migrate_entrypoint: DATABASE_URL is not set", file=sys.stderr)
        return 1

    pool = await pg.create_pool(dsn, min_size=1, max_size=2)
    try:
        applied = await pg.apply_migrations(pool)
        # Data steps that need Python (the master key, schema validation), so
        # they can't live in a .sql migration. Idempotent: a no-op once done.
        backfilled = await tenants.backfill(pool)
    finally:
        await pool.close()

    if applied:
        print(f"migrate_entrypoint: applied migrations {applied}")
    else:
        print("migrate_entrypoint: database already at latest schema version")
    for item in backfilled["imported"]:
        print(
            f"migrate_entrypoint: tenant {item['tenant_id']}: imported settings "
            f"{sorted(item['config'])}, persona sections {item['prompt_sections']}"
            + (f", dropped {item['dropped']}" if item["dropped"] else "")
        )
    if backfilled["customer_refs"]:
        print(f"migrate_entrypoint: set customer_ref on {backfilled['customer_refs']} conversation(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
