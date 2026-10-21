-- Operator corrections as rules (rules.py, rules_api.py).
--
-- A rule is one short sentence the bot must follow, proposed by the model
-- from a draft an operator edited before approving it (messages.draft_text,
-- 0016) or from a correction on a flagged chat (finetune_examples, 0012), or
-- written by hand. The admin accepts it; active rules go into every reply's
-- prompt (prompt_layers.render), for every account of the industry or for
-- one account. Once a week they are folded into the prompt itself through
-- an ordinary finetune run (source 'rules') the admin reviews and applies;
-- applying marks them merged, and they stop being added separately.
--
-- Rules never hold chat text: the model writes them from masked context,
-- and the admin reads every one before it goes live.

CREATE TABLE prompt_rules (
  id          BIGSERIAL   PRIMARY KEY,
  industry_id INTEGER     NOT NULL REFERENCES industries(id),
  -- NULL: every account of the industry (the escort accounts share one
  -- tuning, so this is the usual scope). Set: only this account.
  tenant_id   INTEGER     REFERENCES tenants(id),
  -- Where a fold should put it: one of prompt_layers.SECTION_KEYS.
  section     TEXT        NOT NULL CHECK (section IN ('about', 'services', 'hours_location', 'booking', 'faq',
                                                      'tone', 'boundaries', 'sign_off', 'writing_samples')),
  text        TEXT        NOT NULL CHECK (char_length(text) BETWEEN 5 AND 400),
  -- draft_edit: from an edited draft; correction: from a flagged chat's
  -- correction; manual: typed by the admin.
  source      TEXT        NOT NULL CHECK (source IN ('draft_edit', 'correction', 'manual')),
  -- What it came from: {"tenant_id": n, "message_id": n} for an edited
  -- draft, plus "example_id" for a correction; {"tenant_id": n} for a rule
  -- typed on an account's page.
  source_ref  JSONB       NOT NULL DEFAULT '{}',
  -- proposed: waiting for the admin; active: in every reply's prompt;
  -- merged: folded into a prompt version (merged_into); rejected: never
  -- proposed again for the same correction; retired: taken out by hand.
  status      TEXT        NOT NULL DEFAULT 'proposed'
              CHECK (status IN ('proposed', 'active', 'merged', 'rejected', 'retired')),
  -- The prompt version tag that absorbed it, e.g. "b1/i1v4/c2".
  merged_into TEXT,
  created_by  TEXT        NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Every reply's prompt reads the active rules of one industry and one
-- account (TenantStore.bundle), and the cap counts them per scope.
CREATE INDEX idx_prompt_rules_industry ON prompt_rules (industry_id, status) WHERE tenant_id IS NULL;
CREATE INDEX idx_prompt_rules_tenant ON prompt_rules (tenant_id, status) WHERE tenant_id IS NOT NULL;
-- "Has this correction a rule yet?", in any status, so a rejected one is
-- never proposed again.
CREATE INDEX idx_prompt_rules_source ON prompt_rules (source, ((source_ref->>'message_id')))
  WHERE source <> 'manual';

-- The edits and corrections the suggestion has already sent to the model.
-- Most of them hold no general rule (a typo, a one-off), and without this
-- the same ones would be sent again on every suggestion, ahead of new ones.
CREATE TABLE prompt_rule_reads (
  source     TEXT        NOT NULL CHECK (source IN ('draft_edit', 'correction')),
  -- messages.id of the edited draft, or of the corrected message.
  message_id BIGINT      NOT NULL,
  tenant_id  INTEGER     NOT NULL REFERENCES tenants(id),
  read_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, message_id)
);

-- A finetune run can now fold the active rules into the prompt.
ALTER TABLE finetune_runs DROP CONSTRAINT finetune_runs_source_check;
ALTER TABLE finetune_runs ADD CONSTRAINT finetune_runs_source_check
  CHECK (source IN ('screenshots', 'text', 'examples', 'hand', 'rules'));
