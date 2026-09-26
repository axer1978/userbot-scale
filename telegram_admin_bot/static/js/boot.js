"use strict";

/* ----------------------------------------------------------------- boot */

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
