---
name: android-debugging
description: Android debugging - adb, logcat, stack traces, tombstones, ANR traces, dumpsys, bugreport, Perfetto, SELinux denials, root-cause loop.
keywords: adb, logcat, adb shell, tombstone, debuggerd, ndk-stack, addr2line, anr, /data/anr, dumpsys, meminfo, gfxinfo, bugreport, perfetto, systrace, strictmode, leakcanary, am start, force-stop, pm grant, getprop, setprop, avc: denied, crash, fatal exception, native crash, sigsegv, userdebug, emulator console
files: AndroidManifest.xml, Android.bp
---
# Android Debugging

## How to reason
- Follow the loop: reproduce -> capture minimal signal -> hypothesis -> confirm with a targeted check -> fix -> add regression test. Do not change code before the signal points to it.
- First establish facts: which device/serial, build type (user/userdebug/eng), Android version (ro.build.version.sdk), app version, and exact repro steps.
- Classify the failure: Java/Kotlin crash (FATAL EXCEPTION), native crash (tombstone, signal), ANR, hang, wrong output, performance (jank/startup), permission/SELinux denial, process killed (LMK). Each has a different primary artifact.
- Read the whole stack trace: the deepest "Caused by:" is usually the root; find the first frame in project code.
- Correlate by time and pid: logcat lines near the failure from the same pid/tid; system_server lines (ActivityManager, PackageManager) for lifecycle and kill reasons.
- Know your privileges: user builds deny root, run-as works only for debuggable apps; userdebug allows adb root.

## Do
- Target a device explicitly when several are attached: `adb -s <serial> ...` (or ANDROID_SERIAL env var).
- Filter logcat narrowly:
```bash
adb logcat -c                                   # clear before repro
adb logcat --pid=$(adb shell pidof -s com.example.app)
adb logcat -b crash                             # crash buffer only
adb logcat -s MyTag:D ActivityManager:I         # tag filters
adb logcat -v threadtime -d > log.txt           # dump and exit
```
- Native crash: pull tombstone (`adb shell ls /data/tombstones`, root/bugreport needed on most builds), symbolize with unstripped libs: `ndk-stack -sym <path-to-obj/local/abi> -i tombstone.txt` or `llvm-addr2line -Cfe libfoo.so <pc>`; in AOSP use development/scripts/stack.
- ANR: read the "ANR in" logcat block (reason, CPU), then the trace in /data/anr (via bugreport on user builds); inspect the main thread stack and what lock it waits on, and which thread holds it.
- Use dumpsys for state, not guesses: `dumpsys activity activities` (task/back stack), `dumpsys activity services <pkg>`, `dumpsys package <pkg>` (permissions, components, versions), `dumpsys meminfo <pkg>`, `dumpsys gfxinfo <pkg> framestats`, `dumpsys battery`, `dumpsys window`, `dumpsys jobscheduler`.
- Control the app: `am start -W -n pkg/.Activity` (startup timing), `am force-stop pkg`, `am broadcast -a ACTION -n pkg/.Receiver`, `pm grant pkg android.permission.X`, `pm clear pkg`, `cmd package compile`, `settings put global|secure|system key value`.
- Properties: `getprop ro.build.type`, `setprop log.tag.MyTag VERBOSE` to enable Log.isLoggable output.
- Performance: record a Perfetto trace (`adb shell perfetto -o /data/misc/perfetto-traces/trace ...` or record_android_trace script / Android Studio profiler), open in ui.perfetto.dev; add trace sections with androidx.tracing trace("name") { }.
- Enable StrictMode in debug builds for disk/network on main and leaked closeables; add LeakCanary as debugImplementation for leaks.
- SELinux: grep `avc: denied` and read scontext, tcontext, tclass, and permission; fix policy minimally (see aosp skill). `getenforce` shows mode.
- Full context: `adb bugreport bugreport.zip` contains logs, ANR traces, tombstones, dumpsys.
- Emulator: `adb emu` or telnet console for geo fix, network speed, power, and sms events.

## Avoid
- Guessing from a single log line -> collect the full trace and surrounding pid logs.
- Adding print statements everywhere -> targeted log at the hypothesis point, then remove.
- Fixing the symptom (catch-and-ignore, null-check) without knowing why the value is wrong.
- `setenforce 0` as a fix -> only a diagnostic on userdebug; write policy.
- Assuming root on user builds; using `su` paths that do not exist -> use `adb root` on userdebug or run-as for debuggable apps.
- Reading obfuscated release traces raw -> retrace with mapping.txt.
- Debugging on a stale build -> confirm installed versionCode/build time (`dumpsys package pkg | grep version`).
- Running unbounded `adb logcat` in a non-interactive shell -> use -d or a timeout so the command returns.

## Commands
```bash
adb devices -l
adb install -r -t -d app-debug.apk           # replace, allow test APK, allow downgrade (debuggable)
adb shell pidof com.example.app
adb shell run-as com.example.app ls files    # debuggable apps only
adb shell dumpsys activity processes | grep -i <pkg>
adb shell cmd activity get-current-user
adb pull /data/anr/ ./anr                    # userdebug with adb root
adb root; adb shell dmesg | grep -i avc
```
PowerShell: capture the pid first (`$p = adb shell pidof -s <pkg>; adb logcat --pid=$p`); use `Select-String` instead of grep and `adb logcat -d > log.txt` then search the file. adb.exe must be on PATH or use $env:ANDROID_HOME\platform-tools\adb.exe.

## Verify before COMPLETED
- Root cause stated with the evidence (log line, stack frame, dumpsys field) that proves it.
- Repro steps no longer reproduce the failure on the same device/build; logcat clean of the original exception/ANR/avc.
- No new crashes, StrictMode violations, or leaks introduced on the touched path.
- Regression test added (unit or instrumented) that fails before the fix, or an explicit reason why one is not feasible.
