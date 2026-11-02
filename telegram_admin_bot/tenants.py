"""Tenants, industries and the three prompt layers, in Postgres.

Everything here that changes state writes an audit row in the same
transaction (audit.py). Validation is tenant_config.py (config) and
prompt_layers.py (prompts); this module only stores what those accept.

Versioning: prompt_versions rows are immutable. Which version each layer
uses is a pointer: platform_settings 'base_prompt_version' for the base,
industries.template_version for an industry, tenants.prompt_version for a
client. Saving creates version max+1 and points at it; a rollback moves the
pointer back and never rewrites history. tenants.prompt_pin_version, when
set, makes one tenant render with a fixed industry version instead of the
industry's live one.

Folders: an industry can sit inside another one (industries.parent_id), any
levels deep. A sub-folder's template holds only its changes to its
parent's, and its settings (own_config) only what it sets itself; its
clients get the parent's with those on top (folder_sections, and
default_config, kept up to date by save_industry_config). A client's own
folder is still "its industry" everywhere: tenants.industry_id, runs,
rules, examples.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import asyncpg

import audit
import config_store
import crypto
import prompt_layers
import tenant_config
from tenant_config import ConfigError

log = logging.getLogger("tenants")

BASE = "base"
INDUSTRY = "industry"
CLIENT = "client"


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    """Optimistic-concurrency failure: someone else saved in between."""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _tenant(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "industry_id": row["industry_id"],
        "status": row["status"],
        "channel": row["channel"],
        "session_id": row["session_id"],
        "config_json": _json(row["config_json"]),
        "config_revision": row["config_revision"],
        "prompt_version": row["prompt_version"],
        "prompt_pin_version": row["prompt_pin_version"],
        "billing_next_due": row["billing_next_due"].isoformat() if row["billing_next_due"] else None,
        "created_at": row["created_at"].isoformat(timespec="seconds"),
    }


def _industry(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        # None for a top-level industry; else the folder this one sits in.
        "parent_id": row["parent_id"],
        "template_version": row["template_version"],
        # What its clients inherit: the parent's with own_config on top.
        "default_config": _json(row["default_config"]),
        # What this folder sets itself (the same as default_config at the top).
        "own_config": _json(row["own_config"]),
        "config_revision": row["config_revision"],
    }


PATH_SEPARATOR = " › "

# The folder and the folders above it (depth 0 = the folder itself). Folders
# get their parent when they are made and never move, so there is no cycle;
# the depth limit is there anyway.
_CHAIN_SQL = """
WITH RECURSIVE up AS (
  SELECT i.*, 0 AS depth FROM industries i WHERE i.id = $1
  UNION ALL
  SELECT p.*, up.depth + 1 FROM industries p JOIN up ON p.id = up.parent_id WHERE up.depth < 50
)
SELECT * FROM up ORDER BY depth DESC
"""

# The folder and every folder under it, each after its parent.
_SUBTREE_SQL = """
WITH RECURSIVE down AS (
  SELECT i.*, 0 AS depth FROM industries i WHERE i.id = $1
  UNION ALL
  SELECT c.*, down.depth + 1 FROM industries c JOIN down ON c.parent_id = down.id WHERE down.depth < 50
)
SELECT * FROM down ORDER BY depth, id
"""


def _version(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "layer": row["layer"],
        "ref_id": row["ref_id"],
        "version": row["version"],
        "content": _json(row["content"]),
        "note": row["note"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(timespec="seconds"),
    }


@dataclass
class Bundle:
    """What a tenant's runtime needs: its row, its effective config, and its
    rendered system prompt."""
    tenant: dict[str, Any]
    industry: dict[str, Any]
    resolved: tenant_config.Resolved
    inherited: dict[str, Any]          # config without the client layer
    prompt: prompt_layers.Rendered
    # The raw base/industry/client content used, and the operator rules
    # ("rules": their texts, as rendered).
    layers: dict[str, Any]

    @property
    def config(self) -> dict[str, Any]:
        return self.resolved.as_dict()


class TenantStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------ reads

    async def get(self, tenant_id: int) -> dict[str, Any]:
        row = await self._pool.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
        if row is None:
            raise NotFound(f"no tenant {tenant_id}")
        return _tenant(row)

    async def by_session(self, session_id: str) -> Optional[dict[str, Any]]:
        row = await self._pool.fetchrow("SELECT * FROM tenants WHERE session_id = $1", session_id)
        return _tenant(row) if row else None

    async def list(self) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT t.*, s.label AS session_label, s.state AS session_state, s.is_active AS session_active
              FROM tenants t LEFT JOIN telegram_sessions s ON s.session_id = t.session_id
             ORDER BY t.industry_id, lower(t.name), t.id
            """
        )
        out = []
        for row in rows:
            item = _tenant(row)
            item["session_state"] = row["session_state"]
            item["session_active"] = row["session_active"]
            out.append(item)
        return out

    async def get_industry(self, industry_id: int) -> dict[str, Any]:
        row = await self._pool.fetchrow("SELECT * FROM industries WHERE id = $1", industry_id)
        if row is None:
            raise NotFound(f"no industry {industry_id}")
        return _industry(row)

    async def list_industries(self) -> list[dict[str, Any]]:
        """Every folder, with its "path" ("escort › Tallinn"), sorted by it."""
        rows = await self._pool.fetch("SELECT * FROM industries ORDER BY lower(name), id")
        folders = {r["id"]: _industry(r) for r in rows}

        def path(folder: dict[str, Any], seen: frozenset = frozenset()) -> list[str]:
            parent = folders.get(folder["parent_id"]) if folder["parent_id"] not in seen else None
            return (path(parent, seen | {folder["id"]}) if parent else []) + [folder["name"]]

        out = [{**f, "path": PATH_SEPARATOR.join(path(f))} for f in folders.values()]
        return sorted(out, key=lambda f: ([part.lower() for part in f["path"].split(PATH_SEPARATOR)], f["id"]))

    async def chain(self, industry_id: int) -> list[dict[str, Any]]:
        """The folder and the folders above it, the top-level industry first."""
        rows = await self._pool.fetch(_CHAIN_SQL, industry_id)
        if not rows:
            raise NotFound(f"no industry {industry_id}")
        return [_industry(r) for r in rows]

    async def subtree(self, industry_id: int) -> list[dict[str, Any]]:
        """The folder and every folder under it, each after its parent."""
        rows = await self._pool.fetch(_SUBTREE_SQL, industry_id)
        if not rows:
            raise NotFound(f"no industry {industry_id}")
        return [_industry(r) for r in rows]

    async def folder_sections(
        self, industry_id: int, version: Optional[int] = None, *, chain: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, str]:
        """The template this folder hands its clients: the top-level
        industry's sections with each sub-folder's changes on top, down to
        this one. `version` replaces this folder's live version (a pin)."""
        chain = chain or await self.chain(industry_id)
        sections: dict[str, str] = {}
        for n, folder in enumerate(chain):
            wanted = version if version and n == len(chain) - 1 else folder["template_version"]
            content = (await self.version(INDUSTRY, folder["id"], wanted))["content"]
            if folder["parent_id"] is None:
                sections = prompt_layers.validate_industry(content)["sections"]
            else:
                sections = prompt_layers.apply_folder(sections, content)
        return sections

    async def versions(self, layer: str, ref_id: int) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT * FROM prompt_versions WHERE layer = $1 AND ref_id = $2 ORDER BY version DESC",
            layer, ref_id,
        )
        return [_version(r) for r in rows]

    async def version(self, layer: str, ref_id: int, version: int) -> dict[str, Any]:
        row = await self._pool.fetchrow(
            "SELECT * FROM prompt_versions WHERE layer = $1 AND ref_id = $2 AND version = $3",
            layer, ref_id, version,
        )
        if row is None:
            raise NotFound(f"no {layer} prompt version {version} for {ref_id}")
        return _version(row)

    async def base_version(self) -> int:
        return int(_json(await self._pool.fetchval(
            "SELECT value FROM platform_settings WHERE key = 'base_prompt_version'"
        )))

    async def base(self) -> dict[str, Any]:
        return await self.version(BASE, 0, await self.base_version())

    async def bundle(self, tenant_id: int) -> Bundle:
        """Raises ConfigError / PromptError if what is stored no longer
        validates; saves are validated, so that means someone edited the
        database by hand."""
        tenant = await self.get(tenant_id)
        chain = await self.chain(tenant["industry_id"])
        # The client's own folder, with the names of the folders above it.
        industry = {**chain[-1], "path": PATH_SEPARATOR.join(f["name"] for f in chain),
                    "root_id": chain[0]["id"], "root_name": chain[0]["name"]}
        inherited = tenant_config.resolve(industry["default_config"], None)
        resolved = tenant_config.resolve(industry["default_config"], tenant["config_json"])

        base = await self.base()
        industry_version = tenant["prompt_pin_version"] or industry["template_version"]
        industry_content = {"sections": await self.folder_sections(industry["id"], industry_version, chain=chain)}
        parents = [(folder["id"], folder["template_version"]) for folder in chain[:-1]]
        client_content = {"overrides": {}, "addendum": ""}
        if tenant["prompt_version"]:
            client_content = (await self.version(CLIENT, tenant_id, tenant["prompt_version"]))["content"]
        # The operator's active rules, the industry's and this account's
        # (rules.py). Imported here: rules.py builds on this module.
        import rules

        active_rules = await rules.active_for(self._pool, tenant_id, industry["id"])
        prompt = prompt_layers.render(
            base=base["content"],
            industry=industry_content,
            client=client_content,
            business_name=tenant["name"],
            language_policy=resolved.config.language_policy,
            versions=(base["version"], industry["id"], industry_version, tenant["prompt_version"]),
            rules=active_rules,
            parents=parents,
        )
        return Bundle(
            tenant=tenant,
            industry=industry,
            resolved=resolved,
            inherited=inherited.as_dict(),
            prompt=prompt,
            layers={"base": base["content"], "industry": industry_content, "client": client_content,
                    "industry_version": industry_version, "rules": active_rules},
        )

    async def bundle_for_session(self, session_id: str) -> Bundle:
        tenant = await self.by_session(session_id)
        if tenant is None:
            raise NotFound(f"no tenant owns session {session_id!r}")
        return await self.bundle(tenant["id"])

    # ----------------------------------------------------------- tenants

    async def create(
        self, *, name: str, industry_id: Optional[int] = None, session_id: Optional[str] = None,
        actor: str, reason: str = "",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("A tenant needs a name.")
        async with self._pool.acquire() as con, con.transaction():
            if industry_id is None:
                industry_id = await con.fetchval("SELECT min(id) FROM industries")
            row = await con.fetchrow(
                """
                INSERT INTO tenants (name, industry_id, session_id, legacy_imported_at)
                VALUES ($1, $2, $3, now()) RETURNING *
                """,
                name, industry_id, session_id,
            )
            await audit.record(
                con, tenant_id=row["id"], actor=actor, event=audit.TENANT_CREATED, reason=reason,
                payload={"name": name, "industry_id": industry_id, "session_id": session_id},
            )
        return _tenant(row)

    async def update(
        self, tenant_id: int, *, actor: str, reason: str = "",
        name: Optional[str] = None, industry_id: Optional[int] = None,
    ) -> dict[str, Any]:
        before = await self.get(tenant_id)
        changes: dict[str, Any] = {}
        if name is not None and name.strip() and name.strip() != before["name"]:
            changes["name"] = name.strip()
        if industry_id is not None and industry_id != before["industry_id"]:
            industry = await self.get_industry(industry_id)
            # The client overrides must still make a valid config on top of
            # the new industry's defaults.
            tenant_config.resolve(industry["default_config"], before["config_json"])
            changes["industry_id"] = industry_id
            # A pinned version belongs to the old industry's template.
            changes["prompt_pin_version"] = None
        if not changes:
            return before
        sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changes))
        async with self._pool.acquire() as con, con.transaction():
            await con.execute(
                f"UPDATE tenants SET {sets}, updated_at = now() WHERE id = $1", tenant_id, *changes.values()
            )
            await audit.record(
                con, tenant_id=tenant_id, actor=actor, event=audit.TENANT_UPDATED, reason=reason,
                payload={"from": {k: before[k] for k in changes}, "to": changes},
            )
        return await self.get(tenant_id)

    async def save_config(
        self, tenant_id: int, overrides: dict[str, Any], *, actor: str, reason: str = "",
        expected_revision: Optional[int] = None,
    ) -> Bundle:
        """Replace the client-layer overrides. Validated against the tenant's
        industry before anything is written."""
        tenant = await self.get(tenant_id)
        industry = await self.get_industry(tenant["industry_id"])
        before = tenant_config.resolve(industry["default_config"], tenant["config_json"]).as_dict()
        after = tenant_config.resolve(industry["default_config"], overrides).as_dict()
        async with self._pool.acquire() as con, con.transaction():
            row = await con.fetchrow(
                """
                UPDATE tenants SET config_json = $2::jsonb, config_revision = config_revision + 1,
                       updated_at = now()
                 WHERE id = $1 AND ($3::int IS NULL OR config_revision = $3)
                RETURNING config_revision
                """,
                tenant_id, json.dumps(overrides), expected_revision,
            )
            if row is None:
                raise Conflict("This tenant's config was changed by someone else; reload and try again.")
            await audit.record(
                con, tenant_id=tenant_id, actor=actor, event=audit.CONFIG_CHANGED, reason=reason,
                payload={"changes": tenant_config.diff(before, after), "overrides": overrides,
                         "revision": row["config_revision"]},
            )
        return await self.bundle(tenant_id)

    async def save_client_prompt(
        self, tenant_id: int, content: dict[str, Any], *, actor: str, note: str = "",
    ) -> Bundle:
        clean = prompt_layers.validate_client(content)
        await self.get(tenant_id)
        async with self._pool.acquire() as con, con.transaction():
            version = await self._next_version(con, CLIENT, tenant_id)
            await con.execute(
                """
                INSERT INTO prompt_versions (layer, ref_id, tenant_id, version, content, note, created_by)
                VALUES ('client', $1, $1, $2, $3::jsonb, $4, $5)
                """,
                tenant_id, version, json.dumps(clean), note, actor,
            )
            await con.execute(
                "UPDATE tenants SET prompt_version = $2, updated_at = now() WHERE id = $1", tenant_id, version
            )
            await audit.record(
                con, tenant_id=tenant_id, actor=actor, event=audit.PROMPT_VERSION_CREATED, reason=note,
                payload={"layer": CLIENT, "version": version},
            )
        return await self.bundle(tenant_id)

    async def rollback_client_prompt(self, tenant_id: int, version: int, *, actor: str, reason: str = "") -> Bundle:
        tenant = await self.get(tenant_id)
        await self.version(CLIENT, tenant_id, version)
        async with self._pool.acquire() as con, con.transaction():
            await con.execute(
                "UPDATE tenants SET prompt_version = $2, updated_at = now() WHERE id = $1", tenant_id, version
            )
            await audit.record(
                con, tenant_id=tenant_id, actor=actor, event=audit.PROMPT_ROLLBACK, reason=reason,
                payload={"layer": CLIENT, "from": tenant["prompt_version"], "to": version},
            )
        return await self.bundle(tenant_id)

    async def pin(self, tenant_id: int, version: Optional[int], *, actor: str, reason: str = "") -> Bundle:
        """Render this tenant with a fixed industry template version, or
        (version None) follow the industry's live version again."""
        tenant = await self.get(tenant_id)
        if version is not None:
            await self.version(INDUSTRY, tenant["industry_id"], version)
        async with self._pool.acquire() as con, con.transaction():
            await con.execute(
                "UPDATE tenants SET prompt_pin_version = $2, updated_at = now() WHERE id = $1", tenant_id, version
            )
            await audit.record(
                con, tenant_id=tenant_id, actor=actor, event=audit.PROMPT_PINNED, reason=reason,
                payload={"industry_id": tenant["industry_id"], "from": tenant["prompt_pin_version"], "to": version},
            )
        return await self.bundle(tenant_id)

    # -------------------------------------------------------- industries

    async def create_industry(
        self, name: str, *, parent_id: Optional[int] = None, actor: str, reason: str = "",
    ) -> dict[str, Any]:
        """A top-level industry, or with `parent_id` a sub-folder of that
        folder. A sub-folder starts as a copy of nothing: its clients get
        exactly what its parent hands down until it changes something, and
        it requires review when its parent does."""
        name = (name or "").strip()
        if not name:
            raise ValueError("A folder needs a name." if parent_id is not None else "An industry needs a name.")
        async with self._pool.acquire() as con, con.transaction():
            parent = None
            if parent_id is not None:
                parent = await con.fetchrow("SELECT * FROM industries WHERE id = $1", parent_id)
                if parent is None:
                    raise NotFound(f"no industry {parent_id}")
            if await con.fetchval(
                "SELECT 1 FROM industries WHERE coalesce(parent_id, 0) = coalesce($2::int, 0) AND lower(name) = lower($1)",
                name, parent_id,
            ):
                raise ValueError(f"{parent['name']} already has a folder called {name!r}." if parent
                                 else f"An industry called {name!r} already exists.")
            if parent is None:
                row = await con.fetchrow("INSERT INTO industries (name) VALUES ($1) RETURNING *", name)
                content = {"sections": {}}
            else:
                row = await con.fetchrow(
                    "INSERT INTO industries (name, parent_id, default_config, requires_review) "
                    "VALUES ($1, $2, $3::jsonb, $4) RETURNING *",
                    name, parent_id, parent["default_config"]
                    if isinstance(parent["default_config"], str) else json.dumps(parent["default_config"]),
                    parent["requires_review"],
                )
                content = {"overrides": {}}
            await con.execute(
                """
                INSERT INTO prompt_versions (layer, ref_id, version, content, note, created_by)
                VALUES ('industry', $1, 1, $2::jsonb, 'Created empty', $3)
                """,
                row["id"], json.dumps(content), actor,
            )
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.INDUSTRY_CREATED, reason=reason,
                payload={"industry_id": row["id"], "name": name, "parent_id": parent_id},
            )
        return _industry(row)

    async def rename_industry(self, industry_id: int, name: str, *, actor: str, reason: str = "") -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("A folder needs a name.")
        industry = await self.get_industry(industry_id)
        if name == industry["name"]:
            return industry
        async with self._pool.acquire() as con, con.transaction():
            if await con.fetchval(
                "SELECT 1 FROM industries WHERE coalesce(parent_id, 0) = coalesce($2::int, 0) "
                "AND lower(name) = lower($1) AND id <> $3",
                name, industry["parent_id"], industry_id,
            ):
                raise ValueError(f"There is already a folder called {name!r} there.")
            row = await con.fetchrow(
                "UPDATE industries SET name = $2, updated_at = now() WHERE id = $1 RETURNING *", industry_id, name)
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.INDUSTRY_UPDATED, reason=reason,
                payload={"industry_id": industry_id, "from": {"name": industry["name"]}, "to": {"name": name}},
            )
        return _industry(row)

    async def delete_industry(self, industry_id: int, *, actor: str, reason: str = "") -> None:
        """Only an empty sub-folder: no clients and no folders in it, and no
        finetune runs, operator rules or reply examples made in it (their
        history would point nowhere). Its template versions and finetune
        template go with it."""
        industry = await self.get_industry(industry_id)
        if industry["parent_id"] is None:
            raise ValueError("Only a sub-folder can be deleted, not a top-level industry.")
        async with self._pool.acquire() as con, con.transaction():
            if await con.fetchval("SELECT 1 FROM tenants WHERE industry_id = $1 LIMIT 1", industry_id):
                raise ValueError("Move its clients to another folder first.")
            if await con.fetchval("SELECT 1 FROM industries WHERE parent_id = $1 LIMIT 1", industry_id):
                raise ValueError("Delete the folders inside it first.")
            await con.execute("DELETE FROM prompt_versions WHERE layer = 'industry' AND ref_id = $1", industry_id)
            await con.execute("DELETE FROM platform_settings WHERE key = $1", f"finetune_template:{industry_id}")
            try:
                async with con.transaction():
                    await con.execute("DELETE FROM industries WHERE id = $1", industry_id)
            except asyncpg.ForeignKeyViolationError:
                raise ValueError("This folder has finetune runs, operator rules or reply examples of its own, "
                                 "so it is kept. Leave it empty instead.") from None
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.INDUSTRY_DELETED, reason=reason,
                payload={"industry_id": industry_id, "name": industry["name"], "parent_id": industry["parent_id"]},
            )

    async def tenants_in_industry(self, industry_id: int) -> list[dict[str, Any]]:
        rows = await self._pool.fetch("SELECT * FROM tenants WHERE industry_id = $1 ORDER BY id", industry_id)
        return [_tenant(r) for r in rows]

    async def tenants_in_subtree(self, industry_id: int) -> list[dict[str, Any]]:
        """The clients of this folder and of every folder under it: the ones
        a change to this folder reaches."""
        rows = await self._pool.fetch(
            f"WITH folders AS ({_SUBTREE_SQL}) SELECT * FROM tenants WHERE industry_id IN (SELECT id FROM folders) "
            "ORDER BY id", industry_id)
        return [_tenant(r) for r in rows]

    async def save_industry_config(
        self, industry_id: int, overrides: dict[str, Any], *, actor: str, reason: str = "",
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        """Replace what this folder sets itself. Its clients, and the folders
        under it, inherit the result (default_config, recomputed here for
        every one of them). Refused if it would leave any of their tenants
        with an invalid config."""
        folders = await self.subtree(industry_id)
        industry = folders[0]
        above = (await self.get_industry(industry["parent_id"]))["default_config"] if industry["parent_id"] else {}
        handed: dict[int, dict[str, Any]] = {}
        for folder in folders:
            own = overrides if folder["id"] == industry_id else folder["own_config"]
            handed[folder["id"]] = tenant_config.merge(
                above if folder["id"] == industry_id else handed[folder["parent_id"]], own)
        before = tenant_config.resolve(industry["default_config"], None).as_dict()
        after = tenant_config.resolve(handed[industry_id], None).as_dict()
        broken = []
        for folder in folders[1:]:
            try:
                tenant_config.resolve(handed[folder["id"]], None)
            except ConfigError as exc:
                broken.append({"path": f"folder {folder['id']} ({folder['name']})", "message": str(exc)})
        for tenant in await self.tenants_in_subtree(industry_id):
            try:
                tenant_config.resolve(handed[tenant["industry_id"]], tenant["config_json"])
            except ConfigError as exc:
                broken.append({"path": f"tenant {tenant['id']} ({tenant['name']})", "message": str(exc)})
        if broken:
            raise ConfigError(broken)
        async with self._pool.acquire() as con, con.transaction():
            row = await con.fetchrow(
                """
                UPDATE industries SET own_config = $2::jsonb, default_config = $3::jsonb,
                       config_revision = config_revision + 1, updated_at = now()
                 WHERE id = $1 AND ($4::int IS NULL OR config_revision = $4)
                RETURNING *
                """,
                industry_id, json.dumps(overrides), json.dumps(handed[industry_id]), expected_revision,
            )
            if row is None:
                raise Conflict("This industry's config was changed by someone else; reload and try again.")
            for folder in folders[1:]:
                if handed[folder["id"]] != folder["default_config"]:
                    await con.execute("UPDATE industries SET default_config = $2::jsonb, updated_at = now() "
                                      "WHERE id = $1", folder["id"], json.dumps(handed[folder["id"]]))
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.INDUSTRY_CONFIG_CHANGED, reason=reason,
                payload={"industry_id": industry_id, "changes": tenant_config.diff(before, after),
                         "overrides": overrides},
            )
        return _industry(row)

    async def save_folder_standard(
        self, industry_id: int, sections: dict[str, str], *, actor: str, note: str = "",
    ) -> dict[str, Any]:
        """Save a whole standard (every section, as a finetune run or a fold
        writes it) as this folder's template. A top-level industry takes it
        as it is; a sub-folder keeps only what differs from its parent's
        (prompt_layers.folder_changes), so it goes on inheriting the rest."""
        industry = await self.get_industry(industry_id)
        clean = prompt_layers.validate_industry({"sections": sections})
        if industry["parent_id"] is not None:
            parent = await self.folder_sections(industry["parent_id"])
            clean = prompt_layers.folder_changes(parent, clean["sections"])
        return await self.save_industry_template(industry_id, clean, actor=actor, note=note)

    async def save_industry_template(
        self, industry_id: int, content: dict[str, Any], *, actor: str, note: str = "",
    ) -> dict[str, Any]:
        """A new template version, made live at once. (Phase 6 puts the
        replay harness between saving a version and making it live.) A
        sub-folder's content is only its changes (validate_folder)."""
        industry = await self.get_industry(industry_id)
        clean = (prompt_layers.validate_industry(content) if industry["parent_id"] is None
                 else prompt_layers.validate_folder(content))
        async with self._pool.acquire() as con, con.transaction():
            version = await self._next_version(con, INDUSTRY, industry_id)
            await con.execute(
                """
                INSERT INTO prompt_versions (layer, ref_id, version, content, note, created_by)
                VALUES ('industry', $1, $2, $3::jsonb, $4, $5)
                """,
                industry_id, version, json.dumps(clean), note, actor,
            )
            await con.execute(
                "UPDATE industries SET template_version = $2, updated_at = now() WHERE id = $1",
                industry_id, version,
            )
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.PROMPT_VERSION_CREATED, reason=note,
                payload={"layer": INDUSTRY, "industry_id": industry_id, "version": version},
            )
        return await self.get_industry(industry_id)

    async def rollback_industry_template(
        self, industry_id: int, version: int, *, actor: str, reason: str = "",
    ) -> dict[str, Any]:
        industry = await self.get_industry(industry_id)
        await self.version(INDUSTRY, industry_id, version)
        async with self._pool.acquire() as con, con.transaction():
            await con.execute(
                "UPDATE industries SET template_version = $2, updated_at = now() WHERE id = $1",
                industry_id, version,
            )
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.PROMPT_ROLLBACK, reason=reason,
                payload={"layer": INDUSTRY, "industry_id": industry_id,
                         "from": industry["template_version"], "to": version},
            )
        return await self.get_industry(industry_id)

    # -------------------------------------------------------------- base

    async def save_base(self, rules: str, *, actor: str, note: str = "") -> dict[str, Any]:
        clean = prompt_layers.validate_base({"rules": rules})
        async with self._pool.acquire() as con, con.transaction():
            version = await self._next_version(con, BASE, 0)
            await con.execute(
                """
                INSERT INTO prompt_versions (layer, ref_id, version, content, note, created_by)
                VALUES ('base', 0, $1, $2::jsonb, $3, $4)
                """,
                version, json.dumps(clean), note, actor,
            )
            await self._set_base_pointer(con, version, actor)
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.PROMPT_VERSION_CREATED, reason=note,
                payload={"layer": BASE, "version": version},
            )
        return await self.base()

    async def rollback_base(self, version: int, *, actor: str, reason: str = "") -> dict[str, Any]:
        current = await self.base_version()
        await self.version(BASE, 0, version)
        async with self._pool.acquire() as con, con.transaction():
            await self._set_base_pointer(con, version, actor)
            await audit.record(
                con, tenant_id=None, actor=actor, event=audit.PROMPT_ROLLBACK, reason=reason,
                payload={"layer": BASE, "from": current, "to": version},
            )
        return await self.base()

    # ---------------------------------------------------------- helpers

    @staticmethod
    async def _next_version(con: asyncpg.Connection, layer: str, ref_id: int) -> int:
        # Serialise concurrent saves of the same layer so two of them can't
        # both pick the same number (the UNIQUE constraint would reject the
        # second anyway, but with an unhelpful error).
        await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"prompt:{layer}:{ref_id}")
        current = await con.fetchval(
            "SELECT max(version) FROM prompt_versions WHERE layer = $1 AND ref_id = $2", layer, ref_id
        )
        return (current or 0) + 1

    @staticmethod
    async def _set_base_pointer(con: asyncpg.Connection, version: int, actor: str) -> None:
        await con.execute(
            """
            UPDATE platform_settings SET value = $1::jsonb, updated_by = $2, updated_at = now()
             WHERE key = 'base_prompt_version'
            """,
            json.dumps(version), actor,
        )


