/* MissionDeck: API-backed views. GETs observe; explicit actions advance missions. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = {view: "missions", mission: null, tab: "plan", token: null, busy: false,
    capabilities: null, settings: {worker_cap: 2, auto_refresh_seconds: 0}, timer: null,
    selected: null, focusReturn: null, generation: 0, stale: false};
  const titles = {missions: "Missions", composer: "New mission", mission: "Mission control", agents: "Agents", artifacts: "Artifacts", settings: "Settings"};
  const enc = encodeURIComponent;
  const path = (id, suffix = "") => `/api/missions/${enc(id)}${suffix}`;
  const text = value => value == null ? "unknown" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  const time = value => {
    const date = value ? new Date(value) : null;
    return date && Number.isFinite(date.getTime()) ? date.toLocaleString() : "unknown";
  };
  function el(tag, content, cls) {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = text(content);
    if (cls) node.className = cls;
    node.dir = "auto";
    return node;
  }
  function put(id, content) { $(id).textContent = text(content); $(id).dir = "auto"; }
  function message(id, content) { put(id, content || ""); $(id).hidden = !content; }
  function empty(id, content) { $(id).replaceChildren(el("p", content)); $(id).hidden = false; }
  function list(host, values) {
    host.replaceChildren(...(values?.length ? values : ["None reported"]).map(value => el("li", value)));
  }
  function status(value) {
    const name = /^[a-z_]+$/.test(value || "") ? value : "unknown";
    return el("span", value || "unknown", `status-label status-label--${name}`);
  }
  function button(label, fn, cls = "btn btn--ghost") {
    const node = el("button", label, cls); node.type = "button";
    node.addEventListener("click", fn); return node;
  }
  function block(host, title, value) {
    const section = el("section", undefined, "detail-section");
    section.append(el("h3", title));
    if (Array.isArray(value)) { const ul = el("ul"); list(ul, value); section.append(ul); }
    else section.append(el("p", value));
    host.append(section);
  }
  function evidence(host, items) {
    if (!items?.length) { host.append(el("p", "No evidence recorded.", "muted")); return; }
    for (const item of items) {
      const section = el("details", undefined, "evidence-item");
      section.append(el("summary", `${item.type || "Evidence"}${item.command ? ` · ${item.command}` : ""}${item.exit_code != null ? ` · exit ${item.exit_code}` : ""}${item.verdict ? ` · ${item.verdict}` : ""}`));
      section.append(el("pre", text(item), "preview-box")); host.append(section);
    }
  }
  async function request(url, method = "GET", body) {
    const headers = {Accept: "application/json"};
    if (method !== "GET") {
      if (!state.token) state.token = (await request("/api/session")).token;
      headers["X-CC-Token"] = state.token; headers["Content-Type"] = "application/json";
    }
    // Mutations can take several sequential native CLI calls. Do not abort or
    // replay them: a timeout is not proof that the server did not launch a worker.
    const response = await fetch(url, {method, headers, credentials: "same-origin", cache: "no-store",
      ...(body !== undefined ? {body: JSON.stringify(body)} : {})});
    let data;
    try { data = await response.json(); } catch (_) { throw new Error(`HTTP ${response.status}: invalid server response. Refresh saved state before repeating an action.`); }
    if (!response.ok || data.error) {
      const error = data.error;
      throw new Error(error ? `${error.code}: ${error.message}${error.details && Object.keys(error.details).length ? `\n${text(error.details)}` : ""}` : `HTTP ${response.status}: ${text(data.detail || "Request failed")}`);
    }
    return data;
  }
  function setBusy(value) {
    state.busy = value;
    $("app").setAttribute("aria-busy", String(value));
    for (const id of ["btn-create-mission", "btn-save-settings", "btn-refresh-view", "btn-new-mission", "btn-composer-cancel"]) $(id).disabled = value;
    document.querySelectorAll("[data-nav]").forEach(node => { node.disabled = value; });
    document.querySelectorAll("[data-mission-open], [data-retry]").forEach(node => { node.disabled = value; });
    if (state.mission) actionButtons();
  }
  async function work(fn, errorId = "global-message") {
    if (state.busy) return;
    message(errorId, ""); setBusy(true);
    try { await fn(); }
    catch (error) { message(errorId, error.message); state.stale = true; updateFreshness(); }
    finally { setBusy(false); }
  }
  function updateFreshness() {
    const badge = $("freshness-badge");
    badge.hidden = !state.stale;
    badge.textContent = "Read failed · saved snapshot";
  }
  function route(view) {
    state.view = view; state.generation++; state.stale = false;
    closeDetail(false); message("global-message", ""); updateFreshness();
    document.querySelectorAll("[data-view-panel]").forEach(node => { node.hidden = node.dataset.viewPanel !== view; });
    document.querySelectorAll("[data-nav]").forEach(node => {
      const active = node.dataset.nav === (view === "mission" || view === "composer" ? "missions" : view);
      node.classList.toggle("is-active", active);
      if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    });
    put("page-title", titles[view]);
    put("page-sub", view === "mission" ? "Saved mission state · continue explicitly to check Orca" : "Local workspace · real API data");
    $("btn-refresh-view").hidden = view === "composer";
  }
  async function health() {
    const data = await request("/api/health");
    const connected = data.orca?.available === true;
    const host = $("connection-status");
    host.replaceChildren(el("span", "", `status-dot status-dot--${connected ? "ok" : "danger"}`), el("span", connected ? `Orca ${data.orca.version || "version unknown"}` : "Orca unavailable", "status-text"));
    put("workspace-notice", `Workspace: ${data.workspace}. Workers write here. Scope fields constrain tasks within this workspace; they do not select another project.${connected ? "" : " Orca is unavailable; planning and execution require a running local Orca installation."}`);
  }
  async function capabilities() {
    state.capabilities = await request("/api/capabilities");
    return state.capabilities;
  }
  function setSettings(data) {
    state.settings = data;
    $("setting-worker-cap").value = data.worker_cap;
    $("setting-auto-refresh").value = data.auto_refresh_seconds;
    $("setting-redaction").checked = data.redaction_enabled;
    put("auto-refresh-badge", data.auto_refresh_seconds ? `Saved-state refresh ${data.auto_refresh_seconds}s` : "Auto-refresh off");
    $("auto-refresh-badge").hidden = false;
    // This badge is mission-local; do not invent a global active-worker count.
    put("worker-cap-badge", state.view === "mission" && state.mission ? `Mission workers ${state.mission.active_dispatches} · app cap ${data.worker_cap}` : `App worker cap ${data.worker_cap}`);
    clearInterval(state.timer);
    if (data.auto_refresh_seconds > 0) state.timer = setInterval(() => {
      if (!state.busy && !document.hidden && ["missions", "mission", "artifacts"].includes(state.view)) work(refreshView);
    }, data.auto_refresh_seconds * 1000);
  }
  async function showMissions() {
    const data = await request("/api/missions");
    const host = $("missions-list"); host.replaceChildren();
    $("missions-state").hidden = Boolean(data.missions.length);
    if (!data.missions.length) empty("missions-state", "No missions yet. Create a mission to define the goal, scope and acceptance criteria.");
    for (const mission of data.missions) {
      const row = button("", () => work(() => openMission(mission.id)), "mission-row");
      row.dataset.missionOpen = mission.id;
      row.append(el("span", mission.title, "mission-row-title"), status(mission.status),
        el("span", `${mission.coordinator} · plan ${mission.plan_status} · updated ${time(mission.updated_at)}`, "mission-row-meta"));
      const wrapper = el("div"); wrapper.setAttribute("role", "listitem"); wrapper.append(row); host.append(wrapper);
    }
  }
  async function composer() {
    route("composer"); $("composer-form").reset(); message("composer-error", "");
    const select = $("field-coordinator"); select.replaceChildren(el("option", "Loading agents…"));
    const data = await capabilities();
    const prompt = el("option", "Choose an eligible coordinator"); prompt.value = "";
    select.replaceChildren(prompt);
    for (const agent of data.agents) {
      const option = el("option", `${agent.name}${agent.eligible ? "" : ` — ${agent.reason}`}`);
      option.value = agent.id; option.disabled = !agent.eligible; select.append(option);
    }
    put("coordinator-hint", data.agents.some(a => a.eligible) ? "Creating saves a draft. Plan mission launches the planner separately." : "No eligible agents. Check Orca and the Agents view for billing and quota evidence.");
    $("field-title").focus();
  }
  async function openMission(id) {
    const mission = await request(path(id));
    state.mission = mission; state.selected = null; state.tab = "plan";
    route("mission"); renderMission();
  }
  function actionButtons() {
    const mission = state.mission, can = mission.can || {};
    for (const [id, key] of [["btn-plan", "plan"], ["btn-approve", "approve"], ["btn-start", "start"], ["btn-cancel", "cancel"], ["btn-mission-refresh", "refresh"]]) $(id).disabled = state.busy || !can[key];
    const repair = ["invalid", "failed"].includes(mission.plan_status) && mission.repair_count < 1;
    $("btn-repair").hidden = !repair; $("btn-repair").disabled = state.busy || !repair;
    put("btn-approve", `Approve plan v${mission.plan_version}`);
  }
  function renderMission() {
    const m = state.mission;
    put("mission-heading", m.title);
    put("mission-meta", `${m.status} · ${m.coordinator} · ${m.id} · run ${m.run_id || "not created"}`);
    // freshness from the current API is a serialization time, not an Orca check.
    put("mission-snapshot", `Saved state updated ${time(m.updated_at)}. Reading this page does not check Orca liveness. Workspace: ${m.workspace}`);
    put("worker-cap-badge", `Mission workers ${m.active_dispatches} · app cap ${state.settings.worker_cap}`);
    put("exec-cap-value", `${m.active_dispatches} in this mission · app cap ${state.settings.worker_cap}`);
    $("plan-errors").hidden = !m.plan_errors?.length;
    const errors = el("ul"); list(errors, m.plan_errors || []); $("plan-errors").replaceChildren(errors);
    actionButtons(); renderPlan(); renderExecution(); renderDelivery();
    $("mission-events").replaceChildren(...(m.events || []).map(event => el("li", `${time(event.ts)} · ${event.type} · ${event.message}`)));
    switchTab(state.tab);
    if (state.selected) {
      const task = allTasks().find(t => t.id === state.selected || t.plan_task_id === state.selected);
      if (task) renderDetail(task); else closeDetail(false);
    }
  }
  function allTasks() {
    const m = state.mission;
    const running = new Map((m.tasks || []).map(t => [t.plan_task_id, t]));
    return (m.plan?.tasks || m.tasks || []).map(task => ({...task, ...(running.get(task.id) || {})}));
  }
  function taskCard(task, execution = false) {
    const card = button("", () => openDetail(task), execution ? "worker-card" : "task-card");
    card.append(el("span", `${task.plan_task_id || task.id} · ${task.title}`, "task-card-title"), status(task.status || "pending"),
      el("span", `${task.agent} · model ${task.model || "unknown"} · ${task.role}`, "task-card-meta"));
    if (execution) {
      card.append(el("span", `Task: ${task.native_task_id || "not created"}\nDispatch: ${task.dispatch_id || "not started"}`, "mono"));
      if (task.started_at) card.append(el("span", `Started ${time(task.started_at)}${task.ended_at ? ` · ended ${time(task.ended_at)}` : " · end unknown"}`, "muted"));
      if (task.result_summary) card.append(el("span", task.result_summary, "task-card-meta"));
    } else card.append(el("span", `Dependencies: ${(task.deps || []).join(", ") || "none"}`, "task-card-meta"));
    return card;
  }
  function renderPlan() {
    const m = state.mission, plan = m.plan;
    $("plan-content").hidden = !plan; $("plan-empty").hidden = Boolean(plan);
    if (!plan) {
      empty("plan-empty", m.plan_status === "generating" ? `Planner dispatched: ${m.planner?.dispatch_id || "unknown"}. Use Reconcile & continue to check its result.` : "No plan yet. Plan mission launches a native Orca planner; review its result before approval.");
      return;
    }
    put("plan-summary", plan.mission_summary); put("plan-architecture", plan.architecture); put("plan-rationale", plan.selection_rationale);
    list($("plan-departments"), (plan.departments || []).map(dept => `${dept.id} · ${dept.name}`));
    $("plan-tasks").replaceChildren(...allTasks().map(t => taskCard(t)));
    window.MissionGraph.render($("plan-dag"), plan.tasks || [], id => {
      const task = allTasks().find(t => t.plan_task_id === id || t.id === id); if (task) openDetail(task);
    });
  }
  function renderExecution() {
    const m = state.mission, host = $("worker-cards"); host.replaceChildren();
    $("execution-state").hidden = Boolean(m.tasks?.length);
    if (m.planner?.dispatch_id && !m.tasks?.length) empty("execution-state", `Planner ${m.planner.dispatch_id} · ${m.plan_status}. Native state ${m.planner.state || "unknown"}. Reconcile explicitly to check Orca.`);
    else if (!m.tasks?.length) empty("execution-state", "Execution has not started. Review and approve a valid plan first.");
    for (const task of m.tasks || []) {
      const wrapper = el("div"); wrapper.setAttribute("role", "listitem"); wrapper.append(taskCard(task, true)); host.append(wrapper);
    }
  }
  function artifactLink(missionId, artifact) {
    const a = el("a", artifact.rel_path); a.href = path(missionId, `/artifacts/${enc(artifact.id)}`);
    a.target = "_blank"; a.rel = "noopener"; return a;
  }
  function renderDelivery() {
    const m = state.mission;
    $("delivery-content").hidden = false; $("delivery-state").hidden = true;
    const artifacts = $("delivery-artifacts"); artifacts.replaceChildren();
    if (!m.artifacts?.length) list(artifacts, ["No validated artifacts yet."]);
    for (const item of m.artifacts || []) {
      const li = el("li"); li.append(artifactLink(m.id, item), el("p", `${item.size} bytes · ${item.verified ? "verified" : "unverified"} · SHA-256 ${item.sha256}`, "mono")); artifacts.append(li);
    }
    const validations = $("delivery-validation"); validations.replaceChildren();
    evidence(validations, (m.tasks || []).flatMap(t => (t.evidence || []).filter(e => e.type === "validation")));
    const review = $("delivery-review"); review.replaceChildren();
    const reviewers = (m.tasks || []).filter(t => t.role === "review");
    if (!reviewers.length) review.append(el("p", "No review task recorded. Independent review is not established."));
    for (const task of reviewers) {
      review.append(el("p", `${task.title} · ${task.agent} · ${task.status}`));
      evidence(review, (task.evidence || []).filter(e => e.type.startsWith("review")));
    }
    const quality = $("delivery-quality"); quality.replaceChildren(status(m.quality?.status));
    for (const check of m.quality?.checks || []) quality.append(el("p", `${check.id}: ${check.status} · ${check.detail}`));
    list($("delivery-risks"), m.plan?.risks || ["No plan risks recorded. This is not a risk assessment."]);
    put("delivery-acceptance", m.status === "succeeded" ? "Mission service reports success. Inspect the recorded validation and review evidence." : `Acceptance pending · mission ${m.status}`);
    $("delivery-escalation").href = path(m.id, "/escalation");
  }
  function openDetail(task) {
    state.focusReturn = document.activeElement; state.selected = task.id;
    renderDetail(task); $("btn-close-detail").focus();
  }
  function renderDetail(task) {
    $("detail-panel").hidden = false; $("detail-panel").classList.add("is-open");
    document.querySelector(".workspace-body").classList.remove("detail-collapsed");
    $("drawer-backdrop").hidden = false; $("drawer-backdrop").classList.add("is-visible");
    put("detail-title", `${task.plan_task_id || task.id} · ${task.title}`);
    const host = $("detail-body"); host.replaceChildren();
    const planned = state.mission.plan?.tasks?.find(t => t.id === (task.plan_task_id || task.id)) || task;
    block(host, "Agent / model", `${task.agent} / ${task.model || "unknown"}`);
    block(host, "Status", task.status || "planned"); block(host, "Dependencies", task.deps || []);
    block(host, "Inputs", planned.inputs || []); block(host, "Write scope", task.write_scope || []);
    block(host, "Outputs", task.outputs || []); block(host, "Acceptance criteria", task.acceptance || []);
    block(host, "Validation commands", task.validation || []);
    block(host, "Native identifiers", `Task ${task.native_task_id || "unknown"}\nDispatch ${task.dispatch_id || "unknown"}`);
    evidence(host, task.evidence);
    block(host, "Next action", task.status === "running" ? "Use Reconcile & continue to check Orca and advance approved work." : task.result_summary || "Inspect the plan and recorded evidence.");
    if (["failed", "blocked"].includes(task.status) && task.attempt < 2 && !["unknown", "cancelled", "succeeded"].includes(state.mission.status)) {
      const retry = button("Retry task · launches worker", () => work(() => mutate(`/tasks/${enc(task.id)}/retry`, {}), "mission-action-error"), "btn btn--secondary");
      retry.dataset.retry = "true"; retry.disabled = state.busy; host.append(retry);
    }
    if (state.mission.can?.cancel) host.append(el("p", "Use Cancel in the mission toolbar to stop the mission's workers. Individual worker stop is unavailable.", "muted"));
  }
  function closeDetail(restore = true) {
    state.selected = null; $("detail-panel").hidden = true; $("detail-panel").classList.remove("is-open");
    document.querySelector(".workspace-body").classList.add("detail-collapsed");
    $("drawer-backdrop").hidden = true; $("drawer-backdrop").classList.remove("is-visible");
    if (restore && state.focusReturn?.isConnected) state.focusReturn.focus();
  }
  function switchTab(name) {
    state.tab = name;
    document.querySelectorAll("[data-tab]").forEach(node => {
      const active = node.dataset.tab === name;
      node.setAttribute("aria-selected", String(active)); node.tabIndex = active ? 0 : -1; node.classList.toggle("is-active", active);
      $(`panel-${node.dataset.tab}`).hidden = !active;
    });
  }
  async function showAgents() {
    const data = await capabilities(), host = $("agents-matrix"); host.replaceChildren();
    $("agents-state").hidden = Boolean(data.agents.length);
    if (!data.agents.length) empty("agents-state", "No agent capability records available.");
    for (const agent of data.agents) {
      const row = el("article", undefined, "agent-row"); row.setAttribute("role", "row");
      const name = el("div"); name.setAttribute("role", "cell");
      name.append(el("h3", agent.name, "agent-row-name"), status(agent.eligible ? "eligible" : "blocked"), el("p", agent.reason, "agent-row-meta"));
      const facts = el("dl", undefined, "agent-facts"); facts.setAttribute("role", "cell");
      for (const [label, value] of [["Installed", agent.installed ? "yes" : "no"], ["Billing", agent.billing], ["Quota", agent.quota], ["Model", agent.model], ["Evidence", agent.evidence]]) facts.append(el("dt", label), el("dd", value));
      row.append(name, facts); host.append(row);
    }
  }
  async function showArtifacts() {
    const {missions} = await request("/api/missions"), host = $("artifacts-index");
    const groups = await Promise.all(missions.map(async m => ({mission: m, ...(await request(path(m.id, "/artifacts")))})));
    host.replaceChildren(); let count = 0;
    for (const group of groups) {
      if (!group.artifacts.length) continue;
      const section = el("section", undefined, "artifact-group"); section.append(el("h3", group.mission.title));
      const ul = el("ul");
      for (const artifact of group.artifacts) {
        const li = el("li"); li.append(artifactLink(group.mission.id, artifact), el("span", ` · ${artifact.size} bytes · ${artifact.verified ? "verified" : "unverified"}`, "muted")); ul.append(li); count++;
      }
      section.append(ul); host.append(section);
    }
    $("artifacts-state").hidden = count > 0;
    if (!count) empty("artifacts-state", "No artifacts recorded. Completed workers must produce declared outputs and validation evidence.");
  }
  async function refreshView() {
    if (state.view === "missions") await showMissions();
    else if (state.view === "mission" && state.mission) { state.mission = await request(path(state.mission.id)); renderMission(); }
    else if (state.view === "agents") await showAgents();
    else if (state.view === "artifacts") await showArtifacts();
    else if (state.view === "settings") setSettings(await request("/api/settings"));
    state.stale = false; updateFreshness();
  }
  async function mutate(suffix, body) {
    state.mission = await request(path(state.mission.id, suffix), "POST", body);
    state.stale = false; updateFreshness(); renderMission();
  }
  document.querySelectorAll("[data-nav]").forEach(node => node.addEventListener("click", () => work(async () => {
    route(node.dataset.nav); await refreshView(); setSettings(state.settings);
  })));
  $("btn-new-mission").addEventListener("click", () => work(composer));
  $("btn-composer-cancel").addEventListener("click", () => work(async () => { route("missions"); await showMissions(); }));
  $("composer-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!event.currentTarget.reportValidity()) return;
    const body = Object.fromEntries(new FormData(event.currentTarget));
    work(async () => { const m = await request("/api/missions", "POST", body); await openMission(m.id); }, "composer-error");
  });
  $("btn-refresh-view").addEventListener("click", () => work(async () => { await health(); await refreshView(); }));
  for (const [id, suffix] of [["btn-plan", "/plan"], ["btn-repair", "/plan/repair"], ["btn-start", "/start"], ["btn-mission-refresh", "/refresh"]]) $(id).addEventListener("click", () => work(() => mutate(suffix, {}), "mission-action-error"));
  $("btn-approve").addEventListener("click", () => work(() => mutate("/approve", {plan_version: state.mission.plan_version}), "mission-action-error"));
  $("btn-cancel").addEventListener("click", () => {
    if (state.busy) return;
    if (window.confirm("Stop this mission's active workers and request cancellation?")) work(() => mutate("/cancel", {confirm: true}), "mission-action-error");
  });
  document.querySelectorAll("[data-tab]").forEach((node, index, tabs) => {
    node.addEventListener("click", () => switchTab(node.dataset.tab));
    node.addEventListener("keydown", event => {
      const offset = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!offset && !["Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + offset + tabs.length) % tabs.length;
      switchTab(tabs[next].dataset.tab); tabs[next].focus();
    });
  });
  $("btn-close-detail").addEventListener("click", () => closeDetail());
  $("drawer-backdrop").addEventListener("click", () => closeDetail());
  document.addEventListener("keydown", event => { if (event.key === "Escape") closeDetail(); });
  $("settings-form").addEventListener("submit", event => {
    event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
    work(async () => {
      const data = await request("/api/settings", "PUT", {worker_cap: Number($("setting-worker-cap").value), auto_refresh_seconds: Number($("setting-auto-refresh").value), redaction_enabled: $("setting-redaction").checked});
      setSettings(data); message("settings-message", "Settings saved.");
    }, "settings-message");
  });
  work(async () => {
    const results = await Promise.allSettled([health(), request("/api/settings").then(setSettings), showMissions()]);
    const failed = results.filter(result => result.status === "rejected");
    if (failed.length) throw new Error(failed.map(result => result.reason.message).join("\n"));
  });
})();
