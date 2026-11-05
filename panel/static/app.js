/* ARGOS EPP — panel v2 (sin dependencias, funciona sin internet) */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ico = (n, cls = "") => `<svg class="i ${cls}"><use href="/static/icons.svg#${n}"/></svg>`;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* sin almacenamiento */ } },
};

const TIPOS = {
  esp32cam: { icon: "camera", label: "ESP32-S3-CAM" },
  celular: { icon: "smartphone", label: "Celular (IP Webcam / DroidCam)" },
  celularweb: { icon: "smartphone", label: "Celular vinculado" },
  rtsp: { icon: "cctv", label: "Cámara IP (RTSP)" },
  usb: { icon: "video", label: "Webcam USB" },
  url: { icon: "radio-tower", label: "Enlace de video" },
};
const TITULOS = { vivo: "En vivo", transmisiones: "Transmisiones", dispositivos: "Dispositivos", reportes: "Reportes", ajustes: "Ajustes" };
const HINTS = {
  esp32cam: "Nombre mDNS (epp-cam01.local) o la IP de la ESP32.",
  celular: "La dirección que muestra IP Webcam o DroidCam, ej. 192.168.1.20:8080",
  rtsp: "Ej. 192.168.1.50:554/stream1 (con usuario: user:clave@192.168.1.50/stream1)",
  usb: "Número de cámara: 0 es la webcam de la laptop.",
  url: "Enlace http:// de un video MJPEG.",
};

const S = { devs: [], global: true, motor: false, sel: store.get("argos-sel", ""), view: "vivo", zone: "", sig: "", sound: store.get("argos-sound", "0") === "1", lastBeep: 0, evLimit: 50, repDev: "" };

async function api(path, opt = {}) {
  const o = { ...opt };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers = { "Content-Type": "application/json" }; }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = `Error ${r.status}`;
    try { const j = await r.json(); msg = j.detail || msg; } catch { /* sin cuerpo */ }
    throw new Error(msg);
  }
  return r.json();
}

let toastT;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 3200);
}
const fail = (e) => toast(e.message || "Algo salió mal", true);
const fmtTs = (ts) => (ts || "").replace("T", " ").slice(0, 19);
const zoneOf = (d) => d.zona || "Sin zona";
const label = (d) => `${zoneOf(d)} · ${d.nombre}`;
const devById = (id) => S.devs.find((d) => d.id === id);

/* ============ Navegación ============ */
function show(view) {
  S.view = view;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#view-title").textContent = TITULOS[view];
  S.sig = "";
  if (view === "vivo") { liveAttach(true); loadLive(); } else liveAttach(false, true);
  if (view === "transmisiones") renderWall(true);
  if (view === "dispositivos") renderDevices();
  if (view === "reportes") loadReports();
  if (view === "ajustes") loadSettings();
  window.scrollTo(0, 0);
}
$("#nav").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) show(b.dataset.view); });
document.addEventListener("click", (e) => { const g = e.target.closest("[data-go]"); if (g) { e.preventDefault(); show(g.dataset.go); } });

/* ============ Estado general (cada 1.5 s) ============ */
async function tick() {
  try {
    const j = await api("/api/dispositivos");
    S.devs = j.dispositivos; S.global = j.deteccion_global; S.motor = j.motor_activo; $("#legacy-banner").hidden = !j.motor_viejo;
    const sig = JSON.stringify(S.devs.map((d) => [d.id, d.nombre, d.zona, d.habilitado, d.deteccion, d.estado, d.direccion]));
    const changed = sig !== S.sig; S.sig = sig;
    renderCommon();
    if (S.view === "vivo") renderLive();
    if (S.view === "transmisiones") renderWall(changed);
    if (S.view === "dispositivos" && changed && !document.activeElement?.closest("#devices")) renderDevices();
  } catch (e) {
    S.motor = false; $("#motor-chip").innerHTML = '<i class="dot bad"></i><span>Panel sin conexión</span>';
  }
}

function renderCommon() {
  const n = S.devs.filter((d) => d.estado === "en_linea").length;
  const b = $("#nav-count"); b.textContent = n; b.classList.toggle("zero", n === 0);
  $("#motor-chip").innerHTML = S.motor ? '<i class="dot ok"></i><span>Motor activo</span>' : '<i class="dot bad"></i><span>Motor detenido</span>';
  const sw = $("#sw-global"); sw.checked = S.global; sw.closest(".master").classList.toggle("off", !S.global);
}

