"""Write logcat.txt (about 300k threadtime lines) and traces.txt (an ANR dump of many processes).

The logcat has one real ANR plus noise that looks related: slow-dispatch warnings, a
"not responding" dialog that was dismissed for another app, and long lines. traces.txt
holds every thread of many processes; several of them have blocked threads, but only the
ANR process's main thread is the one that matters, and the lock it waits for is held by a
worker thread whose own stack shows where it took the lock.
"""

import random

rng = random.Random(11)
TAGS = ["CarService", "VHAL", "ActivityManager", "CarPropertyService", "AudioFlinger", "WindowManager",
        "chatty", "SurfaceFlinger", "InputDispatcher", "CarMediaService", "MediaSessionService", "Looper"]
LEVELS = "VDIIIIWE"
APP = "com.example.carmedia"
APP_PID = 6120


def stamp(i: int) -> str:
    seconds = i // 50
    return f"11-26 14:{(seconds // 60) % 60:02d}:{seconds % 60:02d}.{i % 1000:03d}"


ANR_AT = 211_400
with open("logcat.txt", "w", encoding="utf-8") as out:
    for i in range(300_000):
        pid = rng.choice([1234, 1301, 2210, 3377, 4410, APP_PID])
        tag, level = rng.choice(TAGS), rng.choice(LEVELS)
        if i % 8111 == 0:
            msg = "dump state=" + "0f" * rng.randint(4000, 9000)
        elif i % 25_013 == 0:
            level, tag = "W", "Looper"
            msg = f"Slow dispatch took {rng.randint(200, 900)}ms main h=android.os.Handler c=null m=0"
        elif i == 90_000:
            level, tag = "W", "ActivityManager"
            msg = "Not responding dialog for com.example.radio was dismissed: process recovered"
        elif i == ANR_AT - 300:
            level, tag, pid = "W", "InputDispatcher", 1234
            msg = (f"Window 7f3e2a1 {APP}/{APP}.PlayerActivity (server) is not responding. "
                   "Waited 5001ms for KeyEvent")
        else:
            msg = f"event {rng.randint(0, 99999)} prop=0x{rng.randint(0, 0xFFFFFFF):08x} area={rng.randint(0, 5)}"
        out.write(f"{stamp(i)}  {pid}  {pid + 9} {level} {tag}: {msg}\n")
        if i == ANR_AT:
            for line in [
                f"ANR in {APP} ({APP}/.PlayerActivity)",
                f"PID: {APP_PID}",
                "Reason: Input dispatching timed out (7f3e2a1 com.example.carmedia/com.example.carmedia.PlayerActivity "
                "(server) is not responding. Waited 5001ms for KeyEvent)",
                "Parent: com.example.carmedia/.PlayerActivity",
                "Load: 7.12 / 6.80 / 5.91",
                "CPU usage from 0ms to 9873ms later:",
                f"  88% {APP_PID}/{APP}: 80% user + 8% kernel",
                "Dumping to /data/anr/anr_2026-11-26-14-10-31-117",
            ]:
                out.write(f"{stamp(i)}  1234  1301 E ActivityManager: {line}\n")


def frames(n, pool):
    return [f"  at {rng.choice(pool)}" for _ in range(n)]


FRAMEWORK = [
    "android.os.MessageQueue.nativePollOnce(Native method)",
    "android.os.MessageQueue.next(MessageQueue.java:335)",
    "android.os.Looper.loopOnce(Looper.java:162)",
    "android.os.Looper.loop(Looper.java:294)",
    "android.os.HandlerThread.run(HandlerThread.java:67)",
    "java.lang.Object.wait(Native method)",
    "java.lang.Thread.sleep(Native method)",
    "java.util.concurrent.locks.LockSupport.park(LockSupport.java:211)",
    "java.util.concurrent.ThreadPoolExecutor.getTask(ThreadPoolExecutor.java:1070)",
    "java.lang.Thread.run(Thread.java:1012)",
]


def thread(name, tid, state, body):
    lines = [f'"{name}" prio=5 tid={tid} {state}',
             f"  | group=\"main\" sCount=1 ucsCount=0 flags=1 obj=0x{rng.randint(0, 0xffffffff):08x} "
             f"self=0x{rng.randint(0, 0xffffffffffff):012x}",
             f"  | sysTid={rng.randint(1000, 9999)} nice=0 cgrp=foreground sched=0/0 handle=0x{rng.randint(0, 0xffffffff):08x}"]
    return lines + body + [""]