def tenant_data_dir(root: Path, tenant_id: int, session_id: Optional[str] = None) -> Path:
    """DATA_DIR/tenants/<tenant id>: this tenant's files (media library,
    bookings.json, last_halt.txt), keyed by tenant so they stay with the
    tenant, not with whichever account it runs on. Before the platform they
    lived in DATA_DIR/<session_id>; the first call after the upgrade moves
    that folder here. The panel and a worker may both get there first, so
    losing the race to the rename is fine."""
    target = root / "tenants" / str(int(tenant_id))
    if session_id and not target.exists():
        legacy = root / session_id
        if legacy.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                legacy.rename(target)
                log.info("Moved %s to %s.", legacy, target)
            except OSError:
                if not target.exists():
                    raise
    target.mkdir(parents=True, exist_ok=True)
    return target


# ------------------------------------------------------------ legacy import
#
# Before the platform, each account's behaviour lived in session_config:
# persona text, timing, safety limits and so on. backfill() turns that into
# the account's tenant overrides once, after migration 0002, and fills in
# customer_ref for conversations stored before it existed. It is idempotent:
# a tenant is imported once (legacy_imported_at), and only NULL refs are set.

_LANGUAGE_WORDS = {
    "fixed:en": ("english", "angļu", "английск", "en"),
    "fixed:lv": ("latvian", "latviešu", "латышск", "lv"),
    "fixed:ru": ("russian", "krievu", "русск", "ru"),
}
_MIRROR_WORDS = ("mirror", "same language", "language they", "their language", "whatever language")