$("#sw-global").addEventListener("change", async (e) => {
  const v = e.target.checked;
  try {
    await api("/api/global", { method: "PUT", body: { deteccion_global: v } });
    S.global = v; renderCommon();
    toast(v ? "Reconocimiento de EPP activado" : "Reconocimiento de EPP desactivado en todos los dispositivos");
  } catch (err) { e.target.checked = !v; fail(err); }
});

$("#btn-sound").addEventListener("click", () => {
  S.sound = !S.sound; store.set("argos-sound", S.sound ? "1" : "0"); paintSound();
  if (S.sound) beep();
});
function paintSound() {
  $("#btn-sound").innerHTML = ico(S.sound ? "volume-2" : "volume-x") + "<span>Sonido</span>";
}
let actx;
function beep() {
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    const o = actx.createOscillator(), g = actx.createGain();
    o.type = "square"; o.frequency.value = 880; g.gain.value = 0.06;
    o.connect(g); g.connect(actx.destination); o.start(); o.stop(actx.currentTime + 0.25);
  } catch { /* sin audio */ }
}

/* ============ En vivo ============ */
let attached = "";
function liveAttach(on, detach) {
  const img = $("#video");
  if (detach || !on) { img.removeAttribute("src"); attached = ""; return; }
  if (S.sel && attached !== S.sel) { img.src = `/video/${S.sel}`; attached = S.sel; }
}

function pickSelection() {
  const enabled = S.devs.filter((d) => d.habilitado);
  if (!devById(S.sel) || (!devById(S.sel).habilitado && enabled.length)) S.sel = (enabled[0] || S.devs[0] || {}).id || "";
}

function renderLive() {
  pickSelection();
  const enabled = S.devs.filter((d) => d.habilitado);
  const chips = $("#live-chips");
  const html = enabled.map((d) => `<button class="chip ${d.id === S.sel ? "on" : ""}" data-id="${esc(d.id)}"><i class="dot ${d.estado === "en_linea" ? "ok" : "warn"}"></i>${esc(label(d))}</button>`).join("")
    || '<span class="soft">No hay dispositivos encendidos. Ve a Dispositivos.</span>';
  if (chips.dataset.h !== html) { chips.innerHTML = html; chips.dataset.h = html; }
  liveAttach(true);

  const d = devById(S.sel);
  const chip = $("#status-chip"), ov = $("#video-overlay");
  if (!d) { chip.className = "status-chip neutral"; chip.textContent = "Sin dispositivo"; ov.hidden = false; $("#overlay-title").textContent = "Sin dispositivo"; $("#overlay-msg").textContent = "Agrega o enciende una cámara en Dispositivos."; return; }
  $("#video-cam").textContent = label(d);
  $("#video-badge .dot, #video-badge i").className = "dot " + (d.estado === "en_linea" ? "ok" : "warn");
  const sw = $("#sw-live-det"); sw.checked = d.deteccion; sw.disabled = !d.habilitado;

  const online = d.estado === "en_linea";
  ov.hidden = online;
  if (!online) {
    $("#overlay-title").textContent = d.estado === "apagado" ? "Transmisión apagada" : d.estado === "motor_detenido" ? "Motor detenido" : "Sin señal";
    $("#overlay-msg").textContent = d.estado === "motor_detenido" ? "Inicia motor_epp.py en la laptop." : d.mensaje || "";
    chip.className = "status-chip neutral"; chip.textContent = d.estado === "apagado" ? "Apagada" : "Esperando señal…";
    return;
  }
  if (!d.deteccion_efectiva) { chip.className = "status-chip off"; chip.textContent = "Reconocimiento desactivado · solo video"; return; }
  if (d.faltantes.length) {
    chip.className = "status-chip bad"; chip.textContent = "Falta: " + d.faltantes.join(", ");
    if (S.sound && Date.now() - S.lastBeep > 4000) { S.lastBeep = Date.now(); beep(); }
  } else if (d.personas > 0) { chip.className = "status-chip ok"; chip.textContent = `EPP completo · ${d.personas} persona${d.personas > 1 ? "s" : ""}`; }
  else { chip.className = "status-chip neutral"; chip.textContent = "Sin personas en cuadro"; }
}

$("#live-chips").addEventListener("click", (e) => {
  const b = e.target.closest(".chip"); if (!b) return;
  S.sel = b.dataset.id; store.set("argos-sel", S.sel); $("#live-chips").dataset.h = ""; renderLive();
});
$("#sw-live-det").addEventListener("change", (e) => setDev(S.sel, { deteccion: e.target.checked }));

