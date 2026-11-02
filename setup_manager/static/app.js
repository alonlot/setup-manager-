"use strict";

/* Setup Manager web UI. Plain JavaScript, no build step. */

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;

const state = {
  overview: null,
  usb: {},            // setup id -> devices
  usbJson: {},        // setup id -> last rendered JSON (skip renders when nothing changed)
  status: {},         // setup id -> status (info tab)
  busy: new Set(),    // keys of running actions
  menu: null,         // key of the open row menu
  terms: new Map(),   // setup id -> terminal session
  runResults: [],
  search: { q: "", results: null },
  filter: "",
  showHubs: false,
  pickedSetups: null,
};

const ICON = {
  pin: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5M9 3h6l-1 6 3 3v2H7v-2l3-3z"/></svg>',
  bolt: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
  lock: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  dots: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></svg>',
  ext: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 3h7v7M21 3l-9 9M19 14v6H4V5h6"/></svg>',
  usb: '<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v14M9 5l3-3 3 3M7 10v3l5 3 5-3V9M7 10h0M17 9h0"/><circle cx="12" cy="19" r="2.5"/><rect x="5.5" y="8" width="3" height="2.5"/><circle cx="17" cy="8" r="1.5"/></svg>',
};

/* ------------------------------------------------------------------ API */

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (_) { /* no body */ }
  if (r.status === 401) {
    showLogin();
    throw new Error("unauthorized");
  }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function toast(msg, kind = "ok", ms = 4500) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? ms * 2 : ms);
}

/* ------------------------------------------------------------------ formatting */

function fmtBytes(n) {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtUptime(s) {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 10) return "just now";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
function bar(label, percent, text) {
  const p = Math.max(0, Math.min(100, percent || 0));
  const cls = p > 90 ? "crit" : p > 75 ? "warn" : "";
  return `<div class="stat-row"><span class="label">${esc(label)}</span><div class="bar"><span class="${cls}" style="width:${p}%"></span></div><span class="val">${esc(text)}</span></div>`;
}
function speed(mbps) {
  if (!mbps) return "";
  const n = Number(mbps);
  return n >= 1000 ? `${n / 1000} Gbps` : `${n} Mbps`;
}

/* ------------------------------------------------------------------ routing */

function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const parts = h.split("/");
  if (parts[0] === "setup" && parts[1]) return { view: "setup", id: decodeURIComponent(parts[1]), tab: parts[2] || "usb" };
  if (parts[0] === "run") return { view: "run" };
  if (parts[0] === "search") return { view: "search", q: decodeURIComponent(parts.slice(1).join("/")) };
  if (parts[0] === "popout" && parts[1]) return { view: "popout", id: decodeURIComponent(parts[1]) };
  return { view: "overview" };
}

function setups() { return state.overview?.setups || []; }
function setupById(id) { return setups().find((s) => s.id === id); }

/* ------------------------------------------------------------------ sidebar & top bar */

function onlineDot(s) {
  if (s.online === true) return '<span class="dot green"></span>';
  if (s.online === false) return '<span class="dot red"></span>';
  return '<span class="dot grey"></span>';
}

function renderChrome() {
  const r = route();
  const list = setups();
  $("#side-count").textContent = list.length ? `· ${list.filter((s) => s.online).length}/${list.length} online` : "";
  $("#setup-list").innerHTML = list.length ? list.map((s) => `
    <a class="setup-item ${r.view === "setup" && r.id === s.id ? "active" : ""}" href="#/setup/${enc(s.id)}/${r.view === "setup" ? r.tab : "usb"}">
      <div class="row1">${onlineDot(s)}<span>${esc(s.name)}</span></div>
      <div class="row2"><span class="mono">${esc(s.host)}</span>${s.online ? `<span>${s.usb_count ?? 0} USB</span>` : ""}</div>
    </a>`).join("") : '<div class="empty-side">No setups yet. Use “Add setup” or “Scan network”.</div>';
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === r.view));
  const ports = state.overview?.local?.ports || [];
  $("#local-count").textContent = ports.length;
}

/* ------------------------------------------------------------------ USB rows */

function devState(d) {
  if (d.attached_here) return ["purple", "Attached here"];
  if (d.state === "in_use") return ["amber", "In use elsewhere"];
  if (d.state === "bound") return ["blue", "Shared"];
  if (d.state === "error") return ["red", "USB/IP error"];
  return ["grey", "Not shared"];
}

function busyBtn(key, cls, label, attrs, disabled = false, title = "") {
  const busy = state.busy.has(key);
  return `<button class="btn sm ${cls}" ${attrs} ${busy || disabled ? "disabled" : ""} title="${esc(title)}">${busy ? '<span class="spin"></span>' : ""}${esc(label)}</button>`;
}

