"use strict";

/* ----------------------------------------------------------------- boot */

$("global-pause").addEventListener("click", async () => {
  const next = !(state.status && state.status.global_pause);
  try {
    applyControls(await sApi("POST", "/global-pause", { global_pause: next }));
  } catch (err) { toast(err.message); }
});
// Phones: the top-bar buttons live behind a menu; choosing one closes it.
$("menu-toggle").addEventListener("click", () => {
  const open = document.body.classList.toggle("menu-open");
  $("menu-toggle").setAttribute("aria-expanded", String(open));
});
$("hdr-actions").addEventListener("click", (ev) => {
  if (ev.target.closest("button")) document.body.classList.remove("menu-open");
});
$("off-chip").addEventListener("click", () => {
  if (can("view.safety")) openSafety("client", state.status && state.status.tenant_id);
});

(async function boot() {
  try {
    await startPanel();
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