async function loadLive() {
  try {
    const [r, ev] = await Promise.all([api("/api/resumen?dias=7"), api("/api/eventos?limit=6")]);
    $("#kpi-hoy").textContent = r.hoy.total; $("#kpi-casco").textContent = r.hoy.por_epp.casco || 0; $("#kpi-chaleco").textContent = r.hoy.por_epp.chaleco || 0;
    const max = Math.max(1, ...r.serie.map((s) => s.total));
    $("#bars").innerHTML = r.serie.map((s) => `<div class="bar-col"><b>${s.total}</b><i style="height:${Math.round((s.total / max) * 70)}px"></i><span>${s.dia.slice(8)}/${s.dia.slice(5, 7)}</span></div>`).join("");
    $("#alerts").innerHTML = ev.eventos.length ? ev.eventos.map((e) => `<li>${e.foto_url ? `<img src="${e.foto_url}" data-big="${e.foto_url}" alt="">` : ""}<div><div>${e.faltantes.map((f) => `<span class="tag">${esc(f)}</span>`).join("")}</div><div class="tt">${esc(e.zona || "Sin zona")} · ${esc(e.camara_nombre)}</div><div class="muted">${esc(fmtTs(e.ts))}</div></div></li>`).join("") : '<li class="muted empty">Aún no hay alertas.</li>';
  } catch { /* se reintenta */ }
}
setInterval(() => { if (S.view === "vivo") loadLive(); }, 5000);
document.addEventListener("click", (e) => { const im = e.target.closest("[data-big]"); if (im) { $("#img-big").src = im.dataset.big; $("#img-dialog").showModal(); } });
$("#img-dialog").addEventListener("click", () => $("#img-dialog").close());

/* ============ Transmisiones ============ */
function renderWall(force) {
  const tx = S.devs.filter((d) => d.habilitado);
  const zones = [...new Set(tx.map(zoneOf))].sort();
  if (S.zone && !zones.includes(S.zone)) S.zone = "";
  const zc = $("#zone-chips");
  zc.innerHTML = zones.length > 1 ? [`<button class="chip ${S.zone ? "" : "on"}" data-z="">Todas las zonas</button>`, ...zones.map((z) => `<button class="chip ${S.zone === z ? "on" : ""}" data-z="${esc(z)}">${ico("map-pin")}${esc(z)}</button>`)].join("") : "";
  const list = tx.filter((d) => !S.zone || zoneOf(d) === S.zone);
  const n = S.devs.filter((d) => d.estado === "en_linea").length;
  $("#tx-sub").textContent = `${n} en línea de ${S.devs.length} dispositivos · ${tx.length} con transmisión encendida`;
  $("#wall-empty").hidden = list.length > 0;
  const wall = $("#wall");
  const key = list.map((d) => d.id).join(",") + "|" + S.zone;
  if (force || wall.dataset.k !== key) {
    wall.dataset.k = key;
    wall.innerHTML = list.map((d) => `<div class="tile" data-id="${esc(d.id)}">
      <div class="tile-v viewfinder vf-b"><img alt="" hidden><div class="nosig">${ico("video-off")}<span>Conectando…</span></div>
        <div class="tile-live"><i class="dot"></i><span class="tl-t">—</span></div><div class="tile-warn" hidden></div></div>
      <div class="tile-f"><div class="tile-n"><b>${esc(d.nombre)}</b><span>${ico("map-pin")} ${esc(zoneOf(d))}</span></div>
        <div class="row"><button class="icon-btn det" title="Reconocimiento de EPP"></button><button class="icon-btn pw" title="Apagar transmisión">${ico("power")}</button></div></div></div>`).join("");
  }
  list.forEach((d) => {
    const t = wall.querySelector(`.tile[data-id="${CSS.escape(d.id)}"]`); if (!t) return;
    const on = d.estado === "en_linea", alert = on && d.deteccion_efectiva && d.faltantes.length > 0;
    t.classList.toggle("alert", alert);
    t.querySelector(".tile-live .dot").className = "dot " + (on ? "ok" : "warn");
    t.querySelector(".tl-t").textContent = on ? (d.deteccion_efectiva ? `${d.fps.toFixed(0)} FPS` : "SOLO VIDEO") : d.estado === "motor_detenido" ? "MOTOR OFF" : "SIN SEÑAL";
    const w = t.querySelector(".tile-warn"); w.hidden = !alert; if (alert) w.textContent = "Falta: " + d.faltantes.join(", ");
    const det = t.querySelector(".det"); det.innerHTML = ico(d.deteccion ? "eye" : "eye-off"); det.classList.toggle("off", !d.deteccion);
    det.title = d.deteccion ? "Reconocimiento activo (clic para desactivar)" : "Reconocimiento desactivado (clic para activar)";
    t.querySelector(".nosig span").textContent = d.mensaje || "Sin señal";
  });
}
$("#zone-chips").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) { S.zone = b.dataset.z; renderWall(true); } });
$("#wall").addEventListener("click", (e) => {
  const t = e.target.closest(".tile"); if (!t) return;
  const id = t.dataset.id;
  if (e.target.closest(".det")) return setDev(id, { deteccion: !devById(id).deteccion });
  if (e.target.closest(".pw")) return setDev(id, { habilitado: false });
  S.sel = id; store.set("argos-sel", id); show("vivo");
});
// Cada cuadro se pide como imagen suelta: el navegador limita ~6 videos continuos, y así caben muchas cámaras.
setInterval(() => {
  if (S.view !== "transmisiones" || document.hidden) return;
  $$("#wall .tile").forEach((t) => {
    const id = t.dataset.id, d = devById(id); if (!d || d.estado !== "en_linea") { t.querySelector("img").hidden = true; t.querySelector(".nosig").hidden = false; return; }
    if (t.dataset.busy) return; t.dataset.busy = "1";
    const im = new Image();
    im.onload = () => { const v = t.querySelector("img"); v.src = im.src; v.hidden = false; t.querySelector(".nosig").hidden = true; delete t.dataset.busy; };
    im.onerror = () => { t.querySelector("img").hidden = true; t.querySelector(".nosig").hidden = false; delete t.dataset.busy; };
    im.src = `/api/live/${id}.jpg?t=${Date.now()}`;
  });
}, 700);

