/* TL-SwitchOS - interface web. Toute modification = commandes CLI envoyees au switch.
   Inspirations : facade et panneau lateral de port (style controleurs Wi-Fi/switch modernes),
   matrice port x VLAN (style switchs administrables PME), resume de sante (style gestion cloud). */
"use strict";
const $ = (s) => document.querySelector(s);
let CSRF = "", ME = null, ST = null, VIEW = "dash", PREV = null, TIMER = null;
const HIST = {}, TOT = [];          // debits par port et total (derniers points)
const HMAX = 60;

/* ---------- theme ---------- */
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("tls-theme", t); } catch (e) { /* navigateur sans stockage */ }
  const b = $("#themeBtn"); if (b) b.replaceChildren(icon(t === "light" ? "moon" : "sun"));
}
(function () {
  let t = null; try { t = localStorage.getItem("tls-theme"); } catch (e) { /* rien */ }
  document.documentElement.dataset.theme = t || (window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
})();

/* ---------- utilitaires DOM (pas d'innerHTML avec des donnees) ---------- */
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(3)) if (c !== null && c !== undefined && c !== false && c !== "")
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
const NS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...kids) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) e.setAttribute(k, v);
  for (const c of kids.flat()) if (c) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
/* icones dessinees ici (traits simples) */
const ICONS = {
  dash: "M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-6H3zM13 9h8V3h-8z",
  ports: "M4 6h16v12H4zM8 10h2v4H8zM14 10h2v4h-2z",
  vlan: "M4 5h16M4 12h16M4 19h16M8 3v4M16 10v4M11 17v4",
  lag: "M4 7h6l4 5-4 5H4M14 12h6",
  sys: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1",
  users: "M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM22 19v-1a4 4 0 0 0-3-3.9M16 4.1a3 3 0 0 1 0 5.8",
  tables: "M3 4h18v16H3zM3 10h18M3 15h18M9 4v16",
  cfg: "M6 2h9l5 5v15H6zM14 2v6h6M9 13h8M9 17h6",
  maj: "M12 3v12M7 10l5 5 5-5M4 21h16",
  console: "M4 5h16v14H4zM7 9l3 3-3 3M12 15h5",
  sun: "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 1v2M12 21v2M1 12h2M21 12h2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  menu: "M3 6h18M3 12h18M3 18h18",
  x: "M6 6l12 12M18 6L6 18",
  ok: "M20 6L9 17l-5-5",
  warn: "M12 3l10 18H2zM12 10v4M12 18h.01",
  save: "M5 3h11l3 3v15H5zM8 3v6h8M8 21v-7h8v7",
  up: "M12 19V5M5 12l7-7 7 7",
  down: "M12 5v14M5 12l7 7 7-7",
};
function icon(n) { return s("svg", { class: "i", viewBox: "0 0 24 24" }, s("path", { d: ICONS[n] || "" })); }

function toast(msg, bad) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), bad ? 7000 : 3500);
}
function fmtBytes(b) { const u = ["o", "Ko", "Mo", "Go", "To"]; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + " " + u[i]; }
function fmtRate(bps) { const u = ["b/s", "Kb/s", "Mb/s", "Gb/s"]; let i = 0; while (bps >= 1000 && i < 3) { bps /= 1000; i++; } return bps.toFixed(i ? 1 : 0) + " " + u[i]; }
function fmtUp(s_) { const d = Math.floor(s_ / 86400), hh = Math.floor(s_ % 86400 / 3600), m = Math.floor(s_ % 3600 / 60); return (d ? d + " j " : "") + hh + " h " + m + " min"; }
const isAdmin = () => ME && ME.priv >= 15;
const vcolor = (vid) => "hsl(" + ((vid * 47) % 360) + " 70% 55%)";
const phys = () => ST.ports.filter((p) => !p.name.startsWith("po"));
const STATUS = { connected: ["Connecté", "ok"], notconnect: ["Pas de lien", ""], disabled: ["Désactivé", "off"], absent: ["Absent", "off"], "err-disabled": ["Bloqué (sécurité)", "off"] };
function statusPill(p) { const [t, c] = STATUS[p.status] || [p.status, ""]; return h("span", { class: "pill " + c }, h("span", { class: "dot " + c }), t); }
function vlanLabel(p) {
  if (p.cg) return "Membre de Po" + p.cg;
  if (p.mode === "access") return h("span", { class: "vchip" }, h("i", { style: "background:" + vcolor(p.access) }), "VLAN " + p.access);
  return h("span", { class: "vchip" }, h("i", { style: "background:var(--blue2)" }), "Trunk " + (p.vlans || "aucune") + (p.native ? " (native " + p.native + ")" : ""));
}

/* ---------- API ---------- */
async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF": CSRF }, body: JSON.stringify(body) };
  const r = await fetch(path, Object.assign({ credentials: "same-origin" }, opt));
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("session expirée"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || ("erreur " + r.status));
  return data;
}

/* Envoie des commandes Cisco. Si le switch arme un retour arriere (acces de gestion touche),
   on confirme tout de suite : si le navigateur n'arrive plus a joindre le switch,
   la confirmation n'arrive jamais et le switch revient seul a l'ancienne config. */
async function sendCmds(lines, okMsg) {
  const pre = $("#cmdlogPre");
  pre.textContent += lines.join("\n") + "\n!\n"; pre.scrollTop = pre.scrollHeight;
  try {
    const r = await api("/api/config", { lines });
    if (r.rollback !== null && r.rollback !== undefined) {
      try { await confirmChange(); } catch (e) { /* le gardien du switch reviendra en arriere */ }
    }
    if (r.errors && r.errors.length) toast("Erreurs : " + r.errors.join(" | "), true);
    else toast(okMsg || "Appliqué");
    await refresh();
    return r;
  } catch (e) { toast(e.message, true); }
}
async function confirmChange() {
  // petite pause : laisse le temps aux liens de se reconstruire, puis prouve qu'on joint encore le switch
  await new Promise((res) => setTimeout(res, 2500));
  for (let i = 0; i < 6; i++) {
    try { await api("/api/confirm", {}); return true; } catch (e) { await new Promise((res) => setTimeout(res, 4000)); }
  }
  toast("Le switch n'est plus joignable : retour arrière automatique en cours (90 s).", true);
  return false;
}
async function save() { try { await api("/api/save", {}); toast("Configuration enregistrée (write memory)"); refresh(); } catch (e) { toast(e.message, true); } }

/* ---------- navigation ---------- */
const NAV = [
  ["Supervision", [["dash", "Tableau de bord"], ["ports", "Ports"], ["tables", "Voisins et MAC"]]],
  ["Configuration", [["vlans", "VLAN"], ["lag", "Agrégats (LACP)"], ["sys", "IP et système"], ["users", "Comptes"]]],
  ["Maintenance", [["cfg", "Sauvegarde"], ["maj", "Mise à jour"], ["console", "Console CLI"]]],
];
const TITLES = Object.fromEntries(NAV.flatMap((g) => g[1]));
const ICON_OF = { dash: "dash", ports: "ports", tables: "tables", vlans: "vlan", lag: "lag", sys: "sys", users: "users", cfg: "cfg", maj: "maj", console: "console" };

