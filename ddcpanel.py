"""
ddcpanel.py - a Monitorian-style tray panel built on monitorcontrol.

    pip install -r requirements.txt
    python ddcpanel.py
"""
import ctypes
import math
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QMenu,
    QPushButton, QSlider, QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget,
)

from monitorcontrol import ColorPreset, get_input_name, get_monitors

APP_NAME = "DDCPanel"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# MCCS VCP codes
LUMINANCE, CONTRAST, VOLUME = 0x10, 0x12, 0x62
MUTE, INPUT, PRESET, POWER = 0x8D, 0x60, 0x14, 0xD6

CONTINUOUS = {"brightness": LUMINANCE, "contrast": CONTRAST, "volume": VOLUME}

SETTERS = {
    "brightness": lambda m, v: m.set_luminance(v),
    "contrast": lambda m, v: m.set_contrast(v),
    "volume": lambda m, v: m.set_volume(v),
    "mute": lambda m, v: m.set_audio_mute_mode(v),
    "input": lambda m, v: m.set_input_source(v),
    # Raw write: set_color_preset() rejects sRGB (0x01) and Native (0x02)
    # because they aren't in the library's ColorPreset enum.
    "preset": lambda m, v: m.vcp.set_vcp_feature(PRESET, v),
    "power": lambda m, v: m.set_power_mode(v),
}


def preset_name(code):
    if code == 0x01:
        return "sRGB"
    if code == 0x02:
        return "Native"
    try:
        name = ColorPreset(code).name.removeprefix("COLOR_TEMP_")
    except ValueError:
        return f"Preset {code:#04x}"
    return name.replace("USER", "User ")


# ---------------------------------------------------------------- Windows helpers

def already_running():
    """Named mutex, so autostart plus a manual launch doesn't give two tray icons."""
    if sys.platform != "win32":
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    already_running.handle = k32.CreateMutexW(None, False, f"Local\\{APP_NAME}")  # keep alive
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def launch_command():
    if getattr(sys, "frozen", False):  # running as the PyInstaller exe
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- hardware side

class Bridge(QObject):
    """Carries results from worker threads back to the UI thread."""
    probed = Signal(int, object)
    values = Signal(int, object)
    error = Signal(int, str)


class MonitorWorker(threading.Thread):
    """
    One thread per display. DDC/CI is slow (tens to hundreds of ms per call),
    so the UI never touches the hardware directly. Pending writes are kept in
    a dict, so while a slider is dragged only the newest value gets sent.
    """
    RETRIES = 3
    MIN_GAP = 0.05  # seconds between writes; many monitors choke on bursts

    def __init__(self, index, monitor, bridge):
        super().__init__(daemon=True, name=f"display-{index}")
        self.index, self.monitor, self.bridge = index, monitor, bridge
        self.supported = None  # set of VCP codes from capabilities, None = unknown
        self.dead = False      # True if the display answered nothing at startup
        self._pending = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = True

    # --- called from the UI thread
    def set(self, feature, value):
        with self._lock:
            self._pending[feature] = value  # latest value wins
        self._wake.set()

    def refresh(self):
        self.set("__refresh__", None)

    def stop(self):
        self._running = False
        self._wake.set()

    # --- worker thread
    def run(self):
        self._probe()
        while self._running:
            self._wake.wait()
            self._wake.clear()
            while self._running:
                with self._lock:
                    if not self._pending:
                        break
                    feature, value = self._pending.popitem()
                if feature == "__refresh__":
                    if not self.dead:  # don't keep poking a display that never answers
                        self.bridge.values.emit(self.index, self._read_values())
                else:
                    self._apply(feature, value)
                    time.sleep(self.MIN_GAP)

    def _has(self, code):
        return self.supported is None or code in self.supported

    def _call(self, fn):
        """Run one hardware call. DDC/CI needs the library's context manager."""
        with self.monitor:
            return fn()

    def _retry(self, fn):
        last = None
        for attempt in range(self.RETRIES):
            try:
                return self._call(fn)
            except Exception as e:  # hardware calls are flaky; a second try often works
                last = e
                time.sleep(0.05 * (attempt + 1))
        raise last

    def _apply(self, feature, value):
        try:
            self._retry(lambda: SETTERS[feature](self.monitor, value))
        except Exception as e:
            self.bridge.error.emit(self.index, f"Couldn't set {feature}: {e}")

    def _read_values(self):
        vals = {}
        for name, code in CONTINUOUS.items():
            if self._has(code):
                try:  # (current, maximum); max isn't always 100
                    vals[name] = self._retry(lambda c=code: self.monitor.vcp.get_vcp_feature(c))
                except Exception:
                    pass
        readers = {
            "mute": (MUTE, lambda: self.monitor.get_audio_mute_mode().value == 0x01),
            "input": (INPUT, self.monitor.get_input_source),
            "preset": (PRESET, lambda: self.monitor.vcp.get_vcp_feature(PRESET)[0]),
        }
        for name, (code, fn) in readers.items():
            if self._has(code):
                try:
                    vals[name] = self._retry(fn)
                except Exception:
                    pass
        return vals

    def _probe(self):
        # On Windows the description is usually "Generic PnP Monitor",
        # so prefer the model name from the capabilities string.
        name = getattr(self.monitor.vcp, "description", "") or f"Monitor {self.index + 1}"
        inputs, presets = [], []
        try:
            caps = self._retry(self.monitor.get_vcp_capabilities)
            if caps.get("model"):
                name = caps["model"]
            if isinstance(caps.get("vcp"), dict) and caps["vcp"]:
                self.supported = set(caps["vcp"])
            inputs = [int(i) for i in caps.get("inputs") or []]
            presets = [int(p) for p in caps.get("color_presets") or []]
        except Exception:
            pass  # some monitors return garbage here; fall back to probing each code
        vals = self._read_values()
        self.dead = not vals
        info = {"name": name, "supported": self.supported, "inputs": inputs, "presets": presets}
        info.update(vals)
        self.bridge.probed.emit(self.index, info)