async function setDev(id, patch) {
  try { await api(`/api/dispositivos/${id}`, { method: "PATCH", body: patch }); Object.assign(devById(id) || {}, patch); S.sig = ""; await tick(); }
  catch (e) { fail(e); renderDevices(); }
}

/* ============ Dispositivos ============ */
let found = [];
function hash(s) { let h = 2166136261; for (const c of s) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }
function blipPos(key) { const h = hash(key), a = ((h % 3600) / 3600) * Math.PI * 2, r = 0.42 + (((h >> 8) % 100) / 100) * 0.46; return [50 + Math.cos(a) * r * 50, 50 + Math.sin(a) * r * 50]; }

function renderBlips() {
  const items = [
    ...S.devs.map((d) => ({ key: d.id, t: d.tipo, name: label(d), cls: d.estado === "en_linea" ? "" : "off" })),
    ...found.filter((f) => !f.agregado).map((f) => ({ key: "f" + f.ip + f.tipo, t: f.tipo, name: f.nombre + " (nuevo)", cls: "new" })),
  ];
  $("#blips").innerHTML = items.map((b) => { const [x, y] = blipPos(b.key); return `<div class="blip ${b.cls}" style="left:${x}%;top:${y}%">${ico(TIPOS[b.t]?.icon || "camera")}<span class="tip">${esc(b.name)}</span></div>`; }).join("");
}

function statePill(d) {
  const m = { en_linea: ["ok", "En línea"], apagado: ["", "Apagado"], sin_senal: ["bad", "Sin señal"], motor_detenido: ["warn", "Motor detenido"] }[d.estado] || ["", d.estado];
  return `<span class="pill ${m[0]}">${m[1]}</span>`;
}

