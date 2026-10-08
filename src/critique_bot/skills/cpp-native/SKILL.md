---
name: cpp-native
description: C/C++ for Android NDK, JNI and AOSP/automotive native code: Soong/CMake, NDK AIDL, ownership, thread safety, sanitizers, debugging.
keywords: c++, cpp, ndk, jni, jnienv, registernatives, cmakelists.txt, ndk-build, android.mk, cc_library, cc_binary, cc_test, libbinder_ndk, scopedastatus, android::sp, unique_ptr, raii, hwasan, asan, ubsan, clang-tidy, tombstone, googletest, gtest, android-base, misra
files: CMakeLists.txt, Android.mk, Application.mk, Android.bp, src/main/cpp, jni
---
# C/C++ Native (NDK, JNI, AOSP)

## How to reason
- Identify the build system and context: Gradle `externalNativeBuild` (CMake/ndk-build) for apps, Soong `Android.bp` for platform code. Check C++ standard (`cpp_std`/`CMAKE_CXX_STANDARD`), STL (`c++_shared`/`c++_static`), and `-Werror` policy.
- Platform vs vendor vs NDK: which libraries may this module link (`vendor: true`, VNDK/LLNDK, `sdk_version`)? Do not add a dependency that crosses the partition boundary.
- Ownership: for every pointer/handle, who frees it and when? Who outlives whom across threads and callbacks?
- Thread model: which threads call in (binder threads, JNI callers, worker threads)? What locks guard which members?
- Error model of the codebase: exceptions are normally disabled in AOSP; use return types (`Result<T>`, `status_t`, `ScopedAStatus`).
- Read existing code first: logging macros, `LOG_TAG`, namespaces, naming, existing gtest fixtures, and how JNI methods are registered.

