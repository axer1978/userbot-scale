-- Context reviews (context_review.py, context_review_api.py; Finetune → Context review).
--
-- A review is one DeepSeek call, on the platform key, that reads an account's real chats of the last days (and
-- those of other accounts the admin adds), masked like the finetune runs, and reports what clients ask, what gets
-- the booking, where the bot goes wrong and the lines it repeats. Report only: nothing is applied to any prompt.
--
-- The chats themselves are never stored. `chats` names the ones read (ref, account, session, chat id, outcome),
-- `counts` holds numbers counted from the database when the review started, and `report` the model's answer as
-- checked (context_review.parse_report). raw_output keeps the model's answer only when it could not be read as a
-- report, so the admin can see what came back.

CREATE TABLE context_reviews (
  id              BIGSERIAL   PRIMARY KEY,
  tenant_id       INTEGER     NOT NULL REFERENCES tenants(id),
  -- The other accounts whose chats were read too.
  also_tenant_ids JSONB       NOT NULL DEFAULT '[]',
  days            INTEGER     NOT NULL CHECK (days BETWEEN 1 AND 365),
  status          TEXT        NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
  model           TEXT        NOT NULL,
  -- [{ref, tenant_id, session_id, chat_id, account, outcome}]: never text.
  chats           JSONB       NOT NULL DEFAULT '[]',
  -- Exact numbers from the database (context_review_api._counts).
  counts          JSONB,
  -- The model's reading, checked.
  report          JSONB,
  raw_output      TEXT        NOT NULL DEFAULT '',
  -- What the check dropped from the model's answer.
  errors          JSONB       NOT NULL DEFAULT '[]',
  error           TEXT        NOT NULL DEFAULT '',
  created_by      TEXT        NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at     TIMESTAMPTZ
);
CREATE INDEX idx_context_reviews_tenant ON context_reviews (tenant_id, created_at DESC);
