import ctypes
import json
import os
import sys
import queue
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

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

try:
    import mss
except Exception:
    mss = None

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LOG_PATH = os.path.join(os.path.dirname(__file__), "recent_logs.txt")
TRIGGER_COOLDOWN_SECONDS = 0.0
LAST_TRIGGER_TIME = 0.0
COMBAT_KEY_DOWN = False
ATTACK_KEY_DOWN = False
ATTACK_STOP_EVENT = None
ATTACK_THREAD = None

# Combat uses ONE permanent worker instead of creating a new thread per tap.
# Every accepted V tap is queued FIFO and runs its complete sequence.
COMBAT_QUEUE = queue.Queue()
COMBAT_THREAD = None
COMBAT_WORKER_START_LOCK = threading.Lock()

# Combo Drain is completely independent from Combat and Attack.
# One accepted trigger tap queues one complete DOWN -> DOWN -> CTRL sequence.
COMBO_DRAIN_KEY_DOWN = False
COMBO_DRAIN_QUEUE = queue.Queue()
COMBO_DRAIN_THREAD = None
COMBO_DRAIN_WORKER_START_LOCK = threading.Lock()

# Fenrir is independent from Combat, Attack, and Combo Drain.
FENRIR_KEY_DOWN = False
FENRIR_QUEUE = queue.Queue()
FENRIR_THREAD = None
FENRIR_WORKER_START_LOCK = threading.Lock()
FENRIR_ACTIVE = False
FENRIR_STATE_LOCK = threading.Lock()

# After Fenrir finishes, an Arduino-held LEFT/RIGHT can be used to reassert
# whichever horizontal direction the user is still physically holding.
FENRIR_RESTORE_LOCK = threading.Lock()
FENRIR_RESTORE_DIRECTION = None
FENRIR_RESTORE_THREAD = None
FENRIR_RESTORE_STOP = threading.Event()

# Chat Protection is a neutral safety gate shared by all helper functions.
# The helper is allowed only while assets/chatbox.png is visibly matched.
CHAT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "assets", "chatbox.png")
CHAT_STATE_LOCK = threading.Lock()
CHAT_SAFE_STATE = False
CHAT_STATUS = "Starting"
CHAT_LAST_SCORE = 0.0
CHAT_LAST_CHECK_TIME = 0.0
CHAT_CACHED_LOCATION = None  # client-relative (x, y)
CHAT_CACHED_CLIENT_BBOX = None
CHAT_TEMPLATE = None
CHAT_TEMPLATE_MTIME = None
CHAT_DETECTOR_THREAD = None
CHAT_DETECTOR_STOP = threading.Event()
CHAT_DETECTOR_START_LOCK = threading.Lock()

LOG_BUFFER = deque([], maxlen=300)
LOG_WIDGET = None

