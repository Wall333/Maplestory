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
ATTACK_KEY_DOWN = False
ATTACK_STOP_EVENT = None
ATTACK_THREAD = None
COMBAT_THREAD = None
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


class ArduinoConnection:
    """Persistent, serialized Arduino connection shared only at the transport layer."""

    def __init__(self):
        self._serial = None
        self._port = None
        self._baud = None
        self._lock = threading.Lock()

    def _close_locked(self):
        arduino = self._serial
        self._serial = None
        self._port = None
        self._baud = None
        if arduino is not None:
            try:
                arduino.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._close_locked()

    def _ensure_connected_locked(self, cfg):
        if serial is None:
            return None

        # Reuse the already-open connection. This avoids repeatedly opening the
        # Uno's COM port for every attack tap.
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    return self._serial
            except Exception:
                pass
            self._close_locked()

        port = resolve_arduino_port(cfg)
        baud = int(cfg.get("baud_rate", 9600))

        arduino = serial.Serial(port, baud, timeout=0.2, write_timeout=0.2)
        self._serial = arduino
        self._port = port
        self._baud = baud
        return arduino

    def send_sequence(self, cfg, payloads, gaps=None, cancel_event=None):
        """Send one logical action without allowing another action to interleave.

        Attack passes its per-hold stop event here. If Space is released while
        attack is waiting for the serial port, the waiting CTRL is discarded
        instead of being sent later after a combat action.
        """
        if not cfg.get("use_arduino", False):
            return False
        if serial is None:
            print("pyserial is not installed. Install it with: pip install pyserial")
            return False

        payloads = list(payloads)
        gaps = list(gaps or [])

        # Acquire in short slices so an attack command can be cancelled while
        # waiting behind another serial action.
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            if self._lock.acquire(timeout=0.005):
                break

        try:
            if cancel_event is not None and cancel_event.is_set():
                return False

            arduino = self._ensure_connected_locked(cfg)
            if arduino is None:
                return False

            for index, payload in enumerate(payloads):
                if cancel_event is not None and cancel_event.is_set():
                    return False

                arduino.write((payload + "\n").encode("ascii"))
                arduino.flush()

                if index < len(payloads) - 1:
                    gap = gaps[index] if index < len(gaps) else 0.0
                    if gap > 0:
                        # Combat's two ALT taps are one action, so keep the port
                        # for the whole pair. Attack has only one payload.
                        time.sleep(gap)
            return True
        except Exception as exc:
            print(f"Arduino send failed: {exc}")
            self._close_locked()
            return False
        finally:
            self._lock.release()

    def connected(self, cfg):
        if serial is None or not cfg.get("use_arduino", False):
            return False
        if not self._lock.acquire(timeout=0.05):
            # A write in progress means the connection is actively being used.
            return True
        try:
            return self._ensure_connected_locked(cfg) is not None
        except Exception:
            self._close_locked()
            return False
        finally:
            self._lock.release()


ARDUINO = ArduinoConnection()


def send_combat_to_arduino(cfg):
    """Combat path: ALT only."""
    hold_ms = int(round(max(0.0, float(cfg.get("combat_hold", 0.01))) * 1000))
    gap = max(0.0, float(cfg.get("double_tap_delay", 0.02)))
    return ARDUINO.send_sequence(
        cfg,
        [f"ALT {hold_ms}", f"ALT {hold_ms}"],
        gaps=[gap],
    )


def send_attack_to_arduino(cfg, stop_event):
    """Attack path: CTRL only; may be cancelled by Space release."""
    hold_ms = int(round(max(0.0, float(cfg.get("attack_hold", 0.01))) * 1000))
    return ARDUINO.send_sequence(
        cfg,
        [f"CTRL {hold_ms}"],
        cancel_event=stop_event,
    )

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


def combat_trigger(cfg):
    """Run one combat action. This path is completely separate from attack."""
    global LAST_TRIGGER_TIME

    if not cfg.get("enabled", True):
        return

    now = time.monotonic()
    if now - LAST_TRIGGER_TIME < TRIGGER_COOLDOWN_SECONDS:
        return
    LAST_TRIGGER_TIME = now

    if not is_dreamms_active():
        log("DreamMS not active; ignoring combat trigger.")
        return

    delay = max(0.0, float(cfg.get("alt_delay", 0.05)))
    log(f"Combat trigger: key={cfg.get('combat_key', 'v')} | delay={delay:.3f}s")
    if delay > 0:
        time.sleep(delay)

    if cfg.get("use_arduino", False):
        send_combat_to_arduino(cfg)
    else:
        combat_step(cfg)


