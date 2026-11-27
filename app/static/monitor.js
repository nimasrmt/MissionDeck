"use strict";
const byId = id => document.getElementById(id);
let busy = false;
let lastSuccess = null;
function cell(row, value, style) {
    const td = document.createElement("td");
    td.textContent = value;
    if (style) td.className = style;
    row.append(td);
}
function displayTime(value) {
    if (!Number.isFinite(value)) return "Unknown";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}
function render(data) {
    const rows = data.sessions;
    byId("count").textContent = String(rows.length);
    byId("working").textContent = String(rows.filter(x => x.state === "working").length);
    byId("silent").textContent = String(rows.filter(x => x.state === "working" && x.assistant_message_present === false).length);
    const fragment = document.createDocumentFragment();
    for (const item of rows) {
        const row = document.createElement("tr");
        cell(row, item.agent + " / " + (item.session_suffix || "unknown") + " (" + item.kind + ")");
        cell(row, item.state, item.state === "working" ? "working" : "");
        const home = item.workspace === "~" || item.repo === "~";
        cell(row, home ? "Home (~) — broad workspace" : item.workspace + " / " + item.repo, home ? "warning" : "");
        cell(row, item.assistant_message_present === true ? "Yes" : item.assistant_message_present === false ? "No" : "Unknown");
        cell(row, displayTime(item.updated_at_ms));
        fragment.append(row);
    }
    if (!rows.length) {
        const row = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 5; td.textContent = "Orca returned no sessions in this snapshot.";
        row.append(td); fragment.append(row);
    }
    byId("sessions").replaceChildren(fragment);
}
async function refresh() {
    if (busy) return;
    busy = true;
    const button = byId("refresh"), status = byId("status");
    button.disabled = true;
    status.className = "";
    status.textContent = "Reading Orca session metadata…";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 65000);
    try {
        const response = await fetch("/api/agent-sessions", {cache:"no-store", signal:controller.signal});
        if (!response.ok) throw new Error("Unavailable");
        const data = await response.json();
        if (!Array.isArray(data.sessions)) throw new Error("Invalid snapshot");
        render(data);
        lastSuccess = data.checked_at || "time unknown";
        status.textContent = "Snapshot: " + lastSuccess + (data.truncated || data.hosts_omitted ? " · Partial result" : "");
    } catch (_) {
        status.className = "stale";
        status.textContent = lastSuccess ? "Refresh failed. STALE snapshot retained: " + lastSuccess : "Orca unavailable or invalid response. No live data.";
    } finally {
        clearTimeout(timer); button.disabled = false; busy = false;
    }
}
byId("refresh").addEventListener("click", refresh);
refresh();