DEFAULTS = {
    "enabled": True,
    "toggle_key": "F12",
    "combat_enabled": True,
    "attack_enabled": True,
    "combo_drain_enabled": True,
    "fenrir_enabled": True,
    "combat_key": "v",
    "combat_hold": 0.01,
    "attack_key": "space",
    "attack_hold": 0.01,
    "combo_drain_key": "x",
    "combo_drain_hold": 0.01,  # legacy fallback
    "combo_drain_down_hold": 0.01,
    "combo_drain_ctrl_hold": 0.01,
    "combo_drain_initial_delay": 0.05,
    "combo_drain_delay_1": 0.05,
    "combo_drain_delay_2": 0.05,
    "fenrir_key": "c",
    "fenrir_suspend_inputs": True,
    "fenrir_down_hold": 0.01,
    "fenrir_direction_hold": 0.01,
    "fenrir_ctrl_hold": 0.01,
    "fenrir_initial_delay": 0.05,
    "fenrir_delay_1": 0.05,
    "fenrir_delay_2": 0.05,

    "chat_protection_enabled": True,
    "chat_checks_per_second": 50,
    "chat_match_threshold": 0.97,

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

    # Do not touch Tkinter widgets from worker threads.
    # refresh_recent_logs() performs the GUI refresh safely on Tk's main thread.

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


def send_virtual_key_state(vk, pressed):
    """Send only key-down or key-up. Used by Fenrir movement restoration fallback."""
    extra = ctypes.c_ulong(0)
    ii_ = INPUT_I()
    flags = 0 if pressed else KEYEVENTF_KEYUP
    ki = KEYBDINPUT(
        wVk=vk,
        wScan=0,
        dwFlags=flags,
        time=0,
        dwExtraInfo=ctypes.pointer(extra),
    )
    ii_.ki = ki
    x = INPUT(type=INPUT_KEYBOARD, ii=ii_)
    USER32.SendInput(1, ctypes.byref(x), ctypes.sizeof(x))


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
    """Local fallback ALT double-tap. Trigger delay is handled by combat_trigger()."""
    hold = max(0.0, float(cfg.get("combat_hold", 0.01)))
    double_tap_delay = max(0.0, float(cfg.get("double_tap_delay", 0.02)))

    log(f"Combat: first ALT tap hold={hold:.4f}s")
    send_virtual_key(VK_MENU, hold)

    if double_tap_delay > 0:
        log(f"Combat: waiting {double_tap_delay:.4f}s before second ALT tap")
        time.sleep(double_tap_delay)

    log(f"Combat: second ALT tap hold={hold:.4f}s")
    send_virtual_key(VK_MENU, hold)
    log("Combat: ALT double-tap complete")



def _set_chat_state(safe, status, score=0.0, cached_location=None, client_bbox=None):
    """Update Chat Protection state atomically."""
    global CHAT_SAFE_STATE, CHAT_STATUS, CHAT_LAST_SCORE
    global CHAT_LAST_CHECK_TIME, CHAT_CACHED_LOCATION, CHAT_CACHED_CLIENT_BBOX

    with CHAT_STATE_LOCK:
        previous_safe = CHAT_SAFE_STATE
        previous_status = CHAT_STATUS

        CHAT_SAFE_STATE = bool(safe)
        CHAT_STATUS = str(status)
        CHAT_LAST_SCORE = float(score)
        CHAT_LAST_CHECK_TIME = time.monotonic()

        if cached_location is not None:
            CHAT_CACHED_LOCATION = tuple(cached_location)
        if client_bbox is not None:
            CHAT_CACHED_CLIENT_BBOX = tuple(client_bbox)

    # Log state changes only; never log every frame.
    if previous_safe != bool(safe) or previous_status != str(status):
        if status in {"Safe", "Blocked"}:
            log(f"Chat Protection: {status} | match={float(score):.3f}")


def _get_chat_state_snapshot():
    with CHAT_STATE_LOCK:
        return {
            "safe": CHAT_SAFE_STATE,
            "status": CHAT_STATUS,
            "score": CHAT_LAST_SCORE,
            "last_check": CHAT_LAST_CHECK_TIME,
            "cached_location": CHAT_CACHED_LOCATION,
            "client_bbox": CHAT_CACHED_CLIENT_BBOX,
        }


def chat_protection_allows(cfg):
    """Fail-safe gate: require a fresh safe result while protection is enabled."""
    if not cfg.get("chat_protection_enabled", True):
        return True

    state = _get_chat_state_snapshot()
    if not state["safe"]:
        return False

    fps = max(1.0, float(cfg.get("chat_checks_per_second", 50)))
    max_age = max(0.20, 6.0 / fps)
    return (time.monotonic() - state["last_check"]) <= max_age


def _get_active_dreamms_client_bbox():
    """Return active DreamMS client bbox as (left, top, right, bottom)."""
    if win32gui is None or win32process is None or psutil is None:
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return None

        if psutil.Process(pid).name().lower() != "dreamms.exe":
            return None

        left_top = win32gui.ClientToScreen(hwnd, (0, 0))
        client = win32gui.GetClientRect(hwnd)
        width = int(client[2] - client[0])
        height = int(client[3] - client[1])
        if width <= 0 or height <= 0:
            return None

        left, top = int(left_top[0]), int(left_top[1])
        return (left, top, left + width, top + height)
    except Exception:
        return None


def _mss_capture_bgr(sct, bbox):
    """Fast MSS capture of one rectangle, returned as BGR uint8."""
    if sct is None or np is None:
        return None

    try:
        left, top, right, bottom = map(int, bbox)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None

        shot = sct.grab({
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        })
        frame = np.asarray(shot, dtype=np.uint8)

        # MSS returns BGRA; OpenCV templates are BGR.
        if frame.ndim != 3 or frame.shape[2] < 3:
            return None
        return frame[:, :, :3]
    except Exception:
        return None


def _load_chat_template_if_needed(force=False):
    """Load/reload assets/chatbox.png when needed or when its file changes."""
    global CHAT_TEMPLATE, CHAT_TEMPLATE_MTIME

    if cv2 is None or np is None:
        return None

    try:
        mtime = os.path.getmtime(CHAT_TEMPLATE_PATH)
    except Exception:
        CHAT_TEMPLATE = None
        CHAT_TEMPLATE_MTIME = None
        return None

    if (
        not force
        and CHAT_TEMPLATE is not None
        and CHAT_TEMPLATE_MTIME == mtime
    ):
        return CHAT_TEMPLATE

    template = cv2.imread(CHAT_TEMPLATE_PATH, cv2.IMREAD_COLOR)
    if template is None or template.size == 0:
        CHAT_TEMPLATE = None
        CHAT_TEMPLATE_MTIME = mtime
        return None

    CHAT_TEMPLATE = template
    CHAT_TEMPLATE_MTIME = mtime
    return CHAT_TEMPLATE


def _same_size_template_score(image_bgr, template_bgr):
    """Very cheap exact-size comparison used by the normal 50 Hz fast path."""
    if image_bgr is None or template_bgr is None or cv2 is None:
        return 0.0
    if image_bgr.shape != template_bgr.shape:
        return 0.0

    try:
        # Same-size matchTemplate produces only one result value, so unlike
        # whole-window searching this is extremely cheap.
        result = cv2.matchTemplate(
            image_bgr,
            template_bgr,
            cv2.TM_CCOEFF_NORMED,
        )
        score = float(result[0, 0])
        if score != score:  # NaN
            return 0.0
        return max(0.0, min(1.0, score))
    except Exception:
        return 0.0


def _chat_full_search(sct, client_bbox, template, threshold):
    """Rare reacquisition search: startup or DreamMS geometry change only."""
    client_bgr = _mss_capture_bgr(sct, client_bbox)
    if client_bgr is None:
        return False, 0.0, None

    th, tw = template.shape[:2]
    ch, cw = client_bgr.shape[:2]
    if ch < th or cw < tw:
        return False, 0.0, None

    try:
        result = cv2.matchTemplate(
            client_bgr,
            template,
            cv2.TM_CCOEFF_NORMED,
        )
        _, max_score, _, max_loc = cv2.minMaxLoc(result)
        score = float(max_score) if max_score == max_score else 0.0
        return score >= threshold, score, tuple(max_loc)
    except Exception:
        return False, 0.0, None


def _chat_detector_loop(cfg):
    """Lightweight detector: one search, then tiny ROI checks at up to 50 Hz."""
    global CHAT_CACHED_LOCATION, CHAT_CACHED_CLIENT_BBOX

    if mss is None:
        _set_chat_state(False, "Missing MSS", 0.0)
        return

    try:
        sct = mss.mss()
    except Exception as exc:
        _set_chat_state(False, f"MSS error: {exc}", 0.0)
        return

    last_template_mtime = None

    try:
        while not CHAT_DETECTOR_STOP.is_set():
            started = time.monotonic()

            try:
                enabled = bool(cfg.get("chat_protection_enabled", True))
                fps = max(
                    1.0,
                    min(100.0, float(cfg.get("chat_checks_per_second", 50))),
                )
                threshold = max(
                    0.50,
                    min(0.9999, float(cfg.get("chat_match_threshold", 0.97))),
                )

                if not enabled:
                    _set_chat_state(True, "Off", 1.0)
                elif cv2 is None or np is None:
                    _set_chat_state(False, "Missing OpenCV/NumPy", 0.0)
                else:
                    template = _load_chat_template_if_needed()
                    if template is None:
                        _set_chat_state(False, "Missing chatbox.png", 0.0)
                    else:
                        try:
                            current_template_mtime = os.path.getmtime(CHAT_TEMPLATE_PATH)
                        except Exception:
                            current_template_mtime = None

                        client_bbox = _get_active_dreamms_client_bbox()

                        if client_bbox is None:
                            _set_chat_state(False, "Game inactive", 0.0)
                        else:
                            state = _get_chat_state_snapshot()
                            cached = state["cached_location"]
                            cached_bbox = state["client_bbox"]

                            # FULL SEARCH is allowed only when:
                            #   1) detector has never acquired the template,
                            #   2) the DreamMS client moved/resized, or
                            #   3) chatbox.png itself changed on disk.
                            geometry_changed = (
                                cached_bbox is None
                                or tuple(cached_bbox) != tuple(client_bbox)
                            )
                            template_changed = (
                                last_template_mtime is not None
                                and current_template_mtime != last_template_mtime
                            )
                            need_reacquire = (
                                cached is None
                                or geometry_changed
                                or template_changed
                            )

                            if need_reacquire:
                                found, score, location = _chat_full_search(
                                    sct,
                                    client_bbox,
                                    template,
                                    threshold,
                                )
                                if found and location is not None:
                                    _set_chat_state(
                                        True,
                                        "Safe",
                                        score,
                                        location,
                                        client_bbox,
                                    )
                                else:
                                    # No repeated whole-window searching while
                                    # the geometry stays unchanged.
                                    with CHAT_STATE_LOCK:
                                        CHAT_CACHED_LOCATION = None
                                        CHAT_CACHED_CLIENT_BBOX = tuple(client_bbox)
                                    _set_chat_state(
                                        False,
                                        "Not acquired",
                                        score,
                                        client_bbox=client_bbox,
                                    )
                            else:
                                # NORMAL FAST PATH:
                                # capture only the exact chatbox-sized rectangle.
                                x, y = cached
                                left, top, right, bottom = client_bbox
                                th, tw = template.shape[:2]
                                client_w = right - left
                                client_h = bottom - top

                                if (
                                    x < 0
                                    or y < 0
                                    or x + tw > client_w
                                    or y + th > client_h
                                ):
                                    # Geometry/location invalid -> force reacquire
                                    # on the next detector cycle.
                                    with CHAT_STATE_LOCK:
                                        CHAT_CACHED_LOCATION = None
                                    _set_chat_state(
                                        False,
                                        "Reacquiring",
                                        0.0,
                                        client_bbox=client_bbox,
                                    )
                                else:
                                    roi = _mss_capture_bgr(
                                        sct,
                                        (
                                            left + x,
                                            top + y,
                                            left + x + tw,
                                            top + y + th,
                                        ),
                                    )
                                    score = _same_size_template_score(
                                        roi,
                                        template,
                                    )

                                    # Crucial optimization:
                                    # a mismatch means "chat unsafe".
                                    # It does NOT trigger a full-window search.
                                    _set_chat_state(
                                        score >= threshold,
                                        "Safe" if score >= threshold else "Blocked",
                                        score,
                                        cached,
                                        client_bbox,
                                    )

                        last_template_mtime = current_template_mtime

            except Exception as exc:
                _set_chat_state(False, f"Detector error: {exc}", 0.0)

            try:
                fps = max(
                    1.0,
                    min(100.0, float(cfg.get("chat_checks_per_second", 50))),
                )
            except Exception:
                fps = 50.0

            period = 1.0 / fps
            elapsed = time.monotonic() - started
            CHAT_DETECTOR_STOP.wait(max(0.001, period - elapsed))
    finally:
        try:
            sct.close()
        except Exception:
            pass


def start_chat_detector(cfg):
    """Start the one permanent Chat Protection detector thread."""
    global CHAT_DETECTOR_THREAD

    if CHAT_DETECTOR_THREAD is not None and CHAT_DETECTOR_THREAD.is_alive():
        return

    with CHAT_DETECTOR_START_LOCK:
        if CHAT_DETECTOR_THREAD is not None and CHAT_DETECTOR_THREAD.is_alive():
            return

        CHAT_DETECTOR_STOP.clear()
        CHAT_DETECTOR_THREAD = threading.Thread(
            target=_chat_detector_loop,
            args=(cfg,),
            daemon=True,
            name="ChatProtection",
        )
        CHAT_DETECTOR_THREAD.start()


def stop_chat_detector():
    CHAT_DETECTOR_STOP.set()


def fenrir_helper_inputs_suspended():
    """True while a Fenrir action requested helper-input suspension."""
    with FENRIR_STATE_LOCK:
        return bool(FENRIR_ACTIVE)


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


def send_combo_drain_to_arduino(cfg):
    """Combo Drain path only: DOWN -> DOWN -> CTRL."""
    legacy_hold = max(0.0, float(cfg.get("combo_drain_hold", 0.01)))
    down_hold_ms = int(round(max(
        0.0, float(cfg.get("combo_drain_down_hold", legacy_hold))
    ) * 1000))
    ctrl_hold_ms = int(round(max(
        0.0, float(cfg.get("combo_drain_ctrl_hold", legacy_hold))
    ) * 1000))

    gap_1 = max(0.0, float(cfg.get("combo_drain_delay_1", 0.05)))
    gap_2 = max(0.0, float(cfg.get("combo_drain_delay_2", 0.05)))

    return ARDUINO.send_sequence(
        cfg,
        [
            f"DOWN {down_hold_ms}",
            f"DOWN {down_hold_ms}",
            f"CTRL {ctrl_hold_ms}",
        ],
        gaps=[gap_1, gap_2],
    )


def combo_drain_step(cfg):
    """Local fallback for Combo Drain when Arduino output is disabled."""
    legacy_hold = max(0.0, float(cfg.get("combo_drain_hold", 0.01)))
    down_hold = max(
        0.0, float(cfg.get("combo_drain_down_hold", legacy_hold))
    )
    ctrl_hold = max(
        0.0, float(cfg.get("combo_drain_ctrl_hold", legacy_hold))
    )

    gap_1 = max(0.0, float(cfg.get("combo_drain_delay_1", 0.05)))
    gap_2 = max(0.0, float(cfg.get("combo_drain_delay_2", 0.05)))

    send_virtual_key(0x28, down_hold)  # VK_DOWN
    if gap_1 > 0:
        time.sleep(gap_1)

    send_virtual_key(0x28, down_hold)  # VK_DOWN
    if gap_2 > 0:
        time.sleep(gap_2)

    send_virtual_key(0x11, ctrl_hold)  # VK_CONTROL



def send_fenrir_key_to_arduino(cfg, command, hold_seconds):
    hold_ms = int(round(max(0.0, float(hold_seconds)) * 1000))
    return ARDUINO.send_sequence(cfg, [f"{command} {hold_ms}"])


def send_fenrir_direction_state(cfg, direction, pressed):
    """Hold/release a horizontal direction after Fenrir.

    Requires Arduino commands:
      LEFT_DOWN / LEFT_UP / RIGHT_DOWN / RIGHT_UP
    """
    direction = str(direction or "").upper()
    if direction not in {"LEFT", "RIGHT"}:
        return False

    suffix = "DOWN" if pressed else "UP"

    if cfg.get("use_arduino", False):
        return ARDUINO.send_sequence(cfg, [f"{direction}_{suffix}"])

    vk = 0x25 if direction == "LEFT" else 0x27
    send_virtual_key_state(vk, pressed)
    return True


def _fenrir_current_horizontal_direction():
    """Current physical horizontal direction; neither/both => None."""
    if keyboard is None:
        return None
    try:
        left = bool(keyboard.is_pressed("left"))
        right = bool(keyboard.is_pressed("right"))
    except Exception:
        return None

    if left == right:
        return None
    return "LEFT" if left else "RIGHT"


def _fenrir_restore_worker(cfg_snapshot, initial_direction, stop_event):
    """Keep the Arduino direction aligned with what the user is physically holding."""
    global FENRIR_RESTORE_DIRECTION, FENRIR_RESTORE_THREAD

    held = initial_direction
    try:
        if held not in {"LEFT", "RIGHT"}:
            return

        send_fenrir_direction_state(cfg_snapshot, held, True)

        with FENRIR_RESTORE_LOCK:
            FENRIR_RESTORE_DIRECTION = held

        while not stop_event.wait(0.005):
            if not is_dreamms_active():
                break
            if not chat_protection_allows(cfg_snapshot):
                break
            if not cfg_snapshot.get("enabled", True):
                break
            if not cfg_snapshot.get("fenrir_enabled", True):
                break

            current = _fenrir_current_horizontal_direction()

            if current == held:
                continue

            # A direction switch can briefly pass through "neither" between
            # releasing one arrow and pressing the other. Give it 20 ms before
            # deciding the user truly released movement.
            if current is None:
                if stop_event.wait(0.020):
                    break
                current = _fenrir_current_horizontal_direction()

            if current == held:
                continue

            send_fenrir_direction_state(cfg_snapshot, held, False)

            if current in {"LEFT", "RIGHT"}:
                held = current
                send_fenrir_direction_state(cfg_snapshot, held, True)
                with FENRIR_RESTORE_LOCK:
                    FENRIR_RESTORE_DIRECTION = held
                continue

            held = None
            break
    finally:
        if held in {"LEFT", "RIGHT"}:
            send_fenrir_direction_state(cfg_snapshot, held, False)

        with FENRIR_RESTORE_LOCK:
            if FENRIR_RESTORE_THREAD is threading.current_thread():
                FENRIR_RESTORE_THREAD = None
            FENRIR_RESTORE_DIRECTION = None


def stop_fenrir_direction_restore(cfg=None, wait=False):
    """Release any Arduino-held post-Fenrir direction."""
    global FENRIR_RESTORE_THREAD

    FENRIR_RESTORE_STOP.set()

    thread = FENRIR_RESTORE_THREAD
    if wait and thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=0.10)

    # If the worker did not get CPU yet, explicitly release the known held key.
    with FENRIR_RESTORE_LOCK:
        held = FENRIR_RESTORE_DIRECTION

    if held in {"LEFT", "RIGHT"} and cfg is not None:
        send_fenrir_direction_state(cfg, held, False)


