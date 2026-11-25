"""Write logcat.txt: about 400k threadtime lines, with one app crash buried in the middle.

The noise includes long lines (some over 20k characters), a caught exception that is
only a warning, and an AndroidRuntime error from another app that is not a crash, so a
naive grep is misleading.
"""

import random

rng = random.Random(7)
TAGS = ["CarService", "VHAL", "ActivityManager", "CarPropertyService", "AudioFlinger", "WindowManager",
        "chatty", "SurfaceFlinger", "InputDispatcher", "CarPowerManagementService", "BluetoothAdapter"]
LEVELS = "VDIIIIWE"
CRASH = [
    "FATAL EXCEPTION: main",
    "Process: com.example.hvac, PID: 5511",
    "java.lang.NullPointerException: Attempt to invoke virtual method "
    "'float android.car.hardware.CarPropertyValue.getValue()' on a null object reference",
    "\tat com.example.hvac.HvacPanelController.onTemperatureChanged(HvacPanelController.java:142)",
    "\tat com.example.hvac.HvacPanelController$1.onChangeEvent(HvacPanelController.java:88)",
    "\tat android.car.hardware.property.CarPropertyManager$CarPropertyEventListenerToService"
    ".lambda$onEvent$0(CarPropertyManager.java:540)",
    "\tat android.os.Handler.handleCallback(Handler.java:959)",
    "\tat android.os.Looper.loop(Looper.java:317)",
]


def stamp(i: int) -> str:
    seconds = i // 40
    return f"11-25 10:{(seconds // 60) % 60:02d}:{seconds % 60:02d}.{i % 1000:03d}"


with open("logcat.txt", "w", encoding="utf-8") as out:
    for i in range(400_000):
        pid = rng.choice([1234, 1301, 2210, 3377, 4410])
        tag = rng.choice(TAGS)
        level = rng.choice(LEVELS)
        if i % 9973 == 0:
            msg = "onPropertyEvent payload=" + "ab" * rng.randint(5000, 12000)
        elif i == 120_000:
            level, tag = "W", "System.err"
            msg = "java.lang.NullPointerException: caught and handled in com.example.media.Sync (retrying)"
        elif i == 150_000:
            level, tag = "E", "AndroidRuntime"
            msg = "Process com.example.radio reported a slow binder call; recovered"
        else:
            msg = f"event {rng.randint(0, 99999)} prop=0x{rng.randint(0, 0xFFFFFFF):08x} area={rng.randint(0, 5)}"
        out.write(f"{stamp(i)}  {pid}  {pid + 7} {level} {tag}: {msg}\n")
        if i == 233_117:
            for line in CRASH:
                out.write(f"{stamp(i)}  5511  5511 E AndroidRuntime: {line}\n")
