# ARGOS EPP — proyecto completo

## Estructura
```
D:\ARGOS-EPP
├─ motor-ia\       motor_epp.py (v6: multicámara, switches de reconocimiento)
├─ panel\          servidor.py + interfaz web (v2.1)
├─ modelos\        ppe.pt  (yolov8n.pt se descarga solo la primera vez)
├─ firmware\       esp32_s3_cam_firmware.ino, discover_camera.py
├─ entrenamiento\  capturar_dataset.py, entrenar_colab.py
├─ docs\           laboratorios, lista de componentes, contexto
├─ evidencias\     fotos de infracciones (una carpeta por día)
├─ config\         dispositivos.json (lo crea el panel)
├─ eventos.db      base de alertas (la crea el motor)
└─ instalar.bat / iniciar_todo.bat / iniciar_motor.bat / iniciar_panel.bat / detener_todo.bat
```
El zip NO trae config\, eventos.db ni .venv a propósito: así no pisa tus dispositivos, alertas ni fotos.

## Instalar sobre tu carpeta actual (CMD como administrador)
Abre CMD como administrador (menú Inicio → escribe cmd → clic derecho → Ejecutar como administrador).

```
:: 0) cierra motor y panel si están corriendo (Ctrl+C en sus ventanas) o:
taskkill /F /IM python.exe

:: 1) respaldo de seguridad (sin .venv ni cache)
robocopy D:\ARGOS-EPP D:\ARGOS-EPP_respaldo /E /XD .venv .pip-cache

:: 2) extraer el zip encima (mantiene tus evidencias, eventos.db y config)
tar -xf "%USERPROFILE%\Downloads\ARGOS_EPP_Completo.zip" -C D:\

:: 3) instalar dependencias y abrir el firewall
cd /d D:\ARGOS-EPP
instalar.bat

:: 4) arrancar todo
iniciar_todo.bat
```
Panel: http://localhost:8000

## Empezar totalmente de cero
Solo si ya hiciste el respaldo del paso 1 (esto borra evidencias y alertas):
```
cd /d D:\
rmdir /s /q D:\ARGOS-EPP
tar -xf "%USERPROFILE%\Downloads\ARGOS_EPP_Completo.zip" -C D:\
cd /d D:\ARGOS-EPP
instalar.bat
iniciar_todo.bat
```
Si quieres conservar solo las evidencias: copia antes `D:\ARGOS-EPP\evidencias` y `D:\ARGOS-EPP\eventos.db` a otro lado y devuélvelas después.

## Uso diario
```
cd /d D:\ARGOS-EPP
iniciar_todo.bat        :: abre motor + panel + navegador
detener_todo.bat        :: los cierra
```
Por separado: `iniciar_motor.bat` y `iniciar_panel.bat`.
Motor con ventana de video (depuración): `iniciar_motor.bat` sin `--no-view` →
`.venv\Scripts\python motor-ia\motor_epp.py --ppe-imgsz 416`

## Comandos de emergencia
```
taskkill /F /IM python.exe                         :: mata motor y panel
netstat -ano | findstr :8000                       :: ¿quién usa el puerto 8000?
del /q D:\ARGOS-EPP\config\dispositivos.json       :: reinicia dispositivos (el panel crea uno nuevo)
netsh advfirewall firewall delete rule name="ARGOS Panel"   :: quita la regla del firewall
```

## Cómo usar los dispositivos
- **Webcam de la laptop:** Dispositivos → «Webcam de la laptop» → Guardar → enciende su switch «Transmisión».
- **ESP32-S3-CAM:** misma red/hotspot que la laptop → «Buscar dispositivos» (radar) o agregar por `epp-cam01.local`.
- **Celular por QR:** Dispositivos → «Vincular celular» → escanea el QR → acepta la advertencia del certificado una vez → «Iniciar transmisión».
- **Reconocimiento on/off:** switch general arriba a la derecha y uno por dispositivo.
