"""Pass when cabin_sensor.te gains exactly the rule for the enforced denials of vendor_cabin_sensor
(file open + read on sysfs_cabin_temp), and nothing broader; other policy files unchanged."""

import re
import sys

ORIGINAL = '# Cabin temperature sensor daemon.\ntype vendor_cabin_sensor, domain;\ntype vendor_cabin_sensor_exec, exec_type, vendor_file_type, file_type;\n\ninit_daemon_domain(vendor_cabin_sensor)\nallow vendor_cabin_sensor vendor_cabin_sensor_data_file:dir create_dir_perms;\nallow vendor_cabin_sensor vendor_cabin_sensor_data_file:file create_file_perms;\n'
problems = []
text = open("sepolicy/vendor/cabin_sensor.te", encoding="utf-8").read()


def norm(line):
    return re.sub(r"\s+", " ", line.split("#", 1)[0]).strip()


old = [norm(line) for line in ORIGINAL.splitlines() if norm(line)]
new = [norm(line) for line in text.splitlines() if norm(line)]
for line in old:
    if line not in new:
        problems.append("original line changed or removed: " + line)
extra = [line for line in new if line not in old]
granted = set()
rule = re.compile(r"^allow vendor_cabin_sensor sysfs_cabin_temp ?: ?file (\{([^}]*)\}|(\w+)) ?;$")
for line in extra:
    match = rule.match(line)
    if not match:
        problems.append("unexpected statement: " + line)
        continue
    granted.update((match.group(2) or match.group(3)).split())
if granted != {"open", "read"}:
    problems.append(f"wanted exactly {{open read}} on sysfs_cabin_temp:file, got {sorted(granted)}")
if open("sepolicy/vendor/media_stats.te", encoding="utf-8").read() != 'type vendor_media_stats, domain;\ntype vendor_media_stats_exec, exec_type, vendor_file_type, file_type;\ninit_daemon_domain(vendor_media_stats)\n':
    problems.append("media_stats.te was changed")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print("ok")
