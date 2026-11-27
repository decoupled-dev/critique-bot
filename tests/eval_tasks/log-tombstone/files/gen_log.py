"""Write logcat.txt: about 350k threadtime lines with two native crashes (tombstones) in it.

One tombstone is the car audio HAL the prompt asks about; the other, earlier one is the
vehicle HAL and is a decoy. In the audio HAL crash the top frame is a libc helper
(memcpy), so the frame to report is the first one in the vendor library below it. The
frames are already symbolized (function+offset and file:line).
"""

import random

rng = random.Random(23)
TAGS = ["CarService", "AudioFlinger", "ActivityManager", "CarAudioService", "audio_hw_primary", "WindowManager",
        "chatty", "SurfaceFlinger", "InputDispatcher", "CarPowerManagementService", "AudioPolicyManager"]
LEVELS = "VDIIIIWE"


def stamp(i: int) -> str:
    seconds = i // 45
    return f"11-26 09:{(seconds // 60) % 60:02d}:{seconds % 60:02d}.{i % 1000:03d}"


def tombstone(pid, tid, thread, exe, signal, frames, abort=None):
    lines = ["*** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***",
             "Build fingerprint: 'example/car_x86_64/car:17/AP4A/12345:userdebug/test-keys'",
             "Revision: '0'", "ABI: 'arm64'", "Timestamp: 2026-11-26 09:41:12.004+0000",
             f"pid: {pid}, tid: {tid}, name: {thread}  >>> {exe} <<<", "uid: 1041", signal]
    if abort:
        lines.append(abort)
    for r in range(0, 29, 4):
        lines.append("    " + "  ".join(f"x{n:<2} {rng.randint(0, 2**64 - 1):016x}" for n in range(r, min(r + 4, 29))))
    lines += ["    lr  0000007a1c2e4a10  sp  0000007a0f3fe980  pc  0000007a1d0cb3b4  pst 0000000020001000", "",
              f"{len(frames)} total frames", "backtrace:"]
    for n, (pc, lib, func, src) in enumerate(frames):
        lines.append(f"      #{n:02d} pc {pc:016x}  {lib} ({func}) ({src})" if src else
                     f"      #{n:02d} pc {pc:016x}  {lib} ({func})")
    return lines


AUDIO = tombstone(
    4321, 4390, "AudioZoneMixer", "/vendor/bin/hw/android.hardware.audio.service-caraudio",
    "signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0000000000000010",
    [(0x4a3b4, "/apex/com.android.runtime/lib64/bionic/libc.so", "__memcpy_aarch64_simd+52", "bionic/libc/arch-arm64/string/memcpy.S:113"),
     (0x12a40, "/vendor/lib64/libcaraudio_zone.so",
      "android::hardware::audio::car::ZoneMixer::applyGain(float*, unsigned long)+212",
      "vendor/example/audio/zone/ZoneMixer.cpp:388"),
     (0x11f08, "/vendor/lib64/libcaraudio_zone.so",
      "android::hardware::audio::car::ZoneMixer::mix(android::hardware::audio::car::Buffer const&)+96",
      "vendor/example/audio/zone/ZoneMixer.cpp:301"),
     (0x9c2c, "/vendor/lib64/hw/audio.primary.caraudio.so", "out_write(audio_stream_out*, void const*, unsigned long)+340",
      "vendor/example/audio/hal/audio_hw.cpp:1204"),
     (0x2d8e8, "/vendor/lib64/libaudiohal_stream.so", "android::StreamOut::writeThread()+180", "hardware/interfaces/audio/StreamOut.cpp:512"),
     (0xe1c4c, "/apex/com.android.runtime/lib64/bionic/libc.so", "__pthread_start(void*)+204", "bionic/libc/bionic/pthread_create.cpp:364"),
     (0x7f2a4, "/apex/com.android.runtime/lib64/bionic/libc.so", "__start_thread+68", "bionic/libc/bionic/clone.cpp:53")])

VHAL = tombstone(
    1610, 1610, "android.hardwar", "/vendor/bin/hw/android.hardware.automotive.vehicle@V3-example-service",
    "signal 6 (SIGABRT), code -1 (SI_QUEUE), fault addr --------",
    [(0x5d3a8, "/apex/com.android.runtime/lib64/bionic/libc.so", "abort+164", "bionic/libc/bionic/abort.cpp:50"),
     (0x1b0f4, "/system/lib64/liblog.so", "__android_log_assert+312", "system/logging/liblog/logger_write.cpp:404"),
     (0x33ab0, "/vendor/lib64/libvhal_example_props.so",
      "android::hardware::automotive::vehicle::PropertyStore::writeValue(VehiclePropValue const&)+640",
      "vendor/example/vhal/PropertyStore.cpp:157"),
     (0x2f71c, "/vendor/lib64/libvhal_example_props.so", "android::hardware::automotive::vehicle::FakeVehicleHardware::setValues()+88",
      "vendor/example/vhal/FakeVehicleHardware.cpp:920")],
    abort="Abort message: 'Check failed: value.areaId != 0 (value.areaId=0, 0=0) '")

with open("logcat.txt", "w", encoding="utf-8") as out:
    for i in range(350_000):
        pid = rng.choice([1234, 1301, 2210, 3377, 4321])
        tag, level = rng.choice(TAGS), rng.choice(LEVELS)
        if i % 9001 == 0:
            msg = "mixer state=" + "7e" * rng.randint(4000, 11000)
        elif i == 30_000:
            level, tag, pid = "W", "libc", 4321
            msg = "malloc(18446744073709551615) failed: returning null pointer (handled)"
        else:
            msg = f"frames {rng.randint(0, 99999)} zone={rng.randint(0, 3)} gain={rng.random():.3f}"
        out.write(f"{stamp(i)}  {pid}  {pid + 3} {level} {tag}: {msg}\n")
        if i in (102_330, 287_904):
            body = VHAL if i == 102_330 else AUDIO
            cpid = 1610 if body is VHAL else 4321
            out.write(f"{stamp(i)}  {cpid}  {cpid} F libc    : Fatal signal received, dumping\n")
            for line in body:
                out.write(f"{stamp(i)}  9876  9876 F DEBUG   : {line}\n")
            name = "vendor.vehicle-hal-default" if body is VHAL else "vendor.audio-hal-caraudio"
            out.write(f"{stamp(i)}     1     1 I init    : Service '{name}' (pid {cpid}) received signal "
                      f"{11 if body is AUDIO else 6}; restarting\n")
