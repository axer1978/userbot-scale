"use strict";

/* ------------------------------------------------------------- settings */

function fillSettings(cfg) {
  $("p-purpose").value = cfg.persona.purpose;
  $("p-tone").value = cfg.persona.tone;
  $("p-languages").value = cfg.persona.languages;
  $("p-boundaries").value = cfg.persona.boundaries;
  $("p-signature").value = cfg.persona.signature_style;

  $("t-min").value = cfg.timing.min_delay_seconds;
  $("t-max").value = cfg.timing.max_delay_seconds;
  $("t-hours-on").checked = cfg.timing.active_hours_enabled;
  $("t-start").value = cfg.timing.active_hours_start;
  $("t-end").value = cfg.timing.active_hours_end;
  $("t-tz").value = cfg.timing.timezone;

  $("b-auto").checked = cfg.behavior.auto_send;
  $("b-log").checked = cfg.behavior.log_all_messages;

  $("h-adaptive").checked = cfg.human.adaptive_style;
  $("h-typing").checked = cfg.human.typing_indicator;
  $("h-read").checked = cfg.human.mark_read;
  $("h-cps").value = cfg.human.typing_speed_cps;
  $("h-tmax").value = cfg.human.typing_max_seconds;

  $("pr-on").checked = cfg.presence.enabled;
  $("pr-on-min").value = cfg.presence.go_online_delay_min;
  $("pr-on-max").value = cfg.presence.go_online_delay_max;
  $("pr-off-min").value = cfg.presence.offline_delay_min;
  $("pr-off-max").value = cfg.presence.offline_delay_max;

  $("cl-on").checked = cfg.context_link.enabled;
  $("cl-auto").checked = cfg.context_link.auto_detect;
  $("cl-sources").value = cfg.context_link.max_sources;
  $("cl-history").value = cfg.context_link.history_limit;
  $("cl-refresh").value = cfg.context_link.refresh_after_messages;

  $("a-model").value = cfg.ai.model;
  $("a-tokens").value = cfg.ai.max_tokens;
  $("a-temp").value = cfg.ai.temperature;
  $("a-concurrency").value = cfg.ai.max_concurrent_requests;

  $("s-daily").value = cfg.safety.daily_send_limit;
  $("s-peers").value = cfg.safety.daily_peer_limit;
  $("s-flood").value = cfg.safety.max_flood_wait_seconds;
  $("s-halt").checked = cfg.safety.halt_on_peer_flood;
  $("s-known").checked = cfg.safety.known_contacts_only;

  $("bk-on").checked = cfg.booking.enabled;
  $("bk-provider").value = cfg.booking.provider;
  $("bk-duration").value = cfg.booking.default_duration_minutes;
  $("bk-scan").value = cfg.booking.scan_messages;
  $("bk-calendar").value = cfg.booking.google_calendar_id;
  $("bk-remind").value = cfg.booking.reminder_minutes_before;
  $("bk-arrival").value = cfg.booking.arrival_instructions;
}

function collectSettings() {
  return {
    persona: {
      purpose: $("p-purpose").value,
      tone: $("p-tone").value,
      languages: $("p-languages").value,
      boundaries: $("p-boundaries").value,
      signature_style: $("p-signature").value,
    },
    timing: {
      min_delay_seconds: Number($("t-min").value),
      max_delay_seconds: Number($("t-max").value),
      active_hours_enabled: $("t-hours-on").checked,
      active_hours_start: $("t-start").value,
      active_hours_end: $("t-end").value,
      timezone: $("t-tz").value,
    },
    behavior: {
      auto_send: $("b-auto").checked,
      log_all_messages: $("b-log").checked,
      // Preserved here so saving Settings can't silently clear the global pause.
      global_pause: state.config ? state.config.behavior.global_pause : false,
    },
    human: {
      adaptive_style: $("h-adaptive").checked,
      typing_indicator: $("h-typing").checked,
      mark_read: $("h-read").checked,
      typing_speed_cps: Number($("h-cps").value),
      typing_max_seconds: Number($("h-tmax").value),
    },
    presence: {
      enabled: $("pr-on").checked,
      go_online_delay_min: Number($("pr-on-min").value),
      go_online_delay_max: Number($("pr-on-max").value),
      offline_delay_min: Number($("pr-off-min").value),
      offline_delay_max: Number($("pr-off-max").value),
    },
    context_link: {
      enabled: $("cl-on").checked,
      auto_detect: $("cl-auto").checked,
      max_sources: Number($("cl-sources").value),
      history_limit: Number($("cl-history").value),
      refresh_after_messages: Number($("cl-refresh").value),
    },
    ai: {
      model: $("a-model").value,
      max_tokens: Number($("a-tokens").value),
      temperature: Number($("a-temp").value),
      max_concurrent_requests: Number($("a-concurrency").value),
    },
    safety: {
      daily_send_limit: Number($("s-daily").value),
      daily_peer_limit: Number($("s-peers").value),
      max_flood_wait_seconds: Number($("s-flood").value),
      halt_on_peer_flood: $("s-halt").checked,
      known_contacts_only: $("s-known").checked,
    },
    booking: {
      enabled: $("bk-on").checked,
      provider: $("bk-provider").value.trim(),
      default_duration_minutes: Number($("bk-duration").value),
      scan_messages: Number($("bk-scan").value),
      google_calendar_id: $("bk-calendar").value.trim(),
      reminder_minutes_before: Number($("bk-remind").value),
      arrival_instructions: $("bk-arrival").value,
    },
    // Outreach, per-contact styles and fine-tune samples are each edited in
    // their own panel; carry them through untouched so saving Settings can't
    // quietly reset them to defaults.
    outreach: state.config ? state.config.outreach : undefined,
    contacts: state.config ? state.config.contacts : {},
    finetune: state.config ? state.config.finetune : { writing_samples: "" },
    media: state.config ? state.config.media : undefined,
  };
}

function applyConfig(cfg) {
  state.config = cfg;
  const paused = cfg.behavior.global_pause;
  const button = $("global-pause");
  button.textContent = paused ? "Automation paused" : "Pause all";
  button.classList.toggle("on", paused);

  const bits = [];
  bits.push(cfg.behavior.auto_send ? "auto-send ON" : "approval required");
  if (cfg.timing.active_hours_enabled) {
    bits.push(`active ${cfg.timing.active_hours_start}–${cfg.timing.active_hours_end} ${cfg.timing.timezone}`);
  }
  $("mode").textContent = bits.join(" · ");
}