def start_fenrir_direction_restore(cfg_snapshot):
    """At combo end, resume whatever LEFT/RIGHT the user is physically holding."""
    global FENRIR_RESTORE_THREAD

    current = _fenrir_current_horizontal_direction()
    if current not in {"LEFT", "RIGHT"}:
        return

    # End any prior restoration before taking ownership for this combo.
    stop_fenrir_direction_restore(cfg_snapshot, wait=True)
    FENRIR_RESTORE_STOP.clear()

    thread = threading.Thread(
        target=_fenrir_restore_worker,
        args=(dict(cfg_snapshot), current, FENRIR_RESTORE_STOP),
        daemon=True,
        name="FenrirDirectionRestore",
    )
    FENRIR_RESTORE_THREAD = thread
    thread.start()


def send_fenrir_to_arduino(cfg, direction):
    """Fenrir: DOWN -> LEFT/RIGHT -> CTRL."""
    down_hold = max(0.0, float(cfg.get("fenrir_down_hold", 0.01)))
    direction_hold = max(0.0, float(cfg.get("fenrir_direction_hold", 0.01)))
    ctrl_hold = max(0.0, float(cfg.get("fenrir_ctrl_hold", 0.01)))
    gap_1 = max(0.0, float(cfg.get("fenrir_delay_1", 0.05)))
    gap_2 = max(0.0, float(cfg.get("fenrir_delay_2", 0.05)))

    send_fenrir_key_to_arduino(cfg, "DOWN", down_hold)
    if gap_1 > 0:
        time.sleep(gap_1)
    send_fenrir_key_to_arduino(cfg, direction, direction_hold)
    if gap_2 > 0:
        time.sleep(gap_2)
    send_fenrir_key_to_arduino(cfg, "CTRL", ctrl_hold)
    return True


def fenrir_step(cfg, direction):
    down_hold = max(0.0, float(cfg.get("fenrir_down_hold", 0.01)))
    direction_hold = max(0.0, float(cfg.get("fenrir_direction_hold", 0.01)))
    ctrl_hold = max(0.0, float(cfg.get("fenrir_ctrl_hold", 0.01)))
    gap_1 = max(0.0, float(cfg.get("fenrir_delay_1", 0.05)))
    gap_2 = max(0.0, float(cfg.get("fenrir_delay_2", 0.05)))

    send_virtual_key(0x28, down_hold)
    if gap_1 > 0:
        time.sleep(gap_1)
    send_virtual_key(0x27 if direction == "RIGHT" else 0x25, direction_hold)
    if gap_2 > 0:
        time.sleep(gap_2)
    send_virtual_key(0x11, ctrl_hold)

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


