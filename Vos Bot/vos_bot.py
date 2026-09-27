import ctypes
import json
import os
import queue
import threading
import time
from collections import deque
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import keyboard
except ImportError:
    keyboard = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None

try:
    import cv2
    import mss
    import numpy as np
except ImportError:
    cv2 = None
    mss = None
    np = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "recent_logs.txt")
VOS_MAP_TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "VOS_map.png")
AREA_TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "area.png")
LIGHTBULB_TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "lightbulb.png")
CRYSTAL_TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "crystal.png")
YETI_TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "yeti.png")
THORNS_TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "thorns.png")

DEFAULTS = {
    "enabled": True,
    "toggle_key": "F11",
    "vos_enabled": True,
    "vos_toggle_key": "F10",
    "vos_output_key": "F",
    "vos_hold": 0.03,
    "vos_interval": 2.0,
    "vos_map_checker_enabled": True,
    "vos_map_checks_per_second": 10,
    "vos_map_match_threshold": 0.60,
    "alignment_overlay_enabled": True,
    "alignment_checks_per_second": 10,
    "alignment_match_threshold": 0.70,
    "alignment_target_mode": "manual",
    "manual_anchor_offset": 0,
    "auto_align_enabled": True,
    "auto_align_tolerance_pixels": 200,
    "auto_align_key_hold": 0.5,
    "auto_align_interval": 0.5,
    "show_spammer_overlay": True,
    "spam_overlay_x_percent": 85,
    "spam_overlay_y_percent": 4,
    "show_crystal": True,
    "loot_crystal": True,
    "yeti_required": True,
    "yeti_checks_per_second": 10,
    "yeti_match_threshold": 0.50,
    "buff_enabled": True,
    "buff_key": "END",
    "buff_key_hold": 0.03,
    "buff_wait_seconds": 5.0,
    "thorns_checks_per_second": 10,
    "thorns_match_threshold": 0.50,
    "use_arduino": True,
    "auto_detect_arduino": True,
    "serial_port": "COM3",
    "baud_rate": 9600,
}

SPAM_KEYS = ("END", "PGDN", "X", "C", "V", "B", "D", "F", "Y")
VK_KEYS = {
    "END": 0x23,
    "PGDN": 0x22,
    "X": 0x58,
    "C": 0x43,
    "V": 0x56,
    "B": 0x42,
    "D": 0x44,
    "F": 0x46,
    "Y": 0x59,
}
OUTPUT_ALIASES = {
    "END": "END",
    "PGDN": "PGDN",
    "PAGE DOWN": "PGDN",
    "PAGEDOWN": "PGDN",
    "NEXT": "PGDN",
    "X": "X",
    "C": "C",
    "V": "V",
    "B": "B",
    "D": "D",
    "F": "F",
    "Y": "Y",
}

LOG_BUFFER = deque(maxlen=300)


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return {**DEFAULTS, **json.load(handle)}
    except (OSError, ValueError, TypeError):
        return DEFAULTS.copy()


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def log(message):
    entry = f"[{time.strftime('%H:%M:%S')}] {message}"
    LOG_BUFFER.append(entry)
    print(entry)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
    except OSError:
        pass


def normalize_output_key(value):
    return OUTPUT_ALIASES.get(str(value).strip().upper(), "END")


def is_dreamms_active():
    return get_active_dreamms_context() is not None


def get_active_dreamms_context():
    """Return (window handle, client-area screen bounds) for active DreamMS."""
    if win32gui is None or win32process is None or psutil is None:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if psutil.Process(pid).name().lower() != "dreamms.exe":
            return None
        left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        rect = win32gui.GetClientRect(hwnd)
        width, height = int(rect[2] - rect[0]), int(rect[3] - rect[1])
        if width <= 0 or height <= 0:
            return None
        return int(hwnd), (int(left), int(top), int(left + width), int(top + height))
    except Exception:
        return None


def detect_arduino_port():
    if list_ports is None:
        return None
    ports = list(list_ports.comports())
    scored = []
    for port in ports:
        vid = getattr(port, "vid", None)
        text = " ".join(
            str(value)
            for value in (
                getattr(port, "description", ""),
                getattr(port, "manufacturer", ""),
                getattr(port, "product", ""),
                getattr(port, "hwid", ""),
            )
            if value
        ).lower()
        score = 0
        if vid in (0x2341, 0x2A03):
            score += 5000
        elif vid == 0x16C0:
            score += 4000
        if "arduino" in text:
            score += 1500
        if "teensy" in text:
            score += 1500
        # A generic "USB Serial Device" is deliberately not sufficient when
        # several unrelated COM devices are connected.
        if score:
            scored.append((score, str(port.device), port.device))
    if not scored:
        return ports[0].device if len(ports) == 1 else None
    scored.sort(reverse=True)
    return scored[0][2]


class ArduinoConnection:
    def __init__(self):
        self._serial = None
        self._port = None
        self._lock = threading.Lock()
        self._logged_first_send = False

    def close(self):
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
            self._port = None
            self._logged_first_send = False

    def _wanted_port(self, config):
        if config.get("auto_detect_arduino", True):
            return detect_arduino_port() or str(config.get("serial_port", "COM3"))
        return str(config.get("serial_port", "COM3"))

    def _connect_locked(self, config):
        if serial is None:
            return False
        port = self._wanted_port(config)
        baud = int(config.get("baud_rate", 9600))
        if self._serial is not None and self._port == port and self._serial.is_open:
            return True
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._port = None
        try:
            self._serial = serial.Serial(port, baud, timeout=0.2, write_timeout=0.5)
            self._port = port
            time.sleep(0.15)
            return True
        except (OSError, serial.SerialException):
            self._serial = None
            return False

    def connected(self, config):
        with self._lock:
            return self._connect_locked(config)

    def send_command(self, config, command, hold_seconds):
        command = str(command).strip().upper()
        allowed = set(SPAM_KEYS) | {"LEFT", "RIGHT"}
        if command not in allowed:
            return False
        payload = f"{command} {max(1, round(hold_seconds * 1000))}\n"
        with self._lock:
            if not self._connect_locked(config):
                return False
            try:
                self._serial.write(payload.encode("ascii"))
                self._serial.flush()
                if not self._logged_first_send:
                    log(f"Arduino write confirmed: {payload.strip()} -> {self._port}")
                    self._logged_first_send = True
                return True
            except (OSError, serial.SerialException):
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
                self._port = None
                return False

    def send_key(self, config, key, hold_seconds):
        return self.send_command(config, normalize_output_key(key), hold_seconds)


ARDUINO = ArduinoConnection()


class VosMapDetector:
    """Continuously verifies that VOS_map.png is visible in active DreamMS."""

    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.matched = False
        self.status = "Starting"
        self.score = 0.0
        self.last_check = 0.0
        self.cached_location = None
        self.cached_hwnd = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="vos-map-checker", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def snapshot(self):
        with self.lock:
            return self.matched, self.status, self.score, self.last_check

    def allows_spam(self):
        if not self.app.config.get("vos_map_checker_enabled", True):
            return True
        matched, _, _, checked = self.snapshot()
        rate = max(1.0, float(self.app.config.get("vos_map_checks_per_second", 10)))
        return matched and time.monotonic() - checked <= max(0.5, 3.0 / rate)

    def _set_state(self, matched, status, score=0.0):
        with self.lock:
            changed = self.matched != bool(matched) or self.status != status
            self.matched = bool(matched)
            self.status = status
            self.score = float(score)
            self.last_check = time.monotonic()
        if changed:
            log(f"VoS Map Checker: {status} | match={score:.3f}")
        if not matched and self.app.spam_active:
            self.app.stop_vos(f"VoS map checker: {status}")

    @staticmethod
    def _capture(sct, bbox):
        left, top, right, bottom = bbox
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        return np.asarray(shot, dtype=np.uint8)[:, :, :3]

    def _loop(self):
        if cv2 is None or mss is None or np is None:
            self._set_state(False, "Missing OpenCV/NumPy/MSS")
            return
        template = cv2.imread(VOS_MAP_TEMPLATE_PATH, cv2.IMREAD_COLOR)
        if template is None or template.size == 0:
            self._set_state(False, "Template missing")
            return
        template_h, template_w = template.shape[:2]

        try:
            with mss.mss() as sct:
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    enabled = bool(self.app.config.get("vos_map_checker_enabled", True))
                    rate = max(1.0, min(100.0, float(self.app.config.get("vos_map_checks_per_second", 10))))
                    threshold = max(0.5, min(0.9999, float(self.app.config.get("vos_map_match_threshold", 0.90))))

                    if not enabled:
                        self.cached_location = None
                        self.cached_hwnd = None
                        self._set_state(True, "Off", 1.0)
                    else:
                        context = get_active_dreamms_context()
                        if context is None:
                            self.cached_location = None
                            self.cached_hwnd = None
                            self._set_state(False, "Game inactive")
                        else:
                            hwnd, bbox = context
                            if hwnd != self.cached_hwnd:
                                self.cached_location = None
                                self.cached_hwnd = hwnd

                            score = 0.0
                            matched = False
                            if self.cached_location is not None:
                                x, y = self.cached_location
                                left, top, right, bottom = bbox
                                if x + template_w <= right - left and y + template_h <= bottom - top:
                                    roi = self._capture(
                                        sct,
                                        (left + x, top + y, left + x + template_w, top + y + template_h),
                                    )
                                    result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
                                    score = float(result[0, 0])
                                    matched = score >= threshold

                            # Reacquire across the client whenever the cached check fails.
                            if not matched:
                                frame = self._capture(sct, bbox)
                                if frame.shape[0] >= template_h and frame.shape[1] >= template_w:
                                    result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
                                    _, score, _, location = cv2.minMaxLoc(result)
                                    matched = float(score) >= threshold
                                    self.cached_location = tuple(location) if matched else None

                            self._set_state(matched, "Matched" if matched else "Not detected", score)

                    elapsed = time.monotonic() - started
                    self.stop_event.wait(max(0.0, (1.0 / rate) - elapsed))
        except Exception as exc:
            self._set_state(False, f"Detector error: {exc}")


