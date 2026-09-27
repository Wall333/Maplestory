# MapleStory helper projects

This repository contains two independent Windows utilities. Each lives in its
own folder with separate configuration, documentation, launcher, dependencies,
and Arduino firmware.

## Projects

- [`Aran keybinds`](Aran%20keybinds/README.md) — configurable Aran combat,
  attack, combo, combo-drain, Fenrir, and visual safety helpers.
- [`Vos Bot`](Vos%20Bot/README.md) — configurable VoS key spam, map checking,
  visual alignment overlay, and optional left/right auto-alignment.

## Portable Windows builds

Ready-to-run packages are placed in `Releases/` when built. Extract one ZIP and
run the `.exe`; Python and pip are not required on the destination PC. The
Arduino still needs the matching shared firmware flashed once.

To rebuild both packages on a development PC, install the source dependencies
and PyInstaller, then run:

```powershell
.\build_portable.ps1
```

These tools are intended for the owner's test environment. Review the rules of
any server or software before using automation.
