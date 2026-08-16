import ctypes
import json
import os
import sys
import threading
import time
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import psutil
except Exception:
    psutil = None

try:
    import win32gui
    import win32process
except Exception:
    win32gui = None
    win32process = None

try:
    import keyboard
except Exception:
    keyboard = None

try:
    import serial
except Exception:
    serial = None

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LOG_PATH = os.path.join(os.path.dirname(__file__), "recent_logs.txt")
TRIGGER_COOLDOWN_SECONDS = 0.0
LAST_TRIGGER_TIME = 0.0
COMBAT_KEY_DOWN = False
LOG_BUFFER = deque([], maxlen=300)
LOG_WIDGET = None

DEFAULTS = {
    "enabled": True,
    "toggle_key": "F12",
    "combat_key": "v",
    "combat_hold": 0.01,
    "attack_key": "space",
    "attack_hold": 0.01,
    "alt_delay": 0.05,
    "double_tap_delay": 0.02,
    "attack_delay": 0.20,
    "use_arduino": True,
    "auto_detect_arduino": True,
    "serial_port": "COM3",
    "baud_rate": 9600,
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return {**DEFAULTS, **cfg}
        except Exception:
            pass
    return DEFAULTS.copy()


def log(message):
    entry = f"[{time.strftime('%H:%M:%S')}] {message}"
    LOG_BUFFER.append(entry)
    print(entry)

    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(entry + "\n")
    except Exception:
        pass

    lines = list(LOG_BUFFER)
    if len(lines) > 300:
        lines = lines[-300:]

    if LOG_WIDGET is not None:
        try:
            LOG_WIDGET.configure(state="normal")
            LOG_WIDGET.delete("1.0", tk.END)
            for item in lines:
                LOG_WIDGET.insert(tk.END, item + "\n")
            LOG_WIDGET.see(tk.END)
            LOG_WIDGET.configure(state="disabled")
        except Exception:
            pass

    try:
        with open(LOG_PATH, "r", encoding="utf-8") as log_file:
            all_lines = log_file.readlines()
        if len(all_lines) > 300:
            with open(LOG_PATH, "w", encoding="utf-8") as log_file:
                log_file.writelines(all_lines[-300:])
    except Exception:
        pass



def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as exc:
        log(f"Failed to save config: {exc}")


USER32 = ctypes.WinDLL("user32", use_last_error=True)
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
VK_MENU = 0x12
PUL = ctypes.POINTER(ctypes.c_ulong)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    )


class INPUT_I(ctypes.Union):
    _fields_ = (("ki", KEYBDINPUT),)


class INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("ii", INPUT_I))


def send_virtual_key(vk, hold=0.03):
    extra = ctypes.c_ulong(0)
    ii_ = INPUT_I()
    ki_down = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=ctypes.pointer(extra))
    ki_up = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=ctypes.pointer(extra))

    ii_.ki = ki_down
    x_down = INPUT(type=INPUT_KEYBOARD, ii=ii_)
    USER32.SendInput(1, ctypes.byref(x_down), ctypes.sizeof(x_down))

    if hold > 0:
        time.sleep(hold)

    ii_.ki = ki_up
    x_up = INPUT(type=INPUT_KEYBOARD, ii=ii_)
    USER32.SendInput(1, ctypes.byref(x_up), ctypes.sizeof(x_up))


def send_character(character, hold=0.03):
    key = str(character).strip()
    if not key:
        return False

    vk = USER32.VkKeyScanW(ord(key)) & 0xFF
    if vk == 0xFF:
        return False

    send_virtual_key(vk, hold)
    return True


def combat_step(cfg):
    delay = max(0.0, float(cfg.get("alt_delay", 0.05)))
    hold = max(0.0, float(cfg.get("combat_hold", 0.01)))
    double_tap_delay = max(0.0, float(cfg.get("double_tap_delay", 0.02)))

    if delay > 0:
        log(f"Combat: delay={delay:.4f}s before trigger")
        time.sleep(delay)
        log("Combat: delay complete, pressing ALT")

    log(f"Combat: first ALT tap hold={hold:.4f}s")
    send_virtual_key(VK_MENU, hold)

    if double_tap_delay > 0:
        log(f"Combat: waiting {double_tap_delay:.4f}s before second ALT tap")
        time.sleep(double_tap_delay)

    log(f"Combat: second ALT tap hold={hold:.4f}s")
    send_virtual_key(VK_MENU, hold)
    log("Combat: ALT double-tap complete")