## Do
- RAII for every resource: `std::unique_ptr` (with custom deleter for C handles), `android::base::unique_fd`, `std::lock_guard`/`std::scoped_lock`. `std::make_unique`/`std::make_shared`.
- AOSP refcounted objects (`RefBase`): hold with `android::sp<T>`, break cycles with `android::wp<T>` and `promote()`. NDK binder objects: `ndk::SharedRefBase::make<T>(...)` returning `std::shared_ptr`.
- JNI: register methods in `JNI_OnLoad` with `RegisterNatives`; cache `jclass` as a global ref and `jmethodID`/`jfieldID` there; return the JNI version.
```cpp
extern "C" JNIEXPORT jint JNI_OnLoad(JavaVM* vm, void*) {
    JNIEnv* env;
    if (vm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) != JNI_OK) return JNI_ERR;
    jclass c = env->FindClass("com/example/Native");
    if (c == nullptr) return JNI_ERR;
    static const JNINativeMethod kMethods[] = {{"nativeInit", "(J)Z", (void*)nativeInit}};
    if (env->RegisterNatives(c, kMethods, std::size(kMethods)) != JNI_OK) return JNI_ERR;
    env->DeleteLocalRef(c);
    return JNI_VERSION_1_6;
}
```
- `JNIEnv*` is per-thread: never cache or share it. Native threads must `AttachCurrentThread` and `DetachCurrentThread` before exit; cache `JavaVM*` instead.
- After any JNI call that can throw, check `env->ExceptionCheck()` and return promptly; do not call most JNI functions with a pending exception.
- Free local refs in loops (`DeleteLocalRef`) or use `PushLocalFrame`/`PopLocalFrame`; promote to `NewGlobalRef` only when storing beyond the call, and delete it.
- Strings: `GetStringUTFChars` yields modified UTF-8; always `ReleaseStringUTFChars`. Wrap in an RAII helper (`ScopedUtfChars` from nativehelper in platform code).
- Soong: `cc_library`, `cc_binary`, `cc_test` with `srcs`, `shared_libs`, `static_libs`, `header_libs`, `cflags`; `test_suites: ["general-tests"]` (or the repo's suite) so atest/TradeFed finds tests.
- NDK AIDL backend: enable `backend: { ndk: { enabled: true } }` in `aidl_interface`, link the generated versioned `-ndk` library and `libbinder_ndk`; implement `Bn*` classes returning `ndk::ScopedAStatus::ok()` or `ScopedAStatus::fromServiceSpecificError(code)`.
- Errors in AOSP C++: `android::base::Result<T>` with `Error()`/`ErrnoError()` and `if (!res.ok()) return res.error();`. Mark functions whose result must be used `[[nodiscard]]`.
- Logging: `#define LOG_TAG "Foo"` before `#include <log/log.h>` and use `ALOGE/ALOGW/ALOGI/ALOGD/ALOGV`; or android-base `LOG(INFO)`, `PLOG(ERROR)` (appends errno), `CHECK()` only for real invariants. NDK apps: `__android_log_print`.
- Thread safety: `std::mutex` + `std::lock_guard`; annotate with `GUARDED_BY`/`REQUIRES` (android-base/thread_annotations.h) where the codebase does; `std::atomic` for single counters/flags; never hold a lock while calling out (binder, JNI, callbacks).

## Avoid
- Raw `new`/`delete` and manual `free` paths -> smart pointers/RAII.
- Undefined behavior: signed overflow, out-of-bounds, use-after-free, dangling `string_view`/`c_str()`, uninitialized reads, strict-aliasing violations via casts, data races, shifting by >= width -> fix the root cause, do not silence the sanitizer.
- Storing `JNIEnv*`, local refs, or `jclass` from `FindClass` across calls/threads -> global refs and `JavaVM*`.
- `FindClass` on a natively created thread (system class loader cannot see app classes) -> cache class in `JNI_OnLoad`.
- Ignoring return values (`write`, `read`, binder status, `Result`) -> check and propagate; handle `EINTR` with `TEMP_FAILURE_RETRY`.
- `-Wno-*` or `// NOLINT` to pass `-Werror` -> fix the code; if unavoidable, scope and justify.
- Blocking binder threads on long work or holding locks across binder calls (deadlocks) -> offload, copy data out of lock.

## Commands
- AOSP (bash, after `source build/envsetup.sh && lunch <target>`): `m <module>`, `mm` in a module dir, `atest <test_module>`, `atest --host <test>` for host-supported tests.
- clang-tidy in Soong: `tidy: true` / `tidy_checks` in the module, or `WITH_TIDY=1 m <module>`.
- Sanitizers in Soong: `sanitize: { address: true }` / `hwaddress: true` / `integer_overflow: true` / `misc_undefined: [...]`; whole-device: `SANITIZE_TARGET=hwaddress m` (arm64).
- App/NDK: `./gradlew :app:externalNativeBuildDebug` or `./gradlew :app:assembleDebug` (PowerShell: `.\gradlew.bat`); CMake directly: `cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=$ANDROID_NDK/build/cmake/android.toolchain.cmake -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-26 && cmake --build build`.
- NDK sanitizers: add `-fsanitize=address` (or `hwaddress` on arm64 with a supporting device) and `-fno-omit-frame-pointer` to compile and link flags.
- Tombstones: `adb bugreport` or `adb shell ls /data/tombstones` (root); symbolize with `ndk-stack -sym <dir with unstripped .so> -dump tombstone.txt` (apps) or `development/scripts/stack` / `llvm-symbolizer` with `out/target/product/<dev>/symbols` (AOSP).

## Verify before COMPLETED
- Module builds with `-Werror` and no new warnings; clang-tidy clean on touched files where enabled.
- gtests pass; new logic covered including error paths; report command and pass counts.
- Ownership and lifetimes documented for new pointers/handles; no leaks of JNI global refs or fds.
- Locks: no lock held across external calls; guarded members annotated where the codebase does.
- If memory/UB-sensitive code changed, run under ASan/HWASan/UBSan if available and report result, or state it was not run.
- No new cross-partition or non-NDK dependencies; Android.bp/CMake diffs listed.