function usbRow(sid, d, showSetup = false) {
  const key = `${sid}/${d.busid}`;
  const [color, label] = devState(d);
  const a = `data-setup="${esc(sid)}" data-busid="${esc(d.busid)}"`;
  let primary;
  if (d.attached_here) primary = busyBtn(key, "detach", "Detach", `data-act="detach" ${a}`, false, "Detach from this PC and stop sharing");
  else if (d.state === "in_use") primary = busyBtn(key, "take", "Take over", `data-act="take" ${a}`, false, "Another client uses it. Take it to this PC.");
  else primary = busyBtn(key, "attach", "Attach", `data-act="attach" ${a}`, d.protected && !d.bound,
    d.protected ? "Protected on the setup PC" : "Share on the setup and attach to this PC");

  const chips = [
    ...(d.ttys || []).map((t) => `<span class="chip tty">/dev/${esc(t)}</span>`),
    ...(d.net || []).map((n) => `<span class="chip net" title="network interface">${esc(n)}</span>`),
    ...[...new Set((d.interfaces || []).map((i) => i.driver).filter((x) => x && x !== "usbip-host"))]
      .map((drv) => `<span class="chip">${esc(drv)}</span>`),
  ].join("");

  const menuOpen = state.menu === key;
  const menu = menuOpen ? `
    <div class="menu" data-menu="${esc(key)}">
      ${d.bound
        ? `<button data-act="unbind" ${a}>Stop sharing (usbip unbind)</button>`
        : `<button data-act="bind" ${a}>Share only (usbip bind)</button>`}
      ${d.attached_here ? `<button data-act="detach-keep" ${a}>Detach, keep shared</button>` : ""}
      <div class="sep"></div>
      <button data-act="sticky" data-enabled="${!d.sticky}" ${a}>${ICON.pin} Keep attached here ${d.sticky ? '<span class="check-mark">✓</span>' : ""}</button>
      <button data-act="autobind" data-enabled="${!d.auto_bind}" ${a}>${ICON.bolt} Auto-share when plugged ${d.auto_bind ? '<span class="check-mark">✓</span>' : ""}</button>
      <div class="sep"></div>
      <button data-act="reset" ${a}>${ICON.refresh} USB reset (re-plug)</button>
      <button data-act="copy" data-text="${esc(d.busid)}">Copy busid</button>
      <button data-act="copy-attach" ${a}>Copy usbip attach command</button>
    </div>` : "";

  return `<tr>
    ${showSetup ? `<td class="nowrap"><a href="#/setup/${enc(sid)}/usb" class="dev-name">${esc(d.setup_name || sid)}</a></td>` : ""}
    <td><span class="pill ${color}" title="${esc(d.state_detail || "")}">${esc(label)}</span></td>
    <td>
      <div class="dev-name">${esc(d.name)}</div>
      <div class="dev-sub"><span class="mono">${esc(d.vidpid)}</span>${d.serial ? `<span>SN ${esc(d.serial)}</span>` : ""}${d.speed ? `<span>${speed(d.speed)}</span>` : ""}${d.class_name && d.class !== "00" ? `<span>${esc(d.class_name)}</span>` : ""}</div>
    </td>
    <td class="mono nowrap">${esc(d.busid)}</td>
    <td class="hide-sm"><div class="chips">${chips}</div></td>
    <td class="hide-sm"><span class="flags">
      <span class="flag sticky ${d.sticky ? "on" : ""}" title="${d.sticky ? "Keep attached: on" : "Keep attached: off"}">${ICON.pin}</span>
      <span class="flag auto ${d.auto_bind ? "on" : ""}" title="${d.auto_bind ? "Auto-share when plugged: on" : "Auto-share when plugged: off"}">${ICON.bolt}</span>
      <span class="flag lock ${d.protected ? "on" : ""}" title="${d.protected ? "Protected on the setup PC" : "Not protected"}">${ICON.lock}</span>
    </span></td>
    <td class="actions">${primary}
      <span class="menu-wrap"><button class="btn sm ghost icon" data-act="menu" data-key="${esc(key)}" title="More actions">${ICON.dots}</button>${menu}</span>
    </td>
  </tr>`;
}

function usbTable(sid, devices, showSetup = false) {
  if (!devices.length) return "";
  return `<div class="table-wrap"><table>
    <thead><tr>${showSetup ? "<th>Setup</th>" : ""}<th>State</th><th>Device</th><th>Bus ID</th><th class="hide-sm">Ports / drivers</th><th class="hide-sm">Flags</th><th></th></tr></thead>
    <tbody>${devices.map((d) => usbRow(d.setup || sid, d, showSetup)).join("")}</tbody>
  </table></div>`;
}

function placeMenus() {
  document.querySelectorAll(".menu").forEach((m) => {
    const rect = m.getBoundingClientRect();
    if (rect.bottom > window.innerHeight - 10) m.classList.add("up");
  });
}

/* ------------------------------------------------------------------ views */

function rescueTerminals() {
  const pool = $("#term-pool");
  for (const t of state.terms.values()) if (t.el.parentNode !== pool) pool.appendChild(t.el);
}

function render() {
  rescueTerminals();
  const r = route();
  document.body.classList.toggle("popout", r.view === "popout");
  renderChrome();
  const main = $("#main");
  if (r.view === "setup") return renderSetup(main, r);
  if (r.view === "run") return renderRunMany(main);
  if (r.view === "search") return renderSearch(main, r.q);
  if (r.view === "popout") return renderPopout(main, r.id);
  return renderOverview(main);
}

function renderOverview(main) {
  const ov = state.overview;
  if (!ov) { main.innerHTML = '<div class="muted">Loading…</div>'; return; }
  const list = ov.setups;
  const ports = ov.local.ports;
  const usbip = ov.local.usbip;
  const online = list.filter((s) => s.online).length;

  const cards = list.map((s) => {
    const mem = s.memory ? (100 * s.memory.used) / s.memory.total : null;
    return `<a class="card" href="#/setup/${enc(s.id)}/usb">
      <div class="title">${onlineDot(s)} ${esc(s.name)}</div>
      <div class="host">${esc(s.host)}:${esc(s.agent_port)}</div>
      ${s.online ? `
        ${bar("CPU", s.cpu_percent, s.cpu_percent != null ? `${s.cpu_percent}%` : "–")}
        ${bar("RAM", mem, s.memory ? fmtBytes(s.memory.used) : "–")}
        <div class="meta"><span>${esc(s.os || "")}</span><span>up ${fmtUptime(s.uptime)}</span></div>
        <div class="meta"><span>${s.usb_count ?? 0} USB · ${s.usb_bound ?? 0} shared · ${s.usb_in_use ?? 0} in use</span>
          ${s.usbip && !s.usbip.usbipd_running ? '<span class="pill amber">usbipd off</span>' : ""}</div>`
        : `<div class="err">${esc(s.error || "connecting…")}</div><div class="meta"><span>last seen ${fmtAgo(s.last_seen)}</span></div>`}
    </a>`;
  }).join("");

  const portRows = ports.map((p) => `<tr>
      <td class="mono">${esc(p.port)}</td>
      <td><div class="dev-name">${esc(p.name || `${p.vid}:${p.pid}`)}</div><div class="dev-sub mono">${esc(p.vid)}:${esc(p.pid)} · ${esc(p.speed)}</div></td>
      <td>${p.setup ? `<a href="#/setup/${enc(p.setup)}/usb">${esc(p.setup_name)}</a>` : `<span class="muted">${esc(p.host)}</span>`}</td>
      <td class="mono">${esc(p.busid)}</td>
      <td class="actions">${p.setup
        ? busyBtn(`${p.setup}/${p.busid}`, "detach", "Detach", `data-act="detach" data-setup="${esc(p.setup)}" data-busid="${esc(p.busid)}"`)
        : busyBtn(`port/${p.port}`, "detach", "Detach", `data-act="local-detach" data-port="${esc(p.port)}"`)}</td>
    </tr>`).join("");

  const sticky = ov.sticky || [];
  const stickyRows = sticky.map((x) => `<tr>
      <td>${x.ok === false ? '<span class="pill red">retrying</span>' : x.ok ? '<span class="pill green">attached</span>' : '<span class="pill grey">waiting</span>'}</td>
      <td>${esc(x.name || x.vidpid || "")}</td>
      <td><a href="#/setup/${enc(x.setup)}/usb">${esc(setupById(x.setup)?.name || x.setup)}</a></td>
      <td class="mono">${esc(x.busid)}</td>
      <td class="small muted">${esc(x.error || "")}</td>
      <td class="actions"><button class="btn sm" data-act="sticky" data-enabled="false" data-setup="${esc(x.setup)}" data-busid="${esc(x.busid)}">Turn off</button></td>
    </tr>`).join("");

  main.innerHTML = `
    <div class="page-head">
      <div><h1>Overview</h1>
        <div class="sub"><span>${list.length} setups</span><span>${online} online</span><span>${ports.length} ${ports.length === 1 ? "device" : "devices"} attached to this PC</span></div>
      </div>
    </div>
    ${usbip && (!usbip.found || usbip.error) ? `<div class="banner red"><div class="grow"><b>Local usbip client problem.</b> Attach and detach will not work on this PC.
        <pre>${esc(usbip.error || `'${usbip.command}' not found. Install usbip (Linux: package usbip or linux-tools) or usbip-win2 (Windows).`)}</pre></div></div>` : ""}
    ${list.length ? `<div class="grid">${cards}</div>` : `
      <div class="empty"><h3>No setups yet</h3>
        <p>Install the agent on each setup PC, then add it here (or use “Scan network”).</p>
        <pre>setup-manager deploy user@setup-pc --name bench-1</pre>
        <p class="small">or on the setup PC: <code>sudo ./install.sh --name bench-1</code> (files: <code>setup-manager agent-files</code>)</p>
        <p><button class="btn primary" data-act="add-setup">+ Add setup</button> <button class="btn" data-act="discover">Scan network</button></p>
      </div>`}
    <h2>Attached to this PC</h2>
    ${ports.length ? `<div class="table-wrap"><table><thead><tr><th>Port</th><th>Device</th><th>From</th><th>Bus ID</th><th></th></tr></thead><tbody>${portRows}</tbody></table></div>`
      : '<div class="empty small">No USB/IP devices are attached to this PC.</div>'}
    ${sticky.length ? `<h2>${ICON.pin} Keep attached</h2><div class="table-wrap"><table><thead><tr><th>State</th><th>Device</th><th>Setup</th><th>Bus ID</th><th>Last error</th><th></th></tr></thead><tbody>${stickyRows}</tbody></table></div>` : ""}
  `;
}