function renderDevices() {
  renderBlips();
  const box = $("#devices");
  if (!S.devs.length) { box.innerHTML = '<div class="card empty">Aún no hay dispositivos. Usa «Buscar dispositivos» o «Vincular celular».</div>'; renderFound(); return; }
  const groups = {};
  S.devs.forEach((d) => (groups[zoneOf(d)] ||= []).push(d));
  box.innerHTML = Object.keys(groups).sort().map((z) => `<div class="zone"><div class="zone-h">${ico("map-pin")}${esc(z)} <small>${groups[z].length} dispositivo${groups[z].length > 1 ? "s" : ""}</small></div>
    ${groups[z].map((d) => `<div class="dev ${d.habilitado ? "" : "off"}" data-id="${esc(d.id)}">
      <div class="dev-ico">${ico(TIPOS[d.tipo]?.icon || "camera")}</div>
      <div class="dev-n"><b>${esc(d.nombre)}</b><div class="sub">${esc(TIPOS[d.tipo]?.label || d.tipo)}${d.direccion ? ` · <span class="mono">${esc(d.direccion)}</span>` : ""}</div><div style="margin-top:4px">${statePill(d)}</div></div>
      <div class="dev-sw">
        <label>Transmisión<input type="checkbox" class="sw" data-k="habilitado" ${d.habilitado ? "checked" : ""}></label>
        <label>Reconocimiento EPP<input type="checkbox" class="sw" data-k="deteccion" ${d.deteccion ? "checked" : ""}></label>
      </div>
      <div class="dev-act">
        ${d.tipo === "celular" ? `<button class="btn small" data-a="link">${ico("qr-code")}Vincular</button>` : ""}
        ${d.tipo !== "celularweb" && d.tipo !== "usb" ? `<button class="icon-btn" data-a="test" title="Probar conexión">${ico("play")}</button>` : ""}
        <button class="icon-btn" data-a="edit" title="Editar">${ico("pencil")}</button>
        <button class="icon-btn danger" data-a="del" title="Eliminar">${ico("trash-2")}</button>
      </div></div>`).join("")}</div>`).join("");
  renderFound();
}

$("#devices").addEventListener("change", (e) => {
  const sw = e.target.closest(".sw"); if (!sw) return;
  setDev(sw.closest(".dev").dataset.id, { [sw.dataset.k]: sw.checked });
});
$("#devices").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-a]"); if (!b) return;
  const id = b.closest(".dev").dataset.id, d = devById(id);
  if (b.dataset.a === "link") return openQR(d.zona === "Sin zona" ? "" : d.zona, "");
  if (b.dataset.a === "edit") return openDevDialog(d);
  if (b.dataset.a === "del") {
    if (!confirm(`¿Eliminar «${d.nombre}»? Las alertas anteriores se conservan.`)) return;
    try { await api(`/api/dispositivos/${id}`, { method: "DELETE" }); toast("Dispositivo eliminado"); S.sig = ""; tick(); } catch (err) { fail(err); }
  }
  if (b.dataset.a === "test") {
    b.disabled = true;
    try { const r = await api(`/api/dispositivos/${id}/probar`, { method: "POST" }); toast(r.detalle, r.ok === false); } catch (err) { fail(err); }
    b.disabled = false;
  }
});

/* ---- Radar: buscar en la red ---- */
let scanTimer;
$("#btn-scan").addEventListener("click", async () => {
  try {
    const job = await api("/api/escaneo", { method: "POST" });
    $("#radar").classList.add("scanning"); $("#scan-bar").hidden = false; $("#btn-scan").disabled = true;
    pollScan(job.id);
  } catch (e) { fail(e); }
});
async function pollScan(id) {
  try {
    const j = await api(`/api/escaneo/${id}`);
    found = j.encontrados; renderFound(); renderBlips();
    const pct = j.total ? Math.round((j.hechos / j.total) * 100) : 100;
    $("#scan-bar i").style.width = pct + "%";
    $("#radar-title").textContent = j.estado === "buscando" ? "Escaneando la red…" : "Búsqueda terminada";
    $("#radar-sub").textContent = j.total ? `${j.red} · ${j.hechos}/${j.total} equipos · ${found.length} encontrados` : "No pude detectar la red local. ¿Estás conectado al WiFi?";
    if (j.estado === "buscando") { scanTimer = setTimeout(() => pollScan(id), 700); return; }
    $("#radar").classList.remove("scanning"); $("#scan-bar").hidden = true; $("#btn-scan").disabled = false;
    if (j.total && !found.length) toast("No encontré cámaras. Revisa que estén encendidas y en la misma red.");
  } catch (e) { $("#radar").classList.remove("scanning"); $("#btn-scan").disabled = false; fail(e); }
}
function renderFound() {
  const ul = $("#found");
  $("#found-n").textContent = found.length ? `· ${found.length}` : "";
  if (!found.length) return;
  ul.innerHTML = found.map((f, i) => `<li><div class="dev-ico">${ico(TIPOS[f.tipo]?.icon || "camera")}</div>
    <div class="info"><b>${esc(f.nombre)}</b><span>${esc(TIPOS[f.tipo]?.label || f.tipo)} · <span class="mono">${esc(f.direccion)}</span>${f.detalle ? " · " + esc(f.detalle) : ""}</span></div>
    ${f.agregado ? '<span class="pill ok">Ya agregado</span>' : `${f.tipo === "celular" ? `<button class="btn small" data-f="link">${ico("qr-code")}Vincular</button>` : ""}<button class="btn small accent" data-f="add" data-i="${i}">${ico("plus")}Agregar</button>`}</li>`).join("");
}
$("#found").addEventListener("click", (e) => {
  const b = e.target.closest("[data-f]"); if (!b) return;
  if (b.dataset.f === "link") return openQR("", "");
  const f = found[+b.dataset.i];
  openDevDialog({ nombre: f.nombre, tipo: f.tipo, direccion: f.direccion, zona: f.zona || "" }, true);
});