def legacy_language(text: str) -> tuple[str, str]:
    """(language_policy, leftover note). One clearly named language becomes
    a fixed policy; anything else becomes mirror, and the original text is
    kept as a note so nothing the operator wrote is lost."""
    raw = (text or "").strip()
    if not raw:
        return "mirror", ""
    low = raw.lower()
    words = set(low.replace(",", " ").replace(".", " ").split())
    if any(w in low for w in _MIRROR_WORDS):
        return "mirror", ""
    hits = [
        policy for policy, names in _LANGUAGE_WORDS.items()
        if any((n in words) if len(n) == 2 else (n in low) for n in names)
    ]
    if len(hits) == 1:
        return hits[0], ""
    return "mirror", f"Language: {raw}"


def _prune(defaults: Any, values: Any) -> Any:
    """Only the leaves of `values` that differ from `defaults`, as a nested
    dict; None when nothing differs."""
    # Recurse into sub-sections only; an empty-dict default (price_floors) is
    # a single leaf.
    if isinstance(defaults, dict) and isinstance(values, dict) and defaults:
        out = {}
        for key, value in values.items():
            if key not in defaults:
                continue
            kept = _prune(defaults[key], value)
            if kept is not None:
                out[key] = kept
        return out or None
    return None if values == defaults else values


