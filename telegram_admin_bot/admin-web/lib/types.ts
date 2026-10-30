// What the panel's API returns, as far as this UI reads it. The server is
// the source of truth (panel.py and the *_api.py routers); fields the UI
// never touches are left out.

/* ---------------------------------------------------------------- sessions */

export type Channel = "telegram" | "whatsapp";

export type Session = {
  session_id: string;
  label?: string | null;
  channel?: Channel | null;
  running_here?: boolean;
  status?: { telegram_connected?: boolean } | null;
};

export type Hold = { kind: string; label: string; reason: string; created_at?: string; created_by?: string };

export type Status = {
  tenant_id?: number | null;
  channel?: Channel | null;
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
  // Telegram shows it once, then it is gone.
  view_once?: boolean;
  // Sent with the arrival instructions (and deleted with them); never by the AI.
  send_on_arrival?: boolean;
  // Sent with the first reply in every new chat.
  greeting?: boolean;
  // The girl she does threesomes with: sent when he asks about one, never as her.
  threesome?: boolean;
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
  extras?: string | null;
  notes?: string | null;
  cancel_reason?: string | null;
  proposed_starts_at?: string | null;
  proposed_by?: "owner" | "customer" | null;
  attendance_confirmed_at?: string | null;
  arrived_at?: string | null;
  arrival_photo_match?: boolean | null;
  // The taxi steps of an outcall (booking.outcall_taxi).
  ready_at?: string | null;
  taxi_link?: string | null;
  taxi_link_at?: string | null;
  in_taxi_at?: string | null;
  in_taxi_by?: "owner" | "admin" | "system" | null;
  // His photo of the place reached her (booking.outcall_proof).
  proof_at?: string | null;
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

// A folder of clients: a top-level industry (parent_id null) or a sub-folder
// of another folder, any levels deep. path: "escort › Tallinn".
export type TreeIndustry = { id: number; name: string; parent_id: number | null; path: string };
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
  // The client can only add to this section (e.g. boundaries), never replace it.
  append_only?: boolean;
};

export type TenantView = {
  tenant: { id: number; name: string; status: string; session_id?: string | null; industry_id: number };
  industry: { id: number; name: string; path: string; parent_id: number | null; template_version: number };
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
  industry: { id: number; name: string; path: string; parent_id: number | null; template_version: number };
  // The folders above it, the top-level industry first (none for that one).
  parents: { id: number; name: string }[];
  // text: what its clients get. In a sub-folder also what it inherits from
  // the folder above and its own change, if any (as a client's layer).
  sections: {
    key: string; heading: string; text: string;
    inherited?: string; override?: { mode: "override" | "append"; text: string } | null; append_only?: boolean;
  }[];
  config: ConfigView;
  versions: Version[];
  tenants: { id: number; name: string; prompt_pin_version?: number | null }[];
  folders: { id: number; name: string }[];
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
  /** ends: the key's last four characters ("" for a short key); null = no key. */
  deepseek_key?: { ends: string } | null;
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
  created_by?: string | null;
  sessions: number;
  tenant_ids: number[];
  // Sign-up at /owner/: "pending" until approved or rejected here.
  status?: "active" | "pending" | "rejected";
  company?: string | null;
  email?: string | null;
  phone?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  review_reason?: string | null;
  terms_accepted?: number | null;
};

export type TermsAcceptance = { version: number; accepted_at: string; ip?: string | null };

/* ------------------------------------------------------------- who is in */

export type PermissionLevel = "allow" | "approve";

/** GET /api/me: the admin, or a moderator and what their role allows. */
export type Me =
  | { admin: true; username: string }
  | { admin: false; username: string; display_name?: string; role?: string | null;
      permissions: Record<string, PermissionLevel> };

/* ---------------------------------------------------------------- whatsapp */

export type WaPairing = {
  pair_id?: string;
  status: "waiting" | "qr" | "code" | "paired" | "failed" | "cancelled" | "expired";
  method?: "qr" | "code";
  qr?: string | null;
  code?: string | null;
  error?: string | null;
  session_id?: string | null;
};