def normalize_key_name(value):
    key = str(value or "").strip()
    if not key:
        return ""
    lowered = key.lower()
    if lowered in {"space", "spacebar", " "}:
        return "space"
    return key


def detect_arduino_port():
    if serial is None:
        return None

    try:
        from serial.tools import list_ports
    except Exception:
        return None

    candidates = []
    for port in list_ports.comports():
        text = " ".join(
            str(part)
            for part in (
                port.device,
                port.name,
                port.description,
                port.manufacturer,
                port.product,
            )
            if part
        ).lower()

        if not text:
            continue

        if "arduino" in text or "r4" in text or "uno" in text or "usb serial device" in text:
            candidates.append(port.device)

    if not candidates:
        return None

    return candidates[0]


def resolve_arduino_port(cfg):
    if cfg.get("auto_detect_arduino", True):
        detected = detect_arduino_port()
        if detected:
            cfg["serial_port"] = detected
            return detected

    return cfg.get("serial_port", "COM3")


def send_to_arduino(cfg, command="JUMP"):
    if not cfg.get("use_arduino", False):
        return False

    if serial is None:
        print("pyserial is not installed. Install it with: pip install pyserial")
        return False

    port = resolve_arduino_port(cfg)
    baud = int(cfg.get("baud_rate", 9600))
    if command == "ATTACK":
        hold_seconds = max(0.0, float(cfg.get("attack_hold", 0.01)))
        payloads = [f"CTRL {int(round(hold_seconds * 1000))}"]
    else:
        hold_seconds = max(0.0, float(cfg.get("combat_hold", 0.01)))
        payloads = [f"ALT {int(round(hold_seconds * 1000))}"]
        gap = max(0.0, float(cfg.get("double_tap_delay", 0.02)))
        if gap > 0:
            payloads.append(f"ALT {int(round(hold_seconds * 1000))}")

    try:
        with serial.Serial(port, baud, timeout=0.5) as arduino:
            for index, payload in enumerate(payloads):
                arduino.write((payload + "\n").encode("ascii"))
                arduino.flush()
                if index < len(payloads) - 1 and gap > 0:
                    time.sleep(gap)
        return True
    except Exception as exc:
        print(f"Arduino send failed on {port}: {exc}")
        return False


def is_dreamms_active():
    if win32gui is None or win32process is None or psutil is None:
        return True

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return False

        process = psutil.Process(pid)
        name = process.name().lower()
        return name == "dreamms.exe"
    except Exception:
        return False


def trigger(cfg):
    global LAST_TRIGGER_TIME

    if not cfg.get("enabled", True):
        return

    now = time.monotonic()
    if now - LAST_TRIGGER_TIME < TRIGGER_COOLDOWN_SECONDS:
        return
    LAST_TRIGGER_TIME = now

    if not is_dreamms_active():
        log("DreamMS not active; ignoring trigger.")
        return

    delay = max(0.0, float(cfg.get("alt_delay", 0.05)))
    log(f"Trigger fired: combat_key={cfg.get('combat_key', 'v')} | window=DreamMS.exe | delay={delay:.3f}s")
    if delay > 0:
        time.sleep(delay)

    if cfg.get("use_arduino", False):
        send_to_arduino(cfg, "JUMP")
    else:
        combat_step(cfg)


def handle_combat_key_press(cfg):
    global COMBAT_KEY_DOWN
    if COMBAT_KEY_DOWN:
        return
    COMBAT_KEY_DOWN = True
    trigger(cfg)


