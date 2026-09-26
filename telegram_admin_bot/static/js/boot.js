"use strict";

/* ----------------------------------------------------------------- boot */

$("open-settings").addEventListener("click", () => {
  if (state.config) fillSettings(state.config);
  $("settings").classList.add("open");
});
$("close-settings").addEventListener("click", () => $("settings").classList.remove("open"));
$("settings").addEventListener("click", (ev) => {
  if (ev.target === $("settings")) $("settings").classList.remove("open");
});

$("settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const saved = await sApi("PUT", "/config", collectSettings());
    applyConfig(saved);
    fillSettings(saved);
    $("settings").classList.remove("open");
    toast("Settings saved.", "info");
  } catch (err) { toast(err.message); }
});

$("global-pause").addEventListener("click", async () => {
  const next = !(state.config && state.config.behavior.global_pause);
  try {
    await sApi("POST", "/global-pause", { global_pause: next });
  } catch (err) { toast(err.message); }
});

(async function boot() {
  try {
    const sessions = await fetchSessions();
    hideGate();
    await afterAuth(sessions);
  } catch (err) {
    if (err && err.status === 401) {
      showGate();
    } else {
      showGate("Could not reach the admin API: " + (err && err.message ? err.message : "unknown error"));
    }
  }
  setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send("ping");
  }, 25000);
})();
