#include "publish.h"
#include "connect.h"
#include "WiFi.h"
#include <SPI.h>


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
