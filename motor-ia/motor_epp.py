"""
============================================================
ARGOS EPP - Motor de IA (Fase 1: checkpoint de entrada)
============================================================

Que hace:
    1. Encuentra la ESP32-S3-CAM en la red (mDNS: epp-cam01.local) o usa
       otra fuente de video (webcam de la laptop para pruebas).
    2. Detecta PERSONAS con yolov8n (COCO) y, para cada persona, recorta su
       imagen y busca EPP (casco, chaleco, ...) con un modelo de EPP.
    3. Si a una persona le falta algun EPP requerido de forma sostenida
       (varios frames seguidos), dispara una alerta:
         - guarda foto de evidencia (D:\\ARGOS-EPP\\evidencias\\AAAA-MM-DD\\)
         - registra el evento en SQLite (D:\\ARGOS-EPP\\eventos.db)
         - avisa al actuador por WiFi (opcional): http://<host>/alerta?tipo=casco,chaleco
         - pitido local en la laptop (opcional, --beep)
    4. Cuida la laptop: baja su propia prioridad, limita los FPS y
       los hilos de CPU.

Ubicacion esperada:  D:\\ARGOS-EPP\\motor-ia\\motor_epp.py
(la carpeta del proyecto se calcula como la carpeta padre de motor-ia)

Uso rapido (con el entorno virtual activado):
    # 1) Probar camara + deteccion de personas, sin alertas de EPP:
    python motor_epp.py --source 0 --person-only          (webcam de la laptop)
    python motor_epp.py --person-only                      (ESP32-S3-CAM por mDNS)

    # 2) Ver las clases que trae tu modelo de EPP:
    python motor_epp.py --info

    # 3) Correr completo (casco + chaleco):
    python motor_epp.py

Teclas en la ventana de video:  q = salir
Parar desde la consola:         Ctrl + C
============================================================
"""

import argparse
import copy
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# ------------------------------------------------------------
# Rutas del proyecto
# ------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
MODELOS_DIR = PROJECT_DIR / "modelos"
EVIDENCIAS_DIR = PROJECT_DIR / "evidencias"
DB_PATH = PROJECT_DIR / "eventos.db"

# ------------------------------------------------------------
# Mapa de EPP -> palabras que identifican la clase en el modelo.
# Funciona con modelos distintos (helmet / Hardhat / Vest / goggles...).
# Las clases que empiezan con "no" (NO-Hardhat, no_helmet) se ignoran.
# ------------------------------------------------------------
EPP_KEYWORDS = {
    "casco": ("helmet", "hardhat", "hard hat", "hard_hat", "hard-hat"),
    "chaleco": ("vest",),
    "lentes": ("goggle", "glasses"),
    "guantes": ("glove",),
    "botas": ("shoe", "boot"),
}
NEGATIVE_RE = re.compile(r"^(no|without)[\s_\-]")


# ============================================================
# LOGICA PURA (sin dependencias pesadas, facil de probar)
# ============================================================
def classify_name(name):
    """Devuelve 'casco', 'chaleco', etc. segun el nombre de clase, o None."""
    n = str(name).strip().lower()
    if NEGATIVE_RE.match(n):
        return None
    for epp, keywords in EPP_KEYWORDS.items():
        if any(k in n for k in keywords):
            return epp
    return None


def build_class_map(names):
    """names: dict {id: nombre}. Devuelve {id: 'casco'|'chaleco'|...} solo de las clases utiles."""
    mapping = {}
    for cid, name in names.items():
        epp = classify_name(name)
        if epp:
            mapping[int(cid)] = epp
    return mapping


class PersistenceFilter:
    """
    Evita falsas alarmas: solo dispara cuando el mismo EPP falta en TODOS los
    ultimos N frames procesados, y respeta un tiempo de espera entre alertas.
    """

    def __init__(self, frames, cooldown_s):
        self.window = deque(maxlen=max(1, frames))
        self.cooldown_s = cooldown_s
        self.last_alert = float("-inf")

    def update(self, missing, now):
        """missing: set de EPP faltantes en este frame (vacio si todo OK o no hay persona)."""
        self.window.append(frozenset(missing))
        if len(self.window) < self.window.maxlen:
            return set()
        common = frozenset.intersection(*self.window)
        if not common:
            return set()
        if now - self.last_alert < self.cooldown_s:
            return set()
        self.last_alert = now
        self.window.clear()
        return set(common)


def parse_epp_conf(text):
    """'casco=0.30,chaleco=0.45' -> {'casco': 0.30, 'chaleco': 0.45}"""
    out = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip().lower()] = float(v)
            except ValueError:
                pass
    return out


def head_region(box, frame_w, frame_h, head_frac=0.40):
    """
    Zona de la cabeza dentro de la caja de la persona: el tercio/40% superior,
    algo ensanchado hacia arriba (por si el casco sobresale de la caja).
    Es una zona pequena, asi el casco ocupa buena parte de la imagen que ve el modelo.
    """
    x1, y1, x2, y2 = box
    ph, pw = y2 - y1, x2 - x1
    cx = (x1 + x2) / 2
    half_w = max(pw * 0.40, min(pw * 0.5, ph * head_frac * 0.9))
    return (
        max(0, int(cx - half_w)),
        max(0, int(y1 - ph * 0.08)),
        min(frame_w, int(cx + half_w)),
        min(frame_h, int(y1 + ph * head_frac)),
    )