/* ---- Diálogo agregar / editar ---- */
const dlg = $("#dev-dialog");
function hint() { $("#d-hint").textContent = HINTS[$("#d-tipo").value] || ""; }
$("#d-tipo").addEventListener("change", hint);
async function zonesList() { try { $("#zonas").innerHTML = (await api("/api/zonas")).map((z) => `<option value="${esc(z)}">`).join(""); } catch { /* opcional */ } }
function openDevDialog(d, isNew) {
  zonesList();
  const web = d && d.tipo === "celularweb";
  $("#dev-title").textContent = d && !isNew ? "Editar dispositivo" : "Agregar dispositivo";
  $("#d-id").value = d && !isNew ? d.id : "";
  $("#d-nombre").value = d?.nombre || ""; $("#d-tipo").value = d?.tipo && TIPOS[d.tipo] && !web ? d.tipo : "esp32cam";
  $("#d-direccion").value = d?.direccion || ""; $("#d-zona").value = d && d.zona && d.zona !== "Sin zona" ? d.zona : "";
  $("#d-tipo").closest("label").hidden = web; $("#d-direccion").closest("label").hidden = web; $("#d-direccion").required = !web;
  hint(); dlg.showModal();
}
$(".quick").addEventListener("click", (e) => {
  const b = e.target.closest(".qk"); if (!b) return;
  const q = b.dataset.q;
  if (q === "celularqr") return openQR("", "");
  const pre = { usb: { nombre: "Webcam de la laptop", tipo: "usb", direccion: "0" }, esp32cam: { nombre: "ESP32-S3-CAM", tipo: "esp32cam", direccion: "epp-cam01.local" }, rtsp: { nombre: "Cámara IP", tipo: "rtsp", direccion: "" } }[q];
  openDevDialog(pre, true);
});
$("#d-direccion").addEventListener("input", (e) => { if (/^\d{1,2}$/.test(e.target.value.trim())) { $("#d-tipo").value = "usb"; hint(); } });
$("#d-tipo").addEventListener("change", () => { if ($("#d-tipo").value === "usb" && !$("#d-direccion").value.trim()) $("#d-direccion").value = "0"; });
$("#d-cancel").addEventListener("click", () => dlg.close());
$("#dev-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#d-id").value, cur = id ? devById(id) : null;
  try {
    if (cur && cur.tipo === "celularweb") await api(`/api/dispositivos/${id}`, { method: "PATCH", body: { nombre: $("#d-nombre").value, zona: $("#d-zona").value } });
    else await api("/api/dispositivos", { method: "POST", body: { id: id || null, nombre: $("#d-nombre").value, tipo: $("#d-tipo").value, direccion: $("#d-direccion").value, zona: $("#d-zona").value || "Sin zona" } });
    dlg.close(); toast("Dispositivo guardado");
    if (!id && found.length) { found.forEach((f) => { if (String(f.direccion).toLowerCase() === $("#d-direccion").value.trim().toLowerCase() && f.tipo === $("#d-tipo").value) f.agregado = true; }); }
    S.sig = ""; await tick(); renderDevices();
  } catch (err) { fail(err); }
});

