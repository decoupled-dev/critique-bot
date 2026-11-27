"""Write logcat.txt: about 30000 threadtime lines of head-unit noise with the event below buried in it.

The noise has harmless errors and warnings, a caught exception, and other apps' lines, so a plain grep
for "Exception" or "E/" is misleading. Deterministic (seeded).
"""

import json
import random

EVENT = json.loads('[[1301, "E", "ActivityManager", "ANR in com.android.car.radio (com.android.car.radio/.RadioActivity)"], [1301, "E", "ActivityManager", "PID: 5021"], [1301, "E", "ActivityManager", "Reason: Input dispatching timed out (b1e2f3a com.android.car.radio/com.android.car.radio.RadioActivity (server) is not responding. Waited 5001ms for FocusEvent(hasFocus=true))"], [1301, "E", "ActivityManager", "Load: 4.1 / 3.9 / 3.2"]]')
AT = 12000
rng = random.Random(14)
TAGS = ["CarService", "VHAL", "ActivityManager", "CarPropertyService", "AudioFlinger", "WindowManager", "chatty",
        "SurfaceFlinger", "InputDispatcher", "CarPowerManagementService", "BluetoothAdapter", "CarMediaService",
        "CarUserService", "PackageManager"]
LEVELS = "VDIIIIIWE"
NOISE = [
    "event {n} prop=0x{h} area={a}",
    "onPropertyEvent id=0x{h} status=AVAILABLE",
    "Skipped {a} frames!  The application may be doing too much work on its main thread.",
    "Failed to get property 0x{h}: status=NOT_AVAILABLE (retry {a})",
    "binder transaction took {n}ms on interface android.car.ICar",
]


def stamp(i):
    seconds = i // 40
    return f"11-25 10:{(seconds // 60) % 60:02d}:{seconds % 60:02d}.{i % 1000:03d}"


with open("logcat.txt", "w", encoding="utf-8") as out:
    for i in range(30000):
        pid = rng.choice([1234, 1301, 2210, 3377, 4410])
        tag = rng.choice(TAGS)
        level = rng.choice(LEVELS)
        msg = rng.choice(NOISE).format(n=rng.randint(0, 99999), h=f"{rng.randint(0, 0xFFFFFFF):08x}", a=rng.randint(0, 9))
        if i == AT // 3:
            level, tag = "W", "System.err"
            msg = "java.io.IOException: caught and handled in com.android.car.media.MediaSync (retrying)"
        out.write(f"{stamp(i)}  {pid}  {pid + 7} {level} {tag}: {msg}\n")
        if i == AT:
            for pid, level, tag, line in EVENT:
                out.write(f"{stamp(i)}  {pid}  {pid} {level} {tag}: {line}\n")
