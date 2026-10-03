-- Staff roles and the approval queue (staff.py).
--
-- A role says, for every action of the catalogue in staff.py, whether a
-- manager holding it may not do it ('off'), may do it ('allow'), or does it
-- for the admin's approval ('approve'). Anything missing from a role is off.
--
-- staff_requests is every change a manager made through a gated route:
-- 'applied' (allowed, or protective and so done at once), 'pending' (waits
-- for the admin), then 'approved' (run), 'failed' (run, refused) or
-- 'rejected' (never run).

CREATE TABLE staff_roles (
  id           SERIAL      PRIMARY KEY,
  name         TEXT        NOT NULL,
  description  TEXT        NOT NULL DEFAULT '',
  -- May sign in to the admin panel (else only /manager/).
  admin_panel  BOOLEAN     NOT NULL DEFAULT false,
  permissions  JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_staff_roles_name ON staff_roles (lower(name));

-- What managers could do before roles existed (manager_api.py's list).
INSERT INTO staff_roles (name, description, admin_panel, permissions) VALUES
  ('Moderator', 'The moderator panel (/manager/): pause bots, read conversations, alerts, sign-ups.', false,
   '{"view.safety": "allow", "view.conversations": "allow", "view.clients": "allow",
     "tenant.pause": "allow", "chat.pause": "allow", "alerts.ack": "allow",
     "clients.approve": "allow", "clients.disable": "allow"}'::jsonb),
  ('Senior moderator', 'Works in the admin panel. Sees everything; changes wait for the admin''s approval.', true,
   '{"view.conversations": "allow", "view.config": "allow", "view.bookings": "allow", "view.safety": "allow",
     "view.clients": "allow", "view.audit": "allow", "view.unanswered": "allow", "view.training": "allow",
     "tenant.pause": "approve", "chat.pause": "approve", "chat.manage": "approve", "chat.send": "approve",
     "drafts.approve": "approve", "drafts.reject": "approve", "alerts.ack": "approve",
     "bookings.act": "approve", "unanswered.handle": "approve",
     "clients.approve": "approve", "clients.disable": "approve"}'::jsonb);

ALTER TABLE managers ADD COLUMN role_id INTEGER REFERENCES staff_roles(id);
UPDATE managers SET role_id = (SELECT id FROM staff_roles WHERE name = 'Moderator');

CREATE TABLE staff_requests (
  id            BIGSERIAL   PRIMARY KEY,
  manager_id    INTEGER     REFERENCES managers(id) ON DELETE SET NULL,
  username      TEXT        NOT NULL,
  role_name     TEXT        NOT NULL DEFAULT '',
  action        TEXT        NOT NULL,
  method        TEXT        NOT NULL,
  path          TEXT        NOT NULL,
  query         TEXT        NOT NULL DEFAULT '',
  content_type  TEXT        NOT NULL DEFAULT '',
  -- Small bodies inline; a big one (a file) on disk under DATA_DIR/staff/.
  body          BYTEA,
  body_file     TEXT,
  tenant_id     INTEGER,
  status        TEXT        NOT NULL
                CHECK (status IN ('applied', 'pending', 'approved', 'rejected', 'failed')),
  -- Why it ran at once although the role says 'approve' ("protective").
  note          TEXT        NOT NULL DEFAULT '',
  decided_by    TEXT,
  decided_at    TIMESTAMPTZ,
  decision_note TEXT        NOT NULL DEFAULT '',
  result_status INTEGER,
  result_body   TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_staff_requests_pending ON staff_requests (created_at) WHERE status = 'pending';
CREATE INDEX idx_staff_requests_manager ON staff_requests (manager_id, id DESC);
CREATE INDEX idx_staff_requests_recent ON staff_requests (id DESC);
