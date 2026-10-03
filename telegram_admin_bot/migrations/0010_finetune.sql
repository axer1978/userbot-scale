-- Finetune from chat screenshots (finetune.py, finetune_api.py).
--
-- The admin uploads screenshots of one business's real chats; a vision
-- model reads them with the industry's finetune template and proposes two
-- prompt layers: the business's client layer and an updated industry
-- template. Nothing goes live until the admin applies a run, and applying
-- saves ordinary prompt versions (with rollback) through tenants.py.
--
-- The screenshots themselves are never stored: they are customers' chats.
-- Only their file names are kept, to show what a run was made from.

CREATE TABLE finetune_runs (
  id               BIGSERIAL   PRIMARY KEY,
  tenant_id        INTEGER     NOT NULL REFERENCES tenants(id),
  industry_id      INTEGER     NOT NULL REFERENCES industries(id),
  status           TEXT        NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'done', 'failed', 'applied', 'discarded')),
  model            TEXT        NOT NULL,
  -- screenshots: read by the vision model; text: chats the operator
  -- transcribed, read by the text model. Like the screenshots, transcripts
  -- are never stored; `files` keeps the conversation names.
  source           TEXT        NOT NULL DEFAULT 'screenshots' CHECK (source IN ('screenshots', 'text')),
  files            JSONB       NOT NULL DEFAULT '[]',
  -- The industry template version the run compared against. Applying is
  -- refused once the industry has moved on, so a run never overwrites
  -- another business's merge into the standard.
  industry_version INTEGER     NOT NULL,
  raw_output       TEXT        NOT NULL DEFAULT '',
  -- {business_layer, industry_sections, notes, errors}
  result           JSONB,
  error            TEXT        NOT NULL DEFAULT '',
  -- {industry_version, client_version}: what applying saved.
  applied          JSONB,
  created_by       TEXT        NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ,
  applied_at       TIMESTAMPTZ
);
CREATE INDEX idx_finetune_runs_tenant ON finetune_runs (tenant_id, created_at DESC);
CREATE INDEX idx_finetune_runs_industry ON finetune_runs (industry_id, status);
