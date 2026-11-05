"""
ARGOS EPP - Captura de fotos para entrenar el modelo.

Uso (PowerShell, con el venv activado, desde D:\\ARGOS-EPP\\entrenamiento):
    python capturar_dataset.py                    # webcam 0
    python capturar_dataset.py --source http://epp-cam01.local:81/stream
    python capturar_dataset.py --tag casco_si     # prefijo en el nombre

Teclas:
    ESPACIO  guarda una foto
    a        activa/desactiva captura automatica (1 foto cada --intervalo s)
    q        salir
Las fotos se guardan en  ..\\entrenamiento\\fotos_crudas\\
"""
import argparse
import time
from pathlib import Path

import cv2

OUT = Path(__file__).resolve().parent / "fotos_crudas"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0")
    ap.add_argument("--tag", default="foto")
    ap.add_argument("--intervalo", type=float, default=1.0)
    args = ap.parse_args()

    src = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise SystemExit(f"No pude abrir la fuente: {args.source}")

    OUT.mkdir(parents=True, exist_ok=True)
    auto = False
    last = 0.0
    n = len(list(OUT.glob("*.jpg")))
    print(f"Guardando en {OUT}  (ya hay {n} fotos). ESPACIO=foto, a=auto, q=salir")

    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue

        now = time.time()
        save = False
        if auto and now - last >= args.intervalo:
            save = True
        view = frame.copy()
        cv2.putText(view, f"fotos: {n}  auto: {'ON' if auto else 'off'}", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.imshow("ARGOS captura", view)

        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        if k == ord("a"):
            auto = not auto
        if k == 32:
            save = True

        if save:
            name = f"{args.tag}_{time.strftime('%Y%m%d_%H%M%S')}_{int(now*1000)%1000:03d}.jpg"
            cv2.imwrite(str(OUT / name), frame)
            n += 1
            last = now

    cap.release()
    cv2.destroyAllWindows()
    print(f"Listo. Total fotos: {n}")


if __name__ == "__main__":
    main()
