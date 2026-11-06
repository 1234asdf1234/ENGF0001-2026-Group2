#include "publish.h"
#include "connect.h"
#include "WiFi.h"
#include <SPI.h>


void setup() {
    Serial.begin(115200);
    wifi_connect();
    publish();
}

void loop() {
    while (WiFi.status() != WL_CONNECTED) {
        wifi_connect();
    }
    client.loop(); 
}