def legacy_overrides(legacy: dict[str, Any], *, used_outreach: bool) -> dict[str, Any]:
    """session_config (as config_store.normalize returns it) -> client-layer
    config overrides holding only what differs from the platform defaults."""
    timing, safety = legacy["timing"], legacy["safety"]
    policy, _ = legacy_language(legacy["persona"].get("languages", ""))
    candidate = {
        "timezone": timing["timezone"],
        "auto_send": legacy["behavior"]["auto_send"],
        "reply_delay": {"min_s": timing["min_delay_seconds"], "max_s": timing["max_delay_seconds"]},
        # The old setting was the window when replies DO go out; quiet hours
        # are its complement.
        "quiet_hours": {
            "enabled": timing["active_hours_enabled"],
            "start": timing["active_hours_end"],
            "end": timing["active_hours_start"],
        },
        "daily_message_cap": safety["daily_send_limit"],
        "language_policy": policy,
        "human": legacy["human"],
        "presence": legacy["presence"],
        "ai": legacy["ai"],
        "safety": {
            "daily_peer_cap": safety["daily_peer_limit"],
            "halt_on_peer_flood": safety["halt_on_peer_flood"],
            "known_contacts_only": safety["known_contacts_only"],
            "max_flood_wait_seconds": safety["max_flood_wait_seconds"],
        },
        # Kept on only for an account that has actually used it.
        "outreach": {**legacy["outreach"], "enabled": used_outreach},
        "media": legacy["media"],
        "booking": _legacy_booking(legacy["booking"]),
    }
    defaults = tenant_config.TenantConfig().model_dump(mode="json")
    return _prune(defaults, candidate) or {}


