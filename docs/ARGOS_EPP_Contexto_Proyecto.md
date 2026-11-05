# ARGOS EPP — Documento de Contexto del Proyecto

Sistema Inteligente de Detección de Equipos de Protección Personal (EPP) mediante Inteligencia Artificial e IoT, para el sector construcción, minería y manufactura en Perú.

---

## 1. Problema y objetivo

**Problema:** En obras de construcción, minas y plantas industriales del Perú, la supervisión del uso de EPP (casco, chaleco reflectivo, lentes de seguridad, guantes) depende de inspección visual manual por personal SSOMA. Esto es inconsistente, no escala a turnos completos, y no deja evidencia sistemática para auditorías de SUNAFIL bajo la Ley N° 29783. Además, muchas obras/minas están en zonas sin internet estable (sierra, selva, minas alejadas).

**Objetivo:** Construir un sistema que detecte automáticamente, mediante visión por computadora, si un trabajador porta el EPP requerido al ingresar a la obra, emita una alerta inmediata indicando qué le falta, registre evidencia fotográfica, y consolide todo en reportes y un dashboard/app para el supervisor SSOMA — funcionando incluso sin conexión a internet.

**Principio de diseño central:** la decisión de alarma siempre es local (offline-first); el registro/reportes/vista remota usa la nube cuando hay conectividad.

---

## 2. Casos de uso

### Caso de uso A — Checkpoint fijo en la entrada de obra (uso principal, Fase 1)

**Escenario:** Cada mañana, al inicio del turno, los trabajadores hacen fila en la entrada de la obra. Una cámara ESP32-S3-CAM apunta a esa fila.

**Flujo paso a paso:**
1. El trabajador se acerca al punto de control.
2. La cámara captura su imagen y la transmite en vivo al motor de IA (laptop, por ahora).
3. El modelo YOLOv8 analiza el frame y determina si porta casco y chaleco reflectivo (fase inicial de clases).
4. **Si tiene todo el EPP:** no pasa nada, el flujo continúa sin fricción.
5. **Si falta algo:** suena una alarma/mensaje de voz local indicando específicamente qué falta (ej. "Trabajador, colóquese el casco"), se toma una foto de evidencia, y se genera un evento con fecha, hora, zona y tipo de infracción.
6. El evento se guarda localmente (SQLite) de inmediato.
7. Si hay internet disponible en ese momento, el evento (foto + JSON) se sincroniza a la nube (AWS). Si no hay internet, queda en cola y se sincroniza apenas vuelva la señal.
8. El supervisor SSOMA revisa el dashboard (en la misma red local o desde la nube) y puede marcar cada evento como "válido" o "falso positivo".
9. Al final del día/semana, se genera un reporte de cumplimiento por trabajador/zona/fecha.

**Duración típica de operación:** ventana corta (30-60 min, inicio de turno), no vigilancia continua de 8-12 horas — eso queda para la Fase 2.

### Caso de uso B — Modo móvil / ronda de inspección (Fase 2, complementario)

**Escenario:** Un supervisor SSOMA recorre distintas zonas de una obra grande (no solo la entrada) con un kit portátil.

**Flujo paso a paso:**
1. El supervisor activa el hotspot de su celular (nombre y clave fijos, configurados una vez).
2. Lleva consigo un ESP32-S3-CAM alimentado por power bank, que se conecta automáticamente a ese hotspot.
3. Apunta la cámara a un grupo de trabajadores en la zona que está inspeccionando.
4. La inferencia corre en la nube (usando los datos móviles del celular) o en un laptop cercano, según disponibilidad.
5. Si detecta una infracción, se registra el evento con foto, zona y hora — no requiere alarma instantánea porque el supervisor está presente y puede actuar directamente.
6. Al terminar la ronda, los eventos quedan en el mismo sistema de reportes que el checkpoint fijo.

**Diferencia clave con el caso A:** es temporal (minutos), no de operación continua, y no depende de un router dedicado.

### Caso de uso C — Consulta remota fuera de la obra (Fase 2, nube)

