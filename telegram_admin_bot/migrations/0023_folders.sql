-- Folders inside folders: an industry can sit inside another one, as a
-- sub-folder (escort › Tallinn), any number of levels deep. Clients in a
-- sub-folder get the parent's prompt and settings, and the sub-folder's own
-- changes on top, so a group of clients can have its own finetuning while
-- the rest of the industry keeps the shared one (tenants.py).
--
-- A top-level industry keeps its prompt as {"sections": {...}}. A
-- sub-folder's prompt versions hold only what it changes, the way a
-- client's do: {"overrides": {"<section>": {"mode": "override"|"append",
-- "text": "..."}}} (prompt_layers.validate_folder).
--
-- Settings: own_config is what the folder itself sets; default_config is
-- what its clients inherit, the parent's default_config with own_config on
-- top. For a top-level industry the two are the same. Saving a folder's
-- settings recomputes default_config for it and every folder under it, so
-- everything that reads default_config (digest, billing, the owner pages)
-- keeps working unchanged.

ALTER TABLE industries ADD COLUMN parent_id INTEGER REFERENCES industries(id);
ALTER TABLE industries ADD COLUMN own_config JSONB NOT NULL DEFAULT '{}'::jsonb;
UPDATE industries SET own_config = default_config;
ALTER TABLE industries ADD CONSTRAINT industries_not_own_parent CHECK (parent_id IS NULL OR parent_id <> id);

-- Names are unique among the folders of one parent, not everywhere: two
-- industries can each have a "Tallinn".
ALTER TABLE industries DROP CONSTRAINT industries_name_key;
CREATE UNIQUE INDEX industries_name_per_parent ON industries (coalesce(parent_id, 0), lower(name));
CREATE INDEX idx_industries_parent ON industries (parent_id);