/* ---- Vincular celular por QR ---- */
const qd = $("#qr-dialog");
let qrTimer, qrCode = "";
function openQR(zona, nombre) {
  zonesList(); clearTimeout(qrTimer);
  $("#q-zona").value = zona || ""; $("#q-nombre").value = nombre || "";
  $("#qr-step1").hidden = false; $("#qr-step2").hidden = true; qd.showModal();
}
$("#btn-link").addEventListener("click", () => openQR("", ""));
$("#qr-close").addEventListener("click", () => qd.close());
qd.addEventListener("close", () => { clearTimeout(qrTimer); qrCode = ""; });
$("#qr-go").addEventListener("click", async () => {
  try {
    const j = await api("/api/vincular", { method: "POST", body: { nombre: $("#q-nombre").value, zona: $("#q-zona").value } });
    qrCode = j.codigo;
    const qr = qrcode(0, "M"); qr.addData(j.url); qr.make();
    $("#qr-img").innerHTML = qr.createSvgTag({ cellSize: 6, margin: 0, scalable: true });
    $("#qr-url").textContent = j.url;
    $("#qr-note").textContent = j.https ? "En el celular: escanea el QR con la cámara. Si el navegador avisa que la conexión no es privada, elige «Avanzado» y «Continuar»: el certificado es de tu propia laptop y solo se acepta una vez."
      : "Esta laptop aún no tiene HTTPS: el celular no podrá abrir su cámara. Instala «pip install cryptography» y reinicia el panel.";
    setQrState("", "loader-circle", "Esperando que el celular escanee…");
    $("#qr-step1").hidden = true; $("#qr-step2").hidden = false;
    pollQR(j.codigo);
  } catch (e) { fail(e); }
});
function setQrState(cls, icon, text) { const s = $("#qr-state"); s.className = "qr-state " + cls; s.innerHTML = `${ico(icon, cls ? "" : "spin")}<span>${esc(text)}</span>`; }
async function pollQR(code) {
  if (code !== qrCode) return;
  try {
    const j = await api(`/api/vincular/${code}`);
    if (j.estado === "vinculado") { setQrState("ok", "circle-check", `Vinculado: ${j.zona} · ${j.nombre}`); toast("Celular vinculado"); S.sig = ""; tick(); setTimeout(() => qd.open && qd.close(), 1800); return; }
    if (j.estado === "expirado") { setQrState("bad", "circle-alert", "El QR venció. Genera uno nuevo."); return; }
  } catch { /* reintenta */ }
  qrTimer = setTimeout(() => pollQR(code), 1500);
}

