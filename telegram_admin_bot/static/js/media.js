"use strict";

/* ------------------------------------------------------------ media */

function renderMedia() {
  const grid = $("m-grid");
  grid.textContent = "";
  if (!state.media.length) {
    grid.appendChild(el("div", "empty", "Nothing loaded yet."));
    return;
  }
  for (const item of state.media) {
    const card = el("div", "media-card");
    card.appendChild(mediaPreview(item, true));
    card.appendChild(el("div", "meta", `${item.kind} #${item.id} · ${item.file}`));

    const desc = el("textarea");
    desc.placeholder = "What is in it? e.g. me at the beach, blue bikini";
    desc.value = item.description;
    card.appendChild(desc);

    const actions = el("div", "actions");
    const save = el("button", "btn small primary", "Save");
    save.addEventListener("click", async () => {
      save.disabled = true;
      try { await sApi("PATCH", `/media/${item.id}`, { description: desc.value }); toast("Saved.", "info"); }
      catch (err) { toast(err.message); }
      save.disabled = false;
    });
    const send = el("button", "btn small", "Send to open chat");
    send.disabled = state.activeChatId === null;
    send.title = state.activeChatId === null ? "Open a conversation first" : "Send this file now, as yourself";
    send.addEventListener("click", async () => {
      send.disabled = true;
      try {
        await sApi("POST", `/conversations/${state.activeChatId}/send-media`, { media_id: item.id });
        toast("Sent.", "info");
      } catch (err) { toast(err.message); }
      send.disabled = false;
    });
    const del = el("button", "btn small warn", "Delete");
    del.addEventListener("click", async () => {
      if (!confirm(`Delete ${item.file}? The file is removed from the media folder.`)) return;
      try { await sApi("DELETE", `/media/${item.id}`); }
      catch (err) { toast(err.message); }
    });
    actions.appendChild(save);
    actions.appendChild(send);
    actions.appendChild(del);
    card.appendChild(actions);
    grid.appendChild(card);
  }
}

async function uploadFiles(files) {
  const progress = $("m-progress");
  let n = 0;
  for (const file of files) {
    n += 1;
    progress.textContent = `Uploading ${file.name} (${n}/${files.length})…`;
    try {
      const res = await fetch(`${sPath("/media/upload")}?name=${encodeURIComponent(file.name)}`, {
        method: "PUT", body: file, headers: { "Content-Type": "application/octet-stream" },
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        toast(`${file.name}: ${detail}`);
      }
    } catch (err) { toast(`${file.name}: ${err.message}`); }
  }
  progress.textContent = "";
}

function fillMediaRules() {
  const m = (state.config && state.config.media) || {};
  $("m-enabled").checked = m.enabled !== false;
  $("m-ask-video").checked = m.ask_before_video !== false;
  $("m-video-approve").checked = m.videos_need_approval !== false;
}

$("open-media").addEventListener("click", async () => {
  $("media").classList.add("open");
  fillMediaRules();
  renderMedia();
  try { state.media = await sApi("GET", "/media"); renderMedia(); }
  catch (err) { toast(err.message); }
});
$("m-close").addEventListener("click", () => $("media").classList.remove("open"));
$("media").addEventListener("click", (ev) => {
  if (ev.target === $("media")) $("media").classList.remove("open");
});
$("m-save").addEventListener("click", async () => {
  try {
    const cfg = JSON.parse(JSON.stringify(state.config));
    cfg.media = {
      enabled: $("m-enabled").checked,
      ask_before_video: $("m-ask-video").checked,
      videos_need_approval: $("m-video-approve").checked,
    };
    applyConfig(await sApi("PUT", "/config", cfg));
    toast("Media rules saved.", "info");
  } catch (err) { toast(err.message); }
});
$("m-files").addEventListener("change", async (ev) => {
  await uploadFiles(Array.from(ev.target.files || []));
  ev.target.value = "";
});
const drop = $("m-drop");
drop.addEventListener("dragover", (ev) => { ev.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", async (ev) => {
  ev.preventDefault();
  drop.classList.remove("over");
  await uploadFiles(Array.from(ev.dataTransfer.files || []));
});