def _run_combat_sequence(cfg_snapshot):
    """Execute one already-accepted Combat action from a settings snapshot."""
    if not chat_protection_allows(cfg_snapshot):
        return
    if not is_dreamms_active():
        log("DreamMS not active; ignoring queued combat action.")
        return

    if cfg_snapshot.get("use_arduino", False):
        send_combat_to_arduino(cfg_snapshot)
    else:
        combat_step(cfg_snapshot)


def _combat_worker_loop():
    """Single FIFO Combat worker. Accepted taps are never cancelled by key release."""
    while True:
        action = COMBAT_QUEUE.get()
        try:
            execute_at = float(action["execute_at"])
            cfg_snapshot = action["cfg"]

            remaining = execute_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

            _run_combat_sequence(cfg_snapshot)
        except Exception as exc:
            log(f"Combat worker error: {exc}")
        finally:
            COMBAT_QUEUE.task_done()


def _ensure_combat_worker():
    """Start the one permanent Combat worker exactly once."""
    global COMBAT_THREAD

    if COMBAT_THREAD is not None and COMBAT_THREAD.is_alive():
        return

    with COMBAT_WORKER_START_LOCK:
        if COMBAT_THREAD is not None and COMBAT_THREAD.is_alive():
            return

        thread = threading.Thread(
            target=_combat_worker_loop,
            daemon=True,
            name="CombatWorker",
        )
        COMBAT_THREAD = thread
        thread.start()


def combat_trigger(cfg):
    """Run one Combat action directly (used by the GUI Test button)."""
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

    cfg_snapshot = dict(cfg)
    delay = max(0.0, float(cfg_snapshot.get("alt_delay", 0.05)))
    log(f"Combat trigger: key={cfg_snapshot.get('combat_key', 'v')} | delay={delay:.3f}s")

    if delay > 0:
        time.sleep(delay)

    _run_combat_sequence(cfg_snapshot)


def handle_combat_key_press(cfg):
    global COMBAT_KEY_DOWN, LAST_TRIGGER_TIME

    if fenrir_helper_inputs_suspended():
        return
    if not chat_protection_allows(cfg):
        return

    # Ignore keyboard auto-repeat while this physical V press is still held.
    if COMBAT_KEY_DOWN:
        return

    # Function/master enable is checked ONLY when deciding whether to accept
    # this physical tap. Once accepted, its entire sequence is guaranteed.
    if not cfg.get("enabled", True):
        return
    if not cfg.get("combat_enabled", True):
        return
    if not is_dreamms_active():
        return

    COMBAT_KEY_DOWN = True

    now = time.monotonic()
    if now - LAST_TRIGGER_TIME < TRIGGER_COOLDOWN_SECONDS:
        return
    LAST_TRIGGER_TIME = now

    # Snapshot all settings at the instant of the accepted tap so later GUI
    # edits cannot change the timing/key-hold values of an action in progress.
    cfg_snapshot = dict(cfg)
    delay = max(0.0, float(cfg_snapshot.get("alt_delay", 0.05)))

    _ensure_combat_worker()

    COMBAT_QUEUE.put({
        "execute_at": now + delay,
        "cfg": cfg_snapshot,
    })


def handle_combat_key_release(event=None):
    global COMBAT_KEY_DOWN
    # Release only rearms V for the next physical tap.
    # It NEVER cancels an already-accepted Combat action.
    COMBAT_KEY_DOWN = False


def attack_loop(cfg, stop_event):
    """Spam CTRL while attack is held. Has its own stop event and no combat state."""
    log(
        f"Attack started: key={cfg.get('attack_key', 'space')} | "
        f"hold={float(cfg.get('attack_hold', 0.01)):.4f}s | "
        f"delay={float(cfg.get('attack_delay', 0.20)):.4f}s"
    )

    while not stop_event.is_set():
        if (
            not cfg.get("enabled", True)
            or not cfg.get("attack_enabled", True)
            or not chat_protection_allows(cfg)
            or not is_dreamms_active()
        ):
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

    if fenrir_helper_inputs_suspended():
        return
    if not chat_protection_allows(cfg):
        return

    if ATTACK_KEY_DOWN:
        return
    if not cfg.get("enabled", True):
        return
    if not cfg.get("attack_enabled", True):
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



def _run_combo_drain_sequence(cfg_snapshot):
    """Execute one already-accepted Combo Drain action."""
    if not chat_protection_allows(cfg_snapshot):
        return
    if not is_dreamms_active():
        log("DreamMS not active; ignoring queued Combo Drain action.")
        return

    if cfg_snapshot.get("use_arduino", False):
        send_combo_drain_to_arduino(cfg_snapshot)
    else:
        combo_drain_step(cfg_snapshot)


def _combo_drain_worker_loop():
    """Single FIFO worker for Combo Drain."""
    while True:
        action = COMBO_DRAIN_QUEUE.get()
        try:
            execute_at = float(action["execute_at"])
            cfg_snapshot = action["cfg"]

            # Initial delay is measured from the accepted trigger press.
            remaining = execute_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

            _run_combo_drain_sequence(cfg_snapshot)
        except Exception as exc:
            log(f"Combo Drain worker error: {exc}")
        finally:
            COMBO_DRAIN_QUEUE.task_done()


def _ensure_combo_drain_worker():
    """Start the permanent Combo Drain worker exactly once."""
    global COMBO_DRAIN_THREAD

    if COMBO_DRAIN_THREAD is not None and COMBO_DRAIN_THREAD.is_alive():
        return

    with COMBO_DRAIN_WORKER_START_LOCK:
        if COMBO_DRAIN_THREAD is not None and COMBO_DRAIN_THREAD.is_alive():
            return

        thread = threading.Thread(
            target=_combo_drain_worker_loop,
            daemon=True,
            name="ComboDrainWorker",
        )
        COMBO_DRAIN_THREAD = thread
        thread.start()


def handle_combo_drain_key_press(cfg):
    """Accept one physical trigger press and queue one full Combo Drain sequence."""
    global COMBO_DRAIN_KEY_DOWN

    if fenrir_helper_inputs_suspended():
        return
    if not chat_protection_allows(cfg):
        return

    if COMBO_DRAIN_KEY_DOWN:
        return

    if not cfg.get("enabled", True):
        return
    if not cfg.get("combo_drain_enabled", True):
        return
    if not is_dreamms_active():
        return

    COMBO_DRAIN_KEY_DOWN = True

    # Snapshot settings at the instant this trigger press is accepted.
    cfg_snapshot = dict(cfg)
    initial_delay = max(
        0.0, float(cfg_snapshot.get("combo_drain_initial_delay", 0.05))
    )
    execute_at = time.monotonic() + initial_delay

    _ensure_combo_drain_worker()
    COMBO_DRAIN_QUEUE.put({
        "execute_at": execute_at,
        "cfg": cfg_snapshot,
    })


def handle_combo_drain_key_release(event=None):
    global COMBO_DRAIN_KEY_DOWN
    # Release only rearms the trigger. It never cancels an accepted sequence.
    COMBO_DRAIN_KEY_DOWN = False


def _run_fenrir_sequence(cfg_snapshot, direction):
    global FENRIR_ACTIVE
    completed = False
    try:
        if not chat_protection_allows(cfg_snapshot):
            return
        if not is_dreamms_active():
            log("DreamMS not active; ignoring queued Fenrir action.")
            return

        if cfg_snapshot.get("use_arduino", False):
            send_fenrir_to_arduino(cfg_snapshot, direction)
        else:
            fenrir_step(cfg_snapshot, direction)

        completed = True
    finally:
        # Sample the user's physical LEFT/RIGHT at the END of the combo.
        # This can differ from the direction captured when Fenrir was triggered.
        if completed and chat_protection_allows(cfg_snapshot) and is_dreamms_active():
            start_fenrir_direction_restore(cfg_snapshot)

        with FENRIR_STATE_LOCK:
            FENRIR_ACTIVE = False


def _fenrir_worker_loop():
    global FENRIR_ACTIVE
    while True:
        action = FENRIR_QUEUE.get()
        try:
            remaining = float(action["execute_at"]) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            _run_fenrir_sequence(action["cfg"], action["direction"])
        except Exception as exc:
            with FENRIR_STATE_LOCK:
                FENRIR_ACTIVE = False
            log(f"Fenrir worker error: {exc}")
        finally:
            FENRIR_QUEUE.task_done()


