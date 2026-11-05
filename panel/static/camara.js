/* ARGOS EPP — página del celular: envía su cámara al panel por la red local. */
"use strict";
const $ = (s) => document.querySelector(s);
const code = new URLSearchParams(location.search).get("c") || "";
const KEY = "argos-cam";
let stream = null, running = false, facing = "environment", reg = null, wake = null, sent = 0, t0 = 0;

function showErr(m) { const e = $("#err"); e.textContent = m; e.hidden = !m; }
function getSaved() { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; } }
function save(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* sin almacenamiento */ } }

(async function init() {
  if (!window.isSecureContext || !navigator.mediaDevices) {
    const https = location.href.replace(/^http:\/\/([^:/]+)(:\d+)?/, (_, h) => `https://${h}:8443`);
    showErr("Este navegador solo permite la cámara en una conexión segura (https). Abre esta dirección y acepta la advertencia del certificado: " + https);
    $("#go").disabled = true; return;
  }
  if (code) {
    try {
      const r = await fetch(`/api/vincular/${encodeURIComponent(code)}`); const j = await r.json();
      if (j.estado === "expirado") { showErr("El QR venció. Genera uno nuevo en el panel (Dispositivos → Vincular celular)."); if (!getSaved()) $("#go").disabled = true; }
      if (j.nombre) $("#nombre").value = j.nombre; if (j.zona) $("#zona").value = j.zona;
    } catch { showErr("No pude hablar con el panel. ¿Estás en la misma red WiFi que la laptop?"); }
  } else if (!getSaved()) { showErr("Falta el código. Escanea el QR que muestra el panel."); $("#go").disabled = true; }
  const s = getSaved(); if (s && !code) { reg = s; $("#nombre").value = s.nombre || ""; $("#zona").value = s.zona || ""; $("#go").textContent = "Reanudar transmisión"; }
})();

async function openCam() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 720 } } });
  const v = $("#v"); v.srcObject = stream; await v.play().catch(() => {});
}

$("#go").addEventListener("click", async () => {
  showErr(""); $("#go").disabled = true;
  try {
    await openCam();
    if (!reg || code) {
      const r = await fetch(`/api/vincular/${encodeURIComponent(code)}/registrar`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ nombre: $("#nombre").value, zona: $("#zona").value }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "No pude vincular");
      reg = await r.json(); save(reg); history.replaceState(null, "", location.pathname);
    }
    $("#setup").hidden = true; $("#run").hidden = false; $("#run").style.display = "grid";
    $("#info").textContent = `${reg.zona} · ${reg.nombre}. Deja esta pantalla encendida.`;
    running = true; sent = 0; t0 = performance.now(); keepAwake(); loop();
  } catch (e) {
    $("#go").disabled = false;
    showErr(e.name === "NotAllowedError" ? "Permiso de cámara denegado. Habilítalo en el candado del navegador y vuelve a intentar." : e.message || "No se pudo iniciar la cámara");
  }
});

async function keepAwake() { try { wake = await navigator.wakeLock?.request("screen"); } catch { /* opcional */ } }
document.addEventListener("visibilitychange", () => { if (running && document.visibilityState === "visible") keepAwake(); });

function setLive(cls, txt) { $("#live").className = "live " + cls; $("#ltxt").textContent = txt; }

async function loop() {
  const v = $("#v"), c = document.createElement("canvas"), ctx = c.getContext("2d");
  while (running) {
    const t = performance.now();
    try {
      if (v.videoWidth) {
        const w = 640, h = Math.round((v.videoHeight / v.videoWidth) * w); c.width = w; c.height = h; ctx.drawImage(v, 0, 0, w, h);
        const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.6));
        const r = await fetch(`/api/ingest/${reg.id}`, { method: "POST", headers: { "X-Token": reg.token }, body: blob });
        if (r.status === 403 || r.status === 404) { running = false; localStorage.removeItem(KEY); showErr("El panel ya no reconoce este celular (lo eliminaron). Vincula de nuevo con un QR nuevo."); break; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        sent++; setLive("ok", `Transmitiendo · ${(sent / ((performance.now() - t0) / 1000)).toFixed(1)} fps`);
      }
    } catch { setLive("bad", "Sin conexión con el panel…"); await new Promise((r) => setTimeout(r, 1000)); }
    const wait = 125 - (performance.now() - t); if (wait > 0) await new Promise((r) => setTimeout(r, wait));
  }
}

$("#flip").addEventListener("click", async () => { facing = facing === "environment" ? "user" : "environment"; try { await openCam(); } catch (e) { showErr(e.message); } });
$("#stop").addEventListener("click", () => {
  running = false; if (stream) stream.getTracks().forEach((t) => t.stop()); try { wake?.release(); } catch { /* ok */ }
  $("#run").hidden = true; $("#setup").hidden = false; $("#go").disabled = false; $("#go").textContent = "Reanudar transmisión";
});
