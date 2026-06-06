import ctypes
import sys
import threading
import time

import hid
import serial
from PIL import Image, ImageDraw, ImageFont
import pystray

# ==========================================================
# SINGLE INSTANCE LOCK
# ==========================================================

ERROR_ALREADY_EXISTS = 183

def acquire_single_instance(name="DualSenseBatteryTray_SingleInstance"):

    handle = ctypes.windll.kernel32.CreateMutexW(
        None,
        False,
        name
    )

    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:

        ctypes.windll.kernel32.CloseHandle(handle)

        return None

    return handle

# ==========================================================
# CONTROLLER CONFIG
# ==========================================================

SONY_VENDOR_ID = 0x054C
DUALSENSE_PRODUCT_ID = 0x0CE6

BATTERY_STATUS = {
    0x0: "Discharging",
    0x1: "Charging",
    0x2: "Fully Charged",
    0xa: "Voltage Error",
    0xb: "Temp Error",
}

REFRESH_SEC = 5

# ==========================================================
# SERIAL CONFIG
# ==========================================================

# CHANGE THIS TO YOUR ESP32 PORT
SERIAL_PORT = "COM5"

BAUD_RATE = 115200

esp = None

# ==========================================================
# COLORS
# ==========================================================

GREEN = (74, 194, 138)
YELLOW = (224, 167, 60)
RED = (226, 92, 92)
GREY = (130, 130, 130)
WHITE = (240, 240, 240)

# ==========================================================
# CONNECT TO ESP32
# ==========================================================

def connect_serial():

    global esp

    try:

        esp = serial.Serial(
            SERIAL_PORT,
            BAUD_RATE,
            timeout=1
        )

        time.sleep(2)

        print(f"Connected to ESP32 on {SERIAL_PORT}")

    except Exception as e:

        print("ESP32 connection failed:")
        print(e)

        esp = None

# ==========================================================
# READ BATTERY
# ==========================================================

def read_battery():

    device = hid.device()

    device.open(
        SONY_VENDOR_ID,
        DUALSENSE_PRODUCT_ID
    )

    device.set_nonblocking(False)

    try:

        try:
            device.get_feature_report(0x05, 41)

        except OSError:
            pass

        report = None

        for _ in range(20):

            data = device.read(128, timeout_ms=1000)

            if not data:
                continue

            if data[0] == 0x31 or (
                data[0] == 0x01 and len(data) >= 54
            ):

                report = data
                break

        if not report:

            return None, None, None, "No controller data"

        report_id = report[0]

        if report_id == 0x01:

            battery_byte = report[53]
            connection = "USB"

        elif report_id == 0x31:

            battery_byte = report[54]
            connection = "Bluetooth"

        else:

            return None, None, None, (
                f"Unknown report 0x{report_id:02X}"
            )

        level_raw = battery_byte & 0x0F

        status_nibble = (battery_byte >> 4) & 0x0F

        percent = min(level_raw * 10, 100)

        status = BATTERY_STATUS.get(
            status_nibble,
            f"Unknown (0x{status_nibble:X})"
        )

        return connection, percent, status, None

    finally:

        device.close()

# ==========================================================
# BATTERY COLOR
# ==========================================================

def battery_color(percent):

    if percent is None:
        return GREY

    if percent > 30:
        return GREEN

    if percent > 15:
        return YELLOW

    return RED

# ==========================================================
# SYSTEM TRAY ICON
# ==========================================================

