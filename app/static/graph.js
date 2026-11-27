/* Dependency graph; all labels are text, including untrusted planner output. */
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  function svgElement(tag, attributes = {}, text) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function render(host, tasks, onSelect) {
    host.replaceChildren();
    if (!tasks.length) return;
    const byId = new Map(tasks.map(task => [task.id, task]));
    const depth = new Map(), visiting = new Set();
    let invalid = false;
    function rank(id) {
      if (depth.has(id)) return depth.get(id);
      if (visiting.has(id) || !byId.has(id)) { invalid = true; return 0; }
      visiting.add(id);
      const deps = byId.get(id).deps || [];
      const value = deps.length ? Math.max(...deps.map(rank)) + 1 : 0;
      visiting.delete(id);
      depth.set(id, value);
      return value;
    }
    tasks.forEach(task => rank(task.id));
    const list = document.createElement("ul");
    list.className = "dag-fallback";
    for (const task of tasks) {
      const item = document.createElement("li");
      item.dir = "auto";
      item.textContent = `${task.id}: ${task.title} · depends on ${(task.deps || []).join(", ") || "no tasks"}`;
      list.append(item);
    }
    if (invalid) {
      const warning = document.createElement("p");
      warning.textContent = "Graph unavailable: invalid dependency structure. Inspect validation errors.";
      host.append(warning, list);
      return;
    }
    // Top-down rows, at most four nodes per row, including independent tasks.
    const positions = new Map();
    let row = 0;
    for (let level = 0; level <= Math.max(...depth.values()); level++) {
      const lane = tasks.filter(task => depth.get(task.id) === level);
      lane.forEach((task, index) => positions.set(task.id, {x: 22 + (index % 4) * 220, y: 20 + (row + Math.floor(index / 4)) * 104}));
      row += Math.ceil(lane.length / 4);
    }
    const width = Math.max(...[...positions.values()].map(p => p.x)) + 222;
    const svg = svgElement("svg", {viewBox: `0 0 ${width} ${row * 104 + 12}`, class: "dag-svg", role: "img", "aria-label": "Task dependency graph. Full task and dependency list follows."});
    const defs = svgElement("defs");
    const marker = svgElement("marker", {id: "dag-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse"});
    marker.append(svgElement("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "#8b9bb4"}));
    defs.append(marker); svg.append(defs);
    for (const task of tasks) {
      const to = positions.get(task.id);
      for (const dep of task.deps || []) {
        const from = positions.get(dep);
        svg.append(svgElement("path", {class: "dag-edge", d: `M${from.x + 100},${from.y + 66} C${from.x + 100},${from.y + 85} ${to.x + 100},${to.y - 18} ${to.x + 100},${to.y}`}));
      }
    }
    for (const task of tasks) {
      const pos = positions.get(task.id);
      const group = svgElement("g", {transform: `translate(${pos.x} ${pos.y})`});
      group.append(svgElement("rect", {class: "dag-node-rect", width: "200", height: "66", rx: "6"}));
      group.append(svgElement("text", {class: "dag-node-label", x: "12", y: "24"}, `${task.id} · ${task.agent || "unknown"}`));
      group.append(svgElement("text", {class: "dag-node-label", x: "12", y: "46"}, String(task.title).slice(0, 25)));
      group.append(svgElement("title", {}, `${task.id}: ${task.title}`));
      if (onSelect) {
        group.setAttribute("role", "button"); group.setAttribute("tabindex", "0");
        group.setAttribute("aria-label", `Inspect ${task.id}: ${task.title}`);
        group.addEventListener("click", () => onSelect(task.id));
        group.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(task.id); }
        });
      }
      svg.append(group);
    }
    host.append(svg, list);
  }
  window.MissionGraph = {render};
})();
