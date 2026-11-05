"""
============================================================
ARGOS EPP - Panel de control (FastAPI) v2
============================================================
Ubicacion:  D:\\ARGOS-EPP\\panel\\servidor.py

Que hace:
    - TRANSMISIONES: todas las camaras en transmision a la vez, con su zona y su estado.
    - DISPOSITIVOS: radar que busca camaras en la red (ESP32-S3-CAM, IP Webcam, DroidCam, RTSP),
      lista por zona, vincular un celular escaneando un QR, y switches por dispositivo:
      transmision (prende/apaga) y reconocimiento de EPP (on/off).
    - REPORTES: alertas con foto, resumen por dispositivo, CSV e impresion/PDF.
    - Switch general del reconocimiento de EPP.

El panel y el motor (motor_epp.py) se comunican por archivos, asi funcionan por separado:
    D:\\ARGOS-EPP\\config\\dispositivos.json    (el panel escribe, el motor lee)
    D:\\ARGOS-EPP\\live\\<id>.jpg, estado.json   (el motor escribe, el panel lee)
    D:\\ARGOS-EPP\\eventos.db y \\evidencias      (el motor escribe, el panel lee)

Uso (con el entorno virtual activado):
    pip install -r requirements.txt
    python servidor.py
        http://localhost:8000                 panel (laptop)
        https://IP-DE-LA-LAPTOP:8443/camara   pagina del celular (la camara del celular exige https)
============================================================
"""

import argparse
import asyncio
import copy
import csv
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PANEL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PANEL_DIR.parent
STATIC_DIR = PANEL_DIR / "static"
CONFIG_PATH = PROJECT_DIR / "config" / "dispositivos.json"
CERT_DIR = PROJECT_DIR / "config" / "cert"
LIVE_DIR = PROJECT_DIR / "live"
DB_PATH = PROJECT_DIR / "eventos.db"
EVIDENCIAS_DIR = PROJECT_DIR / "evidencias"

TIPOS = ("esp32cam", "celular", "celularweb", "rtsp", "usb", "url")
EPP_VALIDOS = ("casco", "chaleco", "lentes", "guantes", "botas")
ESTADOS = ("alertado", "revisado", "falso_positivo")
FRESCO_S = 5.0          # un cuadro/estado mas viejo que esto se considera "sin senal"
CODIGO_TTL_S = 600      # el QR de vinculacion dura 10 minutos

DEFAULT_CONFIG = {
    "version": 2,
    "deteccion_global": True,
    "panel_puerto": 8000,
    "dispositivos": [
        {"id": "cam-01", "nombre": "ESP32-S3-CAM", "tipo": "esp32cam", "direccion": "epp-cam01.local",
         "zona": "Ingreso", "habilitado": False, "deteccion": True},
        {"id": "usb-0", "nombre": "Webcam de la laptop", "tipo": "usb", "direccion": "0",
         "zona": "Pruebas", "habilitado": True, "deteccion": True},
    ],
    "actuador": "",
    "requeridos": ["casco", "chaleco"],
}

DDL = """CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    camara TEXT,
    zona TEXT,
    epp_faltante TEXT,
    foto TEXT,
    confianza REAL,
    estado TEXT DEFAULT 'alertado',
    sync INTEGER DEFAULT 0
)"""

# Estado en memoria del panel
NET = {"http": 8000, "https": None, "https_ok": False}     # puertos reales (los fija main())
PAIRINGS = {}      # codigo -> {"nombre", "zona", "exp", "device_id"}
INGEST = {}        # id de celular vinculado -> (jpeg bytes, timestamp)
SCANS = {}         # id de busqueda -> estado de la busqueda en la red
_cfg_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app):
    ensure_db()
    with _cfg_lock:
        cfg = load_cfg()
        cfg["panel_puerto"] = NET["http"]
        save_cfg(cfg)  # tambien guarda las correcciones de tipo y el 'activo' de compatibilidad
    yield


app = FastAPI(title="ARGOS EPP - Panel", version="2.0", lifespan=lifespan)


# ============================================================
# Configuracion compartida con el motor
# ============================================================
def _atomic_write(path: Path, data: bytes, tries: int = 8) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    for _ in range(tries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.02)
    return False


def normalize_config(cfg: dict) -> dict:
    """Completa lo que falte y migra la config vieja (un solo 'activo') al esquema nuevo."""
    legacy_active = cfg.get("activo")
    for k, v in DEFAULT_CONFIG.items():
        if k != "dispositivos":
            cfg.setdefault(k, copy.deepcopy(v))
    cfg.setdefault("dispositivos", copy.deepcopy(DEFAULT_CONFIG["dispositivos"]))
    for d in cfg["dispositivos"]:
        if "habilitado" not in d:
            d["habilitado"] = (d.get("id") == legacy_active) if legacy_active is not None else True
        d.setdefault("deteccion", True)
        d.setdefault("zona", "Sin zona")
        d["tipo"] = fix_tipo(d.get("tipo", ""), str(d.get("direccion", "")))
    return cfg


