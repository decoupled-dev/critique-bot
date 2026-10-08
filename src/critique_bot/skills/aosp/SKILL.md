---
name: aosp
description: AOSP platform work - Soong/Android.bp, system services, Binder/AIDL, HALs, VINTF, SELinux, init, overlays, API surfaces, atest.
keywords: aosp, android.bp, android.mk, soong, envsetup.sh, lunch, frameworks/base, systemserver, system service, binder, aidl_interface, stable aidl, hidl, vintf, sepolicy, file_contexts, neverallow, init.rc, system property, rro, privapp-permissions, treble, vendor partition, systemapi, hiddenapi, update-api, atest, test_mapping, cts, vts, tradefed, adb remount
files: Android.bp, Android.mk, build/envsetup.sh, TEST_MAPPING, *.rc, sepolicy/*.te, file_contexts, manifest.xml, compatibility_matrix.xml
---
# AOSP Platform Development

## How to reason
- Locate the layer first: app (packages/apps), framework Java (frameworks/base/core, services/core), native services (frameworks/native, system/), HAL interface (hardware/interfaces), HAL impl/device config (device/<oem>/<board>, vendor/). Fix at the layer that owns the behavior.
- Find the owning module: search the nearest Android.bp up the tree; note module name, partition, visibility, and who depends on it.
- Ask which partition the code lands in (system, system_ext, product, vendor, odm). Treble forbids system<->vendor coupling except via stable interfaces (AIDL HAL, sysprop with defined scope, VINTF).
- Changing a public/system API or a frozen AIDL interface has compatibility consequences: API files, versioning, CTS. Decide if a new version or a new method is needed.
- For new privileged behavior ask: which permission, SELinux domain, and allowlist enables it? Missing any one fails silently or at boot.
- Check TEST_MAPPING near the change to learn which presubmit tests cover it.
- Read existing similar code (another service, HAL, or app in the tree) and copy its conventions.

## Do
- Soong patterns:
```
android_app {
    name: "MyPrivApp",
    srcs: ["src/**/*.java", "src/**/*.kt"],
    platform_apis: true,
    certificate: "platform",
    privileged: true,
    system_ext_specific: true,
    static_libs: ["androidx.annotation_annotation"],
    required: ["privapp_allowlist_com.example.myapp"],
}
aidl_interface {
    name: "android.hardware.foo",
    vendor_available: true,
    srcs: ["android/hardware/foo/*.aidl"],
    stability: "vintf",
    backend: { java: { enabled: true }, ndk: { enabled: true } },
    versions_with_info: [{ version: "1", imports: [] }],
    frozen: true,
}
```
- Use visibility: ["//path/to/pkg:__subpackages__"] to restrict dependents; vendor: true / product_specific / system_ext_specific for partition placement.
- Stable AIDL: never modify a frozen version; edit the current (unfrozen) sources, run `m <name>-update-api`, then freeze with `m <name>-freeze-api` when releasing. Add methods at the end; new parcelable fields only appended with defaults.
- AIDL HAL: declare in the device VINTF manifest (vintf_fragments in Android.bp or device manifest.xml) and ensure the framework compatibility matrix lists the version. Register with AServiceManager_addService using "<package>.<IFoo>/default".
- System service: implement in frameworks/base/services, publish via SystemService.publishBinderService in onStart, start it from SystemServer in the right boot phase; enforce callers with mContext.enforceCallingOrSelfPermission and clear identity (Binder.clearCallingIdentity / restoreCallingIdentity in try/finally) before calling into other services.
- Annotate new hidden APIs @hide; system APIs @SystemApi with a permission (@RequiresPermission); run `m update-api` and commit the current.txt / system-current.txt diffs. Fix api lint findings rather than suppressing.
- SELinux: define type, add file_contexts / service_contexts / property_contexts labels, write minimal allow rules using macros (binder_call, add_service, get_prop, set_prop, hal_client_domain). Put vendor policy in device sepolicy dirs, not system/sepolicy, unless it is platform policy.
- init .rc: service with user, group, class, and seclabel only when needed; use on property:... triggers; restrict sysprop writers via property_contexts.
- Privileged permissions: add to privapp-permissions-<pkg>.xml in the matching partition's etc/permissions; otherwise boot fails or perms are denied.
- Config changes for devices: prefer RRO overlays (runtime_resource_overlay module) over editing frameworks/base/core/res defaults.

## Avoid
- Editing out/ or generated files -> change the source and rebuild.
- Blind audit2allow output -> it over-grants and often hits neverallow; write minimal, labeled rules and justify each.
- Labeling with permissive domains or disabling enforcement in shipping configs.
- Adding new code to Android.mk -> use Android.bp; convert if touching heavily.
- Calling @hide APIs from unbundled apps -> use @SystemApi or a platform-signed module.
- Adding vendor dependencies on system libraries not in VNDK/LLNDK, or system dependencies on vendor libs.
- Changing frozen AIDL .aidl files under aidl_api/<name>/<version>/ -> create a new version.
- Running full `m` when one module changed -> build the module, then sync.
- Flashing a device without confirming target, bootloader state, and that vbmeta/partition images match; never `fastboot flashall -w` without stating it wipes data.

## Commands
```bash
source build/envsetup.sh
lunch <product>-<release>-<variant>        # e.g. aosp_cf_x86_64_phone-trunk_staging-userdebug
m                                          # full build
m <module>                                 # one module + deps
mm / mmm path/to/dir                       # modules in cwd / given dir
m update-api                               # regenerate API txt files
m <aidl_name>-update-api                   # stable AIDL current API
atest <ModuleOrTestClass>[#method]
atest --test-mapping path/to/dir           # presubmit tests from TEST_MAPPING
adb root && adb remount                    # userdebug/eng; first time may need adb reboot then remount
adb sync system                            # push rebuilt files from out/target/product/<device>
adb shell stop && adb shell start          # restart framework after pushing services.jar
adb shell dumpsys <service>; adb shell service list
adb logcat | grep "avc: denied"
```
Outputs: out/target/product/<device>/ (images, system/, vendor/), out/soong/ (intermediates). Windows hosts are unsupported for platform builds; use Linux.

## Verify before COMPLETED
- `m <module>` succeeds; for API changes `m update-api` diff is committed and api lint passes; checkapi does not fail.
- For AIDL/HAL: VINTF check passes at build (assemble_vintf/check_vintf) and service registers at boot (`service list`, `lshal` for HIDL).
- SELinux: no new avc: denied in logcat for the feature path; no neverallow build failures.
- Device boots on the lunch target; feature exercised via adb/dumpsys.
- atest of the relevant unit/CTS/VTS tests and TEST_MAPPING presubmit pass.
