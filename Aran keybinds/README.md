# DreamMS Aran Keybind Helper

A configurable Windows helper for Aran inputs. It only accepts action triggers
while `DreamMS.exe` is the active foreground window and can send commands
through a compatible Arduino HID device.

## Features

- Independent Combat, Attack, Combo, Combo Drain, and Fenrir functions.
- Configurable trigger keys, key holds, and sequence timing.
- Persistent Arduino connection with automatic board detection.
- Chat and character-name template checks that can block unsafe input.
- Master toggle and per-function enable switches.
- Persistent JSON settings and recent logs.

## Portable build

Download `Aran-Keybinds-Windows-x64.zip` from the repository's `Releases`
folder, extract the complete ZIP, and run `Aran-Keybinds.exe`. The destination
PC does not need Python or pip. Do not move only the EXE away from its
`_internal` folder.

Flash `arduino_teensy_alt/arduino_teensy_alt.ino` to the Arduino once. The same
extended firmware is compatible with the VoS helper.

## Running from source

```powershell
python -m pip install -r requirements.txt
python dreamms_bot.py
```

The defaults are stored in `config.json`; use the GUI's Save button for normal
changes. The image templates in `assets/` must remain beside the program.