**Escenario:** Un gerente de SSOMA, desde su oficina en otra ciudad, quiere ver el cumplimiento de EPP de varias obras.

**Flujo:**
1. Abre el dashboard web o la app móvil fuera de la red local de la obra.
2. La app/dashboard detecta que no está en la LAN local y consulta la nube (AWS) en vez del servidor local.
3. Ve reportes consolidados de todas las obras sincronizadas, recibe notificaciones push si se configuró una alerta de umbral (ej. "más de 5 infracciones hoy en Obra Norte").

### Caso de uso D — Auditoría SUNAFIL / evidencia legal

**Escenario:** La empresa cliente recibe una inspección de SUNAFIL y debe demostrar que fiscaliza el uso de EPP.

**Flujo:**
1. El supervisor exporta un reporte filtrado por fecha/zona desde el dashboard.
2. El reporte incluye: fecha, hora, trabajador (si hay identificación), tipo de EPP faltante, foto de evidencia, y si fue revisado por un supervisor.
3. Sirve como evidencia documentada de fiscalización activa, alineada a la Ley N° 29783.

---

## 3. Arquitectura general

```
[ESP32-S3-CAM] --stream WiFi local--> [Motor de IA: YOLOv8]
     (ojos)                                    │
                                    alarma local│ evento + foto (si hay infracción)
                              (parlante/relé)   │
                                                ▼
                                    [Backend local: FastAPI + SQLite]
                                         │ (dashboard accesible en LAN)
                                         │
                              sincroniza cuando hay internet
                                         ▼
                          [Nube: AWS — S3, RDS/Postgres, backend central]
                                         │
                            ┌────────────┴────────────┐
                            ▼                          ▼
                    Dashboard web (remoto)      App móvil (remota)
```

**Red local de la obra:** un router (o el hotspot del celular en el MVP) crea una LAN local. El ESP32-S3-CAM y el motor de IA/backend están en esa misma red — no dependen de internet para hablar entre sí.

**Descubrimiento automático:** el ESP32-S3-CAM se anuncia por mDNS (`epp-cam01.local`), así el motor de IA lo encuentra sin necesidad de configurar IPs manualmente cada vez.

**Patrón offline-first (outbox):** todo evento se guarda localmente primero; un proceso en background intenta sincronizar a la nube cada cierto tiempo, y si no hay internet, reintenta después sin perder datos.

---

## 4. Hardware / componentes

| Componente | Detalle actual | Rol |
|---|---|---|
| Cámara | ESP32-S3-CAM, chip N16R8 (16MB flash / 8MB PSRAM), sensor OV3660 | Captura y transmite video (streaming MJPEG), no procesa IA |
| Cómputo de IA (actual) | Laptop Lenovo 83A1, Intel Core i7-1355U (13va gen, GPU integrada Iris Xe, sin GPU dedicada), 16GB RAM | Corre el modelo YOLOv8 (con aceleración OpenVINO por la Iris Xe) |
| Red local (MVP) | Hotspot del celular del usuario | Reemplaza a un router dedicado durante el checkpoint corto |
| Red local (Fase 2, obra grande) | Router 4G dedicado | Da red local + internet 24/7 sin depender de un celular |
| Cómputo de IA (Fase 2) | Raspberry Pi 5 (posible + Google Coral USB Accelerator) | Reemplaza al laptop para operación fija 24/7 sin comprometer el equipo personal |
| Actuador de alarma | ESP32 DevKit + relé + parlante/sirena | Reproduce el mensaje de audio o activa la sirena ante una infracción |

---

## 5. Software / stack tecnológico