class YetiDetector:
    """Presence gate that pauses spam until yeti.png is visible."""

    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.matched = False
        self.status = "Off"
        self.score = 0.0
        self.last_check = 0.0

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="yeti-detector", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def snapshot(self):
        with self.lock:
            return self.matched, self.status, self.score, self.last_check

    def allows_spam(self):
        if not self.app.config.get("yeti_required", False):
            return True
        matched, _, _, checked = self.snapshot()
        rate = max(1.0, float(self.app.config.get("yeti_checks_per_second", 10)))
        return matched and time.monotonic() - checked <= max(0.5, 3.0 / rate)

    def _set_state(self, matched, status, score=0.0):
        with self.lock:
            changed = self.matched != bool(matched) or self.status != status
            self.matched = bool(matched)
            self.status = status
            self.score = float(score)
            self.last_check = time.monotonic()
        if changed and status in {"Detected", "Not detected"}:
            log(f"Yeti Gate: {status} | match={score:.3f}")

    def _loop(self):
        if cv2 is None or mss is None or np is None:
            self._set_state(False, "Missing vision dependencies")
            return
        template = cv2.imread(YETI_TEMPLATE_PATH, cv2.IMREAD_COLOR)
        if template is None or template.size == 0:
            self._set_state(False, "Template missing")
            return
        template_h, template_w = template.shape[:2]
        try:
            with mss.mss() as sct:
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    enabled = bool(self.app.config.get("yeti_required", False))
                    rate = max(1.0, min(100.0, float(self.app.config.get("yeti_checks_per_second", 10))))
                    threshold = max(0.5, min(0.9999, float(self.app.config.get("yeti_match_threshold", 0.85))))
                    if not enabled:
                        self._set_state(True, "Off", 1.0)
                    else:
                        context = get_active_dreamms_context()
                        if context is None:
                            self._set_state(False, "Game inactive")
                        else:
                            frame = VosMapDetector._capture(sct, context[1])
                            if frame.shape[0] < template_h or frame.shape[1] < template_w:
                                self._set_state(False, "Not detected", 0.0)
                            else:
                                result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
                                _, score, _, _ = cv2.minMaxLoc(result)
                                matched = float(score) >= threshold
                                self._set_state(
                                    matched,
                                    "Detected" if matched else "Not detected",
                                    score,
                                )
                    elapsed = time.monotonic() - started
                    self.stop_event.wait(max(0.0, (1.0 / rate) - elapsed))
        except Exception as exc:
            self._set_state(False, f"Detector error: {exc}")


class ThornsDetector:
    """Tracks the Thorns buff icon and its client-relative rectangle."""

    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.matched = False
        self.status = "Off"
        self.score = 0.0
        self.last_check = 0.0
        self.match_rect = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="thorns-detector", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def snapshot(self):
        with self.lock:
            return self.matched, self.status, self.score, self.last_check, self.match_rect

    def allows_spam(self):
        if not self.app.config.get("buff_enabled", False):
            return True
        matched, _, _, checked, _ = self.snapshot()
        rate = max(1.0, float(self.app.config.get("thorns_checks_per_second", 10)))
        return matched and time.monotonic() - checked <= max(0.5, 3.0 / rate)

    def _set_state(self, matched, status, score=0.0, match_rect=None):
        with self.lock:
            changed = self.matched != bool(matched) or self.status != status
            self.matched = bool(matched)
            self.status = status
            self.score = float(score)
            self.last_check = time.monotonic()
            self.match_rect = match_rect if matched else None
        if changed and status in {"Active", "Missing"}:
            log(f"Thorns Buff: {status} | match={score:.3f}")

    def _loop(self):
        if cv2 is None or mss is None or np is None:
            self._set_state(False, "Missing vision dependencies")
            return
        template = cv2.imread(THORNS_TEMPLATE_PATH, cv2.IMREAD_COLOR)
        if template is None or template.size == 0:
            self._set_state(False, "Template missing")
            return
        template_h, template_w = template.shape[:2]
        try:
            with mss.mss() as sct:
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    enabled = bool(self.app.config.get("buff_enabled", False))
                    rate = max(1.0, min(100.0, float(self.app.config.get("thorns_checks_per_second", 10))))
                    threshold = max(0.5, min(0.9999, float(self.app.config.get("thorns_match_threshold", 0.85))))
                    if not enabled:
                        self._set_state(True, "Off", 1.0)
                    else:
                        context = get_active_dreamms_context()
                        if context is None:
                            self._set_state(False, "Game inactive")
                        else:
                            frame = VosMapDetector._capture(sct, context[1])
                            if frame.shape[0] < template_h or frame.shape[1] < template_w:
                                self._set_state(False, "Missing", 0.0)
                            else:
                                result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
                                _, score, _, location = cv2.minMaxLoc(result)
                                matched = float(score) >= threshold
                                rect = (
                                    int(location[0]),
                                    int(location[1]),
                                    int(template_w),
                                    int(template_h),
                                ) if matched else None
                                self._set_state(
                                    matched,
                                    "Active" if matched else "Missing",
                                    score,
                                    rect,
                                )
                    elapsed = time.monotonic() - started
                    self.stop_event.wait(max(0.0, (1.0 / rate) - elapsed))
        except Exception as exc:
            self._set_state(False, f"Detector error: {exc}")


