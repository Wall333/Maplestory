# DreamMS VoS Helper

A separate Windows helper that repeatedly sends a selected key while
`DreamMS.exe` is the foreground window. Available spam keys are `End`,
`Page Down`, `X`, `C`, `V`, `B`, `D`, `F`, and `Y`.

## Current packaged defaults

- Spam `F` every 2 seconds.
- Manual green anchor at the horizontal center (`0px`).
- Auto-align enabled with 200px tolerance, 0.5s hold, and 0.5s interval.
- Crystal display/looting, Yeti-required spam, and Thorns maintenance enabled.
- Map threshold `0.60`; alignment threshold `0.70`; Yeti and Thorns thresholds `0.50`.
- Spam badge enabled at horizontal `85%`, vertical `4%`.

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

The normal green target can use either the center of `area.png` or a manual
horizontal anchor. Manual offset `0` is the center of the DreamMS client;
negative pixel offsets move the target left and positive offsets move it right.
The visual guide and auto-alignment use the same selected target.

Auto alignment can optionally use those two center lines while VoS spam is
running. Inside the configured pixel tolerance it does nothing; outside the
tolerance it taps `LEFT` or `RIGHT` through the selected Arduino/Windows input
path. Movement key hold and correction interval should be tuned conservatively
to avoid overshooting the target.

The in-game overlay can show a large live `SPAMMER ON/OFF` badge. Enabling
**Show crystal** detects `assets/crystal.png` and draws a red vertical guide
while the crystal remains visible. With **Loot crystal** enabled,
auto-alignment prioritizes that red line and holds within tolerance until the
crystal disappears; the guide is then cleared and alignment returns to the
normal area target.

The spam status overlay can be shown or hidden independently. Horizontal and
vertical position sliders move it live using percentages of the DreamMS client,
and the selected position is saved automatically.

The optional **Yeti required** gate checks for `assets/yeti.png`. With the gate
enabled, only skill spam pauses while the Yeti is absent, then resumes
automatically when it is detected. Auto-alignment continues moving during this
wait. The GUI and in-game badge distinguish the waiting state from ON/OFF.

Optional Thorns maintenance checks `assets/thorns.png` with its own detection
rate and threshold. If the icon is missing during an active spam session, spam
and movement pause, the configured buff key is sent, and the helper waits the
configured recovery period (five seconds by default). It resumes only after
the wait has elapsed and Thorns is detected; otherwise it retries. A small
yellow rectangle marks the matched buff icon in the in-game overlay.

## Arduino compatibility

The updated sketch is in `arduino_teensy_vos/arduino_teensy_vos.ino`. It keeps
all commands used by the Aran helper and adds the complete VoS key list. Because
those commands do not all exist in the old flashed firmware, the board must be
flashed once to add them; it does not need to be reflashed when switching
between apps.
