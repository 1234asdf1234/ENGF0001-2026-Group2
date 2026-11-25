#include <Arduino.h>
#line 1 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/mqtt_connection.ino"
#include "publish.h"
#include "connect.h"
#include "WiFi.h"
#include <SPI.h>


#line 7 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/mqtt_connection.ino"
void setup();
#line 15 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/mqtt_connection.ino"
void loop();
#line 27 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/connect.ino"
void print_wifi_info();
#line 50 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/connect.ino"
void wifi_connect(float timeout);
#line 17 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/publish.ino"
void publish();
#line 45 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/publish.ino"
void callback(char *topic, byte *payload, unsigned int length);
#line 7 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/mqtt_connection.ino"
void setup() {
    Serial.begin(115200);
    while (WiFi.status() != WL_CONNECTED) {
        wifi_connect();
    }
    publish();
}

void loop() {
    client.loop(); 
}

#line 1 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/connect.ino"
// for devices not capable of opening browsers,
// glide network can give them access by entering the MAC address,
// directly on their website, glide.co.uk

// eduroam config starts
// configure your connection here
#define USE_EDUROAM 1
#define WIFI_USER "zcabzy2@ucl.ac.uk"
#define WIFI_SSID "Glide-5"
#define WIFI_PASS "Beriaru_Beiliya"
// end of configuration

#include "connect.h"
#include "WiFi.h"

#if USE_EDUROAM
#include "esp_eap_client.h"
const char* ssid = "eduroam";
const char* user = WIFI_USER;
const char* pass = WIFI_PASS;
#else
const char* ssid = WIFI_SSID;
const char* pass = "";  // glide is open network, thus no password required
#endif

// dump some details of the WiFi connection (if any) to the serial line
void print_wifi_info ()
{
  if (WiFi.status() == WL_CONNECTED)
  {
    Serial.print("Connected to WiFi network: ");
    Serial.println(WiFi.SSID());
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());
    Serial.print("Gateway address: ");
    Serial.println(WiFi.gatewayIP());
    Serial.print("Subnet mask: ");
    Serial.println(WiFi.subnetMask());
    Serial.print("MAC address: ");
    Serial.println(WiFi.macAddress());
  }
  else
  {
    Serial.println("WiFi not connected");
    Serial.print("MAC address: ");
    Serial.println(WiFi.macAddress());
  }
}

void wifi_connect (float timeout)
{
  unsigned long deadline;

  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);

#if USE_EDUROAM
  Serial.printf("Connecting to eduroam as user %s\n", user);
  esp_eap_client_set_identity((uint8_t *)user, strlen(user));
  esp_eap_client_set_username((uint8_t *)user, strlen(user));
  esp_eap_client_set_password((uint8_t *)pass, strlen(pass));
  esp_wifi_sta_enterprise_enable();
  WiFi.begin(ssid);
#else
  Serial.printf("Connecting to %s\n", ssid);
  WiFi.begin(ssid, pass);
#endif
  
  deadline = millis() + (unsigned long)(timeout * 1000);

  while ((WiFi.status() != WL_CONNECTED) && (millis() < deadline))
  {
    delay(500);
    Serial.print(".");
  }

  Serial.println("");
  print_wifi_info();
}
// eduroam config ends
#line 1 "/Users/beriaru/Documents/ENGF0001-2026-Group2/mqtt_connection/publish.ino"
#include "publish.h"
#include "connect.h"
#include <WiFi.h>
#include <PubSubClient.h>


// MQTT Broker
const char *mqtt_broker = "broker.emqx.io";
const char *topic = "emqx/esp32";
const char *mqtt_username = "emqx";
const char *mqtt_password = "public";
const int mqtt_port = 1883;

WiFiClient espClient;
PubSubClient client(espClient);

void publish() {
    // Suppose Serial has already been opened
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Not publishing due to no network");
        // terminate immediately
        return;
    }

    //connecting to a mqtt broker
    client.setServer(mqtt_broker, mqtt_port);
    client.setCallback(callback);
    while (!client.connected()) {
        String client_id = "esp32-client-";
        client_id += String(WiFi.macAddress());
        Serial.printf("The client %s connects to the public MQTT broker\n", client_id.c_str());
        if (client.connect(client_id.c_str(), mqtt_username, mqtt_password)) {
            Serial.println("Public EMQX MQTT broker connected");
        } else {
            Serial.print("failed with state ");
            Serial.print(client.state());
            delay(2000); 
        }
    }
    // Publish and subscribe
    client.publish(topic, "Hi, I'm ESP32 ^^");
    client.subscribe(topic);
}

void callback(char *topic, byte *payload, unsigned int length) {
    Serial.print("Message arrived in topic: ");
    Serial.println(topic);
    Serial.print("Message:");
    for (int i = 0; i < length; i++) {
        Serial.print((char) payload[i]);
    }
    Serial.println();
    Serial.println("-----------------------");
}



