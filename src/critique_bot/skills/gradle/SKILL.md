---
name: gradle
description: Gradle and AGP builds - wrapper usage, variants, version catalogs, JDK/toolchain, version compatibility, KSP, dependency conflicts, caches.
keywords: gradle, gradlew, gradlew.bat, gradle wrapper, android gradle plugin, agp, build.gradle.kts, settings.gradle.kts, libs.versions.toml, version catalog, kotlin dsl, buildtypes, productflavors, build variant, ksp, kapt, java toolchain, java_home, unsupported class file major version, duplicate class, dependencyinsight, configuration cache, gradle daemon, compilesdk, namespace
files: gradlew, gradlew.bat, settings.gradle.kts, settings.gradle, build.gradle.kts, build.gradle, gradle/libs.versions.toml, gradle/wrapper/gradle-wrapper.properties, gradle.properties
---
# Gradle and Android Gradle Plugin

## How to reason
- Read in order: gradle/wrapper/gradle-wrapper.properties (Gradle version), settings.gradle(.kts) (pluginManagement, repositories, included modules), root build file, gradle/libs.versions.toml, gradle.properties, then the failing module's build file.
- Identify the failing phase: initialization (settings), configuration (plugin/DSL errors), or execution (a task: compile, kapt/ksp, dex, lint, test). The first error in --console=plain output is usually the real one.
- Check version compatibility before upgrading anything: AGP requires a minimum Gradle and JDK (AGP 8.x needs JDK 17+); Kotlin Gradle plugin version must support the Gradle/AGP version; KSP version is tied to Kotlin version; Compose compiler is the org.jetbrains.kotlin.plugin.compose plugin with Kotlin 2.x.
- Decide which JDK runs Gradle (JAVA_HOME / org.gradle.java.home / IDE setting) vs which JDK compiles (toolchain). "Unsupported class file major version N" means a tool runs on an older JDK than the bytecode it reads (61=17, 65=21).
- Prefer the smallest change: one dependency version, one exclude, one property. Do not mass-upgrade.
- Match the existing DSL (Kotlin vs Groovy) and the existing use of the version catalog.

## Do
- Always use the wrapper: `./gradlew` (bash) or `.\gradlew.bat` (PowerShell/cmd). Add `--console=plain` for readable logs.
- Modern plugin and module setup (Kotlin DSL):
```kotlin
plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.kotlin.android)
  alias(libs.plugins.ksp)
}
android {
  namespace = "com.example.app"
  compileSdk = 35
  defaultConfig { applicationId = "com.example.app"; minSdk = 24; targetSdk = 35 }
  buildTypes { release { isMinifyEnabled = true
    proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro") } }
  compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
}
kotlin { jvmToolchain(17) }
dependencies { implementation(libs.androidx.core.ktx); ksp(libs.room.compiler) }
```
- Version catalog entries in gradle/libs.versions.toml under [versions], [libraries], [plugins]; reference with libs.x.y (dashes become dots). Use BOMs (platform(libs.compose.bom)) for library families.
- namespace in the module build file, not package= in the manifest (removed in AGP 8).
- Flavors: flavorDimensions += "tier"; productFlavors { create("free") { dimension = "tier" } }; tasks become e.g. assembleFreeDebug.
- Resolve conflicts with evidence: dependencyInsight, then a constraint or explicit version; use exclude(group, module) for true duplicate-class cases (e.g. old support lib vs androidx, kotlin-stdlib-jdk7/jdk8 merged into stdlib).
- Migrate kapt to KSP where the processor supports it (Room, Hilt/Dagger, Moshi); keep kapt only for processors without KSP.
- Enable org.gradle.configuration-cache=true and org.gradle.caching=true in gradle.properties only if the build passes with them; fix reported incompatibilities rather than disabling silently.
- Set JVM memory in gradle.properties: org.gradle.jvmargs=-Xmx4g -Dfile.encoding=UTF-8.
- Proxy/offline: systemProp.https.proxyHost / proxyPort in gradle.properties (user-level ~/.gradle/gradle.properties for credentials); --offline only when caches are populated.

## Avoid
- Calling a global `gradle` binary -> wrapper only; change versions via `./gradlew wrapper --gradle-version <v>` (updates properties and scripts).
- Downloading a JDK, Android SDK, or Gradle distribution into the repo, or committing local.properties / sdk.dir -> configure JAVA_HOME / ANDROID_HOME outside the repo; toolchain auto-provisioning stores JDKs in the Gradle user home.
- Using `compile`, `jcenter()`, `buildscript { classpath }` for new plugins -> implementation/api, mavenCentral()/google(), plugins {} block.
- Hardcoding versions in module files when a catalog exists.
- Forcing versions with resolutionStrategy.force across the board -> targeted constraints.
- Deleting ~/.gradle wholesale as a first step -> try --refresh-dependencies or deleting the specific cache/transforms dir; on Windows stop daemons first.
- Running clean before every build -> only when stale outputs are suspected.
- Mixing jvmTarget and Java target versions -> keep Kotlin jvmTarget equal to compileOptions target (toolchain does this).

## Commands
```bash
./gradlew --version                                     # Gradle, Kotlin, JVM in use
./gradlew tasks --all
./gradlew :app:assembleDebug --console=plain --stacktrace
./gradlew :app:assembleDebug --info                     # or --scan for a build scan (uploads data; ask first)
./gradlew :app:dependencies --configuration debugRuntimeClasspath
./gradlew :app:dependencyInsight --dependency okhttp --configuration debugRuntimeClasspath
./gradlew build --refresh-dependencies
./gradlew --stop                                        # stop daemons (Windows file-lock fixes)
```
PowerShell: `.\gradlew.bat :app:assembleDebug --console=plain`; set JDK for the session with `$env:JAVA_HOME="C:\Path\To\jdk-17"`. Windows issues: "Unable to delete file" / locked build dirs -> `.\gradlew.bat --stop`, close IDE indexing, retry; keep project paths short and avoid spaces/non-ASCII; long-path errors need LongPathsEnabled. bash: `export JAVA_HOME=/path/to/jdk-17`.

## Verify before COMPLETED
- `./gradlew --version` shows the expected Gradle and JVM.
- The originally failing task now succeeds with --console=plain; no new deprecation turned into an error.
- dependencyInsight shows the intended resolved version; no duplicate-class or R8 missing-class errors in release build.
- Unit tests for affected modules pass; a clean build (or CI-equivalent task) succeeds if build logic changed.
- No JDK/SDK/distribution files, local.properties, or build outputs added to the repo.