class LaptopWorker(MonitorWorker):
    """
    A laptop's built-in screen. These don't support DDC/CI; Windows controls
    them through WMI instead, the same interface its own brightness slider uses.
    Reuses MonitorWorker's queue and retry loop and only swaps the hardware calls.
    """
    MIN_GAP = 0.02

    def __init__(self, index, bridge):
        super().__init__(index, None, bridge)
        self._wmi = None
        self._methods = None

    def run(self):
        import pythoncom
        pythoncom.CoInitialize()  # COM (which WMI uses) must be set up per thread
        try:
            super().run()
        finally:
            pythoncom.CoUninitialize()

    def _call(self, fn):
        return fn()  # no DDC/CI context manager needed

    def _apply(self, feature, value):
        if feature != "brightness":
            return
        try:
            self._retry(lambda: self._methods.WmiSetBrightness(Timeout=0, Brightness=value))
        except Exception as e:
            self.bridge.error.emit(self.index, f"Couldn't set brightness: {e}")

    def _read_values(self):
        try:
            # Query fresh each time; a cached WMI object would return a stale value.
            current = self._retry(lambda: self._wmi.WmiMonitorBrightness()[0].CurrentBrightness)
            return {"brightness": (int(current), 100)}
        except Exception:
            return {}

    def _probe(self):
        info = {"name": "Built-in display", "supported": {LUMINANCE},
                "inputs": [], "presets": [], "internal": True}
        try:
            import wmi
            self._wmi = wmi.WMI(namespace="root/WMI")
            self._methods = self._wmi.WmiMonitorBrightnessMethods()[0]
            info.update(self._read_values())
        except Exception:
            pass  # desktop PC with no built-in screen, or WMI unavailable
        self.dead = "brightness" not in info
        self.bridge.probed.emit(self.index, info)


# ---------------------------------------------------------------- UI side

def labeled(text, widget):
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lbl = QLabel(text)
    lbl.setFixedWidth(72)
    lay.addWidget(lbl)
    lay.addWidget(widget, 1)
    return row


class SliderRow(QWidget):
    changed = Signal(int)

    def __init__(self, text):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        label = QLabel(text)
        label.setFixedWidth(72)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setPageStep(10)
        self.readout = QLabel("–")
        self.readout.setFixedWidth(30)
        self.readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(label)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.readout)
        self.slider.valueChanged.connect(self._moved)

    def _moved(self, v):
        self.readout.setText(str(v))
        self.changed.emit(v)

    def set_quiet(self, current, maximum):
        """Update from the display without echoing a write back to it."""
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(maximum, 1))
        self.slider.setValue(current)
        self.slider.blockSignals(False)
        self.readout.setText(str(current))

    def set_percent(self, pct):
        self.slider.setValue(round(pct * self.slider.maximum() / 100))

    def percent(self):
        return round(100 * self.slider.value() / max(self.slider.maximum(), 1))