def handle_combat_key_release(event=None):
    global COMBAT_KEY_DOWN
    COMBAT_KEY_DOWN = False


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.enabled = bool(self.cfg.get("enabled", True))
        self.root.title("MapleStory Combat Helper")
        self.root.geometry("620x520")

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        log_frame = ttk.LabelFrame(frm, text="Recent logs")
        log_frame.grid(row=11, column=0, columnspan=6, sticky="nsew", padx=4, pady=(12, 0))
        self.log_view = tk.Text(log_frame, height=12, width=80, state="disabled", wrap=tk.WORD)
        self.log_view.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        global LOG_WIDGET
        LOG_WIDGET = self.log_view
        self.refresh_recent_logs()

        ttk.Label(frm, text="Combat key:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=4)
        self.combat_key = ttk.Entry(frm, width=10)
        self.combat_key.insert(0, self.cfg.get("combat_key", "v"))
        self.combat_key.grid(row=0, column=1, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Combat hold (s):").grid(row=0, column=2, sticky=tk.W, padx=(12, 4), pady=4)
        self.combat_hold = ttk.Entry(frm, width=8)
        self.combat_hold.insert(0, str(self.cfg.get("combat_hold", 0.01)))
        self.combat_hold.grid(row=0, column=3, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Alt delay (s):").grid(row=0, column=4, sticky=tk.W, padx=(12, 4), pady=4)
        self.alt_delay = ttk.Entry(frm, width=8)
        self.alt_delay.insert(0, str(self.cfg.get("alt_delay", 0.05)))
        self.alt_delay.grid(row=0, column=5, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Combat tap gap (s):").grid(row=1, column=4, sticky=tk.W, padx=(12, 4), pady=4)
        self.double_tap_delay = ttk.Entry(frm, width=8)
        self.double_tap_delay.insert(0, str(self.cfg.get("double_tap_delay", 0.02)))
        self.double_tap_delay.grid(row=1, column=5, sticky=tk.W, padx=4, pady=4)

        self.use_arduino = tk.BooleanVar(value=bool(self.cfg.get("use_arduino", True)))
        ttk.Checkbutton(frm, text="Use Arduino Uno R4 WiFi", variable=self.use_arduino).grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=4, pady=4)

        self.auto_detect = tk.BooleanVar(value=bool(self.cfg.get("auto_detect_arduino", True)))
        ttk.Checkbutton(frm, text="Auto-detect Arduino", variable=self.auto_detect).grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Serial port:").grid(row=5, column=0, sticky=tk.W, padx=4, pady=4)
        self.serial_port = ttk.Entry(frm, width=12)
        self.serial_port.insert(0, self.cfg.get("serial_port", "COM3"))
        self.serial_port.grid(row=5, column=1, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Baud:").grid(row=6, column=0, sticky=tk.W, padx=4, pady=4)
        self.baud_rate = ttk.Entry(frm, width=12)
        self.baud_rate.insert(0, str(self.cfg.get("baud_rate", 9600)))
        self.baud_rate.grid(row=6, column=1, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Window status:").grid(row=7, column=0, sticky=tk.W, padx=4, pady=(8, 2))
        self.window_status = ttk.Label(frm, text="Checking...", foreground="orange")
        self.window_status.grid(row=7, column=1, sticky=tk.W, padx=4, pady=(8, 2))

        ttk.Label(frm, text="Toggle status:").grid(row=8, column=0, sticky=tk.W, padx=4, pady=2)
        self.toggle_status = ttk.Label(frm, text="On" if self.enabled else "Off", foreground="green" if self.enabled else "red")
        self.toggle_status.grid(row=8, column=1, sticky=tk.W, padx=4, pady=2)

        ttk.Label(frm, text="Arduino:").grid(row=9, column=0, sticky=tk.W, padx=4, pady=2)
        self.arduino_status = ttk.Label(frm, text="Checking...", foreground="orange")
        self.arduino_status.grid(row=9, column=1, sticky=tk.W, padx=4, pady=2)

        self.update_window_status()
        self.update_arduino_status()
        self.root.after(250, self.refresh_status)

        button_frame = ttk.Frame(frm)
        button_frame.grid(row=10, column=0, columnspan=2, pady=(10, 0), sticky=tk.W)
        ttk.Button(button_frame, text="Save", command=self.save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(button_frame, text="Auto detect", command=self.auto_detect_port).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(button_frame, text="Test", command=self.test).pack(side=tk.LEFT)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def save(self):
        try:
            self.cfg["combat_key"] = normalize_key_name(self.combat_key.get()) or "v"
            self.cfg["combat_hold"] = max(0.0, float(self.combat_hold.get() or 0.01))
            self.cfg["alt_delay"] = max(0.0, float(self.alt_delay.get() or 0.05))
            self.cfg["double_tap_delay"] = max(0.0, float(self.double_tap_delay.get() or 0.02))
            self.cfg["use_arduino"] = bool(self.use_arduino.get())
            self.cfg["auto_detect_arduino"] = bool(self.auto_detect.get())
            self.cfg["serial_port"] = self.serial_port.get().strip() or "COM3"
            self.cfg["baud_rate"] = int(self.baud_rate.get() or 9600)
            self.cfg["enabled"] = bool(self.enabled)
            save_config(self.cfg)
            self.update_window_status()
            self.update_arduino_status()
            self.rebind_hotkey()
            messagebox.showinfo("Saved", "Settings saved.")
        except Exception as exc:
            messagebox.showerror("Invalid input", f"Check your values: {exc}")

    def auto_detect_port(self):
        detected = detect_arduino_port()
        if detected:
            self.cfg["serial_port"] = detected
            self.serial_port.delete(0, tk.END)
            self.serial_port.insert(0, detected)
            self.update_arduino_status()
            messagebox.showinfo("Arduino found", f"Detected Arduino on {detected}")
        else:
            self.arduino_status.config(text="Not found", foreground="red")
            messagebox.showwarning("Arduino not found", "No Arduino-compatible device was detected on the serial ports.")

    def rebind_hotkey(self):
        if keyboard is None:
            return
        key = normalize_key_name(self.cfg.get("combat_key", "v")) or "v"
        toggle_key = str(self.cfg.get("toggle_key", "F12")).strip() or "F12"

        try:
            keyboard.unhook_all_hotkeys()
            keyboard.unhook_all()
        except Exception:
            pass

        try:
            keyboard.on_press_key(toggle_key, lambda event: self.toggle(), suppress=False)
            log(f"Toggle hotkey bound: {toggle_key}")
        except Exception as exc:
            log(f"Toggle hotkey error: {exc}")
        try:
            keyboard.on_press_key(key, lambda event: handle_combat_key_press(self.cfg), suppress=False)
            keyboard.on_release_key(key, lambda event: handle_combat_key_release(event), suppress=False)
            log(f"Combat key bound: {key} (single trigger per press)")
        except Exception as exc:
            log(f"Combat hotkey error: {exc}")

    def update_window_status(self):
        if is_dreamms_active():
            self.window_status.config(text="Active", foreground="green")
        else:
            self.window_status.config(text="Inactive", foreground="red")

    def update_toggle_status(self):
        if self.enabled:
            self.toggle_status.config(text="On", foreground="green")
        else:
            self.toggle_status.config(text="Off", foreground="red")

    def refresh_status(self):
        self.update_window_status()
        self.update_toggle_status()
        self.refresh_recent_logs()
        self.root.after(250, self.refresh_status)

    def refresh_recent_logs(self):
        if self.log_view is None:
            return
        try:
            self.log_view.configure(state="normal")
            self.log_view.delete("1.0", tk.END)
            for entry in list(LOG_BUFFER):
                self.log_view.insert(tk.END, entry + "\n")
            self.log_view.see(tk.END)
            self.log_view.configure(state="disabled")
        except Exception:
            pass

    def update_arduino_status(self):
        if not self.cfg.get("use_arduino", False):
            self.arduino_status.config(text="Off", foreground="gray")
            return

        if serial is None:
            self.arduino_status.config(text="Missing pyserial", foreground="red")
            return

        port = resolve_arduino_port(self.cfg)
        if port:
            self.serial_port.delete(0, tk.END)
            self.serial_port.insert(0, port)
        else:
            port = self.cfg.get("serial_port", "COM3")

        try:
            with serial.Serial(port, int(self.cfg.get("baud_rate", 9600)), timeout=0.2):
                self.arduino_status.config(text="Connected", foreground="green")
        except Exception:
            self.arduino_status.config(text="Not found", foreground="red")

    def toggle(self):
        self.enabled = not self.enabled
        self.cfg["enabled"] = self.enabled
        save_config(self.cfg)
        self.update_toggle_status()
        log(f"Toggle switched to {'ON' if self.enabled else 'OFF'}")

    def test(self):
        try:
            trigger(self.cfg)
        except Exception as exc:
            messagebox.showerror("Trigger failed", str(exc))

    def on_close(self):
        try:
            self.cfg["enabled"] = bool(self.enabled)
            save_config(self.cfg)
        except Exception:
            pass
        if keyboard is not None:
            try:
                keyboard.remove_hotkey(str(self.cfg.get("toggle_key", "F12")).strip() or "F12")
            except Exception:
                pass
            try:
                keyboard.remove_hotkey(normalize_key_name(self.cfg.get("combat_key", "v")) or "v")
            except Exception:
                pass
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        self.root.destroy()
        os._exit(0)


def main():
    log("Starting MapleStory combat helper...")
    if keyboard is None:
        print("keyboard package is missing. Install with: pip install keyboard pyserial")

    root = tk.Tk()
    app = App(root)
    if keyboard is not None:
        app.rebind_hotkey()
    root.mainloop()


if __name__ == "__main__":
    main()
