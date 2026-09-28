"use strict";

/* ------------------------------------------------------------- settings */
// Two kinds of settings reach this page. The account's own (state.config,
// session_config on the server): per-contact styles. The tenant's
// (state.tenantConfig): everything about how the bot behaves, edited under
// Settings / Clients (platform.js). The pause switch is the tenant's manual
// soft-off hold (controls.py), shown from the status.

function applyConfig(cfg) {
  state.config = cfg;
}

// The kill switches for the selected client: "Pause all" is its manual
// hold; the chip says why it is off when it is, whatever the cause.
function applyControls(controls) {
  if (!controls) return;
  const status = state.status || (state.status = {});
  if (controls.holds) {
    status.holds = controls.holds;
    status.global_pause = controls.holds.some((h) => h.kind === "manual");
  }
  if ("off_reason" in controls) status.off_reason = controls.off_reason;
  const paused = !!status.global_pause;
  const button = $("global-pause");
  button.textContent = paused ? "Paused — resume" : "Pause all";
  button.classList.toggle("on", paused);
  const chip = $("off-chip");
  const reason = status.off_reason || "";
  chip.hidden = !reason;
  chip.textContent = reason ? "Sending off: " + reason : "";
  chip.title = reason ? reason + " — see Safety" : "";
}

function applyTenantConfig(cfg) {
  if (!cfg) return;
  state.tenantConfig = cfg;
  const bits = [cfg.auto_send ? "auto-send ON" : "approval required"];
  const quiet = cfg.quiet_hours;
  if (quiet && quiet.enabled) bits.push(`quiet ${quiet.start}–${quiet.end} ${cfg.timezone}`);
  $("mode").textContent = bits.join(" · ");
}
