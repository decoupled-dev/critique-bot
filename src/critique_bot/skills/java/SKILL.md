---
name: java
description: Modern Java 17/21 and AOSP-style Java: types, null handling, exceptions, concurrency, collections, builds and JUnit tests.
keywords: java, javac, jdk 17, jdk 21, record, sealed interface, completablefuture, executorservice, try-with-resources, optional, @nullable, @nonnull, hashcode, junit, junit5, maven, pom.xml, mvn, synchronized, volatile, java.util.concurrent, stream api, generics, errorprone
files: pom.xml, build.gradle, src/main/java, mvnw, Android.bp
---
# Java (17/21, Android/AOSP)

## How to reason
- Check the language level first: `maven.compiler.release`, `java { toolchain }`, `sourceCompatibility`, or Android `compileOptions`. Android code may not support records/switch patterns depending on AGP/desugaring; AOSP Soong Java has its own `java_version`.
- Read neighboring classes before writing: naming (mField vs field), nullability annotations in use (androidx.annotation, javax/jakarta, JSpecify, org.jetbrains), logging library, test framework (JUnit 4 vs 5).
- Who owns this object, which threads touch it, and what guards each mutable field? Write the answer down before editing shared state.
- Is this a public API? Changing signatures, checked exceptions, or nullability breaks callers; search all usages first.
- What are the failure modes: which exceptions can escape, which resources must be closed, what happens on interrupt?
- Is equality/identity relied on (HashMap keys, Sets, caches)? Then equals/hashCode must be consistent and fields immutable.
- Find the existing test for the class and extend it rather than creating a parallel one.

## Do
- Use records for immutable data carriers; validate in the compact constructor and copy mutable inputs:
```java
record Range(int lo, int hi, List<String> tags) {
    Range {
        if (lo > hi) throw new IllegalArgumentException("lo > hi");
        tags = List.copyOf(tags);
    }
}
```
- Model closed hierarchies with sealed interfaces + records and exhaustive switch (Java 21) with no `default`, so new subtypes fail compilation:
```java
sealed interface Shape permits Circle, Square {}
double area(Shape s) {
    return switch (s) {
        case Circle c -> Math.PI * c.r() * c.r();
        case Square q -> q.side() * q.side();
    };
}
```
- Optional only as a return type for "may be absent". Not for fields, parameters, or collections. Use `orElseThrow()`, `map`, `orElseGet` (lazy) rather than `get()`.
- Annotate nullability consistently with the repo (Android: `@Nullable`/`@NonNull` from androidx.annotation or android.annotation in AOSP). Check `Objects.requireNonNull(arg, "arg")` at public API boundaries.
- Prefer `final` fields and unmodifiable collections (`List.of`, `Map.copyOf`, `Collections.unmodifiableList` for views). Return defensive copies of internal mutable state.
- equals/hashCode: override together, use the same fields, use `Objects.equals`/`Objects.hash`; `getClass()` vs `instanceof` deliberately. Records generate both.
- Exceptions: checked for recoverable caller-actionable conditions, unchecked for programming errors. Wrap with cause: `throw new IOException("read " + path, e)`. Restore interrupt: `catch (InterruptedException e) { Thread.currentThread().interrupt(); ... }`.
- Always close resources with try-with-resources (streams, cursors, channels, ParcelFileDescriptor, locks via try/finally).
- Concurrency: use java.util.concurrent (ConcurrentHashMap, AtomicInteger, CountDownLatch) over hand-rolled wait/notify. Guard compound actions with one lock; `volatile` only for single-variable visibility. Document guards (`@GuardedBy("mLock")` in AOSP).
- ExecutorService: own its lifecycle; `shutdown()` then `awaitTermination`. Give threads names via a ThreadFactory. Java 21: virtual threads (`Executors.newVirtualThreadPerTaskExecutor()`) for blocking IO, not CPU-bound work.
- CompletableFuture: pass an explicit executor to `*Async` methods; handle failure with `exceptionally`/`handle`/`whenComplete`; `join()` throws CompletionException wrapping the cause.
- Generics: no raw types; PECS (`? extends T` to read, `? super T` to write); avoid unchecked casts, and if unavoidable, narrow `@SuppressWarnings("unchecked")` to one statement with a reason.
- AOSP style: `mMember` for non-public non-static fields, `sStatic` for static, constants `UPPER_SNAKE`, 100-column limit, 4-space indent, no wildcard imports, import order per repo. Use `Slog`/`Log` with a `TAG` constant.
- JUnit 5: `@Test`, `@BeforeEach`, `assertThrows`, `@ParameterizedTest`. JUnit 4: `@Test`, `@Before`, `@RunWith`, `assertThrows` (4.13+). Do not mix in one class.

## Avoid
- Swallowing exceptions (`catch (Exception e) {}` or log-and-continue on invariant violations) -> handle, rethrow wrapped, or document why ignoring is safe.
- Catching `Throwable`/`Error` broadly -> catch the specific types.
- `Optional.get()` without check, `Optional` fields, returning `null` from an Optional-returning method -> `orElseThrow`, plain nullable field, `Optional.empty()`.
- Mutating a collection while iterating -> `removeIf` or Iterator.remove.
- `Arrays.asList` treated as resizable; `List.of` with nulls (NPE) -> pick the right factory.
- Stream misuse: side effects in `map`/`filter`, reusing a stream, `parallel()` without measurement, `Collectors.toMap` on duplicate keys (throws) -> supply merge function.
- `stream().toList()` returns unmodifiable list; do not mutate it.
- Double-checked locking without `volatile`; synchronizing on `this` or on a boxed/string literal -> private final lock object.
- `HashMap` shared across threads -> ConcurrentHashMap or external locking.
- Ignoring `Future` results (lost exceptions) -> join/get or attach a handler.
- Wildcard imports, reformatting untouched lines.

## Commands
- Maven: `mvn -q -B verify` (compile + tests), single test: `mvn -B -Dtest=FooTest#bar test`. Wrapper: `./mvnw` (bash) / `.\mvnw.cmd` (PowerShell).
- Gradle: `./gradlew build`, `./gradlew test --tests "com.example.FooTest"`; PowerShell: `.\gradlew.bat test --tests "com.example.FooTest"`.
- javac warnings: add `-Xlint:all` (Maven `compilerArgs`, Gradle `options.compilerArgs`); treat new warnings as regressions. Error Prone if configured.
- Android modules: `./gradlew :module:testDebugUnitTest`, `./gradlew :module:lint`.
- AOSP: `m <module>`, `atest <TestModule>` or `atest FooTest#testBar`.

## Verify before COMPLETED
- Code compiles with no new javac/lint warnings on the configured language level.
- Relevant tests run and pass; new behavior has a test covering success and failure paths; report the exact command and pass counts.
- Every resource opened is closed; every caught exception is handled, wrapped, or justified.
- Shared mutable state has a named guard; no new data races introduced.
- equals/hashCode/compareTo remain consistent for changed value types.
- Public API changes listed explicitly with impacted callers updated.
- Style matches the file (naming, imports, 100 columns in AOSP).
