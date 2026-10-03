// What the panel's API returns, as far as this UI reads it. The server is
// the source of truth (panel.py and the *_api.py routers); fields the UI
// never touches are left out.

/* ---------------------------------------------------------------- sessions */

export type Session = {
  session_id: string;
  label?: string | null;
  running_here?: boolean;
  status?: { telegram_connected?: boolean } | null;
};

export type Hold = { kind: string; label: string; reason: string; created_at?: string; created_by?: string };

export type Status = {
  tenant_id?: number | null;
  instance?: string;
  telegram_connected?: boolean;
  telegram_error?: string | null;
  me?: { name?: string } | null;
  persona_configured?: boolean;
  holds?: Hold[];
  global_pause?: boolean;
  off_reason?: string;
};

export type Controls = { holds?: Hold[]; off_reason?: string };

export type AuthStep = {
  step: "credentials" | "code" | "password" | "done";
  notice?: string | null;
  phone?: string | null;
  delivery?: string | null;
  code_length?: number | null;
  next_label?: string | null;
  resend_in?: number | null;
  session_id?: string | null;
};

/* ----------------------------------------------------------- conversations */

export type Conversation = {
  chat_id: number;
  display_name?: string | null;
  username?: string | null;
  is_bot?: boolean;
  automation_paused?: boolean;
  paused_reason?: string | null;
  human_takeover_until?: string | null;
  unread?: number;
  last_message_at?: string | null;
  last_message_preview?: string | null;
};

export type Message = {
  id: number | null;
  chat_id: number;
  direction: "in" | "out";
  status: string;
  text: string;
  created_at?: string;
  attachments?: number[];
};

export type ChatLink = {
  chat_id: number;
  source_id: number;
  source_name: string;
  origin?: "auto" | "manual" | string;
  reason?: string;
};

export type LinkSuggestion = Contact & { reason: string };

export type Contact = { chat_id: number; display_name: string; username?: string | null; is_bot?: boolean };

/* ------------------------------------------------------- per-account config */

export type ContactStyle = {
  persona_extra: string;
  style_notes: string;
  chat_samples: string;
  message_length: string;
  min_delay_seconds: number | null;
  max_delay_seconds: number | null;
  typing_speed_cps: number | null;
  typing_max_seconds: number | null;
  online_delay_min: number | null;
  online_delay_max: number | null;
  offline_delay_min: number | null;
  offline_delay_max: number | null;
};

export type SessionConfig = {
  outreach: { auto_send: boolean; min_gap_seconds: number; max_gap_seconds: number; daily_limit: number };
  contacts?: Record<string, ContactStyle>;
  [key: string]: unknown;
};

export type TenantConfig = {
  auto_send: boolean;
  timezone: string;
  quiet_hours?: { enabled: boolean; start: string; end: string };
  [key: string]: unknown;
};

export type MediaItem = {
  id: number;
  kind: "photo" | "video" | string;
  file: string;
  description: string;
  role?: string | null;
};

export type OutreachItem = {
  chat_id: number;
  display_name?: string;
  status: string;
  error?: string | null;
  message?: string | null;
  goal?: string;
};

/* ---------------------------------------------------------------- bookings */

export type Booking = {
  id: number;
  number: number;
  state: string;
  starts_at: string;
  ends_at: string;
  tz?: string;
  customer_name?: string | null;
  customer_username?: string | null;
  service?: string | null;
  notes?: string | null;
  cancel_reason?: string | null;
  proposed_starts_at?: string | null;
  proposed_by?: "owner" | "customer" | null;
  attendance_confirmed_at?: string | null;
  arrived_at?: string | null;
  arrival_photo_match?: boolean | null;
};

export type BookingEvent = {
  created_at: string;
  action: string;
  actor: string;
  from_state?: string | null;
  to_state: string;
  reason?: string | null;
};

export type BookingsWeek = {
  enabled: boolean;
  timezone: string;
  start: string;
  bookings: Booking[];
  awaiting: Booking[];
};

export type WaitlistEntry = {
  id: number;
  customer_name?: string | null;
  wanted_from: string;
  wanted_to: string;
  state: string;
};

export type AvailabilityRule = {
  weekday: number;
  start_time: string;
  end_time: string;
  slot_minutes: number;
  buffer_minutes: number;
};

export type AiUsage = {
  today: { tokens: number; eur: number };
  month: { tokens: number; eur: number };
  limits: { daily_tokens?: number | null; daily_spend_eur?: number | null;
            monthly_tokens?: number | null; monthly_spend_eur?: number | null };
  reached?: string | null;
};

/* ---------------------------------------------------------------- platform */

export type TreeIndustry = { id: number; name: string };
export type TreeTenant = { id: number; name: string; industry_id: number; status: string; session_id?: string | null };
export type PlatformTree = { base_version: number; industries: TreeIndustry[]; tenants: TreeTenant[] };

export type ConfigField = {
  path: string;
  kind: "bool" | "choice" | "json" | "longtext" | "list" | "map" | "int" | "float" | "str" | string;
  choices?: (string | number)[];
  value: unknown;
  inherited_value?: unknown;
  source: string;
};

