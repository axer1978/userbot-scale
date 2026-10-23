-- Reply examples (examples.py, examples_api.py; config retrieval): earlier
-- moments of real chats, shown to the model as examples of how the business
-- handles a message like the one it is answering.
--
-- One row per turn of a chat: the client's messages and the business's
-- reply to them, where the reply was typed by a person, approved after an
-- operator changed the bot's draft, or comes from a chat the admin flagged
-- good. Both sides are masked when stored (finetune.mask: the client's name
-- and handle, phone numbers, e-mails, the business's address and way in),
-- so the chat itself is never read again to show one. The business's side
-- is always what was really sent, never generated.
--
-- example_queries holds the texts a new client message is compared with:
-- the client's own words (generated = false, one per example) and other
-- ways a client might write the same (generated = true, worded by DeepSeek
-- from the masked client text alone), each with its vector from the
-- embedder (embeddings.py). No pgvector: an industry has a few thousand
-- vectors at most, and the runtime compares them in memory.

CREATE TABLE reply_examples (
  id             BIGSERIAL   PRIMARY KEY,
  tenant_id      INTEGER     NOT NULL REFERENCES tenants(id),
  -- The account's industry, kept in step when the account moves. Examples
  -- without figures are shared with the industry's other accounts
  -- (retrieval.share_in_industry).
  industry_id    INTEGER     NOT NULL REFERENCES industries(id),
  chat_id        BIGINT      NOT NULL,
  -- messages.id of the turn's first client message, and of the last
  -- business message of the reply.
  in_first_id    BIGINT      NOT NULL,
  reply_last_id  BIGINT      NOT NULL,
  -- Our line before the client's messages ('' at the start of a chat).
  context_text   TEXT        NOT NULL DEFAULT '',
  -- The turn's client messages, one per line.
  client_text    TEXT        NOT NULL,
  -- The business's messages, one per line; a rates or extras message is
  -- "[rates message]" (it goes out word for word from the prompt).
  reply_text     TEXT        NOT NULL,
  -- hand: every message of the reply typed by a person (on the phone or in
  --       the panel);
  -- edited: the bot's draft, changed by an operator before approving it
  --       (messages.draft_text); the text is what was sent;
  -- flagged: from a chat the admin flagged good (finetune_examples), up to
  --       where it was flagged; the bot's own replies count there.
  source         TEXT        NOT NULL CHECK (source IN ('hand', 'edited', 'flagged')),
  -- plain when the admin flagged the chat as an ordinary example.
  verdict        TEXT        NOT NULL DEFAULT 'good' CHECK (verdict IN ('good', 'plain')),
  -- From the chat's flag, or else from its bookings (completed: came).
  outcome        TEXT        NOT NULL DEFAULT ''
                 CHECK (outcome IN ('', 'came', 'no_show', 'cancelled', 'never_booked', 'blocked')),
  client_kind    TEXT        NOT NULL DEFAULT ''
                 CHECK (client_kind IN ('', 'serious', 'time_waster', 'safety_risk', 'regular')),
  -- Where the chat's bookings stood when the business replied: no time
  -- asked for yet, a time waiting for the owner, a confirmed time still
  -- ahead, or a visit just past. time_pending is never stored: the owner
  -- alone answers then, and the bot must not learn to.
  phase          TEXT        NOT NULL CHECK (phase IN ('before_time', 'time_pending', 'booked', 'after')),
  -- Any digit in reply_text: a price or a time belongs to its own account,
  -- so these are never shared with the industry.
  has_figures    BOOLEAN     NOT NULL,
  -- The prompt version the bot's draft was written with (edited, flagged);
  -- NULL for a reply typed by hand.
  prompt_version TEXT,
  -- Off: never shown (the admin switched it off).
  active         BOOLEAN     NOT NULL DEFAULT TRUE,
  -- When other wordings of client_text were asked for; NULL: not yet.
  worded_at      TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, chat_id, reply_last_id)
);
CREATE INDEX idx_reply_examples_tenant ON reply_examples (tenant_id, active, phase);
CREATE INDEX idx_reply_examples_industry ON reply_examples (industry_id, phase) WHERE active AND NOT has_figures;
CREATE INDEX idx_reply_examples_unworded ON reply_examples (tenant_id, id) WHERE worded_at IS NULL;

CREATE TABLE example_queries (
  id          BIGSERIAL   PRIMARY KEY,
  example_id  BIGINT      NOT NULL REFERENCES reply_examples(id) ON DELETE CASCADE,
  text        TEXT        NOT NULL,
  generated   BOOLEAN     NOT NULL DEFAULT FALSE,
  -- Unit length (the embedder's), so a dot product is the cosine.
  embedding   REAL[]      NOT NULL CHECK (cardinality(embedding) > 0),
  -- Vectors of two models never compare: a new model means embedding again.
  embed_model TEXT        NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_example_queries_example ON example_queries (example_id, generated);
-- The client's own words, once per example.
CREATE UNIQUE INDEX idx_example_queries_original ON example_queries (example_id) WHERE NOT generated;
CREATE INDEX idx_example_queries_model ON example_queries (embed_model);
