"""Write logcat.txt: about 60000 threadtime lines of head-unit noise with the event below buried in it.

The noise has harmless errors and warnings, a caught exception, and other apps' lines, so a plain grep
for "Exception" or "E/" is misleading. Deterministic (seeded).
"""

import json
import random

EVENT = json.loads('[[3870, "I", "ActivityManager", "Start proc 3870:com.android.car.hvac/u10a41 for activity {com.android.car.hvac/.HvacActivity}"], [3870, "E", "AndroidRuntime", "FATAL EXCEPTION: main"], [3870, "E", "AndroidRuntime", "Process: com.android.car.hvac, PID: 3870"], [3870, "E", "AndroidRuntime", "java.lang.RuntimeException: Unable to start activity ComponentInfo{com.android.car.hvac/com.android.car.hvac.HvacActivity}: java.lang.IllegalStateException: fan level too high: 8"], [3870, "E", "AndroidRuntime", "\\tat android.app.ActivityThread.performLaunchActivity(ActivityThread.java:4164)"], [3870, "E", "AndroidRuntime", "\\tat android.app.ActivityThread.handleLaunchActivity(ActivityThread.java:4322)"], [3870, "E", "AndroidRuntime", "\\tat android.os.Handler.dispatchMessage(Handler.java:107)"], [3870, "E", "AndroidRuntime", "\\tat android.os.Looper.loop(Looper.java:317)"], [3870, "E", "AndroidRuntime", "\\tat android.app.ActivityThread.main(ActivityThread.java:8705)"], [3870, "E", "AndroidRuntime", "Caused by: java.lang.IllegalStateException: fan level too high: 8"], [3870, "E", "AndroidRuntime", "\\tat com.android.car.hvac.HvacController.setFanLevel(HvacController.java:88)"], [3870, "E", "AndroidRuntime", "\\tat com.android.car.hvac.HvacActivity.onCreate(HvacActivity.java:40)"], [3870, "E", "AndroidRuntime", "\\tat android.app.Activity.performCreate(Activity.java:9002)"], [3870, "E", "AndroidRuntime", "\\t... 6 more"], [1301, "I", "ActivityManager", "Process com.android.car.hvac (pid 3870) has died: fg  TOP "]]')
AT = 17321
rng = random.Random(11)
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
    for i in range(60000):
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
