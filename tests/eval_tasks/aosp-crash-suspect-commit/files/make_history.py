"""Build a small git history for packages/services/Car/service and write logcat.txt with the crash.

Commits (oldest first) all touch CarPowerManagementService.java or its folder; the one that
introduced the crash is not the newest one.
"""

import os
import subprocess

FILE = "packages/services/Car/service/src/com/android/car/power/CarPowerManagementService.java"
VHAL = "packages/services/Car/service/src/com/android/car/hal/VehicleHal.java"


def body(extra_init, handler_line, extra_tail=""):
    lines = ["package com.android.car.power;", "", "public class CarPowerManagementService {",
             "    private GarageModeHandler mGarageModeHandler;", "    private boolean mShutdownPending;", ""]
    lines += ["    void init() {"] + extra_init + ["    }", ""]
    while len(lines) < 59:
        lines.append("")
    lines += ["    void handleShutdownPrepare(boolean garageMode) {",
              "        if (!garageMode) {",
              "            finishShutdownPrepare();",
              "            return;",
              "        }",
              handler_line,
              "    }", ""]
    lines += ["    void finishShutdownPrepare() {}", extra_tail, "}"]
    return "\n".join(lines) + "\n"


def commit(message, day, author):
    env = dict(os.environ, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=author.lower().replace(" ", ".") + "@example.com",
               GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL="ci@example.com",
               GIT_AUTHOR_DATE=f"2026-11-{day:02d}T10:00:00", GIT_COMMITTER_DATE=f"2026-11-{day:02d}T10:00:00")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], check=True, env=env)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as out:
        out.write(text)


write(FILE, body(["        mGarageModeHandler = new GarageModeHandler();"],
                 "        mGarageModeHandler.start();"))
write(VHAL, "package com.android.car.hal;\n\npublic class VehicleHal {\n    static final int RETRIES = 3;\n}\n")
commit("power: start garage mode from shutdown prepare", 10, "Ana Ruiz")
write(FILE, body(["        mGarageModeHandler = new GarageModeHandler();"],
                 "        mGarageModeHandler.start();", "    // shutdown prepare timeout: 15 s"))
commit("power: add shutdown prepare timeout", 14, "Ben Okafor")
write(FILE, body(["        if (!mShutdownPending) {", "            mGarageModeHandler = new GarageModeHandler();", "        }"],
                 "        mGarageModeHandler.start();", "    // shutdown prepare timeout: 15 s"))
commit("power: skip garage mode setup when shutdown is pending", 18, "Chen Wei")
write(VHAL, "package com.android.car.hal;\n\npublic class VehicleHal {\n    static final int RETRIES = 5;\n}\n")
commit("vhal: retry property reads five times", 21, "Dana Iyer")
write(FILE, body(["        if (!mShutdownPending) {", "            mGarageModeHandler = new GarageModeHandler();", "        }"],
                 "        mGarageModeHandler.start();", "    // shutdown prepare timeout: 15 s (see go/cpms)"))
commit("power: document the shutdown prepare timeout", 22, "Ben Okafor")

crash = [
    "*** FATAL EXCEPTION IN SYSTEM PROCESS: CarServiceMainThread",
    "Process: com.android.car, PID: 1450",
    "java.lang.NullPointerException: Attempt to invoke virtual method 'void com.android.car.power.GarageModeHandler.start()' on a null object reference",
    "\tat com.android.car.power.CarPowerManagementService.handleShutdownPrepare(CarPowerManagementService.java:65)",
    "\tat com.android.car.power.CarPowerManagementService$PowerHandler.handleMessage(CarPowerManagementService.java:2210)",
    "\tat android.os.Handler.dispatchMessage(Handler.java:107)",
]
with open("logcat.txt", "w", encoding="utf-8") as out:
    for i in range(3000):
        out.write(f"11-25 10:{i // 600:02d}:{(i // 10) % 60:02d}.{i % 1000:03d}  1450  1458 I CarPowerManagementService: state tick {i}\n")
        if i == 2100:
            for line in crash:
                out.write(f"11-25 10:03:30.100  1450  1450 E AndroidRuntime: {line}\n")
