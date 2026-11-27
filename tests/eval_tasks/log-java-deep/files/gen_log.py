"""Write logcat.txt: about 380k threadtime lines with one Car Settings crash.

The crash's top frames are all framework code (ActivityThread, Settings, Parcel) and the
real cause is in a "Caused by:" section whose first app frame is deeper down. Around it
are many decoys: fatal crashes of other packages, caught exceptions printed by
System.err (some of them from Car Settings itself), and StrictMode stacks.
"""

import random

rng = random.Random(31)
TAGS = ["CarService", "ActivityManager", "WindowManager", "chatty", "SurfaceFlinger", "InputDispatcher",
        "CarUxRestrictionsManagerService", "SettingsProvider", "CarSettings", "Choreographer"]
LEVELS = "VDIIIIWE"
APP, APP_PID = "com.example.carsettings", 7302


def stamp(i: int) -> str:
    seconds = i // 55
    return f"11-26 17:{(seconds // 60) % 60:02d}:{seconds % 60:02d}.{i % 1000:03d}"


CRASH = [
    "FATAL EXCEPTION: main",
    f"Process: {APP}, PID: {APP_PID}",
    "java.lang.RuntimeException: Unable to resume activity {com.example.carsettings/com.example.carsettings.MainActivity}: "
    "java.lang.IllegalArgumentException: Invalid brightness level -12 for display 0",
    "\tat android.app.ActivityThread.performResumeActivity(ActivityThread.java:5132)",
    "\tat android.app.ActivityThread.handleResumeActivity(ActivityThread.java:5165)",
    "\tat android.app.servertransaction.ResumeActivityItem.execute(ResumeActivityItem.java:57)",
    "\tat android.app.servertransaction.TransactionExecutor.executeLifecycleState(TransactionExecutor.java:242)",
    "\tat android.app.servertransaction.TransactionExecutor.execute(TransactionExecutor.java:102)",
    "\tat android.app.ActivityThread$H.handleMessage(ActivityThread.java:2594)",
    "\tat android.os.Handler.dispatchMessage(Handler.java:107)",
    "\tat android.os.Looper.loop(Looper.java:317)",
    "\tat android.app.ActivityThread.main(ActivityThread.java:8705)",
    "\tat java.lang.reflect.Method.invoke(Native Method)",
    "\tat com.android.internal.os.RuntimeInit$MethodAndArgsCaller.run(RuntimeInit.java:580)",
    "\tat com.android.internal.os.ZygoteInit.main(ZygoteInit.java:886)",
    "Caused by: java.lang.IllegalArgumentException: Invalid brightness level -12 for display 0",
    "\tat android.os.Parcel.createExceptionOrNull(Parcel.java:3242)",
    "\tat android.os.Parcel.createException(Parcel.java:3226)",
    "\tat android.os.Parcel.readException(Parcel.java:3209)",
    "\tat android.os.Parcel.readException(Parcel.java:3151)",
    "\tat android.content.IContentProvider$Stub$Proxy.call(IContentProvider.java:731)",
    "\tat android.provider.Settings$NameValueCache.putStringForUser(Settings.java:3109)",
    "\tat android.provider.Settings$System.putStringForUser(Settings.java:3891)",
    "\tat android.provider.Settings$System.putIntForUser(Settings.java:4410)",
    "\tat android.provider.Settings$System.putInt(Settings.java:4402)",
    "\tat com.example.carsettings.display.BrightnessController.applyLevel(BrightnessController.java:173)",
    "\tat com.example.carsettings.display.BrightnessController.restoreSaved(BrightnessController.java:121)",
    "\tat com.example.carsettings.display.DisplaySettingsFragment.onResume(DisplaySettingsFragment.java:88)",
    "\tat androidx.fragment.app.Fragment.performResume(Fragment.java:3180)",
    "\tat androidx.fragment.app.FragmentStateManager.resume(FragmentStateManager.java:637)",
    "\tat android.app.Activity.performResume(Activity.java:8968)",
    "\tat android.app.ActivityThread.performResumeActivity(ActivityThread.java:5122)",
    "\t... 13 more",
]


