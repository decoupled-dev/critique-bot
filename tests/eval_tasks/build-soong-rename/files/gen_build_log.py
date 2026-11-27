"""Write build.log: a long Soong/ninja output of m that fails in CarHvac's javac step.

Only the first javac error of the module is in the log (the step stops at it in this
build), plus deprecation warnings and an unrelated retried FAILED: step earlier on.
"""

import random

rng = random.Random(17)
MODULES = ["framework-minus-apex", "services.core", "CarService", "android.car", "SystemUI", "CarSystemUI",
           "hvac-common", "car-frameworks-service", "libbinder", "CarLauncher", "CarSettings"]

with open("build.log", "w", encoding="utf-8") as out:
    out.write("============================================\n")
    out.write("PLATFORM_VERSION_CODENAME=REL\nTARGET_PRODUCT=sdk_car_x86_64\nTARGET_BUILD_VARIANT=userdebug\n")
    out.write("============================================\n")
    total = 158_220
    for step in range(1, total):
        module = rng.choice(MODULES)
        kind = rng.choice(["javac", "turbine", "d8", "r8", "aapt2 compile", "soong_zip", "clang++"])
        out.write(f"[{100 * step // total:3d}% {step}/{total}] //{module}:{module} {kind} {module}.jar\n")
        if step % 1009 == 0:
            out.write(f"packages/apps/Car/Settings/src/com/android/car/settings/Foo{step}.java:{rng.randint(10, 900)}: "
                      "warning: [deprecation] getColor(int) in Resources has been deprecated\n")
        if step == 81_000:
            out.write("FAILED: out/soong/.intermediates/prebuilts/remote/fetch.stamp (retrying, attempt 2 of 3)\n")
            out.write("[ 51% 81001/158220] retry succeeded\n")
    out.write(f"[ 99% {total}/{total + 6}] //packages/apps/CarHvac:CarHvac javac CarHvac.jar\n")
    out.write("FAILED: out/soong/.intermediates/packages/apps/CarHvac/CarHvac/android_common/javac/CarHvac.jar\n")
    out.write("out/soong/host/linux-x86/bin/soong_javac_wrapper ... (command line omitted)\n")
    out.write("packages/apps/CarHvac/src/com/example/hvac/ui/FanPanel.java:15: error: cannot find symbol\n")
    out.write("        mShown = mFan.setSpeed(mFan.getFanLevel() + 1);\n")
    out.write("                     ^\n")
    out.write("  symbol:   method setSpeed(int)\n")
    out.write("  location: variable mFan of type FanController\n")
    out.write("1 error\n")
    out.write("17:05:44 ninja failed with: exit status 1\n")
    out.write("\n#### failed to build some targets (21:07 (mm:ss)) ####\n")