function setupHead(s) {
  const st = state.status[s.id];
  return `
    <div class="page-head">
      <div>
        <h1>${onlineDot(s)} ${esc(s.name)}
          ${s.online ? '<span class="pill green">online</span>' : s.online === false ? '<span class="pill red">offline</span>' : '<span class="pill grey">…</span>'}</h1>
        <div class="sub">
          <span class="mono">${esc(s.host)}:${esc(s.agent_port)}</span>
          ${s.os ? `<span>${esc(s.os)}</span>` : ""}
          ${s.uptime != null ? `<span>up ${fmtUptime(s.uptime)}</span>` : ""}
          ${s.cpu_percent != null ? `<span>CPU ${s.cpu_percent}%</span>` : ""}
          ${s.load ? `<span>load ${s.load.map((x) => x.toFixed(2)).join(" ")}</span>` : ""}
          ${s.version ? `<span>agent v${esc(s.version)}</span>` : ""}
          ${st?.run_as ? `<span>shell user: ${esc(st.run_as)}</span>` : ""}
        </div>
        ${s.notes ? `<div class="sub">${esc(s.notes)}</div>` : ""}
      </div>
      <div class="actions">
        <button class="btn" data-act="refresh" data-setup="${esc(s.id)}">${ICON.refresh} Refresh</button>
        <button class="btn" data-act="edit-setup" data-setup="${esc(s.id)}">Edit</button>
        <button class="btn danger" data-act="remove-setup" data-setup="${esc(s.id)}">Remove</button>
      </div>
    </div>
    ${s.online === false ? `<div class="banner red"><div class="grow"><b>Cannot reach the agent.</b> ${esc(s.error || "")}</div></div>` : ""}
    ${s.online && s.usbip && !s.usbip.usbip_installed ? `<div class="banner"><div class="grow"><b>usbip is not installed on this setup.</b> Install it (Debian: <code>usbip</code>, Ubuntu: <code>linux-tools-generic</code>), or re-run the installer with <code>--install-deps</code>.</div></div>` : ""}
    ${s.online && s.usbip && s.usbip.usbip_installed && !s.usbip.usbipd_running ? `<div class="banner"><div class="grow"><b>usbipd is not running on this setup.</b> Clients cannot attach devices.</div>
        ${busyBtn(`usbipd/${s.id}`, "", "Start usbipd", `data-act="start-usbipd" data-setup="${esc(s.id)}"`)}</div>` : ""}
  `;
}

function renderSetup(main, r) {
  const s = setupById(r.id);
  if (!s) {
    main.innerHTML = state.overview ? `<div class="empty"><h3>Unknown setup “${esc(r.id)}”</h3><a class="btn" href="#/">Back to overview</a></div>` : '<div class="muted">Loading…</div>';
    return;
  }
  const tabs = [["usb", "USB devices", s.usb_count], ["terminal", "Terminal"], ["run", "Run"], ["info", "Info"], ["log", "Agent log"]];
  main.innerHTML = `
    <div id="setup-head">${setupHead(s)}</div>
    <div class="tabs">${tabs.map(([k, l, c]) => `<a class="tab ${r.tab === k ? "active" : ""}" href="#/setup/${enc(s.id)}/${k}">${esc(l)}${c != null ? ` <span class="count">${c}</span>` : ""}</a>`).join("")}</div>
    <div id="tab-body"></div>`;
  const body = $("#tab-body");
  if (r.tab === "usb") renderUsbTab(body, s);
  else if (r.tab === "terminal") renderTerminalTab(body, s);
  else if (r.tab === "run") renderRunPanel(body, [s.id]);
  else if (r.tab === "info") renderInfoTab(body, s);
  else if (r.tab === "log") renderLogTab(body, s);
}

