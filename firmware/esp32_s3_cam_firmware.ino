/*
 * ============================================================
 * Sistema Inteligente de Detección de EPP - Perú
 * Firmware ESP32-S3-CAM (OV3660 / N16R8 - 16MB Flash / 8MB PSRAM)
 * ============================================================
 *
 * Funciones:
 *   - Servidor HTTP de streaming MJPEG en /stream
 *   - Captura de foto única en /capture (JPEG)
 *   - Endpoint de salud en /health (JSON)
 *   - Endpoint de configuración remota en /config (calidad, resolución)
 *   - Reconexión automática de WiFi (no se cuelga si se cae la red)
 *   - LED indicador de estado (parpadeo = buscando red, fijo = conectado)
 *
 * Placa: ESP32-S3-CAM (N16R8) - Sensor OV3660
 * IDE: Arduino IDE 2.x
 * Board Manager: "ESP32S3 Dev Module" (esp32 by Espressif >= 2.0.14)
 *
 * Configuración de placa en Arduino IDE:
 *   - USB CDC On Boot: Enabled
 *   - Flash Size: 16MB
 *   - Partition Scheme: 16M Flash (3MB APP/9.9MB FATFS) o "Huge APP"
 *   - PSRAM: OPI PSRAM  <-- IMPORTANTE, si no lo activas la cámara no arranca
 *
 * Librerías necesarias (todas vienen con el core esp32 de Espressif):
 *   - esp_camera.h
 *   - WiFi.h
 *   - esp_http_server.h
 *   - ESPmDNS.h
 *
 * ============================================================
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <esp_http_server.h>
#include <esp_timer.h>
#include <ESPmDNS.h>

// ============================================================
// CONFIGURACIÓN DE USUARIO - EDITA ESTOS VALORES
// ============================================================
const char* WIFI_SSID     = "TU_RED_WIFI";
const char* WIFI_PASSWORD = "TU_PASSWORD_WIFI";

// Identificador único de esta cámara (usalo si tienes varias)
const char* CAMERA_ID   = "cam-01";
const char* ZONE_NAME   = "zona-ingreso";

// Nombre mDNS: la camara sera accesible siempre en http://<esto>.local
// sin importar que IP le asigne el hotspot cada vez. NO uses espacios ni tildes.
// Si tienes mas de una camara, cambia esto por camara (ej. "epp-cam02")
const char* MDNS_HOSTNAME = "epp-cam01";

// Nivel de calidad JPEG inicial (0-63, menor = mejor calidad, más datos)
// Recomendado para WiFi de ESP32: 10-14
static int jpeg_quality = 12;

// Resolución inicial. Para ESP32-S3 con 8MB PSRAM puedes usar hasta SVGA/XGA
// FRAMESIZE_QVGA (320x240) - más FPS, menos detalle
// FRAMESIZE_VGA  (640x480) - buen balance, recomendado para YOLO
// FRAMESIZE_SVGA (800x600) - más detalle, más carga de WiFi
static framesize_t frame_size = FRAMESIZE_VGA;

// ============================================================
// PINES DE CÁMARA - ESP32-S3-CAM N16R8 (OV3660) - PINOUT ESTÁNDAR
// Si tu placa exacta trae otra serigrafía, ajusta según su datasheet
// ============================================================
#define PWDN_GPIO_NUM     -1
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM     15
#define SIOD_GPIO_NUM     4
#define SIOC_GPIO_NUM     5

#define Y9_GPIO_NUM       16
#define Y8_GPIO_NUM       17
#define Y7_GPIO_NUM       18
#define Y6_GPIO_NUM       12
#define Y5_GPIO_NUM       10
#define Y4_GPIO_NUM       8
#define Y3_GPIO_NUM       9
#define Y2_GPIO_NUM       11
#define VSYNC_GPIO_NUM    6
#define HREF_GPIO_NUM     7
#define PCLK_GPIO_NUM     13

// LED indicador de estado (flash LED integrado en muchas ESP32-S3-CAM)
#define STATUS_LED_PIN    2
#define FLASH_LED_PIN     48   // LED de flash blanco, si tu placa lo trae

// ============================================================
// VARIABLES DE ESTADO
// ============================================================
httpd_handle_t stream_httpd = NULL;
httpd_handle_t control_httpd = NULL;
unsigned long lastFrameTime = 0;
unsigned long frameCount = 0;
unsigned long bootTime = 0;

// ============================================================
// INICIALIZACIÓN DE CÁMARA
// ============================================================
bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // Con PSRAM disponible (8MB), pedimos más buffers para fluidez
  if (psramFound()) {
    config.frame_size   = frame_size;
    config.jpeg_quality  = jpeg_quality;
    config.fb_count      = 2;
    config.fb_location   = CAMERA_FB_IN_PSRAM;
    config.grab_mode     = CAMERA_GRAB_LATEST;
    Serial.println("[CAM] PSRAM detectada - usando doble buffer");
  } else {
    config.frame_size   = FRAMESIZE_CIF;
    config.jpeg_quality  = 15;
    config.fb_count      = 1;
    config.fb_location   = CAMERA_FB_IN_DRAM;
    Serial.println("[CAM] ADVERTENCIA: sin PSRAM, modo reducido");
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[CAM] Error al iniciar camara: 0x%x\n", err);
    return false;
  }

  // Ajustes finos del sensor (funciona para OV3660/OV5640/OV2640)
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_brightness(s, 0);
    s->set_contrast(s, 0);
    s->set_saturation(s, 0);
    s->set_gainceiling(s, (gainceiling_t)0);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    // Si tu instalación queda invertida, cambia hmirror/vflip a 1
  }

  return true;
}

// ============================================================
// CONEXIÓN WIFI CON RECONEXIÓN AUTOMÁTICA
// ============================================================
void connectWiFi() {
  Serial.printf("[WIFI] Conectando a %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Importante: sin esto el streaming se traba por ahorro de energia
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    digitalWrite(STATUS_LED_PIN, !digitalRead(STATUS_LED_PIN)); // parpadeo mientras busca
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(STATUS_LED_PIN, HIGH); // LED fijo = conectado
    Serial.println("");
    Serial.printf("[WIFI] Conectado. IP: %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("[WIFI] Stream disponible en: http://%s/stream\n", WiFi.localIP().toString().c_str());

    // mDNS: asi la camara siempre se encuentra por nombre, sin importar
    // que IP le de el hotspot cada vez que se prende
    if (MDNS.begin(MDNS_HOSTNAME)) {
      MDNS.addService("http", "tcp", 80);
      MDNS.addService("http", "tcp", 81);
      Serial.printf("[MDNS] Activo. Accesible en: http://%s.local\n", MDNS_HOSTNAME);
    } else {
      Serial.println("[MDNS] No se pudo iniciar (no critico, sigue funcionando por IP)");
    }
  } else {
    Serial.println("");
    Serial.println("[WIFI] No se pudo conectar, reintentando en el loop principal...");
  }
}

void checkWiFiConnection() {
  static unsigned long lastCheck = 0;
  if (millis() - lastCheck > 10000) { // revisa cada 10s
    lastCheck = millis();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("[WIFI] Conexion perdida, reconectando...");
      digitalWrite(STATUS_LED_PIN, LOW);
      WiFi.disconnect();
      WiFi.reconnect();
    }
  }
}

// ============================================================
// HANDLER: /stream  (MJPEG multipart - lo que consume Python/OpenCV)
// ============================================================
#define PART_BOUNDARY "123456789000000000000987654321"
static const char* STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char* STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  esp_err_t res = ESP_OK;
  char part_buf[128];

  res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("[STREAM] Error de captura de frame");
      res = ESP_FAIL;
    } else {
      if (fb->format != PIXFORMAT_JPEG) {
        Serial.println("[STREAM] Formato no es JPEG, se descarta frame");
        esp_camera_fb_return(fb);
        res = ESP_FAIL;
      } else {
        size_t hlen = snprintf(part_buf, 128, STREAM_PART, fb->len);
        res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
        if (res == ESP_OK) res = httpd_resp_send_chunk(req, part_buf, hlen);
        if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
        esp_camera_fb_return(fb);
        frameCount++;
      }
    }
    if (res != ESP_OK) break;
    // pequeño delay evita saturar el WiFi del ESP32-S3
    vTaskDelay(1 / portTICK_PERIOD_MS);
  }
  return res;
}

// ============================================================
// HANDLER: /capture  (una sola foto, la usa el backend para snapshots)
// ============================================================
esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

// ============================================================
// HANDLER: /health  (para monitoreo del backend / uptime)
// ============================================================
esp_err_t health_handler(httpd_req_t *req) {
  char json[256];
  unsigned long uptime_s = (millis() - bootTime) / 1000;
  snprintf(json, sizeof(json),
    "{\"camera_id\":\"%s\",\"zone\":\"%s\",\"status\":\"ok\",\"rssi\":%d,"
    "\"free_heap\":%u,\"uptime_s\":%lu,\"frames_sent\":%lu}",
    CAMERA_ID, ZONE_NAME, WiFi.RSSI(), ESP.getFreeHeap(), uptime_s, frameCount);

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, strlen(json));
}

// ============================================================
// HANDLER: /config  (ajustar calidad/resolucion en caliente via query params)
// Ejemplo: http://IP/config?quality=10&framesize=8
// ============================================================
esp_err_t config_handler(httpd_req_t *req) {
  char query[100];
  char param[32];
  sensor_t *s = esp_camera_sensor_get();

  if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK) {
    if (httpd_query_key_value(query, "quality", param, sizeof(param)) == ESP_OK) {
      int q = atoi(param);
      if (q >= 4 && q <= 63) s->set_quality(s, q);
    }
    if (httpd_query_key_value(query, "framesize", param, sizeof(param)) == ESP_OK) {
      int fs = atoi(param);
      if (fs >= 0 && fs <= 13) s->set_framesize(s, (framesize_t)fs);
    }
  }

  const char *resp = "{\"status\":\"config_updated\"}";
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, resp, strlen(resp));
}

// ============================================================
// ARRANQUE DE SERVIDORES HTTP (uno para stream, otro para control)
// Se separan en dos puertos porque el streaming bloquea su propio socket
// ============================================================
void startCameraServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.ctrl_port = 80;

  httpd_uri_t capture_uri = { .uri = "/capture", .method = HTTP_GET, .handler = capture_handler, .user_ctx = NULL };
  httpd_uri_t health_uri  = { .uri = "/health",  .method = HTTP_GET, .handler = health_handler,  .user_ctx = NULL };
  httpd_uri_t config_uri  = { .uri = "/config",  .method = HTTP_GET, .handler = config_handler,  .user_ctx = NULL };

  if (httpd_start(&control_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(control_httpd, &capture_uri);
    httpd_register_uri_handler(control_httpd, &health_uri);
    httpd_register_uri_handler(control_httpd, &config_uri);
  }

  config.server_port = 81;
  config.ctrl_port = 81;
  httpd_uri_t stream_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler, .user_ctx = NULL };

  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
  }
}

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  delay(1000);

  pinMode(STATUS_LED_PIN, OUTPUT);
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

  Serial.println("\n=================================================");
  Serial.println(" Sistema de Deteccion de EPP - Firmware ESP32-S3-CAM");
  Serial.printf(" Camera ID: %s | Zona: %s\n", CAMERA_ID, ZONE_NAME);
  Serial.println("=================================================\n");

  if (!initCamera()) {
    Serial.println("[FATAL] No se pudo iniciar la camara. Reiniciando en 5s...");
    delay(5000);
    ESP.restart();
  }

  connectWiFi();
  startCameraServer();

  bootTime = millis();
  Serial.println("[OK] Sistema listo.");
  Serial.printf("     Stream:  http://%s:81/stream\n", WiFi.localIP().toString().c_str());
  Serial.printf("     Captura: http://%s/capture\n", WiFi.localIP().toString().c_str());
  Serial.printf("     Salud:   http://%s/health\n", WiFi.localIP().toString().c_str());
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  checkWiFiConnection();
  delay(100);
}