/* ---------- connexion ---------- */
function showLogin() {
  $("#app").classList.add("hidden"); $("#pwc").classList.add("hidden"); $("#login").classList.remove("hidden"); clearInterval(TIMER);
}
document.addEventListener("DOMContentLoaded", init);
async function init() {
  $("#nav").replaceChildren(...NAV.map(([g, items]) => h("div", {}, h("div", { class: "nav-g" }, g),
    items.map(([v, t]) => h("a", { "data-v": v }, icon(ICON_OF[v]), t)))));
  $("#burger").append(icon("menu"));
  setTheme(document.documentElement.dataset.theme);
  $("#themeBtn").onclick = () => setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  $("#burger").onclick = () => $("#side").classList.toggle("open");
  $("#loginForm").addEventListener("submit", async (ev) => {
    ev.preventDefault(); $("#loginErr").textContent = "";
    try {
      await api("/api/login", { user: $("#lu").value, password: $("#lp").value });
      $("#lp").value = ""; await start();
    } catch (e) { $("#loginErr").textContent = e.message; }
  });
  $("#pwcForm").addEventListener("submit", async (ev) => {
    ev.preventDefault(); $("#pwcErr").textContent = "";
    if ($("#pn1").value !== $("#pn2").value) { $("#pwcErr").textContent = "Les deux saisies sont différentes."; return; }
    try {
      const r = await api("/api/password", { new: $("#pn1").value });
      if (r.error) { $("#pwcErr").textContent = r.error; return; }
      $("#pn1").value = $("#pn2").value = ""; toast("Mot de passe enregistré"); await start();
    } catch (e) { $("#pwcErr").textContent = e.message; }
  });
  $("#logout").onclick = async () => { await api("/api/logout", {}); showLogin(); };
  $("#saveBtn").onclick = save;
  $("#nav").addEventListener("click", (ev) => { const a = ev.target.closest("a"); if (a && a.dataset.v) { go(a.dataset.v); $("#side").classList.remove("open"); } });
  document.querySelector(".cmdlog-h").onclick = () => $("#cmdlog").classList.toggle("min");
  $("#modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") closeModal(); });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeModal(); });
  const me = await api("/api/me");
  if (me.logged) await start(); else showLogin();
}
async function start() {
  ME = await api("/api/me"); CSRF = ME.csrf;
  $("#login").classList.add("hidden");
  if (ME.must_change) { $("#app").classList.add("hidden"); $("#pwc").classList.remove("hidden"); setTimeout(() => $("#pn1").focus(), 50); return; }
  $("#pwc").classList.add("hidden"); $("#app").classList.remove("hidden");
  $("#who").textContent = ME.user + (isAdmin() ? " · admin" : " · lecture");
  $("#saveBtn").classList.toggle("hidden", !isAdmin());
  await refresh(); go(VIEW);
  clearInterval(TIMER); TIMER = setInterval(() => { if (VIEW === "dash" || VIEW === "ports") refresh(true); else refreshHeader(); }, 4000);
}
async function refreshHeader() { try { ST = await api("/api/state"); header(); } catch (e) { /* hors ligne */ } }
async function refresh(rerender) {
  const old = ST; ST = await api("/api/state"); header();
  if (old) {
    const now = Date.now(); const dt = PREV ? (now - PREV.t2) / 1000 : 4;
    PREV = { t: PREV ? PREV.t2 : now - 4000, t2: now, ports: Object.fromEntries(old.ports.map((p) => [p.name, p])) };
    let trx = 0, ttx = 0;
    for (const p of ST.ports) {
      const o = PREV.ports[p.name]; if (!o || dt <= 0) continue;
      const rx = Math.max(0, (p.rx - o.rx) * 8 / dt), tx = Math.max(0, (p.tx - o.tx) * 8 / dt);
      (HIST[p.name] = HIST[p.name] || []).push({ rx, tx }); if (HIST[p.name].length > HMAX) HIST[p.name].shift();
      if (!p.name.startsWith("po")) { trx += rx; ttx += tx; }
    }
    TOT.push({ rx: trx, tx: ttx }); if (TOT.length > HMAX) TOT.shift();
  }
  if (rerender !== false && !$("#modal").classList.contains("hidden") && VIEW !== "dash" && VIEW !== "ports") return;
  if (rerender !== false) render();
}
function header() {
  $("#hn").textContent = ST.hostname; $("#ver").textContent = "TL-SwitchOS " + ST.version;
  document.title = ST.hostname + " · TL-SwitchOS";
  $("#unsaved").classList.toggle("hidden", !ST.unsaved);
  const rb = $("#rb");
  if (ST.rollback !== null) { rb.textContent = "Retour arrière dans " + ST.rollback + " s"; rb.classList.remove("hidden"); }
  else rb.classList.add("hidden");
}
function go(v) {
  VIEW = v; $("#title").textContent = TITLES[v] || "";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.v === v)); render();
}
function render() {
  if (!ST) return;
  const main = $("#main");
  if (VIEW === "console" && main.dataset.v === "console") return; // ne pas effacer le terminal
  if (VIEW === "maj" && main.dataset.v === "maj") return;         // ne pas effacer le suivi
  const y = window.scrollY;
  main.dataset.v = VIEW; main.replaceChildren(VIEWS[VIEW]());
  window.scrollTo(0, y);
}
function rateOf(name, key) { const a = HIST[name]; return a && a.length ? a[a.length - 1][key] : 0; }
function rate(p, key) { return HIST[p.name] ? fmtRate(rateOf(p.name, key)) : "-"; }

/* ---------- panneau lateral (detail + edition) ---------- */
function openModal(title, body, cmdsFn, okLabel, top) {
  const prev = h("div", { class: "preview" });
  const upd = () => { const c = cmdsFn(); prev.textContent = c.length ? c.join("\n") : "(aucune modification)"; };
  const box = h("div", {},
    h("div", { class: "drawer-h" }, h("div", {}, h("h2", {}, title)), h("button", { class: "icon-btn", onclick: closeModal, "aria-label": "Fermer" }, icon("x"))),
    top || "", body,
    h("div", { class: "sect" }, h("label", {}, "Commandes CLI qui seront envoyées"), prev),
    h("div", { class: "modal-actions" },
      h("button", { class: "btn", onclick: closeModal }, "Fermer"),
      isAdmin() ? h("button", { class: "btn primary", onclick: async () => { const c = cmdsFn(); if (!c.length) return closeModal(); closeModal(); await sendCmds(c); } }, okLabel || "Appliquer") : null));
  body.addEventListener("input", upd); body.addEventListener("change", upd); upd();
  $("#modalBox").replaceChildren(box); $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); document.querySelectorAll(".pp.sel").forEach((e) => e.classList.remove("sel")); }
function field(label, input, hint) { return h("label", {}, label, input, hint ? h("div", { class: "hint" }, hint) : null); }
function sel(opts, val) { return h("select", {}, opts.map(([v, t]) => h("option", { value: v, selected: String(v) === String(val) ? "selected" : null }, t))); }

