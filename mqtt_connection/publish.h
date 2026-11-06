#ifndef PUBLISH_H
#define PUBLISH_H

#include <PubSubClient.h>

extern PubSubClient client; // execute client.loop() in loop()

void publish();
void callback(char *topic, byte *payload, unsigned int length);

#endif
