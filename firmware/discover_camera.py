"""
============================================================
Sistema Inteligente de Deteccion de EPP - Peru
Script de descubrimiento automatico de camara (mDNS)
============================================================

Que hace:
    Busca la ESP32-S3-CAM en la red local por su nombre mDNS
    (epp-cam01.local) sin que tengas que escribir ninguna IP.
    Confirma que el stream y el endpoint /health respondan,
    y te deja la URL lista para copiar al motor de IA.

Uso (un solo comando):
    python discover_camera.py

Requisitos (una sola vez):
    pip install zeroconf requests

Notas:
    - Debes estar conectado al MISMO hotspot/red que el ESP32.
    - Si tienes varias camaras, cambia HOSTNAMES abajo o pasa
      el nombre como argumento: python discover_camera.py epp-cam02
============================================================
"""

import sys
import time
import socket
import requests
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

# Nombres mDNS a buscar (deben coincidir con MDNS_HOSTNAME en el firmware)
DEFAULT_HOSTNAMES = ["epp-cam01"]
DISCOVERY_TIMEOUT_S = 15
HEALTH_TIMEOUT_S = 3


class CameraListener(ServiceListener):
    def __init__(self, target_hostnames):
        self.target_hostnames = [h.lower() for h in target_hostnames]
        self.found = {}  # hostname -> ip

    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name)
        if info is None:
            return
        server = (info.server or "").rstrip(".").lower()
        for target in self.target_hostnames:
            if server.startswith(target):
                ip = socket.inet_ntoa(info.addresses[0]) if info.addresses else None
                if ip:
                    self.found[target] = ip

    def update_service(self, zc, type_, name):
        pass

    def remove_service(self, zc, type_, name):
        pass


def discover(hostnames):
    print(f"[BUSCANDO] Escaneando la red por: {', '.join(h + '.local' for h in hostnames)}")
    print("           (asegurate de estar conectado al mismo hotspot/WiFi que el ESP32)\n")

    zeroconf = Zeroconf()
    listener = CameraListener(hostnames)
    ServiceBrowser(zeroconf, "_http._tcp.local.", listener)

    start = time.time()
    try:
        while time.time() - start < DISCOVERY_TIMEOUT_S:
            if len(listener.found) >= len(hostnames):
                break
            time.sleep(0.5)
    finally:
        zeroconf.close()

    return listener.found


def check_health(ip):
    try:
        resp = requests.get(f"http://{ip}/health", timeout=HEALTH_TIMEOUT_S)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def main():
    hostnames = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_HOSTNAMES
    found = discover(hostnames)

    if not found:
        print("[ERROR] No se encontro ninguna camara.")
        print("        Verifica:")
        print("        1. Que el hotspot este activo")
        print("        2. Que el ESP32-S3-CAM este encendido y con el LED fijo (conectado)")
        print("        3. Que tu laptop este conectada al MISMO hotspot")
        sys.exit(1)

    print("[OK] Camara(s) encontrada(s):\n")
    results = []
    for hostname, ip in found.items():
        health = check_health(ip)
        status = "ONLINE" if health else "SIN RESPUESTA EN /health"
        print(f"  * {hostname}.local  ->  {ip}   [{status}]")
        if health:
            print(f"      zona: {health.get('zone', '?')}  |  rssi: {health.get('rssi', '?')} dBm  "
                  f"|  uptime: {health.get('uptime_s', '?')}s")
        print(f"      Stream:  http://{ip}:81/stream")
        print(f"      Capture: http://{ip}/capture")
        print()
        results.append({
            "hostname": hostname,
            "ip": ip,
            "stream_url": f"http://{ip}:81/stream",
            "capture_url": f"http://{ip}/capture",
            "health_url": f"http://{ip}/health",
            "online": health is not None,
        })

    print("[LISTO] Copia la URL de 'Stream' de arriba en el motor de IA, o usa este")
    print("        script importado como modulo para obtenerla automaticamente.")
    return results


if __name__ == "__main__":
    main()