/* ---------- graphiques SVG ---------- */
function chart(series, height) {
  const W = 600, H = height || 170, n = Math.max(20, ...series.map((se) => se.data.length));
  const max = Math.max(1000, ...series.flatMap((se) => se.data)) * 1.15;
  const svg = s("svg", { class: "chart", viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", style: "height:" + H + "px" });
  for (let i = 1; i < 4; i++) svg.append(s("line", { class: "g", x1: 0, x2: W, y1: H * i / 4, y2: H * i / 4, "vector-effect": "non-scaling-stroke" }));
  for (const se of series) {
    const pts = se.data.map((v, i) => [W * (i + n - se.data.length) / (n - 1), H - (v / max) * H]);
    if (pts.length < 2) continue;
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    svg.append(s("path", { d: line + " L" + pts[pts.length - 1][0] + " " + H + " L" + pts[0][0] + " " + H + " Z", fill: se.color, "fill-opacity": ".12", stroke: "none" }));
    svg.append(s("path", { d: line, fill: "none", stroke: se.color, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
  }
  return h("div", {}, svg, h("div", { class: "row", style: "justify-content:space-between;margin-top:6px" },
    h("div", { class: "chart-leg" }, series.map((se) => h("span", {}, h("i", { style: "background:" + se.color }), se.label + " " + fmtRate(se.data.length ? se.data[se.data.length - 1] : 0)))),
    h("span", { class: "muted", style: "font-size:12px" }, "échelle " + fmtRate(max) + " · temps réel")));
}
function spark(name) {
  const a = HIST[name] || []; const W = 90, H = 24, N = Math.max(20, a.length);
  const max = Math.max(1000, ...a.map((x) => Math.max(x.rx, x.tx)));
  const pl = (k) => a.map((x, i) => (W * (i + N - a.length) / (N - 1)).toFixed(1) + "," + (H - 2 - (x[k] / max) * (H - 4)).toFixed(1)).join(" ");
  return s("svg", { class: "spark", viewBox: "0 0 " + W + " " + H },
    a.length > 1 ? s("polyline", { points: pl("rx"), fill: "none", stroke: "#22c55e", "stroke-width": 1.5 }) : null,
    a.length > 1 ? s("polyline", { points: pl("tx"), fill: "none", stroke: "#60a5fa", "stroke-width": 1.5 }) : null);
}

/* ---------- vues ---------- */
const VIEWS = {};

/* facade realiste : LED lien (vert 1G, orange 100M), LED activite, couleur de VLAN sous le port */
function faceplate() {
  const ps = phys();
  const two = ps.length > 8;
  const cell = (p) => {
    const cls = ["pp", p.status === "connected" ? "up" : p.status === "disabled" ? "dis" : "down",
      p.speed && p.speed.endsWith("M") ? "slow" : "", (rateOf(p.name, "rx") + rateOf(p.name, "tx")) > 2000 ? "act" : ""].join(" ");
    const tag = p.cg ? h("div", { class: "vtag", style: "background:#fbbf24" }) : p.mode === "trunk" ? h("div", { class: "vtag trunk" }) : h("div", { class: "vtag", style: "background:" + vcolor(p.access) });
    const tip = p.long + (p.desc ? " · " + p.desc : "") + "\n" + (STATUS[p.status] || [p.status])[0] + (p.status === "connected" ? " " + p.speed : "");
    const el = h("div", { class: cls, title: tip, onclick: () => { el.classList.add("sel"); editPort(p); } },
      two && +p.name.slice(2) % 2 === 0 ? null : h("span", { class: "num" }, p.name.slice(2)),
      h("div", { class: "rj" }, h("span", { class: "led l" }), h("span", { class: "led r" })),
      two && +p.name.slice(2) % 2 === 0 ? h("span", { class: "num" }, p.name.slice(2)) : null,
      tag, h("span", { class: "lagl" }, p.cg ? "Po" + p.cg : ""));
    return el;
  };
  const order = two ? ps.slice().sort((a, b) => { const x = +a.name.slice(2), y = +b.name.slice(2); return (Math.ceil(x / 2) - Math.ceil(y / 2)) || (x - y); }) : ps;
  return h("div", {},
    h("div", { class: "chassis" },
      h("div", { class: "badge-c" }, h("b", {}, ST.hostname), h("span", {}, "TL-SwitchOS"), h("span", {}, ps.length + " × Gigabit")),
      h("div", { class: "ports" + (two ? " two" : ""), style: two ? "grid-template-rows:auto auto" : "" }, order.map(cell))),
    h("div", { class: "legend" },
      h("span", {}, h("i", { style: "background:#22c55e" }), "lien 1 Gb/s"),
      h("span", {}, h("i", { style: "background:#f59e0b" }), "lien 10/100 Mb/s"),
      h("span", {}, h("i", { style: "background:#273246" }), "pas de lien"),
      h("span", {}, h("i", { style: "background:#ef4444" }), "désactivé"),
      h("span", {}, h("i", { style: "background:repeating-linear-gradient(90deg,#60a5fa 0 4px,#a78bfa 4px 8px)" }), "trunk"),
      h("span", {}, h("i", { style: "background:#fbbf24" }), "agrégat"),
      h("span", { class: "muted" }, "couleur = VLAN d'accès · clic = détail du port")));
}

function healthCard() {
  const ps = phys(), issues = [];
  if (ST.rollback !== null) issues.push(["warn", "Une modification sera annulée dans " + ST.rollback + " s si elle n'est pas confirmée.", null]);
  if (ST.unsaved) issues.push(["warn", "La configuration active n'est pas enregistrée : elle sera perdue au redémarrage.", isAdmin() ? h("button", { class: "btn small primary", onclick: save }, "Enregistrer") : null]);
  if (ST.errors && ST.errors.length) issues.push(["warn", ST.errors.length + " ligne(s) de configuration ignorée(s) au chargement.", h("button", { class: "btn small", onclick: () => go("cfg") }, "Voir")]);
  if (!ST.ips.length) issues.push(["warn", "Aucune adresse IP de gestion active.", h("button", { class: "btn small", onclick: () => go("sys") }, "Configurer")]);
  const err = ps.filter((p) => p.rxerr + p.txerr > 0);
  if (err.length) issues.push(["warn", "Erreurs de transmission sur " + err.map((p) => p.disp).join(", ") + " (câble ou duplex ?).", null]);
  const blk = ST.ports.filter((p) => /block|discard/i.test(p.stp));
  if (blk.length) issues.push(["info", "Spanning-tree bloque " + blk.map((p) => p.disp).join(", ") + " pour éviter une boucle.", null]);
  const dis = ps.filter((p) => p.status === "disabled");
  if (dis.length) issues.push(["info", dis.length + " port(s) désactivé(s) : " + dis.map((p) => p.disp).join(", ") + ".", null]);
  const bad = issues.some((i) => i[0] === "warn");
  return h("div", { class: "card" },
    h("div", { class: "health " + (bad ? "warn" : "ok") }, h("div", { class: "big" }, icon(bad ? "warn" : "ok")),
      h("div", {}, h("b", {}, bad ? "À vérifier" : "Tout fonctionne"),
        h("span", { class: "muted" }, ps.filter((p) => p.status === "connected").length + " port(s) connecté(s) · " + ST.vlans.length + " VLAN · gestion " + (ST.ips.map((i) => i.addr.split("/")[0]).join(", ") || "aucune")))),
    issues.length ? h("ul", { class: "issues" }, issues.map(([lvl, txt, act]) => h("li", {}, h("span", { class: "dot " + (lvl === "warn" ? "warn" : "") }), h("span", {}, txt), act))) : null);
}

VIEWS.dash = () => {
  const ps = phys();
  const up = ps.filter((p) => p.status === "connected").length;
  const last = TOT.length ? TOT[TOT.length - 1] : { rx: 0, tx: 0 };
  const top = ST.ports.filter((p) => !p.name.startsWith("po")).map((p) => [p, rateOf(p.name, "rx") + rateOf(p.name, "tx")]).sort((a, b) => b[1] - a[1]).slice(0, 5);
  const kpi = (ic, k, v, sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, icon(ic), k), h("div", { class: "v" }, v), sub ? h("div", { class: "s" }, sub) : null);
  return h("div", {},
    healthCard(),
    h("div", { class: "grid", style: "margin-bottom:16px" },
      kpi("ports", "Ports connectés", up + " / " + ps.length, ps.filter((p) => p.mode === "trunk").length + " trunk · " + ST.ports.filter((p) => p.name.startsWith("po")).length + " agrégat(s)"),
      kpi("down", "Trafic entrant", fmtRate(last.rx), "tous ports"),
      kpi("up", "Trafic sortant", fmtRate(last.tx), "tous ports"),
      kpi("sys", "Charge processeur", ST.load || "-", "allumé depuis " + fmtUp(ST.uptime))),
    h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Façade"), h("div", { class: "r" }, h("button", { class: "btn small", onclick: () => go("ports") }, "Tous les ports"))), faceplate()),
    h("div", { class: "cols" },
      h("div", { class: "card" }, h("h3", {}, "Trafic du switch"), chart([{ data: TOT.map((x) => x.rx), color: "#22c55e", label: "Entrant" }, { data: TOT.map((x) => x.tx), color: "#60a5fa", label: "Sortant" }])),
      h("div", { class: "card tablewrap" }, h("h3", {}, "Ports les plus actifs"),
        h("table", {}, h("tbody", {}, top.map(([p, r]) => h("tr", { class: "click", onclick: () => editPort(p) },
          h("td", {}, h("b", {}, p.disp), h("div", { class: "muted", style: "font-size:12px" }, p.desc || "")), h("td", {}, spark(p.name)), h("td", { class: "num" }, fmtRate(r)))))))));
};

let PORT_FILTER = "all", PORT_Q = "";
function portTable(compact) {
  const q = PORT_Q.toLowerCase();
  const list = ST.ports.filter((p) => (PORT_FILTER === "all" || (PORT_FILTER === "up" && p.status === "connected") || (PORT_FILTER === "down" && p.status !== "connected") || (PORT_FILTER === "trunk" && p.mode === "trunk"))
    && (!q || (p.disp + " " + p.long + " " + p.desc + " " + p.vlans).toLowerCase().includes(q)));
  const rows = list.map((p) => h("tr", { class: "click", onclick: () => editPort(p) },
    h("td", {}, h("b", {}, p.disp)), h("td", {}, p.desc || h("span", { class: "muted" }, "—")),
    h("td", {}, statusPill(p)), h("td", {}, vlanLabel(p)),
    h("td", {}, p.status === "connected" ? p.speed : "—"),
    compact ? null : h("td", { class: "muted" }, p.stp || "—"),
    h("td", {}, spark(p.name)),
    h("td", { class: "num" }, rate(p, "rx")), h("td", { class: "num" }, rate(p, "tx")),
    compact ? null : h("td", { class: "num muted" }, fmtBytes(p.rx + p.tx)),
    compact ? null : h("td", { class: "num" }, (p.rxerr + p.txerr) ? h("span", { class: "pill warn" }, p.rxerr + p.txerr) : "")));
  const heads = ["Port", "Description", "État", "VLAN", "Vitesse", compact ? null : "STP", "Activité", "Entrée", "Sortie", compact ? null : "Total", compact ? null : "Erreurs"].filter((x) => x !== null);
  return h("table", {}, h("thead", {}, h("tr", {}, heads.map((x) => h("th", { class: ["Entrée", "Sortie", "Total", "Erreurs"].includes(x) ? "num" : "" }, x)))), h("tbody", {}, rows));
}
VIEWS.ports = () => {
  const search = h("input", { placeholder: "Rechercher un port, une description, une VLAN", value: PORT_Q });
  const box = h("div", { class: "tablewrap" }, portTable(false));
  search.addEventListener("input", () => { PORT_Q = search.value; box.replaceChildren(portTable(false)); });
  const seg = h("div", { class: "seg" }, [["all", "Tous"], ["up", "Connectés"], ["down", "Sans lien"], ["trunk", "Trunks"]].map(([k, t]) =>
    h("button", { class: PORT_FILTER === k ? "on" : "", onclick: () => { PORT_FILTER = k; render(); } }, t)));
  return h("div", {}, h("div", { class: "card" }, faceplate()),
    h("div", { class: "card" }, h("div", { class: "toolbar" }, search, seg), box));
};

function editPort(p) {
  if (p.cg) {
    const po = ST.ports.find((x) => x.name === "po" + p.cg);
    if (po && confirm(p.disp + " est membre de Po" + p.cg + ". Configurer Po" + p.cg + " ? (Annuler = ce port seulement)")) return editPort(po);
  }
  const desc = h("input", { value: p.desc, maxlength: 64, placeholder: "ex : Point d'acces" });
  const state = sel([["up", "Activé"], ["down", "Désactivé (shutdown)"]], p.shutdown ? "down" : "up");
  const mode = sel([["access", "Access : une seule VLAN, non taguée (poste, imprimante, caméra)"], ["trunk", "Trunk 802.1Q : plusieurs VLAN taguées (switch, borne, routeur)"]], p.mode);
  const vlanOpts = ST.vlans.map((v) => [v.id, v.id + " - " + v.name]);
  const acc = sel(vlanOpts, p.access);
  const allowed = h("input", { value: p.allowed === null ? "all" : (p.allowed.length ? compress(p.allowed) : "none"), placeholder: "all, none ou 10,20-30" });
  const native = sel(vlanOpts, p.native);
  const pf = h("input", { type: "checkbox", checked: p.portfast });
  const bg = h("input", { type: "checkbox", checked: p.bpduguard });
  const mtu = h("input", { type: "number", min: 68, max: 9216, value: p.mtu || "", placeholder: "1500 par défaut" });
  const accBox = h("div", {}, field("VLAN d'accès", acc));
  const trBox = h("div", {}, field("VLAN autorisées", allowed, "all = toutes, none = aucune, ou une liste comme 10,20-30"), field("VLAN native (non taguée)", native));
  const l2 = h("div", {}, field("Mode", mode), accBox, trBox,
    h("label", { class: "check" }, pf, "PortFast : le port s'active tout de suite (vers un poste ou un serveur)"),
    h("label", { class: "check" }, bg, "BPDU Guard : coupe le port si un autre switch y est branché"));
  const showMode = () => { accBox.classList.toggle("hidden", mode.value !== "access"); trBox.classList.toggle("hidden", mode.value !== "trunk"); };
  mode.addEventListener("change", showMode); showMode();
  const body = h("div", { class: "sect" }, h("h3", { style: "margin-top:12px" }, "Configuration"), field("Description", desc), field("État", state),
    p.cg ? h("p", { class: "muted" }, "Membre de Po" + p.cg + " : les VLAN se règlent sur Po" + p.cg + ".") : l2, field("MTU", mtu, "9000 pour les trames jumbo"));
  const st = (k, v) => h("div", {}, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
  const nb = h("div", { class: "v" }, "…");
  api("/api/lldp").then((d) => { const n = d.find((r) => r.local === p.disp); nb.textContent = n ? n.name + " (" + n.port + ")" : "aucun"; }).catch(() => { nb.textContent = "-"; });
  const top = h("div", {},
    h("div", { class: "row", style: "gap:8px;align-items:center" }, statusPill(p), p.status === "connected" ? h("span", { class: "pill" }, p.speed) : null, h("span", { class: "muted" }, p.long)),
    h("div", { style: "margin-top:12px" }, chart([{ data: (HIST[p.name] || []).map((x) => x.rx), color: "#22c55e", label: "Entrant" }, { data: (HIST[p.name] || []).map((x) => x.tx), color: "#60a5fa", label: "Sortant" }], 110)),
    h("div", { class: "stats" }, st("VLAN", vlanLabel(p)), st("Spanning-tree", p.stp || "—"),
      st("Reçu / envoyé", fmtBytes(p.rx) + " / " + fmtBytes(p.tx)), st("Erreurs", String(p.rxerr + p.txerr)),
      st("Adresse MAC", p.mac || "—"), h("div", {}, h("div", { class: "k" }, "Voisin LLDP"), nb),
      p.members && p.members.length ? st("Membres", p.members.join(", ")) : null));
  openModal(p.disp + (p.desc ? " · " + p.desc : ""), body, () => {
    const c = [];
    if (desc.value !== p.desc) c.push(desc.value ? " description " + desc.value : " no description");
    if ((state.value === "down") !== p.shutdown) c.push(state.value === "down" ? " shutdown" : " no shutdown");
    if (!p.cg) {
      if (mode.value !== p.mode) c.push(" switchport mode " + mode.value);
      if (mode.value === "access" && String(acc.value) !== String(p.access)) c.push(" switchport access vlan " + acc.value);
      if (mode.value === "trunk") {
        const cur = p.allowed === null ? "all" : (p.allowed.length ? compress(p.allowed) : "none");
        if (allowed.value.trim() !== cur) c.push(" switchport trunk allowed vlan " + allowed.value.trim());
        if (String(native.value) !== String(p.native)) c.push(" switchport trunk native vlan " + native.value);
      }
      if (pf.checked !== p.portfast) c.push(pf.checked ? " spanning-tree portfast" : " no spanning-tree portfast");
      if (bg.checked !== p.bpduguard) c.push(bg.checked ? " spanning-tree bpduguard enable" : " no spanning-tree bpduguard");
    }
    const m = mtu.value ? parseInt(mtu.value, 10) : null;
    if (m !== (p.mtu || null)) c.push(m ? " mtu " + m : " no mtu");
    return c.length ? ["interface " + p.long].concat(c) : [];
  }, "Appliquer", top);
}

/* matrice port x VLAN : U = non tagué (access ou native), T = tagué, vide = absent */
let VPEND = {};
function vlanState(p, vid) {
  if (VPEND[p.name] && vid in VPEND[p.name]) return VPEND[p.name][vid];
  if (p.mode === "access") return p.access === vid ? "U" : "";
  if (p.native === vid && (p.allowed === null || p.allowed.includes(vid))) return "U";
  return (p.allowed === null || p.allowed.includes(vid)) ? "T" : "";
}
function vlanTarget(p) {
  const st = {}; ST.vlans.forEach((v) => { st[v.id] = vlanState(p, v.id); });
  return st;
}
function vlanCmds() {
  const out = [], errs = [];
  for (const name of Object.keys(VPEND)) {
    const p = ST.ports.find((x) => x.name === name); if (!p) continue;
    const st = vlanTarget(p);
    const U = Object.keys(st).filter((v) => st[v] === "U").map(Number), T = Object.keys(st).filter((v) => st[v] === "T").map(Number);
    if (U.length > 1) { errs.push(p.disp + " : une seule VLAN non taguée (U) par port"); continue; }
    if (!U.length && !T.length) { errs.push(p.disp + " : au moins une VLAN"); continue; }
    const c = [];
    if (!T.length) {
      if (p.mode !== "access") c.push(" switchport mode access");
      if (p.access !== U[0] || p.mode !== "access") c.push(" switchport access vlan " + U[0]);
    } else {
      if (p.mode !== "trunk") c.push(" switchport mode trunk");
      c.push(" switchport trunk allowed vlan " + compress(T.concat(U)));
      if (U.length) { if (p.native !== U[0] || p.mode !== "trunk") c.push(" switchport trunk native vlan " + U[0]); }
      else if (T.includes(p.native)) { errs.push(p.disp + " : la VLAN native " + p.native + " doit rester non taguée (U) ou être retirée"); continue; }
    }
    if (c.length) out.push("interface " + p.long, ...c);
  }
  return [out, errs];
}
VIEWS.vlans = () => {
  const cols = ST.ports.filter((p) => !p.cg);
  const id = h("input", { placeholder: "ex : 30 ou 30-35" }), name = h("input", { placeholder: "ex : SERVEURS", maxlength: 32 });
  const nPend = Object.values(VPEND).reduce((a, o) => a + Object.keys(o).length, 0);
  const cycle = (p, vid) => {
    if (!isAdmin()) return;
    const cur = vlanState(p, vid), nxt = cur === "" ? "T" : cur === "T" ? "U" : "";
    const orig = (() => { const keep = VPEND[p.name]; VPEND[p.name] = {}; const o = vlanState(p, vid); VPEND[p.name] = keep || {}; return o; })();
    VPEND[p.name] = VPEND[p.name] || {};
    // un seul U par port : passer un autre en U enleve l'ancien U (devient absent)
    if (nxt === "U") ST.vlans.forEach((v) => { if (v.id !== vid && vlanState(p, v.id) === "U") VPEND[p.name][v.id] = ""; });
    if (nxt === orig) delete VPEND[p.name][vid]; else VPEND[p.name][vid] = nxt;
    if (!Object.keys(VPEND[p.name]).length) delete VPEND[p.name];
    render();
  };
  const matrix = h("table", { class: "matrix" },
    h("thead", {}, h("tr", {}, h("th", { class: "vl" }, "VLAN"), cols.map((p) => h("th", { title: p.long + (p.desc ? " · " + p.desc : "") }, p.disp.replace("Gi1/0/", ""))), h("th", {}, ""))),
    h("tbody", {}, ST.vlans.map((v) => h("tr", {},
      h("td", { class: "vl" }, h("span", { class: "vchip" }, h("i", { style: "background:" + vcolor(v.id) }), h("b", {}, v.id), " ", h("span", { class: "muted" }, v.name))),
      cols.map((p) => {
        const st = vlanState(p, v.id), chg = VPEND[p.name] && v.id in VPEND[p.name];
        return h("td", {}, h("button", { class: "cell " + st + (chg ? " chg" : ""), disabled: !isAdmin() || null, title: p.disp + " / VLAN " + v.id + " : " + (st === "U" ? "non taguée" : st === "T" ? "taguée" : "absente") + " (clic pour changer)", onclick: () => cycle(p, v.id) }, st || "·"));
      }),
      h("td", { style: "text-align:right;white-space:nowrap" }, isAdmin() ? [
        h("button", { class: "btn small ghost", onclick: () => { const n = prompt("Nouveau nom de la VLAN " + v.id, v.name); if (n) sendCmds(["vlan " + v.id, " name " + n.trim().replace(/\s+/g, "_")]); } }, "Renommer"),
        v.id !== 1 ? h("button", { class: "btn small ghost", style: "color:var(--red)", onclick: () => { if (confirm("Supprimer la VLAN " + v.id + " ?")) sendCmds(["no vlan " + v.id], "VLAN supprimée"); } }, "Supprimer") : null] : null)))));
  const [cmds, errs] = vlanCmds();
  return h("div", {},
    h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Appartenance des ports aux VLAN"),
      h("div", { class: "r" }, h("span", { class: "pill ok" }, "U non taguée"), h("span", { class: "pill info" }, "T taguée"))),
      h("p", { class: "muted", style: "margin-top:0" }, "Clique sur une case pour passer de absente à taguée (T) puis non taguée (U). Un port avec une seule VLAN U devient un port access, un port avec des VLAN T devient un trunk. Les membres d'agrégat se règlent sur la colonne Po."),
      h("div", { class: "tablewrap" }, matrix),
      nPend ? h("div", { class: "pendbar" }, h("b", {}, nPend + " changement(s)"),
        errs.length ? h("span", { class: "bad", style: "color:var(--red)" }, errs.join(" · ")) : h("span", { class: "muted" }, cmds.length / 2 > 0 ? "" : ""),
        h("div", { style: "margin-left:auto;display:flex;gap:8px" },
          h("button", { class: "btn", onclick: () => { VPEND = {}; render(); } }, "Annuler"),
          h("button", { class: "btn primary", disabled: errs.length ? true : null, onclick: () => openModal("Appliquer les changements de VLAN", h("div", {}), () => cmds, "Appliquer") }, "Vérifier et appliquer"))) : null),
    isAdmin() ? h("div", { class: "card" }, h("h3", {}, "Créer une VLAN"), h("div", { class: "row" }, field("Numéro", id), field("Nom", name),
      h("button", { class: "btn primary", onclick: () => { if (!id.value.trim()) return; const c = ["vlan " + id.value.trim()]; if (name.value.trim()) c.push(" name " + name.value.trim().replace(/\s+/g, "_")); sendCmds(c, "VLAN créée"); } }, "Créer"))) : null);
};
const _sendCmds = sendCmds;
// apres un envoi reussi depuis la matrice, on vide les changements en attente
sendCmds = async function (lines, okMsg) { const r = await _sendCmds(lines, okMsg); if (r && !(r.errors && r.errors.length)) { VPEND = {}; render(); } return r; }; // eslint-disable-line no-func-assign

function compress(list) {
  const v = [...list].sort((a, b) => a - b); const out = []; let i = 0;
  while (i < v.length) { let j = i; while (j + 1 < v.length && v[j + 1] === v[j] + 1) j++; out.push(i === j ? "" + v[i] : v[i] + "-" + v[j]); i = j + 1; }
  return out.join(",");
}
VIEWS.lag = () => {
  const pos = ST.ports.filter((p) => p.name.startsWith("po"));
  const free = ST.ports.filter((p) => !p.name.startsWith("po"));
  const num = h("input", { type: "number", min: 1, max: 64, value: (pos.length ? Math.max(...pos.map((p) => +p.name.slice(2))) + 1 : 1) });
  const mode = sel([["active", "LACP actif (recommandé)"], ["passive", "LACP passif"], ["on", "Statique (sans LACP)"]], "active");
  const boxes = free.map((p) => h("label", { class: "chip" }, h("input", { type: "checkbox", value: p.long, "data-cg": p.cg || "" }), p.disp + (p.cg ? " (Po" + p.cg + ")" : "")));
  return h("div", {}, h("div", { class: "card tablewrap" }, pos.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Agrégat", "Description", "État", "Membres", "VLAN", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, pos.map((p) => h("tr", {}, h("td", {}, h("b", {}, p.disp)), h("td", {}, p.desc),
        h("td", {}, h("span", { class: "badge " + (p.status === "connected" ? "ok" : "") }, p.status)), h("td", {}, p.members.join(", ")),
        h("td", {}, p.mode === "access" ? "access " + p.access : "trunk " + (p.vlans || "aucune")),
        h("td", { style: "text-align:right;white-space:nowrap" }, h("button", { class: "btn small", onclick: () => editPort(p) }, "Configurer"), " ",
          isAdmin() ? h("button", { class: "btn small danger", onclick: () => { if (confirm("Supprimer " + p.disp + " ? Les membres redeviennent des ports simples.")) sendCmds(["no interface " + p.long]); } }, "Supprimer") : null)))))
      : h("p", { class: "muted" }, "Aucun agrégat.")),
    isAdmin() ? h("div", { class: "card" }, h("h3", {}, "Créer un agrégat"),
      h("div", { class: "row" }, field("Numéro", num), field("Mode", mode)),
      h("label", {}, "Ports membres"), h("div", { class: "chips" }, boxes),
      h("p", { class: "muted" }, "L'autre extrémité (routeur, serveur, autre switch) doit avoir le même agrégat, en LACP de préférence."),
      h("button", { class: "btn primary", onclick: () => {
        const sel_ = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
        if (sel_.length < 1) return toast("Choisis au moins un port", true);
        const c = []; sel_.forEach((n) => { c.push("interface " + n, " channel-group " + num.value + " mode " + mode.value); });
        sendCmds(c, "Agrégat Po" + num.value + " créé");
      } }, "Créer")) : null);
};