def handle_combat_key_press(cfg):
    global COMBAT_KEY_DOWN, COMBAT_THREAD
    if COMBAT_KEY_DOWN:
        return
    if not cfg.get("enabled", True):
        return

    COMBAT_KEY_DOWN = True
    thread = threading.Thread(
        target=combat_trigger,
        args=(cfg,),
        daemon=True,
        name="CombatAction",
    )
    COMBAT_THREAD = thread
    thread.start()


def handle_combat_key_release(event=None):
    global COMBAT_KEY_DOWN
    COMBAT_KEY_DOWN = False


def attack_loop(cfg, stop_event):
    """Spam CTRL while attack is held. Has its own stop event and no combat state."""
    log(
        f"Attack started: key={cfg.get('attack_key', 'space')} | "
        f"hold={float(cfg.get('attack_hold', 0.01)):.4f}s | "
        f"delay={float(cfg.get('attack_delay', 0.20)):.4f}s"
    )

    while not stop_event.is_set():
        if not cfg.get("enabled", True) or not is_dreamms_active():
            break

        if cfg.get("use_arduino", False):
            send_attack_to_arduino(cfg, stop_event)
        else:
            send_virtual_key(0x11, max(0.0, float(cfg.get("attack_hold", 0.01))))

        delay = max(0.0, float(cfg.get("attack_delay", 0.20)))
        if delay > 0 and stop_event.wait(delay):
            break

    log("Attack stopped.")


def handle_attack_key_press(cfg):
    global ATTACK_KEY_DOWN, ATTACK_THREAD, ATTACK_STOP_EVENT

    if ATTACK_KEY_DOWN:
        return
    if not cfg.get("enabled", True):
        return
    if not is_dreamms_active():
        return

    ATTACK_KEY_DOWN = True

    # Each hold gets a brand-new Event. An old worker can never be revived by
    # clearing a shared Event during a later Space press.
    stop_event = threading.Event()
    ATTACK_STOP_EVENT = stop_event

    thread = threading.Thread(
        target=attack_loop,
        args=(cfg, stop_event),
        daemon=True,
        name="AttackLoop",
    )
    ATTACK_THREAD = thread
    thread.start()


def stop_attack_loop(wait=False):
    global ATTACK_KEY_DOWN, ATTACK_THREAD, ATTACK_STOP_EVENT

    ATTACK_KEY_DOWN = False

    stop_event = ATTACK_STOP_EVENT
    if stop_event is not None:
        stop_event.set()

    thread = ATTACK_THREAD
    if wait and thread is not None and thread.is_alive() and thread is not threading.current_thread():
        # Only a short cleanup wait. An already-sent Arduino pulse cannot be
        # recalled, but no new CTRL command will be scheduled after release.
        thread.join(timeout=0.25)

    if thread is None or not thread.is_alive():
        if ATTACK_THREAD is thread:
            ATTACK_THREAD = None
        if ATTACK_STOP_EVENT is stop_event:
            ATTACK_STOP_EVENT = None


