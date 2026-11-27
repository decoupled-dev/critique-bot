"""Rename the radio library's module in a commit, as upstream did, and write build.log."""

import os
import subprocess

env = dict(os.environ, GIT_AUTHOR_NAME="Eva Lind", GIT_AUTHOR_EMAIL="eva@example.com", GIT_COMMITTER_NAME="Eva Lind",
           GIT_COMMITTER_EMAIL="eva@example.com", GIT_AUTHOR_DATE="2026-11-20T09:00:00",
           GIT_COMMITTER_DATE="2026-11-20T09:00:00")
with open("frameworks/radio-lib/Android.bp", "w", encoding="utf-8") as out:
    out.write('// Copyright (C) 2026 The Android Open Source Project\n// SPDX-License-Identifier: Apache-2.0\n\njava_library {\n    name: "car-radio-lib",\n    srcs: ["src/**/*.java"],\n}\n')
subprocess.run(["git", "commit", "-q", "-am", "radio-lib: rename car-radio-common to car-radio-lib (module naming guidelines)"],
               check=True, env=env)
with open("build.log", "w", encoding="utf-8") as out:
    out.write("""[100% 1/1] analyzing Android.bp files and generating ninja file at out/soong/build.sdk_car_x86_64.ninja
FAILED: out/soong/build.sdk_car_x86_64.ninja
cd "$(dirname "out/host/linux-x86/bin/soong_build")" && BUILDER="$PWD/$(basename "out/host/linux-x86/bin/soong_build")" && cd / && env -i  "$BUILDER"     --top "$TOP"     --soong_out "out/soong"     --out "out"     -o out/soong/build.sdk_car_x86_64.ninja --globListDir build --globFile out/soong/globs-build.ninja -t -l out/.module_paths/Android.bp.list --available_env out/soong/soong.environment.available --used_env out/soong/soong.environment.used.build Android.bp
error: packages/apps/Car/Radio/Android.bp:4:1: "CarRadioApp" depends on undefined module "car-radio-common".
Or did you mean ["car-radio-lib"]?
16:40:02 soong bootstrap failed with: exit status 1

#### failed to build some targets (17 seconds) ####
""")