VIEWS.sys = () => {
  const hn = h("input", { value: ST.hostname });
  const gw = h("input", { value: ST.gateway || "", placeholder: "192.168.1.1" });
  const dns = h("input", { value: ST.dns.join(" "), placeholder: "192.168.1.1" });
  const ntp = h("input", { value: ST.ntp.join(" "), placeholder: "192.168.1.1" });
  const stp = sel([["rstp", "RSTP (rapide, recommandé)"], ["stp", "STP classique"], ["none", "Désactivé"]], ST.stp_mode);
  const prio = sel(Array.from({ length: 16 }, (_, i) => [i * 4096, i * 4096 + (i === 8 ? " (défaut)" : i === 0 ? " (racine forcée)" : "")]), ST.stp_priority);
  const lldp = h("input", { type: "checkbox", checked: ST.lldp });
  const snmp = h("input", { value: ST.snmp, placeholder: "vide = SNMP coupé" });
  const loc = h("input", { value: ST.snmp_location });
  const sysCmds = () => {
    const c = [];
    if (hn.value.trim() && hn.value.trim() !== ST.hostname) c.push("hostname " + hn.value.trim());
    if (gw.value.trim() !== (ST.gateway || "")) c.push(gw.value.trim() ? "ip default-gateway " + gw.value.trim() : "no ip default-gateway");
    if (dns.value.trim() !== ST.dns.join(" ")) { c.push("no ip name-server"); if (dns.value.trim()) c.push("ip name-server " + dns.value.trim()); }
    if (ntp.value.trim() !== ST.ntp.join(" ")) { ST.ntp.forEach((n) => c.push("no ntp server " + n)); ntp.value.trim().split(/\s+/).filter(Boolean).forEach((n) => c.push("ntp server " + n)); }
    if (stp.value !== ST.stp_mode) c.push(stp.value === "none" ? "no spanning-tree" : "spanning-tree mode " + stp.value);
    if (String(prio.value) !== String(ST.stp_priority)) c.push("spanning-tree priority " + prio.value);
    if (lldp.checked !== ST.lldp) c.push(lldp.checked ? "lldp run" : "no lldp run");
    if (snmp.value.trim() !== ST.snmp) c.push(snmp.value.trim() ? "snmp-server community " + snmp.value.trim() + " ro" : "no snmp-server");
    if (loc.value.trim() !== ST.snmp_location && loc.value.trim()) c.push("snmp-server location " + loc.value.trim());
    return c;
  };
  const sviRows = ST.svis.map((s) => h("tr", {}, h("td", {}, h("b", {}, "Vlan" + s.vid)), h("td", {}, s.dhcp ? "DHCP" : s.addresses.join(", ") || "aucune"),
    h("td", {}, s.desc), h("td", {}, (ST.ips.filter((i) => i.if === "Vlan" + s.vid).map((i) => i.addr).join(", ")) || "-"),
    h("td", { style: "text-align:right;white-space:nowrap" }, isAdmin() ? [h("button", { class: "btn small", onclick: () => editSvi(s) }, "Modifier"), " ",
      h("button", { class: "btn small danger", onclick: () => { if (confirm("Supprimer l'interface Vlan" + s.vid + " ? Si tu gères le switch par cette adresse, tu perdras l'accès (retour arrière automatique en 90 s).")) sendCmds(["no interface Vlan" + s.vid]); } }, "Supprimer")] : null)));
  return h("div", {}, h("div", { class: "card tablewrap" }, h("h3", {}, "Adresses de gestion (interfaces Vlan)"),
      h("table", {}, h("thead", {}, h("tr", {}, ["Interface", "Configurée", "Description", "Active", ""].map((x) => h("th", {}, x)))), h("tbody", {}, sviRows)),
      isAdmin() ? h("button", { class: "btn", style: "margin-top:10px", onclick: () => editSvi(null) }, "Ajouter une interface Vlan") : null,
      ST.ips.some((i) => i.if === "Mgmt0") ? h("p", { class: "muted" }, "Port de gestion dédié Mgmt0 : " + ST.ips.filter((i) => i.if === "Mgmt0").map((i) => i.addr).join(", ") + " (hors switch, toujours joignable)") : null),
    h("div", { class: "grid2" },
      h("div", { class: "card" }, h("h3", {}, "Général"), field("Nom du switch", hn), field("Passerelle par défaut", gw), field("Serveurs DNS (séparés par des espaces)", dns), field("Serveurs NTP", ntp)),
      h("div", { class: "card" }, h("h3", {}, "Protocoles"), field("Spanning-tree", stp), field("Priorité du pont", prio), h("label", { class: "check" }, lldp, "LLDP (découverte des voisins)"), field("Communauté SNMP v2c (lecture)", snmp), field("Emplacement SNMP", loc))),
    isAdmin() ? h("button", { class: "btn primary", onclick: () => { const c = sysCmds(); if (!c.length) return toast("Rien à changer"); openModal("Appliquer les paramètres système", h("div", {}), () => c); } }, "Appliquer…") : null);
};
function editSvi(s) {
  const vid = sel(ST.vlans.map((v) => [v.id, v.id + " - " + v.name]), s ? s.vid : "");
  if (s) vid.disabled = true;
  const meth = sel([["static", "Adresse fixe"], ["dhcp", "DHCP"]], s && s.dhcp ? "dhcp" : "static");
  const addr = h("input", { value: s && !s.dhcp ? (s.addresses[0] || "") : "", placeholder: "192.168.1.20/24" });
  const desc = h("input", { value: s ? s.desc : "" });
  const body = h("div", {}, field("VLAN", vid), field("Méthode", meth), field("Adresse/masque", addr), field("Description", desc),
    h("p", { class: "muted" }, "Si cette modification te coupe l'accès, le switch revient tout seul à l'ancienne config après 90 s."));
  openModal(s ? "Interface Vlan" + s.vid : "Nouvelle interface Vlan", body, () => {
    const c = ["interface Vlan" + vid.value];
    if (meth.value === "dhcp") { if (!(s && s.dhcp)) c.push(" ip address dhcp"); }
    else if (addr.value.trim() && !(s && s.addresses[0] === addr.value.trim())) { if (s) c.push(" no ip address"); c.push(" ip address " + addr.value.trim()); }
    if (desc.value !== (s ? s.desc : "")) c.push(" description " + desc.value);
    return c.length > 1 || !s ? c : [];
  });
}