def _ensure_fenrir_worker():
    global FENRIR_THREAD
    if FENRIR_THREAD is not None and FENRIR_THREAD.is_alive():
        return
    with FENRIR_WORKER_START_LOCK:
        if FENRIR_THREAD is not None and FENRIR_THREAD.is_alive():
            return
        FENRIR_THREAD = threading.Thread(target=_fenrir_worker_loop, daemon=True, name="FenrirWorker")
        FENRIR_THREAD.start()


def _fenrir_direction_from_keyboard():
    """Read only LEFT/RIGHT at trigger time; UP/DOWN are irrelevant."""
    return _fenrir_current_horizontal_direction()


def handle_fenrir_key_press(cfg):
    global FENRIR_KEY_DOWN, FENRIR_ACTIVE
    if not chat_protection_allows(cfg):
        return
    if FENRIR_KEY_DOWN:
        return
    if not cfg.get("enabled", True) or not cfg.get("fenrir_enabled", True):
        return
    if not is_dreamms_active():
        return

    direction = _fenrir_direction_from_keyboard()
    if direction is None:
        return

    # A new Fenrir combo owns direction control; release any prior post-combo hold.
    stop_fenrir_direction_restore(cfg, wait=False)

    with FENRIR_STATE_LOCK:
        if FENRIR_ACTIVE:
            return
        FENRIR_ACTIVE = bool(cfg.get("fenrir_suspend_inputs", True))

    FENRIR_KEY_DOWN = True
    cfg_snapshot = dict(cfg)
    initial_delay = max(0.0, float(cfg_snapshot.get("fenrir_initial_delay", 0.05)))
    _ensure_fenrir_worker()
    FENRIR_QUEUE.put({
        "execute_at": time.monotonic() + initial_delay,
        "cfg": cfg_snapshot,
        "direction": direction,
    })


def handle_fenrir_key_release(event=None):
    global FENRIR_KEY_DOWN
    FENRIR_KEY_DOWN = False