def handle_attack_key_release(event=None):
    # Do not touch combat state here.
    stop_attack_loop(wait=False)


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
        log_frame.grid(row=12, column=0, columnspan=6, sticky="nsew", padx=4, pady=(12, 0))
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

        ttk.Label(frm, text="Attack key:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=4)
        self.attack_key = ttk.Entry(frm, width=10)
        self.attack_key.insert(0, self.cfg.get("attack_key", "space"))
        self.attack_key.grid(row=1, column=1, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Attack CTRL hold (s):").grid(row=1, column=2, sticky=tk.W, padx=(12, 4), pady=4)
        self.attack_hold = ttk.Entry(frm, width=8)
        self.attack_hold.insert(0, str(self.cfg.get("attack_hold", 0.01)))
        self.attack_hold.grid(row=1, column=3, sticky=tk.W, padx=4, pady=4)

        ttk.Label(frm, text="Attack repeat delay (s):").grid(row=2, column=2, sticky=tk.W, padx=(12, 4), pady=4)
        self.attack_delay = ttk.Entry(frm, width=8)
        self.attack_delay.insert(0, str(self.cfg.get("attack_delay", 0.20)))
        self.attack_delay.grid(row=2, column=3, sticky=tk.W, padx=4, pady=4)

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
            combat_key = normalize_key_name(self.combat_key.get()) or "v"
            attack_key = normalize_key_name(self.attack_key.get()) or "space"
            toggle_key = normalize_key_name(self.cfg.get("toggle_key", "F12")) or "F12"
            if combat_key.lower() == attack_key.lower():
                raise ValueError("Combat key and attack key must be different.")
            if combat_key.lower() == toggle_key.lower() or attack_key.lower() == toggle_key.lower():
                raise ValueError("Combat/attack keys must be different from the toggle key.")

            self.cfg["combat_key"] = combat_key
            self.cfg["combat_hold"] = max(0.0, float(self.combat_hold.get() or 0.01))
            self.cfg["attack_key"] = attack_key
            self.cfg["attack_hold"] = max(0.0, float(self.attack_hold.get() or 0.01))
            self.cfg["attack_delay"] = max(0.0, float(self.attack_delay.get() or 0.20))
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

        combat_key = normalize_key_name(self.cfg.get("combat_key", "v")) or "v"
        attack_key = normalize_key_name(self.cfg.get("attack_key", "space")) or "space"
        toggle_key = normalize_key_name(self.cfg.get("toggle_key", "F12")) or "F12"

        # Never permit two actions to share the same physical trigger key.
        if combat_key.lower() == attack_key.lower():
            log("Hotkey error: combat key and attack key are the same; action keys not bound.")
            return

        stop_attack_loop(wait=False)

        try:
            keyboard.unhook_all_hotkeys()
            keyboard.unhook_all()
        except Exception:
            pass

        # F12 always remains available so the helper can be toggled back on.
        try:
            keyboard.on_press_key(toggle_key, lambda event: self.toggle(), suppress=False)
            log(f"Toggle hotkey bound: {toggle_key}")
        except Exception as exc:
            log(f"Toggle hotkey error: {exc}")

        # When disabled, do not hook the action keys at all.
        if not self.enabled:
            log("Helper disabled: combat/attack keys are unbound and pass through normally.")
            return

        try:
            keyboard.on_press_key(
                combat_key,
                lambda event: handle_combat_key_press(self.cfg),
                suppress=False,
            )
            keyboard.on_release_key(
                combat_key,
                lambda event: handle_combat_key_release(event),
                suppress=False,
            )
            log(f"Combat key bound: {combat_key} -> Arduino ALT (key passes through normally)")
        except Exception as exc:
            log(f"Combat hotkey error: {exc}")

        try:
            keyboard.on_press_key(
                attack_key,
                lambda event: handle_attack_key_press(self.cfg),
                suppress=False,
            )
            keyboard.on_release_key(
                attack_key,
                lambda event: handle_attack_key_release(event),
                suppress=False,
            )
            log(f"Attack key bound: {attack_key} -> Arduino CTRL spam (key passes through normally)")
        except Exception as exc:
            log(f"Attack hotkey error: {exc}")

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

        try:
            if ARDUINO.connected(self.cfg):
                port = ARDUINO._port or self.cfg.get("serial_port", "COM3")
                if port:
                    self.cfg["serial_port"] = port
                    self.serial_port.delete(0, tk.END)
                    self.serial_port.insert(0, port)
                self.arduino_status.config(text="Connected", foreground="green")
            else:
                self.arduino_status.config(text="Not found", foreground="red")
        except Exception:
            self.arduino_status.config(text="Not found", foreground="red")

    def toggle(self):
        self.enabled = not self.enabled
        self.cfg["enabled"] = self.enabled
        if not self.enabled:
            stop_attack_loop(wait=False)
        save_config(self.cfg)
        self.update_toggle_status()
        self.rebind_hotkey()
        log(f"Toggle switched to {'ON' if self.enabled else 'OFF'}")

    def test(self):
        try:
            combat_trigger(self.cfg)
        except Exception as exc:
            messagebox.showerror("Trigger failed", str(exc))

    def on_close(self):
        handle_attack_key_release()
        try:
            self.cfg["enabled"] = bool(self.enabled)
            save_config(self.cfg)
        except Exception:
            pass
        ARDUINO.close()
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