VIEWS.users = () => {
  const name = h("input", { placeholder: "ex : technicien" }), priv = sel([[15, "15 - administrateur"], [1, "1 - lecture seule"]], 15), pw = h("input", { type: "password", placeholder: "8 caractères minimum" });
  const en = h("input", { type: "password", placeholder: "nouveau mot de passe enable" });
  const o1 = h("input", { type: "password", autocomplete: "current-password" }), n1 = h("input", { type: "password", autocomplete: "new-password" }), n2 = h("input", { type: "password", autocomplete: "new-password" });
  const mine = h("div", { class: "card" }, h("h3", {}, "Mon mot de passe (" + ME.user + ")"),
    h("div", { class: "row" }, field("Actuel", o1), field("Nouveau", n1), field("Confirmation", n2),
      h("button", { class: "btn primary", onclick: async () => {
        if (n1.value !== n2.value) return toast("Les deux saisies sont différentes", true);
        try { const r = await api("/api/password", { old: o1.value, new: n1.value }); if (r.error) return toast(r.error, true); o1.value = n1.value = n2.value = ""; toast("Mot de passe changé et enregistré"); refresh(); } catch (e) { toast(e.message, true); }
      } }, "Changer")));
  return h("div", {}, mine,
    h("div", { class: "card tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Compte", "Niveau", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, ST.users.map((u) => h("tr", {}, h("td", {}, h("b", {}, u.name)), h("td", {}, u.priv === 15 ? "15 (admin)" : "1 (lecture)"),
        h("td", { style: "text-align:right" }, isAdmin() && u.name !== ME.user ? h("button", { class: "btn small danger", onclick: () => { if (confirm("Supprimer " + u.name + " ?")) sendCmds(["no username " + u.name]); } }, "Supprimer") : null)))))),
    isAdmin() ? h("div", { class: "grid2" },
      h("div", { class: "card" }, h("h3", {}, "Ajouter ou changer un mot de passe"), field("Compte", name), field("Niveau", priv), field("Mot de passe", pw),
        h("button", { class: "btn primary", style: "margin-top:12px", onclick: () => {
          if (!/^[a-z_][a-z0-9_-]{0,30}$/.test(name.value)) return toast("Nom : minuscules, chiffres, - et _", true);
          if (pw.value.length < 8) return toast("Mot de passe trop court", true);
          if (/\s/.test(pw.value)) return toast("Pas d'espace dans le mot de passe", true);
          sendCmds(["username " + name.value + " privilege " + priv.value + " secret 0 " + pw.value], "Compte enregistré"); pw.value = "";
          $("#cmdlogPre").textContent = $("#cmdlogPre").textContent.replace(/secret 0 \S+/g, "secret 0 ********");
        } }, "Enregistrer")),
      h("div", { class: "card" }, h("h3", {}, "Mot de passe enable"), h("p", { class: "muted" }, "Demandé aux comptes de niveau 1 qui tapent enable en CLI."), field("Enable secret", en),
        h("button", { class: "btn primary", style: "margin-top:12px", onclick: () => { if (en.value.length < 8 || /\s/.test(en.value)) return toast("8 caractères minimum, sans espace", true); sendCmds(["enable secret 0 " + en.value]); en.value = ""; $("#cmdlogPre").textContent = $("#cmdlogPre").textContent.replace(/secret 0 \S+/g, "secret 0 ********"); } }, "Enregistrer"))) : null);
};

