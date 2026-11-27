"""Write dmesg.txt: boot noise and many SELinux denials; one enforced denial stops vendor.cabin_sensor."""

import random

rng = random.Random(21)
lines = []
t = 1.0
for i in range(6000):
    t += rng.random() / 50
    kind = rng.random()
    if kind < 0.04:
        lines.append(f"[{t:12.6f}] type=1400 audit(0.0:{i}): avc:  denied  {{ getattr }} for  pid={rng.randint(400, 900)} comm=\"vendor.media_stats\" path=\"/data/vendor/media\" dev=\"dm-5\" ino={rng.randint(1000, 9999)} scontext=u:r:vendor_media_stats:s0 tcontext=u:object_r:vendor_data_file:s0 tclass=dir permissive=1")
    elif kind < 0.07:
        lines.append(f"[{t:12.6f}] type=1400 audit(0.0:{i}): avc:  denied  {{ search }} for  pid=1310 comm=\"surfaceflinger\" name=\"cabin-temp\" dev=\"sysfs\" ino=40213 scontext=u:r:surfaceflinger:s0 tcontext=u:object_r:sysfs:s0 tclass=dir permissive=1")
    else:
        lines.append(f"[{t:12.6f}] binder: {rng.randint(100, 999)}:{rng.randint(100, 999)} transaction {rng.randint(1, 99999)} done")
    if i == 4200:
        for attempt in range(4):
            pid = 2101 + attempt
            lines.append(f"[{t:12.6f}] init: starting service 'vendor.cabin_sensor'...")
            lines.append(f"[{t:12.6f}] type=1400 audit(0.0:{i + attempt}): avc:  denied  {{ open }} for  pid={pid} comm=\"cabin_sensor\" path=\"/sys/devices/platform/cabin-temp/temp\" dev=\"sysfs\" ino=40217 scontext=u:r:vendor_cabin_sensor:s0 tcontext=u:object_r:sysfs_cabin_temp:s0 tclass=file permissive=0")
            lines.append(f"[{t:12.6f}] type=1400 audit(0.0:{i + attempt}): avc:  denied  {{ read }} for  pid={pid} comm=\"cabin_sensor\" name=\"temp\" dev=\"sysfs\" ino=40217 scontext=u:r:vendor_cabin_sensor:s0 tcontext=u:object_r:sysfs_cabin_temp:s0 tclass=file permissive=0")
            lines.append(f"[{t:12.6f}] init: Service 'vendor.cabin_sensor' (pid {pid}) exited with status 1")
        lines.append(f"[{t:12.6f}] type=1400 audit(0.0:{i + 9}): avc:  denied  {{ write }} for  pid=777 comm=\"vendor.media_stats\" name=\"stats\" dev=\"dm-5\" ino=5511 scontext=u:r:vendor_media_stats:s0 tcontext=u:object_r:vendor_data_file:s0 tclass=file permissive=0")
with open("dmesg.txt", "w", encoding="utf-8") as out:
    out.write("\n".join(lines) + "\n")
