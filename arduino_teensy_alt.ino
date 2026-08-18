// Minimal Arduino USB keyboard controller for MapleStory combat.
// Receives: JUMP/COMBAT {delay_ms} or ATTACK {delay_ms}
// Parses the delay and applies it to the key press.
#include <Arduino.h>
#include <Keyboard.h>

void pressKey(uint8_t key, int delayMs) {
  Keyboard.press(key);
  if (delayMs > 0) {
    delay(delayMs);
  }
  Keyboard.release(key);
}

void setup() {
  Serial.begin(9600);
  Keyboard.begin();
}

void loop() {
  if (Serial.available()) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    int spaceIndex = command.indexOf(' ');
    String cmd = (spaceIndex > 0) ? command.substring(0, spaceIndex) : command;
    int delayMs = 10;

    if (spaceIndex > 0) {
      String delayStr = command.substring(spaceIndex + 1);
      delayMs = delayStr.toInt();
    }

    if (cmd == "F") {
      pressKey('f', delayMs);
    }

    if (cmd == "ALT" || cmd == "JUMP" || cmd == "COMBAT") {
      pressKey(KEY_LEFT_ALT, delayMs);
    }

    if (cmd == "CTRL") {
      pressKey(KEY_LEFT_CTRL, delayMs);
    }
    
    //Arrow keys Here
    if (cmd == "LEFT") {
      pressKey(KEY_LEFT_ARROW, delayMs);
    }

    if (cmd == "RIGHT") {
      pressKey(KEY_RIGHT_ARROW, delayMs);
    }

    if (cmd == "UP") {
      pressKey(KEY_UP_ARROW, delayMs);
    }

    if (cmd == "DOWN") {
      pressKey(KEY_DOWN_ARROW, delayMs);
    }
  }
}