VIEWS.tables = () => {
  const macBox = h("div", { class: "tablewrap" }, "Chargement…"), lldpBox = h("div", { class: "tablewrap" }, "Chargement…");
  const filter = h("input", { placeholder: "Filtrer (MAC, port, VLAN)" });
  let macs = [];
  const drawMac = () => {
    const f = filter.value.toLowerCase().replace(/[.:-]/g, "");
    const rows = macs.filter((m) => !f || (m.mac.replace(/:/g, "") + m.port.toLowerCase() + " " + m.vlan).includes(f));
    macBox.replaceChildren(h("table", {}, h("thead", {}, h("tr", {}, ["VLAN", "Adresse MAC", "Type", "Port"].map((x) => h("th", {}, x)))),
      h("tbody", {}, rows.map((m) => h("tr", {}, h("td", {}, m.vlan), h("td", { style: "font-family:monospace" }, m.mac), h("td", {}, m.type), h("td", {}, m.port))))),
      h("p", { class: "muted" }, rows.length + " adresse(s)"));
  };
  filter.addEventListener("input", drawMac);
  api("/api/mac").then((d) => { macs = d; drawMac(); }).catch((e) => macBox.textContent = e.message);
  api("/api/lldp").then((d) => lldpBox.replaceChildren(d.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Port local", "Voisin", "Port distant", "Adresse", "Description"].map((x) => h("th", {}, x)))),
    h("tbody", {}, d.map((r) => h("tr", {}, h("td", {}, r.local), h("td", {}, h("b", {}, r.name)), h("td", {}, r.port), h("td", {}, r.mgmt), h("td", { class: "muted" }, (r.descr || "").slice(0, 60)))))) : h("p", { class: "muted" }, "Aucun voisin LLDP."))).catch((e) => lldpBox.textContent = e.message);
  return h("div", {}, h("div", { class: "card" }, h("h3", {}, "Voisins LLDP"), lldpBox),
    h("div", { class: "card" }, h("div", { class: "row" }, h("h3", {}, "Table MAC"), filter,
      isAdmin() ? h("button", { class: "btn small", onclick: async () => { await api("/api/exec", { line: "clear mac address-table dynamic" }); render(); } }, "Vider") : null), macBox));
};