def fix_tipo(tipo: str, direccion: str) -> str:
    """Corrige tipos mal marcados: un numero (0, 1) es una webcam USB; epp-camXX.local es una ESP32."""
    d = direccion.strip().lower()
    if tipo in ("esp32cam", "celular", "url", "rtsp") and d.isdigit() and len(d) <= 2:
        return "usb"
    if tipo in ("usb", "celular") and (d.endswith(".local") or d.startswith("epp-cam")):
        return "esp32cam"
    if tipo not in TIPOS:
        return "usb" if d.isdigit() else "url"
    return tipo


def sync_legacy_active(cfg: dict) -> None:
    """El motor viejo (v5) solo entiende UN dispositivo 'activo'. Lo mantenemos al dia por compatibilidad."""
    enabled = [d["id"] for d in cfg["dispositivos"] if d.get("habilitado", True)]
    cur = cfg.get("activo")
    cfg["activo"] = cur if cur in enabled else (enabled[0] if enabled else "")


def load_cfg() -> dict:
    if not CONFIG_PATH.exists():
        cfg = normalize_config({})
        save_cfg(cfg)
        return cfg
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(500, f"No pude leer {CONFIG_PATH} (esta danado). Borralo y el panel crea uno nuevo.")
    return normalize_config(cfg)


def save_cfg(cfg: dict) -> None:
    sync_legacy_active(cfg)
    ok = _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"))
    if not ok:
        raise HTTPException(500, "No pude guardar la configuracion (archivo en uso). Intenta otra vez.")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().encode("ascii", "ignore").decode()).strip("-")
    return s[:24] or "dispositivo"


def unique_id(cfg: dict, base: str) -> str:
    ids = {d["id"] for d in cfg["dispositivos"]}
    dev_id, n = base, 2
    while dev_id in ids:
        dev_id, n = f"{base}-{n}", n + 1
    return dev_id


def find_device(cfg: dict, dev_id: str) -> dict:
    for d in cfg["dispositivos"]:
        if d["id"] == dev_id:
            return d
    raise HTTPException(404, f"No existe el dispositivo '{dev_id}'")


def public_device(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "token"}


# ============================================================
# Estado del motor
# ============================================================
def read_motor_state() -> dict:
    """{'activo': bool, 'motor': {...}, 'dispositivos': {id: {...}}}"""
    try:
        est = json.loads((LIVE_DIR / "estado.json").read_text(encoding="utf-8"))
        edad = time.time() - float(est.get("ts", 0))
    except (OSError, ValueError):
        return {"activo": False, "motor": {}, "dispositivos": {}, "legacy": False}
    if "dispositivos" not in est and est.get("camara"):  # motor viejo: una sola camara, estado plano
        return {"activo": edad < FRESCO_S, "legacy": True, "dispositivos": {est["camara"]: est},
                "motor": {"fps": est.get("fps", 0), "requeridos": est.get("requeridos", [])}}
    return {"activo": edad < FRESCO_S, "legacy": False, "motor": est.get("motor", {}),
            "dispositivos": est.get("dispositivos", {})}


def device_status(d: dict, motor: dict) -> dict:
    """Estado de un dispositivo para la interfaz."""
    info = motor["dispositivos"].get(d["id"]) if motor["activo"] else None
    recibiendo = d["tipo"] == "celularweb" and ingest_fresh(d["id"])
    if not d.get("habilitado", True):
        estado, mensaje = "apagado", "Transmisión apagada"
    elif not motor["activo"]:
        estado, mensaje = "motor_detenido", "El motor no está corriendo"
    elif info and info.get("video_ok"):
        estado, mensaje = "en_linea", ""
    elif motor.get("legacy") and not info:
        estado, mensaje = "sin_senal", "Tu motor_epp.py es una versión vieja: solo atiende 1 cámara a la vez."
    else:
        estado, mensaje = "sin_senal", (info or {}).get("mensaje") or "Sin señal"
    det_global = bool(read_cfg_flag("deteccion_global", True))
    return {
        "estado": estado, "mensaje": mensaje, "recibiendo": recibiendo,
        "deteccion_efectiva": bool(det_global and d.get("deteccion", True)),
        "fps": (info or {}).get("fps", 0) if estado == "en_linea" else 0,
        "personas": (info or {}).get("personas", 0) if estado == "en_linea" else 0,
        "faltantes": (info or {}).get("faltantes", []) if estado == "en_linea" else [],
        "ultima_alerta": (info or {}).get("ultima_alerta"),
    }


def read_cfg_flag(key, default):
    try:
        return load_cfg().get(key, default)
    except HTTPException:
        return default


# ============================================================
# Direcciones, pruebas y vista previa
# ============================================================
def normalize_url(tipo: str, direccion: str):
    """Igual que en el motor: lo que escribe el usuario -> fuente de video."""
    d = str(direccion).strip()
    if tipo == "usb":
        return int(d) if d.isdigit() else d
    if tipo == "rtsp":
        return d if "://" in d else "rtsp://" + d
    if "://" not in d:
        d = "http://" + d
    u = urlparse(d)
    if tipo == "celular" and u.path in ("", "/"):
        u = u._replace(path="/video")
    return urlunparse(u)


