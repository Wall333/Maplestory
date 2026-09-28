"""Inventory comparison and sequential shop workflow."""
import os
import threading
import time


class ShopController:
    def __init__(self, app, api):
        self.app, self.api = app, api
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.state = "idle"
        self.test_cycle = False
        self.status = "Off"
        self.inventory_rect = None
        self.context = None
        self.changed = False
        self.matches = {}
        self.checked = 0.0
        self.next_action = 0.0
        self.shop_open_checked = 0.0
        self.shop_open_rect = None
        self.templates = {}
        self.thread = threading.Thread(target=self.detect, daemon=True, name="shop-detector")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)

    def reset(self):
        self.state = "idle"
        self.test_cycle = False
        self.next_action = 0.0

    def request_test(self):
        if self.state != "idle":
            return False
        self.test_cycle = True
        self.state = "open"
        self.next_action = 0.0
        return True

    def detect(self):
        cv, np, mss = (self.api[k] for k in ("cv2", "np", "mss"))
        if cv is None or np is None or mss is None:
            self.status = "Missing vision dependencies"
            return
        names = ("inventory", "shop", "shop_open", "sell_button", "sell_confirm", "invent_empty", "shop_exit")
        for name in names:
            template = cv.imread(os.path.join(self.api["BASE_DIR"], "assets", name + ".png"))
            if template is None:
                self.status = "Missing " + name + ".png"
                return
            self.templates[name] = template
        try:
            with mss.mss() as sct:
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    rate = max(1.0, min(30.0, float(self.app.config.get("inventory_checks_per_second", 5))))
                    if not (self.app.config.get("shop_enabled") or self.app.config.get("inventory_debug")
                            or self.app.spam_active or self.test_cycle):
                        self.status = "Off"
                        self.stop_event.wait(.2)
                        continue
                    context = self.api["get_active_dreamms_context"]()
                    if context is None:
                        with self.lock:
                            self.context = None
                            self.inventory_rect = None
                            self.changed = False
                            self.shop_open_rect = None
                            self.shop_open_checked = 0.0
                        self.status = "Game inactive"
                        self.stop_event.wait(.2)
                        continue
                    frame = self.api["VosMapDetector"]._capture(sct, context[1])
                    threshold = float(self.app.config.get("shop_match_threshold", .9))
                    matches = {}
                    for name, template in self.templates.items():
                        if name == "shop_open":
                            shop_rate = max(.2, min(10.0, float(self.app.config.get("shop_open_checks_per_second", 1))))
                            if started - self.shop_open_checked < 1.0 / shop_rate:
                                if self.shop_open_rect is not None:
                                    matches[name] = self.shop_open_rect
                                continue
                            self.shop_open_checked = started
                            self.shop_open_rect = None
                        h, w = template.shape[:2]
                        if frame.shape[0] < h or frame.shape[1] < w:
                            continue
                        result = cv.matchTemplate(frame, template, cv.TM_CCOEFF_NORMED)
                        _, score, _, loc = cv.minMaxLoc(result)
                        if score >= threshold:
                            matches[name] = (loc[0], loc[1], w, h)
                            if name == "shop_open":
                                self.shop_open_rect = matches[name]
                    inventory = self.templates["inventory"]
                    ih, iw = inventory.shape[:2]
                    # The header does not change when inventory slots fill.
                    header = inventory[:min(35, ih), :, :]
                    rect = None
                    changed = False
                    if frame.shape[0] >= ih and frame.shape[1] >= iw:
                        result = cv.matchTemplate(frame, header, cv.TM_CCOEFF_NORMED)
                        _, score, _, loc = cv.minMaxLoc(result)
                        x, y = loc
                        if score >= float(self.app.config.get("inventory_detection_threshold", .9)) and y + ih <= frame.shape[0]:
                            roi = frame[y:y + ih, x:x + iw]
                            # Compare slots, excluding header and bottom controls.
                            grid = inventory[40:ih-30]
                            if grid.size:
                                similarity = float(cv.matchTemplate(roi[40:ih-30], grid, cv.TM_CCOEFF_NORMED)[0, 0])
                                changed = similarity < float(self.app.config.get("inventory_match_threshold", .97))
                            rect = (x, y, iw, ih)
                    with self.lock:
                        self.context, self.matches = context, matches
                        self.inventory_rect, self.changed = rect, changed
                        self.checked = time.monotonic()
                    self.status = "Inventory changed" if changed else ("Inventory clean" if rect else "Inventory not detected")
                    self.stop_event.wait(max(0.0, 1.0 / rate - (time.monotonic() - started)))
        except Exception as exc:
            self.status = "Detector error: " + str(exc)

    def click(self, rect, context):
        if self.api["get_active_dreamms_context"]() != context or self.app.stop_event.is_set():
            return False
        x, y, w, h = rect
        left, top, _, _ = context[1]
        user32 = self.api["ctypes"].windll.user32
        user32.SetCursorPos(left + x + w // 2, top + y + h // 2)
        if self.app.config.get("use_arduino", True):
            return self.api["ARDUINO"].send_command(self.app.config, "CLICK", .03)
        user32.mouse_event(2, 0, 0, 0, 0)
        user32.mouse_event(4, 0, 0, 0, 0)
        return True

    def tick(self):
        """Called by the spam worker; return True while selling owns inputs."""
        auto_sell = bool(self.app.config.get("shop_enabled", False))
        with self.lock:
            context, changed, checked = self.context, self.changed, self.checked
            matches = dict(self.matches)
            inventory_rect = self.inventory_rect
        rate = max(1.0, min(30.0, float(self.app.config.get("inventory_checks_per_second", 5))))
        shop_rate = max(.2, min(10.0, float(self.app.config.get("shop_open_checks_per_second", 1))))
        max_age = max(.6, 2.0 / rate, 1.0 / shop_rate + .2)
        if self.state == "idle":
            if context is None or time.monotonic() - checked > max_age:
                return False
            if "shop_open" in matches:
                self.state = "inspect"
                self.api["log"]("Shop already open: checking inventory")
            elif changed and auto_sell:
                self.state = "open"
                self.api["log"]("Selling: inventory changed; opening shop")
            else:
                return False
        self.app.spam_paused_reason = "Selling: " + self.state
        if self.app.stop_event.is_set() or self.api["get_active_dreamms_context"]() != context:
            return True
        if context is None or time.monotonic() - checked > max_age or time.monotonic() < self.next_action:
            return True
        target = None
        if self.state == "inspect":
            if "shop_open" not in matches:
                self.reset()
                return False
            if "invent_empty" in matches or "inventory" in matches or (inventory_rect is not None and not changed):
                self.state = "exit"
                self.api["log"]("Shop open with clean/empty inventory: closing shop")
            elif changed:
                self.state = "sell" if auto_sell and self.app.config.get("shop_open_auto_sell", True) else "exit"
                self.api["log"]("Shop open with changed inventory: " + ("selling" if self.state == "sell" else "closing shop"))
        elif self.state == "open":
            if "shop_open" in matches:
                self.state = "sell"
            elif changed or self.test_cycle:
                target = matches.get("shop")
            else:
                self.reset()
                return False
        elif self.state == "sell":
            if "sell_confirm" in matches:
                self.state = "confirm"
            elif "shop_open" in matches:
                target = matches.get("sell_button")
        elif self.state == "confirm":
            if "sell_confirm" in matches:
                if self.app.config.get("use_arduino", True):
                    sent = self.api["ARDUINO"].send_key(self.app.config, "Y", .03)
                else:
                    self.api["send_windows_key"]("Y", .03)
                    sent = True
                if not sent:
                    self.app.stop_vos("Selling: confirmation key failed")
                self.next_action = time.monotonic() + 1
            elif "shop_open" in matches and "invent_empty" in matches:
                self.state = "exit"
        elif self.state == "exit":
            if "shop_open" not in matches:
                self.reset()
                self.api["log"]("Selling complete: shop closed")
                return False
            target = matches.get("shop_exit")
        if target is not None:
            if not self.click(target, context):
                self.app.stop_vos("Selling: click failed")
            self.next_action = time.monotonic() + 1
        return True
