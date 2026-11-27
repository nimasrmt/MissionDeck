/* Isolated browser acceptance of the UI/API contract. No Orca or inference. */
const {test, before, after} = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");
let server, browser, base;
before(async () => {
  const root = path.resolve(__dirname, "../app/static");
  server = http.createServer((req, res) => {
    const name = req.url.replace(/^\/static\//, "").replace(/^\//, "");
    if (!["index.html", "app.js", "graph.js", "styles.css"].includes(name)) { res.writeHead(404); res.end(); return; }
    res.setHeader("Content-Type", name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(path.join(root, name)));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({headless: true});
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });
const plan = {
  mission_summary: "ساخت یک خروجی واقعی", architecture: '<img src=x onerror="alert(1)">',
  selection_rationale: "Included route", departments: [{id: "eng", name: "Engineering"}], risks: ["Needs live acceptance"],
  tasks: [{id: "T1", title: "Implement", role: "implementation", agent: "cursor", model: "unknown", deps: [], inputs: [], write_scope: ["result.md"], outputs: ["result.md"], acceptance: ["Verified"], validation: ["python check.py"]},
    {id: "T2", title: "Review", role: "review", agent: "grok", model: "unknown", deps: ["T1"], inputs: ["result.md"], write_scope: ["review.json"], outputs: ["review.json"], acceptance: ["PASS"], validation: []}]
};
function mission(status = "draft", extra = {}) {
  return {id: "mis_test", title: "آزمون مأموریت", objective: "Objective", coordinator: "cursor", workspace: "/project",
    status, plan_status: status === "planning" ? "generating" : ["draft", "unknown"].includes(status) ? "none" : "valid",
    plan: ["draft", "planning", "unknown"].includes(status) ? null : plan,
    plan_version: 3, repair_count: 0, plan_errors: [], updated_at: "2026-11-27T12:00:00Z",
    tasks: [], artifacts: [], events: [], quality: {status: "pending", checks: []}, active_dispatches: 0,
    planner: {dispatch_id: status === "planning" ? "dispatch_planner" : null},
    can: {plan: status === "draft", approve: status === "awaiting_approval", start: status === "approved", cancel: status === "running", refresh: true}, ...extra};
}
async function setup(initial = null, available = true) {
  const context = await browser.newContext({viewport: {width: 1366, height: 768}});
  const page = await context.newPage(); const errors = [], calls = [];
  const model = {mission: initial, failRead: false, settings: {worker_cap: 2, auto_refresh_seconds: 0, redaction_enabled: true}};
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", async route => {
    const req = route.request(), url = new URL(req.url()), method = req.method(), p = url.pathname;
    const body = req.postDataJSON(); calls.push({path: p, method, body, token: req.headers()["x-cc-token"]});
    const send = (data, status = 200) => route.fulfill({status, contentType: "application/json", body: JSON.stringify(data)});
    if (p === "/api/health") return send({orca: {available, version: null}, workspace: "/project"});
    if (p === "/api/session") return send({token: "csrf-test"});
    if (p === "/api/settings") {
      if (method === "PUT") model.settings = body;
      return send(model.settings);
    }
    if (p === "/api/capabilities") return send({agents: [
      {id: "cursor", name: "Cursor", eligible: true, installed: true, model: "unknown", quota: null, reason: "included", billing: "included_plan", evidence: ["planType=free"]},
      {id: "claude", name: "Claude", eligible: false, installed: true, reason: "paid route", billing: "api_key_metered"}
    ]});
    if (p === "/api/missions") {
      if (method === "POST") model.mission = mission("draft", {title: body.title});
      return send(method === "POST" ? model.mission : {missions: model.mission ? [model.mission] : []});
    }
    if (p === "/api/missions/mis_test/artifacts") return send({artifacts: model.mission.artifacts});
    if (p === "/api/missions/mis_test" && method === "GET") return send(model.failRead ? {error: {code: "read_failed", message: "Snapshot unavailable"}} : model.mission, model.failRead ? 503 : 200);
    if (method === "POST" && p.startsWith("/api/missions/mis_test/")) {
      assert.equal(req.headers()["x-cc-token"], "csrf-test");
      const action = p.split("/").pop();
      if (action === "plan") model.mission = mission("planning");
      if (action === "refresh") model.mission = mission("awaiting_approval");
      if (action === "approve") { assert.equal(body.plan_version, 3); model.mission = mission("approved"); }
      if (action === "repair") model.mission = mission("planning", {repair_count: 1});
      if (action === "start") model.mission = mission("running", {active_dispatches: 1, tasks: [{...plan.tasks[0], id: "tsk_1", plan_task_id: "T1", status: "running", native_task_id: "native_task_1", dispatch_id: "dispatch_1", attempt: 1, evidence: []}]});
      return send(model.mission);
    }
    errors.push(`Unexpected endpoint: ${method} ${p}`); return send({error: {code: "unexpected", message: p}}, 500);
  });
  await page.goto(`${base}/index.html`);
  const idle = () => page.waitForFunction(() => document.getElementById("app").getAttribute("aria-busy") === "false");
  await idle();
  return {page, context, calls, model, errors, idle};
}
async function openMission(env) { await env.page.locator("[data-mission-open]").click(); await env.idle(); }
test("disconnected empty UI, view navigation and responsive layout issue zero mutation requests", async () => {
  const env = await setup(null, false), {page} = env;
  try {
    assert.match(await page.locator("#connection-status").innerText(), /unavailable/);
    assert.match(await page.locator("#missions-state").innerText(), /No missions/);
    assert.equal(await page.locator("#view-composer").isVisible(), false);
    for (const name of ["agents", "artifacts", "settings", "missions"]) {
      await page.locator(`[data-nav="${name}"]`).click(); await env.idle();
      assert.equal(await page.locator(`#view-${name}`).isVisible(), true);
    }
    for (const width of [1366, 900, 390]) {
      await page.setViewportSize({width, height: 768});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    assert.equal(env.calls.some(c => c.method !== "GET"), false); assert.deepEqual(env.errors, []);
  } finally { await env.context.close(); }
});
test("Persian composer requires all fields, disables ineligible agent and only saves a draft", async () => {
  const env = await setup(), {page} = env;
  try {
    await page.locator("#btn-new-mission").click(); await env.idle();
    assert.equal(await page.locator('#field-coordinator option[value="claude"]').isDisabled(), true);
    await page.locator("#btn-create-mission").click(); await env.idle();
    assert.equal(env.calls.some(c => c.method === "POST"), false);
    for (const id of ["title", "objective", "quality", "scope", "constraints"]) await page.locator(`#field-${id}`).fill("آزمون فارسی");
    await page.locator("#field-coordinator").selectOption("cursor");
    await page.locator("#btn-create-mission").click(); await env.idle();
    assert.equal(await page.locator("#mission-heading").innerText(), "آزمون فارسی");
    assert.deepEqual(env.calls.filter(c => c.method === "POST").map(c => c.path), ["/api/missions"]);
    assert.deepEqual(env.errors, []);
  } finally { await env.context.close(); }
});
test("explicit plan, reconcile, approval version and execution; graph labels remain inert", async () => {
  const env = await setup(mission()), {page} = env;
  try {
    await openMission(env);
    await page.locator("#btn-plan").click(); await env.idle();
    assert.match(await page.locator("#plan-empty").innerText(), /dispatch_planner/);
    await page.locator("#btn-refresh-view").click(); await env.idle();
    assert.equal(env.calls.filter(c => c.path.endsWith("/refresh")).length, 0);
    await page.locator("#btn-mission-refresh").click(); await env.idle();
    assert.match(await page.locator("#plan-architecture").innerText(), /<img/);
    assert.equal(await page.locator("#plan-architecture img").count(), 0);
    assert.equal(await page.locator("#plan-dag svg [role=button]").count(), 2);
    assert.match(await page.locator(".dag-fallback").innerText(), /T2.*T1/);
    await page.locator("#plan-dag svg [role=button]").first().press("Enter");
    assert.equal(await page.locator("#detail-panel").isVisible(), true);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#detail-panel").isVisible(), false);
    await page.locator("#btn-approve").click(); await env.idle();
    await page.locator("#btn-start").click(); await env.idle();
    await page.locator("#tab-execution").click();
    assert.match(await page.locator("#worker-cards").innerText(), /native_task_1/);
    await page.locator("#tab-execution").press("ArrowRight");
    assert.equal(await page.locator("#panel-delivery").isVisible(), true);
    assert.equal(await page.locator("#delivery-escalation").getAttribute("href"), "/api/missions/mis_test/escalation");
    assert.deepEqual(env.calls.filter(c => c.method === "POST").map(c => c.path.split("/").pop()), ["plan", "refresh", "approve", "start"]);
    assert.deepEqual(env.errors, []);
  } finally { await env.context.close(); }
});
test("failed saved-state reads retain the visible snapshot and recover without a mutation", async () => {
  const env = await setup(mission("unknown")), {page} = env;
  try {
    await openMission(env); env.model.failRead = true;
    await page.locator("#btn-refresh-view").click(); await env.idle();
    assert.equal(await page.locator("#mission-heading").innerText(), "آزمون مأموریت");
    assert.equal(await page.locator("#freshness-badge").isVisible(), true);
    assert.equal(await page.locator("#btn-start").isEnabled(), false);
    env.model.failRead = false;
    await page.locator("#btn-refresh-view").click(); await env.idle();
    assert.equal(await page.locator("#freshness-badge").isVisible(), false);
    assert.equal(await page.locator("#global-message").isVisible(), false);
    assert.equal(env.calls.some(c => c.method !== "GET"), false);
  } finally { await env.context.close(); }
});
test("invalid plans expose errors and one explicit repair", async () => {
  const env = await setup(mission("blocked", {plan_status: "invalid", plan_errors: ["cycle"], plan: null}));
  try {
    await openMission(env);
    assert.match(await env.page.locator("#plan-errors").innerText(), /cycle/);
    assert.equal(await env.page.locator("#btn-approve").isEnabled(), false);
    await env.page.locator("#btn-repair").click(); await env.idle();
    assert.equal(await env.page.locator("#btn-repair").isVisible(), false);
    assert.equal(env.calls.filter(c => c.path.endsWith("/plan/repair")).length, 1);
  } finally { await env.context.close(); }
});
test("opt-in auto-refresh only reads snapshots, including after opening a mission", async () => {
  const env = await setup(mission("running"));
  try {
    await env.page.locator('[data-nav="settings"]').click(); await env.idle();
    await env.page.locator("#setting-auto-refresh").fill("1");
    await env.page.locator("#btn-save-settings").click(); await env.idle();
    await env.page.locator('[data-nav="missions"]').click(); await env.idle(); await openMission(env);
    const beforeCount = env.calls.filter(c => c.path === "/api/missions/mis_test").length;
    await env.page.waitForFunction(() => document.getElementById("mission-heading").textContent.length > 0);
    await new Promise(resolve => setTimeout(resolve, 1250));
    assert.ok(env.calls.filter(c => c.path === "/api/missions/mis_test").length > beforeCount);
    assert.equal(env.calls.some(c => c.method === "POST"), false);
    assert.deepEqual(env.errors, []);
  } finally { await env.context.close(); }
});
