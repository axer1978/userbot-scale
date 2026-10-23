-- Real chats flagged for finetuning (finetune.py, finetune_api.py).
--
-- The admin opens a finished chat and marks it: handled well or badly, what
-- kind of client it was, how it ended, why, and corrections on single
-- lines. A finetune run can then read the flagged chats instead of
-- screenshots or typed transcripts.
--
-- Only the admin's judgement is stored here. The chat itself stays where it
-- already is (messages) and is read at run time, up to the message the
-- chat had reached when it was flagged.

CREATE TABLE finetune_examples (
  id              BIGSERIAL   PRIMARY KEY,
  tenant_id       INTEGER     NOT NULL REFERENCES tenants(id),
  chat_id         BIGINT      NOT NULL,
  -- messages.id of the chat's last message when it was saved; anything
  -- written in the chat later is not part of the example.
  upto_message_id BIGINT      NOT NULL,
  -- good: learn from it; bad: replies not to repeat; plain: an ordinary example.
  verdict         TEXT        NOT NULL CHECK (verdict IN ('good', 'bad', 'plain')),
  client_kind     TEXT        NOT NULL DEFAULT ''
                  CHECK (client_kind IN ('', 'serious', 'time_waster', 'safety_risk', 'regular')),
  outcome         TEXT        NOT NULL DEFAULT ''
                  CHECK (outcome IN ('', 'came', 'no_show', 'cancelled', 'never_booked', 'blocked')),
  -- How the model should read this chat, in the admin's words.
  note            TEXT        NOT NULL DEFAULT '',
  -- {"<messages.id>": "what was wrong / what it should have said"}
  corrections     JSONB       NOT NULL DEFAULT '{}',
  -- The last run that read it, so the page can offer the ones not read yet.
  last_run_id     BIGINT      REFERENCES finetune_runs(id) ON DELETE SET NULL,
  created_by      TEXT        NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, chat_id)
);
CREATE INDEX idx_finetune_examples_tenant ON finetune_examples (tenant_id, updated_at DESC);

-- A run can now read flagged chats.
ALTER TABLE finetune_runs DROP CONSTRAINT finetune_runs_source_check;
ALTER TABLE finetune_runs ADD CONSTRAINT finetune_runs_source_check
  CHECK (source IN ('screenshots', 'text', 'examples'));