/* ------------------------------------------------------------------- terms */

export type TermsVersion = {
  version: number;
  title: string;
  body: string;
  published_at: string;
  published_by: string;
  requires_acceptance: boolean;
  change_note?: string | null;
  accepted_by: number;
};

export type TermsState = {
  signup: { enabled: boolean; open: boolean };
  current: TermsVersion | null;
  starter: { title: string; body: string };
  placeholder?: string;
  required_version?: number | null;
  outstanding: number;
  history: TermsVersion[];
};

/* ---------------------------------------------------------------- managers */

export type Manager = {
  id: number;
  username: string;
  display_name: string;
  disabled: boolean;
  must_change_password: boolean;
  totp: boolean;
  role_id: number | null;
  role_name?: string | null;
  last_login_at?: string | null;
  created_at?: string | null;
  created_by?: string | null;
  sessions: number;
};

/* ------------------------------------------------------------------- staff */

export type StaffRole = {
  id: number;
  name: string;
  description?: string | null;
  admin_panel: boolean;
  permissions: Record<string, PermissionLevel>;
  members: number;
};

export type StaffAction = { key: string; label: string; group: string; view?: boolean; protective?: boolean };

export type StaffRequest = {
  id: number;
  manager_id: number;
  username: string;
  role_name?: string | null;
  action: string;
  action_label: string;
  method: string;
  path: string;
  query?: string | null;
  body?: string | null;
  tenant_id?: number | null;
  tenant_name?: string | null;
  status: "applied" | "pending" | "approved" | "rejected" | "failed";
  note?: string | null;
  created_at: string;
  decided_at?: string | null;
  decided_by?: string | null;
  decision_note?: string | null;
  result_status?: number | null;
  result_body?: string | null;
};

/* ------------------------------------------------------------ verification */

export type ReviewSummary = {
  pending?: { verifications?: number; photos?: number };
  tenants?: {
    id: number;
    name: string;
    industry: string;
    held?: boolean;
    pending_photos?: number;
    owners?: { id: number; username: string; status?: string | null }[];
  }[];
};

export type Verification = {
  id: number;
  username: string;
  status: "requested" | "submitted" | "approved" | "rejected";
  display_name?: string | null;
  company?: string | null;
  email?: string | null;
  phone?: string | null;
  tenants?: { id: number; name: string }[];
  reason: string;
  requested_by?: string | null;
  submitted_at?: string | null;
  video_bytes?: number | null;
  reviewed_at?: string | null;
  review_reason?: string | null;
  has_video?: boolean;
  video_deleted_at?: string | null;
  challenge?: string | null;
  gesture?: string | null;
  challenge_at?: string | null;
};

export type ReviewPhoto = {
  id: number;
  status: "pending" | "approved" | "rejected" | "withdrawn";
  kind: "photo" | "video" | string;
  tenant_name: string;
  source?: string | null;
  username?: string | null;
  created_at: string;
  original_name?: string | null;
  bytes?: number | null;
  replaces_item?: number | null;
  session_id?: string | null;
  description?: string | null;
  review_reason?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
};

export type ReviewIndustry = { id: number; name: string; requires_review: boolean; tenants: number };

/* ---------------------------------------------------------------- finetune */

export type BusinessLayer = {
  overrides: Record<string, { mode: "override" | "append"; text: string }>;
  addendum: string;
};

export type FinetuneIndustry = {
  industry: { id: number; name: string; path: string; parent_id: number | null; template_version: number };
  // This folder's own template ("" if it has none).
  template: string;
  // Without one of its own, a sub-folder runs on the nearest folder's above
  // it: that template, and the folder's name ("" if it has its own or none).
  inherited_template: string;
  template_from: string;
  // The shipped starting point for this industry ("" if none).
  default_template: string;
  placeholders: string[];
  businesses_so_far: number;
  has_standard: boolean;
  screenshots_ready: boolean;
  transcripts_ready: boolean;
};