class MonitorCard(QFrame):
    resized = Signal()

    def __init__(self, worker):
        super().__init__()
        self.setObjectName("card")
        self.has_brightness = False
        self.dead = False
        self.internal = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)

        head = QHBoxLayout()
        self.title = QLabel(f"Display {worker.index + 1}: detecting…")
        self.title.setObjectName("name")
        self.more = QToolButton(text="⋯")
        self.more.setCheckable(True)
        self.more.setEnabled(False)
        head.addWidget(self.title, 1)
        head.addWidget(self.more)
        lay.addLayout(head)

        self.brightness = SliderRow("Brightness")
        self.brightness.setEnabled(False)
        self.brightness.changed.connect(lambda v: worker.set("brightness", v))
        lay.addWidget(self.brightness)

        # Extra controls, hidden behind the ⋯ button
        self.extra = QWidget()
        ex = QVBoxLayout(self.extra)
        ex.setContentsMargins(0, 4, 0, 0)
        self.contrast = SliderRow("Contrast")
        self.contrast.changed.connect(lambda v: worker.set("contrast", v))
        self.volume = SliderRow("Volume")
        self.volume.changed.connect(lambda v: worker.set("volume", v))
        self.input = QComboBox()
        self.input.activated.connect(lambda i: worker.set("input", self.input.itemData(i)))
        self.preset = QComboBox()
        self.preset.activated.connect(lambda i: worker.set("preset", self.preset.itemData(i)))
        self.input_row = labeled("Input", self.input)
        self.preset_row = labeled("Color", self.preset)
        self.mute = QCheckBox("Mute")
        self.mute.toggled.connect(lambda on: worker.set("mute", "on" if on else "off"))
        self.standby = QPushButton("Standby")
        self.standby.setToolTip("Some monitors only wake again when the video signal changes.")
        self.standby.clicked.connect(lambda: worker.set("power", "standby"))
        for w in (self.contrast, self.volume, self.input_row, self.preset_row):
            ex.addWidget(w)
        bottom = QHBoxLayout()
        bottom.addWidget(self.mute)
        bottom.addStretch()
        bottom.addWidget(self.standby)
        ex.addLayout(bottom)
        self.extra.hide()
        lay.addWidget(self.extra)
        self.more.toggled.connect(self._toggle_extra)

        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.hide()
        lay.addWidget(self.status)

    def _toggle_extra(self, on):
        self.extra.setVisible(on)
        self.resized.emit()

    @staticmethod
    def _fill(combo, codes, namer):
        combo.clear()
        for code in codes:
            combo.addItem(namer(code), int(code))

    def apply_info(self, info):
        self.title.setText(info["name"])
        self.internal = bool(info.get("internal"))
        self.has_brightness = "brightness" in info
        self.dead = not self.has_brightness
        self.brightness.setEnabled(self.has_brightness)
        self.brightness.setVisible(self.has_brightness)
        self.contrast.setVisible("contrast" in info)
        self.volume.setVisible("volume" in info)
        self.mute.setVisible("mute" in info)
        self._fill(self.input, info["inputs"] or ([info["input"]] if "input" in info else []), get_input_name)
        self._fill(self.preset, info["presets"] or ([info["preset"]] if "preset" in info else []), preset_name)
        self.input_row.setVisible(self.input.count() > 0)
        self.preset_row.setVisible(self.preset.count() > 0)
        sup = info["supported"]
        can_standby = sup is None or POWER in sup
        self.standby.setVisible(can_standby)
        has_extra = any(("contrast" in info, "volume" in info, "mute" in info,
                         self.input.count() > 0, self.preset.count() > 0, can_standby))
        self.more.setVisible(has_extra)
        self.more.setEnabled(True)
        if self.dead:
            self.show_message("No response over DDC/CI. Make sure DDC/CI is enabled "
                              "in the monitor's on-screen menu.", sticky=True)
        self.apply_values(info)
        self.resized.emit()

    def apply_values(self, vals):
        for name, row in (("brightness", self.brightness),
                          ("contrast", self.contrast),
                          ("volume", self.volume)):
            if name in vals:
                row.set_quiet(*vals[name])
        if "mute" in vals:
            self.mute.blockSignals(True)
            self.mute.setChecked(vals["mute"])
            self.mute.blockSignals(False)
        for name, combo in (("input", self.input), ("preset", self.preset)):
            if name in vals:
                i = combo.findData(vals[name])
                if i >= 0:
                    combo.setCurrentIndex(i)

    def show_message(self, text, sticky=False):
        # Windows error texts can be paragraphs long: show the start, full text on hover.
        short = text if len(text) <= 110 else text[:107].rstrip() + "…"
        self.status.setText(short)
        self.status.setToolTip(text)
        self.status.show()
        self.resized.emit()
        if not sticky:
            QTimer.singleShot(5000, self._clear_message)

    def _clear_message(self):
        self.status.hide()
        self.resized.emit()