def _legacy_booking(booking: dict[str, Any]) -> dict[str, Any]:
    """The old single check-in reminder becomes a one-item reminders list."""
    out = {k: v for k, v in booking.items() if k != "reminder_minutes_before"}
    minutes = int(booking.get("reminder_minutes_before") or 0)
    out["reminders"] = [{"minutes_before": minutes, "instruction": ""}] if minutes >= 5 else []
    return out


def legacy_prompt(legacy: dict[str, Any]) -> dict[str, Any]:
    persona = legacy["persona"]
    mapping = {
        "about": persona.get("purpose", ""),
        "tone": persona.get("tone", ""),
        "boundaries": persona.get("boundaries", ""),
        "sign_off": persona.get("signature_style", ""),
        "writing_samples": legacy["finetune"].get("writing_samples", ""),
    }
    # The persona's boundaries come on top of the industry's, never instead.
    overrides = {
        key: {"mode": "append" if key in prompt_layers.APPEND_ONLY_SECTIONS else "override",
              "text": text[: prompt_layers.MAX_SECTION_CHARS]}
        for key, text in mapping.items() if text.strip()
    }
    _, note = legacy_language(persona.get("languages", ""))
    return {"overrides": overrides, "addendum": note[: prompt_layers.MAX_ADDENDUM_CHARS]}