export type FinetuneRun = {
  id: number;
  tenant_id: number;
  industry_id: number;
  status: "running" | "done" | "failed" | "applied" | "discarded";
  model: string;
  // screenshots: the vision model read images; text: transcribed chats;
  // examples: real chats flagged from the conversations view; hand: the
  // chats a person typed replies in, picked by the server; rules: a fold of
  // the operator rules into the prompt (files are "rule-12", …).
  source: "screenshots" | "text" | "examples" | "hand" | "rules";
  files: string[];
  industry_version: number;
  error: string;
  applied: { industry_version?: number; client_version?: number } | null;
  created_at: string;
  finished_at?: string | null;
  applied_at?: string | null;
  // Only on a single run (GET /api/finetune/runs/{id}). A part is null when
  // the run does not touch it (a fold with no rules for it, or a block the
  // model got wrong): applying leaves that part as it is.
  result?: {
    business_layer: BusinessLayer | null;
    industry_sections: Record<string, string> | null;
    notes: string;
    errors: string[];
    // A fold only: the rule ids it had per part, and the ones its notes list
    // as FOLDED and SKIPPED. Applying merges the folded ones of the parts
    // applied; every other stays active.
    rules?: { industry: number[]; account: number[]; folded: number[]; skipped: number[] };
  } | null;
  raw_output?: string;
  stale?: string;
  current?: {
    industry_version: number;
    industry_sections: Record<string, string>;
    business_layer: BusinessLayer;
  };
  sections?: { key: string; heading: string; append_only: boolean }[];
  addendum_limit?: number;
};

// A real chat flagged for finetuning (finetune_api.py): the admin's
// judgement only; the chat is read from the conversation when a run starts.
export type ExampleVerdict = "good" | "bad" | "plain";
export type ClientKind = "" | "serious" | "time_waster" | "safety_risk" | "regular";
export type ChatOutcome = "" | "came" | "no_show" | "cancelled" | "never_booked" | "blocked";

export type FinetuneExample = {
  id: number;
  tenant_id: number;
  chat_id: number;
  // "07-good": how the run names it.
  name: string;
  upto_message_id: number;
  verdict: ExampleVerdict;
  client_kind: ClientKind;
  outcome: ChatOutcome;
  note: string;
  // message id → what was wrong / what it should have said
  corrections: Record<string, string>;
  last_run_id: number | null;
  created_at: string;
  updated_at: string;
  display_name: string;
  // Only on GET /api/finetune/examples/{id}: what the model reads.
  preview?: string;
};

// A chat a person typed replies in (finetune_api.py hand-chats), as a run
// from hand-typed chats would read it.
export type HandChat = {
  // "<tenant id>:<chat id>"
  key: string;
  tenant_id: number;
  account: string;
  chat_id: number;
  // "pam-03": how the run names it.
  name: string;
  display_name: string;
  // Replies typed by hand, written by the bot, and his messages.
  typed: number;
  assistant: number;
  client: number;
  last_at: string;
  chars: number;
  // In the default pick: the best that fit one run.
  picked: boolean;
  // What the model reads.
  text: string;
};

export type HandChats = {
  accounts: { tenant_id: number; name: string; chats: number; typed: number; picked: number }[];
  chats: HandChat[];
  limits: { chats: number; chars: number };
};

// stats.quality: one week of one account, in its timezone.
export type QualityWeek = {
  since: string;
  until: string;
  bookings: { booked: number; confirmed: number; completed: number; no_show: number; cancelled: number;
    no_show_rate: number | null };
  messages: { received: number; conversations: number; sent_by_bot: number; sent_by_hand: number;
    chats_by_hand: number };
  unanswered: number;
  replies_written: number;
  drafts: { approved: number; edited: number; rejected: number };
  takeovers: number;
  unanswered_by_reason: Record<string, number>;
};

