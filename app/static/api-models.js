/* User-owned keys are write-only. Every inference test requires an explicit click. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const form = $("api-form"), dialog = $("api-dialog");
  const state = {connections: [], selected: null, busy: false, dirty: false};
  const request = (...args) => window.missiondeckAPI(...args);
  const field = name => form.elements.namedItem(name);
  const say = text => { $("api-model-message").textContent = text; };
  const element = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };

  function controls() {
    for (const n of dialog.querySelectorAll("input,select,button")) n.disabled = state.busy;
    $("test-api-model").disabled = state.busy || state.dirty || !state.selected;
    $("delete-api-model").hidden = !state.selected;
    $("use-api-model").hidden = !state.selected?.verified || state.dirty;
    $("save-api-model").textContent = state.busy ? "Working…" : "Save connection";
  }
  async function run(fn) {
    if (state.busy) return;
    state.busy = true; say(""); controls();
    try { await fn(); } catch (e) { say(e.message); }
    finally { state.busy = false; controls(); }
  }
  function choose(row) {
    state.selected = row || null; state.dirty = false; form.reset();
    for (const key of ["id", "name", "protocol", "base_url", "model", "context_budget", "output_budget"]) {
      if (row && row[key] != null) field(key).value = row[key];
    }
    field("api_key").value = "";
    $("api-key-note").textContent = row?.has_key ? "A key is saved. Leave blank to keep it; changing the endpoint requires a new key." : "Key is stored in a private local file. It is never returned by the settings API.";
    paint(); controls();
  }
  function paint() {
    const host = $("api-connections"); host.replaceChildren();
    for (const row of state.connections) {
      const card = element("button", "", `api-connection${row.id === state.selected?.id ? " selected" : ""}`);
      card.type = "button"; card.disabled = state.busy;
      card.append(element("b", row.name), element("small", row.model), element("span", row.verified ? "✓ Tested · enabled" : "○ Needs test", row.verified ? "api-enabled" : "api-pending"));
      card.onclick = () => { say(""); choose(row); }; host.append(card);
    }
    if (!state.connections.length) host.append(element("p", "Your models will appear here. Add the URL, model ID and key supplied by your provider.", "hint"));
  }
  function update(data, id) {
    state.connections = data.connections || [];
    choose(state.connections.find(r => r.id === id) || null);
    window.dispatchEvent(new Event("missiondeck-routes-changed"));
  }
  $("api-models").onclick = () => run(async () => {
    const data = await request("/api/api-models"); state.connections = data.connections || [];
    choose(state.connections[0]); dialog.showModal();
  });
  $("new-api-model").onclick = () => { say(""); choose(null); field("name").focus(); };
  $("close-api-models").onclick = () => { field("api_key").value = ""; dialog.close(); };
  dialog.addEventListener("close", () => { field("api_key").value = ""; });
  form.addEventListener("input", () => { state.dirty = true; controls(); });
  form.onsubmit = event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    const values = Object.fromEntries(new FormData(form)), id = values.id; delete values.id;
    values.context_budget = Number(values.context_budget); values.output_budget = Number(values.output_budget);
    run(async () => {
      try {
        const before = new Set(state.connections.map(r => r.id));
        const data = await request(id ? `/api/api-models/${encodeURIComponent(id)}` : "/api/api-models", id ? "PUT" : "POST", values);
        const selected = id || data.connections.find(r => !before.has(r.id))?.id;
        update(data, selected); say("Saved locally. No model request was sent. Test & enable when ready.");
      } finally { values.api_key = ""; field("api_key").value = ""; }
    });
  };
  $("test-api-model").onclick = () => run(async () => {
    const id = state.selected.id;
    try {
      const data = await request(`/api/api-models/${encodeURIComponent(id)}/test`, "POST", {});
      update(data, id); say("Tool-call test passed and OpenCode found the model. Choose it for a role in AI & agent library → My API models.");
    } catch (e) {
      try { update(await request("/api/api-models"), id); } catch {}
      throw e;
    }
  });
  $("use-api-model").onclick = () => run(async () => {
    await request("/api/api-models/selection", "PUT", {model: state.selected.route});
    window.dispatchEvent(new Event("missiondeck-routes-changed"));
    say("Selected for new AI roles. Existing roles keep their model. Your API's cost and quota remain unknown.");
  });
  $("delete-api-model").onclick = () => run(async () => {
    const data = await request(`/api/api-models/${encodeURIComponent(state.selected.id)}`, "DELETE");
    update(data); say("Connection and its local key files removed. Saved workflows using this route must choose another model.");
  });
})();