class Panel(QWidget):
    rescan_requested = Signal()

    def __init__(self, workers, bridge):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("panel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(360)
        self.workers = workers
        self.hidden_at = 0.0
        self.anchor = None
        self.internal_found = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        self.cards = [MonitorCard(w) for w in workers]
        self.master = None
        if len(self.cards) > 1:
            self.master = SliderRow("All")
            self.master.changed.connect(self._set_all)
            lay.addWidget(self.master)
        for card in self.cards:
            card.resized.connect(lambda: QTimer.singleShot(0, self._reanchor))
            lay.addWidget(card)
        self.empty = QLabel("No controllable displays found.")
        self.empty.setVisible(not self.cards)
        lay.addWidget(self.empty)

        bridge.probed.connect(self._on_probed)
        bridge.values.connect(self._on_values)
        bridge.error.connect(self._on_error)

    def _set_all(self, pct):
        for card in self.cards:
            if card.has_brightness and not card.isHidden():
                card.brightness.set_percent(pct)  # scaled to each display's max

    def _sync_master(self):
        if self.master:
            card = next((c for c in self.cards if c.has_brightness and not c.isHidden()), None)
            if card:
                self.master.set_quiet(card.brightness.percent(), 100)

    def _update_visibility(self):
        for card in self.cards:
            if card.internal and card.dead:
                card.hide()  # desktop PC: there is no built-in screen
            # With a working built-in screen, a display that ignores DDC/CI
            # entirely is almost always that same laptop screen seen twice.
            elif self.internal_found and card.dead and not card.internal:
                card.hide()
        live = [c for c in self.cards if c.has_brightness and not c.isHidden()]
        if self.master:
            self.master.setVisible(len(live) > 1)
        self.empty.setVisible(all(c.isHidden() for c in self.cards))
        QTimer.singleShot(0, self._reanchor)

    @Slot(int, object)
    def _on_probed(self, i, info):
        card = self.cards[i]
        card.apply_info(info)
        if card.internal and not card.dead:
            self.internal_found = True
        self._update_visibility()
        self._sync_master()

    @Slot(int, object)
    def _on_values(self, i, vals):
        self.cards[i].apply_values(vals)
        self._sync_master()

    @Slot(int, str)
    def _on_error(self, i, msg):
        # A failed command usually means Windows invalidated the monitor handles
        # (display switched off/on, cable replugged, Win+P), so reconnect.
        self.cards[i].show_message(f"Reconnecting to displays. {msg}")
        self.rescan_requested.emit()

    def show_near_tray(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()  # excludes the taskbar
        self.anchor = (g.right() - 8, g.bottom() - 8)
        self._reanchor()
        self.show()
        self.activateWindow()
        for w in self.workers:
            w.refresh()  # pick up changes made elsewhere (monitor buttons, Windows slider)

    def _reanchor(self):
        """Keep the bottom-right corner pinned when the panel grows or shrinks."""
        if self.anchor is None:
            return
        self.adjustSize()
        right, bottom = self.anchor
        self.move(right - self.width(), bottom - self.height())

    def hideEvent(self, e):
        self.hidden_at = time.monotonic()
        super().hideEvent(e)


class Controller(QObject):
    """
    Owns the workers and the panel, and rebuilds both when displays change.
    Windows destroys every monitor handle on a display change (a monitor
    switched off and on, a cable replugged, Win+P), so old handles can't be
    reused; the only fix is to enumerate the monitors again.
    """
    SETTLE_MS = 2000       # let a monitor finish waking before talking to it
    ERROR_COOLDOWN = 5.0   # at most one error-triggered rescan per this many seconds

    def __init__(self, app):
        super().__init__()
        self.bridge = None
        self.workers = []
        self.panel = None
        self._last_rebuild = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.SETTLE_MS)
        self._timer.timeout.connect(self.rebuild)
        app.screenAdded.connect(lambda screen: self.schedule_rescan())
        app.screenRemoved.connect(lambda screen: self.schedule_rescan())
        self.rebuild()

    def schedule_rescan(self):
        self._timer.start()  # restarting the timer turns a burst of changes into one rebuild

    def _rescan_after_error(self):
        if time.monotonic() - self._last_rebuild > self.ERROR_COOLDOWN:
            self.schedule_rescan()

    def stop(self):
        for w in self.workers:
            w.stop()
        self.workers = []

    def rebuild(self):
        self._timer.stop()
        was_open = self.panel is not None and self.panel.isVisible()
        self.stop()
        if self.panel is not None:
            self.panel.hide()
            self.panel.deleteLater()

        # A fresh bridge, so late results from the old workers reach nothing.
        self.bridge = Bridge()
        try:
            monitors = get_monitors()
        except Exception as e:
            print(f"Could not enumerate monitors: {e}", file=sys.stderr)
            monitors = []

        # The laptop's built-in screen (if any) comes first, then DDC/CI monitors.
        workers = []
        if sys.platform == "win32":
            workers.append(LaptopWorker(0, self.bridge))
        offset = len(workers)
        workers += [MonitorWorker(offset + i, m, self.bridge) for i, m in enumerate(monitors)]
        self.workers = workers

        self.panel = Panel(workers, self.bridge)
        self.panel.rescan_requested.connect(self._rescan_after_error)
        for w in workers:
            w.start()  # start after the panel is wired up so no probe result is missed
        self._last_rebuild = time.monotonic()
        if was_open:
            self.panel.show_near_tray()


def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    sun = QColor("#ffc83d")
    p.setPen(Qt.NoPen)
    p.setBrush(sun)
    p.drawEllipse(20, 20, 24, 24)
    pen = QPen(sun)
    pen.setWidth(5)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    for k in range(8):
        a = k * math.pi / 4
        p.drawLine(QPointF(32 + 18 * math.cos(a), 32 + 18 * math.sin(a)),
                   QPointF(32 + 27 * math.cos(a), 32 + 27 * math.sin(a)))
    p.end()
    return QIcon(pm)


STYLE = """
QWidget { color: #e8e8e8; font-size: 10pt; }
QWidget#panel { background: #1f1f1f; border: 1px solid #3a3a3a; }
QFrame#card { background: #2b2b2b; border-radius: 6px; }
QLabel#name { font-weight: 600; }
QLabel#status { color: #ff9b8a; font-size: 9pt; }
QSlider::groove:horizontal { height: 4px; background: #555; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #4cc2ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #4cc2ff; width: 14px; margin: -6px 0; border-radius: 7px; }
QSlider::sub-page:horizontal:disabled, QSlider::handle:horizontal:disabled { background: #666; }
QToolButton, QPushButton, QComboBox { background: #3a3a3a; border: none; border-radius: 4px; padding: 3px 8px; }
QToolButton:checked, QPushButton:hover, QComboBox:hover { background: #4a4a4a; }
QComboBox QAbstractItemView { background: #2b2b2b; selection-background-color: #3d6e8a; }
"""


def main():
    if already_running():
        return

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)

    controller = Controller(app)

    tray = QSystemTrayIcon(make_icon())
    tray.setToolTip("DDC Panel")
    menu = QMenu()
    menu.addAction("Rescan displays", controller.rebuild)
    if sys.platform == "win32":
        autostart = menu.addAction("Start with Windows")
        autostart.setCheckable(True)
        autostart.setChecked(autostart_enabled())
        if autostart.isChecked():
            set_autostart(True)  # refresh the stored path in case the exe was moved
        autostart.toggled.connect(set_autostart)
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)

    def on_tray(reason):
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        panel = controller.panel
        if panel.isVisible():
            panel.hide()
        # Clicking the icon while the popup is open closes it first; don't reopen it.
        elif time.monotonic() - panel.hidden_at > 0.3:
            panel.show_near_tray()

    tray.activated.connect(on_tray)
    tray.show()
    app.aboutToQuit.connect(controller.stop)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()