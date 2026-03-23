#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <Wire.h>
#include <Adafruit_INA219.h>
#include <WiFiManager.h>

namespace {
const char *kFirmwareVersion = "1.0.0";
const char *kPrefsNamespace = "batmon";
const char *kDeviceIdKey = "device_id";
const char *kDeviceNameKey = "device_name";
const char *kBootCountKey = "boot_count";

const uint16_t kServerUdpPort = 4210;
const uint16_t kServerHttpPort = 8080;
const uint64_t kSleepSeconds = 300;
const uint32_t kDiscoveryTimeoutMs = 2500;
const uint32_t kProvisionPortalTimeoutSeconds = 180;
const uint32_t kHttpTimeoutMs = 7000;
const uint8_t kMaxDiscoveryAttempts = 3;
const uint8_t kI2cSdaPin = 21;
const uint8_t kI2cSclPin = 22;

Preferences prefs;
Adafruit_INA219 ina219;
RTC_DATA_ATTR uint32_t rtcBootCounter = 0;

String deviceId;
String deviceName;
uint32_t bootCount = 0;

String makeDefaultDeviceName(const String &id) {
  String suffix = id;
  suffix.replace("esp32-bat-", "");
  return "Battery-" + suffix;
}

String makeDeviceId() {
  uint64_t efuseMac = ESP.getEfuseMac();
  char buffer[20];
  snprintf(buffer, sizeof(buffer), "esp32-bat-%04X%08X",
           static_cast<uint16_t>(efuseMac >> 32),
           static_cast<uint32_t>(efuseMac));
  return String(buffer);
}

void loadConfig() {
  prefs.begin(kPrefsNamespace, false);
  deviceId = prefs.getString(kDeviceIdKey, "");
  if (deviceId.isEmpty()) {
    deviceId = makeDeviceId();
    prefs.putString(kDeviceIdKey, deviceId);
  }

  deviceName = prefs.getString(kDeviceNameKey, "");
  if (deviceName.isEmpty()) {
    deviceName = makeDefaultDeviceName(deviceId);
    prefs.putString(kDeviceNameKey, deviceName);
  }

  bootCount = prefs.getUInt(kBootCountKey, 0) + 1;
  prefs.putUInt(kBootCountKey, bootCount);
}

void saveDeviceName(const String &newName) {
  if (newName.isEmpty() || newName == deviceName) {
    return;
  }

  deviceName = newName;
  prefs.putString(kDeviceNameKey, deviceName);
}

bool ensureWifi() {
  WiFi.mode(WIFI_STA);
  WiFiManager wifiManager;
  wifiManager.setConfigPortalBlocking(true);
  wifiManager.setConfigPortalTimeout(kProvisionPortalTimeoutSeconds);
  wifiManager.setHostname(deviceId.c_str());

  bool connected = wifiManager.autoConnect("BatteryMonitorSetup");
  if (!connected) {
    Serial.println("WiFi provisioning failed or timed out");
    return false;
  }

  Serial.print("Connected to WiFi with IP: ");
  Serial.println(WiFi.localIP());
  return true;
}

bool initIna219() {
  Wire.begin(kI2cSdaPin, kI2cSclPin);
  if (!ina219.begin()) {
    Serial.println("INA219 not found on I2C bus");
    return false;
  }

  ina219.setCalibration_32V_2A();
  return true;
}

float readBatteryVoltage() {
  float busVoltage = ina219.getBusVoltage_V();
  float shuntVoltage = ina219.getShuntVoltage_mV() / 1000.0f;
  return busVoltage + shuntVoltage;
}

bool discoverServer(IPAddress &serverIp, uint16_t &serverPort) {
  WiFiUDP udp;
  if (!udp.begin(kServerUdpPort)) {
    Serial.println("UDP begin failed");
    return false;
  }

  StaticJsonDocument<192> request;
  request["type"] = "discover";
  request["device_id"] = deviceId;
  request["device_name"] = deviceName;

  char payload[192];
  size_t payloadLength = serializeJson(request, payload, sizeof(payload));

  for (uint8_t attempt = 0; attempt < kMaxDiscoveryAttempts; ++attempt) {
    udp.beginPacket(IPAddress(255, 255, 255, 255), kServerUdpPort);
    udp.write(reinterpret_cast<const uint8_t *>(payload), payloadLength);
    udp.endPacket();

    unsigned long startedAt = millis();
    while (millis() - startedAt < kDiscoveryTimeoutMs) {
      int packetSize = udp.parsePacket();
      if (packetSize <= 0) {
        delay(50);
        continue;
      }

      char responseBuffer[256];
      int read = udp.read(responseBuffer, sizeof(responseBuffer) - 1);
      if (read <= 0) {
        continue;
      }
      responseBuffer[read] = '\0';

      StaticJsonDocument<256> response;
      DeserializationError error = deserializeJson(response, responseBuffer);
      if (error) {
        continue;
      }

      const char *responseType = response["type"] | "";
      if (String(responseType) != "server_announce") {
        continue;
      }

      serverIp = udp.remoteIP();
      serverPort = response["http_port"] | kServerHttpPort;
      udp.stop();
      return true;
    }
  }
  udp.stop();
  return false;
}

bool postMeasurement(const IPAddress &serverIp, uint16_t serverPort, float voltage) {
  HTTPClient http;
  String url = "http://" + serverIp.toString() + ":" + String(serverPort) + "/api/devices/report";

  StaticJsonDocument<384> payload;
  payload["device_id"] = deviceId;
  payload["device_name"] = deviceName;
  payload["voltage"] = voltage;
  payload["wifi_rssi"] = WiFi.RSSI();
  payload["firmware_version"] = kFirmwareVersion;
  payload["boot_count"] = bootCount;
  payload["uptime_ms"] = millis();
  payload["ip_address"] = WiFi.localIP().toString();

  String body;
  serializeJson(payload, body);

  http.setTimeout(kHttpTimeoutMs);
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  int statusCode = http.POST(body);
  if (statusCode <= 0) {
    Serial.printf("HTTP POST failed: %s\n", http.errorToString(statusCode).c_str());
    http.end();
    return false;
  }

  String response = http.getString();
  http.end();

  if (statusCode < 200 || statusCode >= 300) {
    Serial.printf("Server returned HTTP %d\n", statusCode);
    return false;
  }

  StaticJsonDocument<256> responseJson;
  if (deserializeJson(responseJson, response) == DeserializationError::Ok) {
    const char *assignedName = responseJson["assigned_name"] | "";
    if (strlen(assignedName) > 0) {
      saveDeviceName(String(assignedName));
    }
  }

  return true;
}

void goToSleep() {
  Serial.printf("Entering deep sleep for %llu seconds\n", kSleepSeconds);
  prefs.end();
  esp_sleep_enable_timer_wakeup(kSleepSeconds * 1000000ULL);
  esp_deep_sleep_start();
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(500);
  ++rtcBootCounter;
  Serial.printf("Wake cycle #%lu\n", static_cast<unsigned long>(rtcBootCounter));

  loadConfig();

  if (!initIna219()) {
    goToSleep();
  }

  float voltage = readBatteryVoltage();
  Serial.printf("Measured voltage: %.3f V\n", voltage);

  if (!ensureWifi()) {
    goToSleep();
  }

  IPAddress serverIp;
  uint16_t serverPort = kServerHttpPort;
  if (!discoverServer(serverIp, serverPort)) {
    Serial.println("Server discovery failed");
    goToSleep();
  }

  Serial.printf("Discovered server: %s:%u\n", serverIp.toString().c_str(), serverPort);
  postMeasurement(serverIp, serverPort, voltage);
  goToSleep();
}

void loop() {
  // Not used because the device sleeps after each measurement cycle.
}
