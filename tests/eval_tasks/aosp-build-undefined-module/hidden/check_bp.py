"""Pass when CarRadioApp depends on car-radio-lib (the renamed module) and the library is unchanged."""

import re
import sys

problems = []
bp = open("packages/apps/Car/Radio/Android.bp", encoding="utf-8").read()
names = re.findall(r'"([^"]+)"', (re.search(r"static_libs\s*:\s*\[([^\]]*)\]", bp) or re.search("()", "")).group(1))
if "car-radio-lib" not in names:
    problems.append("CarRadioApp does not depend on car-radio-lib")
if "car-radio-common" in bp:
    problems.append("CarRadioApp still names car-radio-common")
if "androidx.recyclerview_recyclerview" not in names:
    problems.append("an existing dependency was removed")
if open("frameworks/radio-lib/Android.bp", encoding="utf-8").read() != '// Copyright (C) 2026 The Android Open Source Project\n// SPDX-License-Identifier: Apache-2.0\n\njava_library {\n    name: "car-radio-lib",\n    srcs: ["src/**/*.java"],\n}\n':
    problems.append("the library's Android.bp was changed (the rename is intended)")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("ok")