VIEWS.cfg = () => {
  const pre = h("pre", { class: "cfg" }, "Chargement…");
  api("/api/running").then((t) => pre.textContent = t).catch((e) => pre.textContent = e.message);
  const file = h("input", { type: "file", accept: ".conf,.txt,text/plain" });
  const area = h("textarea", { rows: 8, placeholder: "Colle une configuration complète ici, ou choisis un fichier." });
  file.addEventListener("change", async () => { if (file.files[0]) area.value = await file.files[0].text(); });
  return h("div", {}, h("div", { class: "row", style: "margin-bottom:12px" },
      h("a", { class: "btn", href: "/api/download" }, "Télécharger la running-config"),
      isAdmin() ? h("button", { class: "btn primary", onclick: async () => { await api("/api/save", {}); toast("Enregistrée"); refresh(); } }, "Enregistrer (write memory)") : null),
    h("div", { class: "card" }, h("h3", {}, "running-config"), pre),
    isAdmin() ? h("div", { class: "card" }, h("h3", {}, "Restaurer une configuration"),
      h("p", { class: "muted" }, "Remplace toute la configuration active. Protégé par le retour arrière automatique (90 s). Pense à enregistrer ensuite."),
      file, h("div", { style: "height:8px" }), area,
      h("button", { class: "btn danger", style: "margin-top:10px", onclick: async () => {
        if (!area.value.trim() || !confirm("Remplacer toute la configuration active ?")) return;
        try {
          const r = await api("/api/restore", { text: area.value });
          if (r.errors && r.errors.length) return toast("Config refusée : " + r.errors.slice(0, 3).join(" | "), true);
          await confirmChange(); toast("Configuration restaurée"); refresh();
        } catch (e) { toast(e.message, true); }
      } }, "Restaurer")) : null);
};

