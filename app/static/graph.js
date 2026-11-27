/* Graph canvas. Saved state and explicit local draft edits only. */
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const html = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; n.dir = "auto"; return n; };
  const svg = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k,v]) => n.setAttribute(k, v)); return n; };
  function canConnect(tasks, from, to) {
    const byId = new Map(tasks.map(t => [t.id, t]));
    if (!byId.has(from) || !byId.has(to) || from === to || byId.get(to).deps.includes(from)) return false;
    const stack = [from], seen = new Set();
    while (stack.length) { const id = stack.pop(); if (id === to) return false; if (seen.has(id)) continue; seen.add(id); stack.push(...(byId.get(id)?.deps || [])); }
    return true;
  }
  function render(host, tasks, onSelect, options = {}) {
    host.replaceChildren();
    if (!tasks.length) { host.append(html("p", "Add a step to begin your workflow.", "canvas-empty")); return; }
    const byId = new Map(tasks.map(t => [t.id, t])), depth = new Map(), visiting = new Set(); let invalid = false;
    function rank(id) {
      if (depth.has(id)) return depth.get(id);
      if (visiting.has(id) || !byId.has(id)) { invalid = true; return 0; }
      visiting.add(id); const deps = byId.get(id).deps || [];
      const r = deps.length ? Math.max(...deps.map(rank)) + 1 : 0;
      visiting.delete(id); depth.set(id, r); return r;
    }
    tasks.forEach(t => rank(t.id));
    const fallback = html("ul", undefined, "dag-fallback");
    tasks.forEach(t => fallback.append(html("li", `${t.id}: ${t.title} · depends on ${(t.deps || []).join(", ") || "no tasks"}`)));
    if (invalid) { host.append(html("p", "Invalid dependencies. Fix the plan before approval.", "form-error"), fallback); return; }
    const positions = new Map(); let row = 0;
    const cols = (host.closest(".main")?.clientWidth || 760) < 650 ? 1 : 2;
    for (let level = 0; level <= Math.max(...depth.values()); level++) {
      const lane = tasks.filter(t => depth.get(t.id) === level);
      lane.forEach((t, i) => positions.set(t.id, {x: 28 + (i % cols) * 298, y: 28 + (row + Math.floor(i / cols)) * 204}));
      row += Math.ceil(lane.length / cols);
    }
    const key = options.layoutKey ? `missiondeck-layout:${options.layoutKey}` : null;
    if (key) { try { const data = JSON.parse(localStorage.getItem(key) || "{}"); for (const [id,p] of Object.entries(data)) if (positions.has(id) && Number.isFinite(p.x) && Number.isFinite(p.y) && p.x >= 0 && p.y >= 0 && p.x < 6000 && p.y < 6000) positions.set(id,p); } catch (_) {} }
    const toolbar = html("div", undefined, "canvas-toolbar"), controls = html("div", undefined, "canvas-tools");
    toolbar.append(html("span", `${tasks.length} steps · ${options.editable ? "Editing draft" : "Saved workflow"}`, "canvas-summary"), controls);
    const live = html("p", options.editable ? "Connect steps: choose an output, then an input." : "Select a step to inspect its outputs and evidence.", "canvas-hint"); live.setAttribute("role", "status");
    const viewport = html("div", undefined, "flow-viewport"); viewport.tabIndex = 0; viewport.setAttribute("role", "region"); viewport.setAttribute("aria-label", "Workflow canvas");
    const stage = html("div", undefined, "flow-stage"), inner = html("div", undefined, "flow-inner");
    const lines = svg("svg", {class: "flow-lines", "aria-label": "Task dependencies"});
    const defs = svg("defs"), arrowId = `arrow-${host.id || "flow"}`;
    const marker = svg("marker", {id: arrowId, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto"});
    marker.append(svg("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "context-stroke"})); defs.append(marker); lines.append(defs); inner.append(lines);
    let zoom = 1, source = null; const cards = new Map();
    function paint() {
      const w = Math.max(...[...positions.values()].map(p => p.x)) + 290, h = Math.max(...[...positions.values()].map(p => p.y)) + 180;
      inner.style.width = `${w}px`; inner.style.height = `${h}px`; inner.style.transform = `scale(${zoom})`;
      stage.style.width = `${w * zoom}px`; stage.style.height = `${h * zoom}px`; lines.setAttribute("width", w); lines.setAttribute("height", h);
      lines.querySelectorAll(".flow-edge").forEach(p => p.remove());
      for (const t of tasks) {
        const to = positions.get(t.id); cards.get(t.id).style.transform = `translate(${to.x}px, ${to.y}px)`;
        for (const dep of t.deps || []) {
          const from = positions.get(dep), mid = (from.y + 150 + to.y) / 2;
          const p = svg("path", {class: "flow-edge", d: `M ${from.x + 126} ${from.y + 150} C ${from.x + 126} ${mid} ${to.x + 126} ${mid} ${to.x + 126} ${to.y}`, "marker-end": `url(#${arrowId})`});
          p.dataset.state = byId.get(dep).status || "planned"; lines.append(p);
        }
      }
      return w;
    }
    function persist() { if (key) { try { localStorage.setItem(key, JSON.stringify(Object.fromEntries(positions))); } catch (_) {} } }
    function connect(from, to) {
      if (!canConnect(tasks, from, to)) { live.textContent = "That link is duplicate, self-linked or creates a cycle."; return; }
      source = null; options.onConnect?.(from, to);
    }
    for (const task of tasks) {
      const card = html("article", undefined, "flow-node"); card.dataset.task = task.id; card.dataset.state = task.status || "planned";
      const department = options.departments?.find(d => d.id === task.department)?.name;
      const handle = html("button", `${task.id} · ${department || (task.role === "review" ? "Review" : "Work step")}`, "flow-drag"); handle.type = "button"; handle.setAttribute("aria-label", `Move ${task.id}. Use arrow keys or drag.`);
      handle.addEventListener("keydown", e => {
        const delta = {ArrowLeft: [-20,0], ArrowRight: [20,0], ArrowUp: [0,-20], ArrowDown: [0,20]}[e.key];
        if (!delta) return; e.preventDefault(); const p = positions.get(task.id);
        positions.set(task.id, {x: Math.max(0,Math.min(5900,p.x+delta[0])), y: Math.max(0,Math.min(5900,p.y+delta[1]))}); paint(); persist();
      });
      handle.addEventListener("pointerdown", e => {
        if (e.button !== 0) return; const x=e.clientX, y=e.clientY, p={...positions.get(task.id)}; handle.setPointerCapture(e.pointerId);
        const move = next => { positions.set(task.id,{x:Math.max(0,Math.min(5900,p.x+(next.clientX-x)/zoom)),y:Math.max(0,Math.min(5900,p.y+(next.clientY-y)/zoom))}); paint(); };
        const end = () => { handle.removeEventListener("pointermove",move); handle.removeEventListener("pointerup",end); handle.removeEventListener("pointercancel",end); persist(); };
        handle.addEventListener("pointermove",move); handle.addEventListener("pointerup",end); handle.addEventListener("pointercancel",end);
      });
      const select = html("button", undefined, "flow-node-select"); select.type = "button"; select.setAttribute("aria-label", `Inspect ${task.id}: ${task.title}`);
      let label = task.status || "Planned";
      if (task.status === "pending") { const waits=(task.deps||[]).filter(id=>byId.get(id)?.status!=="succeeded"); label=waits.length?`Waiting for ${waits.join(", ")}`:"Queued"; }
      select.append(html("strong",task.title||"Untitled step"),html("span",`${task.agent||"Choose an agent"} · ${task.model||"unknown"}`,"flow-agent"),html("span",label,"flow-state"));
      select.addEventListener("click",()=>onSelect?.(task.id)); card.append(handle,select);
      if (options.editable) {
        const input=html("button","+","flow-port flow-port--input"), output=html("button","+","flow-port flow-port--output"); input.type=output.type="button"; input.dataset.input=task.id;
        input.setAttribute("aria-label",`Connect to ${task.id}`); output.setAttribute("aria-label",`Connect from ${task.id}`);
        input.addEventListener("click",()=>{if(source)connect(source,task.id);else live.textContent="Choose the previous step's output first.";});
        output.addEventListener("click",()=>{source=task.id;live.textContent=`Choose the input of the step after ${task.id}.`;});
        output.addEventListener("pointerdown",e=>{source=task.id;output.setPointerCapture(e.pointerId);const up=next=>{const target=document.elementFromPoint(next.clientX,next.clientY)?.closest("[data-input]");if(target)connect(task.id,target.dataset.input);};output.addEventListener("pointerup",up,{once:true});});
        card.append(input,output);
      }
      cards.set(task.id,card); inner.append(card);
    }
    const zoomLabel=html("span","100%","zoom-label");
    function scale(z){zoom=Math.max(.7,Math.min(1.5,z));zoomLabel.textContent=`${Math.round(zoom*100)}%`;paint();}
    function tool(label,fn,aria){const b=html("button",label,"btn btn--ghost btn--small");b.type="button";if(aria)b.setAttribute("aria-label",aria);b.addEventListener("click",fn);controls.append(b);}
    tool("−",()=>scale(zoom-.1),"Zoom out workflow");controls.append(zoomLabel);tool("+",()=>scale(zoom+.1),"Zoom in workflow");
    tool("Fit",()=>scale((viewport.clientWidth-24)/paint()));
    tool("Arrange",()=>{if(key){try{localStorage.removeItem(key);}catch(_){}}render(host,tasks,onSelect,options);});
    stage.append(inner);viewport.append(stage);const accessible=html("details",undefined,"flow-accessible");accessible.append(html("summary","Task list and connections"),fallback);
    host.append(toolbar,viewport,live,accessible);paint();
  }
  window.MissionGraph={render,canConnect};
})();
