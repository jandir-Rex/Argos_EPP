# ARGOS EPP - Entrenamiento en Google Colab (GPU gratis)
# Colab: Entorno de ejecucion > Cambiar tipo de entorno > T4 GPU
# Copia cada bloque (separado por # ===== CELDA) en una celda distinta.

# ===== CELDA 1: instalar
!pip -q install ultralytics roboflow

# ===== CELDA 2: descargar tu dataset desde Roboflow (formato YOLOv8)
# En Roboflow: Versions > Export Dataset > YOLOv8 > "show download code" y pega aqui tus datos
from roboflow import Roboflow
rf = Roboflow(api_key="TU_API_KEY")
project = rf.workspace("TU_WORKSPACE").project("TU_PROYECTO")
dataset = project.version(1).download("yolov8")
print(dataset.location)

# ===== CELDA 3: entrenar (fine-tuning, parte de un modelo ya entrenado, NO de cero)
from ultralytics import YOLO
model = YOLO("yolov8n.pt")            # base ligera; si subes ppe.pt a Colab, usa YOLO("ppe.pt")
model.train(
    data=f"{dataset.location}/data.yaml",
    imgsz=416,          # mismo tamano con el que lo vas a exportar y usar
    epochs=80,
    batch=16,
    patience=20,        # corta si deja de mejorar
    degrees=5, fliplr=0.5, hsv_h=0.02, hsv_s=0.6, hsv_v=0.4,   # variacion de luz/color de obra
    mosaic=1.0, close_mosaic=10,
    project="argos", name="epp_v1",
)

# ===== CELDA 4: ver resultados y probar
metrics = model.val()
print("mAP50:", metrics.box.map50, " mAP50-95:", metrics.box.map)
# Matriz de confusion y curvas: argos/epp_v1/  (confusion_matrix.png, results.png)

# ===== CELDA 5: descargar el modelo
from google.colab import files
files.download("argos/epp_v1/weights/best.pt")
