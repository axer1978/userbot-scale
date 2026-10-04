-- Content review: identity/age verification by video and admin review of
-- every photo a client adds, for industries that require it (review.py).
--
-- An industry marked requires_review (the escort market) keeps each of its
-- tenants under a 'verification' hold until a linked client login has an
-- approved verification video. The admin can also ask any client to verify
-- again ("suspected"), which holds their tenants until approved.
--
-- Photos a client submits wait in media_submissions, outside the folder the
-- bot reads, and are copied into its media library only once approved.

ALTER TABLE industries ADD COLUMN requires_review BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE tenant_holds DROP CONSTRAINT tenant_holds_kind_check;
ALTER TABLE tenant_holds ADD CONSTRAINT tenant_holds_kind_check
  CHECK (kind IN ('manual', 'billing', 'spend_cap', 'anomaly', 'telegram', 'whatsapp', 'verification'));

-- One row per verification round of a client login; the newest row is the
-- login's state ('approved' = verified).
CREATE TABLE verifications (
  id             SERIAL      PRIMARY KEY,
  owner_id       INTEGER     NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
  status         TEXT        NOT NULL DEFAULT 'requested'
                 CHECK (status IN ('requested', 'submitted', 'approved', 'rejected')),
  -- Why this round was opened: "required for this industry", or the
  -- admin's reason when they asked for it.
  reason         TEXT        NOT NULL DEFAULT '',
  requested_by   TEXT        NOT NULL,
  -- What the video must show: the code written on paper and the gesture.
  challenge      TEXT,
  gesture        TEXT,
  challenge_at   TIMESTAMPTZ,
  -- The video, AES-GCM encrypted under the master key (review.py), and
  -- deleted VIDEO_RETENTION_DAYS after the decision.
  video_file     TEXT,
  video_type     TEXT,
  video_bytes    INTEGER,
  submitted_at   TIMESTAMPTZ,
  video_deleted_at TIMESTAMPTZ,
  reviewed_by    TEXT,
  reviewed_at    TIMESTAMPTZ,
  review_reason  TEXT        NOT NULL DEFAULT '',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_verifications_owner ON verifications (owner_id, id DESC);
CREATE INDEX idx_verifications_submitted ON verifications (submitted_at) WHERE status = 'submitted';

CREATE TABLE media_submissions (
  id             SERIAL      PRIMARY KEY,
  tenant_id      INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  owner_id       INTEGER     REFERENCES owners(id) ON DELETE SET NULL,
  -- 'owner' = the client uploaded it; 'recheck' = the admin pulled a live
  -- photo back into review.
  source         TEXT        NOT NULL CHECK (source IN ('owner', 'recheck')),
  file           TEXT        NOT NULL,
  original_name  TEXT        NOT NULL DEFAULT '',
  kind           TEXT        NOT NULL CHECK (kind IN ('photo', 'video')),
  bytes          INTEGER     NOT NULL DEFAULT 0,
  description    TEXT        NOT NULL DEFAULT '',
  -- The media library id this one replaces once approved.
  replaces_item  INTEGER,
  status         TEXT        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
  -- The media library id it got when approved.
  media_item     INTEGER,
  reviewed_by    TEXT,
  reviewed_at    TIMESTAMPTZ,
  review_reason  TEXT        NOT NULL DEFAULT '',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_media_submissions_tenant ON media_submissions (tenant_id, id DESC);
CREATE INDEX idx_media_submissions_pending ON media_submissions (created_at) WHERE status = 'pending';
