---
name: kotlin
description: Idiomatic Kotlin 2.x: null safety, sealed/value classes, coroutines and Flow, Java interop, KSP, ktlint/detekt and testing.
keywords: kotlin, .kt, coroutine, coroutines, suspend fun, viewmodelscope, lifecyclescope, dispatchers, withcontext, supervisorjob, stateflow, sharedflow, flow, statein, collectlatest, repeatonlifecycle, sealed class, data class, value class, mockk, runtest, ksp, kapt, ktlint, detekt, @jvmstatic
files: build.gradle.kts, settings.gradle.kts, src/main/kotlin, detekt.yml, .editorconfig
---
# Kotlin 2.x

## How to reason
- Check Kotlin version and plugins in `libs.versions.toml` / `build.gradle.kts`, the kotlinx.coroutines version, and whether ktlint/detekt configs exist. Follow `.editorconfig`.
- Which scope owns each coroutine you launch, and what cancels it? If you cannot name the owner, the design is wrong.
- Which dispatcher does each piece of work run on? Is blocking IO isolated with `withContext(Dispatchers.IO)`? Is the function main-safe?
- Is this stream cold (Flow) or hot (StateFlow/SharedFlow)? Who collects it, and does collection stop when the UI is not visible?
- Can this value be null at runtime (platform types from Java, deserialization, Android APIs)? Read the Java signature/annotations.
- Is the state immutable and exposed read-only (`StateFlow`, `List`) while mutated privately?
- Find existing tests and the test dispatcher setup (e.g. a MainDispatcherRule) before adding coroutine tests.

## Do
- Prefer `val`, read-only collection types, and `copy()` on data classes. Expose `StateFlow` backed by private `MutableStateFlow`:
```kotlin
private val _state = MutableStateFlow(UiState())
val state: StateFlow<UiState> = _state.asStateFlow()
fun onRefresh() = viewModelScope.launch {
    _state.update { it.copy(loading = true) }
    try {
        val items = repo.load()           // main-safe suspend fun
        _state.update { it.copy(loading = false, items = items) }
    } catch (e: IOException) {            // specific type; CancellationException propagates
        _state.update { it.copy(loading = false, error = e.message) }
    }
}
```
- Null safety: `?.`, `?:`, `let` for scoped non-null use, `requireNotNull(x) { "msg" }` / `checkNotNull` for invariants with a message.
- Model states/results with sealed interfaces; use `when` as an expression without `else` so new subtypes are compile errors.
- `@JvmInline value class UserId(val raw: String)` for type-safe IDs without allocation in most paths.
- Extension functions for focused helpers on types you do not own; keep them near usage, not a global grab-bag.
- Scope functions sparingly: `apply` for configuration, `also` for side effects, `let` for nullable chains; never nest them deeply.
- Structured concurrency: launch in `viewModelScope`, `lifecycleScope`, or an injected `CoroutineScope` with a defined lifetime. Use `coroutineScope {}` for parallel decomposition that fails together, `supervisorScope {}`/`SupervisorJob()` when children fail independently.
- Main-safety: suspend functions that do IO switch internally with `withContext(ioDispatcher)`; inject dispatchers for testability.
- Cancellation: cooperative; call `ensureActive()` or suspend points in long loops; clean up in `finally`, using `withContext(NonCancellable)` only for short cleanup that must suspend.
- Exceptions: `try/catch` inside the coroutine, or `CoroutineExceptionHandler` on root `launch` only; `async` exceptions surface at `await()`.
- Flow: `flowOn` to change upstream context; `stateIn(scope, SharingStarted.WhileSubscribed(5_000), initial)` to share expensive upstream; `collectLatest` when only the newest value matters; `distinctUntilChanged` on derived state.
- Android UI collection: `lifecycleScope.launch { repeatOnLifecycle(Lifecycle.State.STARTED) { flow.collect { ... } } }` (Compose: `collectAsStateWithLifecycle()`).
- SharedFlow for events with explicit `replay`/`extraBufferCapacity`; or a `Channel` consumed once.
- Java interop: `@JvmStatic` on companion members called from Java, `@JvmOverloads` for default args, `@JvmField` for constants, `@Throws` when Java callers must catch checked exceptions. Treat platform types as nullable unless annotated.
- Prefer KSP over kapt for processors that support it (Room, Moshi, Hilt via KSP); kapt is in maintenance mode.
- Tests: `runTest {}` with `StandardTestDispatcher`/`UnconfinedTestDispatcher`, `Dispatchers.setMain` in setup and `resetMain` in teardown, `advanceUntilIdle()`. MockK: `coEvery`/`coVerify` for suspend functions. Turbine if the repo uses it for Flow.

## Avoid
- `!!` -> handle null explicitly or `requireNotNull` with a message.
- `GlobalScope` or orphan `CoroutineScope(Dispatchers.IO).launch` -> owned scope.
- `runBlocking` in production/UI code -> suspend or launch in a scope; only in `main` or tests where appropriate.
- Catching `Exception`/`Throwable` (or `runCatching`) in suspend code without rethrowing `CancellationException` -> catch specific types, or `catch (e: CancellationException) { throw e }` first.
- Hard-coded `Dispatchers.IO` in classes under test -> inject a dispatcher.
- `Thread.sleep` in coroutines -> `delay`.
- `lateinit var` for values that may legitimately be absent -> nullable or constructor injection.
- Exposing `MutableStateFlow`/`MutableList` publicly -> read-only types.
- Mutable collections inside StateFlow values (no emission on mutation) -> new immutable instance per update.
- `launchWhenStarted`/`launchWhenResumed` (deprecated) -> `repeatOnLifecycle`.
- `stateIn` with `SharingStarted.Eagerly` in ViewModels by default -> `WhileSubscribed(5_000)` unless eager is required.
- Data classes with `var` properties used as map keys -> `val`.
- Long scope-function chains with `it` shadowing -> named locals.
- Reformatting untouched code or violating ktlint rules the repo enforces.

## Commands
- Build/test: `./gradlew build`, `./gradlew test`, Android: `./gradlew :module:testDebugUnitTest --tests "com.example.FooTest"`. PowerShell: `.\gradlew.bat ...`.
- Lint: `./gradlew ktlintCheck` or `ktlint "src/**/*.kt"` (whichever the repo configures); `./gradlew detekt`.
- Formatting fix (only on files you touched): `./gradlew ktlintFormat` or `ktlint -F <file>`.
- Compiler warnings: check build output for new `w:` lines; respect `allWarningsAsErrors` if set.
- AOSP: `m <module>`, `atest <TestModule>`; ktfmt via preupload hooks where configured.

## Verify before COMPLETED
- Builds with no new compiler warnings; ktlint/detekt clean for touched files.
- No new `!!`, `GlobalScope`, or `runBlocking` in production code.
- Every launched coroutine has an owning scope; cancellation propagates; CancellationException never swallowed.
- Flows collected lifecycle-aware in UI; hot flows exposed read-only.
- Unit tests using `runTest` cover success, failure, and cancellation where relevant; report the exact command and results.
- Java callers (if any) still compile; interop annotations added where needed.