function renderUsbTab(body, s) {
  body.innerHTML = `
    <div class="toolbar">
      <input class="input" id="usb-filter" placeholder="Filter devices" value="${esc(state.filter)}" style="width:260px">
      <label class="check"><input type="checkbox" id="show-hubs" ${state.showHubs ? "checked" : ""}> Show hubs</label>
      <span class="grow"></span>
      <span class="small muted">Attach = share on the setup + attach to this PC. The device then shows here as if plugged in locally.</span>
    </div>
    <div id="usb-table"><div class="muted">Loading devices…</div></div>`;
  $("#usb-filter").addEventListener("input", (e) => { state.filter = e.target.value; drawUsb(s.id, true); });
  $("#show-hubs").addEventListener("change", (e) => { state.showHubs = e.target.checked; loadUsb(s.id, true); });
  if (state.usb[s.id]) drawUsb(s.id, true);
  loadUsb(s.id, true);
}

async function loadUsb(sid, force = false) {
  try {
    const data = await api("GET", `/api/setups/${enc(sid)}/usb${state.showHubs ? "?all=1" : ""}`);
    state.usb[sid] = data.devices;
    drawUsb(sid, force);
  } catch (e) {
    const el = $("#usb-table");
    const r = route();
    if (el && r.view === "setup" && r.id === sid) el.innerHTML = `<div class="banner red"><div class="grow">${esc(e.message)}</div></div>`;
  }
}

function drawUsb(sid, force = false) {
  const el = $("#usb-table");
  const r = route();
  if (!el || r.view !== "setup" || r.id !== sid || r.tab !== "usb") return;
  const q = state.filter.trim().toLowerCase();
  const devs = (state.usb[sid] || []).filter((d) => !q || [d.name, d.vidpid, d.serial, d.busid, ...(d.ttys || [])].join(" ").toLowerCase().includes(q));
  const json = JSON.stringify([devs, state.menu, [...state.busy]]);
  if (!force && json === state.usbJson[sid]) return;
  state.usbJson[sid] = json;
  el.innerHTML = devs.length ? usbTable(sid, devs) : `<div class="empty">${ICON.usb}<h3>No USB devices</h3><p>${q ? "No device matches the filter." : "Plug a device into this setup PC. It shows here in a few seconds."}</p></div>`;
  placeMenus();
}

function renderInfoTab(body, s) {
  body.innerHTML = '<div class="muted">Loading…</div>';
  api("GET", `/api/setups/${enc(s.id)}/status`).then((st) => {
    state.status[s.id] = st;
    const r = route();
    if (r.view !== "setup" || r.id !== s.id || r.tab !== "info") return;
    const mem = st.memory, disk = st.disk;
    body.innerHTML = `
      <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(340px,1fr))">
        <div class="card"><div class="title">System</div><div class="kv" style="margin-top:10px">
          <span class="k">Hostname</span><span>${esc(st.hostname)}</span>
          <span class="k">OS</span><span>${esc(st.os)}</span>
          <span class="k">Kernel</span><span class="mono">${esc(st.kernel)} (${esc(st.arch)})</span>
          <span class="k">Uptime</span><span>${fmtUptime(st.uptime)}</span>
          <span class="k">CPUs</span><span>${esc(st.cpu_count)}</span>
          <span class="k">Load</span><span>${st.load ? st.load.map((x) => x.toFixed(2)).join(" / ") : "–"}</span>
          <span class="k">Agent</span><span>v${esc(st.version)}, runs as ${esc(st.agent_user)}, up ${fmtUptime(st.agent_uptime)}</span>
          <span class="k">Shell user</span><span>${esc(st.run_as)}</span>
        </div></div>
        <div class="card"><div class="title">Resources</div><div style="margin-top:10px">
          ${bar("CPU", st.cpu_percent, st.cpu_percent != null ? `${st.cpu_percent}%` : "–")}
          ${bar("RAM", (100 * mem.used) / mem.total, `${fmtBytes(mem.used)}`)}
          ${bar("Disk", (100 * disk.used) / disk.total, `${fmtBytes(disk.free)} free`)}
          <div class="kv" style="margin-top:12px">
            <span class="k">Memory</span><span>${fmtBytes(mem.used)} of ${fmtBytes(mem.total)}</span>
            <span class="k">Disk /</span><span>${fmtBytes(disk.used)} of ${fmtBytes(disk.total)}</span>
            ${(st.temperatures || []).map((t) => `<span class="k">${esc(t.zone)}</span><span>${t.celsius.toFixed(1)} °C</span>`).join("")}
          </div>
        </div></div>
        <div class="card"><div class="title">Network</div><div class="kv" style="margin-top:10px">
          ${(st.ips || []).map((i) => `<span class="k">${esc(i.iface || "ip")}</span><span class="mono">${esc(i.addr)}${i.prefix ? "/" + esc(i.prefix) : ""}</span>`).join("") || '<span class="muted">no addresses</span>'}
        </div></div>
        <div class="card"><div class="title">USB/IP</div><div class="kv" style="margin-top:10px">
          <span class="k">usbip installed</span><span>${st.usbip.usbip_installed ? "yes" : '<span class="pill red">no</span>'}</span>
          <span class="k">usbipd running</span><span>${st.usbip.usbipd_running ? "yes" : '<span class="pill amber">no</span>'}</span>
          <span class="k">usbip-host module</span><span>${st.usbip.module_loaded ? "loaded" : '<span class="pill amber">not loaded</span>'}</span>
          <span class="k">Devices</span><span>${st.usb_count} (${st.usb_bound} shared, ${st.usb_in_use} in use)</span>
        </div></div>
      </div>`;
  }).catch((e) => { body.innerHTML = `<div class="banner red"><div class="grow">${esc(e.message)}</div></div>`; });
}

function renderLogTab(body, s) {
  body.innerHTML = `<div class="toolbar"><button class="btn" id="log-refresh">${ICON.refresh} Refresh</button></div><div class="log" id="log-box">Loading…</div>`;
  const load = () => api("GET", `/api/setups/${enc(s.id)}/log?n=500`).then((d) => {
    const box = $("#log-box");
    if (!box) return;
    box.innerHTML = d.lines.length ? d.lines.map((l) => `<div class="${esc(l.level)}">${esc(l.msg)}</div>`).join("") : '<span class="muted">empty</span>';
    box.scrollTop = box.scrollHeight;
  }).catch((e) => { const box = $("#log-box"); if (box) box.textContent = e.message; });
  $("#log-refresh").addEventListener("click", load);
  load();
}

/* ------------------------------------------------------------------ terminal */

