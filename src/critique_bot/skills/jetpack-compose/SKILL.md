---
name: jetpack-compose
description: Jetpack Compose UI: state hoisting, side effects, stability/recomposition, Material 3, navigation, previews, accessibility and UI tests.
keywords: jetpack compose, compose, @composable, remember, remembersaveable, mutablestateof, derivedstateof, launchedeffect, disposableeffect, sideeffect, recomposition, lazycolumn, modifier, material3, navhost, navigation-compose, collectasstatewithlifecycle, @preview, createcomposerule, testtag, compose bom, semantics
files:
---
# Jetpack Compose

## How to reason
- Read the version catalog/build file first: Compose BOM version, Kotlin version, and how the compiler is applied. With Kotlin 2.0+, Compose needs the `org.jetbrains.kotlin.plugin.compose` Gradle plugin (version = Kotlin version), not `composeOptions.kotlinCompilerExtensionVersion`.
- Who owns this state? Lift it to the lowest common ancestor that reads or writes it; screen-level state belongs in a ViewModel.
- What triggers recomposition here, and how often? Are parameters stable? Is a frequently changing value read too high in the tree?
- Does this side effect need to restart when inputs change? Pick effect keys deliberately.
- Must this state survive configuration change or process death? (`rememberSaveable`/`SavedStateHandle` vs `remember`.)
- Is the component usable by TalkBack and in large font/dark theme?
- Look at existing screens: theme object, design-system components, navigation setup, test tags convention, and preview annotations in use.

## Do
- Stateless composables: take state and event lambdas, accept `modifier: Modifier = Modifier` as the first optional parameter and apply it to the root:
```kotlin
@Composable
fun Counter(count: Int, onIncrement: () -> Unit, modifier: Modifier = Modifier) {
    Button(onClick = onIncrement, modifier = modifier) { Text("Count: $count") }
}
```
- Screen entry collects ViewModel state lifecycle-aware and passes plain values down:
```kotlin
@Composable
fun HomeRoute(vm: HomeViewModel = hiltViewModel()) {   // or viewModel()
    val state by vm.state.collectAsStateWithLifecycle()
    HomeScreen(state = state, onRefresh = vm::refresh)
}
```
- `remember { }` for objects across recompositions; `rememberSaveable` for user input/selection that must survive recreation; key `remember(key)` when the value depends on inputs.
- `derivedStateOf` when a state changes more often than the derived result (e.g. `listState.firstVisibleItemIndex > 0`); wrap in `remember`.
- Side effects: `LaunchedEffect(key)` for suspend work tied to composition and keys; `DisposableEffect(key)` for register/unregister with `onDispose`; `rememberCoroutineScope()` for launching from callbacks; `SideEffect` to publish state to non-Compose objects after each successful recomposition; `rememberUpdatedState` for latest lambda inside long-lived effects.
- Stability: use immutable UI models (`data class` of `val` with immutable types; `kotlinx.collections.immutable` or `@Immutable`/`@Stable` where the contract truly holds). Strong skipping (default in recent compiler) still compares unstable params by instance; avoid recreating them.
- Lazy lists: provide `key = { it.id }` and `contentType` for heterogeneous items; never put a Lazy list inside a vertically scrolling parent of unbounded height.
- Defer fast-changing reads: use lambda modifiers (`Modifier.offset { IntOffset(x, 0) }`, `graphicsLayer { alpha = a }`) so animation reads happen in layout/draw, not composition.
- Modifier order matters: it applies outside-in. `padding().background()` differs from `background().padding()`; put `clickable` before padding to enlarge touch target as intended.
- Material 3: `MaterialTheme.colorScheme`, `typography`, `shapes`; dynamic color on Android 12+ if the app uses it; no hard-coded colors or text sizes in feature UI.
- Navigation-compose: pass IDs not objects; use type-safe routes (`@Serializable` destinations, Navigation 2.8+) if the project does; ViewModels read args via `SavedStateHandle`.
- Previews: `@Preview` on stateless composables with fake data; cover light/dark and font scale via multipreview annotations if present. Keep previews private and out of production paths.
- Accessibility: `contentDescription` for meaningful icons (null for decorative), `Modifier.semantics { }` / `mergeDescendants`, `Role`, min 48dp touch targets, avoid conveying state by color alone.
- UI tests: `createComposeRule()` (or `createAndroidComposeRule<Activity>()`), find by `onNodeWithText`/`onNodeWithTag`, `performClick()`, `assertIsDisplayed()`; add `Modifier.testTag("...")` matching repo naming.

## Avoid
- Passing ViewModel or `MutableState` deep into children -> pass values and lambdas.
- Side effects (network, logging, writing state) directly in composable body -> effect APIs or ViewModel.
- `LaunchedEffect(Unit)` when work depends on inputs -> key on those inputs; `LaunchedEffect(true)` re-run assumptions.
- Writing state read in the same composition (backwards write, infinite recomposition) -> derive or move to event.
- `collectAsState()` for Android UI -> `collectAsStateWithLifecycle()`.
- `mutableStateOf(mutableListOf())` and mutating the list -> `mutableStateListOf` or replace with new immutable list.
- Allocating objects/lambdas capturing unstable values in hot paths, sorting/filtering in composition -> `remember(input)` or precompute in ViewModel.
- Lazy items without stable keys (lost state, wrong animations) -> `key`.
- `GlobalScope` or `viewModelScope` usage from composables -> `rememberCoroutineScope`/ViewModel functions.
- Old compiler wiring (`kotlinCompilerExtensionVersion`) on Kotlin 2.x -> compose compiler Gradle plugin.
- Mismatched Compose artifact versions -> rely on the BOM, no explicit versions on BOM-managed artifacts.

## Commands
- Build: `./gradlew :app:assembleDebug` (PowerShell: `.\gradlew.bat :app:assembleDebug`).
- Unit tests: `./gradlew :app:testDebugUnitTest`; Robolectric Compose tests run here if configured.
- Instrumented UI tests (device/emulator required): `./gradlew :app:connectedDebugAndroidTest`; single class: `-Pandroid.testInstrumentationRunnerArguments.class=com.example.FooTest`.
- Lint: `./gradlew :app:lintDebug` (Compose lint checks ship with the libraries).
- Stability report (if needed): set `composeCompiler { reportsDestination = layout.buildDirectory.dir("compose_reports") }` and inspect `*-classes.txt`/`*-composables.txt`.

## Verify before COMPLETED
- Project compiles; no new lint warnings in touched files.
- State ownership clear: screen state from ViewModel via `collectAsStateWithLifecycle`; children stateless.
- Effects keyed correctly; no work in composition body.
- Lazy lists keyed; no obvious unstable params in hot composables.
- Previews compile and render for new components.
- UI or Robolectric test covers the new behavior; report command and result, or state that a device was unavailable for instrumented tests.
- Accessibility: content descriptions, roles, and touch target sizes checked.