def process(pid, name, special=None):
    lines = [f"----- pid {pid} at 2026-11-26 14:10:31.117 -----", f"Cmd line: {name}",
             "Build fingerprint: 'example/car_x86_64/car:17/AP4A/12345:userdebug/test-keys'", "ABI: 'x86_64'", ""]
    for tid in range(1, rng.randint(30, 70)):
        if special and tid in special:
            lines += special[tid]
            continue
        tname = "main" if tid == 1 else rng.choice(["Binder", "HwBinder", "RenderThread", "pool", "Jit", "HeapTaskDaemon",
                                                    "FinalizerDaemon", "Signal Catcher", "Profile Saver"]) + f":{pid}_{tid}"
        lines += thread(tname, tid, rng.choice(["Native", "Waiting", "TimedWaiting", "Sleeping"]),
                        frames(rng.randint(4, 12), FRAMEWORK))
    return lines + [f"----- end {pid} -----", "", ""]


LOCK = "0x0b7c41d2"
DECOY_LOCK = "0x05e1aa90"
app_special = {
    1: thread("main", 1, "Blocked", [
        f"  at com.example.carmedia.PlayerController.onPlayPause(PlayerController.java:96)",
        f"  - waiting to lock <{LOCK}> (a com.example.carmedia.library.MediaIndex) held by thread 23",
        "  at com.example.carmedia.PlayerActivity.onKeyUp(PlayerActivity.java:154)",
        "  at android.view.KeyEvent.dispatch(KeyEvent.java:3445)",
        "  at android.app.Activity.dispatchKeyEvent(Activity.java:4423)",
        "  at android.view.ViewRootImpl$ViewPostImeInputStage.processKeyEvent(ViewRootImpl.java:7211)",
        "  at android.os.Looper.loop(Looper.java:294)",
        "  at android.app.ActivityThread.main(ActivityThread.java:8177)",
    ]),
    14: thread("ArtworkLoader", 14, "Blocked", [
        "  at com.example.carmedia.art.ArtworkCache.put(ArtworkCache.java:61)",
        f"  - waiting to lock <{DECOY_LOCK}> (a android.util.LruCache) held by thread 19",
        "  at com.example.carmedia.art.ArtworkLoader.run(ArtworkLoader.java:40)",
        "  at java.lang.Thread.run(Thread.java:1012)",
    ]),
    19: thread("ArtworkPrefetch", 19, "Sleeping", [
        "  at java.lang.Thread.sleep(Native method)",
        f"  - sleeping on <{DECOY_LOCK}> (a android.util.LruCache)",
        "  at com.example.carmedia.art.ArtworkPrefetch.throttle(ArtworkPrefetch.java:77)",
        f"  - locked <{DECOY_LOCK}> (a android.util.LruCache)",
        "  at java.lang.Thread.run(Thread.java:1012)",
    ]),
    23: thread("MediaSyncWorker", 23, "Native", [
        "  at libcore.io.Linux.read(Native method)",
        "  at libcore.io.ForwardingOs.read(ForwardingOs.java:117)",
        "  at java.io.FileInputStream.read(FileInputStream.java:313)",
        "  at com.example.carmedia.library.UsbScanner.readTags(UsbScanner.java:142)",
        "  at com.example.carmedia.library.MediaIndexer.rebuildIndex(MediaIndexer.java:217)",
        f"  - locked <{LOCK}> (a com.example.carmedia.library.MediaIndex)",
        "  at com.example.carmedia.library.MediaIndexer$1.run(MediaIndexer.java:58)",
        "  at java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1145)",
        "  at java.lang.Thread.run(Thread.java:1012)",
    ]),
}

others = [(1301, "system_server"), (2210, "com.android.car.carlauncher"), (3377, "com.example.radio"),
          (4410, "com.android.systemui"), (5002, "com.example.navigation"), (5260, "android.process.media")]
with open("traces.txt", "w", encoding="utf-8") as out:
    for idx, (pid, name) in enumerate(others):
        special = None
        if name == "com.example.radio":  # a blocked main thread in a process that did not ANR
            special = {1: thread("main", 1, "Blocked", [
                "  at com.example.radio.TunerController.seek(TunerController.java:310)",
                "  - waiting to lock <0x0c99e001> (a java.lang.Object) held by thread 8",
                "  at android.os.Looper.loop(Looper.java:294)"]),
                8: thread("TunerHal", 8, "Native", [
                    "  at com.example.radio.TunerHal.nativeTune(Native method)",
                    "  at com.example.radio.TunerHal.tune(TunerHal.java:88)",
                    "  - locked <0x0c99e001> (a java.lang.Object)"])}
        out.write("\n".join(process(pid, name, special)) + "\n")
        if idx == 3:
            out.write("\n".join(process(APP_PID, APP, app_special)) + "\n")