def resolve_host(host: str) -> Optional[str]:
    try:
        return socket.gethostbyname(host)
    except OSError:
        pass
    try:  # mDNS con zeroconf (ya esta instalado para el motor)
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

        target = host.lower().removesuffix(".local")
        found = {}

        class L(ServiceListener):
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info and info.addresses and (info.server or "").lower().startswith(target):
                    found["ip"] = socket.inet_ntoa(info.addresses[0])

            def update_service(self, zc, type_, name):
                pass

            def remove_service(self, zc, type_, name):
                pass

        zc = Zeroconf()
        ServiceBrowser(zc, "_http._tcp.local.", L())
        t0 = time.time()
        try:
            while time.time() - t0 < 3 and "ip" not in found:
                time.sleep(0.2)
        finally:
            zc.close()
        return found.get("ip")
    except Exception:
        return None


def esp32_base(direccion: str) -> Optional[str]:
    d = direccion.strip()
    if "://" in d:
        u = urlparse(d)
        return f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port and u.port not in (80, 81) else "")
    ip = resolve_host(d)
    return f"http://{ip}" if ip else None


def probar_dispositivo(dev: dict) -> dict:
    tipo, direccion = dev["tipo"], dev["direccion"]
    t0 = time.time()

    def result(ok, detalle):
        return {"ok": ok, "ms": int((time.time() - t0) * 1000), "detalle": detalle}

    try:
        if tipo == "esp32cam":
            base = esp32_base(direccion)
            if not base:
                return result(False, f"No encuentro '{direccion}' en la red. Revisa el hotspot y que esté encendida.")
            r = requests.get(base + "/health", timeout=3)
            return result(r.status_code == 200, f"La ESP32 responde en {base} (HTTP {r.status_code})")
        if tipo in ("celular", "url"):
            url = normalize_url(tipo, direccion)
            r = requests.get(url, stream=True, timeout=4)
            ctype = r.headers.get("Content-Type", "?")
            r.close()
            return result(r.status_code < 400, f"{url} respondió HTTP {r.status_code} ({ctype})")
        if tipo == "celularweb":
            ok = ingest_fresh(dev["id"])
            return result(ok, "El celular está enviando video." if ok else
                          "El celular no está enviando video. Abre la página de transmisión en el celular.")
        if tipo == "rtsp":
            u = urlparse(normalize_url(tipo, direccion))
            socket.create_connection((u.hostname, u.port or 554), timeout=3).close()
            return result(True, f"Puerto RTSP abierto en {u.hostname}:{u.port or 554}")
        if tipo == "usb":
            return result(None, "Las webcams USB se prueban al encender la transmisión.")
    except requests.RequestException as e:
        return result(False, f"No responde ({type(e).__name__})")
    except OSError as e:
        return result(False, f"No responde ({e})")
    return result(False, "Tipo de dispositivo desconocido")


def snapshot_url(dev: dict) -> Optional[str]:
    tipo, direccion = dev["tipo"], dev["direccion"]
    if tipo == "esp32cam":
        base = esp32_base(direccion)
        return base + "/capture" if base else None
    if tipo == "celular":
        u = urlparse(normalize_url(tipo, direccion))
        return f"{u.scheme}://{u.netloc}/shot.jpg"  # IP Webcam (Android)
    return None


# ============================================================
# Busqueda de dispositivos en la red (radar)
# ============================================================
def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def lan_hosts() -> List[str]:
    ip = lan_ip()
    if ip.startswith("127."):
        return []
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return [str(h) for h in net.hosts()]


def tcp_open(ip: str, port: int, timeout: float = 0.3) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def probe_host(ip: str) -> List[dict]:
    """Prueba los puertos tipicos de camaras en un equipo y devuelve lo que encuentra."""
    found = []
    if tcp_open(ip, 80):
        try:  # el firmware de la ESP32-S3-CAM responde JSON en /health
            r = requests.get(f"http://{ip}/health", timeout=1.2)
            j = r.json() if r.status_code == 200 else {}
            if isinstance(j, dict) and "camera_id" in j and tcp_open(ip, 81):
                found.append({"ip": ip, "tipo": "esp32cam", "nombre": str(j.get("camera_id") or "ESP32-S3-CAM"),
                              "direccion": ip, "zona": str(j.get("zone") or ""),
                              "detalle": f"Señal WiFi {j.get('rssi', '?')} dBm"})
        except (requests.RequestException, ValueError):
            pass
    if tcp_open(ip, 8080):
        try:  # IP Webcam (Android) sirve una foto en /shot.jpg
            r = requests.get(f"http://{ip}:8080/shot.jpg", stream=True, timeout=1.5)
            ok = r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image")
            r.close()
            if ok:
                found.append({"ip": ip, "tipo": "celular", "nombre": "Celular (IP Webcam)",
                              "direccion": f"{ip}:8080", "zona": "", "detalle": "App IP Webcam"})
        except requests.RequestException:
            pass
    if tcp_open(ip, 4747):
        found.append({"ip": ip, "tipo": "celular", "nombre": "Celular (DroidCam)",
                      "direccion": f"{ip}:4747", "zona": "", "detalle": "App DroidCam"})
    if tcp_open(ip, 554):
        found.append({"ip": ip, "tipo": "rtsp", "nombre": "Cámara IP",
                      "direccion": f"{ip}:554", "zona": "", "detalle": "Puerto RTSP abierto"})
    return found


