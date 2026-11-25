"""Write build.log: a long Soong/ninja build output with one real error near the end.

Warnings and a "FAILED:" line of a different, later-retried step are mixed in, and
the error that matters is about 140k lines down.
"""

import random

rng = random.Random(3)
MODULES = ["libcar-framework-service", "CarService", "android.car", "car-frameworks-service",
           "libbinder", "framework-minus-apex", "services.core", "SystemUI", "CarSystemUI"]

with open("build.log", "w", encoding="utf-8") as out:
    out.write("============================================\n")
    out.write("PLATFORM_VERSION_CODENAME=REL\nTARGET_PRODUCT=sdk_car_x86_64\nTARGET_BUILD_VARIANT=userdebug\n")
    out.write("============================================\n")
    total = 141_380
    for step in range(1, total):
        module = rng.choice(MODULES)
        out.write(f"[{100 * step // total:3d}% {step}/{total}] //{module}:{module} javac {module}.jar\n")
        if step % 997 == 0:
            out.write(f"frameworks/base/core/java/android/os/Foo{step}.java:{rng.randint(10, 900)}: "
                      "warning: [deprecation] getColor(int) in Resources has been deprecated\n")
        if step == 70_000:
            out.write("FAILED: out/soong/.intermediates/flaky/download.stamp (retrying, attempt 2 of 3)\n")
            out.write("[ 49% 70001/141380] retry succeeded\n")
    out.write(f"[ 99% {total}/{total + 4}] //packages/services/Car/service:CarService javac CarService.jar\n")
    out.write("FAILED: out/soong/.intermediates/packages/services/Car/service/CarService/android_common/javac/CarService.jar\n")
    out.write("out/soong/host/linux-x86/bin/soong_javac_wrapper ... (command line omitted)\n")
    out.write("frameworks/car/src/HvacTemperatureHelper.java:28: error: cannot find symbol\n")
    out.write("        return mCarPropertyManger.getProperty(\n")
    out.write("               ^\n")
    out.write("  symbol:   variable mCarPropertyManger\n")
    out.write("  location: class HvacTemperatureHelper\n")
    out.write("1 error\n")
    out.write("16:42:07 ninja failed with: exit status 1\n")
    out.write("\n#### failed to build some targets (12:31 (mm:ss)) ####\n")
