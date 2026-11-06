# --- Configuration ---
# Change FQBN based on your board (e.g., esp32:esp32:esp32wrover, esp32:esp32:esp32c3)
BOARD ?= esp32:esp32:esp32

# Automatically find the first available Silicon Labs or generic USB/UART port on macOS
PORT ?= $(shell ls /dev/cu.usbserial-* /dev/cu.SLAB_USBtoUART /dev/cu.usbserial* 2>/dev/null | head -n 1)

BUILD_DIR ?= build
SOURCES = mqtt_connection.ino publish.h publish.ino connect.h connect.ino 

.PHONY: all compile upload clean monitor

all: compile

compile: $(BUILD_DIR)/.compiled

$(BUILD_DIR)/.compiled: $(SOURCES)
	@mkdir -p $(BUILD_DIR) $(CACHE_DIR)
	arduino-cli compile \
        --fqbn $(BOARD) \
        --build-path $(BUILD_DIR)
	@touch $@


upload: compile
	@if [ -z "$(PORT)" ]; then \
		echo "Error: ESP32 serial port not found. Check your USB connection."; \
		exit 1; \
	fi
	arduino-cli upload -p $(PORT) --fqbn $(BOARD) --input-dir $(BUILD_DIR)

monitor:
	@if [ -z "$(PORT)" ]; then \
		echo "Error: ESP32 serial port not found."; \
		exit 1; \
	fi
	arduino-cli monitor -p $(PORT) -c baudrate=115200

clean:
	rm -rf $(BUILD_DIR)