def make_icon(percent, charging=False):

    size = 64

    img = Image.new(
        "RGBA",
        (size, size),
        (0, 0, 0, 0)
    )

    d = ImageDraw.Draw(img)

    # Battery body

    body = (6, 18, 52, 46)
    nub = (52, 26, 58, 38)

    d.rectangle(body, outline=WHITE, width=3)
    d.rectangle(nub, fill=WHITE)

    # Battery fill

    if percent is not None and percent > 0:

        inner_left  = 9
        inner_top   = 21
        inner_right = 49
        inner_bot   = 43

        fill_w = int(
            (inner_right - inner_left)
            * percent / 100
        )

        if fill_w > 0:

            d.rectangle(
                (
                    inner_left,
                    inner_top,
                    inner_left + fill_w,
                    inner_bot
                ),
                fill=battery_color(percent)
            )

    # Percentage label

    label = "?" if percent is None else str(percent)

    try:

        font = ImageFont.truetype(
            "segoeuib.ttf",
            18
        )

    except OSError:

        font = ImageFont.load_default()

    bbox = d.textbbox(
        (0, 0),
        label,
        font=font
    )

    tw = bbox[2] - bbox[0]

    d.text(
        ((size - tw) // 2, 46),
        label,
        fill=WHITE,
        font=font
    )

    # Charging bolt

    if charging:

        bolt = [
            (30, 22),
            (24, 34),
            (30, 34),
            (26, 42),
            (36, 30),
            (30, 30),
            (34, 22)
        ]

        d.polygon(
            bolt,
            fill=(255, 215, 64)
        )

    return img

# ==========================================================
# SEND DATA TO ESP32
# ==========================================================

def send_to_esp32(percent, status, connection):

    global esp

    if esp is None:
        return

    try:

        message = (
            f"{percent},{status},{connection}\n"
        )

        esp.write(message.encode())

    except Exception as e:

        print("ESP32 send failed:")
        print(e)

# ==========================================================
# TRAY APPLICATION
# ==========================================================

class TrayApp:

    def __init__(self):

        self.icon = pystray.Icon(
            "dualsense_battery",

            make_icon(None),

            "DualSense Battery",

            menu=pystray.Menu(

                pystray.MenuItem(
                    "Refresh now",
                    self.on_refresh
                ),

                pystray.MenuItem(
                    "Quit",
                    self.on_quit
                ),
            ),
        )

        self._stop = threading.Event()

    # ======================================================
    # UPDATE BATTERY
    # ======================================================

    def update(self):

        try:

            connection, percent, status, error = (
                read_battery()
            )

        except OSError as e:

            connection = None
            percent = None
            status = None

            error = str(e)

        if error:

            self.icon.icon = make_icon(None)

            hint = error

            if (
                "access" in hint.lower()
                or "open" in hint.lower()
                or "unable" in hint.lower()
            ):

                hint = (
                    "Controller not found "
                    "(close Steam/DS4Windows?)"
                )

            self.icon.title = f"DualSense: {hint}"

            return

        charging = status in (
            "Charging",
            "Fully Charged"
        )

        # Update tray icon

        self.icon.icon = make_icon(
            percent,
            charging=charging
        )

        self.icon.title = (
            f"DualSense: "
            f"{percent}% • "
            f"{status} • "
            f"{connection}"
        )

        # Send to ESP32

        send_to_esp32(
            percent,
            status,
            connection
        )

        print(
            f"Sent to ESP32: "
            f"{percent}% "
            f"{status} "
            f"{connection}"
        )

    # ======================================================
    # MENU ACTIONS
    # ======================================================

    def on_refresh(self, icon, item):

        threading.Thread(
            target=self.update,
            daemon=True
        ).start()

    def on_quit(self, icon, item):

        self._stop.set()

        icon.stop()

    # ======================================================
    # UPDATE LOOP
    # ======================================================

    def _loop(self):

        while not self._stop.is_set():

            self.update()

            self._stop.wait(REFRESH_SEC)

    # ======================================================
    # RUN APP
    # ======================================================

    def run(self):

        threading.Thread(
            target=self._loop,
            daemon=True
        ).start()

        self.icon.run()

# ==========================================================
# MAIN
# ==========================================================

if __name__ == "__main__":

    _lock = acquire_single_instance()

    if _lock is None:

        sys.exit(0)

    connect_serial()

    TrayApp().run()