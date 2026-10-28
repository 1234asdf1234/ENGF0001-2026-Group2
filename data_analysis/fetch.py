# https://medium.com/@fatmabeyzaaydogan/from-mqtt-to-database-storing-iot-data-with-python-e3849d132a53

import paho.mqtt.client as mqtt

def on_connect(client, userdata, flags, rc):
    print("Connected with result code", rc)
    client.subscribe("bioreactor_sim/nofaults/telemetry/summary")

def on_message(client, userdata, msg):
    print(msg.payload.decode())

client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message

client.connect("engf0001.cs.ucl.ac.uk", 1883, 60)
client.loop_forever()