class AlignmentOverlay:
    """Find the light bulb and target area and draw click-through guides."""

    TRANSPARENT_COLOR = "#ff00ff"

    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.hwnd = None
        self.client_bbox = None
        self.bulb_match = None
        self.area_match = None
        self.bulb_score = 0.0
        self.area_score = 0.0
        self.crystal_match = None
        self.crystal_score = 0.0
        self.crystal_generation = 0
        self.crystal_visible = False
        self.crystal_missing_frames = 0
        self.status = "Off"
        self.last_check = 0.0

        self.window = tk.Toplevel(app.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=self.TRANSPARENT_COLOR)
        try:
            self.window.wm_attributes("-transparentcolor", self.TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(
            self.window,
            bg=self.TRANSPARENT_COLOR,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.window.update_idletasks()
        self._make_click_through()

    def _make_click_through(self):
        try:
            overlay_hwnd = int(self.window.winfo_id())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(overlay_hwnd, -20)
            user32.SetWindowLongW(
                overlay_hwnd,
                -20,
                style | 0x20 | 0x80 | 0x08000000,
            )
            # Prevent the guide lines appearing in MSS captures on supported Windows versions.
            user32.SetWindowDisplayAffinity(overlay_hwnd, 0x00000011)
        except Exception as exc:
            log(f"Overlay click-through setup warning: {exc}")

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._detect_loop, name="alignment-detector", daemon=True)
        self.thread.start()
        self.app.root.after(50, self.render)

    def stop(self):
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def snapshot(self):
        with self.lock:
            return (
                self.status,
                self.client_bbox,
                self.bulb_match,
                self.area_match,
                self.bulb_score,
                self.area_score,
                self.last_check,
                self.crystal_match,
                self.crystal_score,
                self.crystal_generation,
            )

    def clear_crystal(self):
        with self.lock:
            self.crystal_match = None
            self.crystal_score = 0.0
            self.crystal_visible = False
            self.crystal_missing_frames = 0

    def _update_crystal(self, match, score, rate):
        with self.lock:
            if match is not None:
                is_new = not self.crystal_visible
                if self.crystal_match is not None:
                    old_x = self.crystal_match[0] + self.crystal_match[2] / 2.0
                    new_x = match[0] + match[2] / 2.0
                    is_new = is_new or abs(new_x - old_x) > 8
                if is_new:
                    self.crystal_generation += 1
                    log(
                        f"Crystal target detected: generation={self.crystal_generation}, "
                        f"x={match[0] + match[2] / 2.0:.1f}, match={score:.3f}"
                    )
                self.crystal_match = match
                self.crystal_score = float(score)
                self.crystal_visible = True
                self.crystal_missing_frames = 0
            else:
                self.crystal_score = float(score)
                self.crystal_missing_frames += 1
                if self.crystal_missing_frames >= max(2, int(rate * 0.5)):
                    if self.crystal_match is not None:
                        log("Crystal target cleared: crystal is no longer detected")
                    self.crystal_visible = False
                    self.crystal_match = None

    def _set_results(self, status, context=None, bulb=None, area=None, bulb_score=0.0, area_score=0.0):
        with self.lock:
            self.status = status
            self.hwnd = context[0] if context else None
            self.client_bbox = context[1] if context else None
            self.bulb_match = bulb
            self.area_match = area
            self.bulb_score = float(bulb_score)
            self.area_score = float(area_score)
            self.last_check = time.monotonic()

    @staticmethod
    def _best_match(frame, template, threshold):
        th, tw = template.shape[:2]
        if frame.shape[0] < th or frame.shape[1] < tw:
            return None, 0.0
        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        if float(score) < threshold:
            return None, float(score)
        x, y = location
        return (int(x), int(y), int(tw), int(th)), float(score)

    def _detect_loop(self):
        if cv2 is None or mss is None or np is None:
            self._set_results("Missing vision dependencies")
            return
        bulb = cv2.imread(LIGHTBULB_TEMPLATE_PATH, cv2.IMREAD_COLOR)
        area = cv2.imread(AREA_TEMPLATE_PATH, cv2.IMREAD_COLOR)
        crystal = cv2.imread(CRYSTAL_TEMPLATE_PATH, cv2.IMREAD_COLOR)
        if bulb is None or area is None or crystal is None:
            self._set_results("Overlay template missing")
            return
        try:
            with mss.mss() as sct:
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    enabled = bool(
                        self.app.config.get("alignment_overlay_enabled", False)
                        or self.app.config.get("auto_align_enabled", False)
                        or self.app.config.get("show_spammer_overlay", True)
                        or self.app.config.get("show_crystal", False)
                        or self.app.config.get("buff_enabled", False)
                    )
                    rate = max(1.0, min(60.0, float(self.app.config.get("alignment_checks_per_second", 10))))
                    threshold = max(0.5, min(0.9999, float(self.app.config.get("alignment_match_threshold", 0.85))))
                    if not enabled:
                        self._set_results("Off")
                    else:
                        context = get_active_dreamms_context()
                        if context is None:
                            self._set_results("Game inactive")
                        else:
                            frame = VosMapDetector._capture(sct, context[1])
                            bulb_match, bulb_score = self._best_match(frame, bulb, threshold)
                            area_match, area_score = self._best_match(frame, area, threshold)
                            if self.app.config.get("show_crystal", False):
                                crystal_match, crystal_score = self._best_match(frame, crystal, threshold)
                                self._update_crystal(crystal_match, crystal_score, rate)
                            else:
                                self.clear_crystal()
                            manual_target = self.app.config.get("alignment_target_mode", "area") == "manual"
                            if manual_target and bulb_match:
                                status = "Character + manual anchor"
                            elif manual_target:
                                status = "Light bulb not detected"
                            elif bulb_match and area_match:
                                status = "Both matched"
                            elif bulb_match:
                                status = "Area not detected"
                            elif area_match:
                                status = "Light bulb not detected"
                            else:
                                status = "No matches"
                            self._set_results(
                                status, context, bulb_match, area_match, bulb_score, area_score
                            )
                    elapsed = time.monotonic() - started
                    self.stop_event.wait(max(0.0, (1.0 / rate) - elapsed))
        except Exception as exc:
            self._set_results(f"Detector error: {exc}")

    def render(self):
        if self.stop_event.is_set():
            return
        status, bbox, bulb, area, _, _, _, crystal, _, _ = self.snapshot()
        guides_enabled = bool(self.app.config.get("alignment_overlay_enabled", False))
        show_spammer = bool(self.app.config.get("show_spammer_overlay", True))
        show_crystal = bool(self.app.config.get("show_crystal", False))
        thorns_matched, _, _, _, thorns_rect = self.app.thorns_detector.snapshot()
        show_thorns = bool(self.app.config.get("buff_enabled", False) and thorns_matched)
        if bbox is None or not (
            guides_enabled or show_spammer or (show_crystal and crystal is not None) or show_thorns
        ):
            self.window.withdraw()
        else:
            left, top, right, bottom = bbox
            width, height = right - left, bottom - top
            self.window.geometry(f"{width}x{height}+{left}+{top}")
            self.canvas.configure(width=width, height=height)
            self.canvas.delete("all")

            target_mode = self.app.config.get("alignment_target_mode", "area")
            target_marker = None
            target_label = "TARGET"
            if target_mode == "manual":
                offset = int(self.app.config.get("manual_anchor_offset", 0))
                center_x = max(0, min(width - 1, width // 2 + offset))
                target_marker = (center_x, height // 2)
                target_label = f"MANUAL TARGET {offset:+d}px"
            elif area is not None:
                x, y, w, h = area
                target_marker = (x + w // 2, y + h // 2)

            if guides_enabled and target_marker is not None:
                center_x, center_y = target_marker
                self.canvas.create_line(center_x, 0, center_x, height, fill="#39ff14", width=2, dash=(8, 5))
                self.canvas.create_oval(center_x - 5, center_y - 5, center_x + 5, center_y + 5, outline="#39ff14", width=2)
                self.canvas.create_text(center_x + 8, max(10, center_y - 10), text=target_label, fill="#39ff14", anchor="w")

            if guides_enabled and bulb is not None:
                x, y, w, h = bulb
                center_x = x + w // 2
                line_start = y + h
                self.canvas.create_line(center_x, line_start, center_x, height, fill="#00e5ff", width=2)
                self.canvas.create_oval(center_x - 5, y + h // 2 - 5, center_x + 5, y + h // 2 + 5, outline="#00e5ff", width=2)
                self.canvas.create_text(center_x + 8, line_start + 8, text="CHARACTER", fill="#00e5ff", anchor="nw")

            if show_crystal and crystal is not None:
                x, y, w, h = crystal
                center_x = x + w // 2
                center_y = y + h // 2
                self.canvas.create_line(center_x, 0, center_x, height, fill="#ff2020", width=3)
                self.canvas.create_oval(center_x - 6, center_y - 6, center_x + 6, center_y + 6, outline="#ff2020", width=3)
                self.canvas.create_text(center_x + 9, max(10, center_y - 12), text="CRYSTAL", fill="#ff2020", anchor="w")

            if show_thorns and thorns_rect is not None:
                x, y, w, h = thorns_rect
                padding = 3
                self.canvas.create_rectangle(
                    x - padding,
                    y - padding,
                    x + w + padding,
                    y + h + padding,
                    outline="#ffd400",
                    width=2,
                )

            if show_spammer:
                spam_on = bool(self.app.spam_active)
                waiting = spam_on and bool(self.app.spam_paused_reason)
                if waiting:
                    label = "SPAMMER WAITING"
                    color = "#ffc400"
                else:
                    label = "SPAMMER ON" if spam_on else "SPAMMER OFF"
                    color = "#21d921" if spam_on else "#ff3030"
                badge_width, badge_height = 185, 44
                x_percent = max(0, min(100, int(self.app.config.get("spam_overlay_x_percent", 85))))
                y_percent = max(0, min(100, int(self.app.config.get("spam_overlay_y_percent", 5))))
                center_x = round((width - 1) * x_percent / 100.0)
                center_y = round((height - 1) * y_percent / 100.0)
                half_w, half_h = badge_width // 2, badge_height // 2
                center_x = max(half_w + 4, min(width - half_w - 4, center_x))
                center_y = max(half_h + 4, min(height - half_h - 4, center_y))
                x1, x2 = center_x - half_w, center_x + half_w
                y1, y2 = center_y - half_h, center_y + half_h
                self.canvas.create_rectangle(x1, y1, x2, y2, fill="#101010", outline=color, width=3)
                self.canvas.create_text(
                    center_x,
                    center_y,
                    text=label,
                    fill=color,
                    font=("Segoe UI", 18, "bold"),
                )

            self.window.deiconify()
            self.window.lift()
        self.app.root.after(50, self.render)


class KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class InputUnion(ctypes.Union):
    _fields_ = [("ki", KeyboardInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", InputUnion)]


def send_windows_key(key, hold_seconds):
    extra = ctypes.c_ulong(0)
    vk = VK_KEYS[normalize_output_key(key)]
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for flags in (0, 0x0002):
        union = InputUnion()
        union.ki = KeyboardInput(vk, 0, flags, 0, ctypes.pointer(extra))
        item = Input(1, union)
        user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(item))
        if flags == 0 and hold_seconds > 0:
            time.sleep(hold_seconds)


def send_windows_direction(direction, hold_seconds):
    vk = 0x25 if str(direction).upper() == "LEFT" else 0x27
    extra = ctypes.c_ulong(0)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for flags in (0, 0x0002):
        union = InputUnion()
        union.ki = KeyboardInput(vk, 0, flags, 0, ctypes.pointer(extra))
        item = Input(1, union)
        user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(item))
        if flags == 0 and hold_seconds > 0:
            time.sleep(hold_seconds)


class VosApp:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.master_enabled = bool(self.config["enabled"])
        self.spam_active = False
        self.spam_paused_reason = None
        self.buff_resume_not_before = 0.0
        self.stop_event = threading.Event()
        self.worker = None
        self.send_count = 0
        self.last_window_active = None
        self.last_arduino_status = None
        self.hotkey_handles = []
        self.ui_actions = queue.SimpleQueue()
        self.map_detector = VosMapDetector(self)
        self.yeti_detector = YetiDetector(self)
        self.thorns_detector = ThornsDetector(self)
        self.alignment_overlay = None
        self.auto_align_stop = threading.Event()
        self.auto_align_thread = None
        self.auto_align_direction = "Idle"
        self.auto_align_delta = None
        self.auto_align_move_count = 0
        self._anchor_save_job = None
        self._overlay_position_save_job = None

        root.title("DreamMS VoS Helper")
        root.geometry("670x850")
        root.minsize(540, 480)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build_ui()
        self.alignment_overlay = AlignmentOverlay(self)
        self.map_detector.start()
        self.yeti_detector.start()
        self.thorns_detector.start()
        self.alignment_overlay.start()
        self.start_auto_align_controller()
        self.rebind_hotkeys()
        self.refresh_status()
        log("VoS helper started")

    def _build_ui(self):
        scroll_host = ttk.Frame(self.root)
        scroll_host.pack(fill="both", expand=True)
        self.settings_canvas = tk.Canvas(scroll_host, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(
            scroll_host,
            orient="vertical",
            command=self.settings_canvas.yview,
        )
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.settings_canvas.pack(side="left", fill="both", expand=True)

        outer = ttk.Frame(self.settings_canvas, padding=12)
        self.settings_window = self.settings_canvas.create_window(
            (0, 0),
            window=outer,
            anchor="nw",
        )
        outer.bind(
            "<Configure>",
            lambda _event: self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox("all")
            ),
        )
        self.settings_canvas.bind(
            "<Configure>",
            lambda event: self.settings_canvas.itemconfigure(
                self.settings_window,
                width=event.width,
            ),
        )
        self.root.bind("<MouseWheel>", self.on_mousewheel)

        status = ttk.LabelFrame(outer, text="Status", padding=10)
        status.pack(fill="x")
        ttk.Label(status, text="DreamMS window:").grid(row=0, column=0, sticky="w")
        self.window_status = ttk.Label(status, text="Checking...")
        self.window_status.grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(status, text="Master helper:").grid(row=1, column=0, sticky="w")
        self.master_status = ttk.Label(status)
        self.master_status.grid(row=1, column=1, sticky="w", padx=10)
        ttk.Label(status, text="VoS spam:").grid(row=2, column=0, sticky="w")
        self.vos_status = ttk.Label(status)
        self.vos_status.grid(row=2, column=1, sticky="w", padx=10)
        ttk.Label(status, text="Arduino:").grid(row=3, column=0, sticky="w")
        self.arduino_status = ttk.Label(status, text="Checking...")
        self.arduino_status.grid(row=3, column=1, sticky="w", padx=10)
        ttk.Label(status, text="VoS map checker:").grid(row=4, column=0, sticky="w")
        self.map_status = ttk.Label(status, text="Starting...")
        self.map_status.grid(row=4, column=1, sticky="w", padx=10)
        ttk.Label(status, text="Alignment overlay:").grid(row=5, column=0, sticky="w")
        self.alignment_status = ttk.Label(status, text="Off")
        self.alignment_status.grid(row=5, column=1, sticky="w", padx=10)
        ttk.Label(status, text="Auto alignment:").grid(row=6, column=0, sticky="w")
        self.auto_align_status = ttk.Label(status, text="Off")
        self.auto_align_status.grid(row=6, column=1, sticky="w", padx=10)
        ttk.Label(status, text="Yeti gate:").grid(row=7, column=0, sticky="w")
        self.yeti_status = ttk.Label(status, text="Off")
        self.yeti_status.grid(row=7, column=1, sticky="w", padx=10)
        ttk.Label(status, text="Thorns buff:").grid(row=8, column=0, sticky="w")
        self.thorns_status = ttk.Label(status, text="Off")
        self.thorns_status.grid(row=8, column=1, sticky="w", padx=10)

        vos = ttk.LabelFrame(outer, text="VoS Spam", padding=10)
        vos.pack(fill="x", pady=(10, 0))
        self.vos_enabled = tk.BooleanVar(value=bool(self.config["vos_enabled"]))
        ttk.Checkbutton(vos, text="Enable VoS function", variable=self.vos_enabled,
                        command=self.on_vos_enabled_changed).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(vos, text="Toggle hotkey:").grid(row=1, column=0, sticky="w", pady=5)
        self.vos_toggle_key = ttk.Entry(vos, width=14)
        self.vos_toggle_key.insert(0, self.config["vos_toggle_key"])
        self.vos_toggle_key.grid(row=1, column=1, sticky="w")
        ttk.Label(vos, text="Key to spam:").grid(row=2, column=0, sticky="w", pady=5)
        self.output_key = ttk.Combobox(vos, values=SPAM_KEYS, state="readonly", width=12)
        self.output_key.set(normalize_output_key(self.config["vos_output_key"]))
        self.output_key.grid(row=2, column=1, sticky="w")
        ttk.Label(vos, text="Key hold (seconds):").grid(row=3, column=0, sticky="w", pady=5)
        self.hold = ttk.Entry(vos, width=14)
        self.hold.insert(0, str(self.config["vos_hold"]))
        self.hold.grid(row=3, column=1, sticky="w")
        ttk.Label(vos, text="Repeat interval (seconds):").grid(row=4, column=0, sticky="w", pady=5)
        self.interval = ttk.Entry(vos, width=14)
        self.interval.insert(0, str(self.config["vos_interval"]))
        self.interval.grid(row=4, column=1, sticky="w")
        ttk.Button(vos, text="Toggle VoS now", command=self.toggle_vos).grid(row=5, column=0, pady=(8, 0), sticky="w")
        self.yeti_required = tk.BooleanVar(value=bool(self.config["yeti_required"]))
        ttk.Checkbutton(
            vos,
            text="Only spam while yeti.png is detected",
            variable=self.yeti_required,
            command=self.on_yeti_required_changed,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Label(vos, text="Yeti checks per second:").grid(row=7, column=0, sticky="w", pady=5)
        self.yeti_check_rate = ttk.Entry(vos, width=14)
        self.yeti_check_rate.insert(0, str(self.config["yeti_checks_per_second"]))
        self.yeti_check_rate.grid(row=7, column=1, sticky="w")
        ttk.Label(vos, text="Yeti match threshold:").grid(row=8, column=0, sticky="w", pady=5)
        self.yeti_threshold = ttk.Entry(vos, width=14)
        self.yeti_threshold.insert(0, str(self.config["yeti_match_threshold"]))
        self.yeti_threshold.grid(row=8, column=1, sticky="w")

        buff = ttk.LabelFrame(outer, text="Thorns Buff", padding=10)
        buff.pack(fill="x", pady=(10, 0))
        self.buff_enabled = tk.BooleanVar(value=bool(self.config["buff_enabled"]))
        ttk.Checkbutton(
            buff,
            text="Maintain Thorns before continuing spam",
            variable=self.buff_enabled,
            command=self.on_buff_enabled_changed,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(buff, text="Buff key:").grid(row=1, column=0, sticky="w", pady=5)
        self.buff_key = ttk.Combobox(buff, values=SPAM_KEYS, state="readonly", width=12)
        self.buff_key.set(normalize_output_key(self.config["buff_key"]))
        self.buff_key.grid(row=1, column=1, sticky="w")
        ttk.Label(buff, text="Buff key hold (seconds):").grid(row=2, column=0, sticky="w", pady=5)
        self.buff_hold = ttk.Entry(buff, width=14)
        self.buff_hold.insert(0, str(self.config["buff_key_hold"]))
        self.buff_hold.grid(row=2, column=1, sticky="w")
        ttk.Label(buff, text="Wait after buff (seconds):").grid(row=3, column=0, sticky="w", pady=5)
        self.buff_wait = ttk.Entry(buff, width=14)
        self.buff_wait.insert(0, str(self.config["buff_wait_seconds"]))
        self.buff_wait.grid(row=3, column=1, sticky="w")
        ttk.Label(buff, text="Checks per second:").grid(row=4, column=0, sticky="w", pady=5)
        self.thorns_check_rate = ttk.Entry(buff, width=14)
        self.thorns_check_rate.insert(0, str(self.config["thorns_checks_per_second"]))
        self.thorns_check_rate.grid(row=4, column=1, sticky="w")
        ttk.Label(buff, text="Thorns match threshold:").grid(row=5, column=0, sticky="w", pady=5)
        self.thorns_threshold = ttk.Entry(buff, width=14)
        self.thorns_threshold.insert(0, str(self.config["thorns_match_threshold"]))
        self.thorns_threshold.grid(row=5, column=1, sticky="w")

        checker = ttk.LabelFrame(outer, text="VoS Map Checker", padding=10)
        checker.pack(fill="x", pady=(10, 0))
        self.map_checker_enabled = tk.BooleanVar(value=bool(self.config["vos_map_checker_enabled"]))
        ttk.Checkbutton(
            checker,
            text="Stop VoS spam when VOS_map.png is not detected",
            variable=self.map_checker_enabled,
            command=self.on_map_checker_changed,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(checker, text="Checks per second:").grid(row=1, column=0, sticky="w", pady=5)
        self.map_check_rate = ttk.Entry(checker, width=14)
        self.map_check_rate.insert(0, str(self.config["vos_map_checks_per_second"]))
        self.map_check_rate.grid(row=1, column=1, sticky="w")
        ttk.Label(checker, text="Match threshold:").grid(row=2, column=0, sticky="w", pady=5)
        self.map_threshold = ttk.Entry(checker, width=14)
        self.map_threshold.insert(0, str(self.config["vos_map_match_threshold"]))
        self.map_threshold.grid(row=2, column=1, sticky="w")

        overlay = ttk.LabelFrame(outer, text="Character / Area Alignment Test", padding=10)
        overlay.pack(fill="x", pady=(10, 0))
        self.alignment_enabled = tk.BooleanVar(value=bool(self.config["alignment_overlay_enabled"]))
        ttk.Checkbutton(
            overlay,
            text="Show click-through alignment overlay",
            variable=self.alignment_enabled,
            command=self.on_alignment_changed,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(overlay, text="Checks per second:").grid(row=1, column=0, sticky="w", pady=5)
        self.alignment_rate = ttk.Entry(overlay, width=14)
        self.alignment_rate.insert(0, str(self.config["alignment_checks_per_second"]))
        self.alignment_rate.grid(row=1, column=1, sticky="w")
        ttk.Label(overlay, text="Match threshold:").grid(row=2, column=0, sticky="w", pady=5)
        self.alignment_threshold = ttk.Entry(overlay, width=14)
        self.alignment_threshold.insert(0, str(self.config["alignment_match_threshold"]))
        self.alignment_threshold.grid(row=2, column=1, sticky="w")
        self.auto_align_enabled = tk.BooleanVar(value=bool(self.config["auto_align_enabled"]))
        ttk.Checkbutton(
            overlay,
            text="Auto-align with LEFT / RIGHT while VoS spam is running",
            variable=self.auto_align_enabled,
            command=self.on_auto_align_changed,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Label(overlay, text="Happy tolerance (pixels):").grid(row=4, column=0, sticky="w", pady=5)
        self.auto_align_tolerance = ttk.Entry(overlay, width=14)
        self.auto_align_tolerance.insert(0, str(self.config["auto_align_tolerance_pixels"]))
        self.auto_align_tolerance.grid(row=4, column=1, sticky="w")
        ttk.Label(overlay, text="Movement key hold (seconds):").grid(row=5, column=0, sticky="w", pady=5)
        self.auto_align_hold = ttk.Entry(overlay, width=14)
        self.auto_align_hold.insert(0, str(self.config["auto_align_key_hold"]))
        self.auto_align_hold.grid(row=5, column=1, sticky="w")
        ttk.Label(overlay, text="Correction interval (seconds):").grid(row=6, column=0, sticky="w", pady=5)
        self.auto_align_interval = ttk.Entry(overlay, width=14)
        self.auto_align_interval.insert(0, str(self.config["auto_align_interval"]))
        self.auto_align_interval.grid(row=6, column=1, sticky="w")
        self.show_spammer_overlay = tk.BooleanVar(value=bool(self.config["show_spammer_overlay"]))
        ttk.Checkbutton(
            overlay,
            text="Show spam overlay (ON / OFF / WAITING)",
            variable=self.show_spammer_overlay,
            command=self.on_spammer_overlay_changed,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self.show_crystal = tk.BooleanVar(value=bool(self.config["show_crystal"]))
        ttk.Checkbutton(
            overlay,
            text="Show crystal (red vertical guide while detected)",
            variable=self.show_crystal,
            command=self.on_show_crystal_changed,
        ).grid(row=8, column=0, columnspan=2, sticky="w")
        self.loot_crystal = tk.BooleanVar(value=bool(self.config["loot_crystal"]))
        ttk.Checkbutton(
            overlay,
            text="Loot crystal (temporarily prioritize red line)",
            variable=self.loot_crystal,
            command=self.on_loot_crystal_changed,
        ).grid(row=9, column=0, columnspan=2, sticky="w")
        ttk.Separator(overlay, orient="horizontal").grid(
            row=10, column=0, columnspan=2, sticky="ew", pady=8
        )
        ttk.Label(overlay, text="Normal green target:").grid(row=11, column=0, sticky="w")
        self.alignment_target_mode = tk.StringVar(
            value=str(self.config.get("alignment_target_mode", "area"))
        )
        target_choices = ttk.Frame(overlay)
        target_choices.grid(row=11, column=1, sticky="w")
        ttk.Radiobutton(
            target_choices,
            text="Area image",
            value="area",
            variable=self.alignment_target_mode,
            command=self.on_alignment_target_changed,
        ).pack(side="left")
        ttk.Radiobutton(
            target_choices,
            text="Manual anchor",
            value="manual",
            variable=self.alignment_target_mode,
            command=self.on_alignment_target_changed,
        ).pack(side="left", padx=(8, 0))
        self.manual_anchor_offset = tk.IntVar(
            value=int(self.config.get("manual_anchor_offset", 0))
        )
        self.manual_anchor_label = ttk.Label(overlay, text="")
        self.manual_anchor_label.grid(row=12, column=0, sticky="w", pady=5)
        ttk.Scale(
            overlay,
            from_=-1000,
            to=1000,
            orient="horizontal",
            variable=self.manual_anchor_offset,
            command=self.on_manual_anchor_moved,
        ).grid(row=12, column=1, sticky="ew", pady=5)
        overlay.columnconfigure(1, weight=1)
        self.update_manual_anchor_label()
        self.spam_overlay_x = tk.IntVar(
            value=int(self.config.get("spam_overlay_x_percent", 85))
        )
        self.spam_overlay_y = tk.IntVar(
            value=int(self.config.get("spam_overlay_y_percent", 5))
        )
        self.spam_overlay_x_label = ttk.Label(overlay, text="")
        self.spam_overlay_x_label.grid(row=13, column=0, sticky="w", pady=5)
        ttk.Scale(
            overlay,
            from_=0,
            to=100,
            orient="horizontal",
            variable=self.spam_overlay_x,
            command=lambda value: self.on_spam_overlay_position_changed("x", value),
        ).grid(row=13, column=1, sticky="ew", pady=5)
        self.spam_overlay_y_label = ttk.Label(overlay, text="")
        self.spam_overlay_y_label.grid(row=14, column=0, sticky="w", pady=5)
        ttk.Scale(
            overlay,
            from_=0,
            to=100,
            orient="horizontal",
            variable=self.spam_overlay_y,
            command=lambda value: self.on_spam_overlay_position_changed("y", value),
        ).grid(row=14, column=1, sticky="ew", pady=5)
        self.update_spam_overlay_position_labels()

        settings = ttk.LabelFrame(outer, text="General / Arduino", padding=10)
        settings.pack(fill="x", pady=(10, 0))
        ttk.Label(settings, text="Master toggle hotkey:").grid(row=0, column=0, sticky="w")
        self.master_key = ttk.Entry(settings, width=14)
        self.master_key.insert(0, self.config["toggle_key"])
        self.master_key.grid(row=0, column=1, sticky="w")
        self.use_arduino = tk.BooleanVar(value=bool(self.config["use_arduino"]))
        ttk.Checkbutton(settings, text="Use Arduino HID", variable=self.use_arduino).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        self.auto_detect = tk.BooleanVar(value=bool(self.config["auto_detect_arduino"]))
        ttk.Checkbutton(settings, text="Auto-detect Arduino", variable=self.auto_detect).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(settings, text="Serial port:").grid(row=3, column=0, sticky="w", pady=5)
        self.serial_port = ttk.Entry(settings, width=14)
        self.serial_port.insert(0, self.config["serial_port"])
        self.serial_port.grid(row=3, column=1, sticky="w")
        ttk.Label(settings, text="Baud rate:").grid(row=4, column=0, sticky="w", pady=5)
        self.baud_rate = ttk.Entry(settings, width=14)
        self.baud_rate.insert(0, str(self.config["baud_rate"]))
        self.baud_rate.grid(row=4, column=1, sticky="w")
        ttk.Button(settings, text="Save settings", command=self.save).grid(row=5, column=0, pady=(8, 0), sticky="w")
        ttk.Button(settings, text="Master On / Off", command=self.toggle_master).grid(row=5, column=1, pady=(8, 0), sticky="w")

        logs = ttk.LabelFrame(outer, text="Recent logs", padding=6)
        logs.pack(fill="both", expand=True, pady=(10, 0))
        self.log_view = tk.Text(logs, height=8, state="disabled", wrap="word")
        self.log_view.pack(fill="both", expand=True)

    def on_mousewheel(self, event):
        if not hasattr(self, "settings_canvas"):
            return
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.settings_canvas.yview_scroll(-1 if delta > 0 else 1, "units")

    def read_form(self):
        hold = float(self.hold.get())
        interval = float(self.interval.get())
        baud = int(self.baud_rate.get())
        check_rate = float(self.map_check_rate.get())
        threshold = float(self.map_threshold.get())
        alignment_rate = float(self.alignment_rate.get())
        alignment_threshold = float(self.alignment_threshold.get())
        auto_align_tolerance = int(self.auto_align_tolerance.get())
        auto_align_hold = float(self.auto_align_hold.get())
        auto_align_interval = float(self.auto_align_interval.get())
        yeti_rate = float(self.yeti_check_rate.get())
        yeti_threshold = float(self.yeti_threshold.get())
        buff_hold = float(self.buff_hold.get())
        buff_wait = float(self.buff_wait.get())
        thorns_rate = float(self.thorns_check_rate.get())
        thorns_threshold = float(self.thorns_threshold.get())
        if hold < 0 or interval <= 0 or baud <= 0:
            raise ValueError("Hold must be >= 0; interval and baud rate must be > 0.")
        if not 1 <= check_rate <= 100:
            raise ValueError("Map checks per second must be between 1 and 100.")
        if not 0.5 <= threshold <= 0.9999:
            raise ValueError("Map match threshold must be between 0.5 and 0.9999.")
        if not 1 <= alignment_rate <= 60:
            raise ValueError("Alignment checks per second must be between 1 and 60.")
        if not 0.5 <= alignment_threshold <= 0.9999:
            raise ValueError("Alignment match threshold must be between 0.5 and 0.9999.")
        if not 0 <= auto_align_tolerance <= 500:
            raise ValueError("Auto-align tolerance must be between 0 and 500 pixels.")
        if not 0.001 <= auto_align_hold <= 1.0:
            raise ValueError("Movement key hold must be between 0.001 and 1 second.")
        if not 0.02 <= auto_align_interval <= 5.0:
            raise ValueError("Correction interval must be between 0.02 and 5 seconds.")
        if not 1 <= yeti_rate <= 100:
            raise ValueError("Yeti checks per second must be between 1 and 100.")
        if not 0.5 <= yeti_threshold <= 0.9999:
            raise ValueError("Yeti match threshold must be between 0.5 and 0.9999.")
        if not 0.001 <= buff_hold <= 1.0:
            raise ValueError("Buff key hold must be between 0.001 and 1 second.")
        if not 0 <= buff_wait <= 60:
            raise ValueError("Buff wait must be between 0 and 60 seconds.")
        if not 1 <= thorns_rate <= 100:
            raise ValueError("Thorns checks per second must be between 1 and 100.")
        if not 0.5 <= thorns_threshold <= 0.9999:
            raise ValueError("Thorns match threshold must be between 0.5 and 0.9999.")
        master_key = self.master_key.get().strip() or "F11"
        vos_key = self.vos_toggle_key.get().strip() or "F10"
        if master_key.lower() == vos_key.lower():
            raise ValueError("Master and VoS toggle hotkeys must be different.")
        self.config.update({
            "enabled": self.master_enabled,
            "toggle_key": master_key,
            "vos_enabled": bool(self.vos_enabled.get()),
            "vos_toggle_key": vos_key,
            "vos_output_key": normalize_output_key(self.output_key.get()),
            "vos_hold": hold,
            "vos_interval": interval,
            "vos_map_checker_enabled": bool(self.map_checker_enabled.get()),
            "vos_map_checks_per_second": check_rate,
            "vos_map_match_threshold": threshold,
            "alignment_overlay_enabled": bool(self.alignment_enabled.get()),
            "alignment_checks_per_second": alignment_rate,
            "alignment_match_threshold": alignment_threshold,
            "auto_align_enabled": bool(self.auto_align_enabled.get()),
            "auto_align_tolerance_pixels": auto_align_tolerance,
            "auto_align_key_hold": auto_align_hold,
            "auto_align_interval": auto_align_interval,
            "show_spammer_overlay": bool(self.show_spammer_overlay.get()),
            "spam_overlay_x_percent": int(self.spam_overlay_x.get()),
            "spam_overlay_y_percent": int(self.spam_overlay_y.get()),
            "show_crystal": bool(self.show_crystal.get()),
            "loot_crystal": bool(self.loot_crystal.get()),
            "alignment_target_mode": self.alignment_target_mode.get(),
            "manual_anchor_offset": int(self.manual_anchor_offset.get()),
            "yeti_required": bool(self.yeti_required.get()),
            "yeti_checks_per_second": yeti_rate,
            "yeti_match_threshold": yeti_threshold,
            "buff_enabled": bool(self.buff_enabled.get()),
            "buff_key": normalize_output_key(self.buff_key.get()),
            "buff_key_hold": buff_hold,
            "buff_wait_seconds": buff_wait,
            "thorns_checks_per_second": thorns_rate,
            "thorns_match_threshold": thorns_threshold,
            "use_arduino": bool(self.use_arduino.get()),
            "auto_detect_arduino": bool(self.auto_detect.get()),
            "serial_port": self.serial_port.get().strip() or "COM3",
            "baud_rate": baud,
        })

    def save(self):
        try:
            self.read_form()
            save_config(self.config)
            ARDUINO.close()
            self.rebind_hotkeys()
            log("Settings saved")
            messagebox.showinfo("Saved", "VoS settings saved.")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Invalid settings", str(exc))

    def rebind_hotkeys(self):
        if keyboard is None:
            log("The keyboard package is missing; global hotkeys are unavailable")
            return

        # Remove only hooks owned by this app. Some releases of the keyboard
        # package raise from unhook_all_hotkeys() before any hook exists, which
        # previously prevented both of our hotkeys from ever being registered.
        for handle in self.hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
        self.hotkey_handles.clear()

        try:
            self.hotkey_handles.append(
                keyboard.add_hotkey(
                    str(self.config["toggle_key"]),
                    lambda: self.ui_actions.put("toggle_master"),
                    suppress=False,
                )
            )
            self.hotkey_handles.append(
                keyboard.add_hotkey(
                    str(self.config["vos_toggle_key"]),
                    lambda: self.ui_actions.put("toggle_vos"),
                    suppress=False,
                )
            )
            log(f"Hotkeys bound: master={self.config['toggle_key']}, VoS={self.config['vos_toggle_key']}")
        except Exception as exc:
            for handle in self.hotkey_handles:
                try:
                    keyboard.remove_hotkey(handle)
                except Exception:
                    pass
            self.hotkey_handles.clear()
            log(f"Hotkey binding failed: {exc}")

    def process_ui_actions(self):
        """Run keyboard-hook requests on Tk's main thread."""
        while True:
            try:
                action = self.ui_actions.get_nowait()
            except queue.Empty:
                return
            if action == "toggle_master":
                self.toggle_master()
            elif action == "toggle_vos":
                self.toggle_vos()

    def on_vos_enabled_changed(self):
        self.config["vos_enabled"] = bool(self.vos_enabled.get())
        if not self.config["vos_enabled"]:
            self.stop_vos("function disabled")
        save_config(self.config)

    def on_yeti_required_changed(self):
        enabled = bool(self.yeti_required.get())
        self.config["yeti_required"] = enabled
        if not enabled:
            self.spam_paused_reason = None
        save_config(self.config)
        log(f"Yeti-required spam gate {'enabled' if enabled else 'disabled'}")

    def on_buff_enabled_changed(self):
        enabled = bool(self.buff_enabled.get())
        self.config["buff_enabled"] = enabled
        self.buff_resume_not_before = 0.0
        if not enabled and self.spam_paused_reason and (
            self.spam_paused_reason == "Casting Thorns"
            or self.spam_paused_reason.startswith("Waiting for Thorns")
        ):
            self.spam_paused_reason = None
        save_config(self.config)
        log(f"Thorns buff maintenance {'enabled' if enabled else 'disabled'}")

    def on_map_checker_changed(self):
        enabled = bool(self.map_checker_enabled.get())
        self.config["vos_map_checker_enabled"] = enabled
        if enabled and not self.map_detector.allows_spam():
            self.stop_vos("VoS map checker enabled without a match")
        save_config(self.config)
        log(f"VoS Map Checker {'enabled' if enabled else 'disabled'}")

    def on_alignment_changed(self):
        enabled = bool(self.alignment_enabled.get())
        self.config["alignment_overlay_enabled"] = enabled
        save_config(self.config)
        log(f"Alignment overlay {'enabled' if enabled else 'disabled'}")

    def on_auto_align_changed(self):
        enabled = bool(self.auto_align_enabled.get())
        self.config["auto_align_enabled"] = enabled
        if not enabled:
            self.auto_align_direction = "Off"
            self.auto_align_delta = None
        save_config(self.config)
        log(f"Auto alignment {'enabled' if enabled else 'disabled'}")

    def on_spammer_overlay_changed(self):
        enabled = bool(self.show_spammer_overlay.get())
        self.config["show_spammer_overlay"] = enabled
        save_config(self.config)
        log(f"In-game spammer badge {'enabled' if enabled else 'disabled'}")

    def update_spam_overlay_position_labels(self):
        if hasattr(self, "spam_overlay_x_label"):
            self.spam_overlay_x_label.configure(
                text=f"Badge horizontal: {int(self.spam_overlay_x.get())}%"
            )
        if hasattr(self, "spam_overlay_y_label"):
            self.spam_overlay_y_label.configure(
                text=f"Badge vertical: {int(self.spam_overlay_y.get())}%"
            )

    def on_spam_overlay_position_changed(self, axis, value):
        position = max(0, min(100, int(round(float(value)))))
        if axis == "x":
            self.spam_overlay_x.set(position)
            self.config["spam_overlay_x_percent"] = position
        else:
            self.spam_overlay_y.set(position)
            self.config["spam_overlay_y_percent"] = position
        self.update_spam_overlay_position_labels()
        if self._overlay_position_save_job is not None:
            self.root.after_cancel(self._overlay_position_save_job)
        self._overlay_position_save_job = self.root.after(
            300,
            self.save_spam_overlay_position,
        )

    def save_spam_overlay_position(self):
        self._overlay_position_save_job = None
        save_config(self.config)

    def on_show_crystal_changed(self):
        enabled = bool(self.show_crystal.get())
        self.config["show_crystal"] = enabled
        if not enabled and self.alignment_overlay is not None:
            self.alignment_overlay.clear_crystal()
        save_config(self.config)
        log(f"Crystal guide {'enabled' if enabled else 'disabled'}")

    def on_loot_crystal_changed(self):
        enabled = bool(self.loot_crystal.get())
        self.config["loot_crystal"] = enabled
        if enabled:
            self.show_crystal.set(True)
            self.config["show_crystal"] = True
        save_config(self.config)
        log(f"Loot crystal priority {'enabled' if enabled else 'disabled'}")

    def on_alignment_target_changed(self):
        mode = self.alignment_target_mode.get()
        self.config["alignment_target_mode"] = "manual" if mode == "manual" else "area"
        save_config(self.config)
        log(
            "Normal alignment target changed to "
            + ("manual screen anchor" if mode == "manual" else "area.png")
        )

    def update_manual_anchor_label(self):
        offset = int(self.manual_anchor_offset.get())
        self.manual_anchor_label.configure(text=f"Anchor offset: {offset:+d}px (0 = center)")

    def on_manual_anchor_moved(self, value):
        offset = int(round(float(value)))
        self.manual_anchor_offset.set(offset)
        self.config["manual_anchor_offset"] = offset
        self.update_manual_anchor_label()
        if self._anchor_save_job is not None:
            self.root.after_cancel(self._anchor_save_job)
        self._anchor_save_job = self.root.after(300, self.save_manual_anchor)

    def save_manual_anchor(self):
        self._anchor_save_job = None
        save_config(self.config)

    def start_auto_align_controller(self):
        if self.auto_align_thread is not None and self.auto_align_thread.is_alive():
            return
        self.auto_align_stop.clear()
        self.auto_align_thread = threading.Thread(
            target=self._auto_align_loop,
            name="auto-align-controller",
            daemon=True,
        )
        self.auto_align_thread.start()

    def _auto_align_loop(self):
        while not self.auto_align_stop.is_set():
            interval = max(0.02, float(self.config.get("auto_align_interval", 0.10)))
            if not self.config.get("auto_align_enabled", False):
                self.auto_align_direction = "Off"
                self.auto_align_delta = None
                self.auto_align_stop.wait(0.10)
                continue
            if not self.spam_active:
                self.auto_align_direction = "Waiting for VoS"
                self.auto_align_delta = None
                self.auto_align_stop.wait(0.10)
                continue
            # The Yeti gate controls only skill spam. Movement alignment should
            # continue while waiting for a Yeti. Other maintenance pauses,
            # such as casting/recovering Thorns, still pause movement.
            if self.spam_paused_reason and self.spam_paused_reason != "Waiting for yeti":
                self.auto_align_direction = self.spam_paused_reason
                self.auto_align_delta = None
                self.auto_align_stop.wait(0.10)
                continue

            (
                _,
                client_bbox,
                bulb,
                area,
                _,
                _,
                checked,
                crystal,
                _,
                _,
            ) = self.alignment_overlay.snapshot()
            detector_rate = max(1.0, float(self.config.get("alignment_checks_per_second", 10)))
            if (
                bulb is None
                or time.monotonic() - checked > max(0.5, 3.0 / detector_rate)
            ):
                self.auto_align_direction = "Waiting for character marker"
                self.auto_align_delta = None
                self.auto_align_stop.wait(interval)
                continue

            loot_pending = bool(
                self.config.get("loot_crystal", False) and crystal is not None
            )
            character_x = bulb[0] + bulb[2] / 2.0
            if loot_pending:
                target_name = "Crystal"
                target_x = crystal[0] + crystal[2] / 2.0
            elif self.config.get("alignment_target_mode", "area") == "manual":
                target_name = "Manual anchor"
                client_width = client_bbox[2] - client_bbox[0]
                offset = int(self.config.get("manual_anchor_offset", 0))
                target_x = max(0, min(client_width - 1, client_width / 2.0 + offset))
            elif area is not None:
                target_name = "Area"
                target_x = area[0] + area[2] / 2.0
            else:
                self.auto_align_direction = "Waiting for area marker"
                self.auto_align_delta = None
                self.auto_align_stop.wait(interval)
                continue

            delta = character_x - target_x
            tolerance = max(0, int(self.config.get("auto_align_tolerance_pixels", 8)))
            self.auto_align_delta = delta
            if abs(delta) <= tolerance:
                if loot_pending:
                    self.auto_align_direction = "Crystal reached; waiting for pickup"
                else:
                    self.auto_align_direction = "Aligned"
                self.auto_align_stop.wait(interval)
                continue

            direction = "RIGHT" if delta < 0 else "LEFT"
            hold = max(0.001, float(self.config.get("auto_align_key_hold", 0.03)))
            if self.config.get("use_arduino", True):
                sent = ARDUINO.send_command(self.config, direction, hold)
            else:
                send_windows_direction(direction, hold)
                sent = True

            if sent:
                self.auto_align_direction = f"{target_name} {direction}"
                self.auto_align_move_count += 1
            else:
                self.auto_align_direction = "Arduino send failed"
            self.auto_align_stop.wait(interval)

    def toggle_master(self):
        self.master_enabled = not self.master_enabled
        self.config["enabled"] = self.master_enabled
        if not self.master_enabled:
            self.stop_vos("master helper disabled")
        save_config(self.config)
        log(f"Master helper {'ON' if self.master_enabled else 'OFF'}")

    def toggle_vos(self):
        if self.spam_active:
            self.stop_vos("toggle pressed")
            return
        if not self.master_enabled:
            log("VoS did not start: master helper is OFF")
            return
        if not self.config.get("vos_enabled", True):
            log("VoS did not start: function is disabled")
            return
        if not is_dreamms_active():
            log("VoS did not start: DreamMS.exe is not the active window")
            return
        if not self.map_detector.allows_spam():
            _, status, score, _ = self.map_detector.snapshot()
            log(f"VoS did not start: map check blocked ({status}, match={score:.3f})")
            return
        self.stop_event.clear()
        self.send_count = 0
        self.spam_paused_reason = None
        self.buff_resume_not_before = 0.0
        self.spam_active = True
        self.worker = threading.Thread(target=self._spam_loop, name="vos-spam", daemon=True)
        self.worker.start()
        log(f"VoS spam ON ({self.config['vos_output_key']})")

    def stop_vos(self, reason="stopped"):
        was_active = self.spam_active
        self.stop_event.set()
        self.spam_active = False
        self.spam_paused_reason = None
        self.buff_resume_not_before = 0.0
        if was_active:
            log(f"VoS spam OFF ({reason})")

    def _spam_loop(self):
        config = dict(self.config)
        hold = max(0.0, float(config["vos_hold"]))
        interval = max(0.001, float(config["vos_interval"]))
        key = normalize_output_key(config["vos_output_key"])
        while not self.stop_event.is_set():
            if not is_dreamms_active():
                self.stop_vos("DreamMS window lost focus")
                return
            if not self.map_detector.allows_spam():
                self.stop_vos("VoS map not detected")
                return
            if self.config.get("buff_enabled", False):
                now = time.monotonic()
                if now < self.buff_resume_not_before:
                    remaining = self.buff_resume_not_before - now
                    self.spam_paused_reason = f"Waiting for Thorns ({remaining:.1f}s)"
                    if self.stop_event.wait(min(0.05, remaining)):
                        break
                    continue
                if not self.thorns_detector.allows_spam():
                    self.spam_paused_reason = "Casting Thorns"
                    buff_key = normalize_output_key(self.config.get("buff_key", "END"))
                    buff_hold = max(0.001, float(self.config.get("buff_key_hold", 0.03)))
                    if self.config.get("use_arduino", True):
                        sent = ARDUINO.send_key(self.config, buff_key, buff_hold)
                    else:
                        send_windows_key(buff_key, buff_hold)
                        sent = True
                    if not sent:
                        self.stop_vos("Thorns buff key send failed")
                        return
                    wait_seconds = max(0.0, float(self.config.get("buff_wait_seconds", 5.0)))
                    self.buff_resume_not_before = time.monotonic() + wait_seconds
                    self.spam_paused_reason = f"Waiting for Thorns ({wait_seconds:.1f}s)"
                    log(f"Thorns missing: sent {buff_key}, waiting {wait_seconds:.1f}s")
                    if self.stop_event.wait(min(0.05, max(0.001, wait_seconds))):
                        break
                    continue
            if not self.yeti_detector.allows_spam():
                self.spam_paused_reason = "Waiting for yeti"
                if self.stop_event.wait(0.05):
                    break
                continue
            self.spam_paused_reason = None
            if config.get("use_arduino", True):
                if not ARDUINO.send_key(config, key, hold):
                    log("VoS stopped: Arduino send failed")
                    self.stop_vos("Arduino disconnected")
                    return
            else:
                send_windows_key(key, hold)
            self.send_count += 1
            if self.stop_event.wait(interval):
                break

    def refresh_status(self):
        self.process_ui_actions()
        active = is_dreamms_active()
        self.window_status.configure(text="Active" if active else "Inactive", foreground="green" if active else "red")
        self.master_status.configure(text="On" if self.master_enabled else "Off", foreground="green" if self.master_enabled else "red")
        if self.spam_active and self.spam_paused_reason:
            vos_text, vos_color = f"Waiting for Yeti ({self.send_count} sent)", "orange"
        elif self.spam_active:
            vos_text, vos_color = f"Running ({self.send_count} sent)", "green"
        else:
            vos_text, vos_color = "Off", "red"
        self.vos_status.configure(text=vos_text, foreground=vos_color)
        map_matched, map_text, map_score, _ = self.map_detector.snapshot()
        if not self.config.get("vos_map_checker_enabled", True):
            map_color = "gray"
        else:
            map_color = "green" if map_matched else "red"
        score_suffix = f" ({map_score:.2f})" if map_text in {"Matched", "Not detected"} else ""
        self.map_status.configure(text=map_text + score_suffix, foreground=map_color)
        if self.alignment_overlay is not None:
            (
                overlay_text,
                _,
                bulb,
                area,
                bulb_score,
                area_score,
                _,
                crystal,
                crystal_score,
                _,
            ) = self.alignment_overlay.snapshot()
            if not self.config.get("alignment_overlay_enabled", False):
                overlay_color = "gray"
            else:
                manual_target = self.config.get("alignment_target_mode", "area") == "manual"
                target_ready = manual_target or area is not None
                overlay_color = "green" if bulb is not None and target_ready else "orange"
            detail = ""
            if self.config.get("alignment_overlay_enabled", False):
                if self.config.get("alignment_target_mode", "area") == "manual":
                    offset = int(self.config.get("manual_anchor_offset", 0))
                    detail = f" (bulb {bulb_score:.2f}, anchor {offset:+d}px)"
                else:
                    detail = f" (bulb {bulb_score:.2f}, area {area_score:.2f})"
            if self.config.get("show_crystal", False):
                crystal_text = f", crystal {crystal_score:.2f}" if crystal is not None else ", crystal waiting"
                detail = (detail[:-1] + crystal_text + ")") if detail else f" ({crystal_text[2:]})"
            self.alignment_status.configure(text=overlay_text + detail, foreground=overlay_color)
        if not self.config.get("auto_align_enabled", False):
            auto_text, auto_color = "Off", "gray"
        else:
            delta_text = "" if self.auto_align_delta is None else f", delta {self.auto_align_delta:+.1f}px"
            auto_text = f"{self.auto_align_direction}{delta_text} ({self.auto_align_move_count} moves)"
            auto_color = "green" if self.auto_align_direction == "Aligned" else "orange"
        self.auto_align_status.configure(text=auto_text, foreground=auto_color)
        yeti_matched, yeti_text, yeti_score, _ = self.yeti_detector.snapshot()
        if not self.config.get("yeti_required", False):
            yeti_color = "gray"
        else:
            yeti_color = "green" if yeti_matched else "orange"
        yeti_suffix = f" ({yeti_score:.2f})" if yeti_text in {"Detected", "Not detected"} else ""
        self.yeti_status.configure(text=yeti_text + yeti_suffix, foreground=yeti_color)
        thorns_matched, thorns_text, thorns_score, _, _ = self.thorns_detector.snapshot()
        if not self.config.get("buff_enabled", False):
            thorns_color = "gray"
        else:
            thorns_color = "green" if thorns_matched else "orange"
        thorns_suffix = f" ({thorns_score:.2f})" if thorns_text in {"Active", "Missing"} else ""
        self.thorns_status.configure(
            text=thorns_text + thorns_suffix,
            foreground=thorns_color,
        )

        if not self.config.get("use_arduino", True):
            arduino_text, color = "Off (Windows fallback)", "gray"
        elif serial is None:
            arduino_text, color = "Missing pyserial", "red"
        elif ARDUINO.connected(self.config):
            arduino_text, color = f"Connected ({ARDUINO._port})", "green"
        else:
            arduino_text, color = "Disconnected", "red"
        self.arduino_status.configure(text=arduino_text, foreground=color)

        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", tk.END)
        self.log_view.insert(tk.END, "\n".join(LOG_BUFFER))
        self.log_view.see(tk.END)
        self.log_view.configure(state="disabled")
        self.root.after(500, self.refresh_status)

    def on_close(self):
        self.stop_vos("application closing")
        self.map_detector.stop()
        self.yeti_detector.stop()
        self.thorns_detector.stop()
        self.auto_align_stop.set()
        if self.auto_align_thread is not None and self.auto_align_thread.is_alive():
            self.auto_align_thread.join(timeout=1.0)
        if self.alignment_overlay is not None:
            self.alignment_overlay.stop()
        ARDUINO.close()
        if keyboard is not None:
            for handle in self.hotkey_handles:
                try:
                    keyboard.remove_hotkey(handle)
                except Exception:
                    pass
            self.hotkey_handles.clear()
        self.root.destroy()


def main():
    root = tk.Tk()
    VosApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
