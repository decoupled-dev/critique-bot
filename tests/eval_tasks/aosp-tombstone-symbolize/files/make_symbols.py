"""Build the unstripped libvhalclient.so into out/target/product/emu64x/symbols (as m would),
then write tombstone_03 for a crash at the null dereference in VhalClient::getValue.

The tombstone shows the stripped library as a device does: a pc and a BuildId, no function name.
"""

import os
import re
import subprocess

SRC = "hardware/interfaces/automotive/vehicle/aidl/impl/client/VhalClient.cpp"
LIB = "out/target/product/emu64x/symbols/vendor/lib64/libvhalclient.so"
TARGET_LINE = 26
os.makedirs(os.path.dirname(LIB), exist_ok=True)
subprocess.run(["g++", "-g", "-O0", "-fPIC", "-shared", "-o", LIB, SRC], check=True)
symbols = subprocess.run(["nm", LIB], capture_output=True, text=True, check=True).stdout
start = next(int(m.group(1), 16) for m in re.finditer(r"^([0-9a-f]+) T (\S+)$", symbols, re.M)
             if "VhalClient8getValue" in m.group(2))
addresses = [hex(start + offset) for offset in range(0, 400)]
out = subprocess.run(["addr2line", "-e", LIB] + addresses, capture_output=True, text=True, check=True).stdout.splitlines()
pc = next(int(addr, 16) for addr, place in zip(addresses, out)
          if place.split(" (")[0].endswith(f"VhalClient.cpp:{TARGET_LINE}"))
build_id = "6f1c2a9e4b7d03f58a2c11e9d0b4a7c3"
with open("tombstone_03", "w", encoding="utf-8") as stone:
    stone.write(f"""*** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***
Build fingerprint: 'generic/sdk_car_x86_64/emulator_car64_x86_64:16/BP2A.250605.031/13578812:userdebug/dev-keys'
Revision: '0'
ABI: 'x86_64'
Timestamp: 2026-11-25 10:07:12.550813461+0000
Process uptime: 41s
Cmdline: /vendor/bin/hw/android.hardware.automotive.vehicle-V3-default-service
pid: 612, tid: 640, name: binder:612_2  >>> /vendor/bin/hw/android.hardware.automotive.vehicle-V3-default-service <<<
uid: 1000
signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0000000000000000
Cause: null pointer dereference
    rax 0000000000000000  rbx 00007ffd1a2b3c40  rcx 0000000000000001  rdx 0000000000000000

backtrace:
      #00 pc {pc:016x}  /vendor/lib64/libvhalclient.so (BuildId: {build_id})
      #01 pc 0000000000031a7c  /vendor/bin/hw/android.hardware.automotive.vehicle-V3-default-service (BuildId: 0d4e5f60718293a4)
      #02 pc 000000000004b2f0  /system/lib64/libbinder_ndk.so (AIBinder_Class_onTransact+120) (BuildId: 9a8b7c6d5e4f3a2b)
      #03 pc 00000000000c4a10  /apex/com.android.runtime/lib64/bionic/libc.so (__start_thread+64) (BuildId: 1f2e3d4c5b6a7980)
""")
