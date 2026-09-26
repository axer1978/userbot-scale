"use strict";

/* ------------------------------------------------------------- settings */
// Two kinds of settings reach this page. The account's own (state.config,
// session_config on the server): the pause switch and per-contact styles.
// The tenant's (state.tenantConfig): everything about how the bot behaves,
// edited under Settings / Clients (platform.js).

function applyConfig(cfg) {
  state.config = cfg;
  const paused = cfg.behavior.global_pause;
  const button = $("global-pause");
  button.textContent = paused ? "Automation paused" : "Pause all";
  button.classList.toggle("on", paused);
}

function applyTenantConfig(cfg) {
  if (!cfg) return;
  state.tenantConfig = cfg;
  const bits = [cfg.auto_send ? "auto-send ON" : "approval required"];
  const quiet = cfg.quiet_hours;
  if (quiet && quiet.enabled) bits.push(`quiet ${quiet.start}–${quiet.end} ${cfg.timezone}`);
  $("mode").textContent = bits.join(" · ");
}