let TERM_HIST = [], TERM_POS = 0, TERM_PROMPT = null;
/* ---------- mise a jour du logiciel ---------- */
let MAJ_POLL = null;
VIEWS.maj = () => {
  const box = h("div", {});
  const info = h("div", { class: "card" }, h("h3", {}, "Logiciel"), h("p", { class: "muted" }, "Chargement…"));
  const prog = h("div", { class: "card hidden" }, h("h3", {}, "Avancement"), h("pre", { class: "log" }));
  const file = h("input", { type: "file", accept: ".tlsu" });
  const res = h("div", {});
  const resDepot = h("div", {});
  const depot = h("div", { class: "card" }, h("h3", {}, "Mises à jour en ligne"),
    h("p", { class: "muted" }, "Cherche les nouvelles versions de TL-SwitchOS et les mises à jour de sécurité Debian dans les dépôts. La nouvelle version est vérifiée et testée avant d'être activée, la configuration est conservée, et le switch revient tout seul à l'ancienne version si la nouvelle ne fonctionne pas."),
    isAdmin() ? h("button", { class: "btn", onclick: () => chercher() }, "Rechercher les mises à jour") : h("p", {}, "Niveau 15 requis."), resDepot);
  async function chercher() {
    resDepot.replaceChildren(h("p", { class: "muted" }, "Recherche en cours (jusqu'à une minute)…"));
    try {
      const d = await api("/api/maj/depot");
      const avert = d.configure ? "" : h("p", { class: "muted" }, "Dépôt TL-SwitchOS non configuré sur ce switch : seules les mises à jour Debian sont visibles. Installe une fois le fichier .tlsu d'une release récente pour l'activer.");
      if (d.error) { resDepot.replaceChildren(avert, h("p", { class: "bad" }, "Impossible de joindre les dépôts : " + d.error)); return; }
      if (!d.paquets.length) { resDepot.replaceChildren(avert, h("p", {}, "Tout est à jour.")); return; }
      const tl = d.paquets.find((p) => p.nom === "tlswitch");
      resDepot.replaceChildren(avert,
        h("p", {}, d.paquets.length + " paquet(s) à mettre à jour" + (tl ? ", dont TL-SwitchOS " + tl.nouvelle : "") + "."),
        h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Paquet"), h("th", {}, "Installé"), h("th", {}, "Disponible"))),
          h("tbody", {}, d.paquets.slice(0, 40).map((p) => h("tr", {}, h("td", {}, p.nom === "tlswitch" ? "TL-SwitchOS" : p.nom), h("td", {}, p.actuelle), h("td", {}, p.nouvelle))))),
        h("button", { class: "btn primary", onclick: () => { if (confirm("Installer les mises à jour ? Le switch redémarrera si nécessaire.")) lancer("/api/maj/installer", { depot: true }); } }, "Installer les mises à jour"));
    } catch (e) { resDepot.replaceChildren(h("p", { class: "bad" }, e.message)); }
  }
  const envoi = h("div", { class: "card" }, h("h3", {}, "Installer un fichier de mise à jour (.tlsu)"),
    h("p", { class: "muted" }, "Sans accès Internet, ou pour une mise à jour majeure : télécharge le fichier TL-SwitchOS-x.y.z.tlsu de la release, puis choisis-le ici. Il est vérifié (signature, intégrité, compatibilité) avant toute installation. La configuration est conservée, puis le switch redémarre (coupure d'environ 1 à 2 minutes). Si la nouvelle version ne fonctionne pas, il revient tout seul à l'ancienne."),
    file, " ", isAdmin() ? h("button", { class: "btn", onclick: () => verifier() }, "Vérifier le paquet") : h("p", {}, "Niveau 15 requis."), res);
  box.append(info, prog, depot, envoi);

  async function verifier() {
    if (!file.files.length) return toast("Choisis d'abord un fichier .tlsu", true);
    res.replaceChildren(h("p", { class: "muted" }, "Envoi et vérification…"));
    try {
      const r = await fetch("/api/maj/envoi", { method: "POST", credentials: "same-origin", headers: { "X-CSRF": CSRF, "Content-Type": "application/octet-stream" }, body: file.files[0] });
      const d = await r.json();
      if (!r.ok || d.error) { res.replaceChildren(h("p", { class: "bad" }, "Refusé : " + (d.error || r.status))); return; }
      const up = d.action === "mise a niveau";
      res.replaceChildren(h("table", {}, h("tbody", {},
        [["Version", d.actuelle + " → " + d.version + " (" + d.action + ")"], ["Type", d.type === "majeure" ? "MAJEURE : Debian " + (d.debian_actuel || "?") + " → " + (d.debian || "?") : "fonctionnalités"], ["Signature", d.signe ? "valide" : "aucune (ce switch n'a pas de clé)"], ["Date", d.date || "-"]].map(([a, b]) => h("tr", {}, h("th", {}, a), h("td", {}, b))))),
        d.notes ? h("pre", { class: "log" }, d.notes) : "",
        d.type === "majeure" ? h("p", { class: "bad" }, "Mise à jour majeure : Debian est aussi mis à niveau. Compte 20 à 60 minutes, le switch doit joindre Internet, et il ne faut pas couper le courant. La configuration est sauvegardée, mais Debian ne peut pas revenir en arrière automatiquement.") : "",
        h("button", { class: "btn primary", onclick: () => { if (confirm("Installer TL-SwitchOS " + d.version + " ? Le switch redémarrera à la fin.")) lancer("/api/maj/installer", { force: !up }); } }, up ? "Installer la version " + d.version + " et redémarrer" : "Forcer l'installation de " + d.version));
    } catch (e) { res.replaceChildren(h("p", { class: "bad" }, e.message)); }
  }
  async function lancer(url, body) {
    try { const r = await api(url, body); if (r.error) return toast(r.error, true); } catch (e) { return toast(e.message, true); }
    suivre();
  }
  function suivre() {
    prog.classList.remove("hidden"); clearInterval(MAJ_POLL);
    let perdu = 0;
    MAJ_POLL = setInterval(async () => {
      try {
        const st = await api("/api/maj/etat"); perdu = 0; dessiner(st);
        if (st.etat !== "en_cours" && st.etat !== "redemarrage") { clearInterval(MAJ_POLL); MAJ_POLL = null; refreshHeader(); }
      } catch (e) {
        // apres le redemarrage la session n'existe plus : on s'arrete, la page de connexion ramene ici
        if (!$("#login").classList.contains("hidden")) { clearInterval(MAJ_POLL); MAJ_POLL = null; VIEW = "maj"; return; }
        perdu++; const pre = prog.querySelector("pre"); if (pre && perdu === 1) pre.textContent += "\n… le switch redémarre, la page se reconnecte toute seule\n"; }
    }, 2000);
  }
  function dessiner(st) {
    info.replaceChildren(h("h3", {}, "Logiciel"), h("table", {}, h("tbody", {},
      [["Version active", st.actuelle], ["Version précédente", st.precedente || "aucune"], ["Signature exigée", st.signature ? "oui" : "non (pas de clé sur ce switch)"]].map(([a, b]) => h("tr", {}, h("th", {}, a), h("td", {}, b))))),
      st.precedente && isAdmin() ? h("button", { class: "btn", onclick: () => { if (confirm("Revenir à la version " + st.precedente + " ?")) lancer("/api/maj/retour", {}); } }, "Revenir à la version " + st.precedente) : "",
      ...(st.historique && st.historique.length ? [h("h3", {}, "Historique"), h("pre", { class: "log" }, st.historique.join("\n"))] : []));
    if (st.lignes && st.lignes.length) {
      prog.classList.remove("hidden");
      const fin = { ok: "Terminé : la nouvelle version est active.", annulee: "Annulée : retour automatique à la version précédente.", echec: "Échec : voir le détail ci-dessus.", redemarrage: "Le switch redémarre : reconnecte-toi dans 1 à 2 minutes." }[st.etat];
      prog.replaceChildren(h("h3", {}, "Avancement" + (st.vers ? " vers " + st.vers : "")), h("pre", { class: "log" }, st.lignes.join("\n")),
        fin ? h("p", { class: st.etat === "ok" ? "" : "bad" }, fin, " ", h("button", { class: "btn small", onclick: () => location.reload() }, "Recharger la page")) : "");
    }
  }
  api("/api/maj/etat").then((st) => { dessiner(st); if (st.etat === "en_cours") suivre(); }).catch(() => {});
  return box;
};

VIEWS.console = () => {
  const out = h("div", { class: "term" });
  const promptEl = h("span", {}, TERM_PROMPT || ST.hostname + (isAdmin() ? "#" : ">"));
  const inp = h("input", { autocomplete: "off", spellcheck: "false", placeholder: "show ?   (Tab non disponible : utilise ?)" });
  out.textContent = "Console TL-SwitchOS : mêmes commandes qu'en SSH. ? pour l'aide.\n\n";
  inp.addEventListener("keydown", async (ev) => {
    if (ev.key === "ArrowUp") { if (TERM_POS > 0) inp.value = TERM_HIST[--TERM_POS]; ev.preventDefault(); return; }
    if (ev.key === "ArrowDown") { if (TERM_POS < TERM_HIST.length) inp.value = TERM_HIST[++TERM_POS] || ""; ev.preventDefault(); return; }
    if (ev.key !== "Enter") return;
    const line = inp.value; inp.value = "";
    if (line.trim()) { TERM_HIST.push(line); TERM_POS = TERM_HIST.length; }
    out.textContent += promptEl.textContent + " " + line + "\n";
    try {
      const r = await api("/api/exec", { line });
      out.textContent += r.output; TERM_PROMPT = r.prompt; promptEl.textContent = r.prompt;
      if (r.rollback !== null && r.rollback !== undefined) { out.textContent += "% Accès de gestion modifié : confirmation automatique en cours...\n"; if (await confirmChange()) out.textContent += "% Confirmé.\n"; }
      refreshHeader();
    } catch (e) { out.textContent += "% " + e.message + "\n"; }
    out.scrollTop = out.scrollHeight;
  });
  setTimeout(() => inp.focus(), 50);
  return h("div", {}, out, h("div", { class: "term-in" }, promptEl, inp));
};