| Capa | Tecnología | Estado |
|---|---|---|
| Firmware cámara | Arduino/ESP-IDF (`esp_camera`, `ESPmDNS`) | Construido (streaming MJPEG, captura, salud, mDNS) |
| Descubrimiento | Script Python con `zeroconf` | Construido (`discover_camera.py`) |
| Motor de IA | Python + Ultralytics YOLOv8n + OpenVINO (para la Iris Xe del laptop) + ByteTrack (tracking) | Pendiente |
| Backend | FastAPI + SQLAlchemy + SQLite (local) / PostgreSQL (nube) | Pendiente |
| Sincronización | Patrón outbox (cola local → nube cuando hay internet) | Pendiente |
| Nube | AWS: S3 (fotos), RDS/PostgreSQL (datos), backend en EC2/Lambda, Cognito (auth), SNS/Firebase (push) | Pendiente (decisión: AWS) |
| Dashboard web | React + WebSocket (tiempo real) | Pendiente |
| App móvil | Flutter | Pendiente |
| Alarma de audio | Audios pre-grabados por combinación de EPP faltante (o generados una vez con AWS Polly y cacheados) | Pendiente (decisión de enfoque) |

---

## 6. Clases de EPP a detectar (por fase)

- **Fase 1 (MVP):** casco, chaleco reflectivo.
- **Fase 2:** + lentes de seguridad, guantes.
- **Fase 3:** + botas con punta de acero.

Nota técnica: **Amazon Rekognition PPE Detection** existe como servicio, pero solo detecta cobertor de cabeza/rostro/manos — no detecta chaleco reflectivo ni botas. Por eso el modelo de detección se construye con YOLOv8 propio, entrenado con dataset peruano, y no se depende de ese servicio para las clases críticas de este proyecto.

---

## 7. Modelo de datos del evento (referencia)

```json
{
  "id_camara": "epp-cam01",
  "zona": "zona-ingreso",
  "timestamp": "2026-09-25T06:15:32-05:00",
  "tipo_epp_faltante": ["casco", "chaleco"],
  "foto_evidencia_url": "s3://argos-epp/evidencias/evt_00123.jpg",
  "confianza_deteccion": 0.91,
  "estado": "alertado"
}
```

Estados posibles: `alertado`, `revisado_por_supervisor`, `descartado_falso_positivo`.

---

## 8. Roadmap / fases

| Fase | Alcance | Hardware |
|---|---|---|
| Fase 1 (actual) | Checkpoint fijo en la entrada, ventana corta (inicio de turno), MVP funcional con lo que ya se tiene | ESP32-S3-CAM + laptop + hotspot del celular |
| Fase 2 | Monitoreo continuo 24/7 en obras grandes, modo móvil de inspección, integración completa a AWS | + Raspberry Pi 5, router 4G dedicado |
| Fase 3 | Más clases de EPP, multi-cámara, identificación de trabajador (RFID/NFC), reportes avanzados | Según demanda |

---

## 9. Contexto de negocio (resumen)

- Nombre del proyecto: **ARGOS EPP**.
- Modelo de negocio B2B: venta de kit de hardware + suscripción de software (dashboard, app, soporte, actualizaciones del modelo de IA).
- Clientes objetivo: empresas constructoras, mineras y de manufactura en Perú, con obligación normativa de EPP (Ley N° 29783), fiscalizadas por SUNAFIL.
- Diferenciador principal: funcionamiento offline-first (ninguna alarma depende de internet) + modelo de IA entrenado con dataset peruano + dos modos de uso (fijo y móvil).
- Contexto académico: proyecto desarrollado como parte del curso "Diseño de Proyectos de Innovación" (V ciclo, TECSUP), con documentación entregable (Business Model Canvas, PESTEL, Porter, FODA, segmentación de mercado, marketing mix) ya elaborada.

---

## 10. Estado actual del desarrollo

**Ya construido:**
- Firmware completo del ESP32-S3-CAM (streaming MJPEG en `/stream`, captura en `/capture`, salud en `/health`, configuración en `/config`, reconexión WiFi automática, mDNS).
- Script de descubrimiento automático de la cámara en la red (`discover_camera.py`).

**Pendiente (próximos pasos técnicos):**
1. Motor de IA (YOLOv8 + OpenVINO leyendo el stream del ESP32, con reglas de EPP por zona y tracking para evitar alertas duplicadas).
2. Backend local (FastAPI + SQLite + patrón outbox de sincronización a AWS).
3. Dashboard web en tiempo real (React + WebSocket).
4. App móvil (Flutter).
5. Nodo actuador de alarma (ESP32 + relé + audio/sirena).
