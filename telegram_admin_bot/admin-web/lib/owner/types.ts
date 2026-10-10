// What the client dashboard's API answers (owner_api.py, owner_review_api.py).

export type Health = { status: string; last_seen_at: string | null };

export type OwnerTenant = {
  id: number;
  name: string;
  timezone: string;
  /** Photos for this business are added through review. */
  requires_review: boolean;
  bot: { sending: boolean; off_reason: string };
  health: Health;
};

export type Me = {
  id: number;
  username: string;
  display_name: string;
  totp: boolean;
  tenants: OwnerTenant[];
};

export type Account = {
  username: string;
  display_name: string;
  status: string;
  review_reason: string;
  must_change_password: boolean;
  gate: string;
};

export type SignupOptions = { open: boolean; terms: { version: number; title: string } | null };

export type TermsVersion = {
  version: number;
  title: string;
  body: string;
  change_note: string;
  published_at: string;
};

export type OwnerTerms = { terms: TermsVersion | null; accepted: number | null; ok: boolean };

export type Booking = {
  number: number;
  state: string;
  starts_at: string;
  ends_at: string;
  customer_name?: string | null;
  service?: string | null;
  proposed_starts_at?: string | null;
};

export type Period = {
  since?: string;
  bookings: { booked: number; no_show_rate: number | null };
  messages: { received: number; sent_by_bot: number; sent_by_hand: number };
  unanswered: number;
};

export type Dashboard = {
  tenant: OwnerTenant;
  today: string;
  bookings_today: Booking[];
  bookings_upcoming: Booking[];
  summary: { week: Period; today: Period & { since: string }; unanswered_open: number };
  weeks: (Period & { since: string })[];
  unanswered_open: number;
};

export type Overview = {
  tenants: (OwnerTenant & { week: Period; today: Period; unanswered_open: number })[];
};

export type QueueItem = {
  id: number;
  chat_id: number;
  customer?: string | null;
  text?: string | null;
  reason: string;
  status: string;
  created_at: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
};

export type LivePhoto = { id: number; kind: string; description: string };

export type Submission = {
  id: number;
  status: string;
  kind?: string;
  source?: string;
  description?: string;
  created_at: string;
  replaces_item?: number | null;
  media_item?: number | null;
  review_reason?: string | null;
};

export type PhotosData = { live: LivePhoto[]; submissions: Submission[]; max_mb: number };

export type Challenge = { code: string; gesture: string; expires_at: string };

export type VerificationRow = {
  id: number;
  status: string;
  reason: string;
  requested_by: string;
  submitted_at: string | null;
  reviewed_at: string | null;
  review_reason: string | null;
  created_at: string;
  challenge?: Challenge;
};

export type Verification = {
  ok: boolean;
  latest: VerificationRow | null;
  max_mb: number;
  instructions: string[];
};

export type TotpSetup = { secret: string; uri: string };
