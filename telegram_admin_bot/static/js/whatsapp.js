"use strict";

/* ------------------------------------------------- adding a WhatsApp account */

// The number is linked as a device of the phone, like WhatsApp Web. The
// wa-gateway service does the linking; the panel follows it, and this polls
// the panel every 2 s for the latest QR code, pairing code or outcome.
const wa = { pairId: null, status: null, timer: null, lastQr: null };
const WA_FINISHED = ["paired", "failed", "cancelled", "expired"];
const WA_POLL_MS = 2000;

// Same byte encoding as the gateway's QR library (the string is ASCII anyway).
qrcode.stringToBytes = qrcode.stringToBytesFuncs["UTF-8"];

function waPairingInProgress() {
  return !!wa.pairId && !WA_FINISHED.includes(wa.status);
}

function showWaForm() {
  showLoginNotice("");
  showLoginStep("wa-form");
  setTimeout(() => ($("wa-phone").value ? $("wa-deepseek") : $("wa-label")).focus(), 0);
}

$("l-pick-whatsapp").addEventListener("click", showWaForm);
$("wa-back").addEventListener("click", () => { showLoginNotice(""); showLoginStep("l-channel"); });

$("wa-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = $("wa-form").querySelector('button[type="submit"]');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "…";
  showLoginNotice("");
  try {
    const pairing = await api("POST", "/api/wa/pair/start", {
      label: $("wa-label").value.trim(),
      phone: $("wa-phone").value.trim(),
      deepseek_api_key: $("wa-deepseek").value.trim(),
      method: $("wa-method-code").checked ? "code" : "qr",
    });
    $("wa-deepseek").value = "";
    wa.pairId = pairing.pair_id;
    wa.lastQr = null;
    applyWaPairing(pairing);
    startWaPolling();
  } catch (err) {
    showLoginNotice(err.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
});

function startWaPolling() {
  clearInterval(wa.timer);
  wa.timer = setInterval(pollWaPairing, WA_POLL_MS);
}

function stopWaPolling() {
  clearInterval(wa.timer);
  wa.timer = null;
}

async function pollWaPairing() {
  if (!wa.pairId) { stopWaPolling(); return; }
  const pairId = wa.pairId;
  try {
    const pairing = await api("GET", `/api/wa/pair/${encodeURIComponent(pairId)}`);
    if (pairId === wa.pairId) applyWaPairing(pairing);
  } catch (err) {
    if (err.status === 404 && pairId === wa.pairId) {
      applyWaPairing({ status: "expired", error: err.message });
    }
    // Anything else (a network blip): the next poll tries again.
  }
}

const WA_QR_HELP = "On the phone: WhatsApp → Settings (on Android: ⋮ menu) → Linked devices → " +
                   "Link a device, then point the camera at this code. It changes about every 20 seconds.";
const WA_CODE_HELP = "On the phone: WhatsApp → Settings (on Android: ⋮ menu) → Linked devices → " +
                     "Link a device → Link with phone number instead, then type this code.";

function applyWaPairing(p) {
  wa.status = p.status;
  showLoginStep("wa-pairing");
  const help = $("wa-pair-help");
  const qrBox = $("wa-qr-box");
  const code = $("wa-code");
  const finished = WA_FINISHED.includes(p.status);
  qrBox.hidden = p.status !== "qr";
  code.hidden = p.status !== "code";
  help.hidden = !(p.status === "qr" || p.status === "code");
  $("wa-pair-retry").hidden = !(p.status === "failed" || p.status === "expired");
  $("wa-pair-cancel").textContent = finished ? "Close" : "Cancel";

  if (p.status === "qr") {
    help.textContent = WA_QR_HELP;
    if (p.qr !== wa.lastQr) { drawQr(p.qr); wa.lastQr = p.qr; }
    $("wa-pair-status").textContent = "Waiting for the phone to scan…";
  } else if (p.status === "code") {
    help.textContent = WA_CODE_HELP;
    const c = String(p.code || "");
    code.textContent = c.length === 8 ? `${c.slice(0, 4)}-${c.slice(4)}` : c;
    $("wa-pair-status").textContent = "Waiting for the code to be typed on the phone…";
  } else if (p.status === "waiting") {
    $("wa-pair-status").textContent = p.method === "code"
      ? "Asking WhatsApp for a pairing code…" : "Getting a QR code from WhatsApp…";
  } else if (p.status === "paired") {
    onWaLinked(p);
    return;
  } else if (p.status === "cancelled") {
    stopWaPolling();
    showWaForm();
    return;
  } else {  // failed, expired
    stopWaPolling();
    $("wa-pair-status").textContent = "";
    showLoginNotice(p.error || "WhatsApp did not link the number.");
  }
}

async function onWaLinked(p) {
  stopWaPolling();
  wa.pairId = null;
  wa.lastQr = null;
  $("login").classList.remove("open");
  for (const id of ["wa-label", "wa-phone", "wa-deepseek"]) $(id).value = "";
  toast("WhatsApp linked — it starts within about 15 seconds. Replies wait for your approval " +
        "until you turn on auto-send in Settings.", "info");
  try { await fetchSessions(); } catch (_) {}
  if (p.session_id) await selectSession(p.session_id);
}

$("wa-pair-cancel").addEventListener("click", async () => {
  const pairId = wa.pairId;
  stopWaPolling();
  if (pairId && waPairingInProgress()) {
    try { await api("POST", `/api/wa/pair/${encodeURIComponent(pairId)}/cancel`); } catch (_) {}
  }
  wa.pairId = null;
  wa.status = null;
  $("login").classList.remove("open");
});

$("wa-pair-retry").addEventListener("click", () => {
  wa.pairId = null;
  wa.status = null;
  showWaForm();
});

// A QR code on a canvas: dark modules on white, with the 4-module quiet
// zone scanners need, whole pixels per module so it stays sharp.
function drawQr(text) {
  const qr = qrcode(0, "M");
  qr.addData(String(text || ""));
  qr.make();
  const count = qr.getModuleCount();
  const quiet = 4;
  const scale = Math.max(2, Math.floor(264 / (count + quiet * 2)));
  const size = (count + quiet * 2) * scale;
  const canvas = $("wa-qr");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = "#000";
  for (let r = 0; r < count; r += 1) {
    for (let c = 0; c < count; c += 1) {
      if (qr.isDark(r, c)) ctx.fillRect((c + quiet) * scale, (r + quiet) * scale, scale, scale);
    }
  }
}
