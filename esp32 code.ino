#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"

// ─────────────────────────────────────────────
//  Home WiFi credentials  (STA mode — no AP)
// ─────────────────────────────────────────────
const char* ssid     = "Me";
const char* password = "mehedi113";

WebServer server(80);
MAX30105 particleSensor;
bool max30105_ok = false;

// ================= ECG CONFIG =================
const int SENSOR_PIN = 34;
const int LO_PLUS    = 19;
const int LO_MINUS   = 23;

#define MAX_BUFFER 100
uint32_t prevData[MAX_BUFFER];
uint32_t sumData = 0, maxData = 0, avgData = 0;
uint32_t roundrobin = 0, countData = 0;
uint32_t newData;

float x[33], y_lp[33], y_hp[33];
const float SCALING_FACTOR = 0.05;

// ================= BPM CONFIG =================
#define IR_PRESENT     50000
#define RATE_SIZE      4
#define FINGER_TIMEOUT 300

byte rates[RATE_SIZE];
byte rateSpot      = 0;
unsigned long lastBeat = 0;
int  beatAvg       = 0;
int  fingerStatus  = 0;
unsigned long lastFingerTime = 0;
unsigned long lastBPMread    = 0;

// ================= Timing =================
unsigned long lastECGread = 0;

// ================= Latest readings =================
volatile float latestRaw      = 0;
volatile float latestFiltered = 0;
volatile int   latestBPM      = 0;
volatile int   latestFinger   = 0;

// ================= ECG Helper =================
void freqDetec() {
    roundrobin++;
    if (roundrobin >= MAX_BUFFER) roundrobin = 0;

    if (countData < MAX_BUFFER) {
        countData++;
        sumData += newData;
    } else {
        sumData += newData - prevData[roundrobin];
    }

    avgData = sumData / countData;
    if (newData > maxData) maxData = newData;
    prevData[roundrobin] = newData;
}

// ─────────────────────────────────────────────
//  Web Handlers
// ─────────────────────────────────────────────

