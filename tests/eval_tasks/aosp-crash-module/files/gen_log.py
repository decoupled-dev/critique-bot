"""Write logcat.txt: about 40000 threadtime lines of head-unit noise with the event below buried in it.

The noise has harmless errors and warnings, a caught exception, and other apps' lines, so a plain grep
for "Exception" or "E/" is misleading. Deterministic (seeded).
"""

import json
import random

EVENT = json.loads('[[1450, "E", "AndroidRuntime", "*** FATAL EXCEPTION IN SYSTEM PROCESS: CarServiceMainThread"], [1450, "E", "AndroidRuntime", "Process: com.android.car, PID: 1450"], [1450, "E", "AndroidRuntime", "java.lang.NullPointerException: Attempt to invoke virtual method \'com.android.car.power.PowerComponentHandler$Policy com.android.car.power.PowerComponentHandler.getAccumulatedPolicy()\' on a null object reference"], [1450, "E", "AndroidRuntime", "\\tat com.android.car.power.CarPowerManagementService.handleWaitForVhal(CarPowerManagementService.java:412)"], [1450, "E", "AndroidRuntime", "\\tat com.android.car.power.CarPowerManagementService.doHandlePowerStateChange(CarPowerManagementService.java:388)"], [1450, "E", "AndroidRuntime", "\\tat com.android.car.power.CarPowerManagementService$PowerHandler.handleMessage(CarPowerManagementService.java:2210)"], [1450, "E", "AndroidRuntime", "\\tat android.os.Handler.dispatchMessage(Handler.java:107)"], [1450, "E", "AndroidRuntime", "\\tat android.os.Looper.loop(Looper.java:317)"], [1450, "E", "AndroidRuntime", "\\tat android.os.HandlerThread.run(HandlerThread.java:85)"]]')
AT = 2400
rng = random.Random(12)
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
    for i in range(40000):
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
