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
    if (!["index.html", "app.js", "graph.js", "planner.js", "styles.css", "theme.css", "monitor.html", "monitor.css", "monitor.js"].includes(name)) { res.writeHead(404); res.end(); return; }
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
    quality_requirements: "Output checks pass", scope: "smoke/", constraints: "Declared files only",
    status, plan_status: status === "planning" ? "generating" : ["draft", "unknown"].includes(status) ? "none" : "valid",
    plan: ["draft", "planning", "unknown"].includes(status) ? null : plan,
    plan_version: 3, repair_count: 0, plan_errors: [], updated_at: "2026-11-27T12:00:00Z",
    tasks: [], artifacts: [], events: [], quality: {status: "pending", checks: []}, active_dispatches: 0,
    planner: {dispatch_id: status === "planning" ? "dispatch_planner" : null},
    can: {plan: status === "draft", approve: status === "awaiting_approval", start: status === "approved", cancel: status === "running", refresh: true, save_plan: ["draft", "awaiting_approval", "approved", "blocked"].includes(status)}, ...extra};
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
    if (p === "/api/agent-sessions") return send({sessions: [{agent:"cursor",session_suffix:"12345678",kind:"worker",state:"working",workspace:"smoke",repo:"test",assistant_message_present:false,updated_at_ms:1795787942334}], checked_at:"2026-11-27T12:00:00Z"});
    if (p === "/api/missions") {
      if (method === "POST") model.mission = mission("draft", {title: body.title});
      return send(method === "POST" ? model.mission : {missions: model.mission ? [model.mission] : []});
    }
    if (p === "/api/missions/mis_test/artifacts") return send({artifacts: model.mission.artifacts});
    if (p === "/api/missions/mis_test" && method === "GET") return send(model.failRead ? {error: {code: "read_failed", message: "Snapshot unavailable"}} : model.mission, model.failRead ? 503 : 200);
    if (method === "PUT" && p === "/api/missions/mis_test/plan") {
      assert.equal(req.headers()["x-cc-token"], "csrf-test");
      if(model.failSave)return send({error:{code:"version_conflict",message:"The plan changed"}},409);
      model.mission=mission("awaiting_approval",{plan:body.plan,plan_version:body.plan_version+1});return send(model.mission);
    }
    if (method === "POST" && p.startsWith("/api/missions/mis_test/")) {
      assert.equal(req.headers()["x-cc-token"], "csrf-test");
      const action = p.split("/").pop();
      if (action === "plan") model.mission = mission("planning");
      if (action === "refresh") model.mission = mission("awaiting_approval");
      if (action === "approve") { assert.equal(body.plan_version, model.mission.plan_version); model.mission = mission("approved", {plan_version:body.plan_version+1}); }
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
    await page.locator(".composer-advanced summary").click();
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
    await page.locator(".planning-notes summary").click();
    assert.match(await page.locator("#plan-architecture").innerText(), /<img/);
    assert.equal(await page.locator("#plan-architecture img").count(), 0);
    assert.equal(await page.locator("#plan-dag .flow-node-select").count(), 2);
    await page.locator("#plan-dag .flow-accessible summary").click();
    assert.match(await page.locator("#plan-dag .dag-fallback").innerText(), /T2.*T1/);
    await page.locator("#plan-dag .flow-node-select").first().press("Enter");
    assert.equal(await page.locator("#detail-panel").isVisible(), true);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#detail-panel").isVisible(), false);
    await page.locator("#btn-approve").click(); await env.idle();
    await page.locator("#btn-start").click(); await env.idle();
    await page.locator("#tab-execution").click();
    await page.locator(".execution-details summary").click();
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

async function openEditor(env) {
  await openMission(env);
  await env.page.locator("#btn-edit-plan").click(); await env.idle();
  assert.equal(await env.page.locator("#plan-editor").isVisible(), true);
}
async function fillStep(page,title,output) {
  await page.locator("#step-title").fill(title);
  await page.locator("#step-outputs").fill(output);
  await page.locator("#step-acceptance").fill("Output exists and is correct");
  await page.locator("#step-acceptance").press("Tab");
}
async function screenshot(page,name) {
  if(!process.env.MISSIONDECK_SCREENSHOT_DIR)return;
  fs.mkdirSync(process.env.MISSIONDECK_SCREENSHOT_DIR,{recursive:true});
  await page.screenshot({path:path.join(process.env.MISSIONDECK_SCREENSHOT_DIR,name),fullPage:true});
}
test("manual blocks connect, reject a cycle, undo, move, zoom and save without dispatch", async () => {
  const env=await setup(mission()),{page}=env;
  try {
    await openEditor(env);
    await page.locator("#editor-add").click(); await fillStep(page,"Write report","smoke/report.md");
    await page.locator("#editor-add").click(); await fillStep(page,"Check report","smoke/check.py");
    await page.getByRole("button",{name:"Connect from T1",exact:true}).click();
    await page.getByRole("button",{name:"Connect to T2",exact:true}).click();
    assert.equal(await page.locator("#editor-canvas .flow-edge").count(),1);
    await page.getByRole("button",{name:"Connect from T2",exact:true}).click();
    await page.getByRole("button",{name:"Connect to T1",exact:true}).click();
    assert.match(await page.locator("#editor-canvas .canvas-hint").innerText(),/cycle/);
    assert.equal(await page.locator("#editor-canvas .flow-edge").count(),1);
    await page.locator("#editor-undo").click();
    assert.equal(await page.locator("#editor-canvas .flow-edge").count(),0);
    await page.getByRole("button",{name:"Connect from T1",exact:true}).click();
    await page.getByRole("button",{name:"Connect to T2",exact:true}).click();
    const before=await page.locator('#editor-canvas [data-task="T1"]').getAttribute("style");
    await page.locator("#editor-canvas .flow-drag").first().press("ArrowRight");
    assert.notEqual(await page.locator('#editor-canvas [data-task="T1"]').getAttribute("style"),before);
    await page.getByRole("button",{name:"Zoom in workflow",exact:true}).click();
    assert.match(await page.locator("#editor-canvas .zoom-label").innerText(),/110/);
    await screenshot(page,"workflow-editor.png");
    await page.locator("#editor-save").click(); await env.idle();
    assert.equal(await page.locator("#plan-editor").isVisible(),false);
    const saved=env.calls.find(c=>c.method==="PUT"&&c.path.endsWith("/plan"));
    assert.deepEqual(saved.body.plan.tasks[1].deps,["T1"]);
    assert.deepEqual(saved.body.plan.tasks[0].outputs,["smoke/report.md"]);
    assert.equal(env.calls.some(c=>c.method==="POST"),false);
    assert.equal(await page.locator("#btn-approve").isEnabled(),true);
    assert.deepEqual(env.errors,[]);
  } finally {await env.context.close();}
});
test("editing an approved workflow returns it to approval and conflicts preserve edits", async () => {
  const env=await setup(mission("approved")),{page}=env;
  try {
    await openEditor(env); await page.locator("#editor-canvas .flow-node-select").first().click();
    await page.locator("#step-title").fill("Updated implementation");await page.locator("#step-title").press("Tab");
    env.model.failSave=true;
    await page.locator("#editor-save").click();await env.idle();
    assert.match(await page.locator("#editor-error").innerText(),/version_conflict/);
    assert.equal(await page.locator("#plan-editor").isVisible(),true);
    assert.equal(await page.locator("#step-title").inputValue(),"Updated implementation");
    env.model.failSave=false;
    await page.locator("#editor-save").click();await env.idle();
    assert.equal(await page.locator("#btn-start").isVisible(),false);
    assert.match(await page.locator("#btn-approve").innerText(),/v4/);
    assert.equal(env.calls.some(c=>c.method==="POST"),false);
  } finally {await env.context.close();}
});
test("unsaved workflow protects navigation until the user discards it", async () => {
  const env=await setup(mission()),{page}=env;
  try {
    await openEditor(env);await page.locator("#editor-add").click();
    page.once("dialog",dialog=>dialog.dismiss());
    await page.locator('[data-nav="agents"]').click();await env.idle();
    assert.equal(await page.locator("#plan-editor").isVisible(),true);
    page.once("dialog",dialog=>dialog.accept());
    await page.locator("#editor-cancel").click();
    assert.equal(await page.locator("#plan-editor").isVisible(),false);
    assert.equal(env.calls.some(c=>c.method==="PUT"||c.method==="POST"),false);
  } finally {await env.context.close();}
});
test("execution graph shows running work and the dependency that holds the next step", async () => {
  const tasks=plan.tasks.map((t,i)=>({...t,id:`tsk_${i+1}`,plan_task_id:t.id,status:i?"pending":"running",attempt:1,evidence:[]}));
  const env=await setup(mission("running",{tasks,active_dispatches:1})),{page}=env;
  try {
    await openMission(env);await page.locator("#tab-execution").click();
    assert.match(await page.locator("#execution-dag").innerText(),/Waiting for T1/);
    assert.equal(await page.locator('#execution-dag .flow-node[data-state="running"]').count(),1);
    assert.equal(await page.locator("#btn-edit-plan").isVisible(),false);
    await screenshot(page,"workflow-execution.png");
    assert.equal(env.calls.some(c=>c.method!=="GET"),false);
    assert.deepEqual(env.errors,[]);
  } finally {await env.context.close();}
});
test("shared dark canvas and readable contrast survive the browser light color scheme", async () => {
  const env=await setup(mission("awaiting_approval")),{page}=env;
  try {
    await page.emulateMedia({colorScheme:"light"});await openMission(env);
    const colors=await page.evaluate(()=>{
      const s=getComputedStyle(document.body),root=getComputedStyle(document.documentElement);
      return {body:s.backgroundColor,html:root.backgroundColor,text:s.color,muted:root.getPropertyValue("--text-muted"),panel:root.getPropertyValue("--bg-elevated"),primary:root.getPropertyValue("--button-primary")};
    });
    const rgb=value=>value.trim().startsWith("#")?value.trim().slice(1).match(/../g).map(x=>parseInt(x,16)):value.match(/[\d.]+/g).slice(0,3).map(Number);
    const luminance=value=>rgb(value).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
    const contrast=(a,b)=>(Math.max(luminance(a),luminance(b))+.05)/(Math.min(luminance(a),luminance(b))+.05);
    assert.equal(colors.body,colors.html);
    assert.ok(contrast(colors.text,colors.body)>=7);
    assert.ok(contrast(colors.muted,colors.panel)>=4.5);
    assert.ok(contrast("#ffffff",colors.primary)>=4.5);
    await screenshot(page,"workflow-plan.png");
    await page.goto(`${base}/monitor.html`);
    await page.waitForFunction(()=>document.getElementById("count").textContent==="1");
    const monitor=await page.evaluate(()=>({body:getComputedStyle(document.body).backgroundColor,html:getComputedStyle(document.documentElement).backgroundColor,table:getComputedStyle(document.querySelector("table")).backgroundColor}));
    assert.equal(monitor.body,colors.body);assert.equal(monitor.body,monitor.html);assert.notEqual(monitor.table,"rgba(0, 0, 0, 0)");
    assert.equal(await page.getByRole("link",{name:"Open missions",exact:true}).isVisible(),true);
    await screenshot(page,"agent-activity.png");
    assert.deepEqual(env.errors,[]);
  } finally {await env.context.close();}
});