class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.enabled = bool(self.cfg.get("enabled", True))

        self.root.title("MapleStory Combat Helper")
        self.root.geometry("780x760")
        self.root.minsize(720, 680)

        main = ttk.Frame(root, padding=14)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(4, weight=1)

        # Small helper used only for GUI descriptions.
        def help_button(parent, title, text, row, column):
            btn = ttk.Button(
                parent,
                text="?",
                width=2,
                command=lambda: messagebox.showinfo(title, text),
            )
            btn.grid(row=row, column=column, sticky=tk.W, padx=(4, 0), pady=5)
            return btn

        # ------------------------------------------------------------
        # Function tabs
        # ------------------------------------------------------------
        function_frame = ttk.LabelFrame(main, text="Functions", padding=8)
        function_frame.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 10))
        function_frame.columnconfigure(0, weight=1)

        function_tabs = ttk.Notebook(function_frame)
        function_tabs.grid(row=0, column=0, sticky="nsew")

        # ------------------------------------------------------------
        # Combat tab
        # ------------------------------------------------------------
        combat_frame = ttk.Frame(function_tabs, padding=14)
        function_tabs.add(combat_frame, text="  Combat  ")
        combat_frame.columnconfigure(1, weight=1)

        self.combat_enabled = tk.BooleanVar(value=bool(self.cfg.get("combat_enabled", True)))
        ttk.Checkbutton(
            combat_frame,
            text="Enable Combat function",
            variable=self.combat_enabled,
            command=self.on_combat_enabled_changed,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(2, 9))
        help_button(
            combat_frame,
            "Enable Combat",
            "Turns only the Combat function on or off.\n\n"
            "The main helper can remain ON. When unticked, the Combat trigger key is ignored.",
            0, 2,
        )

        ttk.Separator(combat_frame, orient=tk.HORIZONTAL).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, 6)
        )

        ttk.Label(combat_frame, text="Trigger key").grid(row=2, column=0, sticky=tk.W, pady=7)
        self.combat_key = ttk.Entry(combat_frame, width=18)
        self.combat_key.insert(0, self.cfg.get("combat_key", "v"))
        self.combat_key.grid(row=2, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            combat_frame,
            "Combat trigger key",
            "The physical key that starts the Combat function.\n\n"
            "Default: V\n"
            "The key still passes through normally to the game.",
            2, 2,
        )

        ttk.Label(combat_frame, text="ALT hold (s)").grid(row=3, column=0, sticky=tk.W, pady=7)
        self.combat_hold = ttk.Entry(combat_frame, width=18)
        self.combat_hold.insert(0, str(self.cfg.get("combat_hold", 0.01)))
        self.combat_hold.grid(row=3, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            combat_frame,
            "ALT hold",
            "How long each Arduino ALT press is held down.\n\n"
            "Example: 0.01 = 10 ms.",
            3, 2,
        )

        ttk.Label(combat_frame, text="Trigger delay (s)").grid(row=4, column=0, sticky=tk.W, pady=7)
        self.alt_delay = ttk.Entry(combat_frame, width=18)
        self.alt_delay.insert(0, str(self.cfg.get("alt_delay", 0.05)))
        self.alt_delay.grid(row=4, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            combat_frame,
            "Combat trigger delay",
            "Delay after pressing the Combat key before the ALT sequence begins.",
            4, 2,
        )

        ttk.Label(combat_frame, text="ALT tap gap (s)").grid(row=5, column=0, sticky=tk.W, pady=7)
        self.double_tap_delay = ttk.Entry(combat_frame, width=18)
        self.double_tap_delay.insert(0, str(self.cfg.get("double_tap_delay", 0.02)))
        self.double_tap_delay.grid(row=5, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            combat_frame,
            "ALT tap gap",
            "Time between the first and second ALT taps in the Combat action.",
            5, 2,
        )

        # ------------------------------------------------------------
        # Attack tab
        # ------------------------------------------------------------
        attack_frame = ttk.Frame(function_tabs, padding=14)
        function_tabs.add(attack_frame, text="  Attack  ")
        attack_frame.columnconfigure(1, weight=1)

        self.attack_enabled = tk.BooleanVar(value=bool(self.cfg.get("attack_enabled", True)))
        ttk.Checkbutton(
            attack_frame,
            text="Enable Attack function",
            variable=self.attack_enabled,
            command=self.on_attack_enabled_changed,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(2, 9))
        help_button(
            attack_frame,
            "Enable Attack",
            "Turns only the Attack function on or off.\n\n"
            "The main helper can remain ON. When unticked, the Attack trigger key is ignored.",
            0, 2,
        )

        ttk.Separator(attack_frame, orient=tk.HORIZONTAL).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, 6)
        )

        ttk.Label(attack_frame, text="Trigger key").grid(row=2, column=0, sticky=tk.W, pady=7)
        self.attack_key = ttk.Entry(attack_frame, width=18)
        self.attack_key.insert(0, self.cfg.get("attack_key", "space"))
        self.attack_key.grid(row=2, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            attack_frame,
            "Attack trigger key",
            "Hold this key to repeatedly send CTRL through the Arduino.\n\n"
            "Default: Space\n"
            "Release the key to stop attacking.",
            2, 2,
        )

        ttk.Label(attack_frame, text="CTRL hold (s)").grid(row=3, column=0, sticky=tk.W, pady=7)
        self.attack_hold = ttk.Entry(attack_frame, width=18)
        self.attack_hold.insert(0, str(self.cfg.get("attack_hold", 0.01)))
        self.attack_hold.grid(row=3, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            attack_frame,
            "CTRL hold",
            "How long each Arduino CTRL attack press is held down.\n\n"
            "Example: 0.01 = 10 ms.",
            3, 2,
        )

        ttk.Label(attack_frame, text="Repeat delay (s)").grid(row=4, column=0, sticky=tk.W, pady=7)
        self.attack_delay = ttk.Entry(attack_frame, width=18)
        self.attack_delay.insert(0, str(self.cfg.get("attack_delay", 0.20)))
        self.attack_delay.grid(row=4, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            attack_frame,
            "Attack repeat delay",
            "Delay between repeated CTRL attacks while the Attack key is held.\n\n"
            "The first CTRL press still happens immediately.",
            4, 2,
        )

        # ------------------------------------------------------------
        # Combo Drain tab
        # ------------------------------------------------------------
        combo_drain_frame = ttk.Frame(function_tabs, padding=14)
        function_tabs.add(combo_drain_frame, text="  Combo Drain  ")
        combo_drain_frame.columnconfigure(1, weight=1)

        self.combo_drain_enabled = tk.BooleanVar(
            value=bool(self.cfg.get("combo_drain_enabled", True))
        )
        ttk.Checkbutton(
            combo_drain_frame,
            text="Enable Combo Drain function",
            variable=self.combo_drain_enabled,
            command=self.on_combo_drain_enabled_changed,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(2, 9))
        help_button(
            combo_drain_frame,
            "Enable Combo Drain",
            "Turns only the Combo Drain function on or off.\n\n"
            "When enabled, one press of the trigger key performs DOWN → DOWN → CTRL.",
            0, 2,
        )

        ttk.Separator(combo_drain_frame, orient=tk.HORIZONTAL).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, 6)
        )

        ttk.Label(combo_drain_frame, text="Trigger key").grid(
            row=2, column=0, sticky=tk.W, pady=7
        )
        self.combo_drain_key = ttk.Entry(combo_drain_frame, width=18)
        self.combo_drain_key.insert(0, self.cfg.get("combo_drain_key", "x"))
        self.combo_drain_key.grid(row=2, column=1, sticky="ew", padx=(14, 6), pady=7)
        help_button(
            combo_drain_frame,
            "Combo Drain trigger key",
            "Press this physical key once to start one complete Combo Drain sequence.\n\n"
            "The trigger key itself still passes through normally.",
            2, 2,
        )

        ttk.Label(combo_drain_frame, text="DOWN hold (s)").grid(
            row=3, column=0, sticky=tk.W, pady=7
        )
        self.combo_drain_down_hold = ttk.Entry(combo_drain_frame, width=18)
        self.combo_drain_down_hold.insert(
            0, str(self.cfg.get("combo_drain_down_hold", self.cfg.get("combo_drain_hold", 0.01)))
        )
        self.combo_drain_down_hold.grid(
            row=3, column=1, sticky="ew", padx=(14, 6), pady=7
        )
        help_button(
            combo_drain_frame,
            "Combo Drain DOWN hold",
            "How long each of the two DOWN arrow presses is held.\n\n"
            "Example: 0.01 = 10 ms.",
            3, 2,
        )

        ttk.Label(combo_drain_frame, text="CTRL hold (s)").grid(
            row=4, column=0, sticky=tk.W, pady=7
        )
        self.combo_drain_ctrl_hold = ttk.Entry(combo_drain_frame, width=18)
        self.combo_drain_ctrl_hold.insert(
            0, str(self.cfg.get("combo_drain_ctrl_hold", self.cfg.get("combo_drain_hold", 0.01)))
        )
        self.combo_drain_ctrl_hold.grid(
            row=4, column=1, sticky="ew", padx=(14, 6), pady=7
        )
        help_button(
            combo_drain_frame,
            "Combo Drain CTRL hold",
            "How long the final CTRL press is held.\n\n"
            "Example: 0.01 = 10 ms.",
            4, 2,
        )

        ttk.Label(combo_drain_frame, text="Trigger → DOWN 1 delay (s)").grid(
            row=5, column=0, sticky=tk.W, pady=7
        )
        self.combo_drain_initial_delay = ttk.Entry(combo_drain_frame, width=18)
        self.combo_drain_initial_delay.insert(
            0, str(self.cfg.get("combo_drain_initial_delay", 0.05))
        )
        self.combo_drain_initial_delay.grid(
            row=5, column=1, sticky="ew", padx=(14, 6), pady=7
        )
        help_button(
            combo_drain_frame,
            "Initial Combo Drain delay",
            "Delay after pressing the Combo Drain trigger key before the first DOWN command.",
            5, 2,
        )

        ttk.Label(combo_drain_frame, text="DOWN 1 → DOWN 2 delay (s)").grid(
            row=6, column=0, sticky=tk.W, pady=7
        )
        self.combo_drain_delay_1 = ttk.Entry(combo_drain_frame, width=18)
        self.combo_drain_delay_1.insert(
            0, str(self.cfg.get("combo_drain_delay_1", 0.05))
        )
        self.combo_drain_delay_1.grid(
            row=6, column=1, sticky="ew", padx=(14, 6), pady=7
        )
        help_button(
            combo_drain_frame,
            "First Combo Drain delay",
            "Delay between the first DOWN command and the second DOWN command.",
            6, 2,
        )

        ttk.Label(combo_drain_frame, text="DOWN 2 → CTRL delay (s)").grid(
            row=7, column=0, sticky=tk.W, pady=7
        )
        self.combo_drain_delay_2 = ttk.Entry(combo_drain_frame, width=18)
        self.combo_drain_delay_2.insert(
            0, str(self.cfg.get("combo_drain_delay_2", 0.05))
        )
        self.combo_drain_delay_2.grid(
            row=7, column=1, sticky="ew", padx=(14, 6), pady=7
        )
        help_button(
            combo_drain_frame,
            "Second Combo Drain delay",
            "Delay between the second DOWN command and the final CTRL command.",
            7, 2,
        )

        # ------------------------------------------------------------
        # Fenrir tab
        # ------------------------------------------------------------
        fenrir_frame = ttk.Frame(function_tabs, padding=14)
        function_tabs.add(fenrir_frame, text="  Fenrir  ")
        fenrir_frame.columnconfigure(1, weight=1)

        self.fenrir_enabled = tk.BooleanVar(value=bool(self.cfg.get("fenrir_enabled", True)))
        ttk.Checkbutton(fenrir_frame, text="Enable Fenrir function", variable=self.fenrir_enabled, command=self.on_fenrir_enabled_changed).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(2,9))
        help_button(fenrir_frame, "Enable Fenrir", "Turns only the Fenrir function on or off.", 0, 2)

        ttk.Separator(fenrir_frame, orient=tk.HORIZONTAL).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0,6))

        ttk.Label(fenrir_frame, text="Trigger key").grid(row=2, column=0, sticky=tk.W, pady=6)
        self.fenrir_key = ttk.Entry(fenrir_frame, width=18)
        self.fenrir_key.insert(0, self.cfg.get("fenrir_key", "c"))
        self.fenrir_key.grid(row=2, column=1, sticky="ew", padx=(14,6), pady=6)
        help_button(fenrir_frame, "Fenrir trigger key", "Default: C. At trigger time Fenrir reads only LEFT or RIGHT currently held. UP/DOWN are ignored.", 2, 2)

        self.fenrir_suspend_inputs = tk.BooleanVar(value=bool(self.cfg.get("fenrir_suspend_inputs", True)))
        ttk.Checkbutton(fenrir_frame, text="Suspend helper inputs during combo", variable=self.fenrir_suspend_inputs).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=6)
        help_button(fenrir_frame, "Suspend helper inputs", "When enabled, other helper trigger functions are ignored until Fenrir finishes. This does not globally block Windows/HID input, so Arduino combo keys still work.", 3, 2)

        fields = [
            ("DOWN hold (s)", "fenrir_down_hold", 0.01),
            ("LEFT/RIGHT hold (s)", "fenrir_direction_hold", 0.01),
            ("CTRL hold (s)", "fenrir_ctrl_hold", 0.01),
            ("Trigger → DOWN delay (s)", "fenrir_initial_delay", 0.05),
            ("DOWN → LEFT/RIGHT delay (s)", "fenrir_delay_1", 0.05),
            ("LEFT/RIGHT → CTRL delay (s)", "fenrir_delay_2", 0.05),
        ]
        for i, (label, attr, default) in enumerate(fields, start=4):
            ttk.Label(fenrir_frame, text=label).grid(row=i, column=0, sticky=tk.W, pady=6)
            entry = ttk.Entry(fenrir_frame, width=18)
            entry.insert(0, str(self.cfg.get(attr, default)))
            entry.grid(row=i, column=1, sticky="ew", padx=(14,6), pady=6)
            setattr(self, attr, entry)

        # ------------------------------------------------------------
        # Chat Protection tab
        # ------------------------------------------------------------
        chat_frame = ttk.Frame(function_tabs, padding=14)
        function_tabs.add(chat_frame, text="  Chat Protection  ")
        chat_frame.columnconfigure(1, weight=1)

        self.chat_protection_enabled = tk.BooleanVar(
            value=bool(self.cfg.get("chat_protection_enabled", True))
        )
        ttk.Checkbutton(
            chat_frame,
            text="Enable Chat Protection",
            variable=self.chat_protection_enabled,
            command=self.on_chat_protection_changed,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(2, 9))
        help_button(
            chat_frame,
            "Chat Protection",
            "When enabled, helper functions run only while assets/chatbox.png "
            "is visibly matched inside the active DreamMS client.\n\n"
            "If the image disappears or detection becomes uncertain, helper "
            "functions are blocked so normal typing passes through untouched.",
            0, 2,
        )

        ttk.Separator(chat_frame, orient=tk.HORIZONTAL).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, 6)
        )

        ttk.Label(chat_frame, text="Reference image").grid(
            row=2, column=0, sticky=tk.W, pady=7
        )
        ttk.Label(chat_frame, text="assets/chatbox.png").grid(
            row=2, column=1, sticky=tk.W, padx=(14, 6), pady=7
        )

        ttk.Label(chat_frame, text="Checks per second").grid(
            row=3, column=0, sticky=tk.W, pady=7
        )
        self.chat_checks_per_second = ttk.Entry(chat_frame, width=18)
        self.chat_checks_per_second.insert(
            0, str(self.cfg.get("chat_checks_per_second", 50))
        )
        self.chat_checks_per_second.grid(
            row=3, column=1, sticky="ew", padx=(14, 6), pady=7
        )
        help_button(
            chat_frame,
            "Chat detector rate",
            "Fast-path image checks per second.\n\n"
            "Default: 50 Hz (one check about every 20 ms). "
            "After initial acquisition, only the exact chatbox-sized rectangle "
            "is captured with MSS and compared.",
            3, 2,
        )

        ttk.Label(chat_frame, text="Match threshold").grid(
            row=4, column=0, sticky=tk.W, pady=7
        )
        self.chat_match_threshold = ttk.Entry(chat_frame, width=18)
        self.chat_match_threshold.insert(
            0, str(self.cfg.get("chat_match_threshold", 0.97))
        )
        self.chat_match_threshold.grid(
            row=4, column=1, sticky="ew", padx=(14, 6), pady=7
        )
        help_button(
            chat_frame,
            "Image match threshold",
            "Required template similarity from 0 to 1.\n\n"
            "Default: 0.97. Higher is stricter; lower tolerates more visual variation.",
            4, 2,
        )

        ttk.Label(chat_frame, text="Detection method").grid(
            row=5, column=0, sticky=tk.W, pady=7
        )
        ttk.Label(
            chat_frame,
            text="MSS cached ROI + full search only on startup/move/resize",
        ).grid(row=5, column=1, sticky=tk.W, padx=(14, 6), pady=7)

        # ------------------------------------------------------------
        # Arduino / connection settings
        # ------------------------------------------------------------
        arduino_frame = ttk.LabelFrame(main, text="Arduino / Connection", padding=12)
        arduino_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        arduino_frame.columnconfigure(1, weight=1)
        arduino_frame.columnconfigure(4, weight=1)

        self.use_arduino = tk.BooleanVar(value=bool(self.cfg.get("use_arduino", True)))
        ttk.Checkbutton(
            arduino_frame,
            text="Use Arduino Uno R4 WiFi",
            variable=self.use_arduino,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=5)
        help_button(
            arduino_frame,
            "Use Arduino",
            "When enabled, Combat, Attack, Combo Drain, and Fenrir commands are sent through the Arduino USB keyboard controller.",
            0, 2,
        )

        self.auto_detect = tk.BooleanVar(value=bool(self.cfg.get("auto_detect_arduino", True)))
        ttk.Checkbutton(
            arduino_frame,
            text="Auto-detect Arduino",
            variable=self.auto_detect,
        ).grid(row=0, column=3, columnspan=2, sticky=tk.W, padx=(20, 0), pady=5)
        help_button(
            arduino_frame,
            "Auto-detect Arduino",
            "Automatically searches available serial ports for the Arduino instead of relying only on the saved COM port.",
            0, 5,
        )

        ttk.Label(arduino_frame, text="Serial port").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.serial_port = ttk.Entry(arduino_frame, width=14)
        self.serial_port.insert(0, self.cfg.get("serial_port", "COM3"))
        self.serial_port.grid(row=1, column=1, sticky="ew", padx=(10, 4), pady=5)
        help_button(
            arduino_frame,
            "Serial port",
            "The Arduino COM port, for example COM3. Auto-detect can fill this automatically.",
            1, 2,
        )

        ttk.Label(arduino_frame, text="Baud rate").grid(row=1, column=3, sticky=tk.W, padx=(20, 0), pady=5)
        self.baud_rate = ttk.Entry(arduino_frame, width=14)
        self.baud_rate.insert(0, str(self.cfg.get("baud_rate", 9600)))
        self.baud_rate.grid(row=1, column=4, sticky="ew", padx=(10, 4), pady=5)
        help_button(
            arduino_frame,
            "Baud rate",
            "Serial communication speed. This must match Serial.begin(...) in the Arduino sketch.\n\n"
            "Current default: 9600.",
            1, 5,
        )

        # ------------------------------------------------------------
        # Status
        # ------------------------------------------------------------
        status_frame = ttk.LabelFrame(main, text="Status", padding=12)
        status_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        ttk.Label(status_frame, text="Game window:").grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        self.window_status = ttk.Label(status_frame, text="Checking...", foreground="orange")
        self.window_status.grid(row=0, column=1, sticky=tk.W, padx=(0, 24))

        ttk.Label(status_frame, text="Helper:").grid(row=0, column=2, sticky=tk.W, padx=(0, 8))
        self.toggle_status = ttk.Label(
            status_frame,
            text="On" if self.enabled else "Off",
            foreground="green" if self.enabled else "red",
        )
        self.toggle_status.grid(row=0, column=3, sticky=tk.W, padx=(0, 24))

        ttk.Label(status_frame, text="Arduino:").grid(row=0, column=4, sticky=tk.W, padx=(0, 8))
        self.arduino_status = ttk.Label(status_frame, text="Checking...", foreground="orange")
        self.arduino_status.grid(row=0, column=5, sticky=tk.W, padx=(0, 24))

        ttk.Label(status_frame, text="Chat:").grid(row=0, column=6, sticky=tk.W, padx=(0, 8))
        self.chat_status = ttk.Label(status_frame, text="Starting...", foreground="orange")
        self.chat_status.grid(row=0, column=7, sticky=tk.W)

        # ------------------------------------------------------------
        # Actions
        # ------------------------------------------------------------
        button_frame = ttk.Frame(main)
        button_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        ttk.Button(button_frame, text="Save Settings", command=self.save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(button_frame, text="Auto Detect Arduino", command=self.auto_detect_port).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(button_frame, text="Test Combat", command=self.test).pack(side=tk.LEFT)

        ttk.Label(
            button_frame,
            text="F12 toggles the helper on/off",
        ).pack(side=tk.RIGHT)

        # ------------------------------------------------------------
        # Recent logs
        # ------------------------------------------------------------
        log_frame = ttk.LabelFrame(main, text="Recent Logs", padding=8)
        log_frame.grid(row=4, column=0, columnspan=2, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_view = tk.Text(
            log_frame,
            height=12,
            state="disabled",
            wrap=tk.WORD,
        )
        self.log_view.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_view.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_view.configure(yscrollcommand=scrollbar.set)

        global LOG_WIDGET
        LOG_WIDGET = self.log_view
        self.refresh_recent_logs()

        self.update_window_status()
        self.update_arduino_status()
        start_chat_detector(self.cfg)
        self.update_chat_status()
        self.root.after(250, self.refresh_status)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def save(self):
        try:
            combat_key = normalize_key_name(self.combat_key.get()) or "v"
            attack_key = normalize_key_name(self.attack_key.get()) or "space"
            combo_drain_key = normalize_key_name(self.combo_drain_key.get()) or "x"
            fenrir_key = normalize_key_name(self.fenrir_key.get()) or "c"
            toggle_key = normalize_key_name(self.cfg.get("toggle_key", "F12")) or "F12"

            action_keys = {
                "Combat": combat_key,
                "Attack": attack_key,
                "Combo Drain": combo_drain_key,
                "Fenrir": fenrir_key,
            }
            lowered = [value.lower() for value in action_keys.values()]
            if len(set(lowered)) != len(lowered):
                raise ValueError("Combat, Attack, Combo Drain, and Fenrir trigger keys must all be different.")
            if toggle_key.lower() in lowered:
                raise ValueError("Function trigger keys must be different from the toggle key.")

            self.cfg["combat_enabled"] = bool(self.combat_enabled.get())
            self.cfg["attack_enabled"] = bool(self.attack_enabled.get())
            self.cfg["combo_drain_enabled"] = bool(self.combo_drain_enabled.get())
            self.cfg["fenrir_enabled"] = bool(self.fenrir_enabled.get())
            self.cfg["fenrir_suspend_inputs"] = bool(self.fenrir_suspend_inputs.get())
            self.cfg["combat_key"] = combat_key
            self.cfg["combat_hold"] = max(0.0, float(self.combat_hold.get() or 0.01))
            self.cfg["attack_key"] = attack_key
            self.cfg["attack_hold"] = max(0.0, float(self.attack_hold.get() or 0.01))
            self.cfg["attack_delay"] = max(0.0, float(self.attack_delay.get() or 0.20))
            self.cfg["combo_drain_key"] = combo_drain_key
            self.cfg["combo_drain_down_hold"] = max(
                0.0, float(self.combo_drain_down_hold.get() or 0.01)
            )
            self.cfg["combo_drain_ctrl_hold"] = max(
                0.0, float(self.combo_drain_ctrl_hold.get() or 0.01)
            )
            self.cfg["combo_drain_initial_delay"] = max(
                0.0, float(self.combo_drain_initial_delay.get() or 0.05)
            )
            self.cfg["combo_drain_delay_1"] = max(
                0.0, float(self.combo_drain_delay_1.get() or 0.05)
            )
            self.cfg["combo_drain_delay_2"] = max(
                0.0, float(self.combo_drain_delay_2.get() or 0.05)
            )
            self.cfg["fenrir_key"] = fenrir_key
            self.cfg["fenrir_down_hold"] = max(0.0, float(self.fenrir_down_hold.get() or 0.01))
            self.cfg["fenrir_direction_hold"] = max(0.0, float(self.fenrir_direction_hold.get() or 0.01))
            self.cfg["fenrir_ctrl_hold"] = max(0.0, float(self.fenrir_ctrl_hold.get() or 0.01))
            self.cfg["fenrir_initial_delay"] = max(0.0, float(self.fenrir_initial_delay.get() or 0.05))
            self.cfg["fenrir_delay_1"] = max(0.0, float(self.fenrir_delay_1.get() or 0.05))
            self.cfg["fenrir_delay_2"] = max(0.0, float(self.fenrir_delay_2.get() or 0.05))

            self.cfg["chat_protection_enabled"] = bool(self.chat_protection_enabled.get())
            self.cfg["chat_checks_per_second"] = max(
                1, min(100, int(float(self.chat_checks_per_second.get() or 50)))
            )
            self.cfg["chat_match_threshold"] = max(
                0.50, min(0.9999, float(self.chat_match_threshold.get() or 0.97))
            )

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

            # Save can change physical trigger keys. End only an active Attack
            # hold-loop before rebuilding hooks so its old release cannot be lost.
            stop_attack_loop(wait=False)
            self.rebind_hotkey()
            messagebox.showinfo("Saved", "Settings saved.")
        except Exception as exc:
            messagebox.showerror("Invalid input", f"Check your values: {exc}")

    def on_combat_enabled_changed(self):
        """Enable/disable only Combat. No global rebind and no Attack interaction."""
        enabled = bool(self.combat_enabled.get())
        self.cfg["combat_enabled"] = enabled
        save_config(self.cfg)
        log(f"Combat function {'enabled' if enabled else 'disabled'}")

    def on_attack_enabled_changed(self):
        """Enable/disable only Attack. No global rebind and no Combat interaction."""
        enabled = bool(self.attack_enabled.get())
        self.cfg["attack_enabled"] = enabled

        # Attack is a hold-loop, so disabling Attack stops only its own loop.
        if not enabled:
            stop_attack_loop(wait=False)

        save_config(self.cfg)
        log(f"Attack function {'enabled' if enabled else 'disabled'}")

    def on_combo_drain_enabled_changed(self):
        """Enable/disable only Combo Drain. No Combat or Attack interaction."""
        enabled = bool(self.combo_drain_enabled.get())
        self.cfg["combo_drain_enabled"] = enabled
        save_config(self.cfg)
        log(f"Combo Drain function {'enabled' if enabled else 'disabled'}")

    def on_fenrir_enabled_changed(self):
        enabled = bool(self.fenrir_enabled.get())
        self.cfg["fenrir_enabled"] = enabled
        if not enabled:
            stop_fenrir_direction_restore(self.cfg, wait=False)
        save_config(self.cfg)
        log(f"Fenrir function {'enabled' if enabled else 'disabled'}")

    def on_chat_protection_changed(self):
        enabled = bool(self.chat_protection_enabled.get())
        self.cfg["chat_protection_enabled"] = enabled
        save_config(self.cfg)

        if enabled:
            # Fail-safe until the detector confirms the safe reference image.
            _set_chat_state(False, "Starting", 0.0)
            start_chat_detector(self.cfg)
        else:
            _set_chat_state(True, "Off", 1.0)

        log(f"Chat Protection {'enabled' if enabled else 'disabled'}")

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
        combo_drain_key = normalize_key_name(self.cfg.get("combo_drain_key", "x")) or "x"
        fenrir_key = normalize_key_name(self.cfg.get("fenrir_key", "c")) or "c"
        toggle_key = normalize_key_name(self.cfg.get("toggle_key", "F12")) or "F12"

        # Never permit functions to share the same physical trigger key.
        action_keys = [combat_key.lower(), attack_key.lower(), combo_drain_key.lower(), fenrir_key.lower()]
        if len(set(action_keys)) != len(action_keys) or toggle_key.lower() in action_keys:
            log("Hotkey error: function trigger keys must all be unique and different from the toggle key.")
            return

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
            log("Helper disabled: function keys are unbound and pass through normally.")
            return

        # Keep both action hooks registered while the MASTER helper is ON.
        # The actual function handlers enforce combat_enabled / attack_enabled.
        # This keeps release events reliable and avoids checkbox-driven rebinds.

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
            log(
                f"Combat key bound: {combat_key} | "
                f"function={'ON' if self.cfg.get('combat_enabled', True) else 'OFF'}"
            )
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
            log(
                f"Attack key bound: {attack_key} | "
                f"function={'ON' if self.cfg.get('attack_enabled', True) else 'OFF'}"
            )
        except Exception as exc:
            log(f"Attack hotkey error: {exc}")

        try:
            keyboard.on_press_key(
                combo_drain_key,
                lambda event: handle_combo_drain_key_press(self.cfg),
                suppress=False,
            )
            keyboard.on_release_key(
                combo_drain_key,
                lambda event: handle_combo_drain_key_release(event),
                suppress=False,
            )
            log(
                f"Combo Drain key bound: {combo_drain_key} | "
                f"function={'ON' if self.cfg.get('combo_drain_enabled', True) else 'OFF'}"
            )
        except Exception as exc:
            log(f"Combo Drain hotkey error: {exc}")

        try:
            keyboard.on_press_key(fenrir_key, lambda event: handle_fenrir_key_press(self.cfg), suppress=False)
            keyboard.on_release_key(fenrir_key, lambda event: handle_fenrir_key_release(event), suppress=False)
            log(f"Fenrir key bound: {fenrir_key} | function={'ON' if self.cfg.get('fenrir_enabled', True) else 'OFF'}")
        except Exception as exc:
            log(f"Fenrir hotkey error: {exc}")

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

    def update_chat_status(self):
        if not self.cfg.get("chat_protection_enabled", True):
            self.chat_status.config(text="Off", foreground="gray")
            return

        state = _get_chat_state_snapshot()
        status = state["status"]
        score = state["score"]

        if status == "Safe" and chat_protection_allows(self.cfg):
            self.chat_status.config(
                text=f"Safe ({score:.2f})",
                foreground="green",
            )
        elif status == "Blocked":
            self.chat_status.config(
                text=f"Blocked ({score:.2f})",
                foreground="red",
            )
        elif status == "Game inactive":
            self.chat_status.config(text="Game inactive", foreground="gray")
        else:
            self.chat_status.config(text=status, foreground="orange")

    def refresh_status(self):
        self.update_window_status()
        self.update_toggle_status()
        self.update_chat_status()
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
            stop_fenrir_direction_restore(self.cfg, wait=False)
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
        stop_chat_detector()
        stop_fenrir_direction_restore(self.cfg, wait=True)
        handle_attack_key_release()
        handle_combo_drain_key_release()
        handle_fenrir_key_release()
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