// Root — nice dashboard page
void handleRoot() {
    String ip = WiFi.localIP().toString();
    String html = R"rawhtml(
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ECG Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #1a1a2e; color: #eaeaea; font-family: Arial, sans-serif; padding: 20px; }
    h1 { color: #00d9ff; text-align: center; margin-bottom: 6px; font-size: 22px; }
    .subtitle { text-align: center; color: #888; font-size: 13px; margin-bottom: 20px; }
    .bpm-box { background: #16213e; border-radius: 12px; padding: 20px; text-align: center; margin-bottom: 16px; }
    .bpm-val { font-size: 64px; font-weight: bold; color: #e94560; }
    .bpm-lbl { color: #aaa; font-size: 14px; }
    .status-row { display: flex; gap: 10px; margin-bottom: 16px; }
    .badge { flex: 1; background: #16213e; border-radius: 8px; padding: 12px; text-align: center; font-size: 13px; }
    canvas { width: 100%; background: #0d1b2a; border-radius: 8px; display: block; margin-bottom: 12px; }
    .lbl { color: #888; font-size: 12px; margin-bottom: 4px; padding-left: 4px; }
    .footer { text-align: center; color: #444; font-size: 11px; margin-top: 16px; }
  </style>
</head>
<body>
  <h1>📈 ECG Monitor</h1>
  <p class="subtitle">Live stream from ESP32 — same network</p>

  <div class="bpm-box">
    <div class="bpm-val" id="bpmVal">--</div>
    <div class="bpm-lbl">BPM</div>
  </div>

  <div class="status-row">
    <div class="badge" id="fingerBadge">👆 Finger: --</div>
    <div class="badge" id="connBadge" style="color:#f39c12;">● Connecting…</div>
  </div>

  <p class="lbl">Raw ECG</p>
  <canvas id="rawCanvas" height="120"></canvas>
  <p class="lbl">Filtered ECG</p>
  <canvas id="filtCanvas" height="120"></canvas>

  <div class="footer">ESP32 IP: )rawhtml";

    html += ip;
    html += R"rawhtml( &nbsp;|&nbsp; /stream &nbsp;|&nbsp; /data</div>

<script>
  const rawCanvas  = document.getElementById('rawCanvas');
  const filtCanvas = document.getElementById('filtCanvas');
  const rawCtx     = rawCanvas.getContext('2d');
  const filtCtx    = filtCanvas.getContext('2d');
  rawCanvas.width  = rawCanvas.offsetWidth;
  filtCanvas.width = filtCanvas.offsetWidth;

  const W = rawCanvas.width;
  const H = 120;
  const MAX_PTS = W;

  let rawPts = [], filtPts = [];

  function drawLine(ctx, pts, color) {
    if (pts.length < 2) return;
    const mn = Math.min(...pts), mx = Math.max(...pts);
    const rng = mx - mn || 1;
    ctx.clearRect(0, 0, W, H);
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    pts.forEach((v, i) => {
      const px = (i / (MAX_PTS - 1)) * W;
      const py = H - ((v - mn) / rng) * (H - 10) - 5;
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.stroke();
  }

  const evtSrc = new EventSource('/stream');
  evtSrc.onopen = () => {
    document.getElementById('connBadge').textContent = '● Connected';
    document.getElementById('connBadge').style.color = '#00ff88';
  };
  evtSrc.onerror = () => {
    document.getElementById('connBadge').textContent = '● Disconnected';
    document.getElementById('connBadge').style.color = '#e94560';
  };
  evtSrc.onmessage = (e) => {
    const parts = e.data.split(',');
    if (parts.length < 4) return;
    const raw    = parseFloat(parts[0]);
    const filt   = parseFloat(parts[1]);
    const bpm    = parseInt(parts[2]);
    const finger = parseInt(parts[3]);

    rawPts.push(raw);   if (rawPts.length  > MAX_PTS) rawPts.shift();
    filtPts.push(filt); if (filtPts.length > MAX_PTS) filtPts.shift();

    drawLine(rawCtx,  rawPts,  '#00d9ff');
    drawLine(filtCtx, filtPts, '#00ff88');

    document.getElementById('bpmVal').textContent = finger ? bpm : '--';
    document.getElementById('fingerBadge').textContent =
      finger ? '👆 Finger: Detected' : '👆 Finger: --';
    document.getElementById('fingerBadge').style.color =
      finger ? '#00ff88' : '#f39c12';
  };
</script>
</body>
</html>
)rawhtml";

    server.send(200, "text/html", html);
}

// /data — single poll
void handleData() {
    String data = String(latestRaw, 2) + "," +
                  String(latestFiltered, 4) + "," +
                  String(latestBPM) + "," +
                  String(latestFinger);
    server.send(200, "text/plain", data);
}

// /stream — SSE for Python app & browser dashboard
void handleStream() {
    WiFiClient client = server.client();
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: text/event-stream");
    client.println("Cache-Control: no-cache");
    client.println("Connection: keep-alive");
    client.println("Access-Control-Allow-Origin: *");
    client.println();

    while (client.connected()) {
        processSensors();
        String data = "data: " +
                      String(latestRaw, 2) + "," +
                      String(latestFiltered, 4) + "," +
                      String(latestBPM) + "," +
                      String(latestFinger) + "\n\n";
        client.print(data);
        delay(5);
    }
}

// /ip — returns IP as plain text (handy for Python auto-discovery)
void handleIP() {
    server.send(200, "text/plain", WiFi.localIP().toString());
}

// ================= SENSOR PROCESS =================
void processSensors() {
    unsigned long now = millis();

    static unsigned long lastDetect = 0;
    if (!max30105_ok && now - lastDetect > 5000) {
        lastDetect = now;
        max30105_ok = particleSensor.begin(Wire, I2C_SPEED_FAST);
        if (max30105_ok) {
            particleSensor.setup();
            particleSensor.setPulseAmplitudeRed(0x1F);
            particleSensor.setPulseAmplitudeGreen(0);
            Serial.println("MAX30105 detected dynamically!");
        }
    }

    // ECG @ 200 Hz
    if (now - lastECGread >= 5) {
        lastECGread = now;

        newData = analogRead(SENSOR_PIN);
        freqDetec();
        float raw = (float)newData;

        for (int i = 32; i > 0; i--) {
            x[i]    = x[i - 1];
            y_lp[i] = y_lp[i - 1];
            y_hp[i] = y_hp[i - 1];
        }
        x[0] = raw;

        y_lp[0] = 2 * y_lp[1] - y_lp[2] + x[0] - 2 * x[6] + x[12];
        y_hp[0] = y_hp[1] - y_lp[0] / 32.0 + y_lp[16] - y_lp[17] + y_lp[32] / 32.0;

        float der      = (1.0 / 8.0) * (2 * y_hp[0] + y_hp[1] - y_hp[3] - 2 * y_hp[4]);
        float filtered = der * der * SCALING_FACTOR;

        latestRaw      = raw;
        latestFiltered = filtered;
    }

    // BPM @ 50 Hz
    if (!max30105_ok) {
        latestBPM    = 0;
        latestFinger = 0;
        return;
    }

    if (now - lastBPMread >= 20) {
        lastBPMread = now;

        particleSensor.check();
        while (particleSensor.available()) {
            long irValue = particleSensor.getFIFOIR();
            particleSensor.nextSample();

            if (irValue > IR_PRESENT) {
                fingerStatus  = 1;
                lastFingerTime = now;

                if (checkForBeat(irValue)) {
                    if (lastBeat > 0) {
                        int bpmInstant = 60000 / (now - lastBeat);
                        if (bpmInstant > 20 && bpmInstant < 255) {
                            rates[rateSpot++] = bpmInstant;
                            rateSpot %= RATE_SIZE;
                            int sum = 0;
                            for (byte i = 0; i < RATE_SIZE; i++) sum += rates[i];
                            beatAvg = sum / RATE_SIZE;
                        }
                    }
                    lastBeat = now;
                }
            }
        }

        if (now - lastFingerTime > FINGER_TIMEOUT) {
            fingerStatus = 0;
            beatAvg      = 0;
            for (byte i = 0; i < RATE_SIZE; i++) rates[i] = 0;
        }

        latestBPM    = beatAvg;
        latestFinger = fingerStatus;
    }
}

// ================= SETUP =================
void setup() {
    Serial.begin(115200);
    pinMode(LO_PLUS,  INPUT);
    pinMode(LO_MINUS, INPUT);

    for (int i = 0; i < 33; i++) x[i] = y_lp[i] = y_hp[i] = 0;
    for (int i = 0; i < MAX_BUFFER; i++) prevData[i] = 0;
    for (byte i = 0; i < RATE_SIZE; i++) rates[i] = 0;

    Wire.begin();

    max30105_ok = particleSensor.begin(Wire, I2C_SPEED_FAST);
    if (max30105_ok) {
        particleSensor.setup();
        particleSensor.setPulseAmplitudeRed(0x1F);
        particleSensor.setPulseAmplitudeGreen(0);
        Serial.println("MAX30105 detected");
    } else {
        Serial.println("WARNING: MAX30105 NOT detected");
    }

    // ── Connect to home WiFi (STA mode) ──
    Serial.print("Connecting to WiFi: ");
    Serial.println(ssid);
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid, password);

    int tries = 0;
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
        tries++;
        if (tries > 40) {   // 20 sec timeout — fallback to AP
            Serial.println("\nWiFi failed! Falling back to AP mode.");
            WiFi.softAP("ECG_Monitor", "ecg12345");
            Serial.print("AP IP: ");
            Serial.println(WiFi.softAPIP());
            goto server_start;
        }
    }

    Serial.println();
    Serial.print("Connected! IP: ");
    Serial.println(WiFi.localIP());
    Serial.println("Open this IP in your browser or paste it in the Python app.");

server_start:
    server.on("/",       handleRoot);
    server.on("/data",   handleData);
    server.on("/stream", handleStream);
    server.on("/ip",     handleIP);
    server.begin();
    Serial.println("HTTP server started");
}

// ================= LOOP =================
void loop() {
    server.handleClient();
    processSensors();
}
