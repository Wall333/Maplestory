# DreamMS VoS Helper

A separate Windows helper that repeatedly sends a selected key while
`DreamMS.exe` is the foreground window. Available spam keys are `End`,
`Page Down`, `X`, `C`, `V`, `B`, `D`, `F`, and `Y`.

## Setup

1. Install Python dependencies: `pip install -r requirements.txt`
2. Flash the updated Arduino sketch once (see below).
3. Run `run_vos_bot.bat`.

Defaults: `F11` toggles the whole helper, and `F10` starts/stops VoS spam.
The output key, hold time, repeat interval, COM port, and hotkeys are editable.

The spam loop stops automatically if DreamMS loses focus. Turning off Arduino
mode uses Windows `SendInput` as a testing fallback.

The optional VoS Map Checker searches the active DreamMS client for
`assets/VOS_map.png`. It blocks startup and immediately stops active spam when
the image is no longer detected. Detection rate and match threshold are
configurable in the GUI.

The optional Character / Area Alignment Test detects `assets/lightbulb.png`
and `assets/area.png`. Its transparent, click-through overlay draws a cyan line
downward from the character marker and a green vertical target line through the
middle of the rock-area template. The status row shows both live match scores.

Auto alignment can optionally use those two center lines while VoS spam is
running. Inside the configured pixel tolerance it does nothing; outside the
tolerance it taps `LEFT` or `RIGHT` through the selected Arduino/Windows input
path. Movement key hold and correction interval should be tuned conservatively
to avoid overshooting the target.

## Arduino compatibility

The updated sketch is in `arduino_teensy_vos/arduino_teensy_vos.ino`. It keeps
all commands used by the Aran helper and adds the complete VoS key list. Because
those commands do not all exist in the old flashed firmware, the board must be
flashed once to add them; it does not need to be reflashed when switching
between apps.