def mdns_scan(job: dict, seconds: float = 3.0) -> None:
    """Las ESP32-S3-CAM se anuncian por mDNS como epp-camXX.local."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except Exception:
        return

    class L(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            server = (info.server or "").rstrip(".").lower() if info else ""
            if info and info.addresses and server.startswith("epp-cam"):
                ip = socket.inet_ntoa(info.addresses[0])
                add_found(job, {"ip": ip, "tipo": "esp32cam", "nombre": server.removesuffix(".local"),
                                "direccion": server, "zona": "", "detalle": "Anunciada por mDNS"})

        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

    zc = None
    try:
        zc = Zeroconf()
        ServiceBrowser(zc, "_http._tcp.local.", L())
        time.sleep(seconds)
    except Exception:
        pass
    finally:
        if zc:
            zc.close()


def add_found(job: dict, item: dict) -> None:
    with job["lock"]:
        if any(f["ip"] == item["ip"] and f["tipo"] == item["tipo"] for f in job["encontrados"]):
            return
        item["id"] = uuid.uuid4().hex[:8]
        job["encontrados"].append(item)


def run_scan(job: dict, hosts: List[str]) -> None:
    job["total"] = len(hosts)

    def work(ip):
        for item in probe_host(ip):
            add_found(job, item)
        with job["lock"]:
            job["hechos"] += 1

    try:
        t = threading.Thread(target=mdns_scan, args=(job,), daemon=True)
        t.start()
        with ThreadPoolExecutor(max_workers=48) as pool:
            list(pool.map(work, hosts))
        t.join(timeout=4)
    finally:
        job["estado"] = "listo"
        job["fin"] = time.time()


def start_scan(hosts: Optional[List[str]] = None) -> dict:
    # solo una busqueda a la vez; se limpian las viejas
    for jid in [k for k, v in SCANS.items() if v["estado"] == "listo" and time.time() - v.get("fin", 0) > 300]:
        SCANS.pop(jid, None)
    for j in SCANS.values():
        if j["estado"] == "buscando":
            return j
    hosts = lan_hosts() if hosts is None else hosts
    job = {"id": uuid.uuid4().hex[:8], "estado": "buscando", "total": len(hosts), "hechos": 0,
           "encontrados": [], "red": (hosts[0].rsplit(".", 1)[0] + ".0/24") if hosts else "", "lock": threading.Lock(),
           "inicio": time.time()}
    SCANS[job["id"]] = job
    threading.Thread(target=run_scan, args=(job, hosts), daemon=True).start()
    return job


def scan_view(job: dict, cfg: dict) -> dict:
    known = {(d["tipo"], str(d["direccion"]).lower()) for d in cfg["dispositivos"]}
    with job["lock"]:
        items = [dict(i) for i in job["encontrados"]]
    for i in items:
        i["agregado"] = (i["tipo"], str(i["direccion"]).lower()) in known
    return {"id": job["id"], "estado": job["estado"], "total": job["total"], "hechos": job["hechos"],
            "red": job["red"], "encontrados": items}


# ============================================================
# Celular vinculado por QR (transmite desde el navegador)
# ============================================================
def ingest_fresh(dev_id: str) -> bool:
    f = INGEST.get(dev_id)
    return bool(f and time.time() - f[1] < FRESCO_S)


def pairing_urls(code: str) -> dict:
    ip = lan_ip()
    http = f"http://{ip}:{NET['http']}/camara?c={code}"
    https = f"https://{ip}:{NET['https']}/camara?c={code}" if NET["https_ok"] and NET["https"] else None
    return {"url": https or http, "https": bool(https)}


class VincularIn(BaseModel):
    nombre: str = ""
    zona: str = ""


class RegistrarIn(BaseModel):
    nombre: str = ""
    zona: str = ""


@app.post("/api/vincular")
def api_vincular(body: VincularIn):
    now = time.time()
    for c in [c for c, p in PAIRINGS.items() if p["exp"] < now]:
        PAIRINGS.pop(c, None)
    code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")[:8]
    PAIRINGS[code] = {"nombre": body.nombre.strip(), "zona": body.zona.strip(), "exp": now + CODIGO_TTL_S, "device_id": None}
    return {"codigo": code, "expira_en": CODIGO_TTL_S, **pairing_urls(code)}


@app.get("/api/vincular/{code}")
def api_vincular_estado(code: str):
    p = PAIRINGS.get(code)
    if not p or (p["exp"] < time.time() and not p["device_id"]):
        return {"estado": "expirado"}
    if p["device_id"]:
        return {"estado": "vinculado", "dispositivo": p["device_id"], "nombre": p["nombre"], "zona": p["zona"]}
    return {"estado": "pendiente", "nombre": p["nombre"], "zona": p["zona"], "expira_en": int(p["exp"] - time.time())}


@app.post("/api/vincular/{code}/registrar")
def api_vincular_registrar(code: str, body: RegistrarIn):
    p = PAIRINGS.get(code)
    if not p or p["exp"] < time.time() or p["device_id"]:
        raise HTTPException(410, "El código ya no es válido. Genera otro QR en el panel.")
    nombre = body.nombre.strip() or p["nombre"] or "Celular"
    zona = body.zona.strip() or p["zona"] or "Sin zona"
    with _cfg_lock:
        cfg = load_cfg()
        dev = {"id": unique_id(cfg, slugify(nombre)), "nombre": nombre, "tipo": "celularweb", "direccion": "",
               "zona": zona, "habilitado": True, "deteccion": True, "token": secrets.token_urlsafe(16)}
        cfg["dispositivos"].append(dev)
        save_cfg(cfg)
    p.update(device_id=dev["id"], nombre=nombre, zona=zona)
    return {"id": dev["id"], "token": dev["token"], "nombre": nombre, "zona": zona}


@app.post("/api/ingest/{dev_id}")
async def api_ingest(dev_id: str, request: Request):
    dev = find_device(load_cfg(), dev_id)
    token = request.headers.get("x-token", "")
    if dev["tipo"] != "celularweb" or not dev.get("token") or not secrets.compare_digest(token, dev["token"]):
        raise HTTPException(403, "Token inválido")
    data = await request.body()
    if len(data) > 2_000_000 or data[:2] != b"\xff\xd8":
        raise HTTPException(400, "Se esperaba una imagen JPEG")
    INGEST[dev_id] = (data, time.time())
    return {"ok": True}


@app.get("/camara", include_in_schema=False)
def pagina_camara():
    return FileResponse(STATIC_DIR / "camara.html", headers={"Cache-Control": "no-store"})


def mjpeg_part(data: bytes) -> bytes:
    return (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")


@app.get("/stream/{dev_id}")
def stream_ingest(dev_id: str):
    """Video crudo de un celular vinculado (lo lee el motor)."""
    if not ingest_fresh(dev_id):
        raise HTTPException(404, "El celular no está enviando video")

    def gen():
        last = 0.0
        while True:
            f = INGEST.get(dev_id)
            if not f or time.time() - f[1] > FRESCO_S:
                return  # el motor reintenta solo
            if f[1] != last:
                last = f[1]
                yield mjpeg_part(f[0])
            time.sleep(0.04)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


# ============================================================
# API: informacion y estado
# ============================================================
@app.get("/api/info")
def api_info():
    ip = lan_ip()
    return {"lan_ip": ip, "puerto_http": NET["http"], "puerto_https": NET["https"] if NET["https_ok"] else None,
            "url_panel": f"http://{ip}:{NET['http']}", "proyecto": str(PROJECT_DIR)}


@app.get("/api/estado")
def api_estado():
    cfg = load_cfg()
    motor = read_motor_state()
    devs = {}
    for d in cfg["dispositivos"]:
        devs[d["id"]] = {**device_status(d, motor), "habilitado": d.get("habilitado", True), "deteccion": d.get("deteccion", True)}
    m = motor["motor"] if motor["activo"] else {}
    return {"motor_activo": motor["activo"], "fps": m.get("fps", 0), "deteccion_global": cfg["deteccion_global"],
            "requeridos": m.get("requeridos", cfg["requeridos"]), "dispositivos": devs}


# ============================================================
# API: dispositivos y zonas
# ============================================================
class DispositivoIn(BaseModel):
    id: Optional[str] = None
    nombre: str
    tipo: str
    direccion: str = ""
    zona: str = "Sin zona"
    habilitado: Optional[bool] = None
    deteccion: Optional[bool] = None


class DispositivoPatch(BaseModel):
    nombre: Optional[str] = None
    zona: Optional[str] = None
    habilitado: Optional[bool] = None
    deteccion: Optional[bool] = None


@app.get("/api/dispositivos")
def api_dispositivos():
    cfg = load_cfg()
    motor = read_motor_state()
    out = [{**public_device(d), **device_status(d, motor)} for d in cfg["dispositivos"]]
    return {"deteccion_global": cfg["deteccion_global"], "motor_activo": motor["activo"],
            "motor_viejo": bool(motor["activo"] and motor.get("legacy")), "dispositivos": out}


@app.get("/api/zonas")
def api_zonas():
    cfg = load_cfg()
    return sorted({d.get("zona", "") for d in cfg["dispositivos"] if d.get("zona")})


@app.post("/api/dispositivos")
def api_dispositivo_guardar(d: DispositivoIn):
    if d.tipo not in TIPOS or d.tipo == "celularweb":
        raise HTTPException(400, f"Tipo inválido. Usa: {', '.join(t for t in TIPOS if t != 'celularweb')}")
    nombre, direccion = d.nombre.strip(), d.direccion.strip()
    if not nombre or not direccion:
        raise HTTPException(400, "Falta el nombre o la dirección.")
    with _cfg_lock:
        cfg = load_cfg()
        existing = next((x for x in cfg["dispositivos"] if x["id"] == d.id), None) if d.id else None
        dev = {"id": existing["id"] if existing else unique_id(cfg, slugify(nombre)), "nombre": nombre,
               "tipo": d.tipo, "direccion": direccion, "zona": d.zona.strip() or "Sin zona",
               "habilitado": d.habilitado if d.habilitado is not None else (existing["habilitado"] if existing else True),
               "deteccion": d.deteccion if d.deteccion is not None else (existing["deteccion"] if existing else True)}
        dev["tipo"] = fix_tipo(dev["tipo"], dev["direccion"])
        if existing:
            cfg["dispositivos"] = [dev if x["id"] == dev["id"] else x for x in cfg["dispositivos"]]
        else:
            cfg["dispositivos"].append(dev)
        if dev["habilitado"]:
            cfg["activo"] = dev["id"]
        save_cfg(cfg)
    return dev


@app.patch("/api/dispositivos/{dev_id}")
def api_dispositivo_patch(dev_id: str, p: DispositivoPatch):
    with _cfg_lock:
        cfg = load_cfg()
        dev = find_device(cfg, dev_id)
        if p.nombre is not None and p.nombre.strip():
            dev["nombre"] = p.nombre.strip()
        if p.zona is not None:
            dev["zona"] = p.zona.strip() or "Sin zona"
        if p.habilitado is not None:
            dev["habilitado"] = p.habilitado
            if p.habilitado:
                cfg["activo"] = dev_id  # motor viejo: el ultimo que se enciende pasa a ser el activo
                if read_motor_state()["legacy"]:  # y como solo atiende uno, apagamos los demas
                    for x in cfg["dispositivos"]:
                        if x["id"] != dev_id:
                            x["habilitado"] = False
        if p.deteccion is not None:
            dev["deteccion"] = p.deteccion
        save_cfg(cfg)
    return public_device(dev)


@app.delete("/api/dispositivos/{dev_id}")
def api_dispositivo_borrar(dev_id: str):
    with _cfg_lock:
        cfg = load_cfg()
        find_device(cfg, dev_id)
        cfg["dispositivos"] = [x for x in cfg["dispositivos"] if x["id"] != dev_id]
        save_cfg(cfg)
    INGEST.pop(dev_id, None)
    return {"ok": True}


@app.post("/api/dispositivos/{dev_id}/probar")
def api_dispositivo_probar(dev_id: str):
    return probar_dispositivo(find_device(load_cfg(), dev_id))


@app.get("/api/dispositivos/{dev_id}/snapshot")
def api_dispositivo_snapshot(dev_id: str):
    dev = find_device(load_cfg(), dev_id)
    url = snapshot_url(dev)
    if not url:
        raise HTTPException(404, "Este tipo de dispositivo no tiene vista previa.")
    try:
        r = requests.get(url, timeout=4)
        r.raise_for_status()
    except requests.RequestException:
        raise HTTPException(502, "El dispositivo no respondió.")
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/escaneo")
def api_escaneo_iniciar():
    job = start_scan()
    return scan_view(job, load_cfg())


@app.get("/api/escaneo/{job_id}")
def api_escaneo_estado(job_id: str):
    job = SCANS.get(job_id)
    if not job:
        raise HTTPException(404, "Búsqueda no encontrada")
    return scan_view(job, load_cfg())


class GlobalIn(BaseModel):
    deteccion_global: bool


@app.put("/api/global")
def api_global(g: GlobalIn):
    with _cfg_lock:
        cfg = load_cfg()
        cfg["deteccion_global"] = g.deteccion_global
        save_cfg(cfg)
    return {"deteccion_global": g.deteccion_global}


class AjustesIn(BaseModel):
    requeridos: List[str]
    actuador: str = ""


@app.get("/api/ajustes")
def api_ajustes():
    cfg = load_cfg()
    return {"requeridos": cfg["requeridos"], "actuador": cfg["actuador"], "epp_validos": list(EPP_VALIDOS),
            "deteccion_global": cfg["deteccion_global"]}


@app.put("/api/ajustes")
def api_ajustes_guardar(a: AjustesIn):
    req = [r for r in EPP_VALIDOS if r in a.requeridos]
    if not req:
        raise HTTPException(400, "Elige al menos un EPP obligatorio.")
    with _cfg_lock:
        cfg = load_cfg()
        cfg["requeridos"] = req
        cfg["actuador"] = a.actuador.strip()
        save_cfg(cfg)
    return {"requeridos": req, "actuador": cfg["actuador"]}


# ============================================================
# Base de datos de eventos (la llena el motor)
# ============================================================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db() -> None:
    with db() as c:
        c.execute(DDL)


def device_names() -> dict:
    try:
        return {d["id"]: d["nombre"] for d in load_cfg()["dispositivos"]}
    except HTTPException:
        return {}


def row_to_event(r: sqlite3.Row, names: dict) -> dict:
    return {
        "id": r["id"], "ts": r["ts"], "camara": r["camara"], "camara_nombre": names.get(r["camara"], r["camara"]),
        "zona": r["zona"], "faltantes": [x for x in (r["epp_faltante"] or "").split(",") if x],
        "confianza": r["confianza"], "estado": r["estado"],
        "foto_url": f"/api/eventos/{r['id']}/foto" if r["foto"] else None,
    }


def build_query(desde, hasta, camara, zona, epp, estado, despues_id):
    where, params = [], []
    if desde:
        where.append("substr(ts,1,10) >= ?")
        params.append(desde)
    if hasta:
        where.append("substr(ts,1,10) <= ?")
        params.append(hasta)
    if camara:
        where.append("camara = ?")
        params.append(camara)
    if zona:
        where.append("zona = ?")
        params.append(zona)
    if epp:
        where.append("(',' || epp_faltante || ',') LIKE ?")
        params.append(f"%,{epp},%")
    if estado:
        where.append("estado = ?")
        params.append(estado)
    if despues_id:
        where.append("id > ?")
        params.append(despues_id)
    return (" WHERE " + " AND ".join(where)) if where else "", params


# ============================================================
# API: eventos, resumen, reportes
# ============================================================
@app.get("/api/eventos")
def api_eventos(limit: int = Query(100, ge=1, le=1000), desde: Optional[str] = None, hasta: Optional[str] = None,
                camara: Optional[str] = None, zona: Optional[str] = None, epp: Optional[str] = None,
                estado: Optional[str] = None, despues_id: Optional[int] = None):
    q, params = build_query(desde, hasta, camara, zona, epp, estado, despues_id)
    names = device_names()
    with db() as c:
        rows = c.execute(f"SELECT * FROM eventos{q} ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        total = c.execute(f"SELECT COUNT(*) FROM eventos{q}", params).fetchone()[0]
    return {"total": total, "eventos": [row_to_event(r, names) for r in rows]}


class EstadoIn(BaseModel):
    estado: str


@app.patch("/api/eventos/{ev_id}")
def api_evento_estado(ev_id: int, body: EstadoIn):
    if body.estado not in ESTADOS:
        raise HTTPException(400, f"Estado inválido. Usa: {', '.join(ESTADOS)}")
    with db() as c:
        cur = c.execute("UPDATE eventos SET estado = ? WHERE id = ?", (body.estado, ev_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "Evento no encontrado")
    return {"ok": True}


@app.get("/api/eventos/{ev_id}/foto")
def api_evento_foto(ev_id: int):
    with db() as c:
        row = c.execute("SELECT foto FROM eventos WHERE id = ?", (ev_id,)).fetchone()
    if not row or not row["foto"]:
        raise HTTPException(404, "Sin foto")
    # Solo se usan el dia y el nombre del archivo: la ruta guardada nunca se abre directamente.
    parts = re.split(r"[\\/]", row["foto"])
    name, day = Path(parts[-1]).name, parts[-2] if len(parts) > 1 else ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(404, "Ruta de foto inválida")
    path = EVIDENCIAS_DIR / day / name
    if not path.is_file():
        raise HTTPException(404, "La foto ya no está en la carpeta de evidencias")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/resumen")
def api_resumen(dias: int = Query(7, ge=1, le=90), desde: Optional[str] = None, hasta: Optional[str] = None,
                camara: Optional[str] = None):
    hoy = datetime.now().date()
    hasta_d = datetime.strptime(hasta, "%Y-%m-%d").date() if hasta else hoy
    desde_d = datetime.strptime(desde, "%Y-%m-%d").date() if desde else hasta_d - timedelta(days=dias - 1)
    n_dias = max(1, min(120, (hasta_d - desde_d).days + 1))
    names = device_names()
    cfg = load_cfg()
    with db() as c:
        rows = c.execute("SELECT * FROM eventos WHERE substr(ts,1,10) BETWEEN ? AND ? ORDER BY id",
                         (desde_d.isoformat(), hasta_d.isoformat())).fetchall()
    serie = {(desde_d + timedelta(days=i)).isoformat(): 0 for i in range(n_dias)}
    hoy_total, hoy_epp = 0, {}
    por_dev = {d["id"]: {"id": d["id"], "nombre": d["nombre"], "zona": d.get("zona", ""), "tipo": d["tipo"],
                         "total": 0, "por_epp": {}, "ultima": None} for d in cfg["dispositivos"]}
    for r in rows:
        cam = r["camara"]
        entry = por_dev.setdefault(cam, {"id": cam, "nombre": names.get(cam, cam), "zona": r["zona"] or "", "tipo": "",
                                         "total": 0, "por_epp": {}, "ultima": None})
        entry["total"] += 1
        entry["ultima"] = r["ts"]
        eppl = [e for e in (r["epp_faltante"] or "").split(",") if e]
        for e in eppl:
            entry["por_epp"][e] = entry["por_epp"].get(e, 0) + 1
        if camara and cam != camara:
            continue
        dia = r["ts"][:10]
        if dia in serie:
            serie[dia] += 1
        if dia == hoy.isoformat():
            hoy_total += 1
            for e in eppl:
                hoy_epp[e] = hoy_epp.get(e, 0) + 1
    return {"hoy": {"total": hoy_total, "por_epp": hoy_epp},
            "serie": [{"dia": d, "total": n} for d, n in serie.items()],
            "por_dispositivo": sorted(por_dev.values(), key=lambda x: (-x["total"], x["nombre"])),
            "total_periodo": len(rows), "desde": desde_d.isoformat(), "hasta": hasta_d.isoformat()}


@app.get("/api/eventos.csv")
def api_eventos_csv(desde: Optional[str] = None, hasta: Optional[str] = None, camara: Optional[str] = None,
                    zona: Optional[str] = None, epp: Optional[str] = None, estado: Optional[str] = None):
    q, params = build_query(desde, hasta, camara, zona, epp, estado, None)
    names = device_names()
    with db() as c:
        rows = c.execute(f"SELECT * FROM eventos{q} ORDER BY id DESC", params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "fecha_hora", "dispositivo", "id_dispositivo", "zona", "epp_faltante", "confianza", "estado", "foto"])
    for r in rows:
        foto = re.split(r"[\\/]", r["foto"])[-1] if r["foto"] else ""
        w.writerow([r["id"], r["ts"].replace("T", " "), names.get(r["camara"], r["camara"]), r["camara"], r["zona"],
                    r["epp_faltante"], f"{(r['confianza'] or 0):.2f}", r["estado"], foto])
    sufijo = f"_{slugify(camara)}" if camara else ""
    nombre = f"argos_eventos{sufijo}_{datetime.now():%Y%m%d_%H%M}.csv"
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


# ============================================================
# Video en vivo
# ============================================================
def read_file(path: Path) -> Optional[bytes]:
    for _ in range(5):
        try:
            return path.read_bytes()
        except PermissionError:
            time.sleep(0.02)
        except OSError:
            return None
    return None


def safe_id(dev_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]{1,40}", dev_id):
        raise HTTPException(404, "Dispositivo no encontrado")
    return dev_id


def latest_frame(dev_id: str):
    """(bytes, marca de tiempo) del cuadro mas reciente de un dispositivo, o (None, 0).
    Prefiere el video anotado por el motor; si el motor no lo procesa, usa el video crudo del celular vinculado."""
    p = LIVE_DIR / f"{dev_id}.jpg"
    try:
        m = p.stat().st_mtime
    except OSError:
        m = 0.0
    if not m:  # motor viejo: publica un unico live.jpg de la camara activa
        old = read_motor_state()
        if old["legacy"] and dev_id in old["dispositivos"]:
            p = LIVE_DIR / "live.jpg"
            try:
                m = p.stat().st_mtime
            except OSError:
                m = 0.0
    if m and time.time() - m < FRESCO_S:
        data = read_file(p)
        if data:
            return data, m
    f = INGEST.get(dev_id)
    if f and time.time() - f[1] < FRESCO_S:
        return f[0], f[1]
    return None, 0.0


@app.get("/api/live/{dev_id}.jpg")
def api_live_jpg(dev_id: str):
    data, _ = latest_frame(safe_id(dev_id))
    if not data:
        raise HTTPException(404, "Sin video")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/video/{dev_id}")
def video(dev_id: str):
    dev_id = safe_id(dev_id)

    def gen():
        last, last_send = 0.0, 0.0
        while True:
            data, ts = latest_frame(dev_id)
            now = time.time()
            if data and (ts != last or now - last_send > 2):
                last, last_send = ts, now
                yield mjpeg_part(data)
            time.sleep(0.06)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


# ============================================================
# Paginas
# ============================================================
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================
# HTTPS autofirmado (la camara del celular solo funciona en https)
# ============================================================
def ensure_cert(ip: str):
    """Crea (o reutiliza) un certificado autofirmado valido para localhost y la IP de la laptop."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception:
        return None
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert_p, key_p, ip_p = CERT_DIR / "cert.pem", CERT_DIR / "key.pem", CERT_DIR / "ip.txt"
    if cert_p.exists() and key_p.exists() and ip_p.exists() and ip_p.read_text().strip() == ip:
        return str(cert_p), str(key_p)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ARGOS EPP (local)")])
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    now = datetime.utcnow()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=825)).add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(key, hashes.SHA256()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
    ip_p.write_text(ip)
    return str(cert_p), str(key_p)


async def serve(host: str, port: int, https_port: int):
    import uvicorn

    NET["http"] = port
    ip = lan_ip()
    cert = ensure_cert(ip)
    servers = [uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))]
    if cert:
        NET.update(https=https_port, https_ok=True)
        s2 = uvicorn.Server(uvicorn.Config(app, host=host, port=https_port, ssl_certfile=cert[0], ssl_keyfile=cert[1],
                                           log_level="warning"))
        s2.install_signal_handlers = lambda: None  # el primero maneja Ctrl+C
        servers.append(s2)

    print("\n  ARGOS EPP - Panel v2")
    print(f"  En esta laptop:     http://localhost:{port}")
    print(f"  Desde el celular:   http://{ip}:{port}")
    if cert:
        print(f"  Cámara del celular: https://{ip}:{https_port}/camara   (el panel te muestra el QR en Dispositivos)")
    else:
        print("  AVISO: falta 'cryptography', sin https el celular no puede transmitir su cámara.")
        print("         Instálalo con:  pip install cryptography")
    print(f"  Proyecto:           {PROJECT_DIR}")
    print("  Ctrl + C para detener\n")

    tasks = [asyncio.create_task(s.serve()) for s in servers]
    await tasks[0]
    for s in servers[1:]:
        s.should_exit = True
    await asyncio.gather(*tasks[1:], return_exceptions=True)


def main():
    ap = argparse.ArgumentParser(description="ARGOS EPP - Panel")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = accesible desde el celular en la misma red")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--https-port", type=int, default=8443)
    args = ap.parse_args()
    try:
        asyncio.run(serve(args.host, args.port, args.https_port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