function termSession(sid) {
  let t = state.terms.get(sid);
  if (t) return t;
  const el = document.createElement("div");
  el.className = "term-host";
  const term = new Terminal({
    cursorBlink: true, fontSize: 13, scrollback: 20000, allowProposedApi: true,
    fontFamily: 'ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace',
    theme: { background: "#0a0c10", foreground: "#e6e9ef", cursor: "#4f8cff", selectionBackground: "#2a3b5c" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  t = { el, term, fit, ws: null, status: "disconnected", opened: false };
  term.onData((d) => { if (t.ws && t.ws.readyState === 1) t.ws.send(JSON.stringify({ t: "i", d })); });
  term.onResize(({ cols, rows }) => { if (t.ws && t.ws.readyState === 1) t.ws.send(JSON.stringify({ t: "r", c: cols, r: rows })); });
  new ResizeObserver(() => { if (t.opened && el.offsetParent) { try { fit.fit(); } catch (_) { /* hidden */ } } }).observe(el);
  state.terms.set(sid, t);
  return t;
}

function termConnect(sid) {
  const t = termSession(sid);
  if (t.ws) { t.ws.onclose = null; t.ws.close(); }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/setups/${enc(sid)}/shell?cols=${t.term.cols}&rows=${t.term.rows}`);
  ws.binaryType = "arraybuffer";
  t.ws = ws;
  t.status = "connecting";
  updateTermBar(sid);
  ws.onopen = () => { t.status = "connected"; updateTermBar(sid); t.term.focus(); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let m = {};
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.t === "exit") t.term.write(`\r\n\x1b[90m[shell exited${m.code != null ? ` with code ${m.code}` : ""}]\x1b[0m\r\n`);
      if (m.t === "error") t.term.write(`\r\n\x1b[31m${m.error}\x1b[0m\r\n`);
    } else {
      t.term.write(new Uint8Array(ev.data));
    }
  };
  ws.onclose = () => {
    if (t.ws !== ws) return;
    t.ws = null;
    t.status = "disconnected";
    updateTermBar(sid);
  };
}

function updateTermBar(sid) {
  const el = document.querySelector(`[data-term-status="${CSS.escape(sid)}"]`);
  if (!el) return;
  const t = state.terms.get(sid);
  const st = t ? t.status : "disconnected";
  el.innerHTML = st === "connected" ? '<span class="pill green">connected</span>' : st === "connecting" ? '<span class="pill amber">connecting…</span>' : '<span class="pill grey">disconnected</span>';
  const btn = document.querySelector(`[data-term-connect="${CSS.escape(sid)}"]`);
  if (btn) btn.textContent = st === "connected" ? "Reconnect" : "Connect";
}

function mountTerminal(slot, sid) {
  const t = termSession(sid);
  slot.replaceWith(t.el);
  if (!t.opened) { t.term.open(t.el); t.opened = true; }
  requestAnimationFrame(() => { try { t.fit.fit(); } catch (_) { /* ignore */ } t.term.focus(); });
  if (!t.ws && t.status === "disconnected" && !t.autoTried) { t.autoTried = true; termConnect(sid); }
  updateTermBar(sid);
}

function termBar(s, popout) {
  return `<div class="term-bar">
      <span data-term-status="${esc(s.id)}"></span>
      <span class="mono">${esc(s.name)} · ${esc(s.host)}</span>
      <span class="grow"></span>
      <button class="btn sm" data-act="term-connect" data-term-connect="${esc(s.id)}" data-setup="${esc(s.id)}">Connect</button>
      <button class="btn sm" data-act="term-clear" data-setup="${esc(s.id)}">Clear</button>
      ${popout ? "" : `<a class="btn sm" href="#/popout/${enc(s.id)}" target="_blank" title="Open in a new window">${ICON.ext} Pop out</a>`}
    </div>`;
}

function renderTerminalTab(body, s) {
  body.innerHTML = `<div class="term-shell">${termBar(s, false)}<div id="term-slot"></div></div>`;
  mountTerminal($("#term-slot"), s.id);
}

function renderPopout(main, sid) {
  const s = setupById(sid) || { id: sid, name: sid, host: "" };
  document.title = `${s.name} · terminal`;
  main.innerHTML = `<div class="term-shell">${termBar(s, true)}<div id="term-slot"></div></div>`;
  mountTerminal($("#term-slot"), sid);
}

/* ------------------------------------------------------------------ run commands */

const DEFAULT_SNIPPETS = ["uname -a", "lsusb", "ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null", "dmesg | tail -n 40", "usbip list -l", "uptime", "df -h /"];
function loadLS(key, def) { try { return JSON.parse(localStorage.getItem(key)) ?? def; } catch (_) { return def; } }
function saveLS(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (_) { /* private mode */ } }

function renderRunMany(main) {
  main.innerHTML = `<div class="page-head"><div><h1>Run on many setups</h1><div class="sub">Run one shell command on the selected setups at the same time.</div></div></div><div id="run-body"></div>`;
  if (!state.pickedSetups) state.pickedSetups = new Set(setups().filter((s) => s.online).map((s) => s.id));
  renderRunPanel($("#run-body"), null);
}

function renderRunPanel(body, fixedTargets) {
  const snippets = loadLS("sm_snippets", DEFAULT_SNIPPETS);
  const history = loadLS("sm_history", []);
  const picker = fixedTargets ? "" : `<div class="setup-picker">${setups().map((s) => `
      <span class="pick ${state.pickedSetups.has(s.id) ? "on" : ""}" data-act="pick" data-setup="${esc(s.id)}">${onlineDot(s)} ${esc(s.name)}</span>`).join("")}
      <button class="btn sm ghost" data-act="pick-all">All</button><button class="btn sm ghost" data-act="pick-none">None</button></div>`;
  body.innerHTML = `
    ${picker}
    <form class="run-form" id="run-form" autocomplete="off">
      <input class="input mono" id="run-cmd" list="run-history" placeholder="Command, e.g. ls -l /dev/ttyACM*" required>
      <datalist id="run-history">${history.map((h) => `<option value="${esc(h)}">`).join("")}</datalist>
      <input class="input" id="run-timeout" type="number" min="1" value="60" title="Timeout (seconds)" style="width:90px">
      <input class="input" id="run-user" placeholder="user (default)" style="width:140px" title="Run as this user (empty = the setup default)">
      <button class="btn primary" id="run-go" type="submit">Run</button>
    </form>
    <div class="snippets"><span class="small muted">Quick:</span>
      ${snippets.map((c, i) => `<span class="chip snippet" data-act="snippet" data-i="${i}" title="Click to use. Shift+click to remove.">${esc(c)}</span>`).join("")}
      <button class="btn sm ghost" data-act="snippet-save">+ Save current</button>
    </div>
    <div id="run-results"></div>`;
  const form = $("#run-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const command = $("#run-cmd").value.trim();
    if (!command) return;
    const targets = fixedTargets || [...state.pickedSetups];
    if (!targets.length) { toast("Select at least one setup", "err"); return; }
    const timeout = Number($("#run-timeout").value) || 60;
    const user = $("#run-user").value.trim() || undefined;
    saveLS("sm_history", [command, ...history.filter((h) => h !== command)].slice(0, 50));
    const btn = $("#run-go");
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> Running';
    const started = Date.now();
    try {
      let results;
      if (targets.length === 1) {
        try { results = [await api("POST", `/api/setups/${enc(targets[0])}/exec`, { command, timeout, user })]; }
        catch (err) { results = [{ setup: targets[0], command, error: err.message }]; }
      } else {
        results = (await api("POST", "/api/exec", { setups: targets, command, timeout })).results;
      }
      results.forEach((r) => { r.command = r.command || command; r.at = started; });
      state.runResults = [...results, ...state.runResults].slice(0, 40);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "Run";
      drawRunResults(fixedTargets);
    }
  });
  drawRunResults(fixedTargets);
  $("#run-cmd").focus();
}

function drawRunResults(fixedTargets) {
  const el = $("#run-results");
  if (!el) return;
  const list = fixedTargets ? state.runResults.filter((r) => fixedTargets.includes(r.setup)) : state.runResults;
  el.innerHTML = list.map((r) => {
    const s = setupById(r.setup);
    const pill = r.error ? '<span class="pill red">error</span>'
      : r.timed_out ? '<span class="pill amber">timeout</span>'
      : r.exit_code === 0 ? '<span class="pill green">exit 0</span>' : `<span class="pill red">exit ${esc(r.exit_code)}</span>`;
    return `<div class="result">
      <div class="result-head">${pill}<b>${esc(s?.name || r.setup)}</b><span class="cmd">$ ${esc(r.command)}</span>
        <span class="muted">${r.duration != null ? `${r.duration}s` : ""}</span>
        <button class="btn sm ghost" data-act="copy" data-text="${esc(r.stdout || r.error || "")}">Copy</button></div>
      ${r.error ? `<pre class="out err">${esc(r.error)}</pre>` : ""}
      ${r.stdout ? `<pre class="out">${esc(r.stdout)}</pre>` : ""}
      ${r.stderr ? `<pre class="out err">${esc(r.stderr)}</pre>` : ""}
      ${!r.error && !r.stdout && !r.stderr ? '<pre class="out muted">(no output)</pre>' : ""}
    </div>`;
  }).join("");
}

/* ------------------------------------------------------------------ search */

function renderSearch(main, q) {
  main.innerHTML = `<div class="page-head"><div><h1>USB search</h1><div class="sub">Devices on all online setups that match “${esc(q)}”</div></div></div><div id="search-body"><div class="muted">Searching…</div></div>`;
  $("#search").value = q;
  state.search.q = q;
  api("GET", `/api/usb/find?q=${enc(q)}`).then((d) => {
    state.search.results = d.devices;
    drawSearch();
  }).catch((e) => { $("#search-body").innerHTML = `<div class="banner red">${esc(e.message)}</div>`; });
}

function drawSearch() {
  const el = $("#search-body");
  if (!el || route().view !== "search") return;
  const devs = state.search.results || [];
  el.innerHTML = devs.length ? usbTable(null, devs, true) : '<div class="empty"><h3>Nothing found</h3><p>Try a vendor name, a vid:pid like 0483:374b, a serial number or a tty name.</p></div>';
  placeMenus();
}

/* ------------------------------------------------------------------ modals */

function openModal({ title, body, foot, wide, onMount }) {
  const root = $("#modal-root");
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog"><div class="modal-head">${esc(title)}</div><div class="modal-body">${body}</div><div class="modal-foot">${foot || ""}</div></div>`;
  root.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  back.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  back.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
  if (onMount) onMount(back, close);
  const first = back.querySelector("input, button.primary");
  if (first) first.focus();
  return close;
}

function confirmModal(title, text, okLabel = "OK", danger = false) {
  return new Promise((resolve) => {
    let done = false;
    const close = openModal({
      title,
      body: `<p style="margin:0;white-space:pre-wrap">${esc(text)}</p>`,
      foot: `<button class="btn" data-close>Cancel</button><button class="btn ${danger ? "danger solid" : "primary"}" id="confirm-ok">${esc(okLabel)}</button>`,
      onMount: (el, close) => {
        el.querySelector("#confirm-ok").addEventListener("click", () => { done = true; close(); resolve(true); });
        new MutationObserver((_, obs) => { if (!el.isConnected) { obs.disconnect(); if (!done) resolve(false); } }).observe($("#modal-root"), { childList: true });
      },
    });
    return close;
  });
}

function setupForm(existing, prefill = {}) {
  const s = existing || prefill;
  const isEdit = !!existing;
  openModal({
    title: isEdit ? `Edit ${existing.name}` : "Add setup",
    body: `<form class="form" id="setup-form" autocomplete="off">
      <div class="field"><label>Name</label><input class="input" name="name" value="${esc(s.name || "")}" placeholder="bench-1"></div>
      <div class="field"><label>Host / IP</label><input class="input mono" name="host" value="${esc(s.host || "")}" placeholder="192.168.1.20" required></div>
      <div class="field"><label>Agent port</label><input class="input" name="port" type="number" value="${esc(s.port || "")}" placeholder="8765"></div>
      <div class="field"><label>USB/IP port</label><input class="input" name="usbip_port" type="number" value="${esc(s.usbip_port || 3240)}"></div>
      <div class="field full"><label>Agent token</label><input class="input mono" name="token" type="password" placeholder="${isEdit ? (existing.has_token ? "unchanged (stored)" : "uses agent_defaults.token") : "printed by install.sh (CONFIG_TOKEN=…)"}">
        <span class="hint">Stored in the manager config file. Empty uses <code>agent_defaults.token</code>.</span></div>
      <div class="field full" style="flex-direction:row;gap:18px">
        <label class="check"><input type="checkbox" name="tls" ${s.tls ? "checked" : ""}> TLS (https)</label>
        <label class="check"><input type="checkbox" name="verify_tls" ${s.verify_tls ? "checked" : ""}> Verify certificate</label>
      </div>
      <div class="field full"><label>Notes</label><input class="input" name="notes" value="${esc(s.notes || "")}" placeholder="Board, location, owner…"></div>
    </form>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn primary" id="setup-save">${isEdit ? "Save" : "Add setup"}</button>`,
    onMount: (el, close) => {
      const form = el.querySelector("#setup-form");
      const save = async () => {
        if (!form.reportValidity()) return;
        const fd = new FormData(form);
        const body = {
          name: fd.get("name").trim() || fd.get("host").trim(), host: fd.get("host").trim(),
          port: Number(fd.get("port")) || null, usbip_port: Number(fd.get("usbip_port")) || 3240,
          tls: fd.get("tls") === "on", verify_tls: fd.get("verify_tls") === "on", notes: fd.get("notes").trim(),
        };
        const token = fd.get("token");
        if (token) body.token = token;
        const btn = el.querySelector("#setup-save");
        btn.disabled = true;
        btn.innerHTML = '<span class="spin"></span> Connecting';
        try {
          const r = isEdit ? await api("PUT", `/api/setups/${enc(existing.id)}`, body) : await api("POST", "/api/setups", body);
          close();
          toast(r.online ? `${r.setup.name}: connected` : `${r.setup.name} saved, but offline: ${r.error}`, r.online ? "ok" : "err");
          await pollOverview();
          location.hash = `#/setup/${enc(r.setup.id)}/usb`;
        } catch (e) {
          toast(e.message, "err");
          btn.disabled = false;
          btn.textContent = isEdit ? "Save" : "Add setup";
        }
      };
      el.querySelector("#setup-save").addEventListener("click", save);
      form.addEventListener("submit", (e) => { e.preventDefault(); save(); });
      form.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
    },
  });
}

function discoverModal() {
  openModal({
    title: "Scan network for agents",
    wide: true,
    body: '<div id="disc-body"><div class="muted"><span class="spin" style="display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite"></span> Sending UDP broadcast…</div></div>',
    foot: '<button class="btn" id="disc-again">Scan again</button><button class="btn primary" data-close>Close</button>',
    onMount: (el, close) => {
      const run = async () => {
        const box = el.querySelector("#disc-body");
        box.innerHTML = '<div class="muted">Scanning…</div>';
        try {
          const { found } = await api("POST", "/api/discover");
          box.innerHTML = found.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Address</th><th>Version</th><th></th></tr></thead><tbody>
            ${found.map((a, i) => `<tr><td><b>${esc(a.name)}</b><div class="dev-sub">${esc(a.hostname)}</div></td><td class="mono">${esc(a.host)}:${esc(a.port)}</td><td>${esc(a.version)}</td>
              <td class="actions">${a.known_as ? `<span class="pill green">added as ${esc(a.known_as)}</span>` : `<button class="btn sm primary" data-disc="${i}">Add</button>`}</td></tr>`).join("")}
            </tbody></table></div>
            <p class="small muted">The agent token is not sent by discovery. You type it when you add the setup (or set <code>agent_defaults.token</code>).</p>`
            : '<div class="empty small">No agents answered. Discovery uses UDP broadcast on port 8765 and works only in the same subnet. Add setups by IP instead.</div>';
          box.querySelectorAll("[data-disc]").forEach((b) => b.addEventListener("click", () => {
            const a = found[Number(b.dataset.disc)];
            close();
            setupForm(null, { name: a.name, host: a.host, port: a.port, tls: a.tls });
          }));
        } catch (e) { box.innerHTML = `<div class="banner red">${esc(e.message)}</div>`; }
      };
      el.querySelector("#disc-again").addEventListener("click", run);
      run();
    },
  });
}

let loginOpen = false;
function showLogin() {
  if (loginOpen) return;
  loginOpen = true;
  openModal({
    title: "Manager token required",
    body: '<div class="field"><label>Token (the "token" value in the manager config)</label><input class="input mono" id="login-token" type="password"></div>',
    foot: '<button class="btn primary" id="login-go">Sign in</button>',
    onMount: (el, close) => {
      const go = async () => {
        try {
          await api("POST", "/api/login", { token: el.querySelector("#login-token").value });
          loginOpen = false;
          close();
          pollOverview();
        } catch (e) { toast(e.message, "err"); }
      };
      el.querySelector("#login-go").addEventListener("click", go);
      el.querySelector("#login-token").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    },
  });
}

/* ------------------------------------------------------------------ actions */

async function runAction(key, fn, okMsg) {
  if (state.busy.has(key)) return;
  state.busy.add(key);
  state.menu = null;
  redrawDynamic();
  try {
    const r = await fn();
    toast(okMsg || r?.message || "done", "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    state.busy.delete(key);
    await refreshAfterAction();
  }
}

async function refreshAfterAction() {
  const r = route();
  const jobs = [pollOverview()];
  if (r.view === "setup") jobs.push(loadUsb(r.id, true));
  if (r.view === "search" && state.search.q !== undefined) {
    jobs.push(api("GET", `/api/usb/find?q=${enc(state.search.q)}`).then((d) => { state.search.results = d.devices; drawSearch(); }).catch(() => {}));
  }
  await Promise.all(jobs);
  redrawDynamic();
}

function redrawDynamic() {
  const r = route();
  if (r.view === "setup" && r.tab === "usb") drawUsb(r.id, true);
  else if (r.view === "search") drawSearch();
  else if (r.view === "overview") renderOverview($("#main"));
}

function findDevice(sid, busid) {
  return (state.usb[sid] || []).find((d) => d.busid === busid) || (state.search.results || []).find((d) => d.setup === sid && d.busid === busid);
}

async function usbAction(act, sid, busid, extra = {}) {
  const key = `${sid}/${busid}`;
  const url = (a) => `/api/setups/${enc(sid)}/usb/${enc(busid)}/${a}`;
  const dev = findDevice(sid, busid);
  const name = dev ? `${dev.name} (${busid})` : busid;
  switch (act) {
    case "attach":
      return runAction(key, () => api("POST", url("attach"), {}), `Attached ${name} to this PC`);
    case "take":
      if (!(await confirmModal("Take over device?", `${name} is in use by another client. Taking it disconnects that client.`, "Take over", true))) return;
      return runAction(key, () => api("POST", url("attach"), { force: true }), `Attached ${name} to this PC`);
    case "detach":
      return runAction(key, () => api("POST", url("detach"), { unbind: true }), `Detached ${name}`);
    case "detach-keep":
      return runAction(key, () => api("POST", url("detach"), { unbind: false }), `Detached ${name} (still shared)`);
    case "bind":
      return runAction(key, async () => {
        try { return await api("POST", url("bind"), {}); } catch (e) {
          if (/force/i.test(e.message) && await confirmModal("Force bind?", e.message, "Bind anyway", true)) return api("POST", url("bind"), { force: true });
          throw e;
        }
      }, `Shared ${name}`);
    case "unbind":
      if (dev && dev.state === "in_use" && !dev.attached_here && !(await confirmModal("Stop sharing?", `${name} is in use by another client. It loses the device.`, "Stop sharing", true))) return;
      return runAction(key, () => api("POST", url("unbind"), {}), `Stopped sharing ${name}`);
    case "reset":
      return runAction(key, () => api("POST", url("reset"), {}));
    case "sticky":
      return runAction(key, () => api("POST", url("sticky"), { enabled: extra.enabled }));
    case "autobind":
      return runAction(key, () => api("POST", url("autobind"), { enabled: extra.enabled }));
    default:
      return undefined;
  }
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); toast("Copied");
  }
}

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) {
    if (state.menu && !e.target.closest(".menu")) { state.menu = null; redrawDynamic(); }
    return;
  }
  const act = el.dataset.act;
  const sid = el.dataset.setup;
  const busid = el.dataset.busid;
  if (act !== "menu" && state.menu && act !== "copy" && act !== "copy-attach") state.menu = null;
  switch (act) {
    case "menu":
      e.stopPropagation();
      state.menu = state.menu === el.dataset.key ? null : el.dataset.key;
      redrawDynamic();
      return;
    case "attach": case "take": case "detach": case "detach-keep": case "bind": case "unbind": case "reset":
      return usbAction(act, sid, busid);
    case "sticky": case "autobind":
      return usbAction(act, sid, busid, { enabled: el.dataset.enabled === "true" });
    case "copy":
      state.menu = null; redrawDynamic();
      return copyText(el.dataset.text || "");
    case "copy-attach": {
      const s = setupById(sid);
      state.menu = null; redrawDynamic();
      return copyText(`sudo usbip attach -r ${s ? s.host : sid} -b ${busid}`);
    }
    case "local-detach":
      return runAction(`port/${el.dataset.port}`, () => api("POST", "/api/local/detach", { port: Number(el.dataset.port) }));
    case "add-setup": return setupForm(null);
    case "discover": return discoverModal();
    case "edit-setup": return setupForm(setupById(sid));
    case "remove-setup": {
      const s = setupById(sid);
      if (!(await confirmModal("Remove setup?", `Remove ${s.name} (${s.host}) from the manager? The agent on the PC keeps running.`, "Remove", true))) return;
      try { await api("DELETE", `/api/setups/${enc(sid)}`); toast(`Removed ${s.name}`); location.hash = "#/"; await pollOverview(); }
      catch (err) { toast(err.message, "err"); }
      return;
    }
    case "refresh":
      try { await api("POST", `/api/setups/${enc(sid)}/refresh`); } catch (err) { toast(err.message, "err"); }
      await pollOverview();
      return loadUsb(sid, true);
    case "start-usbipd":
      return runAction(`usbipd/${sid}`, () => api("POST", `/api/setups/${enc(sid)}/usbipd/start`), "usbipd started");
    case "term-connect": return termConnect(sid);
    case "term-clear": { const t = state.terms.get(sid); if (t) { t.term.clear(); t.term.focus(); } return; }
    case "pick":
      if (state.pickedSetups.has(sid)) state.pickedSetups.delete(sid); else state.pickedSetups.add(sid);
      el.classList.toggle("on");
      return;
    case "pick-all":
      state.pickedSetups = new Set(setups().map((s) => s.id));
      document.querySelectorAll(".pick").forEach((p) => p.classList.add("on"));
      return;
    case "pick-none":
      state.pickedSetups = new Set();
      document.querySelectorAll(".pick").forEach((p) => p.classList.remove("on"));
      return;
    case "snippet": {
      const snippets = loadLS("sm_snippets", DEFAULT_SNIPPETS);
      const i = Number(el.dataset.i);
      if (e.shiftKey) { snippets.splice(i, 1); saveLS("sm_snippets", snippets); el.remove(); return; }
      const input = $("#run-cmd");
      input.value = snippets[i];
      input.focus();
      return;
    }
    case "snippet-save": {
      const v = $("#run-cmd").value.trim();
      if (!v) { toast("Type a command first", "err"); return; }
      const snippets = loadLS("sm_snippets", DEFAULT_SNIPPETS);
      if (!snippets.includes(v)) snippets.push(v);
      saveLS("sm_snippets", snippets);
      const r = route();
      const body = r.view === "run" ? $("#run-body") : $("#tab-body");
      renderRunPanel(body, r.view === "run" ? null : [r.id]);
      $("#run-cmd").value = v;
      return;
    }
    default:
      return undefined;
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.menu) { state.menu = null; redrawDynamic(); }
});

/* ------------------------------------------------------------------ polling & boot */

async function pollOverview() {
  try {
    state.overview = await api("GET", "/api/overview");
  } catch (_) {
    return;
  }
  renderChrome();
  const r = route();
  if (r.view === "overview" && !state.menu) renderOverview($("#main"));
  if (r.view === "setup") {
    const s = setupById(r.id);
    const head = $("#setup-head");
    if (s && head) head.innerHTML = setupHead(s);
    else if (!head) render();
    const count = document.querySelector(".tab.active .count, .tab .count");
    if (count && s) count.textContent = s.usb_count ?? "";
  }
}

setInterval(pollOverview, 3000);
setInterval(() => {
  const r = route();
  if (document.hidden) return;
  if (r.view === "setup" && r.tab === "usb") loadUsb(r.id);
}, 2500);

window.addEventListener("hashchange", () => { state.menu = null; render(); });
window.addEventListener("resize", placeMenus);
$("#btn-add").addEventListener("click", () => setupForm(null));
$("#btn-discover").addEventListener("click", discoverModal);
$("#btn-local").addEventListener("click", () => { location.hash = "#/"; });
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#search").value.trim();
  location.hash = `#/search/${enc(q)}`;
});
$("#btn-theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  saveLS("sm_theme", next);
});
document.documentElement.dataset.theme = loadLS("sm_theme", "dark");

render();
pollOverview().then(render);