def decoy_fatal(pkg, pid, exc, frames):
    return (["FATAL EXCEPTION: main", f"Process: {pkg}, PID: {pid}", exc]
            + [f"\tat {f}" for f in frames], pid)


DECOYS = {
    40_000: decoy_fatal("com.example.navigation", 5101, "java.lang.IllegalStateException: Map surface released",
                        ["android.view.SurfaceControl.checkNotReleased(SurfaceControl.java:1312)",
                         "com.example.navigation.map.MapRenderer.draw(MapRenderer.java:410)",
                         "android.os.Looper.loop(Looper.java:317)"]),
    121_000: decoy_fatal("com.example.radio", 5340, "java.lang.IllegalArgumentException: Invalid band 9",
                         ["android.hardware.radio.RadioManager.openTuner(RadioManager.java:1881)",
                          "com.example.radio.TunerSession.open(TunerSession.java:64)",
                          "android.os.Looper.loop(Looper.java:317)"]),
    305_000: decoy_fatal("com.example.carlauncher", 5560, "java.lang.NullPointerException: Attempt to read from null array",
                         ["android.widget.GridView.layoutChildren(GridView.java:1392)",
                          "com.example.carlauncher.AppGridAdapter.getView(AppGridAdapter.java:52)",
                          "android.os.Looper.loop(Looper.java:317)"]),
}
CAUGHT = [  # printed by System.err, the process keeps running
    (APP_PID, ["java.lang.IllegalArgumentException: Unknown unit 'mi' (falling back to km)",
               "\tat com.example.carsettings.units.UnitsController.parse(UnitsController.java:44)",
               "\tat com.example.carsettings.units.UnitsFragment.onCreate(UnitsFragment.java:31)"]),
    (APP_PID, ["java.io.FileNotFoundException: /data/user/0/com.example.carsettings/files/profile.json",
               "\tat java.io.FileInputStream.open0(Native Method)",
               "\tat com.example.carsettings.profiles.ProfileStore.load(ProfileStore.java:96)"]),
    (5101, ["java.net.SocketTimeoutException: timeout",
            "\tat com.example.navigation.traffic.TrafficClient.fetch(TrafficClient.java:201)"]),
    (5340, ["java.lang.IllegalStateException: tuner busy",
            "\tat com.example.radio.TunerSession.seek(TunerSession.java:120)"]),
]
CRASH_AT = 266_480

with open("logcat.txt", "w", encoding="utf-8") as out:
    for i in range(380_000):
        pid = rng.choice([1234, 1301, 2210, 5101, 5340, APP_PID])
        tag, level = rng.choice(TAGS), rng.choice(LEVELS)
        if i % 7919 == 0:
            msg = "ux restrictions=" + "3c" * rng.randint(4000, 10000)
        else:
            msg = f"event {rng.randint(0, 99999)} user=10 display={rng.randint(0, 2)}"
        out.write(f"{stamp(i)}  {pid}  {pid + 5} {level} {tag}: {msg}\n")
        if i % 6007 == 0:
            cpid, lines = rng.choice(CAUGHT)
            for line in lines:
                out.write(f"{stamp(i)}  {cpid}  {cpid + 21} W System.err: {line}\n")
        if i in DECOYS:
            lines, dpid = DECOYS[i]
            for line in lines:
                out.write(f"{stamp(i)}  {dpid}  {dpid} E AndroidRuntime: {line}\n")
        if i == CRASH_AT:
            for line in CRASH:
                out.write(f"{stamp(i)}  {APP_PID}  {APP_PID} E AndroidRuntime: {line}\n")
            out.write(f"{stamp(i)}  1234  1301 I ActivityManager: Process {APP} (pid {APP_PID}) has died: fg  TOP\n")