async def import_legacy(pool: asyncpg.Pool, tenant: dict[str, Any]) -> dict[str, Any]:
    """Turn one tenant's pre-platform session_config into its overrides."""
    store = TenantStore(pool)
    session_id = tenant["session_id"]
    raw = await pool.fetchval("SELECT config FROM session_config WHERE session_id = $1", session_id)
    report: dict[str, Any] = {"config": {}, "prompt_sections": [], "dropped": []}
    if raw is not None:
        legacy = config_store.normalize(_json(raw))
        used_outreach = bool(await pool.fetchval("SELECT 1 FROM outreach WHERE session_id = $1 LIMIT 1", session_id))
        overrides = legacy_overrides(legacy, used_outreach=used_outreach)
        industry = await store.get_industry(tenant["industry_id"])
        # Whatever does not validate under the new schema (an unknown
        # timezone, say) is dropped and reported rather than blocking the
        # rest of the import.
        for _ in range(len(tenant_config.leaf_paths())):
            try:
                tenant_config.resolve(industry["default_config"], overrides)
                break
            except ConfigError as exc:
                for error in exc.errors:
                    report["dropped"].append(error)
                    _drop(overrides, error["path"])
        else:
            # An error that names no droppable field: import nothing rather
            # than guess. The persona below is still imported.
            report["dropped"].append({"path": "(all)", "message": "config could not be made valid"})
            overrides = {}
        if overrides:
            await store.save_config(tenant["id"], overrides, actor=audit.MIGRATION,
                                    reason="Imported from the pre-platform account settings")
        report["config"] = overrides
        prompt = legacy_prompt(legacy)
        if prompt["overrides"] or prompt["addendum"]:
            await store.save_client_prompt(tenant["id"], prompt, actor=audit.MIGRATION,
                                           note="Imported from the pre-platform persona")
            report["prompt_sections"] = sorted(prompt["overrides"])
    async with pool.acquire() as con, con.transaction():
        await con.execute("UPDATE tenants SET legacy_imported_at = now() WHERE id = $1", tenant["id"])
        await audit.record(con, tenant_id=tenant["id"], actor=audit.MIGRATION, event=audit.LEGACY_IMPORTED,
                           reason="Pre-platform settings imported", payload=report)
    return report