export type Quality = {
  tenant_id: number;
  timezone: string;
  auto_send: boolean;
  takeover_hours: number;
  weeks: QualityWeek[];
  totals: { typed_by_hand: number; chats_by_hand: number; drafts_edited: number; chats_flagged: number };
};

// An operator rule (rules.py): one short sentence added to every reply's
// prompt while active, for the whole industry or one account, until a fold
// writes it into the prompt itself (merged).
export type RuleScope = "industry" | "account";
export type RuleStatus = "proposed" | "active" | "merged" | "rejected" | "retired";

export type PromptRule = {
  id: number;
  industry_id: number;
  // null: industry-wide.
  tenant_id: number | null;
  scope: RuleScope;
  section: string;
  text: string;
  // draft_edit: a draft an operator changed before approving it; correction:
  // a correction on a flagged chat; manual: typed by the admin; booker: an
  // instruction for every customer the booker wrote in her booking chat.
  source: "draft_edit" | "correction" | "manual" | "booker";
  source_ref: { tenant_id?: number; message_id?: number; example_id?: number };
  status: RuleStatus;
  // The prompt version tag that holds it, once merged ("b1/i1v3/c2").
  merged_into: string | null;
  // "admin", or "suggested" by the model.
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type RulesWaiting = { draft_edits: number; corrections: number };

// GET /api/finetune/rules: the rules that reach one account (its
// industry's and its own, in every status, oldest first).
export type RulesView = {
  tenant_id: number;
  industry: { id: number; name: string };
  rules: PromptRule[];
  // Active rules per scope, at most.
  cap: number;
  counts: Record<RuleScope, { active: number; proposed: number }>;
  // Edits and corrections no suggestion has read yet.
  waiting: RulesWaiting;
  // DEEPSEEK_PLATFORM_KEY is set: suggesting and folding need it.
  platform_key: boolean;
  sections: { key: string; heading: string }[];
  last_fold: FinetuneRun | null;
};

export type RuleSuggestion = { rules: PromptRule[]; read: number; waiting: RulesWaiting };

// GET /api/finetune/style-card (stylecard.py): how the business types,
// measured from the replies typed by hand, even while the card is off.
export type StyleCard = {
  tenant_id: number;
  enabled: boolean;
  scope: RuleScope;
  days: number;
  min_replies: number;
  // As the model would get it; "" below min_replies.
  note: string;
  stats: {
    messages: number;
    turns: number;
    chats: number;
    median_words: number;
    median_messages_per_turn: number;
    lower_start_share: number;
    no_end_punctuation_share: number;
    emoji_share: number;
    top_emojis: { emoji: string; count: number }[];
    openers: { text: string; chats: number }[];
  };
};

// Reply examples (examples.py, examples_api.py): real moments of the chats,
// masked, shown to the model as examples when config retrieval is on.
export type RetrievalSettings = {
  enabled: boolean;
  // At most this many examples a reply, at least this alike (0 to 1), in at
  // most this many characters.
  k: number;
  min_similarity: number;
  max_chars: number;
  // Also the industry's other accounts' examples without numbers.
  share_in_industry: boolean;
  copy_guard: boolean;
};

// hand: typed by a person; edited: a draft an operator changed before
// approving it; flagged: in a chat the admin flagged.
export type ReplyExampleSource = "hand" | "edited" | "flagged";
// A time waiting for the owner is never an example, so never a phase here.
export type ReplyExamplePhase = "before_time" | "booked" | "after";

export type ReplyExamplesStatus = {
  tenant_id: number;
  industry_id: number;
  retrieval: RetrievalSettings;
  // Some account of the industry has retrieval on, so the hourly round
  // keeps this account's examples up to date.
  in_hourly_round: boolean;
  counts: {
    total: number;
    active: number;
    with_figures: number;
    embedded: number;
    not_embedded: number;
    // Examples with wordings, and the wordings in all.
    worded: number;
    wordings: number;
    by_source: Record<ReplyExampleSource, number>;
    by_phase: Record<ReplyExamplePhase, number>;
    // The industry's other accounts' active examples without numbers.
    shared_in_industry: number;
  };
  embedder: { up: boolean; model: string | null };
  // DEEPSEEK_PLATFORM_KEY is set: wording needs it.
  wordings_ready: boolean;
  last_ingest_at: string | null;
};

export type ReplyExample = {
  id: number;
  tenant_id: number;
  chat_id: number;
  source: ReplyExampleSource;
  verdict: "good" | "plain";
  phase: ReplyExamplePhase;
  outcome: string;
  client_kind: string;
  has_figures: boolean;
  active: boolean;
  // What the business had just said, the client's message, her reply: masked.
  context_text: string;
  client_text: string;
  reply_text: string;
  embedded: boolean;
  // The client side reworded by the model.
  wordings: string[];
  created_at: string;
};

export type ReplyExamplePage = { total: number; items: ReplyExample[] };

export type ReplyExamplesRebuild = ReplyExamplesStatus & {
  // more: there are chats left to read; call again to go on.
  ingest: { chats: number; turns: number; added: number; removed: number; more: boolean;
    skipped: Record<string, number> };
  embedded: number;
};

export type ReplyExamplesWorded = ReplyExamplesStatus & {
  result: { examples: number; wordings: number; calls: number; error: string };
};

export type ReplyExampleMatch = {
  id: number;
  similarity: number;
  // The text that matched best, and whether it is a generated wording.
  matched: string;
  generated: boolean;
  client_text: string;
  reply_text: string;
  context_text: string;
  source: ReplyExampleSource;
  verdict: "good" | "plain";
  phase: ReplyExamplePhase;
  has_figures: boolean;
  // From this account, not another of the industry.
  own: boolean;
  tenant_id: number;
  account: string;
  chat_id: number;
  chat: string;
  // In the note the bot would get.
  shown: boolean;
};

// POST /api/finetune/reply-examples/try, whether retrieval is on or not.
export type ReplyExampleTry = {
  tenant_id: number;
  phase: ReplyExamplePhase | null;
  retrieval: RetrievalSettings;
  model: string;
  vectors: number;
  ms: number;
  // The best ten, best first.
  results: ReplyExampleMatch[];
  // As the model would get it; "" when nothing is shown.
  note: string;
  // The message names a time: only the owner answers, so nothing is shown.
  time_named: boolean;
};

export type ChatLine = { id: number; who: "client" | "business" | "event"; text: string; at: string };

export type ChatExampleView = {
  tenant_id: number;
  example: FinetuneExample | null;
  lines: ChatLine[];
  preview: string;
  // Messages written since it was flagged.
  newer: number;
};

/* ------------------------------------------------ messages to the booker */
// GET/PUT /api/tenants/{id}/booker-messages (admin) and
// /api/owner/booker-messages?tenant_id=N (owner): the language of the bot's
// messages to the person who answers booking requests, and the account's
// own wording for the main ones (booker_settings.py).

export type BookerLanguage = "en" | "ru" | "lv" | "es";

export type BookerBlank = { name: string; about: string };

export type BookerTemplate = {
  key: string;
  label: string;
  about: string;
  blanks: BookerBlank[];
  // The default text in every language.
  defaults: Record<BookerLanguage, string>;
  // Blanks added back automatically when the text leaves them out.
  kept: string[];
};

export type BookerPreview = {
  // Each message as the bot would send it, for a made-up booking.
  preview: Record<string, string>;
  // What is wrong with a text (that one then shows as its default).
  problems: Record<string, string[]>;
};

export type BookerMessagesView = BookerPreview & {
  language: BookerLanguage;
  // The account's own texts; a missing one is the default.
  messages: Record<string, string>;
  languages: { key: BookerLanguage; label: string }[];
  templates: BookerTemplate[];
  max_length: number;
  revision: number;
};

export type BookerMessagesBody = {
  language: BookerLanguage;
  messages: Record<string, string>;
  expected_revision?: number;
  reason?: string;
};
