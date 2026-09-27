#include <Arduino.h>
#include <Keyboard.h>
#include <Mouse.h>

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
  Mouse.begin();
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

    // Existing commands
    if (cmd == "CLICK") Mouse.click(MOUSE_LEFT);
    if (cmd == "F") {
      pressKey('f', delayMs);
    }

    if (cmd == "ALT" || cmd == "JUMP" || cmd == "COMBAT") {
      pressKey(KEY_LEFT_ALT, delayMs);
    }

    if (cmd == "CTRL") {
      pressKey(KEY_LEFT_CTRL, delayMs);
    }

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

    // VoS helper commands. These additions are backward-compatible with all
    // existing Aran commands, allowing both apps to share one firmware.
    if (cmd == "END") {
      pressKey(KEY_END, delayMs);
    }

    if (cmd == "PGDN" || cmd == "PAGEDOWN") {
      pressKey(KEY_PAGE_DOWN, delayMs);
    }

    if (cmd == "X") {
      pressKey('x', delayMs);
    }

    if (cmd == "C") {
      pressKey('c', delayMs);
    }

    if (cmd == "V") {
      pressKey('v', delayMs);
    }

    if (cmd == "B") {
      pressKey('b', delayMs);
    }

    if (cmd == "D") {
      pressKey('d', delayMs);
    }

    if (cmd == "Y") {
      pressKey('y', delayMs);
    }

    // Combo overlap commands.
    // These do not delay or auto-release, so CTRL commands can continue
    // while DOWN remains held.
    if (cmd == "DOWN_DOWN") {
      Keyboard.press(KEY_DOWN_ARROW);
    }

    if (cmd == "DOWN_UP") {
      Keyboard.release(KEY_DOWN_ARROW);
    }

    // Fenrir direction-resume commands.
    // These intentionally do NOT auto-release.
    if (cmd == "LEFT_DOWN") {
      Keyboard.press(KEY_LEFT_ARROW);
    }

    if (cmd == "LEFT_UP") {
      Keyboard.release(KEY_LEFT_ARROW);
    }

    if (cmd == "RIGHT_DOWN") {
      Keyboard.press(KEY_RIGHT_ARROW);
    }

    if (cmd == "RIGHT_UP") {
      Keyboard.release(KEY_RIGHT_ARROW);
    }
  }
}