def _drop(overrides: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node = overrides
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)
    # Remove sub-objects the drop left empty.
    if len(parts) > 1 and isinstance(overrides.get(parts[0]), dict) and not overrides[parts[0]]:
        overrides.pop(parts[0])


async def backfill(pool: asyncpg.Pool) -> dict[str, Any]:
    """Run once after migrations (migrate_entrypoint.py). Safe to re-run."""
    imported = []
    rows = await pool.fetch(
        "SELECT * FROM tenants WHERE legacy_imported_at IS NULL AND session_id IS NOT NULL ORDER BY id"
    )
    for row in rows:
        tenant = _tenant(row)
        report = await import_legacy(pool, tenant)
        imported.append({"tenant_id": tenant["id"], **report})
        log.info("Imported pre-platform settings into tenant %s (%s).", tenant["id"], tenant["name"])

    refs = 0
    missing = await pool.fetch(
        "SELECT c.tenant_id, c.session_id, c.chat_id, t.channel FROM conversations c "
        "JOIN tenants t ON t.id = c.tenant_id WHERE c.customer_ref IS NULL"
    )
    for row in missing:
        await pool.execute(
            "UPDATE conversations SET customer_ref = $3 WHERE tenant_id = $1 AND chat_id = $2",
            row["tenant_id"], row["chat_id"], crypto.customer_ref(row["tenant_id"], row["channel"], row["chat_id"]),
        )
        refs += 1
    return {"imported": imported, "customer_refs": refs}
