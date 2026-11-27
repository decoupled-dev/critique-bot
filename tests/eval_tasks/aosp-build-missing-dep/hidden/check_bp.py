"""Pass when CarHvacApp links car-hvac-common statically and nothing else changed."""

import re
import sys

problems = []
bp = open("packages/apps/CarHvac/Android.bp", encoding="utf-8").read()
block = bp[bp.find('name: "CarHvacApp"'):]
libs = re.search(r"static_libs\s*:\s*\[([^\]]*)\]", block)
names = re.findall(r'"([^"]+)"', libs.group(1)) if libs else []
if "car-hvac-common" not in names:
    problems.append("CarHvacApp's static_libs do not include car-hvac-common")
if "androidx.annotation_annotation" not in names:
    problems.append("an existing static_libs entry was removed")
if re.search(r"(?<!static_)\blibs\s*:\s*\[[^\]]*car-hvac-common", block):
    problems.append("car-hvac-common is in libs (compile only), not static_libs: the app would crash at runtime")
if open("frameworks/hvac-common/Android.bp", encoding="utf-8").read() != '// Copyright (C) 2026 The Android Open Source Project\n// SPDX-License-Identifier: Apache-2.0\n\njava_library {\n    name: "car-hvac-common",\n    srcs: ["src/**/*.java"],\n    sdk_version: "system_current",\n}\n':
    problems.append("frameworks/hvac-common/Android.bp was changed")
if "import com.android.car.hvac.common.FanLevels;" not in open("packages/apps/CarHvac/src/com/android/car/hvac/FanPanel.java", encoding="utf-8").read():
    problems.append("FanPanel.java no longer imports FanLevels (the build graph is the bug, not the code)")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("ok")