export type ConfigView = {
  fields: ConfigField[];
  overrides: Record<string, unknown>;
  revision: number;
  effective?: Record<string, unknown>;
};

export type Version = { version: number; note?: string | null; created_by: string; created_at: string; content?: unknown };

export type PromptSection = {
  key: string;
  heading: string;
  inherited?: string;
  override?: { mode: "override" | "append"; text: string } | null;
};

export type TenantView = {
  tenant: { id: number; name: string; status: string; session_id?: string | null; industry_id: number };
  industry: { id: number; name: string; template_version: number };
  config: ConfigView;
  prompt: {
    sections: PromptSection[];
    addendum: string;
    addendum_limit: number;
    client_version: number;
    client_versions: Version[];
    industry_versions: Version[];
    pinned: number | null;
    version_tag: string;
    rendered: string;
  };
};

export type IndustryView = {
  industry: { id: number; name: string; template_version: number };
  sections: { key: string; heading: string; text: string }[];
  config: ConfigView;
  versions: Version[];
  tenants: { id: number; name: string; prompt_pin_version?: number | null }[];
};

export type BaseView = {
  current: { version: number; content: { rules: string } };
  versions: Version[];
};

export type ConfigProposal = {
  intent: string;
  valid: boolean;
  errors: { path: string; message: string }[];
  changes: { path: string; from: unknown; to: unknown }[];
  proposal: unknown;
  overrides: Record<string, unknown>;
  revision: number;
};

export type AuditEvent = {
  created_at: string;
  event: string;
  actor: string;
  reason?: string | null;
  payload?: Record<string, unknown> | null;
};

/* ------------------------------------------------------------------ safety */

export type Alert = {
  id: number;
  tenant_id: number | null;
  kind: string;
  severity: string;
  message: string;
  count: number;
  last_at: string;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
};

export type SafetySummary = {
  alerts?: { total: number; critical: number };
  global_stop?: { on: boolean; reason?: string; at?: string | null };
  scheduler?: { stale: boolean; at?: string | null };
};

export type Health = {
  status: string;
  status_since?: string | null;
  last_seen_at?: string | null;
  last_error?: string | null;
  last_error_at?: string | null;
  rate_limited_until?: string | null;
  logins?: { current?: boolean; device?: string; platform?: string; app?: string; country?: string; created?: string }[] | null;
  logins_checked_at?: string | null;
};

export type Billing = {
  status: "active" | "grace" | "suspended";
  next_due?: string | null;
  grace_until?: string | null;
  notice_sent_at?: string | null;
};

export type SafetyOverview = SafetySummary & {
  global_stop: { on: boolean; reason?: string; at?: string | null };
  scheduler: { stale: boolean; at?: string | null };
  tenants: {
    id: number;
    name: string;
    label?: string | null;
    session_id?: string | null;
    health: Health;
    holds: Hold[];
    billing: Billing;
    open_alerts: number;
  }[];
};

export type TenantControls = {
  tenant: { name: string; label?: string | null; session_id?: string | null; state?: string };
  off_reason?: string | null;
  holds: Hold[];
  billing: Billing;
  health: Health;
  proxy?: { type: string; host: string; port: number; username?: string | null } | null;
  alerts: Alert[];
};

/* -------------------------------------------------------------- unanswered */

export type UnansweredItem = {
  id: number;
  status: "open" | "reviewed" | "added_to_template";
  reason: string;
  created_at: string;
  tenant_name: string;
  customer?: string | null;
  chat_id: number;
  text?: string | null;
  detail?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
};

export type UnansweredList = {
  open: number;
  tenants: { id: number; name: string }[];
  items: UnansweredItem[];
};

/* ------------------------------------------------------------------ review */

export type ReviewCounts = { total: number; undecided: number; approved: number; edited: number; rejected: number };

export type ReviewBatch = {
  id: number;
  tenant_id: number;
  name: string;
  date_from: string;
  date_to: string;
  status: "open" | "done";
  counts: ReviewCounts;
  first_undecided: number | null;
};

export type ReviewItem = {
  id: number;
  chat_id: number;
  chat_name?: string | null;
  sent_at?: string | null;
  context?: { role: "user" | "assistant"; content: string }[];
  reply: string;
  decision?: "approve" | "reject" | "edit" | null;
  edited_text?: string | null;
};

/* -------------------------------------------------------------- onboarding */

export type OnboardingStatus = {
  tenant_id: number;
  name: string;
  industry?: string;
  industry_id?: number;
  session_id?: string | null;
  session_label?: string | null;
  session_state?: string | null;
  configured: boolean;
  next_step?: string | null;
  steps: Record<string, boolean>;
  staging: { enabled: boolean; test_chats: string[] };
};

/* ------------------------------------------------------------------ owners */

export type Owner = {
  id: number;
  username: string;
  display_name: string;
  disabled: boolean;
  must_change_password: boolean;
  totp: boolean;
  last_login_at?: string | null;
  created_at?: string | null;
  sessions: number;
  tenant_ids: number[];
};
