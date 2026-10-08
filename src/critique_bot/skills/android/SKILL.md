---
name: android
description: Android app development - architecture, lifecycle, manifest, permissions, coroutines, Hilt, Room, WorkManager, R8, ANRs and leaks.
keywords: android, androidmanifest, activity, fragment, viewmodel, viewmodelscope, lifecyclescope, livedata, stateflow, hilt, dagger, room database, workmanager, pendingintent, runtime permission, targetsdk, minsdk, proguard, r8, anr, memory leak, foreground service, broadcastreceiver, contentprovider, intent filter, savedstatehandle, mvvm, mvi, repository pattern
files: AndroidManifest.xml, app/src/main/AndroidManifest.xml, src/main/AndroidManifest.xml, proguard-rules.pro, app/proguard-rules.pro
---
# Android App Development

## How to reason
- Read the module's build.gradle(.kts) first: minSdk, targetSdk, compileSdk, namespace, plugins (Hilt, KSP, Compose, Room). targetSdk decides which platform behavior changes apply.
- Read AndroidManifest.xml (merged manifest if available: app/build/intermediates/merged_manifests/) before touching components, permissions, or intent filters.
- Identify the layer: UI (Activity/Fragment/Composable) -> ViewModel (state holder) -> Repository (single source of truth) -> data sources (Room, network, DataStore). Put the fix in the lowest layer that owns the bug.
- Ask: does this survive configuration change (rotation, locale, dark mode, multi-window) and process death? Where does the state live?
- Ask: which thread/dispatcher runs this? Main thread work > ~5s on input -> ANR; disk/network on main -> StrictMode violation or jank.
- For anything touching other apps or the system: exported flag, permission, PendingIntent mutability, package visibility (<queries>).
- Follow existing project patterns (DI style, state type, navigation) rather than introducing new libraries.

## Do
- Expose UI state as immutable StateFlow from the ViewModel; collect with lifecycle awareness:
```kotlin
private val _state = MutableStateFlow(UiState())
val state: StateFlow<UiState> = _state.asStateFlow()
// Fragment/Activity
viewLifecycleOwner.lifecycleScope.launch {
  viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) { vm.state.collect(::render) }
}
// Compose: val s by vm.state.collectAsStateWithLifecycle()
```
- Launch ViewModel work in viewModelScope; switch to Dispatchers.IO / Default inside the repository (main-safe suspend functions). Inject dispatchers for testability.
- Persist small UI state across process death with SavedStateHandle; persist data in Room/DataStore.
- Hilt: @HiltAndroidApp on Application, @AndroidEntryPoint on Activity/Fragment/Service, @HiltViewModel + @Inject constructor, modules with @InstallIn(SingletonComponent::class) and @Binds for interfaces.
- Room: DAOs return Flow<T> for observation and use suspend for one-shot; provide Migration objects on schema version bump; exportSchema = true with room.schemaLocation. Use KSP, not kapt.
- WorkManager for deferrable guaranteed work (CoroutineWorker, constraints, unique work names with ExistingWorkPolicy). Long-running user-visible work: foreground service with a declared foregroundServiceType (required at targetSdk 34+) and matching FOREGROUND_SERVICE_* permission.
- Set android:exported explicitly on every component with an intent-filter (required since targetSdk 31). Default to exported="false".
- PendingIntent: always specify FLAG_IMMUTABLE unless the receiver must fill in extras (inline reply, bubbles), then FLAG_MUTABLE with an explicit Intent.
- Runtime permissions via ActivityResultContracts.RequestPermission / RequestMultiplePermissions; handle denial and shouldShowRequestPermissionRationale. POST_NOTIFICATIONS needs runtime grant at targetSdk 33+.
- Context-registered receivers at targetSdk 34+: pass RECEIVER_EXPORTED or RECEIVER_NOT_EXPORTED (ContextCompat.registerReceiver).
- Android 15 (targetSdk 35): edge-to-edge is enforced; handle WindowInsets. Android 16: large-screen orientation/resizability restrictions ignored for sw600dp+ - do not rely on locked orientation.
- Resources: strings in res/values/strings.xml with plurals and placeholders; qualifiers (values-night, values-sw600dp, values-<lang>, drawable-<density>); never hardcode user-visible text or dimensions in code.
- R8: add keep rules for reflection-based code (serialization, JNI, classes referenced by name); prefer library-provided consumer rules. Test release builds with minify enabled.

## Avoid
- Holding Activity/View/Context in ViewModel, singletons, or static fields -> leak. Use applicationContext or AndroidViewModel only when needed.
- GlobalScope or unmanaged CoroutineScope -> use viewModelScope, lifecycleScope, or an injected application scope.
- runBlocking on the main thread -> make the call suspend.
- Collecting flows in lifecycleScope.launch without repeatOnLifecycle -> wasted work in background, crashes on view access.
- Using fragment `this` as LifecycleOwner for view observation -> use viewLifecycleOwner; null out view binding in onDestroyView.
- Storing secrets/API keys in code or resources -> they ship in the APK.
- Implicit intents for internal broadcasts -> use explicit intents with setPackage, or in-process mechanisms.
- Bumping targetSdk without reviewing that version's behavior changes list.
- Adding android:configChanges to suppress recreation -> fix state handling instead.
- Blanket `-keep class ** { *; }` -> write narrow keep rules.
- Requesting permissions not needed (QUERY_ALL_PACKAGES, MANAGE_EXTERNAL_STORAGE) -> Play policy rejects; use <queries> and scoped storage/MediaStore/SAF.

## Commands
```bash
./gradlew :app:assembleDebug --console=plain          # Windows: .\gradlew.bat :app:assembleDebug
./gradlew :app:lintDebug                               # report in app/build/reports/
./gradlew :app:testDebugUnitTest
./gradlew :app:assembleRelease                         # verify R8/minify
./gradlew :app:processDebugMainManifest               # merged manifest under app/build/intermediates
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n <applicationId>/<fully.qualified.Activity>
adb logcat -v color --pid=$(adb shell pidof -s <applicationId>)   # PowerShell: $pid1 = adb shell pidof -s <id>; adb logcat --pid=$pid1
adb shell am kill <applicationId>   # simulate process death while app is in background
```
Note: R8 mapping at app/build/outputs/mapping/release/mapping.txt; retrace with the retrace tool from cmdline-tools.

## Verify before COMPLETED
- Project builds for the changed variant; lint shows no new errors (exported, permissions, MissingPermission, NewApi).
- Unit tests for changed ViewModel/Repository pass; add one if behavior changed.
- Rotation and process death preserve state (Don't Keep Activities developer option or am kill).
- No main-thread disk/network (StrictMode in debug), no new leaks on screen exit.
- Manifest: every exported component intentional; PendingIntent flags set; foreground service types declared.
- If R8-sensitive code changed, assembleRelease builds and the release app runs the path.