/* ============ Reportes ============ */
function ymd(d) { return d.toISOString().slice(0, 10); }
function repParams(extra = {}) {
  const p = new URLSearchParams();
  const v = { desde: $("#f-desde").value, hasta: $("#f-hasta").value, camara: $("#f-camara").value, zona: $("#f-zona").value, epp: $("#f-epp").value, estado: $("#f-estado").value, ...extra };
  Object.entries(v).forEach(([k, x]) => x && p.set(k, x));
  return p;
}
function fillFilterLists() {
  const z = $("#f-zona"), c = $("#f-camara"), zv = z.value, cv = c.value;
  z.innerHTML = '<option value="">Todas</option>' + [...new Set(S.devs.map(zoneOf))].sort().map((x) => `<option>${esc(x)}</option>`).join("");
  c.innerHTML = '<option value="">Todos</option>' + S.devs.map((d) => `<option value="${esc(d.id)}">${esc(label(d))}</option>`).join("");
  z.value = zv; c.value = cv;
}
async function loadReports() {
  if (!$("#f-desde").value) { const h = new Date(), d = new Date(Date.now() - 6 * 864e5); $("#f-desde").value = ymd(d); $("#f-hasta").value = ymd(h); }
  fillFilterLists(); S.evLimit = 50;
  try {
    const q = new URLSearchParams({ desde: $("#f-desde").value, hasta: $("#f-hasta").value });
    const r = await api("/api/resumen?" + q);
    const zona = $("#f-zona").value;
    const list = r.por_dispositivo.filter((x) => !zona || x.zona === zona);
    $("#rep-cards").innerHTML = list.map((x) => {
      const t = TIPOS[x.tipo] || TIPOS.esp32cam;
      const badges = Object.entries(x.por_epp).map(([k, n]) => `<span class="tag">${esc(k)} ${n}</span>`).join("") || '<span class="muted">Sin alertas</span>';
      return `<div class="rep ${S.repDev === x.id ? "sel" : ""}"><div class="rep-h"><div class="dev-ico">${ico(t.icon)}</div><div><b>${esc(x.nombre)}</b><span>${ico("map-pin")} ${esc(x.zona || "Sin zona")}</span></div></div>
        <div class="rep-n">${x.total}<small>alertas</small></div><div class="rep-b">${badges}</div>
        <div class="rep-f"><span>${x.ultima ? "Última: " + esc(fmtTs(x.ultima)) : "Sin registros"}</span><span class="row"><button class="btn small" data-r="ver" data-id="${esc(x.id)}">Ver</button><button class="btn small ghost" data-r="csv" data-id="${esc(x.id)}">${ico("download")}CSV</button></span></div></div>`;
    }).join("") || '<div class="card muted">No hay dispositivos.</div>';
  } catch (e) { fail(e); }
  loadEvents();
}
async function loadEvents() {
  try {
    const p = repParams({ limit: S.evLimit }); const j = await api("/api/eventos?" + p);
    const dev = $("#f-camara").value ? devById($("#f-camara").value) : null;
    $("#ev-title").textContent = dev ? `Eventos · ${label(dev)}` : "Eventos";
    $("#tbody").innerHTML = j.eventos.map((e) => `<tr><td>${e.foto_url ? `<img src="${e.foto_url}" data-big="${e.foto_url}" loading="lazy" alt="Evidencia">` : "—"}</td>
      <td class="ts">${esc(fmtTs(e.ts))}</td><td><b>${esc(e.zona || "Sin zona")}</b> · ${esc(e.camara_nombre)}</td>
      <td>${e.faltantes.map((f) => `<span class="tag">${esc(f)}</span>`).join("")}</td><td class="mono">${Math.round((e.confianza || 0) * 100)}%</td>
      <td class="no-print"><select data-ev="${e.id}">${[["alertado", "Alertado"], ["revisado", "Revisado"], ["falso_positivo", "Falso positivo"]].map(([v, t]) => `<option value="${v}" ${e.estado === v ? "selected" : ""}>${t}</option>`).join("")}</select></td></tr>`).join("");
    $("#tbl-empty").hidden = j.eventos.length > 0;
    $("#tbl-count").textContent = j.total ? `Mostrando ${j.eventos.length} de ${j.total}` : "";
    $("#btn-more").hidden = j.eventos.length >= j.total;
  } catch (e) { fail(e); }
}
$("#btn-filter").addEventListener("click", () => { S.repDev = $("#f-camara").value; loadReports(); });
$("#btn-more").addEventListener("click", () => { S.evLimit += 50; loadEvents(); });
$("#rep-cards").addEventListener("click", (e) => {
  const b = e.target.closest("[data-r]"); if (!b) return;
  if (b.dataset.r === "ver") { $("#f-camara").value = b.dataset.id; S.repDev = b.dataset.id; S.evLimit = 50; loadReports(); $("#ev-title").scrollIntoView({ behavior: "smooth" }); }
  else location.href = "/api/eventos.csv?" + repParams({ camara: b.dataset.id });
});
$("#tbody").addEventListener("change", async (e) => {
  const s = e.target.closest("select[data-ev]"); if (!s) return;
  try { await api(`/api/eventos/${s.dataset.ev}`, { method: "PATCH", body: { estado: s.value } }); toast("Estado actualizado"); } catch (err) { fail(err); }
});
$("#btn-csv").addEventListener("click", () => (location.href = "/api/eventos.csv?" + repParams()));
$("#btn-print").addEventListener("click", () => {
  const dev = $("#f-camara").value ? devById($("#f-camara").value) : null;
  $("#print-head").innerHTML = `<h2>ARGOS EPP — Reporte de alertas${dev ? ": " + esc(label(dev)) : ""}</h2><p>Del ${esc($("#f-desde").value)} al ${esc($("#f-hasta").value)} · generado ${esc(new Date().toLocaleString("es-PE"))}</p>`;
  window.print();
});

/* ============ Ajustes ============ */
async function loadSettings() {
  try {
    const [a, info] = await Promise.all([api("/api/ajustes"), api("/api/info")]);
    $("#req-checks").innerHTML = a.epp_validos.map((k) => `<label><input type="checkbox" value="${k}" ${a.requeridos.includes(k) ? "checked" : ""}>${k}</label>`).join("");
    $("#s-actuador").value = a.actuador || "";
    $("#lan-url").textContent = info.url_panel;
  } catch (e) { fail(e); }
}
$("#btn-save-set").addEventListener("click", async () => {
  try {
    await api("/api/ajustes", { method: "PUT", body: { requeridos: $$("#req-checks input:checked").map((i) => i.value), actuador: $("#s-actuador").value } });
    $("#set-msg").textContent = "Guardado. El motor lo aplica en unos segundos.";
  } catch (e) { fail(e); }
});

/* ============ Inicio ============ */
(async function init() {
  paintSound();
  try { const i = await api("/api/info"); $("#side-url").textContent = i.url_panel.replace("http://", ""); } catch { /* opcional */ }
  await tick(); pickSelection(); show("vivo");
  setInterval(tick, 1500);
})();