def items_for_person(dets, class_map, person_box, conf_thr, per_class=None):
    """
    dets: lista de (id_clase, confianza, (x1, y1, x2, y2)) en coordenadas del frame.
    Devuelve el set de EPP ('casco', 'chaleco'...) cuyo centro cae dentro de la caja de la persona.
    per_class: umbrales propios por EPP, ej. {'casco': 0.30}; los demas usan conf_thr.
    """
    found = set()
    per_class = per_class or {}
    px1, py1, px2, py2 = person_box
    for cid, cf, (a, b, c, d) in dets:
        epp = class_map.get(cid)
        if not epp or cf < per_class.get(epp, conf_thr):
            continue
        cx, cy = (a + c) / 2, (b + d) / 2
        if px1 <= cx <= px2 and py1 <= cy <= py2:
            found.add(epp)
    return found


class PersonTracker:
    """
    Memoria corta por persona: un EPP cuenta como "puesto" hasta hold_s segundos despues de la
    ultima vez que se vio. Evita que el chaleco/casco "parpadee" cuando la confianza baja un
    instante (movimiento, angulo, luz). Une personas entre frames por cercania del centro.
    """

    def __init__(self, hold_s, max_dist=0.25, ttl_s=3.0):
        self.hold_s = hold_s
        self.max_dist = max_dist
        self.ttl_s = ttl_s
        self.tracks = []

    def update(self, items, now, frame_w):
        """items: [(box, found_set)]. Devuelve la lista de sets 'efectivos' (con memoria), mismo orden."""
        self.tracks = [t for t in self.tracks if now - t["last"] <= self.ttl_s]
        used, out = set(), []
        for box, found in items:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            best, best_d = None, self.max_dist * frame_w
            for i, t in enumerate(self.tracks):
                if i in used:
                    continue
                d = ((t["cx"] - cx) ** 2 + (t["cy"] - cy) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = i, d
            if best is None:
                self.tracks.append({"cx": cx, "cy": cy, "seen": {}, "last": now})
                best = len(self.tracks) - 1
            used.add(best)
            t = self.tracks[best]
            t["cx"], t["cy"], t["last"] = cx, cy, now
            for epp in found:
                t["seen"][epp] = now
            out.append({e for e, ts in t["seen"].items() if now - ts <= self.hold_s})
        return out


def pad_box(box, frame_w, frame_h, pad=0.08):
    x1, y1, x2, y2 = box
    pw, ph = (x2 - x1) * pad, (y2 - y1) * pad
    return (
        max(0, int(x1 - pw)),
        max(0, int(y1 - ph)),
        min(frame_w, int(x2 + pw)),
        min(frame_h, int(y2 + ph)),
    )


# ============================================================
# PANEL: CONFIGURACION COMPARTIDA, VIDEO EN VIVO Y ESTADO
# (el panel y el motor se hablan por archivos en D:\ARGOS-EPP\config y \live)
# ============================================================
CONFIG_PATH = PROJECT_DIR / "config" / "dispositivos.json"
LIVE_DIR = PROJECT_DIR / "live"

DEFAULT_CONFIG = {
    "version": 2,
    "deteccion_global": True,   # switch maestro del reconocimiento de EPP
    "panel_puerto": 8000,       # el panel lo actualiza; el motor lo usa para leer celulares vinculados por QR
    "dispositivos": [
        {"id": "cam-01", "nombre": "ESP32-S3-CAM", "tipo": "esp32cam", "direccion": "epp-cam01.local",
         "zona": "Ingreso", "habilitado": False, "deteccion": True},
        {"id": "usb-0", "nombre": "Webcam de la laptop", "tipo": "usb", "direccion": "0",
         "zona": "Pruebas", "habilitado": True, "deteccion": True},
    ],
    "actuador": "",
    "requeridos": ["casco", "chaleco"],
}


def atomic_write(path, data, tries=8):
    """Escribe en un temporal y reemplaza (el lector nunca ve un archivo a medias)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    for _ in range(tries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:  # Windows: el otro proceso lo tiene abierto un instante
            time.sleep(0.02)
    return False


def save_config(cfg, path=None):
    return atomic_write(path or CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8"))


def normalize_config(cfg):
    """Completa lo que falte y migra la config vieja (un solo 'activo') al esquema nuevo (varios en transmision)."""
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
    return cfg


def load_config(path=None):
    """Lee la config que edita el panel. Si no existe la crea; si esta ilegible devuelve None (se conserva la anterior)."""
    p = Path(path or CONFIG_PATH)
    if not p.exists():
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config(cfg, p)
        return cfg
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    return normalize_config(cfg)


def normalize_url(tipo, direccion):
    """Convierte lo que escribe el usuario en una fuente de video que OpenCV entiende."""
    d = str(direccion).strip()
    if tipo == "usb":
        return int(d) if d.isdigit() else d
    if tipo == "rtsp":
        return d if "://" in d else "rtsp://" + d
    if "://" not in d:
        d = "http://" + d
    u = urlparse(d)
    if tipo == "celular" and u.path in ("", "/"):
        u = u._replace(path="/video")  # IP Webcam y DroidCam sirven el video en /video
    return urlunparse(u)


def device_source(dev, resolver, cfg=None):
    """(fuente, error). resolver(host) -> ip | None, solo se usa para la ESP32-S3-CAM por nombre mDNS."""
    tipo, direccion = dev.get("tipo", ""), str(dev.get("direccion", "")).strip()
    if tipo == "celularweb":  # celular vinculado por QR: el panel reenvia su video en /stream/<id>
        puerto = (cfg or {}).get("panel_puerto", 8000)
        return f"http://127.0.0.1:{puerto}/stream/{dev.get('id', '')}", None
    if not direccion:
        return None, "El dispositivo no tiene direccion."
    if tipo == "esp32cam" and "://" not in direccion:
        ip = resolver(direccion)
        if not ip:
            return None, (f"No encontre {direccion}. Revisa: hotspot activo, ESP32-S3-CAM encendida "
                          f"y esta laptop en el mismo hotspot.")
        return f"http://{ip}:81/stream", None
    return normalize_url(tipo, direccion), None


class LivePublisher:
    """Publica para el panel: el ultimo cuadro anotado de CADA camara (live/<id>.jpg) y el estado (estado.json)."""

    def __init__(self, live_dir, min_frame_interval=0.12, min_status_interval=1.0):
        self.dir = Path(live_dir)
        self.min_frame = min_frame_interval
        self.min_status = min_status_interval
        self.last_frame = {}
        self.last_status = 0.0

    def frame(self, cv2, cam_id, img, now):
        if now - self.last_frame.get(cam_id, 0.0) < self.min_frame:
            return
        self.last_frame[cam_id] = now
        try:
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                atomic_write(self.dir / f"{cam_id}.jpg", buf.tobytes())
        except Exception as e:  # el panel es opcional: nunca debe tumbar el motor
            print(f"[PANEL] No pude publicar el video: {e}")

    def due(self, now):
        return now - self.last_status >= self.min_status

    def status(self, data, now):
        self.last_status = now
        data = dict(data, ts=time.time())
        try:
            atomic_write(self.dir / "estado.json", json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            print(f"[PANEL] No pude publicar el estado: {e}")


# ============================================================
# UTILIDADES DE SISTEMA
# ============================================================
@contextmanager
def working_dir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def lower_priority():
    """Baja la prioridad del proceso para que la laptop siga fluida."""
    try:
        import psutil

        proc = psutil.Process()
        if os.name == "nt":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            proc.nice(10)
        print("[SISTEMA] Prioridad del proceso bajada (la laptop sigue fluida).")
    except Exception as e:  # no es critico
        print(f"[SISTEMA] No se pudo bajar la prioridad ({e}).")


def beep():
    try:
        import winsound

        threading.Thread(target=lambda: winsound.Beep(1000, 400), daemon=True).start()
    except ImportError:
        print("\a", end="")


def notify_actuator(host, missing):
    """Avisa al actuador (ESP32 / Atom EchoS3R): GET /alerta?tipo=casco,chaleco"""
    url = f"http://{host}/alerta?tipo={','.join(sorted(missing))}"

    def _send():
        try:
            import requests

            requests.get(url, timeout=2)
        except Exception as e:
            print(f"[ACTUADOR] No respondio ({url}): {e}")

    threading.Thread(target=_send, daemon=True).start()


# ============================================================
# CAMARA
# ============================================================
def resolve_camera(host, timeout_s=15):
    """Resuelve epp-cam01.local -> IP. Primero DNS de Windows, luego zeroconf."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        pass

    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    target = host.lower().removesuffix(".local")
    found = {}

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if info is None:
                return
            server = (info.server or "").rstrip(".").lower()
            if server.startswith(target) and info.addresses:
                found["ip"] = socket.inet_ntoa(info.addresses[0])

        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    ServiceBrowser(zc, "_http._tcp.local.", Listener())
    start = time.time()
    try:
        while time.time() - start < timeout_s and "ip" not in found:
            time.sleep(0.3)
    finally:
        zc.close()
    return found.get("ip")


class LatestFrameReader(threading.Thread):
    """Lee el video todo el tiempo y guarda solo el ultimo frame (evita retraso acumulado)."""

    def __init__(self, source, cv2):
        super().__init__(daemon=True)
        self.source = source
        self.cv2 = cv2
        self.cap = None
        self.frame = None
        self.seq = 0
        self.lock = threading.Lock()
        self.running = True

    def run(self):
        while self.running:
            if self.cap is None or not self.cap.isOpened():
                self.cap = self.cv2.VideoCapture(self.source)
                if not self.cap.isOpened():
                    time.sleep(2)
                    continue
            ok, frame = self.cap.read()
            if not ok:
                self.cap.release()
                self.cap = None
                time.sleep(1)
                continue
            with self.lock:
                self.frame = frame
                self.seq += 1

    def get(self):
        with self.lock:
            return self.frame, self.seq

    def stop(self):
        self.running = False
        if self.cap is not None:
            self.cap.release()


# ============================================================
# MODELOS
# ============================================================
def resolve_weights(path_str):
    p = Path(path_str)
    if p.parent == Path("."):  # nombre suelto -> carpeta modelos
        p = MODELOS_DIR / p.name
    return p


def load_model(YOLO, path_str, imgsz, use_openvino):
    weights = resolve_weights(path_str)
    if not weights.exists():
        if Path(path_str).parent == Path("."):
            print(f"[MODELO] Descargando {weights.name} (solo la primera vez)...")
            MODELOS_DIR.mkdir(parents=True, exist_ok=True)
            with working_dir(MODELOS_DIR):
                YOLO(weights.name)  # ultralytics lo descarga en la carpeta actual
        if not weights.exists():
            raise FileNotFoundError(
                f"No encuentro el modelo: {weights}\n"
                f"Descargalo y copialo ahi (ver instrucciones al final de la respuesta)."
            )

    if not use_openvino:
        return YOLO(str(weights)), "PyTorch"

    # El tamano de entrada queda fijo al exportar, por eso va en el nombre de la carpeta:
    # si cambias --ppe-imgsz / --person-imgsz se exporta de nuevo solo.
    ov_dir = weights.parent / f"{weights.stem}_{imgsz}_openvino_model"
    default_dir = weights.parent / f"{weights.stem}_openvino_model"
    try:
        if not ov_dir.exists():
            print(f"[MODELO] Exportando {weights.name} a OpenVINO ({imgsz}px, solo la primera vez, puede tardar)...")
            if default_dir.exists():  # sobrante de una version anterior del script
                shutil.rmtree(default_dir)
            YOLO(str(weights)).export(format="openvino", imgsz=imgsz)
            default_dir.rename(ov_dir)
        return YOLO(str(ov_dir), task="detect"), "OpenVINO"
    except Exception as e:
        print(f"[MODELO] OpenVINO fallo para {weights.name} ({e}). Uso PyTorch.")
        return YOLO(str(weights)), "PyTorch"


def ppe_detect(model, img, conf, imgsz, kw):
    """Corre el modelo de EPP. Devuelve [(id_clase, confianza, (x1, y1, x2, y2))] en coordenadas de img."""
    r = model.predict(img, conf=conf, imgsz=imgsz, verbose=False, **kw)[0]
    out = []
    if r.boxes is None:
        return out
    classes = r.boxes.cls.int().tolist()
    if not classes:
        return out
    xyxy = r.boxes.xyxy.cpu().numpy()
    confs = r.boxes.conf.cpu().numpy()
    for k, c, b in zip(classes, confs, xyxy):
        out.append((int(k), float(c), tuple(int(v) for v in b)))
    return out


# ============================================================
# EVENTOS (SQLite + evidencia)
# ============================================================
def init_db(path):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # el panel puede leer mientras el motor escribe
    except sqlite3.DatabaseError:
        pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS eventos (
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
    )
    conn.commit()
    return conn


def save_event(conn, cv2, annotated, missing, confidence, cam_id, zone):
    now = datetime.now()
    day_dir = EVIDENCIAS_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    tag = "-".join(sorted(missing))
    photo = day_dir / f"evt_{now.strftime('%H%M%S')}_{cam_id}_{tag}.jpg"
    cv2.imwrite(str(photo), annotated)
    conn.execute(
        "INSERT INTO eventos (ts, camara, zona, epp_faltante, foto, confianza, estado, sync) "
        "VALUES (?, ?, ?, ?, ?, ?, 'alertado', 0)",
        (now.isoformat(timespec="seconds"), cam_id, zone, ",".join(sorted(missing)),
         str(photo), float(confidence)),
    )
    conn.commit()
    return photo


# ============================================================
# DIBUJO
# ============================================================
def draw(cv2, frame, persons, status_text, debug_dets=(), names=None, thr=0.4):
    out = frame.copy()
    # --debug: todo lo que ve el modelo de EPP (amarillo = supera el umbral, naranja = no lo supera)
    for cid, cf, (a, b, c, d) in debug_dets:
        color = (0, 255, 255) if cf >= thr else (0, 140, 255)
        cv2.rectangle(out, (a, b), (c, d), color, 1)
        label = f"{(names or {}).get(cid, cid)} {cf:.2f}"
        cv2.putText(out, label, (a, max(12, b - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    for p in persons:
        x1, y1, x2, y2 = p["box"]
        ok = not p["missing"]
        color = (0, 180, 0) if ok else (0, 0, 255)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = "OK" if ok else "FALTA: " + ", ".join(p["missing"])
        cv2.putText(out, label, (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    cv2.putText(out, status_text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 1)
    return out


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================
def parse_args():
    ap = argparse.ArgumentParser(description="ARGOS EPP - Motor de IA (multicamara)")
    ap.add_argument("--source", default="panel",
                    help="panel = usar los dispositivos que activas en el panel (por defecto) | auto = buscar la "
                         "ESP32-S3-CAM por mDNS | 0 = webcam | URL del stream")
    ap.add_argument("--no-live", action="store_true", help="no publicar video/estado para el panel")
    ap.add_argument("--camera-host", default="epp-cam01.local", help="nombre mDNS de la camara (con --source auto)")
    ap.add_argument("--camera-id", default="cam-01", help="id de la camara (solo con --source distinto de panel)")
    ap.add_argument("--zone", default="Ingreso", help="zona (solo con --source distinto de panel)")
    ap.add_argument("--person-model", default="yolov8n.pt", help="modelo de personas (COCO)")
    ap.add_argument("--ppe-model", default="ppe.pt", help="modelo de EPP (en D:\\ARGOS-EPP\\modelos)")
    ap.add_argument("--require", default="casco,chaleco", help="EPP obligatorios (el panel los puede cambiar)")
    ap.add_argument("--person-only", action="store_true", help="solo personas, sin alertas (para probar camara/FPS)")
    ap.add_argument("--info", action="store_true", help="muestra las clases del modelo de EPP y sale")
    ap.add_argument("--person-conf", type=float, default=0.45)
    ap.add_argument("--ppe-conf", type=float, default=0.40)
    ap.add_argument("--person-imgsz", type=int, default=640)
    ap.add_argument("--ppe-imgsz", type=int, default=320)
    ap.add_argument("--ppe-mode", choices=["crop", "frame"], default="crop",
                    help="crop = busca EPP en el recorte de cada persona | frame = en todo el cuadro (usa --ppe-imgsz 640)")
    ap.add_argument("--hold", type=float, default=1.5,
                    help="segundos que un EPP sigue contando como puesto tras verlo por ultima vez (0 = sin memoria)")
    ap.add_argument("--epp-conf", default="casco=0.30,chaleco=0.35",
                    help="umbral propio por EPP, ej. casco=0.30,chaleco=0.45 (los no listados usan --ppe-conf)")
    ap.add_argument("--no-head-pass", action="store_true",
                    help="desactiva la 2da pasada sobre la cabeza (por defecto: si no ve casco, mira la cabeza ampliada)")
    ap.add_argument("--head-imgsz", type=int, default=640,
                    help="resolucion de la 2da pasada sobre la cabeza (640 reconoce mucho mejor el casco)")
    ap.add_argument("--head-frac", type=float, default=0.40,
                    help="fraccion superior de la persona que se considera cabeza (0.40 = 40%%)")
    ap.add_argument("--debug", action="store_true",
                    help="muestra TODO lo que detecta el modelo de EPP (incluso con baja confianza) para diagnosticar")
    ap.add_argument("--min-person-height", type=float, default=0.30,
                    help="altura minima de la persona (fraccion del alto del frame); ignora gente lejana")
    ap.add_argument("--max-persons", type=int, default=4)
    ap.add_argument("--frames", type=int, default=5, help="frames seguidos con el mismo faltante antes de alertar")
    ap.add_argument("--cooldown", type=float, default=10.0, help="segundos entre alertas (por camara)")
    ap.add_argument("--max-fps", type=float, default=8.0,
                    help="limite de FPS procesados EN TOTAL (se reparten entre las camaras activas)")
    ap.add_argument("--threads", type=int, default=4, help="hilos de CPU para PyTorch/OpenCV")
    ap.add_argument("--device", default=None, help="opcional, ej. intel:gpu (no probado en tu equipo)")
    ap.add_argument("--no-openvino", action="store_true", help="usar PyTorch normal en vez de OpenVINO")
    ap.add_argument("--actuator", default=None, help="host del actuador, ej. epp-actuador.local")
    ap.add_argument("--beep", action="store_true", help="pitido local en la laptop al alertar")
    ap.add_argument("--no-view", action="store_true", help="sin ventana de video (menos CPU)")
    return ap.parse_args()


class Slot:
    """Estado de una camara en transmision (cada una tiene su propio filtro y memoria)."""

    def __init__(self, dev):
        self.dev = dev
        self.id = dev["id"]
        self.key = None
        self.reader = None
        self.err = ""
        self.filt = None
        self.tracker = None
        self.last_seq = -1
        self.last_proc = 0.0
        self.last_frame_time = time.time()
        self.last_open_try = 0.0
        self.video_ok = False
        self.persons = 0
        self.missing = []
        self.last_alert = None
        self.n_proc = 0
        self.fps = 0.0

    @property
    def label(self):
        return f"{self.dev.get('zona', '')} / {self.dev.get('nombre', self.id)}"


def main():
    args = parse_args()

    # --- imports pesados (aqui, para que el resto sea facil de probar) ---
    import cv2
    from ultralytics import YOLO

    cv2.setNumThreads(args.threads)
    try:
        import torch

        torch.set_num_threads(args.threads)
    except Exception:
        pass
    lower_priority()

    use_ov = not args.no_openvino
    kw = {"device": args.device} if args.device else {}

    # --- modelo de EPP ---
    ppe_model, head_model, ppe_backend = None, None, ""
    ppe_names, class_map, detectable = {}, {}, set()
    required = [r.strip() for r in args.require.split(",") if r.strip()]
    per_class = parse_epp_conf(args.epp_conf)
    if not args.person_only or args.info:
        try:
            ppe_model, ppe_backend = load_model(YOLO, args.ppe_model, args.ppe_imgsz, use_ov)
        except FileNotFoundError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
        ppe_names = dict(ppe_model.names)  # se lee una sola vez (cada lectura recarga el modelo OpenVINO)
        class_map = build_class_map(ppe_names)
        print(f"[MODELO EPP] Clases del modelo: {ppe_names}")
        print(f"[MODELO EPP] Clases que uso: { {ppe_names[k]: v for k, v in class_map.items()} }")
        if args.info:
            return
        head_model = ppe_model
        if not args.no_head_pass and args.head_imgsz != args.ppe_imgsz:
            head_model, _ = load_model(YOLO, args.ppe_model, args.head_imgsz, use_ov)
        detectable = set(class_map.values())
        not_detectable = [r for r in required if r not in detectable]
        if not_detectable:
            print(f"[AVISO] Tu modelo NO detecta: {not_detectable}. Los quito de los obligatorios.")
            required = [r for r in required if r in detectable]
        if not required:
            print("[ERROR] Ningun EPP obligatorio es detectable con este modelo. Revisa --require o cambia de modelo.")
            sys.exit(1)

    # --- modelo de personas ---
    person_model, person_backend = load_model(YOLO, args.person_model, args.person_imgsz, use_ov)
    print(f"[MODELOS] Personas: {person_backend}" + (f" | EPP: {ppe_backend}" if ppe_model else ""))

    # --- dispositivos ---
    # panel = los dispositivos "en transmision" se eligen en el panel (config/dispositivos.json) y se pueden
    # prender/apagar en caliente. Con --source distinto de panel se usa una sola camara, como antes.
    panel_mode = args.source == "panel"
    if panel_mode:
        cfg = load_config()
        print(f"[PANEL] Modo panel: los dispositivos se administran en el panel ({CONFIG_PATH})")
    else:
        if args.source == "auto":
            print(f"[CAMARA] Buscando {args.camera_host} ...")
            ip = resolve_camera(args.camera_host)
            if not ip:
                print("[ERROR] No encontre la camara. Revisa: hotspot activo, ESP32-S3-CAM encendida y esta laptop en el mismo hotspot.")
                print("        Tambien puedes pasar la IP a mano:  --source http://IP:81/stream")
                sys.exit(1)
            dev = {"tipo": "url", "direccion": f"http://{ip}:81/stream"}
        elif args.source.isdigit():
            dev = {"tipo": "usb", "direccion": args.source}
        else:
            dev = {"tipo": "url", "direccion": args.source}
        dev.update({"id": args.camera_id, "nombre": args.camera_id, "zona": args.zone, "habilitado": True, "deteccion": True})
        cfg = normalize_config({"dispositivos": [dev], "requeridos": required})

    conn = init_db(DB_PATH)
    live = LivePublisher(LIVE_DIR)
    slots = {}
    det_global, actuator = True, args.actuator
    modo = "SOLO PERSONAS" if args.person_only else f"EPP requeridos: {', '.join(required)}"
    dbg = {"t": 0.0}

    # ------------------------------------------------------------
    def open_slot(slot, dev, now):
        slot.last_open_try = now
        if slot.reader is not None:
            slot.reader.stop()
            slot.reader = None
        slot.key = (dev["tipo"], dev["direccion"])
        source, err = device_source(dev, lambda hst: resolve_camera(hst, 4), cfg)
        if err:
            if err != slot.err:
                print(f"[CAMARA] {slot.label}: {err}")
            slot.err = err
        else:
            slot.err = ""
            print(f"[CAMARA] {slot.label}: {source}")
            slot.reader = LatestFrameReader(source, cv2)
            slot.reader.start()
        slot.filt = PersistenceFilter(args.frames, args.cooldown)
        slot.tracker = PersonTracker(args.hold)
        slot.last_seq, slot.last_frame_time = -1, now

    def raw_frame(frame, slot, motivo):
        out = frame.copy()
        cv2.putText(out, f"{slot.label} | {motivo}", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 1)
        return out

    def analyze(slot, frame, now):
        """Personas + EPP de un cuadro. Devuelve (imagen anotada, personas, EPP faltantes, EPP a alertar)."""
        h, w = frame.shape[:2]

        # 1) personas
        res = person_model.predict(frame, classes=[0], conf=args.person_conf,
                                   imgsz=args.person_imgsz, verbose=False, **kw)[0]
        boxes = res.boxes.xyxy.cpu().numpy() if res.boxes is not None else []
        confs = res.boxes.conf.cpu().numpy() if res.boxes is not None else []

        candidates = []
        order = sorted(range(len(boxes)), key=lambda i: -(boxes[i][3] - boxes[i][1]))
        for i in order[: args.max_persons]:
            x1, y1, x2, y2 = [int(v) for v in boxes[i]]
            if (y2 - y1) < args.min_person_height * h:
                continue  # persona muy lejos / muy pequena
            candidates.append(((x1, y1, x2, y2), float(confs[i])))

        # 2) EPP de cada persona
        # En --debug el modelo corre con confianza muy baja (0.05) para poder VER que detecta;
        # el umbral real (--ppe-conf) se aplica despues, en items_for_person.
        run_conf = min(args.ppe_conf, 0.05) if args.debug else args.ppe_conf
        debug_dets = []
        frame_dets = None
        if ppe_model is not None and candidates and args.ppe_mode == "frame":
            frame_dets = ppe_detect(ppe_model, frame, run_conf, args.ppe_imgsz, kw)

        persons = []
        for box, pconf in candidates:
            missing, found = [], set()
            if ppe_model is not None:
                pbox = pad_box(box, w, h)
                if args.ppe_mode == "frame":
                    dets = frame_dets
                else:
                    cx1, cy1, cx2, cy2 = pbox
                    crop = frame[cy1:cy2, cx1:cx2]
                    if crop.size == 0 or min(crop.shape[:2]) < 20:
                        continue
                    dets = [(cid, cf, (a + cx1, b + cy1, c + cx1, d + cy1))
                            for cid, cf, (a, b, c, d) in
                            ppe_detect(ppe_model, crop, run_conf, args.ppe_imgsz, kw)]
                    debug_dets.extend(dets)
                found = items_for_person(dets, class_map, pbox, args.ppe_conf, per_class)
                # 2da pasada: si falta el casco, mira SOLO la cabeza (ampliada); el casco ocupa mas
                # imagen y el modelo lo reconoce mucho mejor que dentro del cuerpo completo.
                if not args.no_head_pass and "casco" in required and "casco" not in found:
                    hx1, hy1, hx2, hy2 = head_region(box, w, h, args.head_frac)
                    head = frame[hy1:hy2, hx1:hx2]
                    if head.size and min(head.shape[:2]) >= 20:
                        hdets = [(cid, cf, (a + hx1, b + hy1, c + hx1, d + hy1))
                                 for cid, cf, (a, b, c, d) in
                                 ppe_detect(head_model, head, run_conf, args.head_imgsz, kw)]
                        debug_dets.extend(hdets)
                        found |= items_for_person(hdets, class_map, (hx1, hy1, hx2, hy2),
                                                  args.ppe_conf, per_class)
                missing = [r for r in required if r not in found]
            persons.append({"box": box, "missing": missing, "conf": pconf, "found": found})
        if frame_dets is not None:
            debug_dets = frame_dets
        if ppe_model is not None and args.hold > 0 and persons:
            effective = slot.tracker.update([(p["box"], p["found"]) for p in persons], now, w)
            for p, eff in zip(persons, effective):
                p["missing"] = [r for r in required if r not in eff]

        # 3) persistencia + alerta
        frame_missing = set().union(*[p["missing"] for p in persons]) if persons else set()
        to_alert = slot.filt.update(frame_missing, now) if ppe_model is not None else set()

        status = f"{slot.label} | {modo} | personas: {len(persons)}"
        if args.debug and ppe_model is not None:
            annotated = draw(cv2, frame, persons, status, debug_dets, ppe_names,
                             min([args.ppe_conf, *per_class.values()]))
            if persons and now - dbg["t"] >= 1.0:
                dbg["t"] = now
                txt = " | ".join(f"{ppe_names.get(c, c)} {cf:.2f}"
                                 for c, cf, _ in sorted(debug_dets, key=lambda d: -d[1])[:8])
                tag = f":{slot.id}" if len(slots) > 1 else ""
                print(f"[DEBUG{tag}] EPP visto por el modelo (umbral {args.ppe_conf}): "
                      f"{txt or 'NADA (ni con confianza 0.05)'}")
        else:
            annotated = draw(cv2, frame, persons, status)
        return annotated, persons, frame_missing, to_alert

    def build_status(now):
        devs = {}
        for s in slots.values():
            on = det_global and s.dev.get("deteccion", True)
            devs[s.id] = {
                "nombre": s.dev.get("nombre", s.id), "zona": s.dev.get("zona", ""),
                "video_ok": bool(s.video_ok and now - s.last_frame_time < 5),
                "mensaje": s.err or ("" if s.video_ok else "Sin video"),
                "deteccion": bool(on), "fps": round(s.fps, 1), "personas": s.persons,
                "faltantes": s.missing, "ultima_alerta": s.last_alert,
            }
        return {"motor": {"fps": round(sum(s.fps for s in slots.values()), 1), "requeridos": required,
                          "deteccion_global": det_global, "modo": modo, "camaras": len(slots)},
                "dispositivos": devs}

    # ------------------------------------------------------------
    print(f"[LISTO] {modo} | {args.max_fps} FPS max en total | alerta tras {args.frames} frames | q = salir, Ctrl+C = parar\n")
    last_cfg_check, t_stats = 0.0, time.time()
    try:
        while True:
            now = time.time()

            # --- panel: leer config cada 1.5 s (dispositivos, EPP requeridos, switches, actuador) ---
            if now - last_cfg_check >= 1.5:
                last_cfg_check = now
                if panel_mode:
                    new_cfg = load_config()
                    if new_cfg is not None:
                        cfg = new_cfg
                    if ppe_model is not None:
                        req = [r for r in cfg.get("requeridos", []) if r in detectable]
                        if req and req != required:
                            required = req
                            modo = f"EPP requeridos: {', '.join(required)}"
                            print(f"[PANEL] EPP requeridos: {', '.join(required)}")
                    actuator = args.actuator or cfg.get("actuador") or None
                new_det = bool(cfg.get("deteccion_global", True))
                if new_det != det_global:
                    print(f"[PANEL] Reconocimiento de EPP {'ACTIVADO' if new_det else 'DESACTIVADO'} (global)")
                det_global = new_det
                enabled = {d["id"]: d for d in cfg["dispositivos"] if d.get("habilitado", True)}
                for sid in [s for s in slots if s not in enabled]:
                    if slots[sid].reader is not None:
                        slots[sid].reader.stop()
                    print(f"[CAMARA] {slots[sid].label}: transmision detenida")
                    del slots[sid]
                for sid, dev in enabled.items():
                    slot = slots.get(sid)
                    if slot is None:
                        slot = slots[sid] = Slot(dev)
                    slot.dev = dev
                    if (dev["tipo"], dev["direccion"]) != slot.key or (slot.reader is None and now - slot.last_open_try >= 5):
                        open_slot(slot, dev, now)

            # --- procesar un cuadro nuevo de cada camara (reparte los FPS entre todas) ---
            n_active = max(1, sum(1 for s in slots.values() if s.reader is not None))
            interval = n_active / max(0.5, args.max_fps)
            did, first_view, quit_now = False, None, False
            for slot in sorted(slots.values(), key=lambda s: s.last_proc):
                if slot.reader is None:
                    slot.video_ok = False
                    continue
                frame, seq = slot.reader.get()
                now = time.time()
                if frame is None or seq == slot.last_seq:
                    if now - slot.last_frame_time > 5:
                        if slot.video_ok:
                            print(f"[VIDEO] {slot.label}: sin frames hace mas de 5 s (reconectando...)")
                        slot.video_ok = False
                    continue
                if now - slot.last_proc < interval:
                    continue
                slot.last_seq, slot.last_proc, slot.last_frame_time = seq, now, now
                slot.video_ok = True

                detect_on = det_global and slot.dev.get("deteccion", True) and (ppe_model is not None or args.person_only)
                if detect_on:
                    annotated, persons, frame_missing, to_alert = analyze(slot, frame, now)
                    slot.persons, slot.missing = len(persons), sorted(frame_missing)
                    if to_alert:
                        conf = max((p["conf"] for p in persons if p["missing"]), default=0.0)
                        photo = save_event(conn, cv2, annotated, to_alert, conf, slot.id, slot.dev.get("zona", ""))
                        print(f"[ALERTA] {datetime.now():%H:%M:%S} [{slot.label}] falta: {', '.join(sorted(to_alert))} -> {photo}")
                        slot.last_alert = {"ts": datetime.now().isoformat(timespec="seconds"),
                                           "faltantes": sorted(to_alert), "foto": str(photo)}
                        if args.beep:
                            beep()
                        if actuator:
                            notify_actuator(actuator, to_alert)
                else:
                    annotated = raw_frame(frame, slot, "reconocimiento desactivado")
                    slot.persons, slot.missing = 0, []

                if not args.no_live:
                    live.frame(cv2, slot.id, annotated, now)
                slot.n_proc += 1
                did = True
                if first_view is None:
                    first_view = annotated

            # --- estado para el panel ---
            if not args.no_live and live.due(time.time()):
                live.status(build_status(time.time()), time.time())

            # --- ventana local (opcional) ---
            if not args.no_view and first_view is not None:
                cv2.imshow("ARGOS EPP  (q = salir)", first_view)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            # --- estadisticas ---
            now = time.time()
            if now - t_stats >= 5:
                for s in slots.values():
                    s.fps = s.n_proc / (now - t_stats)
                    s.n_proc = 0
                total = sum(s.fps for s in slots.values())
                detalle = " | ".join(f"{s.id}: {s.fps:.1f}" for s in slots.values())
                print(f"[FPS] {total:.1f} procesados/s en total ({detalle or 'sin camaras activas'}) "
                      f"| personas en vista: {sum(s.persons for s in slots.values())}")
                t_stats = now

            if not did:
                time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n[PARADO] Ctrl+C recibido.")
    finally:
        for s in slots.values():
            if s.reader is not None:
                s.reader.stop()
        conn.close()
        if not args.no_view:
            cv2.destroyAllWindows()
        print("[FIN] Motor detenido.")


if __name__ == "__main__":
    main()
