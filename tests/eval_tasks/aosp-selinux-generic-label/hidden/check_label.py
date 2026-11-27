"""Pass when the fan node gets its own sysfs type (declared, labeled with genfscon) and fan_ctl.te
allows that type, never the generic sysfs label."""

import re
import sys

problems = []
te = open("sepolicy/vendor/fan_ctl.te", encoding="utf-8").read()
file_te = open("sepolicy/vendor/file.te", encoding="utf-8").read()
genfs = open("sepolicy/vendor/genfs_contexts", encoding="utf-8").read()
if re.search(r"allow\s+vendor_fan_ctl\s+sysfs\s*:", te):
    problems.append("fan_ctl.te allows the generic sysfs type")
labels = re.findall(r"^\s*genfscon\s+sysfs\s+(/devices/platform/fan-hwmon\S*)\s+u:object_r:(\w+):s0", genfs, re.M)
labels = [(path, kind) for path, kind in labels if kind != "sysfs"]
if not labels:
    problems.append("no genfscon line labels /devices/platform/fan-hwmon... with its own type")
for path, kind in labels:
    if not re.search(r"^\s*type\s+" + kind + r"\s*,[^;]*\bsysfs_type\b[^;]*;", file_te, re.M):
        problems.append(f"type {kind} is not declared with sysfs_type in file.te")
    allowed = set()
    for match in re.finditer(r"allow\s+vendor_fan_ctl\s+" + kind + r"\s*:\s*file\s+(\{[^}]*\}|\w+)\s*;", te):
        allowed.update(match.group(1).strip("{} ").split())
    if not {"open", "read"} <= allowed:
        problems.append(f"fan_ctl.te does not allow open and read on {kind}:file")
    if allowed - {"open", "read", "getattr"}:
        problems.append(f"fan_ctl.te allows more than needed on {kind}: {sorted(allowed)}")
if 'genfscon sysfs /devices/platform/cabin-temp/temp u:object_r:sysfs_cabin_temp:s0' not in genfs or 'type sysfs_cabin_temp, fs_type, sysfs_type;' not in file_te:
    problems.append("existing policy lines were changed")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("ok")
