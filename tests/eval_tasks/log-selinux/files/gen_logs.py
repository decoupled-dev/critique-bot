"""Write logcat.txt (about 250k lines) and dmesg.txt (about 120k lines) full of SELinux denials.

Many domains are denied many things (a "denial storm"), most with permissive=1 from
domains still in development, so they block nothing. The vehicle logger service keeps
crashing because one denial is enforced: it cannot connect to the vehicle bus socket.
A look-alike domain (vendor_vehicle_logctl) and a permissive getattr denial for the
logger itself are decoys.
"""

import random

rng = random.Random(5)
NOISE = [  # (comm, scontext, tcontext, tclass, perm)
    ("hal_audio", "hal_audio_default", "sysfs", "file", "read"),
    ("surfaceflinger", "surfaceflinger", "vendor_display_prop", "file", "open"),
    ("cameraserver", "cameraserver", "vendor_camera_data_file", "dir", "search"),
    ("vendor.evs", "hal_evs_default", "gpu_device", "chr_file", "ioctl"),
    ("RenderThread", "untrusted_app", "proc_stat", "file", "read"),
    ("vehicle_logctl", "vendor_vehicle_logctl", "vendor_vehicle_bus", "unix_stream_socket", "connectto"),
    ("carwatchdogd", "carwatchdogd", "proc_pressure_mem", "file", "getattr"),
    ("netd", "netd", "vendor_net_prop", "file", "map"),
    ("vehicle_logger", "vendor_vehicle_logger", "sysfs_thermal", "dir", "getattr"),
]
SERVICE_PID = 812


def avc(seq, comm, pid, perm, scon, tcon, tclass, permissive, path):
    return (f"avc:  denied  {{ {perm} }} for  pid={pid} comm=\"{comm}\" {path} "
            f"scontext=u:r:{scon}:s0 tcontext=u:{'r' if tclass.endswith('socket') else 'object_r'}:{tcon}:s0 "
            f"tclass={tclass} permissive={permissive}")


REAL = dict(comm="vehicle_logger", perm="connectto", scon="vendor_vehicle_logger", tcon="vendor_vehicle_bus",
            tclass="unix_stream_socket", permissive=0, path='path="/dev/socket/vehicle_bus"')

with open("dmesg.txt", "w", encoding="utf-8") as out:
    for i in range(120_000):
        t = f"[{i * 0.0137:12.6f}]"
        if i % 7 == 0:
            comm, scon, tcon, tclass, perm = rng.choice(NOISE)
            path = f'name="{rng.choice(["stat", "temp", "fb0", "card0", "vehicle_bus", "mem"])}" dev="tmpfs" ino={rng.randint(100, 99999)}'
            out.write(f"{t} type=1400 audit({1764140000 + i // 50}.{i % 1000:03d}:{i}): "
                      + avc(i, comm, rng.randint(300, 6000), perm, scon, tcon, tclass, 1, path) + "\n")
        elif i in (61_200, 61_900, 62_600, 63_300, 64_000):
            pid = SERVICE_PID + (i - 61_200) // 700 * 31
            out.write(f"{t} type=1400 audit({1764140000 + i // 50}.{i % 1000:03d}:{i}): "
                      + avc(i, **{**REAL, "pid": pid}) + "\n")
            out.write(f"{t} init: Service 'vendor.vehicle_logger' (pid {pid}) exited with status 1\n")
            out.write(f"{t} init: Sending signal 9 to service 'vendor.vehicle_logger' (pid {pid}) process group...\n")
        else:
            out.write(f"{t} {rng.choice(['binder', 'usb', 'healthd', 'lowmemorykiller', 'cfg80211', 'ext4'])}: "
                      f"event {rng.randint(0, 99999)} state=0x{rng.randint(0, 0xFFFF):04x}\n")

TAGS = ["CarService", "VHAL", "ActivityManager", "CarPropertyService", "AudioFlinger", "chatty", "SurfaceFlinger"]
with open("logcat.txt", "w", encoding="utf-8") as out:
    for i in range(250_000):
        s = i // 40
        st = f"11-26 08:{(s // 60) % 60:02d}:{s % 60:02d}.{i % 1000:03d}"
        if i % 5 == 0:
            comm, scon, tcon, tclass, perm = rng.choice(NOISE)
            out.write(f"{st}   {rng.randint(300, 6000)}   {rng.randint(300, 6000)} W {comm}: type=1400 audit(0.0:{i}): "
                      + avc(i, comm, 0, perm, scon, tcon, tclass, 1, 'name="x"').replace("pid=0 ", "") + "\n")
        elif i in (127_000, 127_500, 128_000):
            pid = SERVICE_PID + (i - 127_000) // 500 * 31
            out.write(f"{st}   {pid}   {pid} E vehicle_logger: connect(/dev/socket/vehicle_bus) failed: Permission denied\n")
            out.write(f"{st}   {pid}   {pid} F vehicle_logger: cannot reach the vehicle bus, exiting\n")
        else:
            out.write(f"{st}  {rng.choice([1234, 1301, 2210])}  {rng.choice([1240, 1310])} "
                      f"{rng.choice('VDIIIW')} {rng.choice(TAGS)}: event {rng.randint(0, 99999)}\n")
