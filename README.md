# DreamMS Helper

Small Windows utility to send a simple "combat step" key sequence to DreamMS.exe only when the game window is active.

Install:

```powershell
python -m pip install -r requirements.txt
```

Run:

```powershell
python "dreamms_bot.py"
```

Usage:
- F12 toggles the bot on/off (default). The bot only triggers when the foreground window is `DreamMS.exe`.
- Use the GUI to change the combat key (default `v`) and save settings (persisted to `config.json`).
- The "Trigger CombatStep" button in the GUI performs the action manually.

Notes:
- This tool uses low-level SendInput calls; some games may block synthetic input. If it doesn't work, run with administrator privileges or try other input methods.
 - This tool uses low-level SendInput calls; some games may block synthetic input. The script will try `pydirectinput` (DirectInput) if installed, which often works with games. If it still doesn't work, run with administrator privileges or try sending window messages.
 - This tool uses low-level SendInput calls; some games may block synthetic input. The script will try `pydirectinput` (DirectInput) if installed, which often works with games. If it still doesn't work, run with administrator privileges or try sending window messages.

DLL injection (advanced)
- A minimal DLL + injector are included for an in-process injection approach. This will load a DLL into the game's process and the DLL sends an Alt keypress from inside the process. This is powerful and risky: it can trigger anti-cheat or be considered a modification to the game process. Use at your own risk.

Build DLL (MSVC):

```powershell
cd "d:\Script projects\Tarkov\maplestory scripts"
cl /LD dll_sendalt.c user32.lib
```

Inject DLL (requires Administrator):

```powershell
python inject_dll.py DreamMS.exe dll_sendalt.dll
```

Warnings:
- DLL injection and driver-level methods can violate terms of service and may cause bans. Prefer hardware HID if you want reliability without modifying the game process.