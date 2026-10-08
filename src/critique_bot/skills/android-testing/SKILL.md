---
name: android-testing
description: Android unit and instrumented testing - JUnit, Espresso, Compose UI tests, Robolectric, mocks, coroutine/Flow tests, Hilt tests, flake fixes.
keywords: espresso, androidx test, androidjunit4, instrumented test, connectedandroidtest, testdebugunittest, robolectric, uiautomator, compose ui test, createcomposerule, testtag, mockk, mockito, runtest, standardtestdispatcher, unconfinedtestdispatcher, maindispatcherrule, turbine, hiltandroidtest, idlingresource, test orchestrator, flaky test, atest, junit5
files: src/test/**, src/androidTest/**, src/sharedTest/**, TEST_MAPPING, AndroidTest.xml
---
# Android Testing

## How to reason
- Decide the cheapest level that proves the behavior: local JVM unit test (src/test) > Robolectric (src/test with Android framework) > instrumented (src/androidTest, device/emulator). Pyramid: many unit, fewer integration, few end-to-end UI.
- Read existing tests and test utilities first (rules, fakes, base classes, HiltTestRunner) and match their style and libraries (MockK vs Mockito, JUnit4 vs 5, Truth vs AssertJ).
- Check build.gradle: testInstrumentationRunner, testOptions (unitTests.isIncludeAndroidResources, execution ANDROIDX_TEST_ORCHESTRATOR), and which test deps exist before using an API.
- Ask what the test must control: time, dispatchers, network, clock, randomness, device state. Untamed sources cause flakes.
- Prefer fakes for your own interfaces (repositories, data sources); mock only boundaries you do not own.
- For a failing test, read the full failure and stack trace before editing; determine whether the test or the code is wrong.

## Do
- Name tests by behavior: `loadUser_whenNetworkFails_emitsError()` or backticked Kotlin names; structure Arrange / Act / Assert with one behavior per test.
- Coroutines: use runTest; replace Main with a rule; inject dispatchers into production code.
```kotlin
class MainDispatcherRule(val d: TestDispatcher = UnconfinedTestDispatcher()) : TestWatcher() {
  override fun starting(description: Description) = Dispatchers.setMain(d)
  override fun finished(description: Description) = Dispatchers.resetMain()
}
@get:Rule val main = MainDispatcherRule()
@Test fun refresh_updatesState() = runTest {
  val vm = MyViewModel(FakeRepo(), StandardTestDispatcher(testScheduler))
  vm.refresh(); advanceUntilIdle()
  assertEquals(Loaded, vm.state.value)
}
```
- Flow: Turbine `flow.test { assertEquals(a, awaitItem()); cancelAndIgnoreRemainingEvents() }`; for StateFlow, collect in backgroundScope or test with Turbine.
- MockK: `coEvery { repo.load() } returns x`, `coVerify(exactly = 1) { ... }`, relaxed mocks sparingly. mockito-kotlin: `whenever(...)`, `verify(...)`.
- Espresso: `onView(withId(R.id.save)).perform(click()); onView(withText("Saved")).check(matches(isDisplayed()))`; lists with RecyclerViewActions.actionOnItemAtPosition; intents with Intents.init()/intended(hasComponent(...))/Intents.release() or IntentsRule.
- Synchronize with IdlingResource (CountingIdlingResource, OkHttp idling resource) or Espresso's main-looper idling; Compose uses waitUntil { } with a condition.
- Compose: `@get:Rule val rule = createComposeRule()` (or createAndroidComposeRule<Activity>()); find by semantics: onNodeWithText, onNodeWithContentDescription, onNodeWithTag (Modifier.testTag); assertIsDisplayed, performClick; use useUnmergedTree = true when needed.
- Hilt: @HiltAndroidTest, `@get:Rule(order = 0) val hilt = HiltAndroidRule(this)`, hilt.inject(), @TestInstallIn or @UninstallModules + @BindValue for replacements; a custom runner returning HiltTestApplication.
- Robolectric: @RunWith(AndroidJUnit4::class), @Config(sdk = [...]) only when needed; enable unitTests.isIncludeAndroidResources = true for resources.
- UiAutomator for cross-app/system UI (notifications, permission dialogs); grant permissions with GrantPermissionRule when the dialog is not under test.
- Orchestrator + clearPackageData for isolation in instrumented suites with shared state.
- AOSP: add tests to TEST_MAPPING presubmit; android_test / android_robolectric_test / cc_test modules with test_suites.

## Avoid
- Thread.sleep / SystemClock.sleep -> idling resources, waitUntil, or virtual time (advanceTimeBy).
- Real network, real time, real databases on disk in unit tests -> fakes, injected Clock, Room.inMemoryDatabaseBuilder.
- Asserting on implementation details or verifying every mock call -> assert observable outputs/state.
- Shared mutable state between tests (singletons, static fields) -> reset in @After or inject fresh instances.
- Tests dependent on order, locale, timezone, or animations -> disable animations on test devices, fix locale/timezone in setup.
- Mixing JUnit 5 annotations in instrumented tests without the junit5 plugin -> instrumented tests are JUnit 4 by default.
- Catching exceptions to make tests pass, or @Ignore to hide a failure -> fix the cause or explain.
- Mocking data classes, Flow, or Kotlin final classes with Mockito without mockito-inline (default in Mockito 5) -> use real values.

## Commands
```bash
./gradlew :app:testDebugUnitTest --console=plain                  # Windows: .\gradlew.bat :app:testDebugUnitTest
./gradlew :app:testDebugUnitTest --tests "com.example.FooTest"
./gradlew :app:testDebugUnitTest --tests "*FooTest.loadUser*"
./gradlew :app:connectedDebugAndroidTest                          # needs device/emulator
./gradlew :app:connectedDebugAndroidTest "-Pandroid.testInstrumentationRunnerArguments.class=com.example.FooTest"
adb shell am instrument -w -e class com.example.FooTest com.example.test/androidx.test.runner.AndroidJUnitRunner
adb shell settings put global animator_duration_scale 0   # also window_ and transition_animation_scale
atest FooTests; atest FooTests:com.example.FooTest#method; atest --iterations 20 FooTests   # AOSP
```
In PowerShell quote -P arguments containing '=' or dots as shown. Reports: build/reports/tests/ and build/outputs/androidTest-results/.

## Verify before COMPLETED
- New/changed tests fail without the fix and pass with it.
- The targeted test class passes, then the module's full test task passes.
- Run suspect tests repeatedly (loop or --iterations) to confirm no flake.
- No sleeps, no real network, no leaked Main dispatcher; resources reset in teardown.
- Instrumented tests actually ran on a device (check count in the report, not just BUILD SUCCESSFUL).
